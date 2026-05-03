import multiprocessing
import os
import queue
import re
import shutil
import signal
import subprocess
import time
from dataclasses import dataclass, replace
from pathlib import Path

from tqdm import tqdm

from .cases import LOG_DIR, TEST_CASES, TestCase
from .numa import (
    NUMACTL_AUTO,
    NUMACTL_REQUIRE,
    NumaAllocator,
    NumaBinding,
    NumaBindingError,
    create_allocator,
    wrap_command,
)
from .process import CommandResult, ResourceUsage, run_command, terminate_process_group
from .progress import (
    PROGRESS_TQDM,
    ProgressOutput,
    close_progress_output,
    create_progress_output,
)
from .status import TAG_COMPILE_FAIL, TAG_FAIL, TAG_HANG, TAG_OK, TAG_TIMEOUT


COMPILE_TIMEOUT_SECONDS = 60
TEST_TIMEOUT_RETURN_CODE = 9999
RTL_GVM_WORKER_THREADS = 8
DEFAULT_WORKER_THREADS = 1
POCL_CACHE_DIR_NAME = ".pocl-cache"
RTL_GVM_BACKEND_BASES = {"rtl", "rtlsim", "gpgpu", "gvm"}
BACKEND_SCHEDULE_RANKS = {
    "gvm-with-cache": 0,
    "rtlsim-with-cache": 1,
    "gvm-no-cache": 2,
    "rtlsim-no-cache": 3,
    "cyclesim": 4,
    "sbt": 5,
    "spike": 6,
}
SECONDS_PER_MINUTE = 60
SECONDS_PER_HOUR = 60 * SECONDS_PER_MINUTE
KIB_PER_MIB = 1024
KIB_PER_GIB = 1024 * KIB_PER_MIB
NSEC_PER_SEC = 1_000_000_000

# 触发 hang 分类的"仿真还在干活"标志。命中 = 仿真在 commit 指令 / 做 lsu / 在
# 输出 testcase 友好结果——这些都说明只是慢，不是死锁。
# 没命中且 timeout = 仿真只剩 [RTL debug]@<cycle> 在递增、kernel 已经 hang。
_HANG_ACTIVITY_RE = re.compile(
    rb"(?:^sm\s+\d+\s+warp\s+\d+|lsu\.[wr]|\bfinish\b|TEST PASS|Finish the training|Verilator: end at)",
    re.MULTILINE,
)

_pool = None
_active_pids = None
_active_rep_cwds = None
_job_events = None
_interrupting = False
JOB_EVENT_STARTED = "started"


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
    run_idx: int
    total_reps: int
    timeout_scale: float
    numa_binding: NumaBinding | None = None


@dataclass(frozen=True)
class JobResult:
    backend_name: str
    testcase_index: int
    run_idx: int
    rc: int
    tag: str


@dataclass(frozen=True)
class ActiveJob:
    job: TestJob
    async_result: object
    worker_threads: int


@dataclass(frozen=True)
class RunSummary:
    result: str
    return_code: int
    total_wall_time_sec: float
    timeout_limit_sec: float
    compile_result: CommandResult | None
    execute_result: CommandResult | None


def install_signal_handler() -> None:
    signal.signal(signal.SIGINT, _signal_handler)


def create_shared_state(manager) -> dict:
    return {
        "active_pids": manager.dict(),
        "active_rep_cwds": manager.dict(),
        "job_events": manager.Queue(),
    }


def run_plan(
    backend_configs: list[BackendRunConfig],
    selected_indices: list[int],
    jobs: int,
    timeout_scale: float,
    shared_state: dict,
    *,
    numactl_policy: str,
    progress_mode: str = PROGRESS_TQDM,
) -> tuple[list[tuple], int]:
    global _active_pids, _active_rep_cwds, _job_events, _pool
    _active_pids = shared_state["active_pids"]
    _active_rep_cwds = shared_state["active_rep_cwds"]
    _job_events = shared_state["job_events"]

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    for config in backend_configs:
        _prepare_log_dir(LOG_DIR / config.name)

    results_by_backend: dict[str, list[list[tuple[int, str] | None] | None]] = {
        config.name: [None] * len(TEST_CASES)
        for config in backend_configs
    }
    test_jobs = _build_test_jobs(backend_configs, selected_indices, timeout_scale)
    _print_plan_header(
        backend_configs,
        jobs,
        timeout_scale,
        test_jobs,
        selected_indices,
        numactl_policy,
    )
    _validate_worker_thread_budget(jobs, test_jobs)
    numa_allocator = _create_numa_allocator(numactl_policy, test_jobs)

    pool_processes = _pool_process_count(jobs, test_jobs)
    _pool = multiprocessing.Pool(
        processes=pool_processes,
        initializer=_init_worker,
        initargs=(_worker_state(shared_state),),
    )
    try:
        _collect_plan_results(
            backend_configs,
            test_jobs,
            results_by_backend,
            len(selected_indices),
            jobs,
            numa_allocator,
            numactl_policy,
            progress_mode,
        )
    except BaseException:
        _terminate_pool()
        _cleanup_active_rep_cwds()
        raise
    else:
        _close_pool()

    return _build_mode_outputs(backend_configs, selected_indices, results_by_backend)


