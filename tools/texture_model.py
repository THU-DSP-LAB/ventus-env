#!/usr/bin/env python3
# Background: exercise the standalone texture functional model before Spike/TMU integration.
# Flow: build its C++ tests or trace demo in a temporary directory, then execute.
# Usage: python3 tools/texture_model.py test [--sanitize]
#        python3 tools/texture_model.py demo
# Maintenance: keep model and test code under tools/texture_model/; no device ABI is defined.

from __future__ import annotations

import argparse
from pathlib import Path
import shutil
import subprocess
import tempfile


def main() -> int:
    parser = argparse.ArgumentParser(description="Standalone C++ texture functional model")
    parser.add_argument("action", choices=("test", "demo"))
    parser.add_argument("--sanitize", action="store_true", help="enable address/UB sanitizers")
    parser.add_argument("--cxx", default="c++", help="host C++17 compiler executable")
    args = parser.parse_args()
    compiler = shutil.which(args.cxx)
    if compiler is None:
        parser.error(f"host compiler not found: {args.cxx}")
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
