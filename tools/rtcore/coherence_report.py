"""Summarize all-depth RT ray coherence from a record-trace render log.

Background: geometric similarity alone does not prove that rays share BVH
work.  The record-trace RTL variant emits ray metadata, world-space ray
samples, node visits, and physical AS-line requests.  This tool joins them by
the scheduler's trace epoch and reports both geometric and traversal coherence.

Usage: python3 tools/rtcore.py coherence --log RENDER_LOG --output REPORT_DIR

Maintenance: this parser intentionally consumes only `[RTCORE coherence-*]`
and `[RTCORE node]` records.  Keep these records diagnostic-only; normal RTL
and normal PMU runs must not depend on this tool or its fields.
"""
from __future__ import annotations

import argparse
import itertools
import json
import math
import re
import statistics
import struct
from collections import defaultdict
from pathlib import Path
from typing import Iterable

META_RE = re.compile(
    r"\[RTCORE coherence-meta\] warp=\s*(\d+) epoch=\s*(\d+) issue=\s*(\d+) lane=\s*(\d+) depth=\s*(\d+) "
    r"active_level=\s*(\d+) mask=0x([0-9a-fA-F]+)"
)
RAY_RE = re.compile(
    r"\[RTCORE coherence-ray\] warp=\s*(\d+) epoch=\s*(\d+) lane=\s*(\d+) depth=\s*(\d+) "
    r"active_level=\s*(\d+) source=(\w+) "
    r"o=\[([0-9a-fA-F]+),([0-9a-fA-F]+),([0-9a-fA-F]+)\] "
    r"d=\[([0-9a-fA-F]+),([0-9a-fA-F]+),([0-9a-fA-F]+)\] "
    r"tmin=([0-9a-fA-F]+) tmax=([0-9a-fA-F]+)"
)
NODE_RE = re.compile(
    r"\[RTCORE node\] warp=\s*(\d+).* epoch=\s*(\d+) issue=\s*(\d+) lane=\s*(\d+) kind=\s*(\d+) ref=0x([0-9a-fA-F]+) object=(\d+)"
)
LINE_RE = re.compile(
    r"\[RTCORE coherence-line\] warp=\s*(\d+) epoch=\s*(\d+) lane=\s*(\d+) addr=0x([0-9a-fA-F]+) is64=(\d+) object=(\d+)"
)


def f32(hex_word: str) -> float:
    return struct.unpack("!f", struct.pack("!I", int(hex_word, 16)))[0]


def finite_vector(values: Iterable[float]) -> bool:
    return all(math.isfinite(v) for v in values)


def norm(v: tuple[float, float, float]) -> float:
    return math.sqrt(sum(x * x for x in v))


def normalize(v: tuple[float, float, float]) -> tuple[float, float, float] | None:
    length = norm(v)
    if not math.isfinite(length) or length <= 1e-20:
        return None
    return tuple(x / length for x in v)


def percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    pos = (len(ordered) - 1) * q
    lo, hi = int(math.floor(pos)), int(math.ceil(pos))
    return ordered[lo] if lo == hi else ordered[lo] * (hi - pos) + ordered[hi] * (pos - lo)


def mean(values: Iterable[float]) -> float | None:
    values = list(values)
    return sum(values) / len(values) if values else None


def depth_bucket(depth: int) -> str:
    return str(depth) if depth < 3 else "3+"


def assign_dynamic_depth(meta: dict[tuple[int, int, int], dict]) -> None:
    """Assign ray depth from the ordered commands of each logical lane.

    A normal RTcore session is released after a terminal traversal.  The next
    traceRay from a raygen loop therefore returns as freshCommand, so neither
    fresh/restart nor compiler active_level alone denotes its dynamic bounce.
    issueId is monotonically issued by that shader warp; repeated appearances
    of one lane are precisely its sequential traversal rays.
    """
    by_lane: dict[tuple[int, int], list[tuple[int, int, int]]] = defaultdict(list)
    for warp, epoch, lane in meta:
        by_lane[(warp, lane)].append((warp, epoch, lane))
    for keys in by_lane.values():
        for dynamic_depth, key in enumerate(sorted(keys, key=lambda item: meta[item]["issue"])):
            meta[key]["trace_depth"] = dynamic_depth


