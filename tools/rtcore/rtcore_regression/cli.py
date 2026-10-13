"""Five-case Vulkan RT image regression (default: 16x9, software versus Spike)."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import signal
import struct
import subprocess
import sys
import time
import zlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .perf import load_snapshot, summarize
from .reporting import fingerprint, log_excerpt, render_status, source_state, write_json


ROOT = Path(__file__).resolve().parents[3]
DEFAULT_WORKLOAD = ROOT / "build/rt-workload/source"
DEFAULT_MANIFEST = ROOT / "testcases/RTcoreSpikeCase/SaschaWillems_Vulkan/ventus_rtcore_regression_cases.json"
DEFAULT_BUILD = ROOT / "build/rt-workload/build"

# Keep the RTL backend definition here alongside its implementation.
# A manifest may still
# override these paths for a deliberately different local build.
BUILTIN_HARDWARE_RUNNERS: dict[str, dict[str, Any]] = {
    "verilator": {
        "kind": "ventus-verilator",
        "mesa_build": "mesa/build-ventus",
        "llvm_build": "llvm/build",
        "driver_library": "driver/build-rtcore-rtl-{pmu_mode}/driver/rtlsim_device/librtlsim_driver.so",
        "runtime_library_dir": "install-rtcore-rtl-{pmu_mode}/lib",
        "timeout_seconds": 600,
    },
}


class RegressionError(RuntimeError):
    pass


def workload_build_dir(workload: Path, explicit: Path | None, defaults: dict) -> Path:
    return (explicit or (DEFAULT_BUILD if workload == DEFAULT_WORKLOAD
                         else workload / defaults["build_dir"])).resolve()


def with_rtlsim_variant(runner: dict[str, Any], pmu_mode: str, variant: str) -> dict[str, Any]:
    """Select an explicitly named isolated RTLSIM install, preserving default paths."""
    if not variant:
        return runner
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", variant):
        raise RegressionError("--rtlsim-variant must contain only letters, digits, '_' or '-'")
    if runner.get("kind") != "ventus-verilator":
        return runner
    selected = dict(runner)
    marker = f"-{pmu_mode}/"
    replacement = f"-{pmu_mode}-{variant}/"
    for key in ("driver_library", "runtime_library_dir"):
        path = str(selected[key]).format(pmu_mode=pmu_mode)
        if marker not in path:
            raise RegressionError(f"cannot apply RTLSIM variant to {key}: {path}")
        selected[key] = path.replace(marker, replacement, 1)
    return selected


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def git_revision(path: Path) -> str | None:
    if not (path / ".git").exists():
        return None
    result = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "HEAD"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else None


def parse_ppm(path: Path) -> tuple[int, int, bytes]:
    data = path.read_bytes()
    position = 0

    def token() -> bytes:
        nonlocal position
        while position < len(data):
            if data[position:position + 1] == b"#":
                newline = data.find(b"\n", position)
                if newline < 0:
                    raise RegressionError(f"unterminated PPM comment in {path}")
                position = newline + 1
            elif data[position] in b" \t\r\n":
                position += 1
            else:
                break
        start = position
        while position < len(data) and data[position] not in b" \t\r\n":
            position += 1
        if start == position:
            raise RegressionError(f"truncated PPM header in {path}")
        return data[start:position]

    if token() != b"P6":
        raise RegressionError(f"{path} is not a binary PPM (P6)")
    width, height, maximum = int(token()), int(token()), int(token())
    if maximum != 255 or width <= 0 or height <= 0:
        raise RegressionError(f"invalid PPM geometry in {path}")
    # P6 has one header delimiter; whitespace-valued first pixels are data.
    if position >= len(data) or data[position] not in b" \t\r\n":
        raise RegressionError(f"missing PPM payload delimiter in {path}")
    position += 1
    pixels = data[position:]
    if len(pixels) != width * height * 3:
        raise RegressionError(f"invalid PPM payload length in {path}")
    return width, height, pixels


def write_png(ppm: Path, png: Path) -> None:
    width, height, pixels = parse_ppm(ppm)
    raw = b"".join(b"\0" + pixels[row * width * 3:(row + 1) * width * 3]
                   for row in range(height))

    def chunk(kind: bytes, contents: bytes) -> bytes:
        return (struct.pack(">I", len(contents)) + kind + contents +
                struct.pack(">I", zlib.crc32(kind + contents) & 0xFFFFFFFF))

    png.write_bytes(
        b"\x89PNG\r\n\x1a\n" +
        chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)) +
        chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b"")
    )


def compare_images(reference: Path, actual: Path, max_abs_limit: int) -> dict[str, Any]:
    ref_width, ref_height, ref_pixels = parse_ppm(reference)
    actual_width, actual_height, actual_pixels = parse_ppm(actual)
    if (ref_width, ref_height) != (actual_width, actual_height):
        raise RegressionError(
            f"image dimensions differ: {reference}={ref_width}x{ref_height}, "
            f"{actual}={actual_width}x{actual_height}"
        )
    differences = [abs(left - right) for left, right in zip(ref_pixels, actual_pixels)]
    max_abs = max(differences, default=0)
    mean_abs = sum(differences) / len(differences) if differences else 0.0
    differing_channels = sum(value != 0 for value in differences)
    mismatches = [(i % ref_width, i // ref_width) for i in range(ref_width * ref_height)
                  if ref_pixels[i * 3:i * 3 + 3] != actual_pixels[i * 3:i * 3 + 3]]
    rows = [0] * ref_height
    columns = [0] * ref_width
    for x, y in mismatches:
        rows[y] += 1
        columns[x] += 1
    result = {
        "reference": str(reference),
        "actual": str(actual),
        "width": ref_width,
        "height": ref_height,
        "reference_sha256": sha256(reference),
        "actual_sha256": sha256(actual),
        "max_abs_error": max_abs,
        "mean_abs_error": mean_abs,
        "differing_channels": differing_channels,
        "differing_pixels": len(mismatches),
        "differing_pixels_by_row": rows,
        "differing_pixels_by_column": columns,
        "difference_bounds": ({"x_min": min(x for x, _ in mismatches),
                               "x_max": max(x for x, _ in mismatches),
                               "y_min": min(y for _, y in mismatches),
                               "y_max": max(y for _, y in mismatches)} if mismatches else None),
        "difference_samples": [{"x": x, "y": y,
                                "reference": list(ref_pixels[(y * ref_width + x) * 3:(y * ref_width + x) * 3 + 3]),
                                "actual": list(actual_pixels[(y * ref_width + x) * 3:(y * ref_width + x) * 3 + 3])}
                               for x, y in mismatches[:16]],
        "max_abs_limit": max_abs_limit,
        "passed": max_abs <= max_abs_limit,
    }
    return result


def require_file(path: Path, label: str) -> None:
    if not path.is_file():
        raise RegressionError(f"{label} not found: {path}")


def require_executable(path: Path, label: str) -> None:
    if not path.is_file() or not os.access(path, os.X_OK):
        raise RegressionError(f"{label} is not executable: {path}")


def first_existing(candidates: list[Path], label: str, executable: bool = False) -> Path:
    for candidate in candidates:
        if candidate.is_file() and (not executable or os.access(candidate, os.X_OK)):
            return candidate
    rendered = "\n  ".join(str(candidate) for candidate in candidates)
    raise RegressionError(f"could not find {label}; checked:\n  {rendered}")


def verify_rt_release(elf_dir: Path, llvm_objdump: Path, render_log: Path) -> dict[str, int]:
    """Require a fresh ELF and Spike control trace to contain active release."""
    elfs = sorted(elf_dir.glob("*.riscv"))
    if not elfs:
        raise RegressionError(f"no generated RT ELF found below {elf_dir}")
    release_in_elf = 0
    for elf in elfs:
        result = subprocess.run(
            [str(llvm_objdump), "-d", "--mattr=+v,+zfinx", str(elf)],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
        if result.returncode:
            raise RegressionError(f"could not disassemble {elf}: {result.stderr.strip()}")
        release_in_elf += len(re.findall(r"\bvt\.rt\.release\b", result.stdout))
    if not release_in_elf:
        raise RegressionError(f"generated RT ELF has no vt.rt.release: {elf_dir}")

    try:
        trace = render_log.read_text(encoding="utf-8", errors="replace")
    except OSError as error:
        raise RegressionError(f"cannot read Spike RT control trace {render_log}: {error}") from error
    releases = re.findall(
        r"VENTUS_RT_CONTROL op=release .*?active_mask=0x([0-9a-fA-F]+)", trace)
    active_releases = sum(int(mask, 16) != 0 for mask in releases)
    if not active_releases:
        raise RegressionError(f"Spike did not commit an active vt.rt.release; see {render_log}")
    traverses = len(re.findall(r"VENTUS_RT_CONTROL op=traverse\b", trace))
    return {"elf_release_instructions": release_in_elf,
            "trace_traverses": traverses,
            "trace_releases": len(releases),
            "trace_active_releases": active_releases}


def run_command(command: list[str], cwd: Path, env: dict[str, str], log: Path,
                allow_failure: bool = False, timeout_seconds: int | None = None) -> int:
    is_render = log.name == "render.log"
    if is_render:
        write_json(log.parent / "begin.json", {
            "command": command, "cwd": str(cwd), "timeout_seconds": timeout_seconds,
            "environment": {k: v for k, v in env.items() if k.startswith(("VENTUS_", "VK_", "MESA_"))
                            or k == "LD_LIBRARY_PATH"},
            "app": fingerprint(Path(command[command.index("--benchmark") - 1]
                                    if "--benchmark" in command else command[0])),
            "dependencies": [fingerprint(Path(env[k])) for k in
                             ("VENTUS_VK_DRIVER_LIB", "VENTUS_VK_LLC", "VENTUS_VK_LD_LLD", "VENTUS_VK_CRT0")
                             if k in env],
        })
    with log.open("w", encoding="utf-8") as stream:
        stream.write("command=" + json.dumps(command) + "\n")
        stream.flush()
        try:
            process = subprocess.Popen(command, cwd=cwd, env=env, stdout=stream,
                                       stderr=subprocess.STDOUT, start_new_session=True)
        except OSError as error:
            if is_render:
                render_status(log.parent, None, error=str(error))
            raise RegressionError(f"cannot launch command: {error}; see {log}") from error
        try:
            exit_status = process.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
            stream.write(f"\nTIMEOUT after {timeout_seconds} seconds\n")
            stream.flush()
            if is_render:
                render_status(log.parent, process.returncode, timed_out=True)
            raise RegressionError(
                f"command timed out after {timeout_seconds} seconds; see {log}")
    if is_render:
        status = render_status(log.parent, exit_status)
        if not status["image_was_captured"]:
            raise RegressionError(f"{status['status']}: exit={exit_status}; see {log}\n{status['error_excerpt']}")
    if exit_status and not allow_failure:
        raise RegressionError(f"command failed ({exit_status}); see {log}")
    return exit_status


def write_render_status(render_dir: Path, renderer: str, exit_status: int) -> None:
    render_status(render_dir, exit_status)


def successful_xvfb_cleanup_failure(exit_status: int, log: Path, image: Path) -> bool:
    """Recognize xvfb-run failures that occur after a completed render.

    The renderer has already produced a fresh image and printed its benchmark
    completion line.  This covers Debian's vanished-helper cleanup exit and
    sandboxed runners where Xvfb cannot own ``/tmp/.X11-unix`` but the process
    can use an already available display.  Other nonzero renderer exits remain
    failures.
    """
    if exit_status != 1 or not image.is_file():
        return False
    try:
        text = log.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    # The launcher diagnostics are written to xvfb.log, not this render log.
    # A fresh complete benchmark image is therefore the reliable boundary: the
    # child renderer succeeded and only the wrapper returned the nonzero code.
    return "Benchmark finished" in text


def remove_render_dir(render_dir: Path) -> None:
    """Replace only one renderer's artifact, never a sibling renderer's result."""
    if render_dir.exists():
        shutil.rmtree(render_dir)
    render_dir.mkdir(parents=True, exist_ok=True)


def load_manifest(path: Path) -> dict[str, Any]:
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RegressionError(f"cannot load manifest {path}: {error}") from error
    if manifest.get("version") != 1 or not isinstance(manifest.get("cases"), list):
        raise RegressionError(f"unsupported regression manifest: {path}")
    return manifest


def hardware_runners(defaults: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Merge portable runner defaults with workload-local overrides."""
    configured = defaults.get("hardware_runners")
    if not isinstance(configured, dict):
        raise RegressionError("regression manifest has no hardware_runners object")
    result = {name: dict(config) for name, config in BUILTIN_HARDWARE_RUNNERS.items()}
    for name, config in configured.items():
        if not isinstance(config, dict):
            raise RegressionError(f"hardware runner {name!r} is not an object")
        result[name] = {**result.get(name, {}), **config}
    return result


