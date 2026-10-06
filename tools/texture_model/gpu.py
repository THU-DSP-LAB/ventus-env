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
    for stage in ("rgen", "rmiss"):
        with (output / f"compile-{stage}.log").open("w") as log:
            subprocess.run([glslang, "-V", "--target-env", "vulkan1.2",
                            str(source / f"sample.{stage}"), "-o", str(output / f"sample.{stage}.spv")],
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
               str(output / "numeric.csv"), str(args.case_limit), str(int(args.inject_mismatch))]
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
    match = re.search(r"^SUMMARY (.+)$", text, re.MULTILINE)
    summary = dict(item.split("=", 1) for item in match[1].split()) if match else {}
    libraries = [Path(line[8:]).resolve() for line in text.splitlines() if line.startswith("LIBRARY ")]
    expected_libraries = {paths[name].resolve() for name in ("mesa", "driver", "spike")}
    checked_frontier = "V5 final ELF completion frontier passed:" in text
    elf_paths = list(elf_dir.glob("*.riscv"))
    errors = []
    if timeout:
        errors.append("runtime timed out")
    if not summary:
        errors.append("numeric summary is missing")
    if not expected_libraries.issubset(set(libraries)):
        errors.append("loaded ICD/driver/Spike libraries do not match the selected local builds")
    if not checked_frontier or not elf_paths:
        errors.append("checked, retained V5 ELF evidence is missing")
    if summary.get("cases") != str(min(args.case_limit, 180)):
        errors.append("not all requested sampler cases completed")
    if summary.get("samples") != str(args.case_limit * 128) or summary.get("components") != str(args.case_limit * 512):
        errors.append("numeric coverage counts are incomplete")
    if any(summary.get(name) != "0" for name in ("mismatches", "markers_failed", "buffer_failed")):
        errors.append("numeric comparison, completion marker, or buffer writeback failed")
    if returncode:
        errors.append(f"comparison process returned {returncode}")
    manifest = {
        "status": "PASS" if not errors else "FAIL", "recorded_at": datetime.now(timezone.utc).isoformat(),
        "profile": "Vulkan V5 megakernel to Spike; explicit LOD0, equal min/mag; no GPU mip support claim",
        "readback": "direct SSBO and GLSL buffer-reference stores to coherent host-visible buffers",
        "summary": summary, "errors": errors, "timeout": timeout,
        "injected_mismatch": args.inject_mismatch, "checked_frontier": checked_frontier,
        "command": command,
        "runtime_environment": {name: value for name, value in env.items()
                                if name.startswith(("VENTUS_VK_", "VK_DRIVER_", "VK_ICD_"))},
        "loaded_libraries": [str(path) for path in libraries],
        "binary_sha256": {name: digest(path) for name, path in paths.items()},
        "source_sha256": {name: digest(source / name) for name in
                          ("model.hpp", "model.cpp", "gpu.cpp", "gpu.py", "sample.rgen", "sample.rmiss")},
        "entrypoint_sha256": digest(root / "tools/texture_model.py"),
        "elf_sha256": {path.name: digest(path) for path in elf_paths},
        "numeric_csv_sha256": digest(output / "numeric.csv") if (output / "numeric.csv").exists() else None,
    }
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    if match:
        print("SUMMARY", match[1], flush=True)
    if errors:
        print("FAIL:", "; ".join(errors), flush=True)
        print("\n".join(text.splitlines()[-18:]), flush=True)
    else:
        print("PASS checked Vulkan-to-Spike texture comparison", flush=True)
    return 1 if errors else 0
