"""Fast checks for the full-app image-regression launch contract."""
import unittest
import contextlib
import io
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from tools.rtcore.rtcore_regression import cli

from tools.rtcore.rtcore_regression.cli import (
    DEFAULT_BUILD, DEFAULT_WORKLOAD, base_app_command, workload_build_dir,
)


class AppCommandTests(unittest.TestCase):
    def test_generated_workload_build_matches_wrapper(self):
        self.assertEqual(workload_build_dir(DEFAULT_WORKLOAD, None, {"build_dir": "build"}),
                         DEFAULT_BUILD)

    def test_personal_workload_and_explicit_build_are_preserved(self):
        workload = Path("/tmp/personal-workload")
        self.assertEqual(workload_build_dir(workload, None, {"build_dir": "build"}),
                         workload / "build")
        self.assertEqual(workload_build_dir(DEFAULT_WORKLOAD, Path("/tmp/app-build"), {}),
                         Path("/tmp/app-build"))

    def test_image_regression_disables_benchmark_warmup(self):
        for frames in (1, 3):
            self.assertEqual(
                base_app_command(Path('/tmp/raytracingtextures'), 16, 9, frames),
                ['/tmp/raytracingtextures', '--benchmark', '--benchwarmup', '0',
                 '--benchmarkframes', str(frames), '--width', '16', '--height', '9'],
            )


class RegressionSelectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "CMakeLists.txt").touch()
        self.manifest = cli.load_manifest(cli.DEFAULT_MANIFEST)
        self.software_succeeds = True
        self.image_size = None
        self.rendered = []

    def software(self, case, defaults, workload, app, width, height, frames, case_dir, timeout):
        self.rendered.append((case["id"], width, height))
        directory = case_dir / "software"
        directory.mkdir()
        w, h = self.image_size or (width, height)
        image = directory / "image.ppm"
        image.write_bytes(f"P6\n{w} {h}\n255\n".encode() + b"abc" * w * h)
        (directory / "render.json").write_text(json.dumps({
            "process_succeeded": self.software_succeeds,
            "status": "captured" if self.software_succeeds else "process_failed",
            "exit_status": 0 if self.software_succeeds else 7,
        }))
        return image

    def invoke(self, *arguments):
        def hardware(case, name, *args):
            return {"case": case["id"], "runner": name, "passed": True}
        with patch.object(cli, "build_case", return_value=self.root / "app"), \
             patch.object(cli, "software_render", side_effect=self.software) as software, \
             patch.object(cli, "run_hardware_case", side_effect=hardware) as hardware_mock, \
             patch.object(cli, "preflight_runner") as preflight, \
             patch.object(cli, "fingerprint", return_value=None), \
             patch.object(cli, "source_state", return_value={}), \
             contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            rc = cli.main(["--workload-dir", str(self.root), "--out-dir", str(self.root),
                           *arguments])
        return rc, software, hardware_mock, preflight

    def test_default_runs_all_five_at_16x9_against_spike(self):
        rc, software, hardware, preflight = self.invoke()
        self.assertEqual(rc, 0)
        expected = [case["id"] for case in self.manifest["cases"]]
        self.assertEqual(len(expected), 5)
        self.assertEqual(self.rendered, [(name, 16, 9) for name in expected])
        self.assertEqual([call.args[1] for call in hardware.call_args_list], ["spike"] * 5)
        preflight.assert_called_once()
        for case in self.manifest["cases"]:
            self.assertNotIn("status", case)

    def test_backend_routing_and_software_only_skips_hardware_preflight(self):
        for backend, expected in (("software", []), ("spike", ["spike"]),
                                  ("rtl", ["verilator"]), ("all", ["spike", "verilator"])):
            with self.subTest(backend=backend):
                rc, software, hardware, preflight = self.invoke(
                    "--backend", backend, "--case", "raytracingintersection")
                self.assertEqual(rc, 0)
                software.assert_called_once()
                self.assertEqual([call.args[1] for call in hardware.call_args_list], expected)
                self.assertEqual(preflight.call_count, len(expected))

    def test_software_backend_needs_no_hardware_manifest_configuration(self):
        with patch.object(cli, "hardware_runners", side_effect=AssertionError("hardware used")):
            rc, _, hardware, _ = self.invoke("--backend", "software")
        self.assertEqual(rc, 0)
        hardware.assert_not_called()

    def test_dimensions_and_repeated_case_selection(self):
        rc, _, hardware, _ = self.invoke("--width", "32", "--height", "16",
                                       "--case", "raytracingtextures", "--case", "raytracingshadows")
        self.assertEqual(rc, 0)
        self.assertEqual(self.rendered, [("raytracingtextures", 32, 16), ("raytracingshadows", 32, 16)])
        self.assertEqual(hardware.call_count, 2)

    def test_software_capture_with_process_failure_cannot_pass(self):
        self.software_succeeds = False
        rc, _, _, _ = self.invoke("--backend", "software")
        self.assertEqual(rc, 1)

    def test_software_wrong_image_dimensions_cannot_pass(self):
        self.image_size = (1, 1)
        rc, _, _, _ = self.invoke("--backend", "software")
        self.assertEqual(rc, 1)

    def test_unknown_case_is_rejected_before_render(self):
        rc, software, hardware, _ = self.invoke("--case", "missing")
        self.assertEqual(rc, 2)
        software.assert_not_called()
        hardware.assert_not_called()

    def test_legacy_runner_name_maps_to_rtl(self):
        rc, _, hardware, _ = self.invoke("--hardware-runner", "verilator", "--case", "raytracingshadows")
        self.assertEqual(rc, 0)
        self.assertEqual(hardware.call_args.args[1], "verilator")

    def test_software_rejects_spike_only_options(self):
        for option in ("--spike-commit-log", "--require-rt-release"):
            with self.subTest(option=option):
                rc, software, hardware, _ = self.invoke("--backend", "software", option)
                self.assertEqual(rc, 2)
                software.assert_not_called()
                hardware.assert_not_called()

    def test_invalid_backend_and_conflicting_selectors_are_rejected(self):
        for arguments in (("--backend", "missing"),
                          ("--backend", "rtl", "--hardware-runner", "spike")):
            with self.subTest(arguments=arguments), self.assertRaises(SystemExit) as error:
                self.invoke(*arguments)
            self.assertEqual(error.exception.code, 2)


if __name__ == '__main__':
    unittest.main()
