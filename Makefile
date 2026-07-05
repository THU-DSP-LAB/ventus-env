default: init

# Download rodinia dataset
DATASET_URL ?= https://cloud.tsinghua.edu.cn/f/60be001427c546fba292/?dl=1
rodinia_data.tar.xz:
	curl -L $(DATASET_URL) -o rodinia_data.tar.xz
rodinia_data: rodinia_data.tar.xz
	tar -xf rodinia_data.tar.xz

submodules: 
	git submodule update --init --recursive --filter=blob:none --progress -j 8

init: submodules rodinia_data

.PHONY: submodules init rodinia_data
