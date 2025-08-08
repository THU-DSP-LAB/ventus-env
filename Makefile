
init: .patched
	git submodule update --init --recursive

.patched:
	patch spike/fesvr/device.h spike.patch
	touch .patched


