import os
import resource
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import IO


PROCESS_GROUP_TERM_GRACE_SECONDS = 5
PROCESS_WAIT_POLL_SECONDS = 0.1
NSEC_PER_SEC = 1_000_000_000


@dataclass(frozen=True)
class ResourceUsage:
    user_time_sec: float
    system_time_sec: float
    peak_rss_kb: int
    minor_page_faults: int
    major_page_faults: int
    voluntary_context_switches: int
    involuntary_context_switches: int
    filesystem_input_ops: int
    filesystem_output_ops: int

    @classmethod
    def zero(cls) -> "ResourceUsage":
        return cls(
            user_time_sec=0,
            system_time_sec=0,
            peak_rss_kb=0,
            minor_page_faults=0,
            major_page_faults=0,
            voluntary_context_switches=0,
            involuntary_context_switches=0,
            filesystem_input_ops=0,
            filesystem_output_ops=0,
        )

    def combine(self, other: "ResourceUsage") -> "ResourceUsage":
        return ResourceUsage(
            user_time_sec=self.user_time_sec + other.user_time_sec,
            system_time_sec=self.system_time_sec + other.system_time_sec,
            peak_rss_kb=max(self.peak_rss_kb, other.peak_rss_kb),
            minor_page_faults=self.minor_page_faults + other.minor_page_faults,
            major_page_faults=self.major_page_faults + other.major_page_faults,
            voluntary_context_switches=(
                self.voluntary_context_switches + other.voluntary_context_switches
            ),
            involuntary_context_switches=(
                self.involuntary_context_switches + other.involuntary_context_switches
            ),
            filesystem_input_ops=self.filesystem_input_ops + other.filesystem_input_ops,
            filesystem_output_ops=self.filesystem_output_ops + other.filesystem_output_ops,
        )


@dataclass(frozen=True)
class CommandResult:
    return_code: int
    timed_out: bool
    wall_time_sec: float
    resources: ResourceUsage


@dataclass(frozen=True)
class WaitResult:
    return_code: int
    resources: ResourceUsage


class ProcessWaitTimeout(RuntimeError):
    pass


def run_command(
    cmd: list[str],
    cwd: Path,
    env: dict[str, str],
    log_file: IO[str],
    timeout: float,
    active_pids,
) -> CommandResult:
    process = subprocess.Popen(
        cmd,
        stdout=log_file,
        stderr=log_file,
        cwd=cwd,
        env=env,
        start_new_session=True,
    )
    active_pids[process.pid] = True
    start_ns = time.monotonic_ns()
    timed_out = False
    try:
        wait_result = wait_for_process(process.pid, timeout)
    except ProcessWaitTimeout:
        timed_out = True
        wait_result = terminate_process(process.pid)
    finally:
        active_pids.pop(process.pid, None)
    process.returncode = wait_result.return_code
    wall_time_sec = (time.monotonic_ns() - start_ns) / NSEC_PER_SEC
    return CommandResult(
        return_code=wait_result.return_code,
        timed_out=timed_out,
        wall_time_sec=wall_time_sec,
        resources=wait_result.resources,
    )


def wait_for_process(pid: int, timeout: float) -> WaitResult:
    deadline = time.monotonic() + timeout
    while True:
        wait_result = wait_for_process_once(pid)
        if wait_result is not None:
            return wait_result

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ProcessWaitTimeout
        time.sleep(min(PROCESS_WAIT_POLL_SECONDS, remaining))


def wait_for_process_once(pid: int) -> WaitResult | None:
    waited_pid, status, usage = os.wait4(pid, os.WNOHANG)
    if waited_pid == 0:
        return None
    return WaitResult(os.waitstatus_to_exitcode(status), resource_usage_from_wait4(usage))


def terminate_process(pid: int) -> WaitResult:
    terminate_process_group(pid)
    try:
        return wait_for_process(pid, PROCESS_GROUP_TERM_GRACE_SECONDS)
    except ProcessWaitTimeout:
        pass

    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    return wait_for_process_without_timeout(pid)


def wait_for_process_without_timeout(pid: int) -> WaitResult:
    waited_pid, status, usage = os.wait4(pid, 0)
    if waited_pid != pid:
        raise RuntimeError(f"wait4 returned pid {waited_pid}, expected {pid}")
    return WaitResult(os.waitstatus_to_exitcode(status), resource_usage_from_wait4(usage))


def terminate_process_group(pid: int) -> None:
    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        return


def resource_usage_from_wait4(usage) -> ResourceUsage:
    return ResourceUsage(
        user_time_sec=usage.ru_utime,
        system_time_sec=usage.ru_stime,
        peak_rss_kb=usage.ru_maxrss,
        minor_page_faults=usage.ru_minflt,
        major_page_faults=usage.ru_majflt,
        voluntary_context_switches=usage.ru_nvcsw,
        involuntary_context_switches=usage.ru_nivcsw,
        filesystem_input_ops=usage.ru_inblock,
        filesystem_output_ops=usage.ru_oublock,
    )
