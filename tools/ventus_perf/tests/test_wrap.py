import os
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


if __name__ == "__main__":
    unittest.main()
