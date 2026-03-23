from __future__ import annotations

import argparse
import os
from pathlib import Path

from ventus_perf import report as report_module
from ventus_perf import wrap as wrap_module


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ventus_perf.py")
    subparsers = parser.add_subparsers(dest="subcommand", required=True)

    run_parser = subparsers.add_parser("run")
    run_parser.add_argument("--warmup", type=int, default=0)
    run_parser.add_argument("--repeat", type=int, default=1)
    run_parser.add_argument("--profile", action="append", choices=["nsys", "ncu"], default=[])
    run_parser.add_argument("--ncu-kernel")
    run_parser.add_argument("child_command", nargs=argparse.REMAINDER)

    report_parser = subparsers.add_parser("report")
    report_parser.add_argument("input_dir")
    return parser


def _run_command(args: argparse.Namespace) -> int:
    if not args.child_command or args.child_command[0] != "--" or len(args.child_command) == 1:
        raise SystemExit("run requires a child command after --")
    if args.ncu_kernel and "ncu" not in args.profile:
        raise SystemExit("--ncu-kernel requires --profile ncu")
    command = args.child_command[1:]
    experiment_dir, _ = wrap_module.run_experiment(
        command=command,
        warmup=args.warmup,
        repeat=args.repeat,
        profiles=args.profile,
        ncu_kernel=args.ncu_kernel,
        env=dict(os.environ),
        output_root=Path("build/ventus-perf"),
    )
    report = report_module.load_input_report(experiment_dir)
    report_module.write_report_outputs(experiment_dir, report)
    print(report_module.render_summary_text(report))
    return 0


def _report_command(args: argparse.Namespace) -> int:
    report = report_module.load_input_report(Path(args.input_dir))
    output_dir = report_module.write_report_outputs(Path(args.input_dir), report)
    print(report_module.render_summary_text(report))
    return 0 if output_dir else 1


def main(argv: list[str]) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.subcommand == "run":
        return _run_command(args)
    if args.subcommand == "report":
        return _report_command(args)
    raise SystemExit(f"unsupported command: {args.subcommand}")
