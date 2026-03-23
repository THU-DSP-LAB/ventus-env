from __future__ import annotations

import json
import math
from pathlib import Path

from ventus_perf.model import load_events_for_pass, load_input_manifests


TOP_LEVEL_BUCKETS = (
    "host_overhead",
    "memcpy_h2d",
    "kernel_launch",
    "kernel_wait",
    "memcpy_d2h",
    "teardown",
    "uncategorized",
)

MEMCPY_H2D_EVENTS = {
    "buffer_write",
    "buffer_copy",
    "buffer_fill",
    "map_mem",
    "vt_copy_to_dev",
    "vmemcpy_h2d",
    "pmemcpy_h2d",
    "copy_to_dev",
}
MEMCPY_D2H_EVENTS = {
    "buffer_read",
    "vt_copy_from_dev",
    "unmap_mem",
    "vmemcpy_d2h",
    "pmemcpy_d2h",
    "copy_from_dev",
}
KERNEL_LAUNCH_EVENTS = {
    "kernel_submit",
    "kernel_arg_pack",
    "kernel_arg_upload",
    "kernel_elf_upload",
    "kernel_metadata_upload",
    "vt_start",
    "add_kernel",
    "generate_ptx_via_sbt",
    "read_generated_ptx",
    "cuModuleLoadDataEx",
    "cuModuleGetFunction",
    "cuLaunchKernel",
}
KERNEL_WAIT_EVENTS = {"kernel_wait", "vt_ready_wait", "cuCtxSynchronize", "ready_wait_loop"}
DERIVED_BUCKETS = ("memcpy_h2d", "kernel_launch", "kernel_wait", "memcpy_d2h")
SUB_BUCKET_NAMES = ("kernel_launch", "kernel_wait")
PROFILER_PASS_TYPES = {"nsys", "ncu"}
AUXILIARY_FACT_EVENT_TYPES = {"sim_time_ns", "sim_time", "step_count", "flush_tail_steps"}
NS_PER_US = 1_000
NS_PER_MS = 1_000_000
NS_PER_S = 1_000_000_000


def _render_perfetto_metadata_event(pid: int, tid: int, name: str, value: str) -> dict:
    return {
        "ph": "M",
        "pid": pid,
        "tid": tid,
        "name": name,
        "args": {"name": value},
    }


def _render_perfetto_complete_event(event: dict, pid: int, tid: int) -> dict:
    duration_ns = int(event["ts_end_ns"]) - int(event["ts_start_ns"])
    return {
        "ph": "X",
        "name": str(event["event_type"]),
        "cat": str(event.get("stream") or "unknown"),
        "ts": int(event["ts_start_ns"]) / NS_PER_US,
        "dur": duration_ns / NS_PER_US,
        "pid": pid,
        "tid": tid,
        "args": {
            "event_id": event.get("event_id"),
            "parent_event_id": event.get("parent_event_id"),
            "pass_id": event.get("pass_id"),
            "pass_type": event.get("pass_type"),
            "pass_state": event.get("pass_state"),
            "stream": event.get("stream"),
            "scope_id": event.get("scope_id"),
            "queue_id": event.get("queue_id"),
            "launch_seq": event.get("launch_seq"),
            "kernel_occurrence": event.get("kernel_occurrence"),
            "kernel_signature_hash": event.get("kernel_signature_hash"),
            "kernel_name": event.get("kernel_name"),
            "source_pid": event.get("pid"),
            "source_tid": event.get("tid"),
            "attrs": event.get("attrs", {}),
        },
    }


def _render_perfetto_trace(passes: list[dict], events_by_pass: dict[str, list[dict]]) -> dict:
    trace_events = []
    pass_pid_map = {
        pass_manifest["pass_id"]: index
        for index, pass_manifest in enumerate(passes, start=1)
    }
    for pass_manifest in passes:
        pass_id = pass_manifest["pass_id"]
        pid = pass_pid_map[pass_id]
        pass_events = events_by_pass.get(pass_id, [])
        streams = sorted({str(event.get("stream") or "unknown") for event in pass_events})
        stream_tid_map = {stream: index for index, stream in enumerate(streams, start=1)}
        trace_events.append(_render_perfetto_metadata_event(pid, 0, "process_name", pass_id))
        for stream, tid in stream_tid_map.items():
            track_name = f"{pass_id}:{stream}"
            trace_events.append(_render_perfetto_metadata_event(pid, tid, "thread_name", track_name))
        sorted_events = sorted(
            pass_events,
            key=lambda event: (
                int(event["ts_start_ns"]),
                int(event["ts_end_ns"]),
                str(event["event_type"]),
                str(event.get("event_id") or ""),
            ),
        )
        for event in sorted_events:
            stream = str(event.get("stream") or "unknown")
            tid = stream_tid_map[stream]
            trace_events.append(_render_perfetto_complete_event(event, pid, tid))
    return {
        "traceEvents": trace_events,
        "displayTimeUnit": "ns",
    }


