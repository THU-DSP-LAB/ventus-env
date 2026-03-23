from __future__ import annotations

import argparse
import os
from pathlib import Path

from ventus_perf import report as report_module
from ventus_perf import wrap as wrap_module


DEFAULT_OUTPUT_ROOT = Path("build/ventus-perf")


class _HelpFormatter(
    argparse.RawDescriptionHelpFormatter,
    argparse.ArgumentDefaultsHelpFormatter,
):
    pass


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ventus-perf.py",
        description="Run wrapper-managed Ventus perf passes and regenerate offline reports.",
        epilog=(
            "Examples:\n"
            "  python3 tools/ventus-perf.py run --repeat 1 -- ./run\n"
            "  python3 tools/ventus-perf.py run --profile nsys --repeat 1 -- ./run\n"
            "  python3 tools/ventus-perf.py report build/ventus-perf/<experiment-id>/\n\n"
            f"Notes:\n"
            f"  The driver default backend is {wrap_module.DEFAULT_BACKEND} when VENTUS_BACKEND is unset.\n"
            f"  Perf runs currently support: {wrap_module.supported_phase1_backend_list()}.\n"
            f"  Reports are written under {DEFAULT_OUTPUT_ROOT}/<experiment-id>/reports/."
        ),
        formatter_class=_HelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="subcommand", required=True)

    run_parser = subparsers.add_parser(
        "run",
        help="run a wrapper-managed experiment and generate reports",
        description=(
            "Execute the child command through the Ventus perf wrapper, capture manifests/logs, "
            "and render offline reports."
        ),
        epilog=(
            "Examples:\n"
            "  python3 tools/ventus-perf.py run --repeat 1 -- ./run\n"
            "  python3 tools/ventus-perf.py run --warmup 1 --repeat 3 -- ./run\n"
            "  python3 tools/ventus-perf.py run --profile nsys --repeat 1 -- ./run\n\n"
            f"Backend notes:\n"
            f"  The driver default backend is {wrap_module.DEFAULT_BACKEND} when VENTUS_BACKEND is unset.\n"
            f"  Perf runs currently support: {wrap_module.supported_phase1_backend_list()}."
        ),
        formatter_class=_HelpFormatter,
    )
    run_parser.add_argument("--warmup", type=int, default=0, help="number of warmup passes to run")
    run_parser.add_argument("--repeat", type=int, default=1, help="number of measured passes to run")
    run_parser.add_argument(
        "--profile",
        action="append",
        choices=["nsys", "ncu"],
        default=[],
        help="append an explicit profiler pass after the measured pass",
    )
    run_parser.add_argument(
        "--ncu-kernel",
        help="target kernel name for the NCU profiler pass; requires --profile ncu",
    )
    run_parser.add_argument(
        "child_command",
        nargs=argparse.REMAINDER,
        help="child command, introduced by --, for example: -- ./run",
    )

    report_parser = subparsers.add_parser(
        "report",
        help="regenerate reports from an experiment or pass directory",
        description="Load an existing wrapper-managed experiment or pass directory and rewrite reports.",
        epilog=(
            "Examples:\n"
            "  python3 tools/ventus-perf.py report build/ventus-perf/<experiment-id>/\n"
            "  python3 tools/ventus-perf.py report build/ventus-perf/<experiment-id>/passes/measure-0001/\n\n"
            "Outputs:\n"
            "  summary.txt, summary.json, timeline.json, kernels.json, perfetto.json,\n"
            "  and profiler.json when profiler passes exist."
        ),
        formatter_class=_HelpFormatter,
    )
    report_parser.add_argument("input_dir", help="experiment directory or wrapper-managed pass directory")
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
        output_root=DEFAULT_OUTPUT_ROOT,
    )
    report = report_module.load_input_report(experiment_dir)
    output_dir = report_module.write_report_outputs(experiment_dir, report)
    print(f"Experiment: {experiment_dir}")
    print(f"Reports: {output_dir}")
    print()
    print(report_module.render_summary_text(report))
    return 0


def _report_command(args: argparse.Namespace) -> int:
    report = report_module.load_input_report(Path(args.input_dir))
    output_dir = report_module.write_report_outputs(Path(args.input_dir), report)
    print(f"Reports: {output_dir}")
    print()
    print(report_module.render_summary_text(report))
    return 0 if output_dir else 1


def main(argv: list[str]) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        if args.subcommand == "run":
            return _run_command(args)
        if args.subcommand == "report":
            return _report_command(args)
        raise SystemExit(f"unsupported command: {args.subcommand}")
    except (FileNotFoundError, ValueError) as exc:
        parser.exit(2, f"{parser.prog}: error: {exc}\n")
