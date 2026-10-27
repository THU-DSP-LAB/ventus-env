"""Validate and summarize the Verilator PMU artifact owned by one RT regression.

This is deliberately an internal module, rather than a separate user-facing
tool: PMU validation is part of deciding whether a Verilator image regression
is a valid run.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


COUNTERS = (
    "traverse_issued", "traverse_active_lanes", "completion_count",
    "completion_lanes", "release_issued", "active_session_cycles",
    "command_backpressure_cycles", "completion_backpressure_cycles",
    "release_backpressure_cycles", "local_sram_read_requests",
    "local_sram_write_requests", "local_sram_read_responses",
    "local_sram_backpressure_cycles", "ray_setup_outputs", "as_line_acquires",
    "as_line_ready", "as_acquire_backpressure_cycles", "as_cache_resident_hits",
    "as_cache_pending_merges", "as_cache_misses", "as_cache_refills",
    "as_cache_evictions", "box4_issued",
    "box4_results", "triangle_issued", "triangle_results",
    "triangle_resolve_issued", "triangle_resolve_results", "stack_pushes",
    "stack_pops",
)


def load_snapshot(path: Path) -> dict[str, Any]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"cannot read performance JSON {path}: {error}") from error
    if not isinstance(document, dict) or document.get("schema_version") != 2:
        raise ValueError("expected RTcore performance schema_version=2")
    if document.get("simulator") != "verilator-rtl":
        raise ValueError("expected a cache-enabled Verilator RTL performance snapshot")
    if not isinstance(document.get("sm"), list):
        raise ValueError("snapshot has no SM counter array")
    return document


def summarize(document: dict[str, Any]) -> dict[str, Any]:
    validate_extended(document)
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
        "schema_version": 2,
        "simulator": document["simulator"],
        "clock_cycles": document.get("clock_cycles"),
        "external_memory_read_latency_cycles": document.get(
            "external_memory_read_latency_cycles"),
        "sm_count": len(document["sm"]),
        "counters": total,
        # Preserve per-SM detail and exact histogram bins for plotting. Do not
        # turn absent instrumentation into zeros or sum ratios across SMs.
        "extended": document.get("extended"),
        "derived": {
            "traverses_per_active_session_cycle": traverses / sessions if sessions else 0.0,
            "active_lanes_per_traverse": total["traverse_active_lanes"] / traverses
            if traverses else 0.0,
            "completion_lanes_per_completion": total["completion_lanes"] /
            total["completion_count"] if total["completion_count"] else 0.0,
            "local_sram_transactions_per_traverse": (
                total["local_sram_read_requests"] + total["local_sram_write_requests"]
            ) / traverses if traverses else 0.0,
            "local_sram_backpressure_fraction": total["local_sram_backpressure_cycles"] /
            sessions if sessions else 0.0,
            "as_cache_average_waiter_uses": total["as_line_ready"] /
            total["as_cache_refills"] if total["as_cache_refills"] else 0.0,
            "box4_results_per_issue": total["box4_results"] / total["box4_issued"]
            if total["box4_issued"] else 0.0,
            "triangle_results_per_issue": total["triangle_results"] /
            total["triangle_issued"] if total["triangle_issued"] else 0.0,
            "triangle_resolve_results_per_issue": (
                total["triangle_resolve_results"] / total["triangle_resolve_issued"]
                if total["triangle_resolve_issued"] else 0.0
            ),
        },
    }


def validate_extended(document: dict[str, Any]) -> None:
    """Check transport and cross-check the host sampler against RTL counters.

    Old runtimes have no optional API; absence stays unavailable, not zero.
    """
    extended = document.get("extended")
    if extended is None:
        return
    if not isinstance(extended, dict) or extended.get("schema_version") != 1:
        raise ValueError("expected extended PMU schema_version=1")
    mode = extended.get("mode")
    if mode not in ("off", "basic", "detailed"):
        raise ValueError("invalid extended PMU mode")
    sms = extended.get("sm")
    if not isinstance(sms, list) or len(sms) != len(document["sm"]):
        raise ValueError("extended PMU SM count mismatch")
    for sm, original in zip(sms, document["sm"]):
        if not isinstance(sm, dict) or sm.get("id") != original.get("id"):
            raise ValueError("extended PMU SM identity mismatch")
        if mode != "detailed":
            continue
        detail = sm.get("rt_detailed")
        if not isinstance(detail, dict):
            raise ValueError("detailed mode has no RT observer snapshot")
        units = detail.get("units")
        if not isinstance(units, list):
            raise ValueError("detailed snapshot has no IU array")
        rt = original["rtcore"]
        for kind, prefix, expected_count in (("box4", "box4", 8), ("triangle", "triangle", 2),
                                              ("resolve", "triangle_resolve", 1)):
            group = [u for u in units if u.get("kind") == kind]
            if len(group) != expected_count:
                raise ValueError(f"detailed {kind} unit count mismatch")
            for event, counter in (("issues", "issued"), ("results", "results")):
                observed = sum(u["statistics"]["counters"].get(event, 0) for u in group)
                if observed != rt[f"{prefix}_{counter}"]:
                    raise ValueError(f"detailed {kind} {event} disagrees with RTL PMU")
        counts = detail["statistics"]["counters"]
        for error in ("unmatched_as_serves", "unmatched_injections", "invalid_lane_state",
                      "unexpected_live_entry_allocation", "unmatched_evictions"):
            if counts.get(error, 0):
                raise ValueError(f"detailed observer reported {error}")
        for unit in units:
            if unit["statistics"]["counters"].get("unmatched_results", 0):
                raise ValueError("detailed IU observer reported unmatched results")
        if detail.get("lsu", {}).get("counters", {}).get("unmatched_results", 0):
            raise ValueError("detailed LSU observer reported unmatched results")
        for host, rtl in (("as_acquires", "as_line_acquires"), ("as_serves", "as_line_ready"),
                          ("as_allocations", "as_cache_misses"), ("as_refills", "as_cache_refills")):
            if counts.get(host, 0) != rt[rtl]:
                raise ValueError(f"detailed {host} disagrees with RTL PMU")
        if detail.get("as_policy") == "retained_reverse_replace":
            if counts.get("as_evictions", 0) != rt["as_cache_evictions"]:
                raise ValueError("detailed as_evictions disagrees with RTL PMU")
