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
    def test_preserves_optional_extended_metrics(self):
        document = snapshot({name: 0 for name in COUNTERS})
        self.assertIsNone(summarize(document)["extended"])
        document["extended"] = {"schema_version": 1, "mode": "basic", "sm": [{"id": 0}]}
        self.assertEqual(summarize(document)["extended"], document["extended"])

    def test_detailed_cross_checks_host_sampler(self):
        document = snapshot({name: 0 for name in COUNTERS})
        detail = {"statistics": {"counters": {}}, "units": [
            {"kind": kind, "index": index, "statistics": {"counters": {}}}
            for kind, count in (("box4", 8), ("triangle", 2), ("resolve", 1))
            for index in range(count)]}
        document["extended"] = {"schema_version": 1, "mode": "detailed",
                                "sm": [{"id": 0, "rt_detailed": detail}]}
        summarize(document)
        detail["as_policy"] = "retained_reverse_replace"
        detail["statistics"]["counters"]["as_evictions"] = 1
        with self.assertRaisesRegex(ValueError, "as_evictions disagrees"):
            summarize(document)
        document["sm"][0]["rtcore"]["as_cache_evictions"] = 1
        summarize(document)
        detail["statistics"]["counters"]["unmatched_evictions"] = 1
        with self.assertRaisesRegex(ValueError, "unmatched_evictions"):
            summarize(document)
        detail["statistics"]["counters"]["unmatched_evictions"] = 0
        detail["units"][0]["statistics"]["counters"]["issues"] = 1
        with self.assertRaisesRegex(ValueError, "disagrees with RTL"):
            summarize(document)

    def test_sums_all_sm_counters(self):
        first = {name: 1 for name in COUNTERS}
        second = {name: 2 for name in COUNTERS}
        report = summarize(snapshot(first, second))
        self.assertEqual(report["sm_count"], 2)
        self.assertEqual(report["counters"]["box4_issued"], 3)
        self.assertEqual(report["derived"]["active_lanes_per_traverse"], 1.0)
        self.assertEqual(report["derived"]["as_cache_average_waiter_uses"], 1.0)

    def test_rejects_bad_schema_and_missing_counter(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "perf.json"
            path.write_text(json.dumps({"schema_version": 1}), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "schema_version=2"):
                load_snapshot(path)
        incomplete = {name: 0 for name in COUNTERS if name != "stack_pops"}
        with self.assertRaisesRegex(ValueError, "stack_pops"):
            summarize(snapshot(incomplete))
