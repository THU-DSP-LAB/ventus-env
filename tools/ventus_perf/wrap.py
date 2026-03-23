from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path


DEFAULT_BACKEND = "spike"
SUPPORTED_PHASE1_BACKEND_NAMES = ("ptx", "sbt", "ptxsim", "sbtsim")
SUPPORTED_PHASE1_BACKENDS = set(SUPPORTED_PHASE1_BACKEND_NAMES)
SUPPORTED_PROFILER_TYPES = {"nsys", "ncu"}
MAX_NSYS_TOP_KERNELS = 5


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def default_install_prefix() -> Path:
    return repo_root() / "install"


def supported_phase1_backend_list() -> str:
    return ", ".join(SUPPORTED_PHASE1_BACKEND_NAMES)


def normalize_backend(backend: str) -> str:
    value = (backend or "").strip().lower()
    return value.split("-", 1)[0]


def validate_phase1_backend(backend: str) -> str:
    normalized = normalize_backend(backend)
    if normalized not in SUPPORTED_PHASE1_BACKENDS:
        raise ValueError(
            "unsupported backend for phase1 perf run: "
            f"{backend}; supported perf backends: {supported_phase1_backend_list()}"
        )
    return normalized


def validate_profiler_backend(backend: str) -> str:
    normalized = normalize_backend(backend)
    if normalized not in SUPPORTED_PHASE1_BACKENDS:
        raise ValueError(
            "profiler passes require a supported phase1 backend: "
            f"{backend}; supported perf backends: {supported_phase1_backend_list()}"
        )
    return normalized


def build_perf_env(
    base_env: dict[str, str],
    *,
    experiment_id: str,
    pass_id: str,
    pass_type: str,
    pass_dir: Path,
) -> dict[str, str]:
    env = dict(base_env)
    env["VENTUS_PERF"] = "1"
    env["VENTUS_PERF_DETAIL"] = env.get("VENTUS_PERF_DETAIL", "default")
    env["VENTUS_PERF_EXPERIMENT_ID"] = experiment_id
    env["VENTUS_PERF_PASS_ID"] = pass_id
    env["VENTUS_PERF_PASS_TYPE"] = pass_type
    env["VENTUS_PERF_OUT_DIR"] = str(pass_dir)
    return env


def prepend_env_path(current: str | None, prefix: Path) -> str:
    prefix_str = str(prefix)
    if not current:
        return prefix_str
    return f"{prefix_str}:{current}"


def build_runtime_env(base_env: dict[str, str]) -> dict[str, str]:
    env = dict(base_env)
    install_prefix = Path(env.get("VENTUS_INSTALL_PREFIX", default_install_prefix()))
    env["VENTUS_INSTALL_PREFIX"] = str(install_prefix)
    env.setdefault("VENTUS_BACKEND", DEFAULT_BACKEND)
    env["PATH"] = prepend_env_path(env.get("PATH"), install_prefix / "bin")
    env["LD_LIBRARY_PATH"] = prepend_env_path(env.get("LD_LIBRARY_PATH"), install_prefix / "lib")
    env.setdefault("POCL_DEVICES", "ventus")
    env.setdefault("POCL_ENABLE_UNINIT", "1")
    env.setdefault("OCL_ICD_VENDORS", str(install_prefix / "lib" / "libpocl.so"))
    return env


