"""Run with python3 -m unittest tools.rtcore.rtcore_regression.test_perf."""
import json
from pathlib import Path
import tempfile
import unittest

from .perf import COUNTERS, load_snapshot, summarize


def snapshot(*sms):
    return {
        "schema_version": 2,
        "simulator": "verilator-rtl",
        "clock_cycles": 99,
        "sm": [{"id": index, "rtcore": sm} for index, sm in enumerate(sms)],
    }


class PerfTest(unittest.TestCase):
    def test_sums_all_sm_counters(self):
        first = {name: 1 for name in COUNTERS}
        second = {name: 2 for name in COUNTERS}
        report = summarize(snapshot(first, second))
        self.assertEqual(report["sm_count"], 2)
        self.assertEqual(report["counters"]["box4_issued"], 3)
        self.assertEqual(report["derived"]["active_lanes_per_traverse"], 1.0)

    def test_rejects_bad_schema_and_missing_counter(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "perf.json"
            path.write_text(json.dumps({"schema_version": 1}), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "schema_version=2"):
                load_snapshot(path)
        incomplete = {name: 0 for name in COUNTERS if name != "stack_pops"}
        with self.assertRaisesRegex(ValueError, "stack_pops"):
            summarize(snapshot(incomplete))