def lcp(a: list[tuple[int, int]], b: list[tuple[int, int]]) -> int:
    count = 0
    for left, right in zip(a, b):
        if left != right:
            break
        count += 1
    return count


def fmt(value: float | int | None, digits: int = 3) -> str:
    if value is None:
        return "—"
    if isinstance(value, int):
        return str(value)
    if value and abs(value) < 10 ** -digits:
        return f"{value:.{digits}e}"
    return f"{value:.{digits}f}"


def parse_log(path: Path) -> dict:
    meta: dict[tuple[int, int, int], dict] = {}
    rays: dict[tuple[int, int, int], dict] = {}
    nodes: dict[tuple[int, int, int], list[tuple[int, int]]] = defaultdict(list)
    lines: dict[tuple[int, int, int], list[int]] = defaultdict(list)
    ignored = 0
    for text in path.read_text(errors="replace").splitlines():
        if match := META_RE.search(text):
            warp, epoch, issue, lane, depth, active_level, mask = match.groups()
            meta[(int(warp), int(epoch), int(lane))] = {"issue": int(issue), "depth": int(depth),
                                             "active_level": int(active_level), "mask": int(mask, 16)}
        elif match := RAY_RE.search(text):
            fields = match.groups()
            warp, epoch, lane, depth = map(int, fields[:4])
            origin = tuple(f32(x) for x in fields[6:9])
            direction = tuple(f32(x) for x in fields[9:12])
            record = {"depth": depth, "active_level": int(fields[4]), "source": fields[5], "origin": origin,
                      "direction": direction, "tmin": f32(fields[12]), "tmax": f32(fields[13])}
            # A ray can consume many world-space nodes.  The first consumer is
            # its root-side sample and avoids overweighting long traversals.
            rays.setdefault((warp, epoch, lane), record)
        elif match := NODE_RE.search(text):
            warp, epoch, _issue, lane, kind, ref, object_ray = match.groups()
            nodes[(int(warp), int(epoch), int(lane))].append((int(object_ray), int(ref, 16)))
        elif match := LINE_RE.search(text):
            warp, epoch, lane, address, _is64, _object_ray = match.groups()
            lines[(int(warp), int(epoch), int(lane))].append(int(address, 16))
        elif "[RTCORE coherence" in text or "[RTCORE node]" in text:
            ignored += 1
    assign_dynamic_depth(meta)
    return {"meta": meta, "rays": rays, "nodes": nodes, "lines": lines, "ignored": ignored}


