import json
import pathlib
import subprocess
import tempfile
import unittest

from ventus_perf import report as ventus_perf_report


FIXTURE_ROOT = pathlib.Path(__file__).resolve().parent / "fixtures"
MINIMAL_FIXTURE = FIXTURE_ROOT / "minimal_experiment"
INCOMPLETE_FIXTURE = FIXTURE_ROOT / "incomplete_experiment"
OVERLAP_FIXTURE = FIXTURE_ROOT / "overlap_case"
MULTI_BACKEND_FIXTURE = FIXTURE_ROOT / "multi_backend_experiment"
PROFILER_FIXTURE = FIXTURE_ROOT / "profiler_experiment"
PROFILER_FAILURE_FIXTURE = FIXTURE_ROOT / "profiler_failure_experiment"


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

    def test_summary_text_includes_profiler_reference_section(self) -> None:
        report = ventus_perf_report.load_input_report(PROFILER_FIXTURE)

        summary_text = ventus_perf_report.render_summary_text(report)

        self.assertIn("Profiler Reference", summary_text)
        self.assertIn("nsys: completed", summary_text)
        self.assertIn("matadd 123.456 us", summary_text)

    def test_report_normalizes_multi_backend_memcpy_events(self) -> None:
        report = ventus_perf_report.load_input_report(MULTI_BACKEND_FIXTURE)

        self.assertEqual(report["measured_pass_count"], 3)
        self.assertGreater(report["summary"]["buckets"]["memcpy_h2d"], 0)
        self.assertGreater(report["summary"]["buckets"]["memcpy_d2h"], 0)
        self.assertNotIn("sim_time_ns", report["summary"]["buckets"])
        self.assertNotIn("step_count", report["summary"]["buckets"])
        self.assertIn("auxiliary_facts", report["summary"])
        self.assertIn("sim_time_ns", report["summary"]["auxiliary_facts"])
        self.assertIn("step_count", report["summary"]["auxiliary_facts"])

    def test_profiler_passes_are_exposed_without_polluting_baseline_summary(self) -> None:
        report = ventus_perf_report.load_input_report(PROFILER_FIXTURE)

        self.assertEqual(report["measured_pass_count"], 1)
        self.assertIn("profiler", report)
        self.assertEqual(
            [entry["pass_type"] for entry in report["profiler"]["passes"]],
            ["nsys", "ncu"],
        )
        self.assertEqual(
            report["profiler"]["passes"][1]["profile_target"]["kernel_name"],
            "matadd",
        )
        self.assertEqual({event["pass_type"] for event in report["timeline"]}, {"measure"})
        self.assertEqual({kernel["pass_id"] for kernel in report["kernels"]}, {"measure-0001"})
        self.assertIn("profiler_reference", report["summary"])
        self.assertIn("nsys", report["summary"]["profiler_reference"])
        nsys_reference = report["summary"]["profiler_reference"]["nsys"]
        self.assertEqual(nsys_reference["pass_id"], "nsys-0001")
        self.assertEqual(nsys_reference["state"], "completed")
        self.assertTrue(nsys_reference["artifact_exists"])
        self.assertEqual(
            nsys_reference["top_kernels"],
            [{"kernel_name": "matadd", "gpu_time_ns": 123456}],
        )
        self.assertNotIn("profiler_reference", report["profiler"])

    def test_profiler_failures_and_missing_artifacts_remain_explicit(self) -> None:
        report = ventus_perf_report.load_input_report(PROFILER_FAILURE_FIXTURE)

        self.assertIn("profiler", report)
        self.assertEqual(report["profiler"]["passes"][0]["state"], "failed")
        self.assertFalse(report["profiler"]["passes"][0]["artifacts"][0]["exists"])
        self.assertIn("profiler_reference", report["summary"])
        self.assertEqual(report["summary"]["profiler_reference"]["nsys"]["state"], "failed")
        self.assertFalse(report["summary"]["profiler_reference"]["nsys"]["artifact_exists"])
        self.assertEqual(report["summary"]["profiler_reference"]["nsys"]["top_kernels"], [])
        self.assertIn("error", report["summary"]["profiler_reference"]["nsys"])

    def test_profiler_passes_do_not_pollute_baseline_without_completed_measure_passes(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            experiment_dir = pathlib.Path(tmpdir) / "experiment"
            measure_dir = experiment_dir / "passes" / "measure-0001"
            nsys_dir = experiment_dir / "passes" / "nsys-0001"
            measure_dir.mkdir(parents=True)
            (nsys_dir / "artifacts" / "nsys").mkdir(parents=True)
            (experiment_dir / "experiment.json").write_text(
                json.dumps(
                    {
                        "experiment_id": "exp-no-measure-success",
                        "state": "failed",
                        "actual_passes": ["passes/measure-0001", "passes/nsys-0001"],
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            (measure_dir / "pass.json").write_text(
                json.dumps(
                    {
                        "pass_id": "measure-0001",
                        "pass_type": "measure",
                        "state": "failed",
                        "exit_code": 1,
                        "event_files": ["events.vt.jsonl"],
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            (measure_dir / "events.vt.jsonl").write_text(
                json.dumps(
                    {
                        "stream": "vt",
                        "event_type": "vt_start",
                        "ts_start_ns": 0,
                        "ts_end_ns": 10,
                        "launch_seq": 1,
                        "kernel_name": "measure_kernel",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            (nsys_dir / "pass.json").write_text(
                json.dumps(
                    {
                        "pass_id": "nsys-0001",
                        "pass_type": "nsys",
                        "state": "completed",
                        "exit_code": 0,
                        "event_files": ["events.vt.jsonl"],
                        "artifacts": [{"kind": "nsys_summary", "path": "artifacts/nsys/summary.json"}],
                        "recorder_errors": [],
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            (nsys_dir / "events.vt.jsonl").write_text(
                json.dumps(
                    {
                        "stream": "vt",
                        "event_type": "vt_start",
                        "ts_start_ns": 100,
                        "ts_end_ns": 110,
                        "launch_seq": 1,
                        "kernel_name": "profiler_kernel",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            (nsys_dir / "artifacts" / "nsys" / "summary.json").write_text(
                json.dumps(
                    {
                        "tool": "nsys",
                        "top_kernels": [{"kernel_name": "profiler_kernel", "gpu_time_ns": 110}],
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            report = ventus_perf_report.load_input_report(experiment_dir)

        self.assertEqual(report["measured_pass_count"], 0)
        self.assertEqual({event["pass_type"] for event in report["timeline"]}, {"measure"})
        self.assertEqual({kernel["pass_id"] for kernel in report["kernels"]}, {"measure-0001"})
        self.assertEqual(
            {
                event["args"]["pass_type"]
                for event in report["perfetto"]["traceEvents"]
                if event.get("ph") == "X"
            },
            {"measure"},
        )
        self.assertIn("profiler", report)
        self.assertIn("profiler_reference", report["summary"])
        self.assertEqual(
            report["summary"]["profiler_reference"]["nsys"]["top_kernels"],
            [{"kernel_name": "profiler_kernel", "gpu_time_ns": 110}],
        )

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

    def test_write_report_outputs_also_writes_perfetto_trace(self) -> None:
        report = ventus_perf_report.load_input_report(MINIMAL_FIXTURE)

        with tempfile.TemporaryDirectory() as tmpdir:
            work_dir = pathlib.Path(tmpdir) / "experiment"
            work_dir.mkdir()

            output_dir = ventus_perf_report.write_report_outputs(work_dir, report)

            perfetto_path = output_dir / "perfetto.json"
            self.assertTrue(perfetto_path.exists())
            payload = pathlib.Path(perfetto_path).read_text(encoding="utf-8")
            trace = json.loads(payload)
            self.assertIn("traceEvents", trace)
            self.assertGreater(len(trace["traceEvents"]), 0)
            complete_events = [event for event in trace["traceEvents"] if event.get("ph") == "X"]
            metadata_events = [event for event in trace["traceEvents"] if event.get("ph") == "M"]
            self.assertGreater(len(complete_events), 0)
            self.assertGreater(len(metadata_events), 0)

    def test_write_report_outputs_writes_profiler_json_when_profiler_passes_exist(self) -> None:
        report = ventus_perf_report.load_input_report(PROFILER_FIXTURE)

        with tempfile.TemporaryDirectory() as tmpdir:
            work_dir = pathlib.Path(tmpdir) / "experiment"
            work_dir.mkdir()

            output_dir = ventus_perf_report.write_report_outputs(work_dir, report)

            profiler_path = output_dir / "profiler.json"
            self.assertTrue(profiler_path.exists())
            payload = json.loads(profiler_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["passes"][0]["pass_type"], "nsys")

    def test_write_report_outputs_skips_profiler_json_without_profiler_passes(self) -> None:
        report = ventus_perf_report.load_input_report(MINIMAL_FIXTURE)

        with tempfile.TemporaryDirectory() as tmpdir:
            work_dir = pathlib.Path(tmpdir) / "experiment"
            work_dir.mkdir()

            output_dir = ventus_perf_report.write_report_outputs(work_dir, report)

            self.assertFalse((output_dir / "profiler.json").exists())

    def test_spike_style_pass_without_kernel_wait_is_still_reportable(self) -> None:
        summary = ventus_perf_report._build_pass_summary(
            {
                "pass_id": "measure-0003",
                "start_mono_ns": 0,
                "end_mono_ns": 100,
            },
            [
                {"event_type": "copy_to_dev", "ts_start_ns": 0, "ts_end_ns": 8, "launch_seq": 0},
                {"event_type": "run_total", "ts_start_ns": 10, "ts_end_ns": 90, "launch_seq": 1},
                {"event_type": "copy_from_dev", "ts_start_ns": 50, "ts_end_ns": 57, "launch_seq": 0},
            ],
        )

        self.assertEqual(summary["buckets"]["kernel_wait"], 0)
        self.assertGreater(summary["buckets"]["uncategorized"], 0)


if __name__ == "__main__":
    unittest.main()