def select_cases(manifest: dict[str, Any], wanted: list[str]) -> list[dict[str, Any]]:
    cases = {case["id"]: case for case in manifest["cases"]}
    ids = wanted or list(cases)
    selected: list[dict[str, Any]] = []
    for case_id in ids:
        case = cases.get(case_id)
        if not case:
            raise RegressionError(f"unknown case '{case_id}'")
        selected.append(case)
    return selected


def build_case(workload: Path, build_dir: Path, case_id: str, log: Path, enabled: bool) -> Path:
    app = build_dir / "bin" / case_id
    if enabled:
        run_command(["cmake", "--build", str(build_dir), "--target", case_id],
                    workload, os.environ.copy(), log)
    require_executable(app, f"{case_id} application")
    return app


def base_app_command(app: Path, width: int, height: int, frames: int) -> list[str]:
    # Image regression needs exactly the requested frames, not a timed warmup.
    # One slow RTL warmup frame otherwise doubles runtime and the driver may
    # capture a different frame than the software's first-frame screenshot.
    return [str(app), "--benchmark", "--benchwarmup", "0", "--benchmarkframes", str(frames),
            "--width", str(width), "--height", str(height)]


def software_render(case: dict[str, Any], defaults: dict[str, Any], workload: Path,
                    app: Path, width: int, height: int, frames: int, case_dir: Path,
                    timeout_seconds: int) -> Path:
    capture_name = case.get("software_capture_env")
    if not capture_name:
        raise RegressionError(f"{case['id']} has no software capture hook")
    icd = Path(defaults["software_icd"])
    require_file(icd, "Lavapipe ICD")
    if not shutil.which("xvfb-run"):
        raise RegressionError("xvfb-run is required for software image capture")
    render_dir = case_dir / "software"
    remove_render_dir(render_dir)
    ppm = render_dir / "image.ppm"
    env = os.environ.copy()
    env.update({
        "VK_DRIVER_FILES": str(icd), "VK_ICD_FILENAMES": str(icd),
        "VK_LOADER_DRIVERS_SELECT": icd.name, "LIBGL_ALWAYS_SOFTWARE": "1",
        "MESA_LOADER_DRIVER_OVERRIDE": "llvmpipe", capture_name: str(ppm),
    })
    exit_status = run_command(
        ["xvfb-run", "-a", "-e", str(render_dir / "xvfb.log"),
         *base_app_command(app, width, height, frames)],
        workload, env, render_dir / "render.log", allow_failure=True,
        timeout_seconds=timeout_seconds)
    require_file(ppm, f"software image for {case['id']}")
    if successful_xvfb_cleanup_failure(exit_status, render_dir / "render.log", ppm):
        exit_status = 0
    write_png(ppm, render_dir / "image.png")
    write_render_status(render_dir, "lavapipe", exit_status)
    return ppm


