#!/usr/bin/env python3
"""Final-ELF RT completion gate (ADR-0024).

Run: python3 tools/rtcore.py kernel.riscv --objdump install/bin/llvm-objdump
The implementation and regression tests live in tools/rtcore/. Strict mode is
the default; --mode audit is diagnostic only and must not be used for release.
Keep this entry point thin and maintain encoding/CFG checks in that directory.
"""

from rtcore.verify_rt_completion_frontier import main

if __name__ == "__main__":
    raise SystemExit(main())
