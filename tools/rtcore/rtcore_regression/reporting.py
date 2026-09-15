"""Bounded failure diagnostics and provenance for renderer invocations."""
from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def fingerprint(path: Path) -> dict:
    resolved = path.resolve()
    result = {"path": str(path), "resolved_path": str(resolved)}
    if resolved.is_file():
        digest = hashlib.sha256()
        with resolved.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        result["sha256"] = digest.hexdigest()
    return result


def source_state(root: Path) -> dict:
    result = {}
    for name in ("gpgpu", "driver", "mesa", "llvm", "spike"):
        repo = root / name
        revision = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                                  capture_output=True, text=True)
        diff = subprocess.run(["git", "-C", str(repo), "diff", "HEAD", "--"],
                              capture_output=True)
        status = subprocess.run(["git", "-C", str(repo), "status", "--porcelain"],
                                capture_output=True, text=True)
        untracked = subprocess.run(
            ["git", "-C", str(repo), "ls-files", "--others", "--exclude-standard", "--",
             "ventus/src", "sim-verilator", "driver", "src", "riscv"], capture_output=True, text=True)
        result[name] = {"revision": revision.stdout.strip(),
                        "dirty": bool(status.stdout.strip()),
                        "diff_sha256": hashlib.sha256(diff.stdout).hexdigest(),
                        "status": status.stdout.splitlines(),
                        "untracked_sources": [fingerprint(repo / path) for path in
                                              untracked.stdout.splitlines()
                                              if Path(path).suffix in (".scala", ".c", ".cc", ".cpp", ".h", ".hpp")]}
    return result


def log_excerpt(log: Path, limit: int = 8192) -> str:
    if not log.exists():
        return ""
    with log.open("rb") as stream:
        stream.seek(max(0, log.stat().st_size - limit))
        tail = stream.read(limit).decode("utf-8", errors="replace").replace("\\n", "\n")
    lines = tail.splitlines()
    errors = [line for line in lines if any(word in line.lower() for word in
              ("assertion", "%error", "aborting", "terminate", "what():", "timeout",
               "watchdog", "execution failed", "abnormally"))]
    return "\n".join(errors or lines[-8:])[-4096:]


def render_status(directory: Path, exit_status: int | None, timed_out: bool = False,
                  error: str | None = None) -> dict:
    image = directory / "image.ppm"
    category = ("timeout" if timed_out else "process_failed" if exit_status != 0 else
                "missing_image" if not image.is_file() else "captured")
    excerpt = error or (log_excerpt(directory / "render.log")
                        if category != "captured" else "")
    launcher_log = directory / "xvfb.log"
    if exit_status != 0 and launcher_log.exists():
        excerpt = (excerpt + "\nXvfb: " + log_excerpt(launcher_log))[-4096:]
    result = {"renderer": directory.name, "exit_status": exit_status,
              "timed_out": timed_out, "status": category,
              "process_succeeded": exit_status == 0 and not timed_out,
              "image_was_captured": image.is_file(),
              "log": str(directory / "render.log"),
              "error_excerpt": excerpt,
              "launcher_log": str(launcher_log) if launcher_log.exists() else None,
              "generated_elf": [fingerprint(p) for p in sorted(directory.glob("elf/*.riscv"))]}
    write_json(directory / "render.json", result)
    return result
