noop:
	echo "Hello from make!"

permitting-data:
	curl https://data.somervillema.gov/api/v3/views/nneb-s3f7/export.csv?accessType=DOWNLOAD \
		-X POST \ 
		-o data/raw/ma/somerville/permits/$(date +%Y_%m_%d).csv

ASSESSOR_DIR = ./data/raw/ma/assessor

assessor-data:
	@if [ -d "$(ASSESSOR_DIR)" ] && [ ! "$$(find "$(ASSESSOR_DIR)" -maxdepth 0 -empty)" ]; then \
		echo "MA assessor data found."; \
	else \
		echo "Error: '$(ASSESSOR_DIR)' does not exist or is empty. Request at https://www.mass.gov/forms/massgis-request-statewide-parcel-data"; \
		exit 1; \
	fi