def cohort_metrics(keys: list[tuple[int, int, int]], parsed: dict) -> dict:
    rays = [parsed["rays"][key] for key in keys if key in parsed["rays"]]
    dirs = [normalize(ray["direction"]) for ray in rays]
    dirs = [d for d in dirs if d is not None]
    result: dict[str, float | int | None] = {"rays_with_geometry": len(rays)}
    if dirs:
        direction_sum = tuple(sum(d[axis] for d in dirs) for axis in range(3))
        mean_dir = normalize(direction_sum)
        result["direction_resultant"] = norm(direction_sum) / len(dirs)
        if mean_dir is not None:
            angles = [math.degrees(math.acos(max(-1.0, min(1.0, sum(a * b for a, b in zip(d, mean_dir)))))) for d in dirs]
            result["direction_angle_p50_deg"] = percentile(angles, 0.50)
            result["direction_angle_p95_deg"] = percentile(angles, 0.95)
            origins = [ray["origin"] for ray in rays if finite_vector(ray["origin"])]
            if origins:
                center = tuple(sum(o[axis] for o in origins) / len(origins) for axis in range(3))
                perpendicular = []
                for origin in origins:
                    delta = tuple(origin[axis] - center[axis] for axis in range(3))
                    parallel = sum(delta[axis] * mean_dir[axis] for axis in range(3))
                    perpendicular.append(math.sqrt(max(0.0, sum(x * x for x in delta) - parallel * parallel)))
                result["origin_transverse_rms"] = math.sqrt(sum(x * x for x in perpendicular) / len(perpendicular))
                scale = percentile([ray["tmax"] for ray in rays if math.isfinite(ray["tmax"]) and ray["tmax"] > 1e-20], 0.50)
                result["median_tmax"] = scale
                if scale is not None:
                    result["origin_transverse_over_tmax"] = result["origin_transverse_rms"] / scale
    node_paths = [parsed["nodes"].get(key, [])[:12] for key in keys]
    line_sets = [set(parsed["lines"].get(key, [])) for key in keys]
    result["nodes_per_ray"] = mean(len(path) for path in node_paths)
    result["unique_lines_per_ray"] = (len(set().union(*line_sets)) / len(keys)) if keys else None
    pairs = list(itertools.combinations(range(len(keys)), 2))
    if pairs:
        result["node_lcp_p50"] = percentile([lcp(node_paths[a], node_paths[b]) for a, b in pairs], 0.50)
        result["node_lcp_p95"] = percentile([lcp(node_paths[a], node_paths[b]) for a, b in pairs], 0.95)
        jaccards = []
        for a, b in pairs:
            union = line_sets[a] | line_sets[b]
            if union:
                jaccards.append(len(line_sets[a] & line_sets[b]) / len(union))
        result["line_jaccard_mean"] = mean(jaccards)
    return result


def distribution_stats(values: list[float]) -> dict[str, float | int | None]:
    """Keep a full percentile summary rather than hiding spread behind a mean."""
    if not values:
        return {"n": 0, "mean": None, "p25": None, "p50": None, "p75": None,
                "p95": None, "p99": None, "min": None, "max": None}
    return {"n": len(values), "mean": mean(values), "p25": percentile(values, .25),
            "p50": percentile(values, .50), "p75": percentile(values, .75),
            "p95": percentile(values, .95), "p99": percentile(values, .99),
            "min": min(values), "max": max(values)}


def detailed_distributions(parsed: dict,
                           epochs: dict[tuple[int, int], list[tuple[int, int, int]]]) -> dict[str, dict[str, list[float]]]:
    """Produce ray- and pair-weighted samples for the report's detailed tables."""
    values: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    for keys in epochs.values():
        by_depth: dict[str, list[tuple[int, int, int]]] = defaultdict(list)
        for key in keys:
            by_depth[depth_bucket(parsed["meta"][key]["trace_depth"])].append(key)
        for bucket, group in by_depth.items():
            rays = [(key, parsed["rays"][key]) for key in group if key in parsed["rays"]]
            dirs = [(key, normalize(ray["direction"])) for key, ray in rays]
            dirs = [(key, direction) for key, direction in dirs if direction is not None]
            if dirs:
                direction_sum = tuple(sum(direction[axis] for _, direction in dirs) for axis in range(3))
                mean_dir = normalize(direction_sum)
                if mean_dir is not None:
                    for _, direction in dirs:
                        cosine = max(-1.0, min(1.0, sum(a * b for a, b in zip(direction, mean_dir))))
                        values[bucket]["direction_angle_deg"].append(math.degrees(math.acos(cosine)))
                    finite_origins = [(key, ray["origin"]) for key, ray in rays if finite_vector(ray["origin"])]
                    if finite_origins:
                        center = tuple(sum(origin[axis] for _, origin in finite_origins) / len(finite_origins)
                                       for axis in range(3))
                        for _, origin in finite_origins:
                            delta = tuple(origin[axis] - center[axis] for axis in range(3))
                            parallel = sum(delta[axis] * mean_dir[axis] for axis in range(3))
                            values[bucket]["origin_transverse"].append(math.sqrt(max(
                                0.0, sum(x * x for x in delta) - parallel * parallel)))
            paths = [parsed["nodes"].get(key, [])[:12] for key in group]
            line_sets = [set(parsed["lines"].get(key, [])) for key in group]
            for path, line_set in zip(paths, line_sets):
                values[bucket]["nodes_per_ray"].append(float(len(path)))
                values[bucket]["unique_lines_per_ray"].append(float(len(line_set)))
            for left, right in itertools.combinations(range(len(group)), 2):
                values[bucket]["node_lcp"].append(float(lcp(paths[left], paths[right])))
                union = line_sets[left] | line_sets[right]
                if union:
                    values[bucket]["line_jaccard"].append(len(line_sets[left] & line_sets[right]) / len(union))
    return {bucket: dict(metric_values) for bucket, metric_values in values.items()}