def run_test_job(job: TestJob) -> JobResult:
    _emit_job_started(job)
    run_env = os.environ.copy()
    run_env["VENTUS_BACKEND"] = job.backend.env_backend
    compile_env = os.environ.copy()
    compile_env.pop("VENTUS_BACKEND", None)

    rc, tag = _run_single_rep(job, run_env, compile_env)
    return JobResult(job.backend.name, job.testcase_index, job.run_idx, rc, tag)


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
    _cleanup_active_rep_cwds()
    raise KeyboardInterrupt


def _build_test_jobs(
    backend_configs: list[BackendRunConfig],
    selected_indices: list[int],
    timeout_scale: float,
) -> list[TestJob]:
    """把 (backend × case × rep) 完全摊平进 pool。每个 rep 在同级 .rep_* cwd
    中从干净 Git tree 或 reflink copy 独立编译、运行，避免同源目录运行产物互相覆盖。"""
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
            for run_idx in range(1, config.repeat + 1):
                jobs.append(TestJob(config, index, TEST_CASES[index], run_idx, config.repeat, timeout_scale))
    return jobs


def _schedule_backend_configs(backend_configs: list[BackendRunConfig]) -> list[BackendRunConfig]:
    indexed_configs = enumerate(backend_configs)
    return [
        config
        for _, config in sorted(indexed_configs, key=lambda item: (_backend_schedule_rank(item[1]), item[0]))
    ]


def _backend_schedule_rank(config: BackendRunConfig) -> int:
    return BACKEND_SCHEDULE_RANKS.get(config.env_backend, len(BACKEND_SCHEDULE_RANKS))


def worker_threads_for_backend(backend: str) -> int:
    backend_base = backend.split("-")[0].lower()
    if backend_base in RTL_GVM_BACKEND_BASES:
        return RTL_GVM_WORKER_THREADS
    return DEFAULT_WORKER_THREADS


def worker_threads_for_job(job: TestJob) -> int:
    return worker_threads_for_backend(job.backend.env_backend)


def should_bind_numa(job: TestJob) -> bool:
    return worker_threads_for_job(job) > DEFAULT_WORKER_THREADS


def _create_numa_allocator(policy: str, test_jobs: list[TestJob]) -> NumaAllocator:
    min_cpus_per_node = max(
        (worker_threads_for_job(job) for job in test_jobs if should_bind_numa(job)),
        default=0,
    )
    allocator, warning = create_allocator(policy, min_cpus_per_node)
    if warning is not None:
        tqdm.write(f"NUMA auto-bind disabled: {warning}")
    elif allocator.enabled:
        tqdm.write(
            "NUMA auto-bind enabled for RTL/GVM jobs "
            f"(min cpus per node={min_cpus_per_node}, numa nodes={allocator.capacity})"
        )
    return allocator


def _validate_worker_thread_budget(worker_threads: int, test_jobs: list[TestJob]) -> None:
    largest_job = max((worker_threads_for_job(job) for job in test_jobs), default=0)
    if largest_job > worker_threads:
        raise ValueError(
            f"--jobs={worker_threads} is too small for selected backends; "
            f"largest testcase requires {largest_job} worker threads"
        )


def _pool_process_count(worker_threads: int, test_jobs: list[TestJob]) -> int:
    if not test_jobs:
        return 1
    return min(worker_threads, len(test_jobs))


