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