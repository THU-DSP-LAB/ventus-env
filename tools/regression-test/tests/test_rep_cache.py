import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


PACKAGE_NAME = "ventus_regression_test_cache_tests"
PACKAGE_DIR = Path(__file__).resolve().parents[1]


def load_module(module_name: str):
    if PACKAGE_NAME not in sys.modules:
        spec = importlib.util.spec_from_file_location(
            PACKAGE_NAME,
            PACKAGE_DIR / "__init__.py",
            submodule_search_locations=[str(PACKAGE_DIR)],
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules[PACKAGE_NAME] = module
        spec.loader.exec_module(module)
    return __import__(f"{PACKAGE_NAME}.{module_name}", fromlist=[module_name])


class RepCacheTests(unittest.TestCase):
    def test_run_single_rep_uses_rep_local_pocl_cache(self):
        runner = load_module("runner")
        cases = load_module("cases")
        config = runner.BackendRunConfig("spike", "spike", {0}, 1)
        testcase = cases.TestCase("cache_case", Path("/unused/source"), ["./run"], need_make=True)
        job = runner.TestJob(config, 0, testcase, 1, 1, 1)

        with tempfile.TemporaryDirectory() as tmpdir:
            rep_cwd = Path(tmpdir) / "rep"
            rep_cwd.mkdir()
            seen_envs = []

            def fake_run_command(cmd, cwd, env, log_file, timeout, active_pids):
                seen_envs.append(env.copy())
                return 0

            with mock.patch.object(runner, "_make_rep_cwd", return_value=rep_cwd), \
                 mock.patch.object(runner, "_cleanup_rep_cwd"), \
                 mock.patch.object(runner, "run_command", side_effect=fake_run_command):
                rc, tag = runner._run_single_rep(job, {"VENTUS_BACKEND": "spike"}, {})

        self.assertEqual((rc, tag), (0, runner.TAG_OK))
        self.assertEqual(len(seen_envs), 2)
        for env in seen_envs:
            self.assertEqual(env["POCL_CACHE_DIR"], str(rep_cwd / runner.POCL_CACHE_DIR_NAME))
        self.assertEqual(seen_envs[1]["VENTUS_BACKEND"], "spike")
        self.assertNotIn("VENTUS_BACKEND", seen_envs[0])


if __name__ == "__main__":
    unittest.main()
