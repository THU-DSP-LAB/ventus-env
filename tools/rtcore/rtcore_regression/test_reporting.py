"""Run with python3 -m unittest tools.rtcore.rtcore_regression.test_reporting."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from . import cli
from .cli import RegressionError, compare_images, parse_ppm, run_command
from .reporting import log_excerpt


class ReportingTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name) / "verilator"
        self.directory.mkdir()
        self.log = self.directory / "render.log"

    def run_renderer(self, code, timeout=5):
        return run_command([sys.executable, "-c", code], self.directory,
                           os.environ.copy(), self.log, allow_failure=True,
                           timeout_seconds=timeout)

    def status(self):
        return json.loads((self.directory / "render.json").read_text())

    def test_assertion_without_image(self):
        with self.assertRaisesRegex(RegressionError, "process_failed.*exit=7"):
            self.run_renderer("print('Assertion failed: release warp'); raise SystemExit(7)")
        self.assertEqual(self.status()["exit_status"], 7)
        self.assertIn("release warp", self.status()["error_excerpt"])
        self.assertTrue((self.directory / "begin.json").exists())

    def test_zero_exit_missing_image(self):
        with self.assertRaisesRegex(RegressionError, "missing_image"):
            self.run_renderer("pass")
        self.assertTrue(self.status()["process_succeeded"])
        self.assertFalse(self.status()["image_was_captured"])

    def test_image_then_failure_is_not_success(self):
        self.run_renderer("from pathlib import Path; Path('image.ppm').write_bytes(b'P6\\n1 1\\n255\\nabc'); raise SystemExit(6)")
        self.assertTrue(self.status()["image_was_captured"])
        self.assertFalse(self.status()["process_succeeded"])
        self.assertEqual(self.status()["status"], "process_failed")

    def test_timeout_records_result(self):
        with self.assertRaisesRegex(RegressionError, "timed out"):
            self.run_renderer("import time; time.sleep(20)", timeout=0.1)
        self.assertEqual(self.status()["status"], "timeout")
        self.assertFalse(self.status()["process_succeeded"])

    def test_normal_capture(self):
        self.assertEqual(self.run_renderer("from pathlib import Path; Path('image.ppm').write_bytes(b'P6\\n1 1\\n255\\nabc')"), 0)
        self.assertEqual(self.status()["status"], "captured")

    def test_log_excerpt_bounds_and_literal_newlines(self):
        self.log.write_text("x" * 100000 + r"\nAssertion failed: owner\nAborting...")
        excerpt = log_excerpt(self.log)
        self.assertLessEqual(len(excerpt), 4096)
        self.assertIn("Assertion failed: owner\nAborting", excerpt)

    def test_launcher_failure_is_included_in_report(self):
        (self.directory / "xvfb.log").write_text("Fatal server error: Cannot establish listening sockets")
        status = cli.render_status(self.directory, 1)
        self.assertIn("Cannot establish listening sockets", status["error_excerpt"])
        self.assertTrue(status["launcher_log"].endswith("xvfb.log"))

    def test_zero_exit_hang_keeps_watchdog_reason(self):
        self.log.write_text("PMU watchdog detected HANG\n" + "normal benchmark output\n" * 15)
        status = cli.render_status(self.directory, 0)
        self.assertEqual(status["status"], "missing_image")
        self.assertIn("watchdog detected HANG", status["error_excerpt"])

    def test_xvfb_cleanup_failure_after_capture_is_benign(self):
        (self.directory / "image.ppm").write_bytes(b"P6\n1 1\n255\nabc")
        self.log.write_text("Benchmark finished\n/usr/bin/xvfb-run: 91: kill: No such process\n")
        self.assertTrue(cli.successful_xvfb_cleanup_failure(
            1, self.log, self.directory / "image.ppm"))
        self.assertFalse(cli.successful_xvfb_cleanup_failure(
            1, self.log, self.directory / "missing.ppm"))

    def test_ppm_whitespace_pixel_and_last_row_difference(self):
        reference = self.directory / "ref.ppm"
        actual = self.directory / "actual.ppm"
        reference.write_bytes(b"P6\n4 4\n255\n" + bytes([10, 13, 32]) * 12 + bytes([119] * 12))
        actual.write_bytes(b"P6\n4 4\n255\n" + bytes([10, 13, 32]) * 12 + bytes([0, 0, 51]) * 4)
        self.assertEqual(len(parse_ppm(reference)[2]), 48)
        result = compare_images(reference, actual, 0)
        self.assertEqual(result["differing_pixels"], 4)
        self.assertEqual(result["differing_pixels_by_row"], [0, 0, 0, 4])
        self.assertEqual(result["difference_bounds"], dict(x_min=0, x_max=3, y_min=3, y_max=3))
        self.assertEqual(result["difference_samples"][0]["actual"], [0, 0, 51])

    def test_failed_process_cannot_pass_matching_images(self):
        image = self.directory / "image.ppm"
        image.write_bytes(b"P6\n1 1\n255\nabc")
        software_dir = self.directory.parent / "software"
        software_dir.mkdir()
        (software_dir / "image.ppm").write_bytes(image.read_bytes())
        cli.render_status(self.directory, -6)
        cli.render_status(software_dir, 0)
        args = SimpleNamespace(timeout_seconds=5, max_abs_error=0, manifest=Path("unused"))
        with patch.object(cli, "verilator_render", return_value=(image, None)), \
             patch.object(cli, "record_case") as record:
            result = cli.run_hardware_case(
                {"id": "test"}, "verilator", {}, Path(self.temp.name), Path("app"),
                1, 1, 1, self.directory.parent, software_dir / "image.ppm", args,
                {"execution_profile": "megakernel"})
        self.assertFalse(result["passed"])
        self.assertIn("process_failed", result["detail"])
        self.assertFalse(record.call_args.args[5]["passed"])

    def test_pmu_failures_keep_captured_image_and_status(self):
        root = Path(self.temp.name)
        runner = {"kind": "ventus-verilator", "mesa_build": "mesa", "llvm_build": "llvm",
                  "driver_library": "driver.so", "runtime_library_dir": "runtime"}
        for name in ("driver.so", "runtime/libVentusRTL-withcache.so", "runtime/crt0.o",
                     "llvm/bin/llc", "llvm/bin/ld.lld", "llvm/bin/llvm-nm",
                     "mesa/src/ventus/vulkan/ventus_devenv_icd.x86_64.json"):
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch()
            path.chmod(0o700)
        for invalid in (False, True):
            with self.subTest(invalid=invalid):
                def capture(*args, **kwargs):
                    (self.directory / "image.ppm").write_bytes(b"P6\n1 1\n255\nabc")
                    if invalid:
                        (self.directory / "perf.json").write_text("invalid")
                    return 0
                with patch.object(cli, "ROOT", root), patch.object(cli, "run_command", side_effect=capture), \
                     patch.object(cli, "summarize_verilator_pmu", side_effect=RegressionError("invalid PMU")):
                    image, summary = cli.verilator_render({"id": "test"}, runner, root, root / "app",
                                                         1, 1, 1, root, 5)
                self.assertIsNone(summary)
                self.assertTrue(image.exists())
                self.assertIn("pmu_error", self.status())


if __name__ == "__main__":
    unittest.main()
