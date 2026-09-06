noop:
	echo "Hello from make!"

pull-permitting-data:
	curl https://data.somervillema.gov/api/v3/views/nneb-s3f7/export.csv?accessType=DOWNLOAD \
		-X POST \ 
		-o data/raw/ma/somerville/permits/$(date +%Y_%m_%d).csv

