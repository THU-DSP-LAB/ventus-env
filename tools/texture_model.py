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
import math
from pathlib import Path
import shutil
import subprocess
import tempfile


def main() -> int:
    parser = argparse.ArgumentParser(description="Standalone C++ texture functional model")
    parser.add_argument("action", choices=("test", "demo", "gpu", "gpu-checks", "gpu-mips", "gpu-mip-linear-check", "gpu-multi", "gpu-multi-linear", "gpu-min-mag", "gpu-multi-min-mag", "gpu-srgb", "gpu-srgb-values", "gpu-multi-srgb", "gpu-fetch-query", "gpu-swizzle", "gpu-fetch-swizzle", "gpu-multi-swizzle", "gpu-border", "gpu-fetch-border", "gpu-multi-border", "gpu-separate", "gpu-fetch-separate", "gpu-multi-separate", "gpu-multi-array", "gpu-multi-array-separate", "gpu-fetch-array", "gpu-multi-uniform", "gpu-multi-uniform-separate", "gpu-fetch-uniform", "gpu-multi-nonuniform", "gpu-multi-nonuniform-separate", "gpu-fetch-nonuniform"))
    parser.add_argument("--array-binding", type=int, choices=(2, 12), default=2)
    parser.add_argument("--array-length", type=int, choices=(1, 2, 4), default=4)
    parser.add_argument("--array-index", type=int, default=None)
    parser.add_argument("--image-layers", type=int, choices=(1,2,4), help="2D array image layer profile, independent of descriptor array length")
    parser.add_argument("--linear-image", action="store_true", help="use LINEAR sampled array images and check mapped subresource layouts")
    parser.add_argument("--single-layer-view", action="store_true", help="sample a 2D view of one layer of the array image")
    parser.add_argument("--inject-layer-mismatch", action="store_true", help="layer negative control: pin shader array layer to zero")
    parser.add_argument("--offset", type=int, nargs=2, help="constant signed texel offset.xy in [-8,7] for sampling/fetch")
    parser.add_argument("--inject-offset-mismatch", action="store_true", help="offset negative control: replace shader offset with zero")
    parser.add_argument("--constant-gradient", type=float, nargs=4, help="use compile-time dx.xy and dy.xy derivatives instead of the per-invocation gradient corpus")
    parser.add_argument("--gradients", action="store_true", help="use explicit 2D textureGrad with per-invocation gradient corpus in sampling GPU profiles")
    parser.add_argument("--inject-gradient-mismatch", action="store_true", help="gradient negative control: replace shader gradients with zero")
    parser.add_argument("--lod-bias", type=float, default=0, help="finite sampler bias in [-16,16]; multi profiles use opposite biases for the two samplers")
    parser.add_argument("--inject-bias-mismatch", action="store_true", help="bias negative control: give GPU samplers zero bias while oracle retains requested bias")
    parser.add_argument("--sanitize", action="store_true", help="enable address/UB sanitizers")
    parser.add_argument("--cxx", default="c++", help="host C++17 compiler executable")
    parser.add_argument("--mesa-build", type=Path, help="local Mesa build directory for gpu")
    parser.add_argument("--out-dir", type=Path, help="empty artifact directory for gpu")
    parser.add_argument("--case-limit", type=int, default=None, help="cases (default: 180 LOD0 / 360 mip / 432 multi-binding / 1440 min-mag or sRGB / 144 sRGB-values / 2880 fetch-query or swizzle / 6720 border or separate)")
    parser.add_argument("--timeout", type=float, default=900, help="gpu runtime timeout in seconds")
    parser.add_argument("--inject-lane-index-mismatch", action="store_true", help="nonuniform checker negative control; pins lane component of descriptor indices to zero")
    parser.add_argument("--inject-index-mismatch", action="store_true", help="uniform-index checker negative control; pins GPU selectors to element zero and must fail")
    parser.add_argument("--inject-mismatch", action="store_true", help="gpu checker negative control; must fail")
    parser.add_argument("--inject-query-mismatch", action="store_true", help="fetch/query checker negative control; must fail")
    args = parser.parse_args()
    if args.image_layers is not None and args.action in ("test","demo","gpu-checks","gpu","gpu-srgb-values"):
        parser.error("image-layers requires a mip-capable sampling/fetch GPU profile")
    if args.linear_image and args.image_layers is None:
        parser.error("linear-image requires image-layers")
    if args.single_layer_view and args.image_layers is None:
        parser.error("single-layer-view requires image-layers")
    if args.inject_layer_mismatch and (args.image_layers not in (2,4) or args.single_layer_view):
        parser.error("inject-layer-mismatch requires an array view of two or four image layers")
    if args.offset is not None and (args.action in ("test","demo","gpu-checks","gpu-srgb-values") or any(v < -8 or v > 7 for v in args.offset)):
        parser.error("offset requires a sampling/fetch GPU profile and two integers in [-8,7]")
    if args.inject_offset_mismatch and (not args.offset or args.offset==[0,0] or args.action.startswith("gpu-fetch")):
        parser.error("inject-offset-mismatch requires a nonzero sampling offset")
    if args.constant_gradient is not None and (not args.gradients or any(not math.isfinite(v) or abs(v)>3.4028234663852886e38 for v in args.constant_gradient)):
        parser.error("constant-gradient requires gradients and four finite binary32 values")
    if args.inject_gradient_mismatch and args.constant_gradient==[0,0,0,0]:
        parser.error("a zero constant gradient cannot serve as the mismatch control")
    if args.gradients and (args.action in ("test","demo","gpu-checks","gpu","gpu-srgb-values") or args.action.startswith("gpu-fetch")):
        parser.error("gradients requires a mip-capable sampling GPU profile")
    if args.inject_gradient_mismatch and not args.gradients:
        parser.error("inject-gradient-mismatch requires gradients")
    if not math.isfinite(args.lod_bias) or abs(args.lod_bias)>16:
        parser.error("lod-bias must be finite and in [-16,16]")
    if args.inject_bias_mismatch and (args.lod_bias==0 or args.action in ("test","demo","gpu-checks") or args.action.startswith("gpu-fetch")):
        parser.error("inject-bias-mismatch requires nonzero bias and a sampling GPU profile")
    if args.array_index is None:
        args.array_index = args.array_length - 1
    if not 0 <= args.array_index < args.array_length:
        parser.error("array index must be within array length")
    if args.inject_index_mismatch and args.action not in ("gpu-multi-uniform", "gpu-multi-uniform-separate", "gpu-fetch-uniform"):
        parser.error("inject-index-mismatch requires a uniform-index profile")
    if args.inject_lane_index_mismatch and args.action not in ("gpu-multi-nonuniform", "gpu-multi-nonuniform-separate", "gpu-fetch-nonuniform"):
        parser.error("inject-lane-index-mismatch requires a nonuniform profile")
    compiler = shutil.which(args.cxx)
    if compiler is None:
        parser.error(f"host compiler not found: {args.cxx}")
    maximum = 6720 if args.action in ("gpu-border", "gpu-fetch-border", "gpu-separate", "gpu-fetch-separate", "gpu-fetch-array", "gpu-fetch-uniform", "gpu-fetch-nonuniform") else 2880 if args.action in ("gpu-fetch-query", "gpu-swizzle", "gpu-fetch-swizzle") else 144 if args.action == "gpu-srgb-values" else 1440 if args.action in ("gpu-min-mag", "gpu-srgb") else 432 if args.action.startswith("gpu-multi") else 360 if args.action in ("gpu-mips", "gpu-mip-linear-check") else 180
    if args.case_limit is None:
        args.case_limit = maximum
    if args.case_limit < 1 or args.case_limit > maximum or args.timeout <= 0:
        parser.error("case-limit exceeds selected profile and timeout must be positive")
    if args.inject_query_mismatch and (args.action not in ("gpu-fetch-query", "gpu-fetch-swizzle", "gpu-fetch-border", "gpu-fetch-separate", "gpu-fetch-array", "gpu-fetch-uniform", "gpu-fetch-nonuniform") or args.inject_mismatch):
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