def summarize(parsed: dict) -> dict:
    epochs: dict[tuple[int, int], list[tuple[int, int, int]]] = defaultdict(list)
    for key in parsed["meta"]:
        epochs[key[:2]].append(key)
    buckets: dict[str, list[dict]] = defaultdict(list)
    banks: dict[str, list[dict]] = defaultdict(list)
    for epoch, keys in epochs.items():
        by_depth: dict[str, list[tuple[int, int, int]]] = defaultdict(list)
        for key in keys:
            by_depth[depth_bucket(parsed["meta"][key]["trace_depth"])].append(key)
        for bucket, group in by_depth.items():
            metrics = cohort_metrics(group, parsed)
            metrics["rays"] = len(group)
            buckets[bucket].append(metrics)
        for bank in range(8):
            group = [key for key in keys if key[2] % 8 == bank]
            if len(group) >= 2:
                metrics = cohort_metrics(group, parsed)
                metrics["rays"] = len(group)
                banks["lane_mod_8"].append(metrics)
    def aggregate(groups: dict[str, list[dict]]) -> dict:
        output = {}
        for name, samples in groups.items():
            output[name] = {"cohorts": len(samples), "rays": sum(s["rays"] for s in samples)}
            for field in ("rays_with_geometry", "direction_resultant", "direction_angle_p50_deg",
                          "direction_angle_p95_deg", "origin_transverse_rms", "median_tmax",
                          "origin_transverse_over_tmax", "nodes_per_ray", "unique_lines_per_ray",
                          "node_lcp_p50", "node_lcp_p95", "line_jaccard_mean"):
                values = [s[field] for s in samples if s.get(field) is not None]
                output[name][field] = mean(values)
        return output
    active_levels: dict[str, int] = defaultdict(int)
    for item in parsed["meta"].values():
        active_levels[str(item["active_level"])] += 1
    distributions = detailed_distributions(parsed, epochs)
    return {"input": {"meta_rays": len(parsed["meta"]), "geometry_rays": len(parsed["rays"]),
                      "epochs": len(epochs), "ignored_trace_lines": parsed["ignored"],
                      "compiler_active_levels": dict(sorted(active_levels.items(), key=lambda item: int(item[0])))},
            "by_depth": aggregate(buckets), "fixed_iu_bank": aggregate(banks),
            "distributions": {bucket: {name: distribution_stats(items) for name, items in metrics.items()}
                              for bucket, metrics in distributions.items()},
            "histogram_samples": distributions}


