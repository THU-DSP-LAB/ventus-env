from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path


SUPPORTED_PHASE1_BACKENDS = {"ptx", "sbt", "ptxsim", "sbtsim"}


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def default_install_prefix() -> Path:
    return repo_root() / "install"


def normalize_backend(backend: str) -> str:
    value = (backend or "").strip().lower()
    return value.split("-", 1)[0]


def validate_phase1_backend(backend: str) -> str:
    normalized = normalize_backend(backend)
    if normalized not in SUPPORTED_PHASE1_BACKENDS:
        raise ValueError(f"unsupported backend for phase1 perf run: {backend}")
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
    )
    write_json_atomic(pass_path / "pass.json", manifest)
    return manifest


def run_experiment(
    *,
    command: list[str],
    warmup: int,
    repeat: int,
    env: dict[str, str],
    output_root: Path,
    cwd: str | None = None,
) -> tuple[Path, dict]:
    runtime_env = build_runtime_env(env)
    backend = validate_phase1_backend(runtime_env.get("VENTUS_BACKEND", ""))
    experiment_id = f"{int(time.time())}-pid{os.getpid()}"
    experiment_dir = Path(output_root) / experiment_id
    experiment_dir.mkdir(parents=True, exist_ok=True)
    scheduled_passes = [
        f"passes/warmup-{index:04d}" for index in range(1, warmup + 1)
    ] + [f"passes/measure-{index:04d}" for index in range(1, repeat + 1)]
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
