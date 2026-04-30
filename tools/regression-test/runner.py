import fnmatch
import multiprocessing
import os
import re
import shutil
import signal
import subprocess
from dataclasses import dataclass
from pathlib import Path

from tqdm import tqdm

from .cases import LOG_DIR, TEST_CASES, TestCase
from .process import run_command, terminate_process_group
from .status import TAG_COMPILE_FAIL, TAG_FAIL, TAG_HANG, TAG_OK, TAG_TIMEOUT, summarize_runs


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

# Per-rep cwd 的 symlink 排除清单：测例自己 runtime 重写/重新生成的产物。
# 把它们排除在 symlink 之外，否则多个并行 rep 会通过 symlink 写穿透到原始
# 文件、相互覆盖，引发 "ELF: cannot open file 'object0.riscv'" 等 race。
# 让每个 rep 在自己的 cwd 里 fresh 生成这些文件即可。
_RUNTIME_PRODUCT_GLOBS = (
    "object0.*",                                  # PoCL kernel artifacts: cl/dump/riscv/vmem/riscv.log
    "*_0.log", "*_0.data", "*_0.metadata",        # PoCL per-kernel runtime files
    "gvm.log", "mygvm.log",                       # GVM trace
    "*.fst", "*.fst.hier",                        # Verilator/wave dumps
    "*.kernel.log",
)

# 触发 hang 分类的"仿真还在干活"标志。命中 = 仿真在 commit 指令 / 做 lsu / 在
# 输出 testcase 友好结果——这些都说明只是慢，不是死锁。
# 没命中且 timeout = 仿真只剩 [RTL debug]@<cycle> 在递增、kernel 已经 hang。
_HANG_ACTIVITY_RE = re.compile(
    rb"(?:^sm\s+\d+\s+warp\s+\d+|lsu\.[wr]|\bfinish\b|TEST PASS|Finish the training|Verilator: end at)",
    re.MULTILINE,
)

_pool = None
_compile_status = None
_compile_locks = None
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
    run_idx: int
    total_reps: int
    timeout_scale: float


@dataclass(frozen=True)
class JobResult:
    backend_name: str
    testcase_index: int
    run_idx: int
    rc: int
    tag: str


def install_signal_handler() -> None:
    signal.signal(signal.SIGINT, _signal_handler)


