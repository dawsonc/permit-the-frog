"""Load the raw Somerville permit data, filter, and save a clean version.

Steps:
1. Filter to residential
2. Strip down to just relevant categories
    - Application #, Application Date, Type/Subtype, Project Description, Address, Parcel #, Lat/Lon, Contractor Company Name
3. Process the project description to get a project type (for now, just regex on project description)
    - heat_pumps, electrical_panel, solar_pv, other_hvac, other
4. Merge with assessor data on LOC_ID (needs normalization)
    - YEAR_BUILT, RES_AREA, NUM_ROOMS, STYLE, USE_CODE, STORIES (noramlized)
"""

# Coding style: minimal abstraction for the MVP, clearly readable data pipeline. Callable from CLI. Add a makefile action when done
# Output to data/processed/ma/somerville/joined_data.csv

from __future__ import annotations

import argparse
import re
from pathlib import Path

import geopandas as gpd
import pandas as pd

DEFAULT_PERMIT_DIR = Path("data/raw/ma/somerville/permits")
DEFAULT_ASSESSOR_GDB = Path(
    "data/raw/ma/assessor/2026_09_06/M274_parcels_gdb/M274_parcels_CY25_FY25_sde.gdb"
)
DEFAULT_OUT = Path("data/processed/ma/somerville/joined_data.csv")

# The four construction trades. Every other Application Type in the file is a
# license (food, block parties, ...) with no occupancy class and no parcel work.
PERMIT_FAMILIES = ["Building Permit", "Electrical Permit", "Gas Fitting", "Plumbing Permit"]

# Raw permit column -> output name. Gas Fitting and Plumbing never populate
# Contractor Company Name, so Applicant Company Name is the only party on those.
PERMIT_COLS = {
    "Application Number": "application_number",
    "Application Date": "application_date",
    "Application Type": "application_type",
    "Application Subtype": "application_subtype",
    "Project Description or Business Name": "project_description",
    "Application Address": "address",
    "Assessor's Parcel Number": "apn",
    "Application Latitude": "latitude",
    "Application Longitude": "longitude",
    "Contractor Company Name": "contractor_company_name",
    "Applicant Company Name": "applicant_company_name",
}

ASSESS_COLS = {
    "PROP_ID": "prop_id",
    "LOC_ID": "loc_id",
    "YEAR_BUILT": "year_built",
    "RES_AREA": "res_area",
    "NUM_ROOMS": "num_rooms",
    "STYLE": "style",
    "USE_CODE": "use_code",
    "STORIES": "stories",
    "UNITS": "units",
}

# The assessor attributes carried onto a permit, plus the provenance of the match.
ASSESS_VALUE_COLS = [
    "prop_id", "loc_id", "year_built", "res_area", "num_rooms",
    "style", "use_code", "stories", "units", "assess_records", "match_level",
]
ASSESS_OUT_COLS = ["parcel_key", *ASSESS_VALUE_COLS]
PARCEL_OUT_COLS = ["parcel_prefix", *ASSESS_VALUE_COLS]

OUTPUT_COLS = [
    "application_number", "application_date", "application_type", "application_subtype",
    "project_description", "project_type", "address", "apn", "parcel_key", "parcel_prefix",
    "latitude", "longitude", "contractor_company_name", "applicant_company_name",
    "match_level", "assess_records", "loc_id", "prop_id",
    "year_built", "res_area", "num_rooms", "style", "use_code", "stories", "units",
]

# Ordered, first match wins. Order carries real weight here:
#  - "solar panels" would be caught by any bare `panel` rule, so solar_pv runs
#    first AND the panel patterns below all require a qualifier.
#  - bare `amp` is a substring of example/camp/ramp, so it must be word-bounded
#    and digit-prefixed.
PROJECT_TYPE_PATTERNS = [
    ("solar_pv", r"solar|photovoltaic|\bpv\b|kw\s*(?:dc|ac)\b"),
    ("heat_pumps", r"heat[\s-]?pump|mini[\s-]?split|ductless|\bashp\b|air[\s-]?source"),
    (
        "electrical_panel",
        r"(?:electrical|main|sub)[\s-]?panel"
        r"|panel\s+(?:upgrade|change|replace|swap)"
        r"|(?:upgrade|change|replace)\s+(?:the\s+)?panel"
        r"|service\s+(?:upgrade|change)|upgrade\s+(?:the\s+)?service|\bnew\s+service\b"
        # "200 amp", "200amps", and the "200A service" shorthand. The trailing
        # `s?` matters: `amp\b` alone silently misses every plural.
        r"|\b\d+\s*-?\s*amps?\b|\b\d+\s*a\s+(?:service|meter|panel)\b"
        r"|\bcircuit\s+breaker\b|load\s+cent(?:er|re)",
    ),
    (
        "other_hvac",
        r"furnace|boiler|hvac|air[\s-]?condition|condens[eo]r"
        r"|\bac\s+unit\b|\ba\s*/\s*c\b|\bcentral\s+air\b|\bair\s+handler\b"
        r"|\bheating\s+system\b|\bradiant\s+(?:heat|floor)\b"
        # Only baseboard *heat* -- bare "baseboard" is carpentry trim in ~53%
        # of the rows that mention it.
        r"|baseboard\s+heat|electric\s+baseboard"
        r"|duct\s?work|\brtu\b",
    ),
]
PROJECT_TYPE_REGEXES = [(label, re.compile(pat)) for label, pat in PROJECT_TYPE_PATTERNS]


