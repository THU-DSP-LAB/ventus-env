from __future__ import annotations

import json
import math
from pathlib import Path

from ventus_perf.model import load_events_for_pass, load_input_manifests


TOP_LEVEL_BUCKETS = (
    "host_overhead",
    "h2d",
    "kernel_prepare",
    "kernel_exec_wait",
    "d2h",
    "teardown",
    "uncategorized",
)

H2D_EVENTS = {"buffer_write", "buffer_copy", "buffer_fill", "map_mem", "vt_copy_to_dev"}
D2H_EVENTS = {"buffer_read", "vt_copy_from_dev", "unmap_mem"}
KERNEL_PREPARE_EVENTS = {
    "vt_start",
    "generate_ptx_via_sbt",
    "read_generated_ptx",
    "cuModuleLoadDataEx",
    "cuModuleGetFunction",
    "cuLaunchKernel",
}
KERNEL_WAIT_EVENTS = {"kernel_wait", "vt_ready_wait", "cuCtxSynchronize"}
NS_PER_US = 1_000
NS_PER_MS = 1_000_000
NS_PER_S = 1_000_000_000


def _interval_duration(intervals: list[tuple[int, int]]) -> int:
    if not intervals:
        return 0
    merged = []
    for start, end in sorted(intervals):
        if not merged or start > merged[-1][1]:
            merged.append([start, end])
        else:
            merged[-1][1] = max(merged[-1][1], end)
    return sum(end - start for start, end in merged)


def _extract_interval(event: dict) -> tuple[int, int]:
    return int(event["ts_start_ns"]), int(event["ts_end_ns"])


def _bucket_for_event(event_type: str) -> str | None:
    if event_type in H2D_EVENTS:
        return "h2d"
    if event_type in D2H_EVENTS:
        return "d2h"
    if event_type in KERNEL_PREPARE_EVENTS:
        return "kernel_prepare"
    if event_type in KERNEL_WAIT_EVENTS:
        return "kernel_exec_wait"
    return None


def _pass_duration_ns(pass_manifest: dict, pass_events: list[dict]) -> int:
    if "duration_ns" in pass_manifest:
        return int(pass_manifest["duration_ns"])
    if "end_mono_ns" in pass_manifest and "start_mono_ns" in pass_manifest:
        return int(pass_manifest["end_mono_ns"]) - int(pass_manifest["start_mono_ns"])
    if not pass_events:
        return 0
    starts = [int(event["ts_start_ns"]) for event in pass_events]
    ends = [int(event["ts_end_ns"]) for event in pass_events]
    return max(ends) - min(starts)


def _group_kernel_windows(pass_events: list[dict]) -> dict[int, dict[str, object]]:
    groups: dict[int, dict[str, object]] = {}
    for event in pass_events:
        launch_seq = int(event.get("launch_seq") or 0)
        if launch_seq <= 0:
            continue
        group = groups.setdefault(
            launch_seq,
            {
                "launch_seq": launch_seq,
                "kernel_name": event.get("kernel_name"),
                "events": [],
                "prepare": [],
                "wait": [],
            },
        )
        group["events"].append(event)
        bucket = _bucket_for_event(str(event["event_type"]))
        if bucket == "kernel_prepare":
            group["prepare"].append(_extract_interval(event))
        elif bucket == "kernel_exec_wait":
            group["wait"].append(_extract_interval(event))
        if not group.get("kernel_name") and event.get("kernel_name"):
            group["kernel_name"] = event["kernel_name"]
    return groups


