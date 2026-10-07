"""Build and run a pinned local Vulkan-to-Spike numeric comparison."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import shlex
import subprocess
import uuid


def digest(path: Path) -> str:
    checksum = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            checksum.update(block)
    return checksum.hexdigest()


def run(args, compiler: str) -> int:
    source = Path(__file__).resolve().parent
    root = source.parent.parent
    mesa = (args.mesa_build or root / "mesa/build-ventus").resolve()
    paths = {
        "icd": mesa / "src/ventus/vulkan/ventus_devenv_icd.x86_64.json",
        "mesa": mesa / "src/ventus/vulkan/libvulkan_ventus.so",
        "driver": root / "driver/build/driver/spike_device/libspike_driver.so",
        "spike": root / "spike/build/libspike_main.so",
        "llc": root / "llvm/build/bin/llc",
        "ld_lld": root / "llvm/build/bin/ld.lld",
        "llvm_nm": root / "llvm/build/bin/llvm-nm",
        "llvm_objdump": root / "llvm/build/bin/llvm-objdump",
        "crt0": root / "llvm/build-rt-libclc/lib/crt0.o",
        "checker": root / "tools/rtcore.py",
    }
    for name, path in paths.items():
        if not path.is_file():
            raise RuntimeError(f"missing local {name}: {path}")
    glslang = shutil.which("glslangValidator")
    if glslang is None:
        raise RuntimeError("glslangValidator is required")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output = args.out_dir or root / "artifacts/texture_model" / f"gpu-{stamp}-{uuid.uuid4().hex[:8]}"
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    if any(output.iterdir()):
        raise RuntimeError(f"output directory must be empty to exclude stale evidence: {output}")
    elf_dir = output / "elf"
    elf_dir.mkdir()
    separate = args.action in ("gpu-separate", "gpu-fetch-separate", "gpu-multi-separate")
    linear_check = args.action == "gpu-mip-linear-check"
    min_mag = args.action == "gpu-min-mag"
    srgb = args.action in ("gpu-srgb", "gpu-srgb-values", "gpu-multi-srgb")
    fetch_query = args.action in ("gpu-fetch-query", "gpu-fetch-swizzle", "gpu-fetch-border", "gpu-fetch-separate")
    border_mode = args.action in ("gpu-border", "gpu-fetch-border", "gpu-multi-border", "gpu-separate", "gpu-fetch-separate", "gpu-multi-separate")
    view_swizzle = args.action in ("gpu-swizzle", "gpu-fetch-swizzle", "gpu-multi-swizzle") or border_mode
    srgb_values = args.action == "gpu-srgb-values"
    multi_srgb = args.action == "gpu-multi-srgb"
    multi_min_mag = args.action == "gpu-multi-min-mag"
    multi_linear = args.action == "gpu-multi-linear"
    multi = args.action.startswith("gpu-multi")
    mips = args.action == "gpu-mips" or linear_check or min_mag or (srgb and not multi_srgb) or fetch_query or (view_swizzle and not multi)
    rgen_name = "sample_fetch.rgen" if fetch_query else "sample_multi.rgen" if multi else "sample_mip.rgen" if mips else "sample.rgen"
    if separate:
        rgen_name = "sample_fetch_separate.rgen" if fetch_query else "sample_multi_separate.rgen" if multi else "sample_separate.rgen"
    for stage in ("rgen", "rmiss"):
        with (output / f"compile-{stage}.log").open("w") as log:
            subprocess.run([glslang, "-V", "--target-env", "vulkan1.2",
                            str(source / (rgen_name if stage == "rgen" else "sample.rmiss")), "-o", str(output / f"sample.{stage}.spv")],
                           stdout=log, stderr=subprocess.STDOUT, check=True)
    flags = shlex.split(subprocess.check_output(["pkg-config", "--cflags", "--libs", "vulkan"], text=True))
    executable = output / "compare"
    with (output / "compile-host.log").open("w") as log:
        subprocess.run([compiler, "-std=c++17", "-O2", "-Wall", "-Wextra", "-Werror",
                        "-Wno-missing-field-initializers", "-pedantic", str(source / "model.cpp"),
                        str(source / "gpu.cpp"), "-o", str(executable), *flags],
                       stdout=log, stderr=subprocess.STDOUT, check=True)
    env = os.environ.copy()
    # Exclude inherited alternative kernels, no-op/early-exit and debug profiles.
    for name in list(env):
        if name.startswith("VENTUS_VK_"):
            env.pop(name)
    env.update({
        "VK_DRIVER_FILES": str(paths["icd"]), "VK_ICD_FILENAMES": str(paths["icd"]),
        "VENTUS_VK_DRIVER_BACKEND": "spike", "VENTUS_VK_DRIVER_BRIDGE": "1",
        "VENTUS_VK_EMIT_ELF": "1", "VENTUS_VK_DRIVER_LIB": str(paths["driver"]),
        "VENTUS_VK_SHADER_ARTIFACT_DIR": str(elf_dir), "VENTUS_VK_RETAIN_SHADER_ARTIFACTS": "1",
        "VENTUS_VK_LLC": str(paths["llc"]), "VENTUS_VK_LD_LLD": str(paths["ld_lld"]),
        "VENTUS_VK_LLVM_NM": str(paths["llvm_nm"]),
        "VENTUS_VK_LLVM_OBJDUMP": str(paths["llvm_objdump"]),
        "VENTUS_VK_RT_COMPLETION_CHECKER": str(paths["checker"]),
        "VENTUS_VK_CRT0": str(paths["crt0"]),
        "VENTUS_VK_LLVM_TOOL_LIB_DIR": str(root / "llvm/build/lib"),
        "VENTUS_SPIKE_LOG": "0", "MESA_SHADER_CACHE_DISABLE": "true",
        "LD_LIBRARY_PATH": ":".join([str(root / "spike/build"), str(paths["driver"].parent),
                                      str(root / "llvm/build/lib"), env.get("LD_LIBRARY_PATH", "")]),
    })
    command = [str(executable), str(output / "sample.rgen.spv"), str(output / "sample.rmiss.spv"),
               str(output / "numeric.csv"), str(args.case_limit), str(2 if args.inject_query_mismatch else int(args.inject_mismatch))]
    if mips:
        command.append("fetch-separate" if separate and fetch_query else "separate" if separate else "fetch-border" if border_mode and fetch_query else "border" if border_mode else "fetch-swizzle" if fetch_query and view_swizzle else "swizzle" if view_swizzle else "fetch-query" if fetch_query else "srgb-values" if srgb_values else "srgb" if srgb else "min-mag" if min_mag else "mip-linear" if linear_check else "mips")
    if multi:
        command.append("multi-separate" if separate else "multi-border" if border_mode else "multi-swizzle" if view_swizzle else "multi-srgb" if multi_srgb else "multi-min-mag" if multi_min_mag else "multi-linear" if multi_linear else "multi")
    checks_only = args.action == "gpu-checks"
    bad_paths = []
    if checks_only:
        shader_source = (source / "sample.rgen").read_text()
        original = "textureLod(source, requests.uv[i], 0.0)"
        variants = {
            "offset": "textureLodOffset(source, requests.uv[i], 0.0, ivec2(1, 0))",
            "gradients": "textureGrad(source, requests.uv[i], vec2(1), vec2(1))",
            "gather": "textureGather(source, requests.uv[i])",
            "texel-fetch-offset": "texelFetchOffset(source, ivec2(0), 0, ivec2(1, 0))",
            "projected": "textureProjLod(source, vec3(requests.uv[i], 2), 0.0)",
        }
        variants = {name: shader_source.replace(original, value) for name, value in variants.items()}
        variants["descriptor-array"] = shader_source.replace("sampler2D source;", "sampler2D source[2];").replace(original, "textureLod(source[1], requests.uv[i], 0.0)")
        variants["separate-image-array"] = shader_source.replace("uniform sampler2D source;", "uniform texture2D source[2];\nlayout(set = 0, binding = 6) uniform sampler state;").replace(original, "textureLod(sampler2D(source[1], state), requests.uv[i], 0.0)")
        for name, value in variants.items():
            path = output / f"reject-{name}.rgen"
            path.write_text(value)
            spv = path.with_suffix(".spv")
            with (output / f"compile-reject-{name}.log").open("w") as log:
                subprocess.run([glslang, "-V", "--target-env", "vulkan1.2", str(path), "-o", str(spv)], stdout=log, stderr=subprocess.STDOUT, check=True)
            bad_paths.append(spv)
        command += ["checks", *(str(path) for path in bad_paths)]
    print(f"GPU comparison artifacts: {output}", flush=True)
    timeout = False
    with (output / "run.log").open("w") as log:
        try:
            result = subprocess.run(command, env=env, cwd=output, stdout=log,
                                    stderr=subprocess.STDOUT, timeout=args.timeout, check=False)
            returncode = result.returncode
        except subprocess.TimeoutExpired:
            timeout = True
            returncode = 124
    text = (output / "run.log").read_text(errors="replace")
    match = re.search(r"^" + ("CHECKS" if checks_only else "SUMMARY") + r" (.+)$", text, re.MULTILINE)
    summary = dict(item.split("=", 1) for item in match[1].split()) if match else {}
    libraries = [Path(line[8:]).resolve() for line in text.splitlines() if line.startswith("LIBRARY ")]
    expected_libraries = {paths[name].resolve() for name in ("mesa", "driver", "spike")}
    checked_frontier = "V5 final ELF completion frontier passed:" in text
    elf_paths = list(elf_dir.glob("*.riscv"))
    errors = []
    resource_match = re.search(r"^PROFILE (.+)$", text, re.MULTILINE)
    resources = dict(item.split("=", 1) for item in resource_match[1].split()) if resource_match else {}
    if separate:
        expected_sets = "1" if fetch_query else "2"
        expected_bindings = "0" if fetch_query else "2" if multi else "1"
        if (resources.get("separate") != "1" or resources.get("descriptor_sets") != expected_sets or
                resources.get("sampler_bindings") != expected_bindings or
                (fetch_query and resources.get("sampler_objects") != "0")):
            errors.append("independent image/sampler or samplerless resource profile incomplete")
    if timeout:
        errors.append("runtime timed out")
    if not summary:
        errors.append("numeric summary is missing")
    if checks_only:
        expected_libraries = {paths["mesa"].resolve()}
    if not expected_libraries.issubset(set(libraries)):
        errors.append("loaded ICD/driver/Spike libraries do not match the selected local builds")
    if not checked_frontier or not elf_paths:
        errors.append("checked, retained V5 ELF evidence is missing")
    if checks_only:
        if summary.get("shaders") != str(len(bad_paths)) or summary.get("samplers") != "14" or summary.get("errors") != "0" or summary.get("images") != "8" or summary.get("views") != "10":
            errors.append("profile rejection checks incomplete")
    else:
        if summary.get("cases") != str(args.case_limit):
            errors.append("not all requested sampler cases completed")
        outputs = 2 if multi else 1
        requests = 256 if srgb_values else 64
        if summary.get("samples") != str(args.case_limit * requests * 2 * outputs) or summary.get("components") != str(args.case_limit * requests * (24 if multi else 8)):
            errors.append("numeric coverage counts are incomplete")
        if (srgb or view_swizzle) and summary.get("format_checks") != "18":
            errors.append("sRGB/UNORM format feature and unsupported usage checks incomplete")
        if fetch_query and (summary.get("format_checks") != "18" or
                            summary.get("query_components") != str(args.case_limit * 64 * 6) or
                            summary.get("query_failed") != "0"):
            errors.append("fetch/query format coverage or exact property comparison failed")
        if view_swizzle and not multi:
            mapping_counts = [0] * 9
            border_counts = [0] * 3
            for case in range(args.case_limit):
                color = (case // 8) % 3
                mapping = case % 2 if border_mode and not fetch_query and color == 1 else case % 9
                mapping_counts[mapping] += 1
                border_counts[color] += 1
            for m in range(9):
                if summary.get(f"mapping{m}") != str(mapping_counts[m]):
                    errors.append(f"view mapping {m} coverage incomplete")
            if border_mode:
                for color in range(3):
                    if summary.get(f"border{color}") != str(border_counts[color]):
                        errors.append(f"border color {color} coverage incomplete")
        if multi:
            if summary.get("mixed_results") != str(args.case_limit * 128):
                errors.append("mixed-result coverage incomplete")
            for phase in range(8):
                expected = max(0, (args.case_limit + 7 - phase) // 8)
                if summary.get(f"phase{phase}") != str(expected):
                    errors.append(f"phase {phase} coverage incomplete")
            for pair in range(3):
                expected = max(0, min(144, args.case_limit - pair * 144))
                if summary.get(f"pair{pair}") != str(expected):
                    errors.append(f"resource pair {pair} coverage incomplete")
        if any(summary.get(name) != "0" for name in ("mismatches", "markers_failed", "buffer_failed")):
            errors.append("numeric comparison, completion marker, or buffer writeback failed")
    if returncode:
        errors.append(f"comparison process returned {returncode}")
    if multi:
        filters = "independent min/mag and linear/nearest mip filters" if multi_min_mag or multi_srgb or view_swizzle else "linear/nearest and linear/linear mip filters" if multi_linear else "mip-nearest"
        profile = "two fixed combined bindings (set0:2,13), simultaneous samples and mix, view/sampler swaps and updates; " + filters
        if multi_srgb:
            profile += "; mixed sRGB/UNORM, both sRGB channel orders, shared sRGB image views"
        if view_swizzle:
            profile += "; independent view component mappings, mixed sRGB/UNORM and shared sRGB image"
        if border_mode:
            profile += "; per-binding float border colors and mixed border address modes, legal core mappings only"
    elif checks_only:
        profile = "API/compiler rejection checks (no execution)"
    elif mips:
        filters = "four min/mag combinations and both mip filters" if min_mag else "mip-linear" if linear_check else "mip-nearest"
        profile = "Vulkan V5 megakernel to Spike; 2D mip views, dynamic explicit LOD, " + filters
        if srgb:
            profile = "Vulkan V5 megakernel to Spike; RGBA/BGRA sRGB; " + ("all256 byte codes at texel centers" if srgb_values else "four min/mag combinations and both mip filters, view/address/clamp/upload")
        if fetch_query:
            profile = "Vulkan V5 megakernel to Spike; legal integer fetch, exact view-relative dimensions/level queries, four UNORM/sRGB formats, irrelevant sampler changes"
        if view_swizzle:
            profile = ("integer fetch/property queries" if fetch_query else "independent min/mag/mip sampling") + "; four UNORM/sRGB formats, nine rotating view mapping corpus, mip/address/view/clamp/upload"
        if border_mode:
            profile += "; all seven border address pairs, three core float border colors; opaque-black sampling restricted to identity"
    else:
        profile = "Vulkan V5 megakernel to Spike; explicit LOD0, equal min/mag"
    if separate:
        profile += "; sampled image in set0, sampler in set1 bindings6/7; samplerless layout for fetch/query"
    manifest = {
        "status": "PASS" if not errors else "FAIL", "recorded_at": datetime.now(timezone.utc).isoformat(),
        "profile": profile,
        "resource_profile": resources,
        "readback": "direct SSBO and GLSL buffer-reference stores to coherent host-visible buffers",
        "summary": summary, "errors": errors, "timeout": timeout,
        "injected_mismatch": args.inject_mismatch, "checked_frontier": checked_frontier,
        "injected_query_mismatch": args.inject_query_mismatch,
        "command": command,
        "runtime_environment": {name: value for name, value in env.items()
                                if name.startswith(("VENTUS_VK_", "VK_DRIVER_", "VK_ICD_"))},
        "loaded_libraries": [str(path) for path in libraries],
        "binary_sha256": {name: digest(path) for name, path in paths.items()},
        "source_sha256": {name: digest(source / name) for name in
                          ("model.hpp", "model.cpp", "gpu.cpp", "gpu.py", "sample.rgen", "sample_mip.rgen", "sample_multi.rgen", "sample_fetch.rgen", "sample_separate.rgen", "sample_fetch_separate.rgen", "sample_multi_separate.rgen", "sample.rmiss")},
        "entrypoint_sha256": digest(root / "tools/texture_model.py"),
        "rejection_shader_sha256": {path.name: digest(path) for path in bad_paths},
        "elf_sha256": {path.name: digest(path) for path in elf_paths},
        "numeric_csv_sha256": digest(output / "numeric.csv") if (output / "numeric.csv").exists() else None,
        "query_csv_sha256": digest(output / "numeric.csv.queries.csv") if (output / "numeric.csv.queries.csv").exists() else None,
    }
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    if match:
        print("CHECKS" if checks_only else "SUMMARY", match[1], flush=True)
    if errors:
        print("FAIL:", "; ".join(errors), flush=True)
        print("\n".join(text.splitlines()[-18:]), flush=True)
    else:
        print("PASS API/compiler profile checks" if checks_only else "PASS checked Vulkan-to-Spike texture comparison", flush=True)
    return 1 if errors else 0