def spike_render(case: dict[str, Any], runner: dict[str, Any], workload: Path,
                 app: Path, width: int, height: int, frames: int, case_dir: Path,
                 commit_log: bool, timeout_seconds: int,
                 require_rt_release: bool) -> tuple[Path, dict[str, int] | None]:
    if runner.get("kind") != "ventus-spike":
        raise RegressionError(f"unsupported hardware runner kind: {runner.get('kind')}")
    mesa_build = ROOT / runner["mesa_build"]
    llvm_build = ROOT / runner["llvm_build"]
    spike_build = ROOT / runner["spike_build"]
    install = ROOT / "install"
    driver = first_existing([
        ROOT / runner["driver_library"], install / "lib/libspike_driver.so"
    ], "Spike driver library")
    # Prefer the canonical build tree so a fresh LLVM change is exercised.
    # The installed toolchain is an explicit fallback for environments where
    # the source checkout has not been built yet.
    llc = first_existing([llvm_build / "bin/llc", install / "bin/llc"], "Ventus llc", True)
    lld = first_existing([llvm_build / "bin/ld.lld", install / "bin/ld.lld"], "Ventus ld.lld", True)
    llvm_nm = first_existing([llvm_build / "bin/llvm-nm", install / "bin/llvm-nm"], "Ventus llvm-nm", True)
    llvm_objdump = first_existing([llvm_build / "bin/llvm-objdump", install / "bin/llvm-objdump"],
                                  "Ventus llvm-objdump", True)
    crt0 = first_existing([
        *([Path(os.environ["VENTUS_VK_CRT0"])] if os.environ.get("VENTUS_VK_CRT0") else []),
        install / "lib/crt0.o", ROOT / "llvm/build-rt-libclc/lib/crt0.o",
        ROOT / "llvm/build-libclc/lib/crt0.o",
    ], "Ventus crt0")
    icd = mesa_build / "src/ventus/vulkan/ventus_devenv_icd.x86_64.json"
    require_file(icd, "canonical Mesa Ventus ICD")
    require_file(spike_build / "libspike_main.so", "Spike library")
    render_dir = case_dir / "spike"
    remove_render_dir(render_dir)
    elf_dir = render_dir / "elf"
    elf_dir.mkdir(parents=True, exist_ok=True)
    ppm = render_dir / "image.ppm"
    env = os.environ.copy()
    tool_lib = llvm_build / "lib"
    env.update({
        "VK_DRIVER_FILES": str(icd), "VK_ICD_FILENAMES": str(icd),
        "VENTUS_VK_DRIVER_BACKEND": "spike", "VENTUS_VK_DRIVER_BRIDGE": "1",
        "VENTUS_VK_EMIT_ELF": "1", "VENTUS_VK_DRIVER_LIB": str(driver),
        "VENTUS_VK_DUMP_IMAGE": str(ppm), "VENTUS_VK_SHADER_ARTIFACT_DIR": str(elf_dir),
        "VENTUS_VK_LLC": str(llc), "VENTUS_VK_LD_LLD": str(lld),
        "VENTUS_VK_LLVM_NM": str(llvm_nm), "VENTUS_VK_CRT0": str(crt0),
        "VENTUS_VK_LLVM_TOOL_LIB_DIR": str(tool_lib), "MESA_SHADER_CACHE_DISABLE": "true",
        # Commit logging writes one record per simulated instruction and is
        # therefore unsuitable for normal image regression.
        "VENTUS_SPIKE_LOG": "1" if commit_log else "0",
        "LD_LIBRARY_PATH": ":".join([str(spike_build), str(driver.parent), str(tool_lib),
                                        env.get("LD_LIBRARY_PATH", "")]),
    })
    if require_rt_release:
        env.update({"VENTUS_RT_TRACE_CONTROL": "1", "VENTUS_RT_TRACE_CONTROL_MAX": "4096"})
    if case.get("hardware_capture_env"):
        env.pop("VENTUS_VK_DUMP_IMAGE", None)
        env[case["hardware_capture_env"]] = str(ppm)
    exit_status = run_command(base_app_command(app, width, height, frames), workload, env,
                              render_dir / "render.log", allow_failure=True,
                              timeout_seconds=timeout_seconds)
    require_file(ppm, f"Spike image for {case['id']}")
    write_png(ppm, render_dir / "image.png")
    write_render_status(render_dir, "spike", exit_status)
    release_verification = (verify_rt_release(elf_dir, llvm_objdump, render_dir / "render.log")
                            if require_rt_release else None)
    return ppm, release_verification


