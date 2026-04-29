import multiprocessing
import os
import signal
import subprocess
from dataclasses import dataclass
from pathlib import Path

from tqdm import tqdm

from .cases import LOG_DIR, TEST_CASES, TestCase
from .process import run_command, terminate_process_group
from .status import TAG_COMPILE_FAIL, TAG_FAIL, TAG_OK, TAG_TIMEOUT, summarize_runs


COMPILE_TIMEOUT_SECONDS = 60
TEST_TIMEOUT_RETURN_CODE = 9999
BACKEND_SCHEDULE_RANKS = {
    "gvm-with-cache": 0,
    "rtlsim-with-cache": 1,
    "gvm-no-cache": 2,
    "rtlsim-no-cache": 3,
    "cyclesim": 4,
    "sbt": 5,
    "spike": 6,
}

_pool = None
_compile_status = None
_compile_locks = None
_run_locks = None
_active_pids = None
_interrupting = False


@dataclass(frozen=True)
class BackendRunConfig:
    env_backend: str
    name: str
    checklist: set[int]
    repeat: int


@dataclass(frozen=True)
class TestJob:
    backend: BackendRunConfig
    testcase_index: int
    testcase: TestCase
    timeout_scale: float


@dataclass(frozen=True)
class JobResult:
    backend_name: str
    testcase_index: int
    runs: list[tuple[int, str]]


def install_signal_handler() -> None:
    signal.signal(signal.SIGINT, _signal_handler)


def create_shared_state(manager) -> dict:
    paths = {str(tc.path) for tc in TEST_CASES}
    return {
        "compile_status": manager.dict(),
        "compile_locks": {path: manager.Lock() for path in paths},
        "run_locks": {path: manager.Lock() for path in paths},
        "active_pids": manager.dict(),
    }


def run_plan(
    backend_configs: list[BackendRunConfig],
    selected_indices: list[int],
    jobs: int,
    timeout_scale: float,
    shared_state: dict,
) -> tuple[list[tuple], int]:
    global _active_pids, _pool
    _active_pids = shared_state["active_pids"]

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    (LOG_DIR / "compile").mkdir(parents=True, exist_ok=True)
    for config in backend_configs:
        (LOG_DIR / config.name).mkdir(parents=True, exist_ok=True)

    results_by_backend = {
        config.name: [None] * len(TEST_CASES)
        for config in backend_configs
    }
    test_jobs = _build_test_jobs(backend_configs, selected_indices, timeout_scale)
    _print_plan_header(backend_configs, jobs, timeout_scale, test_jobs)

    _pool = multiprocessing.Pool(processes=jobs, initializer=_init_worker, initargs=(_worker_state(shared_state),))
    try:
        _collect_plan_results(backend_configs, test_jobs, results_by_backend, len(selected_indices))
    except BaseException:
        _terminate_pool()
        raise
    else:
        _close_pool()

    return _build_mode_outputs(backend_configs, selected_indices, results_by_backend)


def run_test_job(job: TestJob) -> JobResult:
    run_env = os.environ.copy()
    run_env["VENTUS_BACKEND"] = job.backend.env_backend
    compile_env = os.environ.copy()
    compile_env.pop("VENTUS_BACKEND", None)

    if job.testcase.need_make:
        compile_result = _compile_testcase(job.testcase, compile_env, job.backend.name)
        if compile_result is not None:
            return JobResult(job.backend.name, job.testcase_index, [compile_result] * job.backend.repeat)

    with _run_locks[str(job.testcase.path)]:
        runs = _run_testcase_repeated(job, run_env)
    return JobResult(job.backend.name, job.testcase_index, runs)


def _signal_handler(signum, frame) -> None:
    global _interrupting, _pool
    if _interrupting:
        raise KeyboardInterrupt
    _interrupting = True
    tqdm.write("Interrupt received, terminating all test cases...")
    if _active_pids is not None:
        for pid in list(_active_pids.keys()):
            terminate_process_group(int(pid))
    if _pool is not None:
        _pool.terminate()
    raise KeyboardInterrupt


