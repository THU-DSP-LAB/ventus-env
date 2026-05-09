import argparse
import multiprocessing
import os
from collections import Counter
from dataclasses import dataclass

from .cases import (
    MATRIX_PRESETS,
    REQUIRED_PRESETS,
    TEST_CASES,
    required_case_indices_for_backend,
    selected_case_indices_for_backend,
)


WITHCACHE_DEFAULT_REPEAT = 10
JOBS_NUMERATOR = 2
JOBS_DENOMINATOR = 3
WITH_CACHE_SUFFIXES = {"cache", "withcache", "with-cache", "with"}
NO_CACHE_SUFFIXES = {"nocache", "no-cache", "withoutcache", "without-cache", "without"}


@dataclass(frozen=True)
class BackendSelection:
    env_backend: str
    log_name: str


def normalize_backend(backend: str | None) -> BackendSelection:
    raw_backend = backend if backend is not None else os.environ.get("VENTUS_BACKEND", "spike")
    raw_backend = raw_backend.strip().lower()
    if raw_backend in {"isa", "spike"}:
        return BackendSelection("spike", "spike")
    if raw_backend in {"cycle", "cyclesim", "systemc", "simulator"}:
        return BackendSelection("cyclesim", "cyclesim")
    if raw_backend in {"sbt", "ptx", "ptxsim", "sbtsim"}:
        return BackendSelection("sbt", "sbt")

    parsed = _parse_rtl_or_gvm_backend(raw_backend)
    if parsed is not None:
        return parsed
    return BackendSelection(raw_backend, raw_backend)


def _parse_rtl_or_gvm_backend(backend: str) -> BackendSelection | None:
    parts = backend.split("-")
    base = parts[0]
    suffix = "-".join(parts[1:])

    if backend in {"rtlsim-with-cache-gvm", "rtl-with-cache-gvm"}:
        return BackendSelection("gvm-with-cache", "rtlsim-with-cache-gvm")
    if backend in {"rtlsim-no-cache-gvm", "rtl-no-cache-gvm"}:
        return BackendSelection("gvm-no-cache", "rtlsim-no-cache-gvm")

    if base in {"rtl", "rtlsim", "gpgpu"}:
        cache_suffix = _normalize_cache_suffix(suffix)
        env_backend = f"rtlsim-{cache_suffix}"
        return BackendSelection(env_backend, f"rtlsim-{cache_suffix}")
    if base == "gvm":
        cache_suffix = _normalize_cache_suffix(suffix)
        return BackendSelection(f"gvm-{cache_suffix}", f"rtlsim-{cache_suffix}-gvm")
    return None


def _normalize_cache_suffix(suffix: str) -> str:
    if not suffix or suffix in WITH_CACHE_SUFFIXES:
        return "with-cache"
    if suffix in NO_CACHE_SUFFIXES:
        return "no-cache"
    return suffix


def suggest_default_jobs() -> int:
    cpu = _available_cpu_count()
    return _default_worker_threads(cpu)


def suggest_default_matrix_jobs(total_jobs: int) -> int:
    return suggest_default_jobs()


def _available_cpu_count() -> int:
    if hasattr(os, "sched_getaffinity"):
        return len(os.sched_getaffinity(0))
    return os.cpu_count() or multiprocessing.cpu_count() or 1