def newest_permit_csv(directory: Path) -> Path:
    """Latest dated extract in the permits dir (filenames are YYYY_MM_DD.csv, so they sort)."""
    candidates = sorted(directory.glob("*.csv"))
    if not candidates:
        raise FileNotFoundError(f"No permit CSV found in {directory}. Run `make permitting-data`.")
    return candidates[-1]


def load_permits(path: Path) -> pd.DataFrame:
    """Read the raw extract as text, then convert only the columns we keep.

    Everything ships quoted, so reading as str first keeps pandas from silently
    coercing the money and flag columns we aren't using anyway.
    """
    df = pd.read_csv(path, dtype=str, low_memory=False)
    df = df[list(PERMIT_COLS)].rename(columns=PERMIT_COLS)
    df["application_date"] = pd.to_datetime(df["application_date"], errors="coerce")
    for col in ("latitude", "longitude"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


def filter_residential(df: pd.DataFrame) -> pd.DataFrame:
    """Keep residential permits in the four trade families.

    The subtype encodes occupancy differently per family -- "Residential",
    "Residential Repair", "Residential - Existing" -- but the class is always
    the first word of the first " - " segment.
    """
    df = df[df["application_type"].isin(PERMIT_FAMILIES)].copy()
    occupancy = df["application_subtype"].fillna("Unknown").str.split(" - ").str[0].str.split(" ").str[0]
    return df[occupancy == "Residential"].copy()


def classify_project(description: str | float) -> str:
    """Bucket a free-text project description into a project type. First match wins."""
    if not isinstance(description, str):
        return "other"
    text = description.lower()
    for label, regex in PROJECT_TYPE_REGEXES:
        if regex.search(text):
            return label
    return "other"


def normalize_parcel_id(value: str | float) -> str | None:
    """Reconcile permit and assessor parcel identifiers.

    Permits zero-pad and hyphenate ("004-B-00044-000000"); the assessor uses
    unpadded underscores ("4_B_44", "102_E_17_M" where the last segment is a
    condo unit). Stripping the padding and dropping segments that become empty
    puts both on the same key. Junk values ("", "103 a 13") return None so they
    miss the join rather than matching something wrong.
    """
    if not isinstance(value, str):
        return None
    segments = [s.lstrip("0") for s in re.split(r"[-_]", value.strip())]
    return "-".join(s for s in segments if s) or None


def parcel_prefix(keys: pd.Series) -> pd.Series:
    """The map-block-lot prefix of a normalized parcel key, dropping any unit suffix.

    "1-E-21-527" -> "1-E-21", so a permit filed against a condo unit and one
    filed against the building as a whole land on the same parcel.
    """
    return keys.str.split("-").str[:3].str.join("-")


def load_assessor(gdb_path: Path) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Read the MassGIS L3 assessor tables, keyed for joining.

    Returns (unit-level assessments, parcel-level aggregates). Condo and
    multi-unit buildings only appear in M274Assess as individual units
    ("42_B_4_101", ...), so a permit filed against the parcel as a whole
    ("042-B-00004-000000") only matches once those units are rolled up.
    """
    assess = gpd.read_file(gdb_path, layer="M274Assess")[list(ASSESS_COLS)].rename(columns=ASSESS_COLS)
    # STORIES is CAMA free text but happens to be clean decimals; 0 is a
    # missing-value sentinel (mostly condo units), not a real building.
    assess["stories"] = pd.to_numeric(assess["stories"], errors="coerce").replace(0, pd.NA)
    assess["parcel_key"] = assess["prop_id"].map(normalize_parcel_id)

    units = assess.dropna(subset=["parcel_key"]).drop_duplicates("parcel_key", keep="first").copy()
    units["assess_records"] = 1
    units["match_level"] = "unit"

    # Route 1: roll unit rows up by their map-block-lot prefix, dropping the
    # unit suffix. Derived from PROP_ID alone, so it works for the ~900 condo
    # parcels that M274TaxPar has no polygon for.
    by_prefix = _aggregate_parcels(assess, parcel_prefix(assess["parcel_key"]))

    # Route 2: for parcels the prefix misses, the polygon layer maps a parcel
    # id onto the LOC_ID its unit rows share. FEE parcels are the real
    # ownership polygons, so prefer them where a key is duplicated.
    taxpar = gpd.read_file(gdb_path, layer="M274TaxPar")[["MAP_PAR_ID", "LOC_ID", "POLY_TYPE"]]
    taxpar = taxpar.sort_values("POLY_TYPE", key=lambda s: s.ne("FEE"), kind="stable")
    taxpar["parcel_prefix"] = parcel_prefix(taxpar["MAP_PAR_ID"].map(normalize_parcel_id))
    taxpar = taxpar.dropna(subset=["parcel_prefix"]).drop_duplicates("parcel_prefix", keep="first")
    by_loc_id = taxpar[["parcel_prefix", "LOC_ID"]].rename(columns={"LOC_ID": "loc_id"}).merge(
        _aggregate_parcels(assess, assess["loc_id"]).drop(columns="parcel_prefix"), on="loc_id"
    )

    parcels = pd.concat([by_prefix, by_loc_id], ignore_index=True)
    parcels = parcels.drop_duplicates("parcel_prefix", keep="first")  # prefix route wins
    parcels["match_level"] = "parcel"
    return units[ASSESS_OUT_COLS], parcels[PARCEL_OUT_COLS]


def _aggregate_parcels(assess: pd.DataFrame, keys: pd.Series) -> pd.DataFrame:
    """Roll unit rows up to one row per parcel, grouped by `keys`.

    Areas and room counts sum to whole-building totals; year and stories take
    the median and the categoricals the mode. These are building aggregates,
    not per-unit values -- `match_level` marks which rows they are.
    """
    mode = lambda s: s.mode().iloc[0] if not s.mode().empty else None  # noqa: E731
    return (
        assess.assign(parcel_prefix=keys)
        .dropna(subset=["parcel_prefix"])
        .groupby("parcel_prefix", as_index=False)
        .agg(
            prop_id=("prop_id", "first"),
            loc_id=("loc_id", mode),
            year_built=("year_built", "median"),
            res_area=("res_area", "sum"),
            num_rooms=("num_rooms", "sum"),
            style=("style", mode),
            use_code=("use_code", mode),
            stories=("stories", "median"),
            units=("units", "sum"),
            assess_records=("prop_id", "size"),
        )
    )


def join_assessor(permits: pd.DataFrame, units: pd.DataFrame, parcels: pd.DataFrame) -> pd.DataFrame:
    """Match a unit-level assessment first, then fall back to the parcel aggregate."""
    joined = permits.merge(units, on="parcel_key", how="left")

    missing = joined["match_level"].isna()
    fallback = joined.loc[missing, ["parcel_prefix"]].merge(parcels, on="parcel_prefix", how="left")
    joined.loc[missing, ASSESS_VALUE_COLS] = fallback[ASSESS_VALUE_COLS].to_numpy()
    return joined


def process(permits_path: Path, gdb_path: Path) -> pd.DataFrame:
    permits = load_permits(permits_path)
    print(f"Loaded {len(permits):,} rows from {permits_path}")

    permits = filter_residential(permits)
    print(f"  {len(permits):,} residential permits in {', '.join(PERMIT_FAMILIES)}")

    permits["project_type"] = permits["project_description"].map(classify_project)
    permits["parcel_key"] = permits["apn"].map(normalize_parcel_id)
    permits["parcel_prefix"] = parcel_prefix(permits["parcel_key"])

    units, parcels = load_assessor(gdb_path)
    print(f"  {len(units):,} unit-level and {len(parcels):,} parcel-level assessor records")

    joined = join_assessor(permits, units, parcels)

    matched = joined["match_level"].notna()
    print(f"\nAssessor match rate: {matched.mean():.1%} ({matched.sum():,} of {len(joined):,})")
    print(joined["match_level"].value_counts(dropna=False).to_string())
    print("\nProject types:")
    print(joined["project_type"].value_counts().to_string())
    return joined


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--permits", type=Path, default=None,
        help=f"Permit CSV (default: newest in {DEFAULT_PERMIT_DIR})",
    )
    parser.add_argument("--assessor-gdb", type=Path, default=DEFAULT_ASSESSOR_GDB)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args()

    permits_path = args.permits or newest_permit_csv(DEFAULT_PERMIT_DIR)
    joined = process(permits_path, args.assessor_gdb)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    joined[OUTPUT_COLS].to_csv(args.out, index=False)
    print(f"\nWrote {len(joined):,} rows x {len(OUTPUT_COLS)} columns to {args.out}")


if __name__ == "__main__":
    main()
