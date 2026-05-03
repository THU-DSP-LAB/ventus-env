import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path


PACKAGE_NAME = "ventus_regression_test_process_tests"
PACKAGE_DIR = Path(__file__).resolve().parents[1]


def load_module(module_name: str):
    if PACKAGE_NAME not in sys.modules:
        spec = importlib.util.spec_from_file_location(
            PACKAGE_NAME,
            PACKAGE_DIR / "__init__.py",
            submodule_search_locations=[str(PACKAGE_DIR)],
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules[PACKAGE_NAME] = module
        spec.loader.exec_module(module)
    return __import__(f"{PACKAGE_NAME}.{module_name}", fromlist=[module_name])


class ProcessMetricTests(unittest.TestCase):
    def test_run_command_reports_metrics_for_success(self):
        process = load_module("process")
        command = [sys.executable, "-c", "print('metric-ok')"]

        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = Path(tmpdir) / "run.log"
            with open(log_path, "w") as log_file:
                result = process.run_command(
                    command,
                    Path(tmpdir),
                    os.environ.copy(),
                    log_file,
                    5,
                    {},
                )

            self.assertEqual(result.return_code, 0)
            self.assertFalse(result.timed_out)
            self.assertGreaterEqual(result.wall_time_sec, 0)
            self.assertGreater(result.resources.peak_rss_kb, 0)
            self.assertIn("metric-ok", log_path.read_text())

    def test_run_command_reports_metrics_after_timeout(self):
        process = load_module("process")
        command = [sys.executable, "-c", "import time; time.sleep(5)"]

        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = Path(tmpdir) / "run.log"
            with open(log_path, "w") as log_file:
                result = process.run_command(
                    command,
                    Path(tmpdir),
                    os.environ.copy(),
                    log_file,
                    0.1,
                    {},
                )

        self.assertTrue(result.timed_out)
        self.assertLess(result.wall_time_sec, 2)
        self.assertLess(result.return_code, 0)
        self.assertGreater(result.resources.peak_rss_kb, 0)


if __name__ == "__main__":
    unittest.main()
