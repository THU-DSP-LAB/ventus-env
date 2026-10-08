"""Exact-bin statistics and presentation helpers; no plotting dependency needed."""
import unittest

from ..pmu_report import STATES, describe, histogram, table


class StatisticsTests(unittest.TestCase):
    def test_continuation_state_matches_scheduler(self):
        self.assertEqual(len(STATES), 16)
        self.assertEqual(STATES[15], "ContinuationInit")

    def test_numeric_order_not_lexicographic(self):
        self.assertEqual(histogram({"10": 2, "2": 3, "0": 0}), [(2, 3), (10, 2)])

    def test_weighted_mean_and_nearest_rank(self):
        self.assertEqual(describe({"1": 95, "101": 5}), (100, 6.0, 1, 1, 101, 101))

    def test_no_samples_are_not_zero_latency(self):
        self.assertIsNone(describe({}))
        self.assertIsNone(describe({"0": 0}))

    def test_single_sample(self):
        self.assertEqual(describe({"7": 1}), (1, 7.0, 7, 7, 7, 7))

    def test_negative_rejected(self):
        with self.assertRaises(ValueError):
            histogram({"2": -1})

    def test_missing_table_cell(self):
        self.assertIn("—", table(["value"], [[None]]))
