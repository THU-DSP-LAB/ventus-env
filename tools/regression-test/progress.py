import time
from typing import Any

from tqdm import tqdm

from .cases import TEST_CASES
from .status import TAG_OK, summarize_runs


OVERALL_PROGRESS_KEY = "_overall"
PROGRESS_TQDM = "tqdm"
PROGRESS_CI = "ci"
PROGRESS_NONE = "none"
CI_PROGRESS_SUMMARY_INTERVAL_SECONDS = 30
CI_PROGRESS_HEARTBEAT_INTERVAL_SECONDS = 5 * 60
TQDM_BAR_FORMAT = "{desc}: {percentage:3.0f}%|{bar}| {n_fmt}/{total_fmt} {postfix}"
TQDM_BACKEND_DISPLAY_NAMES = {
    "rtl-with-cache": "rtl-cache",
    "rtl-no-cache": "rtl-nocache",
    "rtlsim-with-cache": "rtl-cache",
    "rtlsim-no-cache": "rtl-nocache",
    "gvm-with-cache": "rtl-cache-gvm",
    "gvm-no-cache": "rtl-nocache-gvm",
    "rtlsim-with-cache-gvm": "rtl-cache-gvm",
    "rtlsim-no-cache-gvm": "rtl-nocache-gvm",
}


def _print_ci_event(message: str) -> None:
    print(message, flush=True)


class ProgressOutput:
    def job_started(
        self,
        backend_name: str,
        testcase_index: int,
        run_idx: int,
        backend_results: list[list[tuple[int, str] | None] | None],
        backend_started: list[list[bool] | None],
    ) -> None:
        pass

    def job_completed(
        self,
        backend_name: str,
        testcase_index: int,
        run_idx: int,
        rc: int,
        tag: str,
        backend_results: list[list[tuple[int, str] | None] | None],
        backend_started: list[list[bool] | None],
    ) -> None:
        pass

    def close(self) -> None:
        pass

    def tick(
        self,
        results_by_backend: dict[str, list[list[tuple[int, str] | None] | None]],
        started_by_backend: dict[str, list[list[bool] | None]],
    ) -> None:
        pass


class TqdmProgressOutput(ProgressOutput):
    def __init__(self, bars: dict[str, tqdm]):
        self._bars = bars

    def job_started(
        self,
        backend_name: str,
        testcase_index: int,
        run_idx: int,
        backend_results: list[list[tuple[int, str] | None] | None],
        backend_started: list[list[bool] | None],
    ) -> None:
        self._update(backend_name, backend_results, backend_started, completed=False)

    def job_completed(
        self,
        backend_name: str,
        testcase_index: int,
        run_idx: int,
        rc: int,
        tag: str,
        backend_results: list[list[tuple[int, str] | None] | None],
        backend_started: list[list[bool] | None],
    ) -> None:
        self._update(backend_name, backend_results, backend_started, completed=True)

    def close(self) -> None:
        for bar in self._bars.values():
            bar.close()

    def _update(
        self,
        backend_name: str,
        backend_results: list[list[tuple[int, str] | None] | None],
        backend_started: list[list[bool] | None],
        completed: bool,
    ) -> None:
        if completed and OVERALL_PROGRESS_KEY in self._bars:
            self._bars[OVERALL_PROGRESS_KEY].update(1)
        if completed:
            self._bars[backend_name].update(1)
        pass_count, fail_count, flaky_count, running_count = _count_statuses_with_running(
            backend_results,
            backend_started,
        )
        self._bars[backend_name].set_postfix_str(
            _format_tqdm_status_postfix(pass_count, fail_count, flaky_count, running_count)
        )


