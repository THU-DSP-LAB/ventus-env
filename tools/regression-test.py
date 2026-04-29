#!/usr/bin/env python3
# Ventus regression test entrypoint.
# Loads the multi-file implementation from tools/regression-test/
# and preserves the standard invocation form:
#   python3 tools/regression-test.py [options]
import importlib.util
import sys
from pathlib import Path


PACKAGE_NAME = "ventus_regression_test"
PACKAGE_DIR = Path(__file__).resolve().parent / "regression-test"


def load_main():
    spec = importlib.util.spec_from_file_location(
        PACKAGE_NAME,
        PACKAGE_DIR / "__init__.py",
        submodule_search_locations=[str(PACKAGE_DIR)],
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[PACKAGE_NAME] = module
    spec.loader.exec_module(module)
    return __import__(f"{PACKAGE_NAME}.cli", fromlist=["main"]).main


if __name__ == "__main__":
    raise SystemExit(load_main()())
