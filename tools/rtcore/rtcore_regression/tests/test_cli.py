"""Fast checks for the full-app image-regression launch contract."""
import unittest
from pathlib import Path

from tools.rtcore.rtcore_regression.cli import base_app_command


class AppCommandTests(unittest.TestCase):
    def test_image_regression_disables_benchmark_warmup(self):
        for frames in (1, 3):
            self.assertEqual(
                base_app_command(Path('/tmp/raytracingtextures'), 16, 9, frames),
                ['/tmp/raytracingtextures', '--benchmark', '--benchwarmup', '0',
                 '--benchmarkframes', str(frames), '--width', '16', '--height', '9'],
            )


if __name__ == '__main__':
    unittest.main()