def _print_plan_header(
    backend_configs: list[BackendRunConfig],
    jobs: int,
    timeout_scale: float,
    test_jobs: list[TestJob],
    selected_indices: list[int],
    numactl_policy: str,
) -> None:
    names = ", ".join(config.name for config in backend_configs)
    print(
        f"\n>>> Running regression plan [{names}] "
        f"(worker threads={jobs}, timeout scale={timeout_scale}, "
        f"cases={len(selected_indices)}, total_reps={len(test_jobs)}, "
        f"numactl={numactl_policy})"
    )


def _prepare_log_dir(log_dir: Path) -> None:
    """Ensure dir exists and is empty of stale `*.log` files from previous runs.
    Stale per-mode logs from a prior commit / different testset can mislead
    analysis if mixed with the new run."""
    log_dir.mkdir(parents=True, exist_ok=True)
    for old_log in log_dir.glob("*.log"):
        try:
            old_log.unlink()
        except OSError:
            pass


def _worker_state(shared_state: dict) -> tuple:
    return (
        shared_state["active_pids"],
        shared_state["active_rep_cwds"],
        shared_state["job_events"],
    )


def _init_worker(state: tuple) -> None:
    global _active_pids, _active_rep_cwds, _job_events
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    _active_pids, _active_rep_cwds, _job_events = state


def _collect_plan_results(
    backend_configs: list[BackendRunConfig],
    test_jobs: list[TestJob],
    results_by_backend: dict[str, list[list[tuple[int, str] | None] | None]],
    selected_count: int,
    worker_thread_budget: int,
    numa_allocator: NumaAllocator,
    numactl_policy: str,
    progress_mode: str,
) -> None:
    repeat_by_backend = {config.name: config.repeat for config in backend_configs}
    started_by_backend: dict[str, list[list[bool] | None]] = {
        config.name: [None] * len(TEST_CASES)
        for config in backend_configs
    }
    progress = create_progress_output(backend_configs, len(test_jobs), selected_count, progress_mode)
    try:
        scheduler = WeightedJobScheduler(
            _pool,
            test_jobs,
            worker_thread_budget,
            numa_allocator,
            numactl_policy=numactl_policy,
        )
        while not scheduler.done:
            scheduler.submit_ready()
            _drain_job_events(started_by_backend, results_by_backend, repeat_by_backend, progress)
            progress.tick(results_by_backend, started_by_backend)
            if scheduler.collect_ready_results(
                results_by_backend,
                repeat_by_backend,
                started_by_backend,
                progress,
            ):
                continue
            time.sleep(0.1)
        _drain_job_events(started_by_backend, results_by_backend, repeat_by_backend, progress)
    finally:
        close_progress_output(progress)


class WeightedJobScheduler:
    def __init__(
        self,
        pool,
        test_jobs: list[TestJob],
        worker_thread_budget: int,
        numa_allocator: NumaAllocator | None = None,
        numactl_policy: str = NUMACTL_AUTO,
    ):
        self._pool = pool
        self._pending = list(test_jobs)
        self._active: list[ActiveJob] = []
        self._completed = 0
        self._used_worker_threads = 0
        self._total = len(test_jobs)
        self._worker_thread_budget = worker_thread_budget
        self._numa_allocator = numa_allocator or NumaAllocator([])
        self._numactl_policy = numactl_policy

    @property
    def done(self) -> bool:
        return self._completed >= self._total

    def submit_ready(self) -> None:
        next_index = 0
        while next_index < len(self._pending):
            job = self._pending[next_index]
            worker_threads = worker_threads_for_job(job)
            if self._used_worker_threads + worker_threads > self._worker_thread_budget:
                next_index += 1
                continue
            if self._should_wait_for_numa_capacity(job, worker_threads):
                next_index += 1
                continue
            self._submit_job(next_index, worker_threads)

    def collect_ready_results(
        self,
        results_by_backend: dict[str, list[list[tuple[int, str] | None] | None]],
        repeat_by_backend: dict[str, int],
        started_by_backend: dict[str, list[list[bool] | None]],
        progress: ProgressOutput,
    ) -> bool:
        collected = False
        for active in list(self._active):
            if not active.async_result.ready():
                continue
            result = active.async_result.get()
            self._active.remove(active)
            self._release_numa_binding(active.job, active.worker_threads)
            self._used_worker_threads -= active.worker_threads
            self._completed += 1
            _record_job_result(result, results_by_backend, repeat_by_backend, started_by_backend, progress)
            collected = True
        return collected

    def _submit_job(self, pending_index: int, worker_threads: int) -> None:
        job = self._pending.pop(pending_index)
        numa_binding = self._allocate_numa_binding(job)
        bound_job = replace(job, numa_binding=numa_binding) if numa_binding is not None else job
        async_result = self._pool.apply_async(run_test_job, (bound_job,))
        self._active.append(ActiveJob(bound_job, async_result, worker_threads))
        self._used_worker_threads += worker_threads

    def _allocate_numa_binding(self, job: TestJob) -> NumaBinding | None:
        if not should_bind_numa(job) or not self._numa_allocator.enabled:
            return None
        binding = self._numa_allocator.allocate(worker_threads_for_job(job))
        if binding is None and self._numactl_policy == NUMACTL_REQUIRE:
            raise NumaBindingError("NUMA CPU binding capacity unavailable")
        return binding

    def _release_numa_binding(self, job: TestJob, worker_threads: int) -> None:
        if job.numa_binding is None or not self._numa_allocator.enabled:
            return
        self._numa_allocator.release(job.numa_binding, worker_threads)

    def _should_wait_for_numa_capacity(self, job: TestJob, worker_threads: int) -> bool:
        if self._numactl_policy != NUMACTL_REQUIRE:
            return False
        if not should_bind_numa(job) or not self._numa_allocator.enabled:
            return False
        if self._numa_allocator.has_capacity(worker_threads):
            return False
        return any(active.job.numa_binding is not None for active in self._active)


