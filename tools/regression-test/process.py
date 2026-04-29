import os
import signal
import subprocess
from pathlib import Path
from typing import IO


PROCESS_GROUP_TERM_GRACE_SECONDS = 5


def run_command(
    cmd: list[str],
    cwd: Path,
    env: dict[str, str],
    log_file: IO[str],
    timeout: float,
    active_pids,
) -> int:
    process = subprocess.Popen(
        cmd,
        stdout=log_file,
        stderr=log_file,
        cwd=cwd,
        env=env,
        start_new_session=True,
    )
    active_pids[process.pid] = True
    try:
        return process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        terminate_process(process)
        raise
    finally:
        active_pids.pop(process.pid, None)


def terminate_process(process: subprocess.Popen) -> None:
    terminate_process_group(process.pid)
    try:
        process.wait(timeout=PROCESS_GROUP_TERM_GRACE_SECONDS)
        return
    except subprocess.TimeoutExpired:
        pass

    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    process.wait()


def terminate_process_group(pid: int) -> None:
    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
