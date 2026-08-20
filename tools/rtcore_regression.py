#!/usr/bin/env python3
"""Run reproducible dual-renderer Vulkan RT regression cases.

Background: Ventus RTcore changes must be checked against a software Vulkan
reference and a selectable hardware backend, not merely a successful launch.
Flow: load the testcase manifest, rebuild each selected app, capture PPM/PNG
from software and hardware, compare pixels, and record every binary/resource.
Usage: python3 tools/rtcore_regression.py [--case raytracingshadows] [--hardware-runner spike]
Maintenance: keep case eligibility and capture hooks in the testcase manifest;
never use mesa/build-ventus-local for a full-app or Spike bridge run.
"""
import importlib.util
import sys
from pathlib import Path


PACKAGE_NAME = "ventus_rtcore_regression"
PACKAGE_DIR = Path(__file__).resolve().parent / "rtcore_regression"


def load_main():
    spec = importlib.util.spec_from_file_location(
        PACKAGE_NAME,
        PACKAGE_DIR / "__init__.py",
        submodule_search_locations=[str(PACKAGE_DIR)],
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[PACKAGE_NAME] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return __import__(f"{PACKAGE_NAME}.cli", fromlist=["main"]).main


if __name__ == "__main__":
    raise SystemExit(load_main()())
