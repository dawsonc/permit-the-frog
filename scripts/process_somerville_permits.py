"""Load the raw Somerville permit data, filter, and save a clean version.

Steps:
1. Filter to residential
2. Strip down to just relevant categories
    - Application #, Application Date, Type/Subtype, Project Description, Address, Parcel #, Lat/Lon, Contractor Company Name
3. Process the project description to get a project type
4. Merge with assessor data
"""