def _record_job_result(
    result: JobResult,
    results_by_backend: dict[str, list[list[tuple[int, str] | None] | None]],
    repeat_by_backend: dict[str, int],
    started_by_backend: dict[str, list[list[bool] | None]],
    progress: ProgressOutput,
) -> None:
    backend_results = results_by_backend[result.backend_name]
    current = backend_results[result.testcase_index]
    if current is None:
        current = [None] * repeat_by_backend[result.backend_name]
        backend_results[result.testcase_index] = current
    current[result.run_idx - 1] = (result.rc, result.tag)
    progress.job_completed(
        result.backend_name,
        result.testcase_index,
        result.run_idx,
        result.rc,
        result.tag,
        backend_results,
        started_by_backend[result.backend_name],
    )


def _emit_job_started(job: TestJob) -> None:
    if _job_events is None:
        return
    _job_events.put((JOB_EVENT_STARTED, job.backend.name, job.testcase_index, job.run_idx))


def _drain_job_events(
    started_by_backend: dict[str, list[list[bool] | None]],
    results_by_backend: dict[str, list[list[tuple[int, str] | None] | None]],
    repeat_by_backend: dict[str, int],
    progress: ProgressOutput,
) -> None:
    if _job_events is None:
        return
    while True:
        try:
            event = _job_events.get_nowait()
        except queue.Empty:
            return
        event_type, backend_name, testcase_index, run_idx = event
        if event_type != JOB_EVENT_STARTED:
            continue
        backend_started = started_by_backend[backend_name]
        current = backend_started[testcase_index]
        if current is None:
            current = [False] * repeat_by_backend[backend_name]
            backend_started[testcase_index] = current
        current[run_idx - 1] = True
        progress.job_started(
            backend_name,
            testcase_index,
            run_idx,
            results_by_backend[backend_name],
            backend_started,
        )


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
    results_by_backend: dict[str, list[list[tuple[int, str] | None] | None]],
) -> tuple[list[tuple], int]:
    mode_outputs: list[tuple] = []
    overall_exit = 0
    for config in backend_configs:
        results = _finalize_backend_results(results_by_backend[config.name])
        checklist_failed = sorted(index for index in config.checklist if not _all_passed(results, index))
        exit_code = 0 if not checklist_failed else 1
        mode_outputs.append((config.name, exit_code, selected_indices, results, config.checklist, checklist_failed, config.repeat))
        overall_exit = overall_exit or exit_code
    return mode_outputs, overall_exit


def _finalize_backend_results(
    per_case_runs: list[list[tuple[int, str] | None] | None],
) -> list[list[tuple[int, str]] | None]:
    """Replace any leftover None placeholders within run lists with a fail marker.
    This shouldn't happen in clean completion, but guards against early termination
    so downstream summarize_runs / format_run_status see well-formed tuples."""
    finalized: list[list[tuple[int, str]] | None] = []
    for runs in per_case_runs:
        if runs is None:
            finalized.append(None)
            continue
        cleaned = [r if r is not None else (TEST_TIMEOUT_RETURN_CODE, TAG_FAIL) for r in runs]
        finalized.append(cleaned)
    return finalized