def _merge_intervals(intervals: list[tuple[int, int]]) -> list[tuple[int, int]]:
    merged = []
    for start, end in sorted(intervals):
        if not merged or start > merged[-1][1]:
            merged.append([start, end])
        else:
            merged[-1][1] = max(merged[-1][1], end)
    return [(start, end) for start, end in merged]


def _interval_duration(intervals: list[tuple[int, int]]) -> int:
    if not intervals:
        return 0
    merged = _merge_intervals(intervals)
    return sum(end - start for start, end in merged)


def _extract_interval(event: dict) -> tuple[int, int]:
    return int(event["ts_start_ns"]), int(event["ts_end_ns"])


def _bucket_for_event(event_type: str) -> str | None:
    if event_type in MEMCPY_H2D_EVENTS:
        return "memcpy_h2d"
    if event_type in MEMCPY_D2H_EVENTS:
        return "memcpy_d2h"
    if event_type in KERNEL_LAUNCH_EVENTS:
        return "kernel_launch"
    if event_type in KERNEL_WAIT_EVENTS:
        return "kernel_wait"
    return None


def _auxiliary_value_for_event(event: dict) -> int:
    attrs = event.get("attrs", {})
    value = attrs.get("value")
    if isinstance(value, (int, float)):
        return int(value)
    raise ValueError(f"missing numeric attrs.value for auxiliary fact event: {event['event_type']}")


def _summarize_auxiliary_facts(pass_events: list[dict]) -> dict[str, dict[str, int]]:
    values_by_type: dict[str, list[int]] = {}
    for event in pass_events:
        event_type = str(event["event_type"])
        if event_type not in AUXILIARY_FACT_EVENT_TYPES:
            continue
        values_by_type.setdefault(event_type, []).append(_auxiliary_value_for_event(event))
    summary = {}
    for event_type, values in values_by_type.items():
        summary[event_type] = {
            "count": len(values),
            "mean_value": int(round(sum(values) / len(values))),
            "min_value": min(values),
            "max_value": max(values),
        }
    return summary


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
                "launch_candidates": [],
                "wait_candidates": [],
            },
        )
        group["events"].append(event)
        bucket = _bucket_for_event(str(event["event_type"]))
        if bucket == "kernel_launch":
            group["launch_candidates"].append(_extract_interval(event))
        elif bucket == "kernel_wait":
            group["wait_candidates"].append(_extract_interval(event))
        if not group.get("kernel_name") and event.get("kernel_name"):
            group["kernel_name"] = event["kernel_name"]
    for group in groups.values():
        wait_candidates = list(group["wait_candidates"])
        wait_intervals = _merge_intervals(wait_candidates)
        wait_start_ns = min((start for start, _ in wait_intervals), default=None)
        launch_intervals = []
        for start, end in group["launch_candidates"]:
            clipped_end = min(end, wait_start_ns) if wait_start_ns is not None else end
            if clipped_end > start:
                launch_intervals.append((start, clipped_end))
        group["launch_intervals"] = _merge_intervals(launch_intervals)
        group["wait_intervals"] = wait_intervals
    return groups


def _detect_concurrency(kernel_groups: dict[int, dict[str, object]]) -> tuple[bool, int]:
    markers: list[tuple[int, int, int]] = []
    for launch_seq, group in kernel_groups.items():
        for start, end in group["launch_intervals"]:
            markers.append((start, 1, launch_seq))
            markers.append((end, -1, launch_seq))
        for start, end in group["wait_intervals"]:
            markers.append((start, 1, launch_seq))
            markers.append((end, -1, launch_seq))
    if not markers:
        return False, 0
    markers.sort(key=lambda item: (item[0], item[1]))
    active_counts: dict[int, int] = {}
    active_sequences = 0
    overlap_ns = 0
    previous_ts = markers[0][0]
    for ts, delta, launch_seq in markers:
        if active_sequences > 1 and ts > previous_ts:
            overlap_ns += ts - previous_ts
        count = active_counts.get(launch_seq, 0)
        if delta < 0:
            if count == 1:
                active_sequences -= 1
                active_counts.pop(launch_seq, None)
            else:
                active_counts[launch_seq] = count - 1
        else:
            if count == 0:
                active_sequences += 1
            active_counts[launch_seq] = count + 1
        previous_ts = ts
    return overlap_ns > 0, overlap_ns


