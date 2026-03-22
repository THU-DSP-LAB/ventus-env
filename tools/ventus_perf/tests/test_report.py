import pathlib
import subprocess
import unittest

from ventus_perf import report as ventus_perf_report


FIXTURE_ROOT = pathlib.Path(__file__).resolve().parent / "fixtures"
MINIMAL_FIXTURE = FIXTURE_ROOT / "minimal_experiment"
INCOMPLETE_FIXTURE = FIXTURE_ROOT / "incomplete_experiment"
OVERLAP_FIXTURE = FIXTURE_ROOT / "overlap_case"


class SummaryViewTests(unittest.TestCase):
    def test_summary_closes_top_level_buckets_for_single_measure_pass(self) -> None:
        report = ventus_perf_report.load_input_report(MINIMAL_FIXTURE)
        self.assertFalse(report["concurrency_detected"])
        self.assertEqual(report["measured_pass_count"], 1)
        self.assertAlmostEqual(
            report["summary"]["top_level_total_ns"],
            report["summary"]["wall_time_ns"],
            delta=1_000_000,
        )
        self.assertIn("kernel_wait", report["summary"]["buckets"])
        self.assertIn("kernel_launch", report["summary"]["sub_buckets"])
        self.assertEqual(
            report["summary"]["wall_time_stats"]["mean_ns"],
            report["summary"]["wall_time_ns"],
        )
        self.assertEqual(
            report["summary"]["wall_time_stats"]["min_ns"],
            report["summary"]["wall_time_ns"],
        )
        self.assertEqual(
            report["summary"]["wall_time_stats"]["max_ns"],
            report["summary"]["wall_time_ns"],
        )

    def test_report_expands_uncategorized_when_overlap_detected(self) -> None:
        report = ventus_perf_report.load_input_report(OVERLAP_FIXTURE)
        self.assertTrue(report["concurrency_detected"])
        self.assertGreater(report["summary"]["buckets"]["uncategorized"], 0)

    def test_incomplete_experiment_is_still_reportable(self) -> None:
        report = ventus_perf_report.load_input_report(INCOMPLETE_FIXTURE)
        self.assertTrue(report["best_effort"])
        self.assertEqual(report["state"], "incomplete")

    def test_failed_or_incomplete_measured_passes_are_not_silently_dropped(self) -> None:
        report = ventus_perf_report.load_input_report(INCOMPLETE_FIXTURE)
        self.assertGreaterEqual(len(report["passes"]), 1)

    def test_kernel_view_groups_events_by_launch_sequence(self) -> None:
        report = ventus_perf_report.load_input_report(MINIMAL_FIXTURE)
        self.assertEqual(len(report["kernels"]), 1)
        self.assertEqual(report["kernels"][0]["launch_seq"], 1)
        self.assertEqual(report["kernels"][0]["kernel_name"], "demo_kernel")

    def test_kernel_launch_bucket_absorbs_submit_side_host_work(self) -> None:
        summary = ventus_perf_report._build_pass_summary(
            {
                "pass_id": "measure-0001",
                "start_mono_ns": 0,
                "end_mono_ns": 100,
            },
            [
                {"event_type": "kernel_submit", "ts_start_ns": 20, "ts_end_ns": 90, "launch_seq": 1},
                {"event_type": "kernel_arg_pack", "ts_start_ns": 20, "ts_end_ns": 40, "launch_seq": 1},
                {"event_type": "kernel_arg_upload", "ts_start_ns": 40, "ts_end_ns": 60, "launch_seq": 1},
                {"event_type": "vt_start", "ts_start_ns": 60, "ts_end_ns": 70, "launch_seq": 1},
                {"event_type": "kernel_wait", "ts_start_ns": 70, "ts_end_ns": 100, "launch_seq": 1},
            ],
        )

        self.assertEqual(summary["buckets"]["kernel_launch"], 50)
        self.assertEqual(summary["buckets"]["kernel_wait"], 30)
        self.assertEqual(summary["buckets"]["uncategorized"], 20)
        self.assertEqual(summary["sub_buckets"]["kernel_launch"]["kernel_submit"], 70)

    def test_summary_text_renders_human_readable_durations(self) -> None:
        report = ventus_perf_report.load_input_report(MINIMAL_FIXTURE)

        summary_text = ventus_perf_report.render_summary_text(report)

        self.assertIn("wall_time: 100.000 ms", summary_text)
        self.assertIn("memcpy_h2d: 10.000 ms", summary_text)
        self.assertIn("kernel_launch: 32.000 ms", summary_text)
        self.assertIn("kernel_wait: 33.000 ms", summary_text)
        self.assertIn("memcpy_d2h: 5.000 ms", summary_text)
        self.assertIn("uncategorized: 20.000 ms", summary_text)
        self.assertNotIn("wall_time_ns:", summary_text)

    def test_repo_root_invocation_can_import_report_module(self) -> None:
        proc = subprocess.run(
            [
                "python3",
                "-c",
                "from ventus_perf import report; print(report.__name__)",
            ],
            cwd=pathlib.Path(__file__).resolve().parents[3],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 0, msg=proc.stdout + proc.stderr)
        self.assertIn("ventus_perf.report", proc.stdout)


if __name__ == "__main__":
    unittest.main()