def write_charts(summary: dict, output: Path) -> list[str]:
    """Write compact per-depth histograms, keeping source data in data.json."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np
    samples = summary["histogram_samples"]
    buckets = sorted(samples, key=lambda x: 99 if x == "3+" else int(x))
    colors = ["#007c91", "#d47a00", "#7b4f9e", "#a13d50"]
    paths: list[str] = []
    depth_counts = [summary["by_depth"][bucket]["rays"] for bucket in buckets]
    figure, axis = plt.subplots(figsize=(7, 3.8), layout="constrained")
    axis.bar(buckets, depth_counts, color=colors[:len(buckets)])
    axis.set(xlabel="Dynamic ray depth", ylabel="Ray count", title="Dynamic ray-depth distribution")
    for index, count in enumerate(depth_counts): axis.text(index, count, str(count), ha="center", va="bottom")
    path = output / "动态depth分布.png"; figure.savefig(path, dpi=160); plt.close(figure); paths.append(path.name)
    chart_specs = [("direction_angle_deg", "Direction angle to cohort mean (deg)", "方向偏差分布.png"),
                   ("nodes_per_ray", "Node visits per ray (first 12)", "节点访问分布.png"),
                   ("unique_lines_per_ray", "Unique AS lines per ray", "AS-line分布.png"),
                   ("node_lcp", "Node LCP per ray pair", "节点LCP分布.png"),
                   ("line_jaccard", "AS-line Jaccard per ray pair", "AS-lineJaccard分布.png")]
    for metric, xlabel, filename in chart_specs:
        figure, axis = plt.subplots(figsize=(7, 3.8), layout="constrained")
        by_bucket = {bucket: samples[bucket].get(metric, []) for bucket in buckets}
        combined = [value for metric_values in by_bucket.values() for value in metric_values]
        if combined:
            bin_count = min(24, max(4, int(math.sqrt(len(combined)))))
            low, high = min(combined), max(combined)
            if low == high:
                low, high = low - .5, high + .5
            edges = np.linspace(low, high, bin_count + 1)
            centers = (edges[:-1] + edges[1:]) / 2
            bin_width = edges[1] - edges[0]
            group_width = bin_width * .84
            bar_width = group_width / len(buckets)
            for index, (color, bucket) in enumerate(zip(colors, buckets)):
                counts, _ = np.histogram(by_bucket[bucket], bins=edges)
                offset = (index - (len(buckets) - 1) / 2) * bar_width
                axis.bar(centers + offset, counts, width=bar_width, alpha=.9,
                         label=f"depth {bucket} (n={len(by_bucket[bucket])})", color=color,
                         edgecolor="white", linewidth=.25)
        axis.set(xlabel=xlabel, ylabel="Count", title=xlabel + " distribution")
        axis.legend(fontsize=8)
        path = output / filename; figure.savefig(path, dpi=160); plt.close(figure); paths.append(path.name)
    return paths


def write_report(summary: dict, output: Path, source: Path) -> None:
    output.mkdir(parents=True, exist_ok=True)
    (output / "data.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    chart_names = write_charts(summary, output)
    rows = []
    for bucket in sorted(summary["by_depth"], key=lambda x: 99 if x == "3+" else int(x)):
        item = summary["by_depth"][bucket]
        rows.append("| {} | {} | {} | {} | {} | {} | {} | {} | {} |".format(
            bucket, item["cohorts"], item["rays"], fmt(item["direction_resultant"]),
            fmt(item["direction_angle_p95_deg"]), fmt(item["origin_transverse_rms"]),
            fmt(item["origin_transverse_over_tmax"]),
            fmt(item["node_lcp_p50"]), fmt(item["line_jaccard_mean"])))
    bank = summary["fixed_iu_bank"].get("lane_mod_8", {})
    distribution_names = {
        "direction_angle_deg": "方向偏差（°，相对各 cohort 平均方向）",
        "origin_transverse": "起点横向距离（世界坐标）",
        "nodes_per_ray": "每 ray node 访问数（最多计 12）",
        "unique_lines_per_ray": "每 ray 唯一 AS-line 数",
        "node_lcp": "ray pair node LCP（最多计 12）",
        "line_jaccard": "ray pair AS-line Jaccard",
    }
    detailed_tables = []
    for metric, title in distribution_names.items():
        detailed_rows = []
        for bucket in sorted(summary["distributions"], key=lambda x: 99 if x == "3+" else int(x)):
            stats = summary["distributions"][bucket].get(metric, {"n": 0})
            detailed_rows.append("| {} | {} | {} | {} | {} | {} | {} | {} | {} |".format(
                bucket, fmt(stats.get("mean")), fmt(stats.get("p25")), fmt(stats.get("p50")),
                fmt(stats.get("p75")), fmt(stats.get("p95")), fmt(stats.get("p99")),
                fmt(stats.get("min")), fmt(stats.get("max"))))
        detailed_tables.append(f"### {title}\n\n| depth | mean | p25 | p50 | p75 | p95 | p99 | min | max |\n"
                               "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |\n" +
                               "\n".join(detailed_rows))
    report = f"""# 全 depth 光线相干性