def summarize_verilator_pmu(perf_json: Path, render_dir: Path) -> Path:
    """Validate and preserve the PMU summary with the owning regression."""
    summary = render_dir / "perf-summary.json"
    try:
        report = summarize(load_snapshot(perf_json))
    except ValueError as error:
        raise RegressionError(f"invalid Verilator PMU snapshot {perf_json}: {error}") from error
    write_json(summary, report)
    return summary


def verilator_render(case: dict[str, Any], runner: dict[str, Any], workload: Path,
                     app: Path, width: int, height: int, frames: int, case_dir: Path,
                     timeout_seconds: int, pmu_mode: str) -> tuple[Path, Path | None]:
    if runner.get("kind") != "ventus-verilator":
        raise RegressionError(f"unsupported hardware runner kind: {runner.get('kind')}")
    mesa_build = ROOT / runner["mesa_build"]
    llvm_build = ROOT / runner["llvm_build"]
    driver = ROOT / runner["driver_library"].format(pmu_mode=pmu_mode)
    runtime_dir = ROOT / runner["runtime_library_dir"].format(pmu_mode=pmu_mode)
    require_file(driver, "dedicated Verilator RTcore driver")
    require_file(runtime_dir / "libVentusRTL-withcache.so", "cache-enabled Verilator RTL model")
    llc = first_existing([llvm_build / "bin/llc", ROOT / "install/bin/llc"], "Ventus llc", True)
    lld = first_existing([llvm_build / "bin/ld.lld", ROOT / "install/bin/ld.lld"], "Ventus ld.lld", True)
    llvm_nm = first_existing([llvm_build / "bin/llvm-nm", ROOT / "install/bin/llvm-nm"], "Ventus llvm-nm", True)
    # Keep the generated kernel runtime paired with the dedicated RTL model.
    # Falling back to install/ first can silently link an older crt0.o while
    # loading the freshly-built install-rtcore-rtl/lib/libVentusRTL*.so.
    crt0 = first_existing([
        runtime_dir / "crt0.o", ROOT / "llvm/build-rt-libclc/lib/crt0.o",
        ROOT / "install/lib/crt0.o", ROOT / "llvm/build-libclc/lib/crt0.o",
    ], "Ventus crt0")
    icd = mesa_build / "src/ventus/vulkan/ventus_devenv_icd.x86_64.json"
    require_file(icd, "canonical Mesa Ventus ICD")
    render_dir = case_dir / "verilator"
    remove_render_dir(render_dir)
    elf_dir = render_dir / "elf"
    elf_dir.mkdir(parents=True, exist_ok=True)
    ppm = render_dir / "image.ppm"
    perf_json = render_dir / "perf.json"
    env = os.environ.copy()
    tool_lib = llvm_build / "lib"
    env.update({
        "VK_DRIVER_FILES": str(icd), "VK_ICD_FILENAMES": str(icd),
        "VENTUS_VK_DRIVER_BACKEND": "rtlsim", "VENTUS_VK_DRIVER_BRIDGE": "1",
        "VENTUS_VK_EMIT_ELF": "1", "VENTUS_VK_DRIVER_LIB": str(driver),
        "VENTUS_VK_DUMP_IMAGE": str(ppm), "VENTUS_VK_SHADER_ARTIFACT_DIR": str(elf_dir),
        "VENTUS_VK_LLC": str(llc), "VENTUS_VK_LD_LLD": str(lld),
        "VENTUS_VK_LLVM_NM": str(llvm_nm), "VENTUS_VK_CRT0": str(crt0),
        "VENTUS_VK_LLVM_TOOL_LIB_DIR": str(tool_lib), "MESA_SHADER_CACHE_DISABLE": "true",
        "VENTUS_BACKEND": "rtl",
        "VENTUS_PMU_MODE": pmu_mode,
        "VENTUS_SPIKE_LOG": "0",
        "LD_LIBRARY_PATH": ":".join([str(runtime_dir), str(driver.parent), str(tool_lib),
                                        env.get("LD_LIBRARY_PATH", "")]),
    })
    if pmu_mode != "off":
        env["VENTUS_RTLSIM_PERF_JSON"] = str(perf_json)
    if case.get("hardware_capture_env"):
        env.pop("VENTUS_VK_DUMP_IMAGE", None)
        env[case["hardware_capture_env"]] = str(ppm)
    exit_status = run_command(base_app_command(app, width, height, frames), workload, env,
                              render_dir / "render.log", allow_failure=True,
                              timeout_seconds=timeout_seconds)
    require_file(ppm, f"Verilator image for {case['id']}")
    write_png(ppm, render_dir / "image.png")
    write_render_status(render_dir, "verilator-rtl", exit_status)
    summary = None
    if pmu_mode != "off":
        try:
            require_file(perf_json, f"Verilator PMU snapshot for {case['id']}")
            summary = summarize_verilator_pmu(perf_json, render_dir)
        except RegressionError as error:
            status = json.loads((render_dir / "render.json").read_text())
            status["pmu_error"] = str(error)
            write_json(render_dir / "render.json", status)
    return ppm, summary


