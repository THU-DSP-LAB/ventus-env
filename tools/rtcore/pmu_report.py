"""Offline detailed PMU report. Exact bins stay in CSV; plots never drive RTL.

Use through tools/rtcore.py report. Input is a completed regression run directory,
not a standalone unvalidated PMU JSON. Matplotlib is imported only when plotting.
State names describe the current 16-state schema; no stage/ray-type attribution
or session-only IU utilization is inferred from marginal histograms.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
from pathlib import Path

from .rtcore_regression.perf import load_snapshot, summarize


STATES = ["Idle", "RayLoad", "HeaderRequest", "HeaderPending", "StackReady",
          "NodePending", "BoxInflight", "BoxPush", "TriangleInflight", "Terminal",
          "ResumePending", "InstanceTransform", "BlasHeaderRequest",
          "BlasHeaderPending", "AabbCandidate", "ContinuationInit"]


def histogram(values):
    result = sorted((int(k), int(v)) for k, v in values.items() if int(v))
    if any(k < 0 or v < 0 for k, v in result):
        raise ValueError("negative histogram bin/count")
    return result


def describe(values):
    bins = histogram(values)
    count = sum(v for _, v in bins)
    if not count:
        return None
    def quantile(q):
        target, total = math.ceil(q * count), 0
        for x, n in bins:
            total += n
            if total >= target:
                return x
    return (count, sum(x * n for x, n in bins) / count,
            quantile(.5), quantile(.95), quantile(.99), bins[-1][0])


def table(headers, rows):
    def fmt(v):
        if v is None:
            return "—"
        return f"{v:.3f}" if isinstance(v, float) else str(v)
    return "\n".join(["| " + " | ".join(map(fmt, headers)) + " |",
                       "|" + "---|" * len(headers)] +
                      ["| " + " | ".join(map(fmt, r)) + " |" for r in rows])


def load_case(label, root, case, geometry):
    base = root / case / geometry
    renderer = base / "verilator"
    comparison = json.loads((base / "comparison.verilator.json").read_text())
    status = json.loads((renderer / "render.json").read_text())
    if not comparison.get("passed") or not status.get("process_succeeded"):
        raise ValueError(f"{base}: image/process did not pass")
    path = renderer / "perf.json"
    raw = load_snapshot(path)
    data = summarize(raw)
    data["original_sm"] = raw["sm"]
    if not data.get("extended") or data["extended"]["mode"] != "detailed":
        raise ValueError(f"{path}: detailed PMU required")
    source = {"label": label, "path": str(path.resolve()),
              "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
              "begin": json.loads((renderer / "begin.json").read_text()),
              "provenance": str((root / "provenance.json").resolve())}
    return data, source


def plot_panels(plt, series, specs, path, title, cdf=False):
    fig, axes = plt.subplots(2, 2, figsize=(12, 8), constrained_layout=True)
    colors = ["#2563eb", "#60a5fa", "#dc2626", "#fb923c"]
    for ax, (name, xlabel, getter) in zip(axes.flat, specs):
        for i, (label, sm) in enumerate(series):
            bins = histogram(getter(sm))
            if not bins:
                continue
            xs, ns = zip(*bins)
            total = sum(ns)
            if cdf:
                acc, ys = 0, []
                for n in ns:
                    acc += n
                    ys.append(acc / total * 100)
            else:
                # Unobserved integer bins are zero, not a plateau between observations.
                counts = dict(bins)
                xs = list(range(bins[0][0], bins[-1][0] + 1))
                ys = [counts.get(x, 0) / total * 100 for x in xs]
            ax.step(xs, ys, where="post" if cdf else "mid", label=label,
                    color=colors[i % len(colors)], linestyle="--" if i % 2 else "-")
            ax.plot(xs, ys, ".", color=colors[i % len(colors)], markersize=2)
        ax.set_title(name)
        ax.set_xlabel(xlabel)
        ax.set_ylabel("累计样本占比（%）" if cdf else "样本占比（%）")
        if cdf:
            ax.set_xscale("symlog", linthresh=1)
            ax.set_ylim(0, 102)
        ax.grid(alpha=.2)
        handles, labels = ax.get_legend_handles_labels()
        if handles:
            ax.legend(fontsize=8)
        else:
            ax.text(.5, .5, "无样本", transform=ax.transAxes, ha="center")
    fig.suptitle(title, fontsize=15)
    fig.savefig(path, dpi=150)
    plt.close(fig)


def lane_heatmap(plt, series, path, title):
    import numpy as np
    fig, axes = plt.subplots(len(series), 1, figsize=(13, 2.8 * len(series)),
                             constrained_layout=True, squeeze=False)
    for ax, (label, sm) in zip(axes.flat, series):
        values = np.full((7, 32), np.nan)
        for lane in range(32):
            bins = histogram(sm["statistics"]["histograms"].get(
                f"lane_{lane}_as_acquires_per_address_per_session", {}))
            total = sum(n for _, n in bins)
            if not total:
                continue
            values[:, lane] = 0
            for uses, n in bins:
                group = uses - 1 if uses <= 4 else 4 if uses <= 8 else 5 if uses <= 16 else 6
                values[group, lane] += n / total * 100
        cmap = plt.get_cmap("YlGnBu").copy()
        cmap.set_bad("#dddddd")
        im = ax.imshow(values, aspect="auto", vmin=0, vmax=100, cmap=cmap)
        ax.set_xticks(range(32))
        ax.set_yticks(range(7), ["1", "2", "3", "4", "5–8", "9–16", "17+"])
        ax.set_xlabel("lane ID；灰色表示无样本")
        ax.set_ylabel("同地址访问次数")
        ax.set_title(label)
        fig.colorbar(im, ax=ax, label="该 lane 的地址/session 样本占比（%）")
    fig.suptitle(title)
    fig.savefig(path, dpi=150)
    plt.close(fig)


def write_csv(path, headers, rows):
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(headers)
        writer.writerows(rows)


def generate(runs, cases, geometry, output, plt):
    output.mkdir(parents=True, exist_ok=False)
    md = ["# RTCore 内部 PMU 图表报告", "",
          "本报告只读取已通过的 detailed 回归，不重跑仿真。各曲线分别归一化，SM 不混合。",
          "", "## 统计口径", "",
          "- IU/LSU busy 表示存在已接收但尚未交付结果的任务，包含输出等待；不是带宽利用率。",
          "- IU 表格占比的分母是各单元 sampled_cycles；组平均按单元采样周期加权。",
          "- 延迟 CDF 的分母是已完成请求数；分位数采用精确整数分桶的 nearest-rank。横轴为对称对数尺度。",
          "- 字段名 refill 在当前 RTL 中指 candidate 请求发出握手，不是回包；injection 才是回包注入 AS table。",
          "- AS 占用、IU 发射/ready 和在途分布按全部采样周期统计；stack-ready、状态和存活 lane 按 session 活跃周期统计。",
          "- command_active_lanes 按命令计数；session_active_lanes 按周期计数，两者不能直接混作同一种样本。",
          "- 状态占比按 session 内有效 mask lane 的累计 lane-cycle 归一化，包含 Terminal，不是单次状态停留延迟。",
          "- 条目 uses 是一次条目生命周期内的服务次数；同地址 fetch/访问次数限定在一个 session 内，不是跨 session 缓存命中率。",
          "- 阻塞计数可能重叠，不能相加成总停顿，更不能画成占比合计100%的饼图。",
          "- CSV 保留全部整数分桶及计数，图表按分布绘制，不把缺失指标当成0。", ""]
    sources, all_counters, all_bins = [], [], []
    for case in cases:
        datasets = []
        for label, root in runs:
            data, source = load_case(label, root, case, geometry)
            datasets.append((label, data))
            sources.append(source)
        series = [(f"{label} SM{sm['id']}", sm["rt_detailed"])
                  for label, data in datasets for sm in data["extended"]["sm"]]
        md += [f"## {case}（{geometry}）", "", "### 总览", "",
               table(["版本", "cycles", "traverse", "活跃lane遍历", "Box4", "Triangle", "AS acquire", "AS refill"],
                     [[label, d["clock_cycles"], *[d["counters"][k] for k in
                       ("traverse_issued", "traverse_active_lanes", "box4_issued", "triangle_issued", "as_line_acquires", "as_cache_refills")]]
                      for label, d in datasets]), ""]
        md += ["### AS 缓存策略", "",
               table(["版本/SM", "策略", "驱逐后再分配次数（session内）"],
                     [[label, sm.get("as_policy", "未记录"), sm.get("post_eviction_reaccess")]
                      for label, sm in series]), ""]
        # Export every group, including per-IU and all lane histograms.
        for label, d in datasets:
            for sm in d["extended"]["sm"]:
                rt = sm["rt_detailed"]
                groups = [("rt", rt["statistics"]), ("lsu", rt["lsu"])]
                groups += [(f"{u['kind']}_{u['index']}", u["statistics"]) for u in rt["units"]]
                groups += [(g, {"counters": sm[g], "histograms": {}}) for g in ("pipeline", "inst_class", "dcache")]
                original = next(s["rtcore"] for s in d["original_sm"] if s["id"] == sm["id"])
                groups += [("rtcore", {"counters": original, "histograms": {}})]
                for group, stats in groups:
                    all_counters += [[case, label, sm["id"], group, k, v] for k, v in stats["counters"].items()]
                    for k, h in stats["histograms"].items():
                        all_bins += [[case, label, sm["id"], group, k, x, n] for x, n in histogram(h)]
            for i, l2 in enumerate(d["extended"].get("l2") or []):
                all_counters += [[case, label, f"L2-{i}", "l2", k, v] for k, v in l2["counters"].items()]
        rows = []
        for label, sm in series:
            for kind in ("box4", "triangle", "resolve", "lsu"):
                units = [sm["lsu"]] if kind == "lsu" else [u["statistics"] for u in sm["units"] if u["kind"] == kind]
                cs = [u["counters"] for u in units]
                n = sum(c["sampled_cycles"] for c in cs)
                counts = [sum(c[k] for c in cs) for k in ("busy_cycles", "empty_ready_without_dispatch_cycles", "output_blocked_cycles", "issues")]
                rows.append([label, kind, len(cs), *[v / n * 100 if n else None for v in counts[:3]], counts[3]])
        md += ["### IU／LSU 利用情况", "", table(["版本/SM", "单元", "数量", "busy %", "空闲且ready无派发 %", "输出阻塞 %", "发射次数"], rows),
               "", "表中三种占比不是互斥分类。每个 IU 的原始计数和延迟/在途分桶见 CSV。", ""]
        unit_maps = [{f"{u['kind']}_{u['index']}": u["statistics"]["counters"] for u in sm["units"]} for _, sm in series]
        names = list(dict.fromkeys(name for units in unit_maps for name in units))
        md += ["#### 各 IU 的 busy 占比（%）", "",
               table(["IU", *[label for label, _ in series]],
                     [[name, *[u[name]["busy_cycles"] / u[name]["sampled_cycles"] * 100
                               if name in u and u[name]["sampled_cycles"] else None for u in unit_maps]] for name in names]), ""]
        h = lambda s, k: s["statistics"]["histograms"].get(k, {})
        latency = [("RT LSU 请求→回包", "周期", lambda s: s["lsu"]["histograms"].get("latency_cycles", {})),
                   ("AS acquire→serve", "周期", lambda s: h(s, "as_acquire_to_serve_cycles")),
                   ("条目分配→请求发出", "周期", lambda s: h(s, "as_allocate_to_refill_cycles")),
                   ("请求发出→回包注入", "周期", lambda s: h(s, "as_refill_to_injection_cycles"))]
        md += ["### 访存与 node 等待", "", table(["版本/SM", "延迟", "样本数", "均值", "P50", "P95", "P99", "最大值"],
                [[label, name, *(describe(getter(s)) or (0, None, None, None, None, None))]
                 for label, s in series for name, _, getter in latency]), ""]
        charts = [("latency", "延迟累计分布", latency, True),
                  ("occupancy", "AS table 占用分布", [(name, "条目数", lambda s, key=key: h(s, key)) for name, key in
                    [("全部占用", "as_occupied_entries"), ("尚未返回", "as_unreturned_entries"), ("已返回", "as_returned_entries"), ("ready", "as_ready_entries")]], False),
                  ("concurrency", "调度与在途并发", [
                    ("Stack-ready（session窗口）", "lane数", lambda s: h(s, "stack_ready_entries")),
                    ("可接收的 IU（全部11个单元）", "IU数", lambda s: h(s, "iu_ready_inputs_per_cycle")),
                    ("每拍实际 IU 发射", "发射数", lambda s: h(s, "iu_issues_per_cycle")),
                    ("RT LSU 在途", "请求数", lambda s: s["lsu"]["histograms"].get("outstanding", {}))], False),
                  ("reuse", "条目服务与同地址复用", [(name, "次数/数量", lambda s, key=key: h(s, key)) for name, key in
                    [("已结束条目服务次数", "as_completed_entry_uses"), ("已结束条目的不同lane数", "as_completed_entry_unique_lanes"),
                     ("同地址每session的fetch次数", "as_fetches_per_address_per_session"), ("每lane同地址每session访问次数", "as_acquires_per_lane_address_per_session")]], False),
                  ("lanes", "活跃lane与状态分布", [(name, "lane数", lambda s, key=key: h(s, key)) for name, key in
                    [("每条命令活跃lane", "command_active_lanes"), ("Session活跃mask", "session_active_lanes"),
                     ("正在遍历的lane", "traversing_lanes"), ("NodePending lane", "state_5_lanes")]], False)]
        for suffix, title, specs, cdf in charts:
            filename = f"{case}-{suffix}.png"
            plot_panels(plt, series, specs, output / filename, f"{case} · {title}", cdf)
            md += [f"### {title}", "", f"![{title}]({filename})", ""]
            if not cdf:
                md += [table(["版本/SM", "分布", "样本数", "均值", "P50", "P95", "P99", "最大值"],
                             [[label, name, *(describe(getter(s)) or (0, None, None, None, None, None))]
                              for label, s in series for name, _, getter in specs]), ""]
        filename = f"{case}-lane-reuse.png"
        lane_heatmap(plt, series, output / filename, f"{case} · lane 同地址复用")
        md += ["### 逐 lane 复用", "", f"![逐lane复用]({filename})", "",
               "热力图每列独立归一化，样本是 (lane, 地址, session)，不是条目的缓存命中次数。原始样本量见 CSV。", ""]
        totals = [sum(s["statistics"]["counters"].get(f"state_{i}_lane_cycles", 0) for i in range(len(STATES))) for _, s in series]
        md += ["### 状态占用", "", table(["状态（lane-cycle %）", *[label for label, _ in series]],
               [[name, *[s["statistics"]["counters"].get(f"state_{i}_lane_cycles", 0) / n * 100 if n else None
                            for (_, s), n in zip(series, totals)]] for i, name in enumerate(STATES)]), ""]
        blockers = ["pop_blocked_header_cycles", "pop_blocked_push_cycles", "pop_blocked_header_line_cycles",
                    "as_acquire_blocked_cycles", "ready_blocked_ray_cycles", "ready_blocked_box_cycles",
                    "ready_blocked_triangle_cycles", "ready_blocked_other_cycles",
                    "stack_ready_not_selected_lane_cycles", "as_ready_not_selected_entry_cycles"]
        md += ["### 已观测阻塞与未选中计数（不互斥）", "",
               table(["字段", *[label for label, _ in series]],
                     [[k, *[s["statistics"]["counters"].get(k) for _, s in series]] for k in blockers]), "",
               "前8项单位为周期；后2项分别为 lane-cycle 和 entry-cycle。不是完整、互斥的调度停顿分解。", ""]
        md += ["### GPU 计数", ""]
        gpu = [(f"{label} SM{sm['id']}", sm) for label, d in datasets for sm in d["extended"]["sm"]]
        keys = [("pipeline", "data_dep_stall_cycles"), ("pipeline", "frontend_stall_cycles"),
                ("pipeline", "lsu_backpressure_cycles"), ("dcache", "read_req"), ("dcache", "read_miss"),
                ("dcache", "mshr_full_cycles"), ("dcache", "rtab_replays")]
        md += [table(["原始字段", *[label for label, _ in gpu]],
                     [[f"{g}.{k}", *[sm[g].get(k) for _, sm in gpu]] for g, k in keys]), "",
               "GPU 各字段有自己的事件口径，不直接相加；全部 L1/L2、前端及指令分类计数见 counters.csv。", ""]
        l2s = [(f"{label} L2-{i}", l2["counters"]) for label, d in datasets
               for i, l2 in enumerate(d["extended"].get("l2") or [])]
        if l2s:
            keys = list(dict.fromkeys(k for _, counters in l2s for k in counters))
            md += ["#### 全局 L2（不是每 SM 的计数）", "",
                   table(["字段", *[label for label, _ in l2s]],
                         [[k, *[c.get(k) for _, c in l2s]] for k in keys]), "",
                   "request/response/memoryRequest/memoryResponse 为握手 beat 数；Blocked 为阻塞周期，directoryHit/Miss 为目录结果事件。", ""]
    md += ["## 数据与限制", "", "- [完整计数 CSV](counters.csv)", "- [完整直方图分桶 CSV](histograms.csv)",
           "- [输入文件、哈希与执行环境](sources.json)", "",
           "未采集：仅session活跃期间的IU busy联合计数、primary/shadow/reflection语义分类、完整candidate策略事件、逐周期时间线。",
           "AS策略见每个输入的as_policy：one_shot_unsent_merge没有resident eviction，驱逐后再次访问不可用；retained_reverse_replace在替换/清空时结算条目uses，并统计session内曾被驱逐地址的再次分配次数。两者的条目生命周期不同。",
           "各延迟分布是不同阶段的边际分布，不能把P95相加。少量命令样本（16×9单帧）不能代表大场景的稳定分布。"]
    write_csv(output / "counters.csv", ["case", "version", "SM_or_L2", "group", "metric", "count"], all_counters)
    write_csv(output / "histograms.csv", ["case", "version", "SM", "group", "metric", "bin", "count"], all_bins)
    (output / "sources.json").write_text(json.dumps(sources, ensure_ascii=False, indent=2) + "\n")
    (output / "报告.md").write_text("\n".join(md) + "\n", encoding="utf-8")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="append", required=True, metavar="LABEL=RUN_DIR")
    parser.add_argument("--case", action="append", default=None)
    parser.add_argument("--geometry", default="16x9")
    parser.add_argument("--output", type=Path, required=True, help="new directory; never overwrite a report")
    args = parser.parse_args(argv)
    runs = []
    for spec in args.run:
        if "=" not in spec:
            parser.error("--run must be LABEL=RUN_DIR")
        label, path = spec.split("=", 1)
        if not label or any(label == previous for previous, _ in runs):
            parser.error("labels must be nonempty and unique")
        runs.append((label, Path(path)))
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib import font_manager
    font = Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc")
    if font.exists():
        font_manager.fontManager.addfont(str(font))
        plt.rcParams["font.family"] = font_manager.FontProperties(fname=str(font)).get_name()
    plt.rcParams["axes.unicode_minus"] = False
    generate(runs, args.case or ["raytracingshadows", "raytracingreflections"], args.geometry, args.output, plt)
    print(args.output / "报告.md")
    return 0
