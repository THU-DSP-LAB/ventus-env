"""Repo-root import shim for the Ventus perf tool package."""

from __future__ import annotations

from pathlib import Path

_PACKAGE_DIR = Path(__file__).resolve().parent.parent / "tools" / "ventus_perf"
__path__ = [str(_PACKAGE_DIR)]