def preflight_runner(runner_name: str, runner: dict[str, Any], pmu_mode: str) -> None:
    """Fail before software rendering when an explicitly selected backend is absent."""
    if runner_name == "spike":
        if runner.get("kind") != "ventus-spike":
            raise RegressionError(f"unsupported hardware runner kind: {runner.get('kind')}")
        require_file(ROOT / runner["driver_library"], "Spike driver library")
        require_file(ROOT / runner["spike_build"] / "libspike_main.so", "Spike library")
        return
    if runner_name == "verilator":
        if runner.get("kind") != "ventus-verilator":
            raise RegressionError(f"unsupported hardware runner kind: {runner.get('kind')}")
        require_file(ROOT / runner["driver_library"].format(pmu_mode=pmu_mode),
                     "dedicated Verilator RTcore driver")
        runtime_dir = ROOT / runner["runtime_library_dir"].format(pmu_mode=pmu_mode)
        require_file(runtime_dir / "libVentusRTL-withcache.so",
                     "cache-enabled Verilator RTL model")
        require_file(ROOT / runner["mesa_build"] /
                     "src/ventus/vulkan/ventus_devenv_icd.x86_64.json",
                     "canonical Mesa Ventus ICD")
        return
    raise RegressionError(f"unsupported hardware runner '{runner_name}'")


