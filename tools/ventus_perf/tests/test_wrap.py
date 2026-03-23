import json
import os
import sys
import unittest
import pathlib
import shutil
import subprocess
import tempfile

from ventus_perf import wrap as ventus_perf_wrap


class ChildContextTests(unittest.TestCase):
    def test_build_perf_env_defaults_to_default_detail(self) -> None:
        env = ventus_perf_wrap.build_perf_env(
            {"PATH": os.environ["PATH"], "VENTUS_BACKEND": "ptx"},
            experiment_id="exp-test",
            pass_id="measure-0001",
            pass_type="measure",
            pass_dir=pathlib.Path("/tmp/ventus-pass"),
        )
        self.assertEqual(env["VENTUS_PERF_DETAIL"], "default")

    def test_phase1_rejects_unsupported_backend(self) -> None:
        with self.assertRaisesRegex(ValueError, "unsupported backend"):
            ventus_perf_wrap.validate_phase1_backend("spike")

    def test_finalize_pass_classifies_signaled_exit(self) -> None:
        manifest = ventus_perf_wrap.finalize_pass_manifest(
            pass_dir="/tmp/ventus-pass",
            pass_id="measure-0001",
            pass_type="measure",
            returncode=-9,
            stdout_path="stdout.log",
            stderr_path="stderr.log",
            event_files=["events.vt.jsonl"],
        )
        self.assertEqual(manifest["state"], "signaled")
        self.assertEqual(manifest["term_signal"], 9)
        self.assertEqual(manifest["stdout_path"], "stdout.log")

    def test_run_pass_writes_begin_and_final_manifests(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            pass_dir = pathlib.Path(tmpdir) / "measure-0001"
            manifest = ventus_perf_wrap.run_pass(
                experiment_id="exp-test",
                pass_id="measure-0001",
                pass_type="measure",
                command=["python3", "-c", "print('hello from child')"],
                pass_dir=pass_dir,
                env={
                    "PATH": os.environ["PATH"],
                    "VENTUS_BACKEND": "ptx",
                },
            )
            self.assertEqual(manifest["state"], "completed")
            self.assertTrue((pass_dir / "pass.begin.json").exists())
            self.assertTrue((pass_dir / "pass.json").exists())
            self.assertIn("stdout.log", manifest["stdout_path"])
            self.assertEqual(manifest["env_summary"]["VENTUS_PERF_DETAIL"], "default")

    def test_build_runtime_env_bootstraps_ventus_defaults(self) -> None:
        env = ventus_perf_wrap.build_runtime_env({"PATH": "/usr/bin"})
        repo_root = pathlib.Path(__file__).resolve().parents[3]
        install_prefix = repo_root / "install"
        self.assertEqual(env["VENTUS_INSTALL_PREFIX"], str(install_prefix))
        self.assertEqual(env["POCL_DEVICES"], "ventus")
        self.assertEqual(env["POCL_ENABLE_UNINIT"], "1")
        self.assertEqual(env["OCL_ICD_VENDORS"], str(install_prefix / "lib" / "libpocl.so"))
        self.assertTrue(env["PATH"].startswith(str(install_prefix / "bin")))
        self.assertTrue(env["LD_LIBRARY_PATH"].startswith(str(install_prefix / "lib")))


class CliTests(unittest.TestCase):
    def test_report_writes_summary_and_json_views(self) -> None:
        fixture = pathlib.Path(__file__).resolve().parent / "fixtures" / "minimal_experiment"
        with tempfile.TemporaryDirectory() as tmpdir:
            work_dir = pathlib.Path(tmpdir) / "experiment"
            shutil.copytree(fixture, work_dir)
            proc = subprocess.run(
                ["python3", "tools/ventus_perf.py", "report", str(work_dir)],
                cwd=pathlib.Path(__file__).resolve().parents[3],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertTrue((work_dir / "reports" / "perfetto.json").exists())
        self.assertEqual(proc.returncode, 0, msg=proc.stdout + proc.stderr)
        self.assertIn("Baseline Attribution", proc.stdout)

    def test_report_writes_profiler_json_for_profiler_experiment(self) -> None:
        fixture = pathlib.Path(__file__).resolve().parent / "fixtures" / "profiler_experiment"
        with tempfile.TemporaryDirectory() as tmpdir:
            work_dir = pathlib.Path(tmpdir) / "experiment"
            shutil.copytree(fixture, work_dir)
            proc = subprocess.run(
                ["python3", "tools/ventus_perf.py", "report", str(work_dir)],
                cwd=pathlib.Path(__file__).resolve().parents[3],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertTrue((work_dir / "reports" / "profiler.json").exists())
        self.assertEqual(proc.returncode, 0, msg=proc.stdout + proc.stderr)

    def test_run_rejects_ncu_kernel_without_ncu_profile(self) -> None:
        proc = subprocess.run(
            [
                "python3",
                "tools/ventus_perf.py",
                "run",
                "--ncu-kernel",
                "matadd",
                "--",
                "python3",
                "-c",
                "print('wrapper child ok')",
            ],
            cwd=pathlib.Path(__file__).resolve().parents[3],
            check=False,
            capture_output=True,
            text=True,
            env={**os.environ, "VENTUS_BACKEND": "ptx"},
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("--ncu-kernel requires --profile ncu", proc.stderr)

    def test_run_executes_child_command_via_wrapper(self) -> None:
        proc = subprocess.run(
            [
                "python3",
                "tools/ventus_perf.py",
                "run",
                "--repeat",
                "1",
                "--",
                "python3",
                "-c",
                "print('wrapper child ok')",
            ],
            cwd=pathlib.Path(__file__).resolve().parents[3],
            check=False,
            capture_output=True,
            text=True,
            env={**os.environ, "VENTUS_BACKEND": "ptx"},
        )
        self.assertEqual(proc.returncode, 0, msg=proc.stdout + proc.stderr)
        self.assertIn("Baseline Attribution", proc.stdout)


class ProfilerPassTests(unittest.TestCase):
    def test_run_experiment_appends_profiler_passes_after_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            output_root = pathlib.Path(tmpdir) / "out"
            _, manifest = ventus_perf_wrap.run_experiment(
                command=[sys.executable, "-c", "print('wrapper child ok')"],
                warmup=1,
                repeat=1,
                profiles=["nsys", "ncu"],
                ncu_kernel="matadd",
                env={
                    "PATH": os.environ["PATH"],
                    "VENTUS_BACKEND": "ptx",
                    "VENTUS_INSTALL_PREFIX": tmpdir,
                },
                output_root=output_root,
            )
            self.assertEqual(
                manifest["actual_passes"],
                [
                    "passes/warmup-0001",
                    "passes/measure-0001",
                    "passes/nsys-0001",
                    "passes/ncu-0001",
                ],
            )
            self.assertEqual(manifest["passes"][2]["pass_type"], "nsys")
            self.assertEqual(
                manifest["passes"][2]["target_argv"],
                [sys.executable, "-c", "print('wrapper child ok')"],
            )
            self.assertEqual(manifest["passes"][3]["target_pass_id"], "measure-0001")
            self.assertEqual(
                manifest["passes"][3]["profile_target"]["kernel_name"],
                "matadd",
            )

    def test_run_experiment_rejects_profiler_on_non_ptx_backend(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            with self.assertRaisesRegex(ValueError, "profiler passes require a supported phase1 backend"):
                ventus_perf_wrap.run_experiment(
                    command=[sys.executable, "-c", "print('wrapper child ok')"],
                    warmup=0,
                    repeat=1,
                    profiles=["nsys"],
                    ncu_kernel=None,
                    env={
                        "PATH": os.environ["PATH"],
                        "VENTUS_BACKEND": "spike",
                        "VENTUS_INSTALL_PREFIX": tmpdir,
                    },
                    output_root=pathlib.Path(tmpdir) / "out",
                )

    def test_run_experiment_records_failed_profiler_pass_when_tool_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            install_prefix = pathlib.Path(tmpdir) / "install"
            install_prefix.mkdir()
            _, manifest = ventus_perf_wrap.run_experiment(
                command=[sys.executable, "-c", "print('wrapper child ok')"],
                warmup=0,
                repeat=1,
                profiles=["nsys"],
                ncu_kernel=None,
                env={
                    "PATH": "",
                    "VENTUS_BACKEND": "ptx",
                    "VENTUS_INSTALL_PREFIX": str(install_prefix),
                },
                output_root=pathlib.Path(tmpdir) / "out",
            )
            self.assertEqual(manifest["passes"][0]["state"], "completed")
            self.assertEqual(manifest["passes"][1]["pass_type"], "nsys")
            self.assertEqual(manifest["passes"][1]["state"], "failed")
            self.assertTrue(manifest["passes"][1]["recorder_errors"])

    def test_run_experiment_extracts_nsys_summary_json_from_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            fake_bin = pathlib.Path(tmpdir) / "bin"
            fake_bin.mkdir()
            fake_nsys = fake_bin / "nsys"
            fake_nsys.write_text(
                "\n".join(
                    [
                        "#!/usr/bin/env python3",
                        "import json",
                        "import pathlib",
                        "import subprocess",
                        "import sys",
                        "",
                        "mode = sys.argv[1]",
                        "if mode == 'profile':",
                        "    output = pathlib.Path(sys.argv[sys.argv.index('--output') + 1])",
                        "    output.parent.mkdir(parents=True, exist_ok=True)",
                        "    output.with_suffix('.nsys-rep').write_text('fake report\\n', encoding='utf-8')",
                        "    command = sys.argv[sys.argv.index('--output') + 2:]",
                        "    raise SystemExit(subprocess.run(command, check=False).returncode)",
                        "if mode == 'stats':",
                        "    sys.stdout.write(json.dumps([{'Name': 'matadd', 'Total Time (ns)': 123456}]))",
                        "    raise SystemExit(0)",
                        "raise SystemExit(2)",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            fake_nsys.chmod(0o755)
            output_root = pathlib.Path(tmpdir) / "out"

            experiment_dir, manifest = ventus_perf_wrap.run_experiment(
                command=[sys.executable, "-c", "print('wrapper child ok')"],
                warmup=0,
                repeat=1,
                profiles=["nsys"],
                ncu_kernel=None,
                env={
                    "PATH": f"{fake_bin}:{os.environ['PATH']}",
                    "VENTUS_BACKEND": "ptx",
                    "VENTUS_INSTALL_PREFIX": tmpdir,
                },
                output_root=output_root,
            )

            self.assertEqual(manifest["passes"][1]["state"], "completed")
            summary_path = experiment_dir / "passes" / "nsys-0001" / "artifacts" / "nsys" / "summary.json"
            self.assertTrue(summary_path.exists())
            payload = json.loads(summary_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["tool"], "nsys")
            self.assertEqual(
                payload["top_kernels"],
                [{"kernel_name": "matadd", "gpu_time_ns": 123456}],
            )

    def test_run_experiment_fails_when_nsys_summary_extraction_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            fake_bin = pathlib.Path(tmpdir) / "bin"
            fake_bin.mkdir()
            fake_nsys = fake_bin / "nsys"
            fake_nsys.write_text(
                "\n".join(
                    [
                        "#!/usr/bin/env python3",
                        "import pathlib",
                        "import subprocess",
                        "import sys",
                        "",
                        "mode = sys.argv[1]",
                        "if mode == 'profile':",
                        "    output = pathlib.Path(sys.argv[sys.argv.index('--output') + 1])",
                        "    output.parent.mkdir(parents=True, exist_ok=True)",
                        "    output.with_suffix('.nsys-rep').write_text('fake report\\n', encoding='utf-8')",
                        "    command = sys.argv[sys.argv.index('--output') + 2:]",
                        "    raise SystemExit(subprocess.run(command, check=False).returncode)",
                        "if mode == 'stats':",
                        "    sys.stderr.write('stats failed\\n')",
                        "    raise SystemExit(7)",
                        "raise SystemExit(2)",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            fake_nsys.chmod(0o755)

            _, manifest = ventus_perf_wrap.run_experiment(
                command=[sys.executable, "-c", "print('wrapper child ok')"],
                warmup=0,
                repeat=1,
                profiles=["nsys"],
                ncu_kernel=None,
                env={
                    "PATH": f"{fake_bin}:{os.environ['PATH']}",
                    "VENTUS_BACKEND": "ptx",
                    "VENTUS_INSTALL_PREFIX": tmpdir,
                },
                output_root=pathlib.Path(tmpdir) / "out",
            )

            self.assertEqual(manifest["passes"][1]["pass_type"], "nsys")
            self.assertEqual(manifest["passes"][1]["state"], "failed")
            self.assertIn("nsys summary extraction failed", manifest["passes"][1]["recorder_errors"][0])
            self.assertEqual(manifest["state"], "failed")


if __name__ == "__main__":
    unittest.main()