def _default_worker_threads(cpu: int) -> int:
    return max(1, cpu * JOBS_NUMERATOR // JOBS_DENOMINATOR)


def detect_default_repeat(backend: str | None) -> int:
    effective = normalize_backend(backend).env_backend
    parts = effective.split("-")
    suffix = "-".join(parts[1:]) if len(parts) > 1 else ""
    if suffix == "with-cache":
        return WITHCACHE_DEFAULT_REPEAT
    return 1


def parse_checklist(value: str, backend: str | None = None) -> set[int]:
    checklist = (value or "all").strip().lower()
    if checklist == "all" and backend is not None:
        return set(required_case_indices_for_backend(normalize_backend(backend).env_backend))
    if checklist in REQUIRED_PRESETS:
        return set(REQUIRED_PRESETS[checklist])
    parts = [p.strip() for p in checklist.split(",") if p.strip()]
    if not parts:
        raise argparse.ArgumentTypeError("--checklist is empty")
    try:
        return set(int(p) for p in parts)
    except ValueError as exc:
        preset_names = ", ".join(sorted(REQUIRED_PRESETS.keys()))
        raise argparse.ArgumentTypeError(
            f"Invalid checklist value. Use preset name ({preset_names}), "
            "or comma-separated integers like 0,1,2"
        ) from exc


def parse_matrix(value: str, checklist_override: str | None = None) -> list[tuple[str, set[int]]]:
    matrix_value = (value or "").strip().lower()
    if matrix_value in MATRIX_PRESETS:
        pairs = list(MATRIX_PRESETS[matrix_value])
    else:
        pairs = _parse_matrix_pairs(matrix_value)
    if checklist_override is not None:
        pairs = [(backend, checklist_override) for backend, _ in pairs]
    return [(backend, parse_checklist(checklist_name, backend)) for backend, checklist_name in pairs]


def _parse_matrix_pairs(value: str) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    current_backend = ""
    current_checklist_parts: list[str] = []
    separator = ";" if ";" in value else ","

    for raw_item in value.split(separator):
        item = raw_item.strip()
        if not item:
            continue
        if ":" in item:
            _append_matrix_pair(pairs, current_backend, current_checklist_parts)
            current_backend, first_checklist = [part.strip() for part in item.split(":", 1)]
            current_checklist_parts = [first_checklist] if first_checklist else []
            continue
        if not current_backend:
            preset_names = ", ".join(sorted(MATRIX_PRESETS.keys()))
            raise argparse.ArgumentTypeError(
                f"Invalid --matrix entry '{item}'. Expect 'backend:checklist', "
                f"or a preset name ({preset_names})."
            )
        current_checklist_parts.append(item)

    _append_matrix_pair(pairs, current_backend, current_checklist_parts)
    if not pairs:
        raise argparse.ArgumentTypeError("--matrix is empty")
    return pairs


def _append_matrix_pair(pairs: list[tuple[str, str]], backend: str, checklist_parts: list[str]) -> None:
    if not backend:
        return
    checklist = ",".join(part for part in checklist_parts if part)
    if not checklist:
        raise argparse.ArgumentTypeError(f"Invalid --matrix entry '{backend}:'. Checklist is empty.")
    pairs.append((backend, checklist))


def validate_checklist_indices(matrix: list[tuple[str | None, set[int]]], parser: argparse.ArgumentParser) -> None:
    for backend, checklist in matrix:
        invalid = sorted(i for i in checklist if i < 0 or i >= len(TEST_CASES))
        if invalid:
            parser.error(
                f"checklist for backend '{backend or '<default>'}' contains invalid indices: "
                f"{invalid} (valid range: 0..{len(TEST_CASES) - 1})"
            )
        disabled = _disabled_checklist_indices(backend, checklist)
        if disabled:
            disabled_names = ", ".join(TEST_CASES[index].name for index in disabled)
            parser.error(
                f"checklist for backend '{backend or '<default>'}' contains cases not enabled for that backend: "
                f"{disabled} ({disabled_names})"
            )


def _disabled_checklist_indices(backend: str | None, checklist: set[int]) -> list[int]:
    backend_name = normalize_backend(backend).env_backend
    enabled = set(selected_case_indices_for_backend(backend_name, list(range(len(TEST_CASES)))))
    return sorted(index for index in checklist if index not in enabled)


def validate_unique_backend_names(matrix: list[tuple[str | None, set[int]]], parser: argparse.ArgumentParser) -> None:
    names = [normalize_backend(backend).log_name for backend, _ in matrix]
    duplicates = sorted(name for name, count in Counter(names).items() if count > 1)
    if duplicates:
        parser.error(f"--matrix contains duplicate normalized backend names: {duplicates}")


def validate_numeric_args(args: argparse.Namespace, parser: argparse.ArgumentParser) -> None:
    if args.jobs is not None and args.jobs <= 0:
        parser.error(f"--jobs must be >= 1, got {args.jobs}")
    if args.repeat is not None and args.repeat < 1:
        parser.error(f"--repeat must be >= 1, got {args.repeat}")
    if args.timeout_scale is not None and args.timeout_scale <= 0:
        parser.error(f"--timeout-scale must be > 0, got {args.timeout_scale}")