def write_json_atomic(path: Path, payload: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
    tmp_path.replace(path)


def list_event_files(pass_dir: Path) -> list[str]:
    return sorted(entry.name for entry in Path(pass_dir).glob("events.*.jsonl") if entry.is_file())


def list_recorder_errors(pass_dir: Path) -> list[str]:
    return sorted(entry.name for entry in Path(pass_dir).glob("*.error*") if entry.is_file())


def make_pass_begin_manifest(
    *,
    pass_id: str,
    pass_type: str,
    pid: int,
    argv: list[str],
    cwd: str,
    stdout_path: str,
    stderr_path: str,
    env_summary: dict[str, str],
    start_mono_ns: int,
    parent_pid: int | None = None,
) -> dict:
    return {
        "pass_id": pass_id,
        "pass_type": pass_type,
        "pid": pid,
        "parent_pid": os.getpid() if parent_pid is None else parent_pid,
        "argv": list(argv),
        "cwd": cwd,
        "stdout_path": stdout_path,
        "stderr_path": stderr_path,
        "env_summary": dict(env_summary),
        "start_mono_ns": start_mono_ns,
    }


def summarize_perf_env(env: dict[str, str]) -> dict[str, str]:
    keys = [
        "VENTUS_BACKEND",
        "VENTUS_PERF",
        "VENTUS_PERF_DETAIL",
        "VENTUS_PERF_EXPERIMENT_ID",
        "VENTUS_PERF_PASS_ID",
        "VENTUS_PERF_PASS_TYPE",
        "VENTUS_PERF_OUT_DIR",
    ]
    return {key: env[key] for key in keys if key in env}


def finalize_pass_manifest(
    *,
    pass_dir: str | Path,
    pass_id: str,
    pass_type: str,
    returncode: int,
    stdout_path: str,
    stderr_path: str,
    event_files: list[str],
    start_mono_ns: int | None = None,
    end_mono_ns: int | None = None,
    pid: int | None = None,
    argv: list[str] | None = None,
    cwd: str | None = None,
    env_summary: dict[str, str] | None = None,
    recorder_errors: list[str] | None = None,
    launcher_argv: list[str] | None = None,
    target_argv: list[str] | None = None,
    profile_target: dict | None = None,
    artifacts: list[dict] | None = None,
    target_pass_id: str | None = None,
) -> dict:
    state = "completed"
    exit_code = returncode
    term_signal = None
    if returncode < 0:
        state = "signaled"
        exit_code = None
        term_signal = -returncode
    elif returncode > 0:
        state = "failed"
    if end_mono_ns is None:
        end_mono_ns = time.monotonic_ns()
    duration_ns = None
    if start_mono_ns is not None:
        duration_ns = end_mono_ns - start_mono_ns
    manifest = {
        "pass_id": pass_id,
        "pass_type": pass_type,
        "pass_dir": str(pass_dir),
        "state": state,
        "exit_code": exit_code,
        "term_signal": term_signal,
        "stdout_path": stdout_path,
        "stderr_path": stderr_path,
        "event_files": list(event_files),
        "recorder_errors": list(recorder_errors or []),
        "end_mono_ns": end_mono_ns,
    }
    if start_mono_ns is not None:
        manifest["start_mono_ns"] = start_mono_ns
    if duration_ns is not None:
        manifest["duration_ns"] = duration_ns
    if pid is not None:
        manifest["pid"] = pid
    if argv is not None:
        manifest["argv"] = list(argv)
    if cwd is not None:
        manifest["cwd"] = cwd
    if env_summary is not None:
        manifest["env_summary"] = dict(env_summary)
    if launcher_argv is not None:
        manifest["launcher_argv"] = list(launcher_argv)
    if target_argv is not None:
        manifest["target_argv"] = list(target_argv)
    if profile_target is not None:
        manifest["profile_target"] = dict(profile_target)
    if artifacts is not None:
        manifest["artifacts"] = [dict(artifact) for artifact in artifacts]
    if target_pass_id is not None:
        manifest["target_pass_id"] = target_pass_id
    return manifest


def run_pass(
    *,
    experiment_id: str,
    pass_id: str,
    pass_type: str,
    command: list[str],
    pass_dir: Path,
    env: dict[str, str],
    cwd: str | None = None,
    launcher_argv: list[str] | None = None,
    target_argv: list[str] | None = None,
    profile_target: dict | None = None,
    artifacts: list[dict] | None = None,
    target_pass_id: str | None = None,
) -> dict:
    pass_path = Path(pass_dir)
    pass_path.mkdir(parents=True, exist_ok=True)
    runtime_env = build_runtime_env(env)
    validate_phase1_backend(runtime_env.get("VENTUS_BACKEND", ""))
    child_env = build_perf_env(
        runtime_env,
        experiment_id=experiment_id,
        pass_id=pass_id,
        pass_type=pass_type,
        pass_dir=pass_path,
    )
    env_summary = summarize_perf_env(child_env)
    stdout_path = pass_path / "stdout.log"
    stderr_path = pass_path / "stderr.log"
    start_mono_ns = time.monotonic_ns()
    try:
        with stdout_path.open("w", encoding="utf-8") as stdout_handle, stderr_path.open(
            "w", encoding="utf-8"
        ) as stderr_handle:
            proc = subprocess.Popen(
                command,
                cwd=cwd,
                env=child_env,
                stdout=stdout_handle,
                stderr=stderr_handle,
                text=True,
            )
            begin_manifest = make_pass_begin_manifest(
                pass_id=pass_id,
                pass_type=pass_type,
                pid=proc.pid,
                argv=command,
                cwd=os.getcwd() if cwd is None else cwd,
                stdout_path=stdout_path.name,
                stderr_path=stderr_path.name,
                env_summary=env_summary,
                start_mono_ns=start_mono_ns,
            )
            write_json_atomic(pass_path / "pass.begin.json", begin_manifest)
            returncode = proc.wait()
    except OSError as error:
        manifest = finalize_pass_manifest(
            pass_dir=pass_path,
            pass_id=pass_id,
            pass_type=pass_type,
            returncode=1,
            stdout_path=stdout_path.name,
            stderr_path=stderr_path.name,
            event_files=[],
            start_mono_ns=start_mono_ns,
            end_mono_ns=time.monotonic_ns(),
            argv=command,
            cwd=os.getcwd() if cwd is None else cwd,
            env_summary=env_summary,
            recorder_errors=[str(error)],
            launcher_argv=launcher_argv,
            target_argv=target_argv,
            profile_target=profile_target,
            artifacts=artifacts,
            target_pass_id=target_pass_id,
        )
        write_json_atomic(pass_path / "pass.json", manifest)
        return manifest
    manifest = finalize_pass_manifest(
        pass_dir=pass_path,
        pass_id=pass_id,
        pass_type=pass_type,
        returncode=returncode,
        stdout_path=stdout_path.name,
        stderr_path=stderr_path.name,
        event_files=list_event_files(pass_path),
        start_mono_ns=start_mono_ns,
        end_mono_ns=time.monotonic_ns(),
        pid=proc.pid,
        argv=command,
        cwd=os.getcwd() if cwd is None else cwd,
        env_summary=env_summary,
        recorder_errors=list_recorder_errors(pass_path),
        launcher_argv=launcher_argv,
        target_argv=target_argv,
        profile_target=profile_target,
        artifacts=artifacts,
        target_pass_id=target_pass_id,
    )
    write_json_atomic(pass_path / "pass.json", manifest)
    return manifest


def normalize_profiles(profiles: list[str] | None) -> list[str]:
    normalized = []
    for profile in profiles or []:
        value = profile.strip().lower()
        if value not in SUPPORTED_PROFILER_TYPES:
            raise ValueError(f"unsupported profiler pass: {profile}")
        if value not in normalized:
            normalized.append(value)
    return normalized


def _profiler_pass_id(profile: str, occurrence: int) -> str:
    return f"{profile}-{occurrence:04d}"


def _build_profiler_artifacts(profile: str) -> list[dict]:
    if profile == "nsys":
        return [{"kind": "nsys_summary", "path": "artifacts/nsys/summary.json"}]
    if profile == "ncu":
        return [{"kind": "ncu_summary", "path": "artifacts/ncu/summary.json"}]
    raise ValueError(f"unsupported profiler pass: {profile}")


def _build_profiler_target(profile: str, ncu_kernel: str | None, target_pass_id: str) -> dict:
    target = {
        "target_pass_id": target_pass_id,
        "strategy": "aggregate_by_kernel" if profile == "nsys" else "targeted_kernel",
    }
    if profile == "ncu" and ncu_kernel is not None:
        target["kernel_name"] = ncu_kernel
    return target


def _build_profiler_command(profile: str, pass_dir: Path, target_command: list[str]) -> list[str]:
    if profile == "nsys":
        output_path = pass_dir / "artifacts" / "nsys" / "report"
        output_path.parent.mkdir(parents=True, exist_ok=True)
        return ["nsys", "profile", "--output", str(output_path), *target_command]
    if profile == "ncu":
        output_path = pass_dir / "artifacts" / "ncu" / "report"
        output_path.parent.mkdir(parents=True, exist_ok=True)
        return ["ncu", "--export", str(output_path), *target_command]
    raise ValueError(f"unsupported profiler pass: {profile}")


def _extract_nsys_top_kernels(stats_payload: list[dict]) -> list[dict]:
    top_kernels = []
    for entry in stats_payload[:MAX_NSYS_TOP_KERNELS]:
        if "Name" not in entry or "Total Time (ns)" not in entry:
            continue
        top_kernels.append(
            {
                "kernel_name": str(entry["Name"]),
                "gpu_time_ns": int(entry["Total Time (ns)"]),
            }
        )
    return top_kernels


def _write_nsys_summary(pass_dir: Path, env: dict[str, str]) -> None:
    report_path = pass_dir / "artifacts" / "nsys" / "report.nsys-rep"
    if not report_path.exists():
        raise FileNotFoundError(f"missing nsys report artifact: {report_path}")
    proc = subprocess.run(
        [
            "nsys",
            "stats",
            "--quiet",
            "--report",
            "cuda_gpu_kern_sum",
            "--format",
            "json",
            "--output",
            "-",
            str(report_path),
        ],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )
    if proc.returncode != 0:
        message = proc.stderr.strip() or proc.stdout.strip() or "unknown nsys stats failure"
        raise RuntimeError(message)
    payload = json.loads(proc.stdout)
    if not isinstance(payload, list):
        raise ValueError("nsys stats did not return a JSON list")
    write_json_atomic(
        pass_dir / "artifacts" / "nsys" / "summary.json",
        {
            "tool": "nsys",
            "top_kernels": _extract_nsys_top_kernels(payload),
        },
    )


def _build_profiler_schedule(profiles: list[str]) -> list[tuple[str, str]]:
    counts: dict[str, int] = {}
    schedule = []
    for profile in profiles:
        counts[profile] = counts.get(profile, 0) + 1
        schedule.append((profile, _profiler_pass_id(profile, counts[profile])))
    return schedule


def run_experiment(
    *,
    command: list[str],
    warmup: int,
    repeat: int,
    profiles: list[str] | None,
    ncu_kernel: str | None,
    env: dict[str, str],
    output_root: Path,
    cwd: str | None = None,
) -> tuple[Path, dict]:
    runtime_env = build_runtime_env(env)
    profiler_passes = normalize_profiles(profiles)
    if ncu_kernel and "ncu" not in profiler_passes:
        raise ValueError("--ncu-kernel requires --profile ncu")
    if profiler_passes:
        backend = validate_profiler_backend(runtime_env.get("VENTUS_BACKEND", ""))
    else:
        backend = validate_phase1_backend(runtime_env.get("VENTUS_BACKEND", ""))
    profiler_schedule = _build_profiler_schedule(profiler_passes)
    experiment_id = f"{int(time.time())}-pid{os.getpid()}"
    experiment_dir = Path(output_root) / experiment_id
    experiment_dir.mkdir(parents=True, exist_ok=True)
    scheduled_passes = [
        f"passes/warmup-{index:04d}" for index in range(1, warmup + 1)
    ] + [f"passes/measure-{index:04d}" for index in range(1, repeat + 1)]
    scheduled_passes.extend(f"passes/{pass_id}" for _, pass_id in profiler_schedule)
    experiment_begin = {
        "experiment_id": experiment_id,
        "backend": backend,
        "state": "running",
        "argv": list(command),
        "cwd": os.getcwd() if cwd is None else cwd,
        "actual_passes": scheduled_passes,
    }
    write_json_atomic(experiment_dir / "experiment.begin.json", experiment_begin)
    manifests = []
    for index in range(1, warmup + 1):
        pass_id = f"warmup-{index:04d}"
        manifests.append(
            run_pass(
                experiment_id=experiment_id,
                pass_id=pass_id,
                pass_type="warmup",
                command=command,
                pass_dir=experiment_dir / "passes" / pass_id,
                env=runtime_env,
                cwd=cwd,
            )
        )
    for index in range(1, repeat + 1):
        pass_id = f"measure-{index:04d}"
        manifests.append(
            run_pass(
                experiment_id=experiment_id,
                pass_id=pass_id,
                pass_type="measure",
                command=command,
                pass_dir=experiment_dir / "passes" / pass_id,
                env=runtime_env,
                cwd=cwd,
            )
        )
    target_pass_id = f"measure-{repeat:04d}" if repeat > 0 else None
    for profile, pass_id in profiler_schedule:
        if target_pass_id is None:
            raise ValueError("profiler passes require at least one measured pass")
        pass_dir = experiment_dir / "passes" / pass_id
        profile_target = _build_profiler_target(profile, ncu_kernel, target_pass_id)
        profiler_command = _build_profiler_command(profile, pass_dir, command)
        manifest = run_pass(
            experiment_id=experiment_id,
            pass_id=pass_id,
            pass_type=profile,
            command=profiler_command,
            pass_dir=pass_dir,
            env=runtime_env,
            cwd=cwd,
            launcher_argv=profiler_command,
            target_argv=command,
            profile_target=profile_target,
            artifacts=_build_profiler_artifacts(profile),
            target_pass_id=target_pass_id,
        )
        if profile == "nsys" and manifest["state"] == "completed":
            try:
                _write_nsys_summary(pass_dir, runtime_env)
            except (OSError, ValueError, TypeError, json.JSONDecodeError, RuntimeError) as exc:
                manifest.setdefault("recorder_errors", []).append(
                    f"nsys summary extraction failed: {exc}"
                )
                manifest["state"] = "failed"
                manifest["exit_code"] = 1
                manifest["term_signal"] = None
                write_json_atomic(pass_dir / "pass.json", manifest)
        manifests.append(manifest)
    final_state = "completed" if all(m["state"] == "completed" for m in manifests) else "failed"
    experiment_manifest = {
        "experiment_id": experiment_id,
        "backend": backend,
        "state": final_state,
        "argv": list(command),
        "cwd": os.getcwd() if cwd is None else cwd,
        "actual_passes": scheduled_passes,
        "passes": manifests,
    }
    write_json_atomic(experiment_dir / "experiment.json", experiment_manifest)
    return experiment_dir, experiment_manifest