def record_case(case: dict[str, Any], runner_name: str, workload: Path, app: Path,
                outputs: dict[str, Path], result: dict[str, Any], case_dir: Path,
                manifest_path: Path, execution_profile: str, width: int, height: int,
                frames: int, rt_release_verification: dict[str, int] | None,
                perf_summary: Path | None) -> None:
    render_dir = case_dir / runner_name
    generated_elf = [
        {"path": str(path), "sha256": sha256(path)}
        for path in sorted(render_dir.rglob("*.riscv"))
    ]
    metadata = {
        "case": case["id"], "runner": runner_name,
        "status": "passed" if result["passed"] else "failed",
        "description": case.get("description", ""),
        "timestamp_unix": time.time(), "workload": str(workload),
        "workload_git_revision": git_revision(workload), "app": str(app),
        "app_sha256": sha256(app), "manifest": str(manifest_path.resolve()),
        "manifest_sha256": sha256(manifest_path),
        "execution_profile": execution_profile,
        "dispatch": {"width": width, "height": height, "benchmark_frames": frames},
        "outputs": {key: str(value) for key, value in outputs.items()},
        "generated_elf": generated_elf,
        "rt_release_verification": rt_release_verification,
        "perf_summary": str(perf_summary) if perf_summary else None,
        "comparison": result,
    }
    (case_dir / f"run.{runner_name}.json").write_text(
        json.dumps(metadata, indent=2) + "\n", encoding="utf-8")


