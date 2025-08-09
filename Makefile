_init: 
	git submodule update --init --recursive

init: _init .patched

.patched:
	patch spike/fesvr/device.h spike.patch
	patch llvm/libclc/riscv32/lib/CMakeLists.txt llvm-libclc.patch
	touch .patched

.PHONY: _init init

