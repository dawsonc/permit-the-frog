"""Load the raw Somerville permit data, filter, and save a clean version.

Steps:
1. Filter to residential
2. Strip down to just relevant categories
    - Application #, Application Date, Type/Subtype, Project Description, Address, Parcel #, Lat/Lon, Contractor Company Name
3. Process the project description to get a project type (for now, just regex on project description)
    - one boolean column each: solar_pv, heat_pumps, heat_pump_water_heater,
      water_heater, cooking, ev_charger, electrical_panel, other_hvac, ess
    - solar_kw and ess_kwh carry system sizes where the description states one
    - flags are independent, so a permit can carry several; all-false is the old "other"
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

# Each pattern is evaluated INDEPENDENTLY and becomes its own boolean column, so
# a job that upgrades the service *and* hangs an EV charger flags both. Nothing
# is ordered and nothing wins over anything else, which puts the whole weight of
# precision on the patterns themselves:
#  - bare `panel` would match "42 solar panels installed", so every panel
#    pattern requires a qualifier ("main panel", "panel upgrade", ...). There is
#    no longer an earlier solar rule shadowing it.
#  - bare `amp` is a substring of example/camp/ramp, so it must be word-bounded
#    and digit-prefixed. The trailing `s?` matters just as much: `amp\b` alone
#    silently misses every plural.
# List order is cosmetic -- it only fixes the output column order.
PROJECT_TYPE_PATTERNS = [
    (
        "solar_pv",
        # Velux sells "solar powered" skylights and "solar blinds" on roofing
        # permits; the lookahead keeps them out. It deliberately does NOT block
        # "solar power" -- that is how genuine PV rows describe themselves
        # ("install solar power system, 30 panels, 11.4 kW DC").
        r"solar(?![\s\w]{0,15}(?:skylight|blind|tube|shade|powered))"
        r"|photovoltaic|\bpv\b|kw\s*(?:dc|ac)\b",
    ),
    (
        # Space conditioning only. A heat pump water heater is a different end
        # use and gets its own flag, so bare "heat pump" is blocked when a water
        # heater follows it directly. The lookahead is deliberately tight: at
        # "Heat Pump and gas water heater" those are two separate appliances.
        "heat_pumps",
        r"heat[\s-]?pump(?!\s*(?:style|electric|hybrid)?\s*(?:hot\s+)?water\s+heater)"
        r"|mini[\s-]?split|ductless|\bashp\b|air[\s-]?source",
    ),
    (
        "heat_pump_water_heater",
        r"heat[\s-]?pump[\s\w]{0,12}water\s+heater|hybrid[\s\w]{0,15}water\s+heater"
        r"|\bhpwh\b|water\s+heater[\s\w]{0,15}heat[\s-]?pump|heat\s+pump\s+style",
    ),
    # Any water heating work, fossil or electric -- the denominator that
    # heat_pump_water_heater is the electrified slice of.
    (
        "water_heater",
        r"water\s+heater|water\s+htr|\bwh\b|hot\s+water\s+(?:tank|heater)"
        r"|tankless|\bhpwh\b|indirect\s+(?:water\s+)?(?:heater|tank)",
    ),
    (
        # "range hood" is ventilation and a wood stove is heating, so both are
        # excluded. Induction appears on just 2 permits, so gas-vs-electric
        # cooking is not separable from this data.
        "cooking",
        r"(?<!wood\s)stove|\brange\b(?!\s+hood)|cook\s?top|\boven\b|induction|\bcooking\b",
    ),
    (
        "ev_charger",
        # "wall connector" is Tesla's product name and is unambiguous here.
        # Bare `tesla` is not -- it also sells Powerwall batteries and solar --
        # so it is only matched next to "charger".
        r"\bev\b|\be\.v\.|electric\s+vehicle|\bevse\b|chargepoint"
        r"|car\s+charger|charging\s+station|wall\s+connector"
        r"|tesla\s+charger|level\s*2\s+charger",
    ),
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
PROJECT_TYPE_COLS = [label for label, _ in PROJECT_TYPE_PATTERNS] + ["ess"]

OUTPUT_COLS = [
    "application_number", "application_date", "application_type", "application_subtype",
    "project_description", *PROJECT_TYPE_COLS, "solar_kw", "ess_kwh",
    "address", "apn", "parcel_key", "parcel_prefix",
    "latitude", "longitude", "contractor_company_name", "applicant_company_name",
    "match_level", "assess_records", "loc_id", "prop_id",
    "year_built", "res_area", "num_rooms", "style", "use_code", "stories", "units",
]


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


# System size, e.g. "7.1 kW DC", "13.975KW", "5.33 KWDC", "4.16 kW-DC".
# DC is the nameplate rating and is preferred: descriptions that quote both
# ("36.26 kW DC / 25.00 kW AC") should yield the DC figure, and an amended
# description ("CHANGED TO 2.46 kWDC ... Install 3.280 kW panels") should yield
# the correction rather than the superseded number, so the first DC match wins.
# `(?!h)` keeps kWh battery capacity out -- 23 solar rows also quote storage.
SOLAR_KW_DC = r"(\d+(?:[.,]\d+)?)\s*kw\s*-?\s*dc"
SOLAR_KW_ANY = r"(\d+(?:[.,]\d+)?)\s*kw(?!h)"
ESS_KWH = r"(\d+(?:[.,]\d+)?)\s*kwh"

# Residential arrays run ~1-15 kW and the largest real one here is a 287 kW
# multifamily roof. Anything outside this window is a misread rather than a
# system -- "Install SE 10,000 KW" is an inverter model number, not 10 MW.
SOLAR_KW_RANGE = (0.5, 500.0)

# Energy storage. The hard part is negation: 193 of the 229 permits that say
# "ESS" say "No ESS", and most of the rest of the battery mentions are smoke
# detectors and emergency lights. So negated phrases are stripped out first,
# and a bare "battery" only counts on a permit that is already solar.
ESS_NEGATED = (
    r"\bno\s*[-/]?\s*(?:ess\b|batter\w*|energy\s+storage)"
    # "No Battery ESS", "No Battery/ESS", "No ESS & battery" all disclaim both.
    r"(?:[\s/&]+(?:ess\b|batter\w*|energy\s+storage))*"
)
ESS_STRONG = r"\bess\b|energy\s+storage|powerwall|storage\s+system|encharge"
ESS_BATTERY = r"\bbatter"


def _parse_number(raw: pd.Series) -> pd.Series:
    """Parse a captured figure, handling both comma conventions.

    Applicants write both "4,76 kW" (decimal comma) and "10,000 KW" (thousands),
    so strip a comma that groups three digits and treat any other as a decimal
    point. Left naive, "4,76 kW" reads as 76.
    """
    cleaned = raw.str.replace(r",(\d{3})\b", r"\1", regex=True)
    return pd.to_numeric(cleaned.str.replace(",", ".", regex=False), errors="coerce")


def extract_solar_kw(descriptions: pd.Series, is_solar: pd.Series) -> pd.Series:
    """System size in kW for solar permits; NA where absent or not a solar job.

    Masked to solar rows because kW is quoted all over the file for things that
    are not arrays -- heat strips, electric coils, battery systems.
    """
    text = descriptions.fillna("").str.lower()
    size = _parse_number(
        text.str.extract(SOLAR_KW_DC, expand=False).fillna(
            text.str.extract(SOLAR_KW_ANY, expand=False)
        )
    )
    return size.where(is_solar & size.between(*SOLAR_KW_RANGE))


def extract_ess_kwh(descriptions: pd.Series, has_ess: pd.Series) -> pd.Series:
    """Storage capacity in kWh; NA where absent or the permit has no storage."""
    text = descriptions.fillna("").str.lower()
    size = _parse_number(text.str.extract(ESS_KWH, expand=False))
    return size.where(has_ess)


def flag_project_types(descriptions: pd.Series) -> pd.DataFrame:
    """One boolean column per project type; a permit can carry several, or none.

    A job that upgrades the service and hangs an EV charger is genuinely both,
    so the flags are independent rather than a single winning label. A row with
    every flag false is the old "other" bucket -- overwhelmingly descriptions
    that never name any equipment ("renovation", "rewire 3 units").

    The one dependency between columns: `other_hvac` means what its name says,
    HVAC that is *not* a heat pump. Left independent it fires on 500 heat-pump
    rows, because a mini-split is itself a condenser and an air handler.

    `heat_pump_water_heater` is a strict subset of `water_heater` (the end use)
    and is kept out of `heat_pumps` (space conditioning) by that pattern's own
    lookahead, so the three answer different questions and can be summed safely.
    """
    text = descriptions.fillna("").str.lower()
    flags = pd.DataFrame(
        {label: text.str.contains(pattern, regex=True) for label, pattern in PROJECT_TYPE_PATTERNS},
        index=descriptions.index,
    )
    flags["other_hvac"] &= ~flags["heat_pumps"]

    # Storage is scored on text with the "No ESS" disclaimers removed.
    scrubbed = text.str.replace(ESS_NEGATED, " ", regex=True)
    flags["ess"] = scrubbed.str.contains(ESS_STRONG, regex=True) | (
        scrubbed.str.contains(ESS_BATTERY, regex=True) & flags["solar_pv"]
    )
    return flags


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

    permits[PROJECT_TYPE_COLS] = flag_project_types(permits["project_description"])
    permits["solar_kw"] = extract_solar_kw(permits["project_description"], permits["solar_pv"])
    permits["ess_kwh"] = extract_ess_kwh(permits["project_description"], permits["ess"])
    permits["parcel_key"] = permits["apn"].map(normalize_parcel_id)
    permits["parcel_prefix"] = parcel_prefix(permits["parcel_key"])

    units, parcels = load_assessor(gdb_path)
    print(f"  {len(units):,} unit-level and {len(parcels):,} parcel-level assessor records")

    joined = join_assessor(permits, units, parcels)

    matched = joined["match_level"].notna()
    print(f"\nAssessor match rate: {matched.mean():.1%} ({matched.sum():,} of {len(joined):,})")
    print(joined["match_level"].value_counts(dropna=False).to_string())

    tagged = joined[PROJECT_TYPE_COLS].sum(axis=1)
    print("\nProject type flags (a permit can carry several):")
    for col in PROJECT_TYPE_COLS:
        print(f"  {col:<17} {joined[col].sum():>6}")
    print(f"  {'-- untagged --':<17} {(tagged == 0).sum():>6}")
    print(f"  {'-- 2+ flags --':<17} {(tagged > 1).sum():>6}")

    kw = joined.loc[joined["solar_pv"], "solar_kw"]
    print(f"\nSolar size: {kw.notna().sum():,} of {len(kw):,} solar permits carry a kW figure "
          f"(median {kw.median():.2f} kW, max {kw.max():.1f})")
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
