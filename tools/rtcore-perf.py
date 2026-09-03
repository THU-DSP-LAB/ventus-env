#!/usr/bin/env python3
"""Summarize cache-enabled Verilator RTcore performance JSON.

Background: Verilator emits one RTcore PMU snapshot per simulation run.  Raw
per-SM counters are useful for debugging but awkward to compare across RT
workloads and architecture configurations.

Approach: validate the stable schema, sum the SM-local RTcore boundary
counters, and derive throughput, active-lane utilization, and RT-LSU pressure.

Usage:
  python3 tools/rtcore-perf.py summary artifacts/perf.json
  python3 tools/rtcore-perf.py summary artifacts/perf.json --json

Maintenance: this intentionally consumes only schema_version=1.  Add a new
reader rather than silently interpreting a future incompatible schema.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


COUNTERS = (
    "traverse_issued",
    "traverse_active_lanes",
    "completion_count",
    "completion_lanes",
    "release_issued",
    "active_session_cycles",
    "command_backpressure_cycles",
    "completion_backpressure_cycles",
    "release_backpressure_cycles",
    "lsu_requests",
    "lsu_responses",
    "lsu_backpressure_cycles",
)


def load_snapshot(path: Path) -> dict[str, Any]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"cannot read performance JSON {path}: {error}") from error
    if not isinstance(document, dict) or document.get("schema_version") != 1:
        raise ValueError("expected RTcore performance schema_version=1")
    if document.get("simulator") != "verilator-rtl":
        raise ValueError("expected a cache-enabled Verilator RTL performance snapshot")
    if not isinstance(document.get("sm"), list):
        raise ValueError("snapshot has no SM counter array")
    return document


def summarize(document: dict[str, Any]) -> dict[str, Any]:
    total = dict.fromkeys(COUNTERS, 0)
    for sm in document["sm"]:
        rtcore = sm.get("rtcore") if isinstance(sm, dict) else None
        if not isinstance(rtcore, dict):
            raise ValueError("every SM entry must contain an rtcore counter object")
        for name in COUNTERS:
            value = rtcore.get(name)
            if not isinstance(value, int) or value < 0:
                raise ValueError(f"invalid RTcore counter {name!r}")
            total[name] += value

    sessions = total["active_session_cycles"]
    traverses = total["traverse_issued"]
    return {
        "schema_version": 1,
        "simulator": document["simulator"],
        "clock_cycles": document.get("clock_cycles"),
        "external_memory_read_latency_cycles": document.get(
            "external_memory_read_latency_cycles"
        ),
        "sm_count": len(document["sm"]),
        "counters": total,
        "derived": {
            "traverses_per_active_session_cycle": traverses / sessions if sessions else 0.0,
            "active_lanes_per_traverse": total["traverse_active_lanes"] / traverses
            if traverses
            else 0.0,
            "completion_lanes_per_completion": total["completion_lanes"]
            / total["completion_count"]
            if total["completion_count"]
            else 0.0,
            "rt_lsu_requests_per_traverse": total["lsu_requests"] / traverses
            if traverses
            else 0.0,
            "rt_lsu_backpressure_fraction": total["lsu_backpressure_cycles"] / sessions
            if sessions
            else 0.0,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="command", required=True)
    summary = subcommands.add_parser("summary", help="validate and summarize one PMU JSON file")
    summary.add_argument("snapshot", type=Path)
    summary.add_argument("--json", action="store_true", help="write machine-readable JSON")
    args = parser.parse_args()

    try:
        report = summarize(load_snapshot(args.snapshot))
    except ValueError as error:
        parser.error(str(error))
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        derived = report["derived"]
        print(f"clock cycles: {report['clock_cycles']}")
        print(f"SM count: {report['sm_count']}")
        print(f"traverses / active-session cycle: {derived['traverses_per_active_session_cycle']:.6f}")
        print(f"active lanes / traverse: {derived['active_lanes_per_traverse']:.3f}")
        print(f"RT-LSU requests / traverse: {derived['rt_lsu_requests_per_traverse']:.3f}")
        print(f"RT-LSU backpressure fraction: {derived['rt_lsu_backpressure_fraction']:.6f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