def _summarize_sub_buckets(pass_events: list[dict]) -> dict[str, dict[str, int]]:
    sub_buckets = {bucket_name: {} for bucket_name in SUB_BUCKET_NAMES}
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
    kernel_groups = _group_kernel_windows(pass_events)
    for event in pass_events:
        bucket = _bucket_for_event(str(event["event_type"]))
        if bucket not in {"memcpy_h2d", "memcpy_d2h"}:
            continue
        intervals[bucket].append(_extract_interval(event))
    for group in kernel_groups.values():
        intervals["kernel_launch"].extend(group["launch_intervals"])
        intervals["kernel_wait"].extend(group["wait_intervals"])
    for bucket in DERIVED_BUCKETS:
        buckets[bucket] = _interval_duration(intervals[bucket])
    wall_time_ns = _pass_duration_ns(pass_manifest, pass_events)
    concurrency_detected, overlap_ns = _detect_concurrency(kernel_groups)
    known_total = sum(buckets[bucket] for bucket in TOP_LEVEL_BUCKETS if bucket != "uncategorized")
    uncategorized = max(wall_time_ns - known_total, 0)
    if concurrency_detected:
        uncategorized = max(uncategorized, overlap_ns or 1)
    buckets["uncategorized"] = uncategorized
    return {
        "wall_time_ns": wall_time_ns,
        "buckets": buckets,
        "sub_buckets": _summarize_sub_buckets(pass_events),
        "auxiliary_facts": _summarize_auxiliary_facts(pass_events),
        "concurrency_detected": concurrency_detected,
    }


def _build_profiler_view(profiler_passes: list[dict]) -> dict:
    rendered_passes = []
    for pass_manifest in profiler_passes:
        artifacts = []
        for artifact in pass_manifest.get("artifacts", []):
            relative_path = artifact["path"]
            artifact_path = Path(pass_manifest["path"]) / relative_path
            artifacts.append(
                {
                    "kind": artifact.get("kind"),
                    "path": relative_path,
                    "exists": artifact_path.exists(),
                }
            )
        rendered_passes.append(
            {
                "pass_id": pass_manifest["pass_id"],
                "pass_type": pass_manifest["pass_type"],
                "state": pass_manifest.get("state"),
                "profile_target": pass_manifest.get("profile_target"),
                "artifacts": artifacts,
            }
        )
    return {"passes": rendered_passes}


def _build_nsys_reference(profiler_passes: list[dict]) -> dict | None:
    nsys_pass = next(
        (pass_manifest for pass_manifest in profiler_passes if pass_manifest.get("pass_type") == "nsys"),
        None,
    )
    if nsys_pass is None:
        return None
    summary_artifact = next(
        (
            artifact
            for artifact in nsys_pass.get("artifacts", [])
            if artifact.get("kind") == "nsys_summary"
        ),
        None,
    )
    artifact_exists = False
    top_kernels: list[dict[str, int | str]] = []
    error = None
    if summary_artifact is not None:
        artifact_path = Path(nsys_pass["path"]) / summary_artifact["path"]
        artifact_exists = artifact_path.exists()
        if artifact_exists:
            try:
                payload = json.loads(artifact_path.read_text(encoding="utf-8"))
                top_kernels = [
                    {
                        "kernel_name": str(kernel["kernel_name"]),
                        "gpu_time_ns": int(kernel["gpu_time_ns"]),
                    }
                    for kernel in payload.get("top_kernels", [])
                    if "kernel_name" in kernel and "gpu_time_ns" in kernel
                ]
            except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
                error = str(exc)
        else:
            error = f"missing nsys summary artifact: {summary_artifact['path']}"
    recorder_errors = nsys_pass.get("recorder_errors") or []
    if error is None and recorder_errors:
        error = str(recorder_errors[-1])
    reference = {
        "pass_id": nsys_pass["pass_id"],
        "state": nsys_pass.get("state"),
        "artifact_exists": artifact_exists,
        "top_kernels": top_kernels,
    }
    if error is not None:
        reference["error"] = error
    return reference


