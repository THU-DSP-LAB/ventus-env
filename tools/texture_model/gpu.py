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
    nonuniform = args.action in ("gpu-multi-nonuniform", "gpu-multi-nonuniform-separate", "gpu-fetch-nonuniform")
    uniform = nonuniform or args.action in ("gpu-multi-uniform", "gpu-multi-uniform-separate", "gpu-fetch-uniform")
    arrays = uniform or args.action in ("gpu-multi-array", "gpu-multi-array-separate", "gpu-fetch-array")
    separate = args.action in ("gpu-separate", "gpu-fetch-separate", "gpu-multi-separate", "gpu-multi-array-separate", "gpu-fetch-array", "gpu-multi-uniform-separate", "gpu-fetch-uniform", "gpu-multi-nonuniform-separate", "gpu-fetch-nonuniform")
    linear_check = args.action == "gpu-mip-linear-check"
    min_mag = args.action == "gpu-min-mag"
    srgb = args.action in ("gpu-srgb", "gpu-srgb-values", "gpu-multi-srgb")
    fetch_query = args.action in ("gpu-fetch-query", "gpu-fetch-swizzle", "gpu-fetch-border", "gpu-fetch-separate", "gpu-fetch-array", "gpu-fetch-uniform", "gpu-fetch-nonuniform")
    border_mode = uniform or args.action in ("gpu-border", "gpu-fetch-border", "gpu-multi-border", "gpu-separate", "gpu-fetch-separate", "gpu-multi-separate", "gpu-multi-array", "gpu-multi-array-separate", "gpu-fetch-array", "gpu-multi-uniform-separate", "gpu-fetch-uniform", "gpu-multi-nonuniform-separate", "gpu-fetch-nonuniform")
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
    rgen_path = source / rgen_name
    if arrays:
        text = rgen_path.read_text()
        if nonuniform:
            text = text.replace("#version 460", "#version 460\n#extension GL_EXT_nonuniform_qualifier : require")
        names = ("source",) if fetch_query else ("source_a", "source_b")
        for name in names:
            text = text.replace(f"{name};", f"{name}[{args.array_length}];")
            index_expr = ("image_b_index" if name == "source_b" else "image_a_index")
            text = re.sub(r"\b" + name + r"\b(?!\[|;)",
                          name + (f"[nonuniformEXT({index_expr})]" if nonuniform else "[selectors.image_index]" if uniform else f"[{args.array_index}]"), text)
        if separate and not fetch_query:
            for name in ("state_a", "state_b"):
                text = text.replace(f"{name};", f"{name}[{args.array_length}];")
                index_expr = "sampler_b_index" if name == "state_b" else "sampler_a_index"
                text = re.sub(r"\b" + name + r"\b(?!\[|;)",
                              name + (f"[nonuniformEXT({index_expr})]" if nonuniform else "[selectors.sampler_index]" if uniform else f"[{args.array_length - 1 - args.array_index}]"), text)
        if args.array_binding != 2:
            text = text.replace("binding = 2)", f"binding = {args.array_binding})")
        if uniform:
            text = text.replace("void main() {", "layout(push_constant, std430) uniform Selectors { uint image_index; uint sampler_index; } selectors;\nvoid main() {")
        if nonuniform:
            lane = "0u" if args.inject_lane_index_mismatch else "i"
            selectors = (f"\n    uint image_a_index = ({lane} + selectors.image_index) % {args.array_length}u;"
                         f"\n    uint image_b_index = ({lane} / 2u + selectors.image_index + 1u) % {args.array_length}u;"
                         f"\n    uint sampler_a_index = ({lane} * 3u + selectors.sampler_index + 1u) % {args.array_length}u;"
                         f"\n    uint sampler_b_index = ({lane} / 2u * 3u + selectors.sampler_index + 2u) % {args.array_length}u;")
            text = text.replace("uint i = gl_LaunchIDEXT.x;", "uint i = gl_LaunchIDEXT.x;" + selectors)
            if multi:
                text = re.sub(r"    vec4 a = (textureLod\(.+);", r"    vec4 a = vec4(0);\n    if (i % 4u != 3u) a = \1;", text)
                text = re.sub(r"    vec4 b = (textureLod\(.+);", r"    vec4 b = vec4(0);\n    if (i % 2u == 0u) b = \1;", text)
        rgen_path = output / "array.rgen"
        rgen_path.write_text(text)
    if args.gradients:
        text = rgen_path.read_text().replace("textureLod(", "textureGrad(")
        text = re.sub(r", requests\.uv(?:_lod)?\[i\]\.z\)", ", gradient_x, gradient_y)", text)
        text = re.sub(r", requests\.uv_lod\[i\]\.w\)", ", gradient_x.yx, gradient_y.yx)", text)
        text = text.replace("requests.uv[i]", "requests.uv[2*i]").replace("requests.uv_lod[i]", "requests.uv_lod[2*i]")
        array = "requests.uv_lod" if multi else "requests.uv"
        vector_x = "vec2(0)" if args.inject_gradient_mismatch else array + "[2*i].zw"
        vector_y = "vec2(0)" if args.inject_gradient_mismatch else array + "[2*i+1].xy"
        if args.constant_gradient is not None and not args.inject_gradient_mismatch:
            vector_x = "vec2(" + ",".join(format(v,".17g") for v in args.constant_gradient[:2]) + ")"
            vector_y = "vec2(" + ",".join(format(v,".17g") for v in args.constant_gradient[2:]) + ")"
        declaration = f"\n    vec2 gradient_x = {vector_x};\n    vec2 gradient_y = {vector_y};"
        text = text.replace("uint i = gl_LaunchIDEXT.x;", "uint i = gl_LaunchIDEXT.x;" + declaration)
        rgen_path = output / "gradients.rgen"
        rgen_path.write_text(text)
    if args.offset is not None:
        vector = [0,0] if args.inject_offset_mismatch else args.offset
        operand = f"ivec2({vector[0]}, {vector[1]})"
        text, count = re.subn(r"(textureLod|textureGrad|texelFetch)\((.*)\)(?=;)",
            lambda match: match[1] + "Offset(" + match[2] + ", " + operand + ")", rgen_path.read_text())
        if count != (2 if multi else 1):
            raise RuntimeError("offset shader transformation did not cover each texture operation")
        rgen_path = output / "offset.rgen"
        rgen_path.write_text(text)
    for stage in ("rgen", "rmiss"):
        with (output / f"compile-{stage}.log").open("w") as log:
            subprocess.run([glslang, "-V", "--target-env", "vulkan1.2",
                            str(rgen_path if stage == "rgen" else source / "sample.rmiss"), "-o", str(output / f"sample.{stage}.spv")],
                           stdout=log, stderr=subprocess.STDOUT, check=True)
    if uniform or args.gradients or args.offset is not None:
        disassembler = shutil.which("spirv-dis")
        if not disassembler:
            raise RuntimeError("spirv-dis is required to retain runtime-index evidence")
        with (output / "sample.rgen.spvasm").open("w") as stream:
            subprocess.run([disassembler, str(output / "sample.rgen.spv")], stdout=stream, check=True)
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
        if name.startswith(("VENTUS_VK_", "VENTUS_TEXTURE_")):
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
    if args.gradients:
        env["VENTUS_TEXTURE_GRADIENTS"] = "1"
        if args.constant_gradient is not None:
            env["VENTUS_TEXTURE_CONSTANT_GRADIENT"] = ",".join(map(str,args.constant_gradient))
    if args.offset is not None:
        env["VENTUS_TEXTURE_OFFSET"] = ",".join(map(str,args.offset))
    env["VENTUS_TEXTURE_LOD_BIAS"] = str(args.lod_bias)
    if args.inject_bias_mismatch:
        env["VENTUS_TEXTURE_INJECT_BIAS_MISMATCH"] = "1"
    if uniform:
        env["VENTUS_TEXTURE_UNIFORM_INDEX"] = "1"
    if nonuniform:
        env["VENTUS_TEXTURE_NONUNIFORM_INDEX"] = "1"
    if args.inject_index_mismatch:
        env["VENTUS_TEXTURE_INJECT_INDEX_MISMATCH"] = "1"
    if arrays:
        env["VENTUS_TEXTURE_ARRAY_LENGTH"] = str(args.array_length)
        env["VENTUS_TEXTURE_ARRAY_INDEX"] = str(args.array_index)
        env["VENTUS_TEXTURE_ARRAY_BINDING"] = str(args.array_binding)
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
            "shadow-lod": "vec4(textureLod(source, vec3(requests.uv[i], 0.5), 0.0))",

            "gather": "textureGather(source, requests.uv[i])",
            "array-sampling": "textureLod(source, vec3(requests.uv[i], 0), 0.0)",
            "projected": "textureProjLod(source, vec3(requests.uv[i], 2), 0.0)",
        }
        variants = {name: shader_source.replace(original, value) for name, value in variants.items()}
        variants["shadow-lod"] = variants["shadow-lod"].replace("sampler2D source;", "sampler2DShadow source;")
        variants["array-sampling"] = variants["array-sampling"].replace("sampler2D source;", "sampler2DArray source;")
        variants["shadow-gradients"] = shader_source.replace("sampler2D source;", "sampler2DShadow source;").replace(original,
            "vec4(textureGrad(source, vec3(requests.uv[i], 0.5), vec2(1), vec2(1)))")
        nonuniform_source = shader_source.replace("#version 460", "#version 460\n#extension GL_EXT_nonuniform_qualifier : require")
        variants["runtime-descriptor-array"] = nonuniform_source.replace("sampler2D source;", "sampler2D source[];").replace(original, "textureLod(source[nonuniformEXT(i & 1u)], requests.uv[i], 0.0)")
        variants["nested-descriptor-array"] = shader_source.replace("sampler2D source;", "sampler2D source[2][2];").replace(original, "textureLod(source[0][1], requests.uv[i], 0.0)")
        variants["dynamic-storage-image-array"] = nonuniform_source.replace("image2D carrier;", "image2D carrier[2];").replace("imageStore(carrier,", "imageStore(carrier[nonuniformEXT(i & 1u)],")
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
    if args.gradients:
        spirv_gradient = (output / "sample.rgen.spvasm").read_text()
        gradient_ir = "\n".join(path.read_text() for path in elf_dir.glob("*.emit.ll"))
        if not re.search(r"OpImageSampleExplicitLod .*\bGrad\b", spirv_gradient) or "fdiv float" not in gradient_ir:
            errors.append("SPIR-V Grad or software footprint/log2 arithmetic evidence missing")
    if arrays:
        final_case = args.case_limit - 1
        selector_case = final_case - int(multi and final_case % 8 == 6)
        final_index = (args.array_index + selector_case) % args.array_length if uniform else args.array_index
        for field, expected in (("array_length", args.array_length), ("array_index", final_index),
                                ("array_binding", args.array_binding)):
            if resources.get(field) != str(expected):
                errors.append(f"array resource profile {field} mismatch")
        if "LIFECYCLE free_reallocate=1 reset_reallocate=1 failed_allocate_null=1" not in text:
            errors.append("array descriptor free/reset/reallocation checks missing")
        if multi and not nonuniform and args.array_length > 1 and args.case_limit >= 5 and int(resources.get("element_copies", "0")) == 0:
            errors.append("descriptor element copy was not exercised")
    if args.offset is not None:
        if not re.search(r"OpImage(?:SampleExplicitLod|Fetch) .*\bConstOffset\b", (output / "sample.rgen.spvasm").read_text()):
            errors.append("actual sampling/fetch SPIR-V ConstOffset evidence missing")
    uniform_profile = {}
    if uniform:
        expected_feature = f"FEATURE sampled_dynamic_query=1 sampled_dynamic_query2=1 sampled_dynamic_enabled=1 sampled_nonuniform_query=1 sampled_nonuniform_enabled={int(nonuniform)} runtime_array=0 partially_bound=0 variable_count=0 storage_dynamic=0 uniform_buffer_dynamic=0 sampled_native=0 implicit_divergent_lod=0"
        if expected_feature not in text:
            errors.append("F09 feature query/enable boundary missing")
        uniform_match = re.search(r"^UNIFORM (.+)$", text, re.MULTILINE)
        uniform_profile = dict(item.split("=", 1) for item in uniform_match[1].split()) if uniform_match else {}
        llvm_text = "\n".join(path.read_text() for path in elf_dir.glob("*.emit.ll"))
        spirv_text = (output / "sample.rgen.spvasm").read_text()
        uniform_profile["dynamic_spirv"] = bool(re.search(r"OpAccessChain .* %source(?:_[ab])? %\w+", spirv_text) and
                                                 "OpVariable %_ptr_PushConstant_Selectors PushConstant" in spirv_text)
        uniform_profile["dynamic_llvm"] = bool(re.search(r"shl i32 %\w+, 7", llvm_text) and
                                                re.search(r"mul i32 %\w+, 88", llvm_text))
        uniform_profile["elf_count"] = len(elf_paths)
        if nonuniform:
            uniform_profile["nonuniform_spirv"] = "OpCapability SampledImageArrayNonUniformIndexing" in spirv_text and "NonUniform" in spirv_text
            lane_match = re.search(r"^LANES (.+)$", text, re.MULTILINE)
            lane_profile = dict(item.split("=", 1) for item in lane_match[1].split()) if lane_match else {}
            uniform_profile["lanes"] = lane_profile
            invocations = args.case_limit * 64
            expected_lanes = {"invocations": invocations, "active_a": invocations * 3 // 4 if multi else invocations,
                              "active_b": invocations // 2 if multi else 0,
                              "inactive_a": invocations // 4 if multi else 0,
                              "inactive_b": invocations // 2 if multi else invocations,
                              "reconverged_mixes": invocations if multi else 0}
            if not uniform_profile["nonuniform_spirv"] or any(lane_profile.get(k) != str(v) for k, v in expected_lanes.items()):
                errors.append("NonUniform decoration/capability or active/inactive lane coverage missing")
        if not uniform_profile["dynamic_spirv"] or (args.array_length > 1 and not uniform_profile["dynamic_llvm"]):
            errors.append("retained SPIR-V/LLVM lacks runtime descriptor-index evidence")
        if len(elf_paths) != 1:
            errors.append("uniform dispatches must reuse exactly one retained ELF")
        if uniform_profile.get("pipelines") != "1":
            errors.append("runtime indices must share one compiled pipeline")
        index_counts = [0] * args.array_length
        for case in range(args.case_limit):
            selector_case = case - int(multi and case % 8 == 6)
            index_counts[(args.array_index + selector_case) % args.array_length] += 1
        for i, count in enumerate(index_counts):
            if uniform_profile.get(f"index{i}") != str(count):
                errors.append(f"runtime index {i} coverage mismatch")
    if checks_only:
        expected_libraries = {paths["mesa"].resolve()}
    if not expected_libraries.issubset(set(libraries)):
        errors.append("loaded ICD/driver/Spike libraries do not match the selected local builds")
    if not checked_frontier or not elf_paths:
        errors.append("checked, retained V5 ELF evidence is missing")
    if checks_only:
        if summary.get("limits") != "2" or summary.get("shaders") != str(len(bad_paths)) or summary.get("samplers") != "20" or summary.get("errors") != "0" or summary.get("images") != "8" or summary.get("views") != "10":
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
    if arrays:
        profile += "; fixed descriptor arrays with " + ("runtime per-lane NonUniform" if nonuniform else "runtime dispatch-uniform push-constant" if uniform else "compile-time") + " image/sampler indices and element updates/copies"
    if separate:
        profile += "; sampled image in set0, sampler in set1 bindings6/7; samplerless layout for fetch/query"
    if args.gradients:
        profile += "; explicit isotropic 2D gradients scaled by view base dimensions; sampler bias/clamp; ordinary LLVM arithmetic and texel loads"
    if args.offset is not None:
        profile += "; constant signed texel offsets[-8,7] before integer addressing in each accessed mip"
    manifest = {
        "status": "PASS" if not errors else "FAIL", "recorded_at": datetime.now(timezone.utc).isoformat(),
        "profile": profile,
        "texel_offset": args.offset,
        "injected_offset_mismatch": args.inject_offset_mismatch,
        "explicit_gradients": args.gradients,
        "constant_gradient": args.constant_gradient,
        "gradient_color_tolerance": 4e-6 if args.gradients else None,
        "injected_gradient_mismatch": args.inject_gradient_mismatch,
        "sampler_lod_bias": args.lod_bias,
        "injected_bias_mismatch": args.inject_bias_mismatch,
        "resource_profile": resources,
        "uniform_profile": uniform_profile,
        "array_profile": {"binding": args.array_binding, "length": args.array_length, "image_index": args.array_index,
                          "sampler_index": args.array_length - 1 - args.array_index} if arrays else None,
        "rgen_source_sha256": digest(rgen_path),
        "readback": "direct SSBO and GLSL buffer-reference stores to coherent host-visible buffers",
        "summary": summary, "errors": errors, "timeout": timeout,
        "injected_mismatch": args.inject_mismatch,
        "injected_index_mismatch": args.inject_index_mismatch,
        "injected_lane_index_mismatch": args.inject_lane_index_mismatch, "checked_frontier": checked_frontier,
        "injected_query_mismatch": args.inject_query_mismatch,
        "command": command,
        "runtime_environment": {name: value for name, value in env.items()
                                if name.startswith(("VENTUS_VK_", "VENTUS_TEXTURE_", "VK_DRIVER_", "VK_ICD_"))},
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