def _detect_concurrency(pass_events: list[dict]) -> tuple[bool, int]:
    groups = _group_kernel_windows(pass_events)
    windows = []
    for launch_seq, group in groups.items():
        prepare = group["prepare"]
        wait = group["wait"]
        if prepare:
            starts = [start for start, _ in prepare]
            ends = [end for _, end in prepare]
            windows.append((launch_seq, min(starts), max(ends)))
        if wait:
            starts = [start for start, _ in wait]
            ends = [end for _, end in wait]
            windows.append((launch_seq, min(starts), max(ends)))
    overlap_ns = 0
    for index, (lhs_seq, lhs_start, lhs_end) in enumerate(windows):
        for rhs_seq, rhs_start, rhs_end in windows[index + 1 :]:
            if lhs_seq == rhs_seq:
                continue
            overlap_start = max(lhs_start, rhs_start)
            overlap_end = min(lhs_end, rhs_end)
            if overlap_end > overlap_start:
                overlap_ns += overlap_end - overlap_start
    return overlap_ns > 0, overlap_ns


def _summarize_sub_buckets(pass_events: list[dict]) -> dict[str, dict[str, int]]:
    sub_buckets = {"kernel_prepare": {}, "kernel_exec_wait": {}}
    for event in pass_events:
        event_type = str(event["event_type"])
        bucket = _bucket_for_event(event_type)
        if bucket not in sub_buckets:
            continue
        duration = int(event["ts_end_ns"]) - int(event["ts_start_ns"])
        sub_buckets[bucket][event_type] = sub_buckets[bucket].get(event_type, 0) + duration
    return sub_buckets


def _build_pass_summary(pass_manifest: dict, pass_events: list[dict]) -> dict:
    buckets = {bucket: 0 for bucket in TOP_LEVEL_BUCKETS}
    intervals = {bucket: [] for bucket in TOP_LEVEL_BUCKETS}
    for event in pass_events:
        bucket = _bucket_for_event(str(event["event_type"]))
        if bucket is None:
            continue
        intervals[bucket].append(_extract_interval(event))
    for bucket in ("h2d", "kernel_prepare", "kernel_exec_wait", "d2h"):
        buckets[bucket] = _interval_duration(intervals[bucket])
    wall_time_ns = _pass_duration_ns(pass_manifest, pass_events)
    concurrency_detected, overlap_ns = _detect_concurrency(pass_events)
    known_total = sum(buckets[bucket] for bucket in TOP_LEVEL_BUCKETS if bucket != "uncategorized")
    uncategorized = max(wall_time_ns - known_total, 0)
    if concurrency_detected:
        uncategorized = max(uncategorized, overlap_ns or 1)
    buckets["uncategorized"] = uncategorized
    return {
        "wall_time_ns": wall_time_ns,
        "buckets": buckets,
        "sub_buckets": _summarize_sub_buckets(pass_events),
        "concurrency_detected": concurrency_detected,
    }


def _render_wall_time_stats(wall_times: list[int]) -> dict[str, int]:
    if not wall_times:
        return {"mean_ns": 0, "min_ns": 0, "max_ns": 0, "stddev_ns": 0}
    mean = sum(wall_times) / len(wall_times)
    variance = sum((value - mean) ** 2 for value in wall_times) / len(wall_times)
    return {
        "mean_ns": int(round(mean)),
        "min_ns": min(wall_times),
        "max_ns": max(wall_times),
        "stddev_ns": int(round(math.sqrt(variance))),
    }


def _render_kernel_view(events_by_pass: dict[str, list[dict]]) -> list[dict]:
    kernels = []
    for pass_id, pass_events in events_by_pass.items():
        for group in _group_kernel_windows(pass_events).values():
            kernels.append(
                {
                    "pass_id": pass_id,
                    "launch_seq": group["launch_seq"],
                    "kernel_name": group.get("kernel_name"),
                    "event_count": len(group["events"]),
                }
            )
    kernels.sort(key=lambda item: (item["pass_id"], item["launch_seq"]))
    return kernels


def _format_duration_ns(duration_ns: int) -> str:
    if duration_ns >= NS_PER_S:
        return f"{duration_ns / NS_PER_S:.3f} s"
    if duration_ns >= NS_PER_MS:
        return f"{duration_ns / NS_PER_MS:.3f} ms"
    if duration_ns >= NS_PER_US:
        return f"{duration_ns / NS_PER_US:.3f} us"
    return f"{duration_ns} ns"