def _build_test_jobs(
    backend_configs: list[BackendRunConfig],
    selected_indices: list[int],
    timeout_scale: float,
) -> list[TestJob]:
    ordered_configs = _schedule_backend_configs(backend_configs)
    if not ordered_configs or not selected_indices:
        return []

    jobs: list[TestJob] = []
    case_count = len(selected_indices)
    case_stride = (case_count + len(ordered_configs) - 1) // len(ordered_configs)
    for case_round in range(case_count):
        for backend_position, config in enumerate(ordered_configs):
            case_position = (case_round + backend_position * case_stride) % case_count
            index = selected_indices[case_position]
            jobs.append(TestJob(config, index, TEST_CASES[index], timeout_scale))
    return jobs


def _schedule_backend_configs(backend_configs: list[BackendRunConfig]) -> list[BackendRunConfig]:
    indexed_configs = enumerate(backend_configs)
    return [
        config
        for _, config in sorted(indexed_configs, key=lambda item: (_backend_schedule_rank(item[1]), item[0]))
    ]


def _backend_schedule_rank(config: BackendRunConfig) -> int:
    return BACKEND_SCHEDULE_RANKS.get(config.env_backend, len(BACKEND_SCHEDULE_RANKS))


def _print_plan_header(
    backend_configs: list[BackendRunConfig],
    jobs: int,
    timeout_scale: float,
    test_jobs: list[TestJob],
) -> None:
    names = ", ".join(config.name for config in backend_configs)
    print(
        f"\n>>> Running regression plan [{names}] "
        f"(jobs={jobs}, timeout scale={timeout_scale}, cases={len(test_jobs)})"
    )


def _worker_state(shared_state: dict) -> tuple:
    return (
        shared_state["compile_status"],
        shared_state["compile_locks"],
        shared_state["run_locks"],
        shared_state["active_pids"],
    )


def _init_worker(state: tuple) -> None:
    global _active_pids, _compile_locks, _compile_status, _run_locks
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    _compile_status, _compile_locks, _run_locks, _active_pids = state


def _collect_plan_results(
    backend_configs: list[BackendRunConfig],
    test_jobs: list[TestJob],
    results_by_backend: dict[str, list[list[tuple[int, str]] | None]],
    selected_count: int,
) -> None:
    bars = _create_progress_bars(backend_configs, len(test_jobs), selected_count)
    try:
        for result in _pool.imap_unordered(run_test_job, test_jobs):
            results_by_backend[result.backend_name][result.testcase_index] = result.runs
            _update_progress_bars(bars, result.backend_name, results_by_backend[result.backend_name])
    finally:
        for bar in bars.values():
            bar.close()


def _create_progress_bars(
    backend_configs: list[BackendRunConfig],
    total_jobs: int,
    selected_count: int,
) -> dict[str, tqdm]:
    bars: dict[str, tqdm] = {
        "_overall": tqdm(total=total_jobs, desc="Overall", unit="test", position=0),
    }
    for position, config in enumerate(backend_configs, start=1):
        bars[config.name] = tqdm(
            total=selected_count,
            desc=f"Running [{config.name}]",
            unit="test",
            position=position,
            leave=True,
        )
    return bars


def _update_progress_bars(
    bars: dict[str, tqdm],
    backend_name: str,
    backend_results: list[list[tuple[int, str]] | None],
) -> None:
    bars["_overall"].update(1)
    bars[backend_name].update(1)
    pass_count, fail_count, flaky_count = _count_statuses(backend_results)
    bars[backend_name].set_postfix_str(f"pass={pass_count}, fail={fail_count}, flaky={flaky_count}")


def _close_pool() -> None:
    global _pool
    if _pool is None:
        return
    _pool.close()
    _pool.join()
    _pool = None


def _terminate_pool() -> None:
    global _pool
    if _pool is None:
        return
    _pool.terminate()
    _pool.join()
    _pool = None


def _build_mode_outputs(
    backend_configs: list[BackendRunConfig],
    selected_indices: list[int],
    results_by_backend: dict[str, list[list[tuple[int, str]] | None]],
) -> tuple[list[tuple], int]:
    mode_outputs: list[tuple] = []
    overall_exit = 0
    for config in backend_configs:
        results = results_by_backend[config.name]
        checklist_failed = sorted(index for index in config.checklist if not _all_passed(results, index))
        exit_code = 0 if not checklist_failed else 1
        mode_outputs.append((config.name, exit_code, selected_indices, results, config.checklist, checklist_failed, config.repeat))
        overall_exit = overall_exit or exit_code
    return mode_outputs, overall_exit


