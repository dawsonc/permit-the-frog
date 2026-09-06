noop:
	echo "Hello from make!"

PERMIT_DIR = ./data/raw/ma/somerville/permits
PERMIT_URL = "https://data.somervillema.gov/api/v3/views/nneb-s3f7/export.csv?accessType=DOWNLOAD"

permitting-data:
	@if [ -d "$(PERMIT_DIR)" ] && [ ! "$$(find "$(PERMIT_DIR)" -maxdepth 0 -empty)" ]; then \
		echo "Somerville permit data found; skipping download."; \
	else \
		echo "No permit data found; downloading..."; \
		mkdir -p "$(PERMIT_DIR)"; \
		curl -fsSL -X POST $(PERMIT_URL) \
			-o "$(PERMIT_DIR)/$$(date +%Y_%m_%d).csv"; \
	fi

ASSESSOR_DIR = ./data/raw/ma/assessor

assessor-data:
	@if [ -d "$(ASSESSOR_DIR)" ] && [ ! "$$(find "$(ASSESSOR_DIR)" -maxdepth 0 -empty)" ]; then \
		echo "MA assessor data found."; \
	else \
		echo "Error: '$(ASSESSOR_DIR)' does not exist or is empty. Request at https://www.mass.gov/forms/massgis-request-statewide-parcel-data"; \
		exit 1; \
	fi

PROCESSED_SOMERVILLE = data/processed/ma/somerville/joined_data.csv

process-somerville: permitting-data assessor-data
	uv run python scripts/process_somerville_data.py --out $(PROCESSED_SOMERVILLE)

.PHONY: noop permitting-data assessor-data process-somerville
