_init: 
	git submodule update --init --recursive

init: _init .patched

.patched:
	patch -d spike/ -p1 < spike.patch
	patch -d llvm/ -p1 < llvm-libclc.patch
	touch .patched

.PHONY: _init init