def create_shared_state(manager) -> dict:
    paths = {str(tc.path) for tc in TEST_CASES}
    return {
        "compile_status": manager.dict(),
        "compile_locks": {path: manager.Lock() for path in paths},
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
    _prepare_log_dir(LOG_DIR / "compile")
    for config in backend_configs:
        _prepare_log_dir(LOG_DIR / config.name)

    results_by_backend: dict[str, list[list[tuple[int, str] | None] | None]] = {
        config.name: [None] * len(TEST_CASES)
        for config in backend_configs
    }
    test_jobs = _build_test_jobs(backend_configs, selected_indices, timeout_scale)
    _print_plan_header(backend_configs, jobs, timeout_scale, test_jobs, selected_indices)

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
    """Run a single rep of one (backend, testcase). Compile is gated by a per-path
    lock + shared compile_status so parallel reps of the same case make-once.
    The actual run happens in a per-rep sibling cwd so reps of the same case can
    safely run concurrently without clobbering each other's runtime products."""
    run_env = os.environ.copy()
    run_env["VENTUS_BACKEND"] = job.backend.env_backend
    compile_env = os.environ.copy()
    compile_env.pop("VENTUS_BACKEND", None)

    if job.testcase.need_make:
        compile_result = _compile_testcase(job.testcase, compile_env, job.backend.name)
        if compile_result is not None:
            rc, tag = compile_result
            return JobResult(job.backend.name, job.testcase_index, job.run_idx, rc, tag)

    rc, tag = _run_single_rep(job, run_env)
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
    raise KeyboardInterrupt


def _build_test_jobs(
    backend_configs: list[BackendRunConfig],
    selected_indices: list[int],
    timeout_scale: float,
) -> list[TestJob]:
    """把 (backend × case × rep) 完全摊平进 pool。慢 case (nn_64k 900s, backprop_1024
    300s) 的多个 reps 也能在不同 worker 上同时跑，避免被串行化把 wall time 推到很长。
    Per-rep cwd isolation (_make_rep_cwd) 保证同 case 的多 reps 不会互相覆盖
    cwd 下的 runtime 产物 (object0.riscv / gvm.log / *.fst 等)。"""
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


def _print_plan_header(
    backend_configs: list[BackendRunConfig],
    jobs: int,
    timeout_scale: float,
    test_jobs: list[TestJob],
    selected_indices: list[int],
) -> None:
    names = ", ".join(config.name for config in backend_configs)
    print(
        f"\n>>> Running regression plan [{names}] "
        f"(jobs={jobs}, timeout scale={timeout_scale}, "
        f"cases={len(selected_indices)}, total_reps={len(test_jobs)})"
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
        shared_state["compile_status"],
        shared_state["compile_locks"],
        shared_state["active_pids"],
    )


def _init_worker(state: tuple) -> None:
    global _active_pids, _compile_locks, _compile_status
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    _compile_status, _compile_locks, _active_pids = state


def _collect_plan_results(
    backend_configs: list[BackendRunConfig],
    test_jobs: list[TestJob],
    results_by_backend: dict[str, list[list[tuple[int, str] | None] | None]],
    selected_count: int,
) -> None:
    repeat_by_backend = {config.name: config.repeat for config in backend_configs}
    bars = _create_progress_bars(backend_configs, len(test_jobs), selected_count)
    try:
        for result in _pool.imap_unordered(run_test_job, test_jobs):
            backend_results = results_by_backend[result.backend_name]
            current = backend_results[result.testcase_index]
            if current is None:
                current = [None] * repeat_by_backend[result.backend_name]
                backend_results[result.testcase_index] = current
            current[result.run_idx - 1] = (result.rc, result.tag)
            _update_progress_bars(bars, result.backend_name, backend_results)
    finally:
        for bar in bars.values():
            bar.close()


def _create_progress_bars(
    backend_configs: list[BackendRunConfig],
    total_reps: int,
    selected_count: int,
) -> dict[str, tqdm]:
    bars: dict[str, tqdm] = {
        "_overall": tqdm(total=total_reps, desc="Overall", unit="rep", position=0),
    }
    for position, config in enumerate(backend_configs, start=1):
        bars[config.name] = tqdm(
            total=selected_count * config.repeat,
            desc=f"Running [{config.name}]",
            unit="rep" if config.repeat > 1 else "test",
            position=position,
            leave=True,
        )
    return bars


def _update_progress_bars(
    bars: dict[str, tqdm],
    backend_name: str,
    backend_results: list[list[tuple[int, str] | None] | None],
) -> None:
    bars["_overall"].update(1)
    bars[backend_name].update(1)
    pass_count, fail_count, flaky_count, running_count = _count_statuses_with_running(backend_results)
    bars[backend_name].set_postfix_str(
        f"pass={pass_count}, fail={fail_count}, flaky={flaky_count}, running={running_count}"
    )


def _count_statuses_with_running(
    backend_results: list[list[tuple[int, str] | None] | None],
) -> tuple[int, int, int, int]:
    """Per-case 完成度统计：跑完的 case 算 pass/fail/flaky；只有部分 reps
    回收的 case 算 running，让中途状态在进度条 postfix 里直观可见。"""
    pass_count = fail_count = flaky_count = running_count = 0
    for runs in backend_results:
        if runs is None:
            continue
        if any(r is None for r in runs):
            running_count += 1
            continue
        _, _, tag = summarize_runs(runs)
        if tag == TAG_OK:
            pass_count += 1
        elif tag == "flaky":
            flaky_count += 1
        else:
            fail_count += 1
    return pass_count, fail_count, flaky_count, running_count


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


def _run_single_rep(job: TestJob, env: dict[str, str]) -> tuple[int, str]:
    """Run one rep in an isolated per-rep cwd. The cwd is a sibling of testcase.path
    populated with symlinks to the testcase inputs (excluding runtime products),
    so concurrent reps of the same case do not collide on object0.riscv etc."""
    run_log_path = _run_log_path(job.backend.name, job.testcase.name, job.run_idx, job.total_reps)
    rep_cwd: Path | None = None
    try:
        rep_cwd = _make_rep_cwd(job.testcase.path, job.run_idx, os.getpid())
        with open(run_log_path, "w") as log_file:
            log_file.write(f"=== Run Test ({job.run_idx}/{job.total_reps}) ===\n")
            log_file.write(f"TestCase {job.testcase_index}: {job.testcase.name} begin (run {job.run_idx}/{job.total_reps})...\n")
            log_file.write(f"COMMAND: {format_command(job.testcase.cmd)}\n")
            log_file.write(f"VENTUS_BACKEND: {env.get('VENTUS_BACKEND', '<unset>')}\n")
            log_file.write(f"REP_CWD: {rep_cwd}\n")
            log_file.flush()
            try:
                rc = run_command(job.testcase.cmd, rep_cwd, env, log_file, job.testcase.timeout * job.timeout_scale, _active_pids)
            except subprocess.TimeoutExpired:
                log_file.write("\nTestcase execution timeout, Failed\n")
                log_file.flush()
                tag = _classify_timeout(run_log_path)
                if tag == TAG_HANG:
                    log_file.write("Classified: HANG (no commit/lsu activity in tail; kernel stuck)\n")
                else:
                    log_file.write("Classified: TIMEOUT (still active in tail; ran out of wall-clock)\n")
                return TEST_TIMEOUT_RETURN_CODE, tag
            return rc, (TAG_OK if rc == 0 else TAG_FAIL)
    finally:
        _cleanup_rep_cwd(rep_cwd)


def _run_log_path(backend_name: str, testcase_name: str, run_idx: int, repeat: int) -> Path:
    if repeat > 1:
        return LOG_DIR / backend_name / f"{testcase_name}.run{run_idx}.log"
    return LOG_DIR / backend_name / f"{testcase_name}.log"


def _is_runtime_product(name: str) -> bool:
    return any(fnmatch.fnmatch(name, pat) for pat in _RUNTIME_PRODUCT_GLOBS)


def _make_rep_cwd(testcase_path: Path, rep_idx: int, run_pid: int) -> Path:
    """Build sibling cwd `<parent>/.rep_<name>_<pid>_<idx>/` for one rep.
    Inputs are symlinked from the original; runtime products (object0.*, *_0.log,
    gvm.log, *.fst) are excluded so each rep generates them fresh in its own cwd.

    Sibling location matters: many cmds use '../../data/...' relative paths,
    which still resolve correctly because parent.parent is identical to the
    original testcase.path.parent.parent."""
    rep_cwd = testcase_path.parent / f".rep_{testcase_path.name}_{run_pid}_{rep_idx}"
    if rep_cwd.exists():
        shutil.rmtree(rep_cwd, ignore_errors=True)
    rep_cwd.mkdir(parents=True, exist_ok=False)
    src_abs = testcase_path.resolve()
    for entry in testcase_path.iterdir():
        if _is_runtime_product(entry.name):
            continue
        os.symlink(src_abs / entry.name, rep_cwd / entry.name)
    return rep_cwd


def _cleanup_rep_cwd(rep_cwd: Path | None) -> None:
    if rep_cwd is None:
        return
    shutil.rmtree(rep_cwd, ignore_errors=True)


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
