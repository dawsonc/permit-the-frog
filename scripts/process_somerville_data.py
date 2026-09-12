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
5. Write a second, filtered table for analysis (FILTER_PARAMS, from notebook 02)
    - joined_data.csv stays the source of truth; joined_filtered_data.csv is derived
"""

# Coding style: minimal abstraction for the MVP, clearly readable data pipeline. Callable from CLI. Add a makefile action when done
# Output to data/processed/ma/somerville/joined_data.csv

from __future__ import annotations

import argparse
import re
from collections import Counter
from pathlib import Path

import geopandas as gpd
import pandas as pd

DEFAULT_PERMIT_DIR = Path("data/raw/ma/somerville/permits")
DEFAULT_ASSESSOR_GDB = Path(
    "data/raw/ma/assessor/2026_09_06/M274_parcels_gdb/M274_parcels_CY25_FY25_sde.gdb"
)
DEFAULT_OUT = Path("data/processed/ma/somerville/joined_data.csv")
DEFAULT_FILTERED_OUT = Path("data/processed/ma/somerville/joined_filtered_data.csv")
DEFAULT_FRONTEND_OUT = Path("frontend/data")

# Analysis filters, ported from section 6 of
# notebooks/02_somerville_processed_summary.ipynb. The unfiltered table stays the
# source of truth; this is written alongside it so the decision stays reversible.
SOM_BBOX = {"lat": (42.37, 42.42), "lon": (-71.14, -71.07)}

FILTER_PARAMS = dict(
    # --- structural: the row can't support attribute-level analysis ---
    require_assessor_match=True,   # drop match_level.isna()
    require_res_area=True,         # drop res_area == 0 (assessor sentinel)
    require_year_built=True,       # drop year_built.isna()

    # --- scope: what counts as a "building" for your question ---
    max_assess_records=6,          # e.g. 5 to keep 1-3 family; None = keep all
    unit_matches_only=False,       # True = drop the parcel-level aggregates entirely

    # --- value ranges ---
    year_built_range=(1630, 2027),
    res_area_range=(200, None),    # None = no upper bound
    num_rooms_max=None,            # e.g. 30

    # --- date coverage ---
    date_range=(None, None),       # e.g. ("2015-01-01", "2025-12-31")

    # --- geography ---
    require_in_bbox=True,
)

# --- Frontend export (frontend/assets/data_spec.md) ------------------------
# A de-identified projection for the static page. The page answers "what do
# people with a house like yours pay?", matching on property attributes and
# never on identity of place, so nothing here may join a permit back to an
# address -- see verify_deidentified().

# Only permits carrying one of these reach the page; the rest name no equipment.
FRONTEND_FLAGS = {
    "solar_pv": "solar", "heat_pumps": "hp", "heat_pump_water_heater": "hpwh",
    "ev_charger": "ev", "electrical_panel": "panel", "other_hvac": "hvac", "ess": "ess",
}

# Both emitted files must bucket on the SAME grid: addresses.json attributes are
# compared against permits.json attributes, so a mismatch silently matches nothing.
YEAR_BUCKET = 10     # decade
AREA_BUCKET = 500    # sq ft

# MA state class code (first three digits of use_code) -> facet label.
PROP_CLASS = {
    "101": "Single family", "102": "Condominium", "104": "Two-family",
    "105": "Three-family", "109": "Multiple houses on one parcel",
    "111": "Apartments", "112": "Apartments",
}
PROP_CLASS_OTHER = "Other / mixed use"

DESC_MAX_CHARS = 200

# An electrical permit groups with the building permit it wires when the two are
# this close. Pair coverage rises steeply to ~14 days (26% of heat-pump building
# permits at 7d, 35% at 14d) and then flattens -- 30d and 90d both land on the
# same median -- so a wider window only adds false pairings.
GROUP_WINDOW_DAYS = 14

# What a collapsed group calls itself. Emitted only in the medians projection --
# the page derives the same label from the trades of the rows it is showing.
MERGED_TRADE = "Building + Electrical"

# Ceiling on how many permits one group may hold, asserted before writing. A
# group is meant to be a job and its wiring; anything much larger means the
# clustering has run together work that only shares a parcel and a fortnight.
MAX_GROUP_SIZE = 6

# Two permits for one project either split the work or restate it, and the cost
# ratio says which. A heat pump's electrical permit is a median 6% of its
# building permit -- genuinely just the wiring, so the two add up. Solar's is a
# median 59%, and 158 pairs carry *identical* costs: that is the same array
# filed twice, where adding would double the project. Below this ratio the
# electrical permit is treated as a sub-scope and summed; at or above it the
# two are treated as restatements and only the larger is kept.
SUBSCOPE_RATIO = 0.33

# Street-type words, longest first so "street" wins over "st" in the alternation.
STREET_SUFFIX_MAP = {
    "street": "st", "avenue": "ave", "road": "rd", "parkway": "pkwy",
    "terrace": "terr", "boulevard": "blvd", "place": "pl", "court": "ct",
    "highway": "hwy", "drive": "dr", "lane": "ln", "square": "sq",
}

# Columns that must never appear in the emitted payload, by substring. Each one
# individually reconstitutes the address column (spec section 5).
FORBIDDEN_COL_PARTS = ("addr", "parcel", "lat", "lon", "loc_id", "prop_id", "apn", "id")

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
    "Estimated Construction Cost": "estimated_construction_cost",
    "Status": "status",
    "Application Neighborhood": "neighborhood",
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
#    pattern requires a qualifier ("main panel", "electrical panel", ...). There is
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
    "status", "neighborhood", "estimated_construction_cost",
    "project_description", *PROJECT_TYPE_COLS, "solar_kw", "ess_kwh", "zones",
    "address", "apn", "parcel_key", "parcel_prefix",
    "latitude", "longitude", "company_name", "company_source",
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
    # Money ships as "450,000". Strip the separators and any currency symbol;
    # blanks and anything unparseable become NaN rather than 0, since "not
    # stated" and "cost nothing" are different facts. Gas Fitting and Plumbing
    # never populate this column at all.
    df["estimated_construction_cost"] = pd.to_numeric(
        df["estimated_construction_cost"].str.replace(r"[$,]", "", regex=True).str.strip(),
        errors="coerce",
    )
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


def merge_company_names(df: pd.DataFrame) -> tuple[pd.Series, pd.Series]:
    """Coalesce the two party columns into one, contractor first.

    Only Building and Electrical permits populate Contractor Company Name; on
    Gas Fitting and Plumbing it is empty on every row and Applicant Company Name
    is the only party named. Reading the contractor column alone therefore looks
    like a third of the file has no company, when really two of the four trades
    record it in the other field.

    Both are stripped first: values carry trailing spaces ("Primus Company "),
    which otherwise makes a blank read as present.

    Returns (company_name, company_source) -- the second says which column the
    name came from, so "who did the work" analyses can still tell a contractor
    from a self-filing applicant.
    """
    contractor, applicant = (
        df[col].str.strip().replace("", pd.NA) for col in
        ("contractor_company_name", "applicant_company_name")
    )
    name = contractor.fillna(applicant)
    source = pd.Series(pd.NA, index=df.index, dtype="object")
    source[applicant.notna()] = "applicant"
    source[contractor.notna()] = "contractor"
    return name, source


# System size, e.g. "7.1 kW DC", "13.975KW", "5.33 KWDC", "4.16 kW-DC".
# DC is the nameplate rating and is preferred: descriptions that quote both
# ("36.26 kW DC / 25.00 kW AC") should yield the DC figure, and an amended
# description ("CHANGED TO 2.46 kWDC ... Install 3.280 kW panels") should yield
# the correction rather than the superseded number, so the first DC match wins.
# `(?!h)` keeps kWh battery capacity out -- 23 solar rows also quote storage.
# `[.,]+` rather than `[.,]`: a typo like "5..50 kW" would otherwise fail to
# match at the "5", and the engine would retry at the "50" and capture 50.
SOLAR_KW_DC = r"(\d+(?:[.,]+\d+)?)\s*kw\s*-?\s*dc"
SOLAR_KW_ANY = r"(\d+(?:[.,]+\d+)?)\s*kw(?!h)"
ESS_KWH = r"(\d+(?:[.,]+\d+)?)\s*kwh"

# Residential arrays run ~1-15 kW and the largest real one here is a 287 kW
# multifamily roof. Anything outside this window is a misread rather than a
# system -- "Install SE 10,000 KW" is an inverter model number, not 10 MW.
SOLAR_KW_RANGE = (0.5, 500.0)

# Energy storage. The hard part is negation: 193 of the 229 permits that say
# "ESS" say "No ESS", and most of the rest of the battery mentions are smoke
# detectors and emergency lights. So negated phrases are stripped out first,
# and a bare "battery" only counts on a permit that is already solar.
ESS_NEGATED = (
    # Bare "storage" has to be in here, not just "energy storage": applicants
    # write "No storage batteries" and "no storage system", and without it the
    # disclaimer is missed and then matched as if it were real storage.
    r"\bno\s*[-/]?\s*(?:ess\b|batter\w*|(?:energy\s+)?storage(?:\s+(?:system|batter\w*))?)"
    # "No Battery ESS", "No Battery/ESS", "No ESS & battery" all disclaim both.
    r"(?:[\s/&]+(?:ess\b|batter\w*|(?:energy\s+)?storage))*"
)
ESS_STRONG = r"\bess\b|energy\s+storage|powerwall|storage\s+system|encharge"
ESS_BATTERY = r"\bbatter"

# Heat pump size proxy: how many indoor units the description names.
ZONE_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4,
              "five": 5, "six": 6, "seven": 7, "eight": 8}
ZONE_NOUN = r"(?:zones?|heads?|indoor\s+units?|mini[\s-]?splits?|fan\s+coils?|air\s+handlers?)"
# A number followed by one of these is a rating, not a count of anything.
ZONE_UNIT = r"(?:amps?|tons?|volts?|kw|k|btus?|seer|inch|ft|hour|hr)"
ZONES_RE = (
    rf"\b(\d{{1,2}}|{'|'.join(ZONE_WORDS)})\s+(?!{ZONE_UNIT}\b)(?:\w+\s+)?{ZONE_NOUN}"
)
ZONES_RANGE = (1, 20)


def _parse_number(raw: pd.Series) -> pd.Series:
    """Parse a captured figure, handling both comma conventions.

    Applicants write both "4,76 kW" (decimal comma) and "10,000 KW" (thousands),
    so strip a comma that groups three digits and treat any other as a decimal
    point. Left naive, "4,76 kW" reads as 76.
    """
    cleaned = raw.str.replace(r"[.,]{2,}", ".", regex=True)      # "5..50" -> "5.50"
    cleaned = cleaned.str.replace(r",(\d{3})\b", r"\1", regex=True)
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


def extract_zones(descriptions: pd.Series, is_heat_pump: pd.Series) -> pd.Series:
    """Indoor-unit count as a heat pump size proxy; NA where not stated.

    Tonnage would be the natural size measure but is not viable here -- only 56
    of the heat pump rows name tons and 17 give BTU. What descriptions do name
    is how many indoor units were hung ("3 zone ductless", "five mini splits",
    "4 ductless fan coils"), which tracks system size closely enough to use.

    One optional word may sit between the number and the noun, because that is
    how half these phrases read. The rating-unit guard is what makes that safe:
    without it "Install 25 amp mini split Disconnect" parses as 25 zones.
    """
    text = descriptions.fillna("").str.lower()
    raw = text.str.extract(ZONES_RE, expand=False)
    count = pd.to_numeric(
        raw.map(lambda v: ZONE_WORDS.get(v, v) if isinstance(v, str) else v),
        errors="coerce",
    )
    return count.where(is_heat_pump & count.between(*ZONES_RANGE))


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
    permits["zones"] = extract_zones(permits["project_description"], permits["heat_pumps"])
    permits["company_name"], permits["company_source"] = merge_company_names(permits)
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

    named = joined["company_name"].notna()
    print(f"\nCompany name: {named.mean():.1%} of permits name a party "
          f"({(~named).sum():,} name none)")
    print(joined["company_source"].value_counts(dropna=False).to_string())

    cost = joined["estimated_construction_cost"]
    print(f"\nConstruction cost: {cost.notna().sum():,} of {len(joined):,} permits state one "
          f"({cost.notna().mean():.1%})")
    print(f"  median ${cost.median():,.0f}   p99 ${cost.quantile(0.99):,.0f}   "
          f"max ${cost.max():,.0f}   zero-or-negative {int((cost <= 0).sum()):,}")

    z = joined.loc[joined["heat_pumps"], "zones"]
    print(f"\nHeat pump zones: {z.notna().sum():,} of {len(z):,} heat pump permits state an "
          f"indoor-unit count ({z.notna().mean():.0%}, median {z.median():.0f})")

    kw = joined.loc[joined["solar_pv"], "solar_kw"]
    print(f"\nSolar size: {kw.notna().sum():,} of {len(kw):,} solar permits carry a kW figure "
          f"(median {kw.median():.2f} kW, max {kw.max():.1f})")
    return joined


def build_filter_rules(frame: pd.DataFrame, p: dict) -> dict[str, pd.Series]:
    """Each entry is a mask of rows the rule would REMOVE. Disabled rules are omitted."""
    rules: dict[str, pd.Series] = {}
    empty = lambda: pd.Series(False, index=frame.index)  # noqa: E731

    if p["require_assessor_match"]:
        rules["no assessor match"] = frame["match_level"].isna()
    if p["require_res_area"]:
        rules["res_area == 0 (sentinel)"] = frame["res_area"].eq(0)
    if p["require_year_built"]:
        rules["year_built missing"] = frame["year_built"].isna()

    if p["max_assess_records"] is not None:
        rules[f"assess_records > {p['max_assess_records']}"] = (
            frame["assess_records"] > p["max_assess_records"]
        ).fillna(False)
    if p["unit_matches_only"]:
        rules["parcel-level aggregate"] = frame["match_level"].eq("parcel")

    lo, hi = p["year_built_range"]
    if lo is not None or hi is not None:
        mask = empty()
        if lo is not None:
            mask |= frame["year_built"].lt(lo).fillna(False)
        if hi is not None:
            mask |= frame["year_built"].gt(hi).fillna(False)
        rules[f"year_built outside {p['year_built_range']}"] = mask

    lo, hi = p["res_area_range"]
    if lo is not None or hi is not None:
        mask = empty()
        if lo is not None:
            mask |= frame["res_area"].lt(lo).fillna(False) & frame["res_area"].ne(0)
        if hi is not None:
            mask |= frame["res_area"].gt(hi).fillna(False)
        rules[f"res_area outside {p['res_area_range']}"] = mask

    if p["num_rooms_max"] is not None:
        rules[f"num_rooms > {p['num_rooms_max']}"] = (
            frame["num_rooms"].gt(p["num_rooms_max"]).fillna(False)
        )

    lo, hi = p["date_range"]
    if lo is not None or hi is not None:
        mask = empty()
        if lo is not None:
            mask |= frame["application_date"] < pd.Timestamp(lo)
        if hi is not None:
            mask |= frame["application_date"] > pd.Timestamp(hi)
        rules[f"date outside {p['date_range']}"] = mask

    if p["require_in_bbox"]:
        rules["lat/lon missing or outside bbox"] = ~(
            frame["latitude"].between(*SOM_BBOX["lat"])
            & frame["longitude"].between(*SOM_BBOX["lon"])
        ).fillna(False)

    return rules


def apply_filters(joined: pd.DataFrame, params: dict) -> pd.DataFrame:
    """Drop rows the filter rules flag, reporting what each rule cost."""
    rules = build_filter_rules(joined, params)
    dropped = pd.DataFrame(rules, index=joined.index)
    flagged = dropped.any(axis=1)
    solo = dropped.sum(axis=1) == 1

    print("\nFilter rules (rows removed):")
    for name in dropped.columns:
        print(f"  {name:<38} {dropped[name].sum():>6}   only: {(dropped[name] & solo).sum():>5}")

    kept = joined[~flagged]
    print(f"  {'-- kept --':<38} {len(kept):>6}   ({len(kept) / len(joined):.1%} of {len(joined):,})")

    # The bias check: a rule that eats one project type is changing the answer,
    # not cleaning the data. Compare each flag's removal rate to the overall one.
    overall = flagged.mean() * 100
    print(f"\nRemoval rate by project type (overall {overall:.1f}%):")
    for col in PROJECT_TYPE_COLS:
        before = joined[col].sum()
        if before:
            rate = (joined[col] & flagged).sum() / before * 100
            print(f"  {col:<24} {rate:>5.1f}%  ({rate - overall:+.1f} pp)")
    return kept


def bucket(values: pd.Series, width: int) -> pd.Series:
    """Floor to a multiple of `width`, preserving NA."""
    return (values // width) * width


def label_prop_class(use_code: pd.Series) -> pd.Series:
    """MA state class code -> facet label. The clean alternative to `style`."""
    return use_code.astype("string").str[:3].map(PROP_CLASS).fillna(PROP_CLASS_OTHER)


def load_addresses(gdb_path: Path) -> pd.DataFrame:
    """One row per building address, rolled up from the unit-level assessor table.

    Read separately from load_assessor so SITE_ADDR never enters the analysis
    CSV's column set. Condo buildings contribute many unit rows per address, so
    attributes are aggregated to a representative dwelling (median), not summed.
    """
    cols = ["SITE_ADDR", "ADDR_NUM", "YEAR_BUILT", "RES_AREA", "STORIES", "USE_CODE"]
    raw = gpd.read_file(gdb_path, layer="M274Assess")[cols]

    addr = (raw["SITE_ADDR"].fillna("").str.replace(r"#.*$", "", regex=True)
            .str.replace(r"\s+", " ", regex=True).str.strip())
    # Vacant parcels and paper streets. The sentinel is not always a bare "0":
    # the file also carries "0R RUTHERFORD AVE" and "0000R WEST ST", so test the
    # leading number's value rather than string-comparing ADDR_NUM.
    lead = pd.to_numeric(addr.str.extract(r"^(\d+)", expand=False), errors="coerce")
    keep = (addr != "") & addr.str.match(r"^\d") & lead.gt(0)

    frame = pd.DataFrame({
        "addr": addr[keep],
        "year_built": pd.to_numeric(raw.loc[keep, "YEAR_BUILT"], errors="coerce"),
        "res_area": pd.to_numeric(raw.loc[keep, "RES_AREA"], errors="coerce"),
        "stories": pd.to_numeric(raw.loc[keep, "STORIES"], errors="coerce").replace(0, pd.NA),
        "prop_class": label_prop_class(raw.loc[keep, "USE_CODE"]),
    })
    mode = lambda s: s.mode().iloc[0] if not s.mode().empty else None  # noqa: E731
    return frame.groupby("addr", as_index=False).agg(
        year_built=("year_built", "median"),
        res_area=("res_area", "median"),
        stories=("stories", "median"),
        prop_class=("prop_class", mode),
    )


def normalize_address(addr: pd.Series) -> pd.Series:
    """Lowercase, collapse whitespace, and abbreviate street types.

    Somerville's assessor file is entirely abbreviated ("MAIN ST"), so a visitor
    typing "Main Street" matches nothing under any matcher unless both sides are
    mapped onto the same vocabulary. app.js must apply this same map to the query.
    """
    out = addr.str.lower().str.replace(r"\s+", " ", regex=True).str.strip()
    for long, short in STREET_SUFFIX_MAP.items():
        out = out.str.replace(rf"\b{long}\b", short, regex=True)
    return out


def build_address_scrubber(addresses: pd.DataFrame) -> re.Pattern:
    """A regex matching '<number> <real Somerville street>' in free text.

    Built from the 697 street names actually in the assessor file rather than a
    generic '<number> <word> <suffix>' pattern, which over-matches ordinary
    prose ("3 Story Ave"). Each street is matched in both its abbreviated and
    spelled-out form, since applicants write "75 myrtle street".
    """
    streets = (addresses["addr"].str.replace(r"^\d+[A-Za-z]?(?:-\d+[A-Za-z]?)?\s+", "", regex=True)
               .str.lower().str.strip())
    expand = {short: long for long, short in STREET_SUFFIX_MAP.items()}
    variants = set()
    for name in streets.dropna().unique():
        if not name:
            continue
        variants.add(name)
        head, _, last = name.rpartition(" ")
        if head and last in expand:
            variants.add(f"{head} {expand[last]}")
    # Longest first so "mystic valley pkwy" wins over a shorter prefix.
    alternation = "|".join(re.escape(v) for v in sorted(variants, key=len, reverse=True))
    return re.compile(rf"\b\d+\s*-?\s*\d*\s+(?:{alternation})\b", re.IGNORECASE)


def scrub_descriptions(desc: pd.Series, scrubber: re.Pattern) -> pd.Series:
    """Redact street addresses, then truncate. Order matters: truncating first
    could cut an address in half and leave the fragment unmatched."""
    return (desc.fillna("").str.replace(scrubber, "[address]", regex=True)
            .str.replace(r"\s+", " ", regex=True).str.strip().str[:DESC_MAX_CHARS])


def group_same_scope_electrical(tagged: pd.DataFrame) -> pd.DataFrame:
    """Stamp an electrical permit and the building permit it wires as one project.

    A heat pump install files two permits: the building permit prices the job
    (median $20,250) and the electrical permit prices only the wiring (median
    $1,350, about 7% of it). Read as two independent rows, the electrical one
    reads as a $1,350 heat pump, which is not a price anyone can act on.

    Both rows are kept -- each permit is real, with its own date, contractor and
    description -- and are given a shared group id instead. The page renders the
    group under one summary row priced by _merged_cost(); collapse_groups() does
    the same thing for the medians. Nothing is discarded to get there.

    Three conditions, and the third is the important one:
      - same parcel, within GROUP_WINDOW_DAYS
      - exactly one building permit in the cluster (two means two projects --
        e.g. units 43 and 45 of a two-family, which must not become one row)
      - the electrical permit introduces NO project type of its own

    Without that last condition a solar building permit would absorb an
    unrelated panel upgrade, and the combined cost would then be counted in
    full toward both the solar median and the panel median.

    The ids assigned here are provisional: they run in parcel order, so
    build_frontend_data renumbers them in emitted order before writing.
    """
    flags = list(FRONTEND_FLAGS)
    t = tagged.sort_values(["parcel_key", "application_date"]).copy()

    # Rows with no parcel are given a unique key so they can never group.
    pk = t["parcel_key"]
    t["_pk"] = pk.where(pk.notna(), "__solo_" + t.index.astype(str))
    gap = t.groupby("_pk")["application_date"].diff().dt.days
    t["_cid"] = t["_pk"].astype(str) + "#" + (
        (gap.isna() | (gap > GROUP_WINDOW_DAYS)).groupby(t["_pk"]).cumsum().astype(str))

    t["gid"] = pd.Series(pd.NA, index=t.index, dtype="Int64")
    n_groups = 0
    for _, g in t.groupby("_cid", sort=False):
        if len(g) < 2:
            continue
        b = g[g["application_type"] == "Building Permit"]
        e = g[g["application_type"] == "Electrical Permit"]
        if len(b) != 1 or e.empty:
            continue
        if (e[flags].max() > b[flags].max()).any():
            continue

        t.loc[b.index.append(e.index), "gid"] = n_groups
        n_groups += 1

    print(f"  grouped {int(t['gid'].notna().sum()):,} permits into {n_groups:,} projects "
          f"(<={GROUP_WINDOW_DAYS}d, same parcel, same scope)")
    return t.drop(columns=["_pk", "_cid"])


def collapse_groups(out: pd.DataFrame) -> pd.DataFrame:
    """One row per project. The medians run on this, never on the emitted rows.

    The page shows every permit; a median must not. Both permits for one heat
    pump would otherwise land in the heat-pump distribution -- the $20,250 job
    and the $1,350 wiring as equals -- and roughly halve the answer.

    A group keeps its building permit's row (condition 3 above guarantees its
    flags already cover the electrical permit's) and takes the group cost, the
    larger system size, and MERGED_TRADE.
    """
    parts = [out[out["gid"].isna()]]
    for _, g in out[out["gid"].notna()].groupby("gid", sort=False):
        b = g[g["trade"] == "Building Permit"]
        e = g[g["trade"] == "Electrical Permit"]
        row = b.iloc[[0]].copy()
        row["cost"] = _merged_cost(b["cost"].sum(min_count=1), e["cost"].sum(min_count=1))
        row["kw"] = g["kw"].max()
        row["trade"] = MERGED_TRADE
        parts.append(row)
    return pd.concat(parts)


def _merged_cost(bc: float, ec: float) -> float:
    """Sum a sub-scope electrical permit; keep the larger of two restatements."""
    if pd.isna(bc):
        return ec
    if pd.isna(ec):
        return bc
    if bc > 0 and ec / bc < SUBSCOPE_RATIO:
        return bc + ec
    return max(bc, ec)


def verify_deidentified(payload: dict, scrubber: re.Pattern) -> None:
    """Refuse to write a payload that could be joined back to an address.

    This is the guard that stops a later "just add parcel_prefix, it's useful
    for debugging" from quietly undoing the whole design.
    """
    cols = payload["cols"]
    idx = {c: i for i, c in enumerate(cols)}
    for col in cols:
        bad = [p for p in FORBIDDEN_COL_PARTS if p in col.lower()]
        # "res_area" contains no forbidden part; guard against accidental matches
        # only on whole-word-ish grounds by exempting the known-safe names.
        # "gid" trips the bare "id" substring. It is exempted here rather than
        # renamed around the check, because the check is worth confronting: a
        # gid does link permits to each other, which no other column does. What
        # makes it safe is that it points nowhere outside the payload -- it is a
        # dense counter handed out in emitted row order, not derived from the
        # parcel key -- and the two assertions below are what hold that true.
        if bad and col not in {"res_area", "attr_level", "gid"}:
            raise AssertionError(f"column {col!r} looks identifying ({bad[0]}) -- refusing to write")

    sizes = Counter(r[idx["gid"]] for r in payload["rows"] if r[idx["gid"]] is not None)
    if sizes:
        if sorted(sizes) != list(range(len(sizes))):
            raise AssertionError("gid is not a dense 0..n-1 counter -- it may carry source order")
        if min(sizes.values()) < 2:
            raise AssertionError("a gid appears on one row -- a group of one is not a group")
        if max(sizes.values()) > MAX_GROUP_SIZE:
            raise AssertionError(
                f"a group holds {max(sizes.values())} permits (cap {MAX_GROUP_SIZE}) -- "
                "the clustering is running unrelated work together")

    leaks = [r[idx["desc"]] for r in payload["rows"] if r[idx["desc"]] and scrubber.search(r[idx["desc"]])]
    if leaks:
        raise AssertionError(f"{len(leaks)} descriptions still contain an address, e.g. {leaks[0]!r}")

    for col, width in (("year_built", YEAR_BUCKET), ("res_area", AREA_BUCKET)):
        off = [r[idx[col]] for r in payload["rows"] if r[idx[col]] is not None and r[idx[col]] % width]
        if off:
            raise AssertionError(f"{len(off)} {col} values are not on a {width} grid, e.g. {off[0]}")


def _jsonable(frame: pd.DataFrame) -> list:
    """Rows as plain lists: NaN -> None, numpy scalars unwrapped, and integral
    floats written as ints. Bucketing makes year_built/res_area/cost whole
    numbers, and "1920.0" costs two bytes a cell over "1920" for nothing."""
    def clean(v):
        if pd.isna(v):
            return None
        if hasattr(v, "item"):
            v = v.item()
        return int(v) if isinstance(v, float) and v.is_integer() else v

    return [[clean(v) for v in row] for row in frame.itertuples(index=False, name=None)]


def build_frontend_data(joined: pd.DataFrame, gdb_path: Path, out_dir: Path,
                        permits_path: Path) -> None:
    """Write the three de-identified files the static page loads."""
    import json

    addresses = load_addresses(gdb_path)
    scrubber = build_address_scrubber(addresses)

    tagged = joined[joined[list(FRONTEND_FLAGS)].any(axis=1)].copy()
    tagged["desc_clean"] = scrub_descriptions(tagged["project_description"], scrubber)
    tagged = group_same_scope_electrical(tagged)
    # Only what the page actually reads. trade/status/zones/stories/style/
    # attr_level were emitted for years and rendered by nothing; they cost ~12%
    # of the gzipped payload. Add a column back here when the page needs it.
    out = pd.DataFrame({
        "date": tagged["application_date"].dt.strftime("%Y-%m"),
        "desc": tagged["desc_clean"],
        "cost": tagged["estimated_construction_cost"],
        **{short: tagged[col].astype(int) for col, short in FRONTEND_FLAGS.items()},
        "kw": tagged["solar_kw"],
        "hood": tagged["neighborhood"],
        "contractor": tagged["company_name"],
        "year_built": bucket(tagged["year_built"], YEAR_BUCKET),
        "res_area": bucket(tagged["res_area"], AREA_BUCKET),
        "trade": tagged["application_type"],
        "prop_class": label_prop_class(tagged["use_code"]),
        # Which project a permit belongs to. Null on the great majority of rows,
        # which are a project of one. Renumbered below.
        "gid": tagged["gid"],
    })
    # Source order is chronological within parcel, so neighbouring rows are
    # usually the same building. Shuffle to break that adjacency, THEN stable-
    # sort by date: gzip's match window is only ~32 KB (about 100 rows), so
    # grouping like rows together cuts the wire size by ~11%. The sort is
    # stable, so order within a month stays shuffled and no parcel adjacency
    # comes back -- de-identification depends on that, not on global disorder.
    out = out.sample(frac=1, random_state=0).reset_index(drop=True)
    out = out.sort_values("date", kind="stable", na_position="last").reset_index(drop=True)

    # Renumber the groups in emitted order. They were handed out in parcel
    # order, and a gid that ran with the parcels would put back exactly the
    # adjacency the shuffle above exists to break. Numbered from the top of the
    # file, a gid tracks the date column instead, which is already public.
    seen = {}
    out["gid"] = [None if pd.isna(v) else seen.setdefault(v, len(seen)) for v in out["gid"]]

    # One row per project, for the medians. Derived here and never emitted: the
    # page gets the permits, and reconstructs the summary row from the group.
    proj = collapse_groups(out)
    grouped = proj[proj["gid"].notna()][["gid", "cost"]].sort_values("gid")

    # The group cost is the one thing the page cannot work out for itself --
    # SUBSCOPE_RATIO lives in this file and nowhere else. Everything else on a
    # summary row (date, flags, size, trade, property attributes) is derivable
    # from the member rows, so it is not shipped twice.
    permits = {"cols": list(out.columns), "rows": _jsonable(out),
               "groups": {"cols": list(grouped.columns), "rows": _jsonable(grouped)}}
    verify_deidentified(permits, scrubber)

    addr_out = addresses.assign(
        norm=normalize_address(addresses["addr"]),
        year_built=bucket(addresses["year_built"], YEAR_BUCKET),
        res_area=bucket(addresses["res_area"], AREA_BUCKET),
    )[["addr", "norm", "year_built", "res_area", "stories", "prop_class"]]

    cost = out["cost"]
    def headline(flag: str, label: str, per: pd.Series | None = None,
                 trade: tuple[str, ...] | None = None) -> dict:
        rows = proj[proj[flag] == 1]
        if trade is not None:
            rows = rows[rows["trade"].isin(trade)]
        value = (rows["cost"] / per[rows.index]).median() if per is not None else rows["cost"].median()
        return {"label": label, "value": None if pd.isna(value) else round(float(value)),
                "unit": "/kW" if per is not None else "", "n": int(rows["cost"].notna().sum())}

    meta = {
        "generated": pd.Timestamp.today().strftime("%Y-%m-%d"),
        "permit_extract": permits_path.stem,
        "assessor_vintage": "CY25_FY25",
        "date_range": [out["date"].min(), out["date"].max()],
        "n_permits": len(out),
        # Permits, then projects: a grouped project is several permits and one
        # price, and the medians below count it once.
        "n_projects": len(proj),
        "n_addresses": len(addr_out),
        "headline": {
            "solar": headline("solar", "Rooftop solar", proj["kw"]),
            # Heat pumps only: an electrical permit for a mini-split prices the
            # wiring, not the project (median $1,350 vs $20,250 on the building
            # permit), so blending them understates the job by ~3x. Solar and
            # panel are left across all trades on purpose -- 98% of costed panel
            # rows ARE electrical permits, where the wiring is the whole project.
            "hp": headline("hp", "Heat pumps", trade=("Building Permit", MERGED_TRADE)),
            "panel": headline("panel", "Electrical panel"),
        },
        "facets": {
            # Off the emitted rows, not the projects: these drive filters the
            # page applies per permit, so every value it holds must be listed.
            "prop_class": sorted(out["prop_class"].dropna().unique()),
            "hood": sorted(out["hood"].dropna().unique()),
        },
    }

    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "permits.json").write_text(json.dumps(permits, separators=(",", ":")))
    (out_dir / "addresses.json").write_text(json.dumps(
        {"cols": list(addr_out.columns), "rows": _jsonable(addr_out)}, separators=(",", ":")))
    (out_dir / "meta.json").write_text(json.dumps(meta, indent=2))

    print(f"\nFrontend: {len(out):,} tagged permits in {len(proj):,} projects, "
          f"{len(addr_out):,} addresses -> {out_dir}")
    print("  " + "  ".join(f"{p.name} {p.stat().st_size / 1e6:.2f} MB"
                           for p in sorted(out_dir.glob("*.json"))))
    print(f"  cost present on {cost.notna().mean():.0%} of rows; "
          f"de-identification assertions passed")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--permits", type=Path, default=None,
        help=f"Permit CSV (default: newest in {DEFAULT_PERMIT_DIR})",
    )
    parser.add_argument("--assessor-gdb", type=Path, default=DEFAULT_ASSESSOR_GDB)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--filtered-out", type=Path, default=DEFAULT_FILTERED_OUT)
    parser.add_argument("--no-filtered", action="store_true",
                        help="skip the filtered table, write only --out")
    parser.add_argument("--frontend-out", type=Path, default=DEFAULT_FRONTEND_OUT)
    parser.add_argument("--no-frontend", action="store_true",
                        help="skip the de-identified frontend JSON files")
    args = parser.parse_args()

    permits_path = args.permits or newest_permit_csv(DEFAULT_PERMIT_DIR)
    joined = process(permits_path, args.assessor_gdb)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    joined[OUTPUT_COLS].to_csv(args.out, index=False)
    print(f"\nWrote {len(joined):,} rows x {len(OUTPUT_COLS)} columns to {args.out}")

    if not args.no_filtered:
        kept = apply_filters(joined, FILTER_PARAMS)
        args.filtered_out.parent.mkdir(parents=True, exist_ok=True)
        kept[OUTPUT_COLS].to_csv(args.filtered_out, index=False)
        print(f"\nWrote {len(kept):,} rows x {len(OUTPUT_COLS)} columns to {args.filtered_out}")

    if not args.no_frontend:
        build_frontend_data(joined, args.assessor_gdb, args.frontend_out, permits_path)


if __name__ == "__main__":
    main()
