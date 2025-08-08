default: init

# Download rodinia dataset
DATASET_URL  = "http://dspdev.ime.tsinghua.edu.cn/images/ventus_dataset/ventus_rodinia_data.tar.xz"
rodinia_data.tar.xz:
	curl -L $(DATASET_URL) -o rodinia_data.tar.xz
rodinia_data: rodinia_data.tar.xz
	tar -xf rodinia_data.tar.xz

submodules: 
	git submodule update --init --recursive --filter=blob:none --progress

init: submodules rodinia_data

.PHONY: submodules init rodinia_data