def print_summary(summary: list[dict[str, Any]], width: int, height: int) -> None:
    print(f"\n=== RT image regression ({width}x{height}, max per-channel error shown below) ===")
    for result in summary:
        state = "PASS" if result["passed"] else "FAIL"
        print(f"{state:4} {result['case']} [{result.get('runner', 'unknown')}]: "
              f"{result.get('detail', '')}")
    passed = sum(result["passed"] for result in summary)
    print(f"Summary: {passed}/{len(summary)} passed")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--workload-dir", type=Path, default=DEFAULT_WORKLOAD)
    parser.add_argument("--build-dir", type=Path)
    parser.add_argument("--out-dir", type=Path, default=ROOT / "artifacts/rtcore-regression",
                        help="artifact root; each invocation creates runs/<unique-id> below it")
    parser.add_argument("--case", action="append", default=[],
                        help="run only this example; repeat to select several (default: all five)")
    backends = parser.add_mutually_exclusive_group()
    backends.add_argument("--backend", choices=("software", "spike", "rtl", "all"),
                          help="renderer to run (default: spike); hardware includes software comparison")
    backends.add_argument("--hardware-runner", choices=("spike", "verilator", "all"),
                          help=argparse.SUPPRESS)
    parser.add_argument("--pmu-mode", choices=("off", "basic", "detailed"), default="off",
                        help="Verilator PMU elaboration mode; must match build-ventus.sh rtcore-rtl")
    parser.add_argument("--rtlsim-variant", default="",
                        help="use isolated install-rtcore-rtl-<pmu>-<variant> and matching driver build")
    parser.add_argument("--width", type=int, help="image width (default: 16)")
    parser.add_argument("--height", type=int, help="image height (default: 9)")
    parser.add_argument("--benchmark-frames", type=int)
    parser.add_argument("--timeout-seconds", type=int,
                        help="per-renderer timeout; timed-out cases are reported and skipped")
    parser.add_argument("--max-abs-error", type=int,
                        help="override the case's maximum per-channel error")
    parser.add_argument("--spike-commit-log", action="store_true",
                        help="enable per-instruction Spike tracing; only allowed up to 16x16")
    parser.add_argument("--require-rt-release", action="store_true",
                        help="require vt.rt.release in fresh ELF and bounded Spike control trace")
    parser.add_argument("--no-build", action="store_true")
    args = parser.parse_args(argv)
    args.backend = args.backend or {"verilator": "rtl"}.get(args.hardware_runner,
                                                           args.hardware_runner) or "spike"
    try:
        manifest = load_manifest(args.manifest)
        workload = args.workload_dir.resolve()
        args.out_dir = args.out_dir.resolve()
        require_file(workload / "CMakeLists.txt", "SaschaWillems workload")
        defaults = manifest["defaults"]
        available_runners = hardware_runners(defaults) if args.backend != "software" else {}
        build_dir = workload_build_dir(workload, args.build_dir, defaults)
        width = args.width if args.width is not None else defaults["width"]
        height = args.height if args.height is not None else defaults["height"]
        frames = args.benchmark_frames if args.benchmark_frames is not None else defaults["benchmark_frames"]
        if (width <= 0 or height <= 0 or frames <= 0 or
                (args.timeout_seconds is not None and args.timeout_seconds <= 0) or
                (args.max_abs_error is not None and args.max_abs_error < 0)):
            raise RegressionError("image dimensions, frame count, and error limit must be non-negative")
        runner_names = {"software": [], "spike": ["spike"],
                        "rtl": ["verilator"], "all": ["spike", "verilator"]}[args.backend]
        if args.spike_commit_log and "spike" not in runner_names:
            raise RegressionError("--spike-commit-log requires the spike runner")
        if args.require_rt_release and "spike" not in runner_names:
            raise RegressionError("--require-rt-release requires the spike runner")
        if args.spike_commit_log and width * height > 256:
            raise RegressionError("--spike-commit-log is limited to 16x16 or smaller; "
                                  "use a microcase for instruction-level tracing")
        runners = []
        for runner_name in runner_names:
            runner = available_runners.get(runner_name)
            if runner is not None:
                runner = with_rtlsim_variant(runner, args.pmu_mode, args.rtlsim_variant)
                runners.append((runner_name, runner))
        if len(runners) != len(runner_names):
            available = ", ".join(sorted(available_runners))
            raise RegressionError(f"missing backend '{args.backend}' (available runners: {available})")
        for runner_name, runner in runners:
            preflight_runner(runner_name, runner, args.pmu_mode)
        selected = select_cases(manifest, args.case)
        args.out_dir = args.out_dir / "runs" / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
                                                + f"-{os.getpid()}")
        args.out_dir.mkdir(parents=True, exist_ok=False)
        print(f"Run directory: {args.out_dir}", flush=True)
        write_json(args.out_dir / "provenance.json", {
            "sources": source_state(ROOT),
            "workload": str(workload),
            "workload_inputs": fingerprint(workload / ".ventus-rt-inputs"),
            "manifest": fingerprint(args.manifest),
            "pmu_mode": args.pmu_mode,
            "backend": args.backend,
            "runners": {name: config for name, config in runners},
            "runtime_libraries": [fingerprint(ROOT / config["runtime_library_dir"].format(
                pmu_mode=args.pmu_mode) /
                                               "libVentusRTL-withcache.so")
                                  for _, config in runners if "runtime_library_dir" in config],
            "mesa_libraries": [fingerprint(p) for p in
                               (ROOT / "mesa/build-ventus/src/ventus/vulkan").glob("libvulkan_ventus.so*")],
            "generated_rtl": fingerprint(ROOT / "gpgpu/sim-verilator/build/generated" /
                                         f"rtl-{args.pmu_mode}/dut.v"),
        })
        summary: list[dict[str, Any]] = []
        for case in selected:
            case_dir = args.out_dir / case["id"] / f"{width}x{height}"
            case_dir.mkdir(parents=True, exist_ok=True)
            try:
                app = build_case(workload, build_dir, case["id"], case_dir / "build.log",
                                 not args.no_build)
                software = software_render(case, defaults, workload, app, width, height, frames,
                                           case_dir, args.timeout_seconds or 60)
                if not runners:
                    status = json.loads((case_dir / "software/render.json").read_text())
                    image_width, image_height, _ = parse_ppm(software)
                    passed = status["process_succeeded"] and (image_width, image_height) == (width, height)
                    result = {"case": case["id"], "runner": "software", "passed": passed,
                              "detail": "image captured (no hardware comparison)" if passed
                              else f"software capture failed: {status['status']} exit={status['exit_status']}; "
                                   f"image={image_width}x{image_height}, expected={width}x{height}",
                              "directory": str(case_dir), "render_status": [status]}
                    write_json(case_dir / "run.software.json", result)
                    summary.append(result)
                for runner_name, runner in runners:
                    try:
                        result = run_hardware_case(case, runner_name, runner, workload, app,
                                                   width, height, frames, case_dir, software,
                                                   args, defaults)
                    except RegressionError as error:
                        result = {"case": case["id"], "runner": runner_name, "passed": False,
                                  "detail": str(error), "directory": str(case_dir)}
                        write_json(case_dir / f"run.{runner_name}.json", result)
                    summary.append(result)
                spike_image = case_dir / "spike/image.ppm"
                rtl_image = case_dir / "verilator/image.ppm"
                if spike_image.is_file() and rtl_image.is_file():
                    write_json(case_dir / "comparison.spike-verilator.json",
                               compare_images(spike_image, rtl_image, 0))
            except RegressionError as error:
                failure = {"case": case["id"], "runner": args.backend, "passed": False,
                           "detail": str(error), "directory": str(case_dir)}
                summary.append(failure)
                write_json(case_dir / "run.failure.json", failure)
        (args.out_dir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
        print_summary(summary, width, height)
        return 0 if all(result["passed"] for result in summary) else 1
    except RegressionError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


def run_hardware_case(case: dict[str, Any], runner_name: str, runner: dict[str, Any],
                      workload: Path, app: Path, width: int, height: int, frames: int,
                      case_dir: Path, software: Path, args: argparse.Namespace,
                      defaults: dict[str, Any]) -> dict[str, Any]:
    timeout = (args.timeout_seconds if args.timeout_seconds is not None
               else int(runner.get("timeout_seconds", 60)))
    if runner_name == "spike":
        hardware, release_verification = spike_render(
            case, runner, workload, app, width, height, frames, case_dir,
            args.spike_commit_log, timeout, args.require_rt_release)
        perf_summary = None
    elif runner_name == "verilator":
        hardware, perf_summary = verilator_render(
            case, runner, workload, app, width, height, frames, case_dir, timeout,
            args.pmu_mode)
        release_verification = None
    else:
        raise RegressionError(f"unsupported hardware runner '{runner_name}'")
    max_abs_error = (args.max_abs_error if args.max_abs_error is not None
                     else case.get("max_abs_error", 0))
    comparison = compare_images(software, hardware, max_abs_error)
    comparison_path = case_dir / f"comparison.{runner_name}.json"
    comparison_path.write_text(json.dumps(comparison, indent=2) + "\n")
    statuses = [json.loads((case_dir / name / "render.json").read_text())
                for name in ("software", runner_name)]
    errors = [f"{s['renderer']}: {s['status']} exit={s['exit_status']}; "
              f"{s['error_excerpt']}; see {s['log']}"
              for s in statuses if not s["process_succeeded"]]
    errors += [s["pmu_error"] for s in statuses if "pmu_error" in s]
    result = dict(comparison)
    result["passed"] = comparison["passed"] and not errors
    result["render_status"] = statuses
    record_case(case, runner_name, workload, app, {
        "software_ppm": software,
        "software_png": software.with_suffix(".png"),
        f"{runner_name}_ppm": hardware,
        f"{runner_name}_png": hardware.with_suffix(".png"),
    }, result, case_dir, args.manifest,
                defaults["execution_profile"], width, height, frames,
                release_verification, perf_summary)
    if not comparison["passed"]:
        errors.append(f"image_mismatch: {comparison['differing_pixels']} pixels, "
                      f"bounds={comparison['difference_bounds']}; see {comparison_path}")
    return {"case": case["id"], "runner": runner_name, "passed": result["passed"],
            "detail": "; ".join(errors) if errors else "image and process passed",
            "directory": str(case_dir)}


if __name__ == "__main__":
    raise SystemExit(main())
