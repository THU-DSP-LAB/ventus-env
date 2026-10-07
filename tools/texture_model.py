#!/usr/bin/env python3
# Background: exercise the standalone texture functional model before Spike/TMU integration.
# Flow: run temporary host builds, or compare actual Vulkan/Spike samples with retained evidence.
# Usage: python3 tools/texture_model.py test [--sanitize]
#        python3 tools/texture_model.py demo
#        python3 tools/texture_model.py gpu [--case-limit 1]
#        python3 tools/texture_model.py gpu-multi
# Maintenance: keep model and test code under tools/texture_model/; no device ABI is defined.

from __future__ import annotations

import argparse
from pathlib import Path
import shutil
import subprocess
import tempfile


def main() -> int:
    parser = argparse.ArgumentParser(description="Standalone C++ texture functional model")
    parser.add_argument("action", choices=("test", "demo", "gpu", "gpu-checks", "gpu-mips", "gpu-mip-linear-check", "gpu-multi", "gpu-multi-linear", "gpu-min-mag", "gpu-multi-min-mag", "gpu-srgb", "gpu-srgb-values", "gpu-multi-srgb", "gpu-fetch-query", "gpu-swizzle", "gpu-fetch-swizzle", "gpu-multi-swizzle", "gpu-border", "gpu-fetch-border", "gpu-multi-border", "gpu-separate", "gpu-fetch-separate", "gpu-multi-separate"))
    parser.add_argument("--sanitize", action="store_true", help="enable address/UB sanitizers")
    parser.add_argument("--cxx", default="c++", help="host C++17 compiler executable")
    parser.add_argument("--mesa-build", type=Path, help="local Mesa build directory for gpu")
    parser.add_argument("--out-dir", type=Path, help="empty artifact directory for gpu")
    parser.add_argument("--case-limit", type=int, default=None, help="cases (default: 180 LOD0 / 360 mip / 432 multi-binding / 1440 min-mag or sRGB / 144 sRGB-values / 2880 fetch-query or swizzle / 6720 border or separate)")
    parser.add_argument("--timeout", type=float, default=900, help="gpu runtime timeout in seconds")
    parser.add_argument("--inject-mismatch", action="store_true", help="gpu checker negative control; must fail")
    parser.add_argument("--inject-query-mismatch", action="store_true", help="fetch/query checker negative control; must fail")
    args = parser.parse_args()
    compiler = shutil.which(args.cxx)
    if compiler is None:
        parser.error(f"host compiler not found: {args.cxx}")
    maximum = 6720 if args.action in ("gpu-border", "gpu-fetch-border", "gpu-separate", "gpu-fetch-separate") else 2880 if args.action in ("gpu-fetch-query", "gpu-swizzle", "gpu-fetch-swizzle") else 144 if args.action == "gpu-srgb-values" else 1440 if args.action in ("gpu-min-mag", "gpu-srgb") else 432 if args.action.startswith("gpu-multi") else 360 if args.action in ("gpu-mips", "gpu-mip-linear-check") else 180
    if args.case_limit is None:
        args.case_limit = maximum
    if args.case_limit < 1 or args.case_limit > maximum or args.timeout <= 0:
        parser.error("case-limit exceeds selected profile and timeout must be positive")
    if args.inject_query_mismatch and (args.action not in ("gpu-fetch-query", "gpu-fetch-swizzle", "gpu-fetch-border", "gpu-fetch-separate") or args.inject_mismatch):
        parser.error("inject-query-mismatch requires a fetch/query profile and excludes inject-mismatch")
    if args.action.startswith("gpu"):
        if args.sanitize:
            parser.error("--sanitize applies to standalone host test/demo only")
        from texture_model.gpu import run

        return run(args, compiler)
    sources = Path(__file__).resolve().parent / "texture_model"
    with tempfile.TemporaryDirectory(prefix="ventus-texture-model-") as directory:
        executable = Path(directory) / args.action
        command = [compiler, "-std=c++17", "-Wall", "-Wextra", "-Werror", "-pedantic"]
        command += ["-O1", "-g", "-fno-omit-frame-pointer",
                    "-fsanitize=address,undefined", "-fno-pie", "-no-pie"] if args.sanitize else ["-O2"]
        command += [str(sources / "model.cpp"), str(sources / f"{args.action}.cpp"),
                    "-o", str(executable)]
        subprocess.run(command, check=True)
        return subprocess.run([str(executable)], check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
