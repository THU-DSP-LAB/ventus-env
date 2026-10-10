"""Fast checks for the full-app image-regression launch contract."""
import unittest
from pathlib import Path

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


if __name__ == '__main__':
    unittest.main()