def _count_statuses(results: list[list[tuple[int, str]] | None]) -> tuple[int, int, int]:
    pass_count = fail_count = flaky_count = 0
    for runs in results:
        if runs is None:
            continue
        _, _, tag = summarize_runs(runs)
        if tag == TAG_OK:
            pass_count += 1
        elif tag == "flaky":
            flaky_count += 1
        else:
            fail_count += 1
    return pass_count, fail_count, flaky_count


def _all_passed(results: list[list[tuple[int, str]] | None], index: int) -> bool:
    runs = results[index]
    return runs is not None and all(rc == 0 for rc, _ in runs)


def _compile_testcase(
    testcase: TestCase,
    compile_env: dict[str, str],
    backend_name: str,
) -> tuple[int, str] | None:
    key = str(testcase.path)
    compile_log_path = LOG_DIR / "compile" / f"{testcase.name}.log"
    with _compile_locks[key]:
        status = _compile_status.get(key)
        with open(compile_log_path, "a") as log_file:
            log_file.write(f"=== Compile Testcase for backend {backend_name} ===\n")
            if status == "ok":
                log_file.write("Already Compiled, Skipping...\n")
                return None
            if status == "failed":
                log_file.write("Previous compile failed, reusing failure.\n")
                return 2, TAG_COMPILE_FAIL
            result = _run_compile_command(testcase, compile_env, log_file)
            _compile_status[key] = "ok" if result is None else "failed"
            return result


def _run_compile_command(testcase: TestCase, compile_env: dict[str, str], log_file) -> tuple[int, str] | None:
    try:
        rc = run_command(["make"], testcase.path, compile_env, log_file, COMPILE_TIMEOUT_SECONDS, _active_pids)
    except subprocess.TimeoutExpired:
        log_file.write("Compile Timeout, Failed\n")
        return 2, TAG_COMPILE_FAIL
    if rc != 0:
        log_file.write("Compile Failed\n")
        return 2, TAG_COMPILE_FAIL
    log_file.write("Compile OK\n")
    return None


def _run_testcase_repeated(job: TestJob, env: dict[str, str]) -> list[tuple[int, str]]:
    runs: list[tuple[int, str]] = []
    for run_idx in range(1, job.backend.repeat + 1):
        run_log_path = _run_log_path(job.backend.name, job.testcase.name, run_idx, job.backend.repeat)
        mode = "w"
        with open(run_log_path, mode) as log_file:
            runs.append(_run_testcase_once(job, env, run_idx, log_file))
    return runs


def _run_log_path(backend_name: str, testcase_name: str, run_idx: int, repeat: int) -> Path:
    if repeat > 1:
        return LOG_DIR / backend_name / f"{testcase_name}.run{run_idx}.log"
    return LOG_DIR / backend_name / f"{testcase_name}.log"


def _run_testcase_once(job: TestJob, env: dict[str, str], run_idx: int, log_file) -> tuple[int, str]:
    repeat = job.backend.repeat
    log_file.write(f"=== Run Test ({run_idx}/{repeat}) ===\n")
    log_file.write(f"TestCase {job.testcase_index}: {job.testcase.name} begin (run {run_idx}/{repeat})...\n")
    log_file.write(f"COMMAND: {format_command(job.testcase.cmd)}\n")
    log_file.write(f"VENTUS_BACKEND: {env.get('VENTUS_BACKEND', '<unset>')}\n")
    log_file.flush()
    try:
        rc = run_command(job.testcase.cmd, job.testcase.path, env, log_file, job.testcase.timeout * job.timeout_scale, _active_pids)
    except subprocess.TimeoutExpired:
        log_file.write("\nTestcase execution timeout, Failed\n")
        return TEST_TIMEOUT_RETURN_CODE, TAG_TIMEOUT
    return rc, TAG_OK if rc == 0 else TAG_FAIL


def format_command(cmd: list[str]) -> str:
    return " ".join(repr(part) if " " in part else part for part in cmd)