def load_input_report(input_dir: Path) -> dict:
    experiment, passes = load_input_manifests(Path(input_dir))
    events_by_pass = {pass_manifest["pass_id"]: load_events_for_pass(pass_manifest) for pass_manifest in passes}
    measured_completed = [
        pass_manifest
        for pass_manifest in passes
        if pass_manifest.get("pass_type") == "measure" and pass_manifest.get("state") == "completed"
    ]
    pass_summaries = {
        pass_manifest["pass_id"]: _build_pass_summary(
            pass_manifest, events_by_pass[pass_manifest["pass_id"]]
        )
        for pass_manifest in measured_completed
    }
    wall_times = [summary["wall_time_ns"] for summary in pass_summaries.values()]
    aggregate_buckets = {bucket: 0 for bucket in TOP_LEVEL_BUCKETS}
    aggregate_sub_buckets = {"kernel_prepare": {}, "kernel_exec_wait": {}}
    for summary in pass_summaries.values():
        for bucket, value in summary["buckets"].items():
            aggregate_buckets[bucket] += value
        for bucket_name, bucket_values in summary["sub_buckets"].items():
            for event_type, value in bucket_values.items():
                aggregate_sub_buckets[bucket_name][event_type] = (
                    aggregate_sub_buckets[bucket_name].get(event_type, 0) + value
                )
    measured_count = len(measured_completed)
    if measured_count:
        for bucket in aggregate_buckets:
            aggregate_buckets[bucket] = int(round(aggregate_buckets[bucket] / measured_count))
        for bucket_name, bucket_values in aggregate_sub_buckets.items():
            for event_type, value in list(bucket_values.items()):
                bucket_values[event_type] = int(round(value / measured_count))
    wall_time_stats = _render_wall_time_stats(wall_times)
    wall_time_ns = wall_time_stats["mean_ns"]
    concurrency_detected = any(summary["concurrency_detected"] for summary in pass_summaries.values())
    return {
        "experiment_id": experiment.get("experiment_id"),
        "state": experiment.get("state", "completed"),
        "best_effort": bool(experiment.get("best_effort", False)),
        "measured_pass_count": measured_count,
        "concurrency_detected": concurrency_detected,
        "passes": passes,
        "timeline": [event for events in events_by_pass.values() for event in events],
        "kernels": _render_kernel_view(events_by_pass),
        "summary": {
            "wall_time_ns": wall_time_ns,
            "top_level_total_ns": sum(aggregate_buckets.values()),
            "buckets": aggregate_buckets,
            "sub_buckets": aggregate_sub_buckets,
            "wall_time_stats": wall_time_stats,
        },
    }


def render_summary_text(report: dict) -> str:
    summary = report["summary"]
    lines = [
        "Baseline Attribution",
        f"experiment_id: {report.get('experiment_id')}",
        f"state: {report.get('state')}",
        f"measured_pass_count: {report.get('measured_pass_count')}",
        f"concurrency_detected: {str(report.get('concurrency_detected')).lower()}",
        f"wall_time: {_format_duration_ns(summary['wall_time_ns'])}",
    ]
    for bucket in TOP_LEVEL_BUCKETS:
        lines.append(f"{bucket}: {_format_duration_ns(summary['buckets'][bucket])}")
    return "\n".join(lines)


def write_report_outputs(input_dir: Path, report: dict) -> Path:
    output_dir = Path(input_dir) / "reports"
    output_dir.mkdir(parents=True, exist_ok=True)
    summary_text = render_summary_text(report)
    (output_dir / "summary.txt").write_text(summary_text + "\n", encoding="utf-8")
    (output_dir / "summary.json").write_text(
        json.dumps(report["summary"], indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (output_dir / "timeline.json").write_text(
        json.dumps(report["timeline"], indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (output_dir / "kernels.json").write_text(
        json.dumps(report["kernels"], indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return output_dir