def _all_passed(results: list[list[tuple[int, str]] | None], index: int) -> bool:
    runs = results[index]
    return runs is not None and all(rc == 0 for rc, _ in runs)


def _run_compile_command(
    cwd: Path,
    compile_env: dict[str, str],
    log_file,
) -> tuple[CommandResult, tuple[int, str] | None]:
    result = run_command(
        ["make"],
        cwd,
        compile_env,
        log_file,
        COMPILE_TIMEOUT_SECONDS,
        _active_pids,
    )
    if result.timed_out:
        log_file.write("Compile Timeout, Failed\n")
        return result, (2, TAG_COMPILE_FAIL)
    if result.return_code != 0:
        log_file.write("Compile Failed\n")
        return result, (2, TAG_COMPILE_FAIL)
    log_file.write("Compile OK\n")
    return result, None


def _run_single_rep(job: TestJob, env: dict[str, str], compile_env: dict[str, str]) -> tuple[int, str]:
    """Run one rep in an isolated sibling cwd so relative ../../data paths keep
    the same meaning while run-generated files stay local to that rep."""
    run_log_path = _run_log_path(job.backend.name, job.testcase.name, job.run_idx, job.total_reps)
    rep_cwd: Path | None = None
    total_start_ns = time.monotonic_ns()
    compile_command_result: CommandResult | None = None
    execute_command_result: CommandResult | None = None
    timeout_limit_sec = job.testcase.timeout * job.timeout_scale
    try:
        rep_cwd = _make_rep_cwd(job.testcase.path, job.run_idx, os.getpid())
        rep_env = _env_with_rep_pocl_cache(env, rep_cwd)
        rep_compile_env = _env_with_rep_pocl_cache(compile_env, rep_cwd)
        run_cmd = wrap_command(job.testcase.cmd, job.numa_binding)
        with open(run_log_path, "w") as log_file:
            log_file.write(f"=== Run Test ({job.run_idx}/{job.total_reps}) ===\n")
            log_file.write(
                f"TestCase {job.testcase_index}: {job.testcase.name} "
                f"begin (run {job.run_idx}/{job.total_reps})...\n"
            )
            if job.numa_binding is not None:
                log_file.write(f"NUMACTL_NODE: {job.numa_binding.node}\n")
                log_file.write(f"NUMACTL_CPUS: {format_cpu_list(job.numa_binding.cpus)}\n")
            log_file.write(f"COMMAND: {format_command(run_cmd)}\n")
            log_file.write(f"VENTUS_BACKEND: {rep_env.get('VENTUS_BACKEND', '<unset>')}\n")
            log_file.write(f"REP_CWD: {rep_cwd}\n")
            log_file.write(f"POCL_CACHE_DIR: {rep_env['POCL_CACHE_DIR']}\n")
            log_file.flush()
            if job.testcase.need_make:
                log_file.write("=== Compile Testcase ===\n")
                compile_command_result, compile_failure = _run_compile_command(
                    rep_cwd,
                    rep_compile_env,
                    log_file,
                )
                log_file.flush()
                if compile_failure is not None:
                    rc, tag = compile_failure
                    _write_run_summary(
                        log_file,
                        RunSummary(
                            result=tag,
                            return_code=rc,
                            total_wall_time_sec=_elapsed_sec(total_start_ns),
                            timeout_limit_sec=timeout_limit_sec,
                            compile_result=compile_command_result,
                            execute_result=None,
                        ),
                    )
                    return rc, tag
                log_file.write("=== Execute Testcase ===\n")
                log_file.flush()
            execute_command_result = run_command(
                run_cmd,
                rep_cwd,
                rep_env,
                log_file,
                timeout_limit_sec,
                _active_pids,
            )
            if execute_command_result.timed_out:
                log_file.write("\nTestcase execution timeout, Failed\n")
                log_file.flush()
                tag = _classify_timeout(run_log_path)
                if tag == TAG_HANG:
                    log_file.write("Classified: HANG (no commit/lsu activity in tail; kernel stuck)\n")
                else:
                    log_file.write("Classified: TIMEOUT (still active in tail; ran out of wall-clock)\n")
                rc = TEST_TIMEOUT_RETURN_CODE
            else:
                rc = execute_command_result.return_code
                tag = TAG_OK if rc == 0 else TAG_FAIL
            _write_run_summary(
                log_file,
                RunSummary(
                    result=tag,
                    return_code=rc,
                    total_wall_time_sec=_elapsed_sec(total_start_ns),
                    timeout_limit_sec=timeout_limit_sec,
                    compile_result=compile_command_result,
                    execute_result=execute_command_result,
                ),
            )
            return rc, tag
    finally:
        _cleanup_rep_cwd(rep_cwd)