class CiProgressOutput(ProgressOutput):
    def __init__(self, total_reps: int):
        self._total_reps = total_reps
        self._completed_reps = 0
        self._last_summary_at = 0.0
        self._last_heartbeat_at = time.monotonic()

    def job_started(
        self,
        backend_name: str,
        testcase_index: int,
        run_idx: int,
        backend_results: list[list[tuple[int, str] | None] | None],
        backend_started: list[list[bool] | None],
    ) -> None:
        testcase = TEST_CASES[testcase_index].name
        _print_ci_event(f"[start] backend={backend_name} case={testcase} run={run_idx}")

    def job_completed(
        self,
        backend_name: str,
        testcase_index: int,
        run_idx: int,
        rc: int,
        tag: str,
        backend_results: list[list[tuple[int, str] | None] | None],
        backend_started: list[list[bool] | None],
    ) -> None:
        self._completed_reps += 1
        testcase = TEST_CASES[testcase_index].name
        _print_ci_event(
            f"[done] backend={backend_name} case={testcase} run={run_idx} "
            f"rc={rc} status={tag} completed={self._completed_reps}/{self._total_reps}"
        )
        self._print_summary_if_due(backend_name, backend_results, backend_started)

    def _print_summary_if_due(
        self,
        backend_name: str,
        backend_results: list[list[tuple[int, str] | None] | None],
        backend_started: list[list[bool] | None],
    ) -> None:
        now = time.monotonic()
        if now - self._last_summary_at < CI_PROGRESS_SUMMARY_INTERVAL_SECONDS:
            return
        self._last_summary_at = now
        pass_count, fail_count, flaky_count, running_count = _count_statuses_with_running(
            backend_results,
            backend_started,
        )
        _print_ci_event(
            f"[summary] backend={backend_name} pass={pass_count} fail={fail_count} "
            f"flaky={flaky_count} running={running_count} "
            f"completed={self._completed_reps}/{self._total_reps}"
        )

    def tick(
        self,
        results_by_backend: dict[str, list[list[tuple[int, str] | None] | None]],
        started_by_backend: dict[str, list[list[bool] | None]],
    ) -> None:
        now = time.monotonic()
        if now - self._last_heartbeat_at < CI_PROGRESS_HEARTBEAT_INTERVAL_SECONDS:
            return
        self._last_heartbeat_at = now
        backend_summaries = []
        for backend_name, backend_results in results_by_backend.items():
            pass_count, fail_count, flaky_count, running_count = _count_statuses_with_running(
                backend_results,
                started_by_backend[backend_name],
            )
            backend_summaries.append(
                f"{backend_name}:pass={pass_count},fail={fail_count},"
                f"flaky={flaky_count},running={running_count}"
            )
        _print_ci_event(
            f"[heartbeat] completed={self._completed_reps}/{self._total_reps} "
            f"backends={' | '.join(backend_summaries)}"
        )


class SilentProgressOutput(ProgressOutput):
    pass


def create_progress_output(
    backend_configs: list[Any],
    total_reps: int,
    selected_count: int,
    progress_mode: str,
) -> ProgressOutput:
    if progress_mode == PROGRESS_CI:
        backends = ",".join(config.name for config in backend_configs)
        _print_ci_event(f"[plan] progress=ci backends={backends} total_reps={total_reps}")
        return CiProgressOutput(total_reps)
    if progress_mode == PROGRESS_NONE:
        return SilentProgressOutput()
    return TqdmProgressOutput(_create_progress_bars(backend_configs, total_reps, selected_count))


def close_progress_output(progress: ProgressOutput) -> None:
    progress.close()


def _create_progress_bars(
    backend_configs: list[Any],
    total_reps: int,
    selected_count: int,
) -> dict[str, tqdm]:
    show_overall = len(backend_configs) > 1
    bars: dict[str, tqdm] = {}
    start_position = 0
    if show_overall:
        bars[OVERALL_PROGRESS_KEY] = tqdm(
            total=total_reps,
            desc="Overall",
            unit="rep",
            position=0,
            bar_format=TQDM_BAR_FORMAT,
            dynamic_ncols=True,
        )
        start_position = 1

    for position, config in enumerate(backend_configs, start=start_position):
        bars[config.name] = tqdm(
            total=_selected_count_for_config(config, selected_count) * config.repeat,
            desc=_format_tqdm_backend_name(config.name),
            unit="rep" if config.repeat > 1 else "test",
            position=position,
            leave=True,
            bar_format=TQDM_BAR_FORMAT,
            dynamic_ncols=True,
        )
    return bars


def _selected_count_for_config(config: Any, default_selected_count: int) -> int:
    selected_indices = getattr(config, "selected_indices", None)
    if selected_indices is None:
        return default_selected_count
    return len(selected_indices)


def _format_tqdm_backend_name(backend_name: str) -> str:
    return TQDM_BACKEND_DISPLAY_NAMES.get(backend_name, backend_name)


def _format_tqdm_status_postfix(
    pass_count: int,
    fail_count: int,
    flaky_count: int,
    running_count: int,
) -> str:
    visible_fail_count = fail_count + flaky_count
    return f"ok={pass_count} fail={visible_fail_count} run={running_count}"


def _count_statuses_with_running(
    backend_results: list[list[tuple[int, str] | None] | None],
    backend_started: list[list[bool] | None],
) -> tuple[int, int, int, int]:
    pass_count = fail_count = flaky_count = running_count = 0
    for runs, started in zip(backend_results, backend_started):
        if _has_running_rep(runs, started):
            running_count += 1
        if runs is None:
            continue
        if any(r is None for r in runs):
            continue
        _, _, tag = summarize_runs(runs)
        if tag == TAG_OK:
            pass_count += 1
        elif tag == "flaky":
            flaky_count += 1
        else:
            fail_count += 1
    return pass_count, fail_count, flaky_count, running_count


def _has_running_rep(runs: list[tuple[int, str] | None] | None, started: list[bool] | None) -> bool:
    if started is None:
        return False
    if runs is None:
        return any(started)
    return any(has_started and result is None for has_started, result in zip(started, runs))
