"""Manifest-driven dual-renderer Vulkan RT image regression."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import signal
import struct
import subprocess
import sys
import time
import zlib
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_WORKLOAD = ROOT / "testcases/RTcoreSpikeCase/SaschaWillems_Vulkan"
DEFAULT_MANIFEST = DEFAULT_WORKLOAD / "ventus_rtcore_regression_cases.json"


class RegressionError(RuntimeError):
    pass


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
    while position < len(data) and data[position] in b" \t\r\n":
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


def run_command(command: list[str], cwd: Path, env: dict[str, str], log: Path,
                allow_failure: bool = False, timeout_seconds: int | None = None) -> int:
    with log.open("w", encoding="utf-8") as stream:
        stream.write("command=" + json.dumps(command) + "\n")
        stream.flush()
        process = subprocess.Popen(command, cwd=cwd, env=env, stdout=stream,
                                   stderr=subprocess.STDOUT, start_new_session=True)
        try:
            exit_status = process.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
            stream.write(f"\nTIMEOUT after {timeout_seconds} seconds\n")
            raise RegressionError(
                f"command timed out after {timeout_seconds} seconds; see {log}")
    if exit_status and not allow_failure:
        raise RegressionError(f"command failed ({exit_status}); see {log}")
    return exit_status


def write_render_status(render_dir: Path, renderer: str, exit_status: int) -> None:
    (render_dir / "render.json").write_text(json.dumps({
        "renderer": renderer,
        "exit_status": exit_status,
        "image_was_captured": True,
    }, indent=2) + "\n", encoding="utf-8")


def load_manifest(path: Path) -> dict[str, Any]:
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RegressionError(f"cannot load manifest {path}: {error}") from error
    if manifest.get("version") != 1 or not isinstance(manifest.get("cases"), list):
        raise RegressionError(f"unsupported regression manifest: {path}")
    return manifest


def select_cases(manifest: dict[str, Any], wanted: list[str], include_candidates: bool) -> list[dict[str, Any]]:
    cases = {case["id"]: case for case in manifest["cases"]}
    ids = wanted or [case["id"] for case in manifest["cases"] if case["status"] == "verified"]
    selected: list[dict[str, Any]] = []
    for case_id in ids:
        case = cases.get(case_id)
        if not case:
            raise RegressionError(f"unknown case '{case_id}'")
        if case["status"] != "verified" and not include_candidates:
            raise RegressionError(
                f"{case_id} is {case['status']}: {case['reason']} "
                "(pass --include-candidates to run it explicitly)"
            )
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
    return [str(app), "--benchmark", "--benchmarkframes", str(frames),
            "--width", str(width), "--height", str(height)]


def software_render(case: dict[str, Any], defaults: dict[str, Any], workload: Path,
                    app: Path, width: int, height: int, frames: int, case_dir: Path,
                    timeout_seconds: int) -> Path:
    capture_name = case.get("software_capture_env")
    if not capture_name:
        raise RegressionError(f"{case['id']} has no software capture hook: {case['reason']}")
    icd = Path(defaults["software_icd"])
    require_file(icd, "Lavapipe ICD")
    if not shutil.which("xvfb-run"):
        raise RegressionError("xvfb-run is required for software image capture")
    render_dir = case_dir / "software"
    render_dir.mkdir(parents=True, exist_ok=True)
    ppm = render_dir / "image.ppm"
    env = os.environ.copy()
    env.update({
        "VK_DRIVER_FILES": str(icd), "VK_ICD_FILENAMES": str(icd),
        "VK_LOADER_DRIVERS_SELECT": icd.name, "LIBGL_ALWAYS_SOFTWARE": "1",
        "MESA_LOADER_DRIVER_OVERRIDE": "llvmpipe", capture_name: str(ppm),
    })
    exit_status = run_command(
        ["xvfb-run", "-a", *base_app_command(app, width, height, frames)],
        workload, env, render_dir / "render.log", allow_failure=True,
        timeout_seconds=timeout_seconds)
    require_file(ppm, f"software image for {case['id']}")
    write_png(ppm, render_dir / "image.png")
    write_render_status(render_dir, "lavapipe", exit_status)
    return ppm


def spike_render(case: dict[str, Any], runner: dict[str, Any], workload: Path,
                 app: Path, width: int, height: int, frames: int, case_dir: Path,
                 commit_log: bool, timeout_seconds: int) -> Path:
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
    crt0 = first_existing([
        install / "lib/crt0.o", ROOT / "llvm/build-rt-libclc/lib/crt0.o",
        ROOT / "llvm/build-libclc/lib/crt0.o",
    ], "Ventus crt0")
    icd = mesa_build / "src/ventus/vulkan/ventus_devenv_icd.x86_64.json"
    require_file(icd, "canonical Mesa Ventus ICD")
    require_file(spike_build / "libspike_main.so", "Spike library")
    render_dir = case_dir / "hardware" / "spike"
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
    exit_status = run_command(base_app_command(app, width, height, frames), workload, env,
                              render_dir / "render.log", allow_failure=True,
                              timeout_seconds=timeout_seconds)
    require_file(ppm, f"Spike image for {case['id']}")
    write_png(ppm, render_dir / "image.png")
    write_render_status(render_dir, "spike", exit_status)
    return ppm


def record_case(case: dict[str, Any], workload: Path, app: Path, outputs: dict[str, Path],
                result: dict[str, Any], case_dir: Path, manifest_path: Path,
                execution_profile: str, width: int, height: int, frames: int) -> None:
    generated_elf = [
        {"path": str(path), "sha256": sha256(path)}
        for path in sorted((case_dir / "hardware").rglob("*.riscv"))
    ]
    metadata = {
        "case": case["id"], "status": case["status"], "reason": case["reason"],
        "timestamp_unix": time.time(), "workload": str(workload),
        "workload_git_revision": git_revision(workload), "app": str(app),
        "app_sha256": sha256(app), "manifest": str(manifest_path.resolve()),
        "manifest_sha256": sha256(manifest_path),
        "execution_profile": execution_profile,
        "dispatch": {"width": width, "height": height, "benchmark_frames": frames},
        "outputs": {key: str(value) for key, value in outputs.items()},
        "generated_elf": generated_elf,
        "comparison": result,
    }
    (case_dir / "run.json").write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")


def print_summary(summary: list[dict[str, Any]], width: int, height: int) -> None:
    print(f"\n=== RT image regression ({width}x{height}, max per-channel error shown below) ===")
    for result in summary:
        state = "PASS" if result["passed"] else "FAIL"
        print(f"{state:4} {result['case']}: {result.get('detail', '')}")
    passed = sum(result["passed"] for result in summary)
    print(f"Summary: {passed}/{len(summary)} passed")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--workload-dir", type=Path, default=DEFAULT_WORKLOAD)
    parser.add_argument("--build-dir", type=Path)
    parser.add_argument("--out-dir", type=Path, default=ROOT / "artifacts/rtcore-regression")
    parser.add_argument("--case", action="append", default=[])
    parser.add_argument("--include-candidates", action="store_true")
    parser.add_argument("--hardware-runner", default="spike")
    parser.add_argument("--width", type=int)
    parser.add_argument("--height", type=int)
    parser.add_argument("--benchmark-frames", type=int)
    parser.add_argument("--timeout-seconds", type=int, default=60,
                        help="per-renderer timeout; timed-out cases are reported and skipped")
    parser.add_argument("--max-abs-error", type=int,
                        help="override the case's maximum per-channel error")
    parser.add_argument("--spike-commit-log", action="store_true",
                        help="enable per-instruction Spike tracing; only allowed up to 16x16")
    parser.add_argument("--no-build", action="store_true")
    args = parser.parse_args(argv)
    try:
        manifest = load_manifest(args.manifest)
        workload = args.workload_dir.resolve()
        args.out_dir = args.out_dir.resolve()
        require_file(workload / "CMakeLists.txt", "SaschaWillems workload")
        defaults = manifest["defaults"]
        build_dir = (args.build_dir or workload / defaults["build_dir"]).resolve()
        width = args.width or defaults["width"]
        height = args.height or defaults["height"]
        frames = args.benchmark_frames or defaults["benchmark_frames"]
        if (width <= 0 or height <= 0 or frames <= 0 or args.timeout_seconds <= 0 or
                (args.max_abs_error is not None and args.max_abs_error < 0)):
            raise RegressionError("image dimensions, frame count, and error limit must be non-negative")
        if args.spike_commit_log and width * height > 256:
            raise RegressionError("--spike-commit-log is limited to 16x16 or smaller; "
                                  "use a microcase for instruction-level tracing")
        runner = defaults["hardware_runners"].get(args.hardware_runner)
        if runner is None:
            available = ", ".join(sorted(defaults["hardware_runners"]))
            raise RegressionError(f"unknown hardware runner '{args.hardware_runner}' (available: {available})")
        selected = select_cases(manifest, args.case, args.include_candidates)
        args.out_dir.mkdir(parents=True, exist_ok=True)
        summary: list[dict[str, Any]] = []
        for case in selected:
            case_dir = args.out_dir / case["id"] / f"{width}x{height}"
            if case_dir.exists():
                shutil.rmtree(case_dir)
            case_dir.mkdir(parents=True)
            try:
                app = build_case(workload, build_dir, case["id"], case_dir / "build.log",
                                 not args.no_build)
                software = software_render(case, defaults, workload, app, width, height, frames,
                                           case_dir, args.timeout_seconds)
                hardware = spike_render(case, runner, workload, app, width, height, frames,
                                        case_dir, args.spike_commit_log, args.timeout_seconds)
                # Image agreement is the regression contract.  A command-line
                # override remains useful only for exploratory diagnosis.
                max_abs_error = args.max_abs_error if args.max_abs_error is not None else 0
                comparison = compare_images(software, hardware, max_abs_error)
                (case_dir / "comparison.json").write_text(
                    json.dumps(comparison, indent=2) + "\n")
                # Preserve the provenance even on a pixel mismatch.  This is
                # essential for a failed artifact to have the same useful
                # <case>/<geometry>/ layout as a passing one.
                record_case(case, workload, app, {
                    "software_ppm": software,
                    "software_png": software.with_suffix(".png"),
                    "hardware_ppm": hardware,
                    "hardware_png": hardware.with_suffix(".png"),
                }, comparison, case_dir, args.manifest,
                            defaults["execution_profile"], width, height, frames)
                if not comparison["passed"]:
                    raise RegressionError(
                        f"image mismatch: max abs error {comparison['max_abs_error']} exceeds "
                        f"{comparison['max_abs_limit']}; see {case_dir / 'comparison.json'}")
                summary.append({"case": case["id"], "passed": True,
                                "detail": (
                                    "exact match" if max_abs_error == 0 else
                                    f"max abs error <= {max_abs_error}"
                                ), "directory": str(case_dir)})
            except RegressionError as error:
                summary.append({"case": case["id"], "passed": False,
                                "detail": str(error), "directory": str(case_dir)})
        (args.out_dir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
        print_summary(summary, width, height)
        return 0 if all(result["passed"] for result in summary) else 1
    except RegressionError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