def _write_run_summary(log_file, summary: RunSummary) -> None:
    resources = _combined_resources(summary.compile_result, summary.execute_result)
    log_file.write("\n=== Regression Run Summary ===\n")
    log_file.write(f"result: {summary.result}\n")
    log_file.write(f"return_code: {summary.return_code}\n")
    log_file.write("time:\n")
    log_file.write(f"  total_wall: {_format_duration(summary.total_wall_time_sec)}\n")
    log_file.write(f"  compile_wall: {_format_phase_duration(summary.compile_result)}\n")
    log_file.write(f"  execute_wall: {_format_phase_duration(summary.execute_result)}\n")
    log_file.write(f"  timeout_limit: {_format_duration(summary.timeout_limit_sec)}\n")
    log_file.write("phases:\n")
    log_file.write(f"  compile: {_format_phase_status(summary.compile_result)}\n")
    log_file.write(f"  execute: {_format_phase_status(summary.execute_result)}\n")
    log_file.write("resources:\n")
    log_file.write(f"  peak_rss: {_format_rss(resources.peak_rss_kb)}\n")
    log_file.write(f"  user_cpu: {_format_duration(resources.user_time_sec)}\n")
    log_file.write(f"  system_cpu: {_format_duration(resources.system_time_sec)}\n")
    log_file.write(f"  cpu_time_total: {_format_duration(_cpu_time_total(resources))}\n")
    log_file.write(f"  minor_page_faults: {resources.minor_page_faults}\n")
    log_file.write(f"  major_page_faults: {resources.major_page_faults}\n")
    log_file.write(f"  voluntary_context_switches: {resources.voluntary_context_switches}\n")
    log_file.write(f"  involuntary_context_switches: {resources.involuntary_context_switches}\n")
    log_file.write(f"  filesystem_input_ops: {resources.filesystem_input_ops}\n")
    log_file.write(f"  filesystem_output_ops: {resources.filesystem_output_ops}\n")
    log_file.flush()


def _combined_resources(*results: CommandResult | None) -> ResourceUsage:
    resources = ResourceUsage.zero()
    for result in results:
        if result is None:
            continue
        resources = resources.combine(result.resources)
    return resources


def _elapsed_sec(start_ns: int) -> float:
    return (time.monotonic_ns() - start_ns) / NSEC_PER_SEC


def _format_phase_duration(result: CommandResult | None) -> str:
    if result is None:
        return "not_run"
    return _format_duration(result.wall_time_sec)


def _format_phase_status(result: CommandResult | None) -> str:
    if result is None:
        return "not_run"
    timed_out = "yes" if result.timed_out else "no"
    return f"return_code={result.return_code}, timed_out={timed_out}"