来源：`{source}`。本报告按同一 shader lane 的 `issueId` 顺序推导动态 ray depth：该 lane 首次 traversal 为 0，之后再次发起的 traversal 为 1、2…；因此可覆盖 raygen 中顺序写出的 reflection/shadow rays。展示按 `0`、`1`、`2`、`3+` 分桶；原始 compiler `active_level` 另存于 `data.json`，不以它替代动态 ray depth。

## 采样覆盖

| trace epoch | 有 depth 的 ray | 有 world-space origin/direction 的 ray |
| ---: | ---: | ---: |
| {summary['input']['epochs']} | {summary['input']['meta_rays']} | {summary['input']['geometry_rays']} |

几何样本取每个 `(epoch, lane)` 第一次 world-space Box4、Instance 或 direct Triangle 消费；没有进入 world node 的 ray 仍计入 depth/活跃度，但没有几何样本。node 公共前缀最多比较前 12 个 `(object/world, nodeRef)`；line Jaccard 比较实际 AS-line 地址集合。

compiler `active_level` 分布（用于核对 ABI lowering，不用于动态 depth 分桶）：`{summary['input']['compiler_active_levels']}`。

## 每 depth cohort

| depth | cohorts | ray | direction resultant R | direction p95 angle (°) | transverse origin RMS | RMS / median tmax | node LCP p50 | AS-line Jaccard mean |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
{chr(10).join(rows) or '| — | 0 | 0 | — | — | — | — | — | — |'}

`R=|Σ normalized(direction)|/N`：1 表示同向，接近 0 表示方向抵消。origin 指标只保留相对平均方向的横向散布，除以 cohort 的 median `tmax`，因此比绝对世界坐标更能反映“在可见距离内是否会穿过相近 BVH 区域”。

## 与当前硬件分组的关系

当前 Box4 固定分组是 `lane % 8` 相同的四条 lane。至少两条 ray 的这些硬件组共有 {bank.get('cohorts', 0)} 个 cohort；其平均 node LCP p50 为 {fmt(bank.get('node_lcp_p50'))}，平均 AS-line Jaccard 为 {fmt(bank.get('line_jaccard_mean'))}。

这两个行为指标比单独的 origin/direction 更适合决定是否值得做稀疏 warp 合并或按 key 重排：若几何相干低但 node LCP/Jaccard 高，仍可能共享 fetch；若几何相干高但二者低，则重排收益有限。

## 分位数明细

下表的几何/node/line 数值按 ray 统计；LCP 与 Jaccard 按同一 depth cohort 内的 ray pair 统计。`p95` 表示 95% 的样本不超过该值，能揭示少数严重发散 ray，而不会像单个最大值一样过度敏感。

{(chr(10) * 2).join(detailed_tables)}

## 分布图

![动态 depth 分布]({chart_names[0]})

![方向偏差分布]({chart_names[1]})

![节点访问分布]({chart_names[2]})

![AS-line 分布]({chart_names[3]})

![节点 LCP 分布]({chart_names[4]})

![AS-line Jaccard 分布]({chart_names[5]})

## 口径限制

- Instance 之后的 object-space traversal 与 world-space ray 分开编码在 node path 中；不混入 origin/direction 几何统计。
- record-trace 为取得 `TraceMeta1.activeLevel` 额外读取一个 RT Local word，不能引用它的 cycles 作为正常性能数据。
- `3+` 是展示桶，`data.json` 可以保留或扩展精确 depth 统计。
"""
    (output / "报告.md").write_text(report)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--log", type=Path, required=True, help="Verilator render.log from a record-trace build")
    parser.add_argument("--output", type=Path, required=True, help="directory for 报告.md and data.json")
    args = parser.parse_args(argv)
    parsed = parse_log(args.log)
    summary = summarize(parsed)
    write_report(summary, args.output, args.log)
    print(f"Wrote {args.output / '报告.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
