
init: .patched
	git submodule update --init --recursive

.patched:
	patch spike/fesvr/device.h spike.patch
	patch llvm/libclc/riscv32/lib/CMakeLists.txt llvm-libclc.patch
	touch .patched