def _build_profiler_reference(profiler_passes: list[dict]) -> dict:
    reference = {}
    nsys_reference = _build_nsys_reference(profiler_passes)
    if nsys_reference is not None:
        reference["nsys"] = nsys_reference
    return reference


def _collect_auxiliary_facts(events_by_pass: dict[str, list[dict]]) -> dict[str, dict[str, int]]:
    values_by_type: dict[str, list[int]] = {}
    for pass_events in events_by_pass.values():
        for event in pass_events:
            event_type = str(event["event_type"])
            if event_type not in AUXILIARY_FACT_EVENT_TYPES:
                continue
            values_by_type.setdefault(event_type, []).append(_auxiliary_value_for_event(event))
    summary = {}
    for event_type, values in values_by_type.items():
        summary[event_type] = {
            "count": len(values),
            "mean_value": int(round(sum(values) / len(values))),
            "min_value": min(values),
            "max_value": max(values),
        }
    return summary


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
    aggregate_sub_buckets = {bucket_name: {} for bucket_name in SUB_BUCKET_NAMES}
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
    if measured_completed:
        baseline_passes = measured_completed
    else:
        baseline_passes = [
            pass_manifest for pass_manifest in passes if pass_manifest.get("pass_type") == "measure"
        ]
    baseline_events_by_pass = {
        pass_manifest["pass_id"]: events_by_pass[pass_manifest["pass_id"]]
        for pass_manifest in baseline_passes
    }
    auxiliary_facts = _collect_auxiliary_facts(baseline_events_by_pass)
    wall_time_stats = _render_wall_time_stats(wall_times)
    wall_time_ns = wall_time_stats["mean_ns"]
    concurrency_detected = any(summary["concurrency_detected"] for summary in pass_summaries.values())
    report = {
        "experiment_id": experiment.get("experiment_id"),
        "state": experiment.get("state", "completed"),
        "best_effort": bool(experiment.get("best_effort", False)),
        "measured_pass_count": measured_count,
        "concurrency_detected": concurrency_detected,
        "passes": passes,
        "timeline": [event for events in baseline_events_by_pass.values() for event in events],
        "perfetto": _render_perfetto_trace(baseline_passes, baseline_events_by_pass),
        "kernels": _render_kernel_view(baseline_events_by_pass),
        "summary": {
            "wall_time_ns": wall_time_ns,
            "top_level_total_ns": sum(aggregate_buckets.values()),
            "buckets": aggregate_buckets,
            "sub_buckets": aggregate_sub_buckets,
            "auxiliary_facts": auxiliary_facts,
            "wall_time_stats": wall_time_stats,
        },
    }
    profiler_passes = [
        pass_manifest for pass_manifest in passes if pass_manifest.get("pass_type") in PROFILER_PASS_TYPES
    ]
    if profiler_passes:
        report["profiler"] = _build_profiler_view(profiler_passes)
        profiler_reference = _build_profiler_reference(profiler_passes)
        if profiler_reference:
            report["summary"]["profiler_reference"] = profiler_reference
    return report


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
    profiler_reference = summary.get("profiler_reference", {})
    nsys_reference = profiler_reference.get("nsys")
    if nsys_reference is not None:
        lines.extend(
            [
                "",
                "Profiler Reference",
                (
                    f"nsys: {nsys_reference.get('state')} "
                    f"(artifact_exists: {str(bool(nsys_reference.get('artifact_exists'))).lower()})"
                ),
            ]
        )
        top_kernels = nsys_reference.get("top_kernels", [])
        if top_kernels:
            for kernel in top_kernels[:3]:
                lines.append(
                    f"top_kernel: {kernel['kernel_name']} {_format_duration_ns(kernel['gpu_time_ns'])}"
                )
        if nsys_reference.get("error"):
            lines.append(f"nsys_error: {nsys_reference['error']}")
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
    (output_dir / "perfetto.json").write_text(
        json.dumps(report["perfetto"], indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (output_dir / "kernels.json").write_text(
        json.dumps(report["kernels"], indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    if "profiler" in report:
        (output_dir / "profiler.json").write_text(
            json.dumps(report["profiler"], indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    return output_dir