def _format_duration(seconds: float) -> str:
    if seconds >= SECONDS_PER_HOUR:
        hours = int(seconds // SECONDS_PER_HOUR)
        remainder = seconds - hours * SECONDS_PER_HOUR
        minutes = int(remainder // SECONDS_PER_MINUTE)
        secs = remainder - minutes * SECONDS_PER_MINUTE
        return f"{hours}h {minutes:02d}m {secs:06.3f}s"
    if seconds >= SECONDS_PER_MINUTE:
        minutes = int(seconds // SECONDS_PER_MINUTE)
        secs = seconds - minutes * SECONDS_PER_MINUTE
        return f"{minutes}m {secs:06.3f}s"
    return f"{seconds:.3f}s"


def _format_rss(kib: int) -> str:
    if kib >= KIB_PER_GIB:
        return f"{kib / KIB_PER_GIB:.2f} GiB ({kib} KiB)"
    if kib >= KIB_PER_MIB:
        return f"{kib / KIB_PER_MIB:.2f} MiB ({kib} KiB)"
    return f"{kib} KiB"


def _cpu_time_total(resources: ResourceUsage) -> float:
    return resources.user_time_sec + resources.system_time_sec


def _env_with_rep_pocl_cache(base_env: dict[str, str], rep_cwd: Path) -> dict[str, str]:
    env = base_env.copy()
    env["POCL_CACHE_DIR"] = str(rep_cwd / POCL_CACHE_DIR_NAME)
    return env


def _run_log_path(backend_name: str, testcase_name: str, run_idx: int, repeat: int) -> Path:
    if repeat > 1:
        return LOG_DIR / backend_name / f"{testcase_name}.run{run_idx}.log"
    return LOG_DIR / backend_name / f"{testcase_name}.log"


def _make_rep_cwd(testcase_path: Path, rep_idx: int, run_pid: int) -> Path:
    """Build sibling cwd `<parent>/.rep_<name>_<pid>_<idx>/` for one rep."""
    rep_cwd = testcase_path.parent / f".rep_{testcase_path.name}_{run_pid}_{rep_idx}"
    _register_rep_cwd(rep_cwd)
    if rep_cwd.exists():
        shutil.rmtree(rep_cwd)
    if not _try_export_git_tree(testcase_path, rep_cwd):
        _copy_tree_with_reflink(testcase_path, rep_cwd)
    return rep_cwd


def _try_export_git_tree(testcase_path: Path, rep_cwd: Path) -> bool:
    try:
        repo_root = _git_toplevel(testcase_path)
        rel_path = testcase_path.resolve().relative_to(repo_root)
    except (OSError, ValueError, subprocess.CalledProcessError):
        return False

    rep_cwd.mkdir(parents=True, exist_ok=False)
    archive = subprocess.Popen(
        ["git", "-C", str(repo_root), "archive", "--format=tar", "HEAD", str(rel_path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    try:
        strip_components = str(len(rel_path.parts))
        tar = subprocess.run(
            ["tar", "-xf", "-", "--strip-components", strip_components, "-C", str(rep_cwd)],
            stdin=archive.stdout,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if archive.stdout is not None:
            archive.stdout.close()
        archive_rc = archive.wait()
    except OSError:
        archive.kill()
        archive.wait()
        shutil.rmtree(rep_cwd, ignore_errors=True)
        return False

    if archive_rc == 0 and tar.returncode == 0:
        return True

    shutil.rmtree(rep_cwd, ignore_errors=True)
    return False


def _git_toplevel(path: Path) -> Path:
    result = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "--show-toplevel"],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    return Path(result.stdout.strip()).resolve()


def _copy_tree_with_reflink(testcase_path: Path, rep_cwd: Path) -> None:
    rep_cwd.mkdir(parents=True, exist_ok=False)
    subprocess.run(
        ["cp", "-a", "--reflink=auto", f"{testcase_path}/.", str(rep_cwd)],
        check=True,
    )


def _register_rep_cwd(rep_cwd: Path) -> None:
    if _active_rep_cwds is not None:
        _active_rep_cwds[str(rep_cwd)] = True


def _cleanup_rep_cwd(rep_cwd: Path | None) -> None:
    if rep_cwd is None:
        return
    shutil.rmtree(rep_cwd, ignore_errors=True)
    if _active_rep_cwds is not None:
        _active_rep_cwds.pop(str(rep_cwd), None)


def _cleanup_active_rep_cwds() -> None:
    if _active_rep_cwds is None:
        return
    for rep_cwd in list(_active_rep_cwds.keys()):
        shutil.rmtree(rep_cwd, ignore_errors=True)
        _active_rep_cwds.pop(rep_cwd, None)


def _classify_timeout(log_path: Path, tail_bytes: int = 16384) -> str:
    """Scan the last `tail_bytes` bytes of the run log:
    - hit on commit/lsu/Verilator markers → TAG_TIMEOUT (slow but progressing)
    - no hits → TAG_HANG (only [RTL debug]@<cycle> ticking, kernel stuck)"""
    try:
        with open(log_path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - tail_bytes))
            tail = f.read()
        return TAG_TIMEOUT if _HANG_ACTIVITY_RE.search(tail) else TAG_HANG
    except OSError:
        return TAG_TIMEOUT


def format_command(cmd: list[str]) -> str:
    return " ".join(repr(part) if " " in part else part for part in cmd)


def format_cpu_list(cpus: tuple[int, ...]) -> str:
    return ",".join(str(cpu) for cpu in cpus)
