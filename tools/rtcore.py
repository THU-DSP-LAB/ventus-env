#!/usr/bin/env python3
"""Maintain the RT ABI and run software/Spike/Verilator image regressions.

Background: keep the two maintained RT tools behind one portable entry point.
Flow: dispatch ABI generation/checking, the image gate or offline PMU reporting.
Usage: python3 tools/rtcore.py abi --check
       python3 tools/rtcore.py regression --hardware-runner all --width 4 --height 4
       python3 tools/rtcore.py report --run LABEL=RUN_DIR --output REPORT_DIR
Maintenance: implementation and tests live in tools/rtcore; no PYTHONPATH setup
is required. Full-app tests must use canonical mesa/build-ventus.
"""
import argparse
import sys

from rtcore import rtcore_abi
from rtcore.rtcore_regression import cli


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("abi", "regression", "report"))
    args = parser.parse_args(sys.argv[1:2])
    if args.command == "report":
        from rtcore import pmu_report
        return pmu_report.main(sys.argv[2:])
    run = rtcore_abi.main if args.command == "abi" else cli.main
    return run(sys.argv[2:])


if __name__ == "__main__":
    raise SystemExit(main())
