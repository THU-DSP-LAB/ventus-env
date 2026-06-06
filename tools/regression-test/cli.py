import argparse
import multiprocessing
import os
import sys

from .cases import LOG_DIR, MATRIX_PRESETS, REQUIRED_PRESETS, TEST_CASES
from .numa import NUMACTL_AUTO, NUMACTL_CHOICES
from .options import (
    WITHCACHE_DEFAULT_REPEAT,
    detect_default_repeat,
    parse_checklist,
    parse_matrix,
    normalize_backend,
    suggest_default_jobs,
    suggest_default_matrix_jobs,
    validate_checklist_indices,
    validate_numeric_args,
    validate_unique_backend_names,
)
from .report import print_github_annotation_summary, print_mode_report
from .runner import BackendRunConfig, create_shared_state, install_signal_handler, run_plan


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    validate_numeric_args(args, parser)
    matrix, matrix_mode = build_matrix(args, parser)
    validate_checklist_indices(matrix, parser)
    if matrix_mode:
        validate_unique_backend_names(matrix, parser)

    timeout_scale = args.timeout_scale if args.timeout_scale is not None else 1
    jobs = args.jobs if args.jobs is not None else choose_default_jobs(matrix, matrix_mode)

    LOG_DIR.mkdir(parents=True, exist_ok=True)

    try:
        with multiprocessing.Manager() as manager:
            install_signal_handler()
            shared_state = create_shared_state(manager)
            mode_outputs, overall_exit = run_all_modes(args, matrix, matrix_mode, jobs, timeout_scale, shared_state)
        print_reports(mode_outputs, overall_exit)
        return effective_exit_code(overall_exit, args.allow_checklist_failure)
    except KeyboardInterrupt:
        return 130
    finally:
        restore_terminal_echo()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Ventus regression test runner")
    parser.add_argument("-t", "--timeout-scale", type=float, default=None, help="Timeout scale (default: 1)")
    parser.add_argument(
        "-j",
        "--jobs",
        type=int,
        default=None,
        help="Worker thread budget (default: nproc * 2 / 3)",
    )
    parser.add_argument(
        "--checklist",
        type=str,
        default=None,
        help=(
            "Single-mode regression: which testcases must pass. "
            "Accepts a comma-separated index list (e.g. 0,2,5) "
            "or a preset name: " + ", ".join(sorted(REQUIRED_PRESETS.keys())) + ". "
            "When --matrix is set, overrides every matrix entry's default checklist."
        ),
    )
    parser.add_argument(
        "--matrix",
        type=str,
        default=None,
        help=(
            "Run multiple (VENTUS_BACKEND, checklist) combinations in one invocation. "
            "Each mode runs every testcase; checklist only controls final pass/fail. "
            "Per-mode logs go to regression-test-logs/<backend>/. "
            "Accepts a preset name (" + ", ".join(sorted(MATRIX_PRESETS.keys())) + ") "
            "or explicit form 'backend1:checklist1,backend2:checklist2'. "
            "Use semicolons between entries when a checklist itself contains commas."
        ),
    )
    parser.add_argument(
        "--repeat",
        type=int,
        default=None,
        help=(
            "Repeat each testcase N times; the case is considered passed only if all N "
            "runs pass. Per-iteration logs are written to <name>.run<i>.log. "
            f"Default: {WITHCACHE_DEFAULT_REPEAT} for RTL/GVM backends using a with-cache "
            "suffix, 1 otherwise. Explicitly setting this flag overrides the default for all modes."
        ),
    )
    parser.add_argument(
        "--numactl",
        choices=NUMACTL_CHOICES,
        default=NUMACTL_AUTO,
        help=(
            "CPU/memory bind RTL/GVM testcase execution with numactl. "
            "auto binds when numactl and lscpu topology are available; "
            "require exits if binding cannot be prepared; off disables binding."
        ),
    )
    parser.add_argument(
        "--progress",
        choices=("tqdm", "ci", "none"),
        default="tqdm",
        help=(
            "Progress output mode. tqdm keeps the interactive progress bars; "
            "ci prints plain one-line events for GitHub Actions; none only prints reports."
        ),
    )
    parser.add_argument(
        "--allow-checklist-failure",
        action="store_true",
        help=(
            "Exit 0 after a completed run even when checklist testcases fail. "
            "Argument errors and runner exceptions still exit non-zero; testcase "
            "failures recorded in the completed report do not."
        ),
    )
    return parser


def build_matrix(args: argparse.Namespace, parser: argparse.ArgumentParser) -> tuple[list[tuple[str | None, set[int]]], bool]:
    try:
        if args.matrix is not None:
            return [(backend, checklist) for backend, checklist in parse_matrix(args.matrix, args.checklist)], True
        default_backend = normalize_backend(None).env_backend
        if args.checklist is not None:
            return [(None, parse_checklist(args.checklist, default_backend))], False
        return [(None, parse_checklist("all", default_backend))], False
    except argparse.ArgumentTypeError as exc:
        parser.error(str(exc))


def choose_default_jobs(matrix: list[tuple[str | None, set[int]]], matrix_mode: bool) -> int:
    if matrix_mode:
        return suggest_default_matrix_jobs(len(TEST_CASES) * len(matrix))
    return suggest_default_jobs()


def run_all_modes(
    args: argparse.Namespace,
    matrix: list[tuple[str | None, set[int]]],
    matrix_mode: bool,
    jobs: int,
    timeout_scale: float,
    shared_state: dict,
) -> tuple[list[tuple], int]:
    selected_indices = list(range(len(TEST_CASES)))
    backend_configs: list[BackendRunConfig] = []
    for backend, checklist in matrix:
        backend_selection = normalize_backend(backend)
        repeat = args.repeat if args.repeat is not None else detect_default_repeat(backend_selection.env_backend)
        backend_configs.append(BackendRunConfig(
            backend_selection.env_backend,
            backend_selection.log_name,
            checklist,
            repeat,
        ))
    return run_plan(
        backend_configs,
        selected_indices,
        jobs,
        timeout_scale,
        shared_state,
        numactl_policy=args.numactl,
        progress_mode=args.progress,
    )


def print_reports(mode_outputs: list[tuple], overall_exit: int) -> None:
    for backend_name, exit_code, selected_indices, results, checklist, checklist_failed, repeat in mode_outputs:
        if repeat > 1:
            print(f"\n[{backend_name}] repeat={repeat} per testcase")
        print_mode_report(backend_name, selected_indices, results, checklist, checklist_failed, exit_code)
    print_github_annotation_summary(mode_outputs)

    if len(mode_outputs) > 1:
        overall_symbol = "\033[92mOK\033[0m" if overall_exit == 0 else "\033[91mFAIL\033[0m"
        overall_desc = "All modes passed." if overall_exit == 0 else "Some modes failed."
        print(f"\n=== Overall ===\n{overall_desc} {overall_symbol}")


def effective_exit_code(overall_exit: int, allow_checklist_failure: bool) -> int:
    if allow_checklist_failure:
        return 0
    return overall_exit


def restore_terminal_echo() -> None:
    if "NOTEBOOK_BASH_KERNEL_CAPABILITIES" not in os.environ and sys.stdin.isatty():
        os.system("stty echo")
