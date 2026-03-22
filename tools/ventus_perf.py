#!/usr/bin/env python3
#
# Background: provide the Phase 1 wrapper/report entrypoint for Ventus PTX perf attribution.
# Flow: parse CLI args, dispatch into the package implementation, and keep `tools/` limited
# to a single user-facing script.
# Usage: python3 tools/ventus_perf.py run -- <cmd>
#        python3 tools/ventus_perf.py report <experiment-dir|pass-dir>
# Maintenance: keep related implementation under `tools/ventus_perf/`; do not add sibling
# helper modules at the `tools/` root.

from __future__ import annotations

import sys

from ventus_perf.cli import main as cli_main


def main(argv: list[str] | None = None) -> int:
    return cli_main(sys.argv[1:] if argv is None else argv)


if __name__ == "__main__":
    raise SystemExit(main())
