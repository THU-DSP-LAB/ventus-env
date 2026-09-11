"""The installed checkout needs no path injection to invoke either tool."""
from pathlib import Path
import subprocess
import sys
import unittest


class EntryTests(unittest.TestCase):
    def test_help_from_outside_repository(self):
        entry = Path(__file__).resolve().parents[2] / "rtcore.py"
        for command in ("abi", "regression"):
            with self.subTest(command=command):
                result = subprocess.run(
                    [sys.executable, str(entry), command, "--help"],
                    cwd="/tmp", capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("usage:", result.stdout)
