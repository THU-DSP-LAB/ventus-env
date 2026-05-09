import io
import importlib.util
import sys
import unittest
from argparse import Namespace
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock


PACKAGE_NAME = "ventus_regression_test_for_tests"
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


class FakeAsyncResult:
    def __init__(self, result):
        self._result = result

    def ready(self):
        return True

    def get(self):
        return self._result


class FakePool:
    def __init__(self):
        self.submitted = []

    def apply_async(self, func, args):
        job = args[0]
        self.submitted.append(job)
        return FakeAsyncResult(func(job))


def make_job(backend):
    runner = load_module("runner")
    cases = load_module("cases")
    config = runner.BackendRunConfig(backend, backend, {0}, 1)
    return runner.TestJob(config, 0, cases.TEST_CASES[0], 1, 1, 1)


class WorkerThreadTests(unittest.TestCase):
    def test_default_jobs_is_two_thirds_of_available_cpu(self):
        options = load_module("options")
        with mock.patch.object(options, "_available_cpu_count", return_value=12):
            self.assertEqual(options.suggest_default_jobs(), 8)

    def test_worker_threads_for_backends(self):
        runner = load_module("runner")

        self.assertEqual(runner.worker_threads_for_backend("rtlsim-with-cache"), 8)
        self.assertEqual(runner.worker_threads_for_backend("gvm-no-cache"), 8)
        self.assertEqual(runner.worker_threads_for_backend("spike"), 1)
        self.assertEqual(runner.worker_threads_for_backend("cyclesim"), 1)

    def test_scheduler_uses_weighted_worker_thread_budget(self):
        runner = load_module("runner")
        jobs = [make_job("rtlsim-with-cache"), make_job("gvm-no-cache"), make_job("spike")]
        pool = FakePool()
        with mock.patch.object(runner, "run_test_job", self._fake_run_test_job):
            scheduler = runner.WeightedJobScheduler(pool, jobs, worker_thread_budget=9)
            scheduler.submit_ready()

        self.assertEqual(
            [job.backend.env_backend for job in pool.submitted],
            ["rtlsim-with-cache", "spike"],
        )

    def test_scheduler_allows_two_rtl_jobs_with_sixteen_worker_threads(self):
        runner = load_module("runner")
        jobs = [make_job("rtlsim-with-cache"), make_job("gvm-no-cache"), make_job("spike")]
        pool = FakePool()
        with mock.patch.object(runner, "run_test_job", self._fake_run_test_job):
            scheduler = runner.WeightedJobScheduler(pool, jobs, worker_thread_budget=16)
            scheduler.submit_ready()

        self.assertEqual(
            [job.backend.env_backend for job in pool.submitted],
            ["rtlsim-with-cache", "gvm-no-cache"],
        )

    def test_ci_matrix_excludes_gpu_dependent_sbt_backend(self):
        options = load_module("options")

        backends = [backend for backend, _ in options.parse_matrix("ci")]

        self.assertEqual(
            backends,
            ["cycle", "rtl-no-cache", "rtl-with-cache", "gvm-no-cache", "gvm-with-cache"],
        )
        self.assertNotIn("sbt", backends)

    def test_backend_specific_cases_only_run_on_spike_and_sbt(self):
        cases = load_module("cases")
        runner = load_module("runner")
        backend_specific_indices = {
            cases.TEST_CASE_INDEX_BY_NAME["cfd_i1"],
            cases.TEST_CASE_INDEX_BY_NAME["dwt2d_192"],
        }
        selected_indices = list(range(len(cases.TEST_CASES)))
        configs = [
            runner.BackendRunConfig("spike", "spike", set(), 1),
            runner.BackendRunConfig("sbt", "sbt", set(), 1),
            runner.BackendRunConfig("cyclesim", "cyclesim", set(), 1),
        ]

        jobs = runner._build_test_jobs(
            runner._attach_backend_selected_indices(configs, selected_indices),
            selected_indices,
            timeout_scale=1,
        )

        backends_by_case = {
            index: [
                job.backend.env_backend
                for job in jobs
                if job.testcase_index == index
            ]
            for index in backend_specific_indices
        }
        for enabled_backends in backends_by_case.values():
            self.assertCountEqual(enabled_backends, ["spike", "sbt"])

    def test_backend_specific_all_checklist_includes_cases_for_spike_and_sbt_only(self):
        cases = load_module("cases")
        options = load_module("options")
        backend_specific_indices = [
            cases.TEST_CASE_INDEX_BY_NAME["cfd_i1"],
            cases.TEST_CASE_INDEX_BY_NAME["dwt2d_192"],
        ]

        matrix = dict(options.parse_matrix("spike:all,sbtsim:all,cycle:all"))

        for index in backend_specific_indices:
            self.assertIn(index, matrix["spike"])
            self.assertIn(index, matrix["sbtsim"])
            self.assertNotIn(index, matrix["cycle"])

    def test_cli_passes_progress_mode_to_runner(self):
        cli = load_module("cli")
        cases = load_module("cases")
        args = Namespace(repeat=1, numactl="off", progress="ci")
        matrix = [("cycle", {0})]
        captured = {}

        def fake_run_plan(*args, **kwargs):
            captured["progress_mode"] = kwargs["progress_mode"]
            config = args[0][0]
            return [
                (config.name, 0, [0], [None] * len(cases.TEST_CASES), config.checklist, [], config.repeat)
            ], 0

        with mock.patch.object(cli, "run_plan", side_effect=fake_run_plan):
            cli.run_all_modes(args, matrix, True, jobs=1, timeout_scale=1, shared_state={})

        self.assertEqual(captured["progress_mode"], "ci")

    def test_ci_progress_tick_prints_heartbeat_after_five_minutes(self):
        progress = load_module("progress")
        interval = progress.CI_PROGRESS_HEARTBEAT_INTERVAL_SECONDS
        results_by_backend = {"rtlsim-no-cache": [None]}
        started_by_backend = {"rtlsim-no-cache": [[True]]}
        output = io.StringIO()

        with mock.patch.object(progress.time, "monotonic", return_value=0):
            progress_output = progress.CiProgressOutput(total_reps=1)

        with redirect_stdout(output), \
             mock.patch.object(progress.time, "monotonic", return_value=interval - 1):
            progress_output.tick(results_by_backend, started_by_backend)

        self.assertEqual(output.getvalue(), "")

        with redirect_stdout(output), \
             mock.patch.object(progress.time, "monotonic", return_value=interval):
            progress_output.tick(results_by_backend, started_by_backend)

        heartbeat = output.getvalue()
        self.assertIn("[heartbeat] completed=0/1", heartbeat)
        self.assertIn("rtlsim-no-cache:pass=0,fail=0,flaky=0,running=1", heartbeat)

    def test_tqdm_backend_names_are_short_display_labels(self):
        progress = load_module("progress")

        self.assertEqual(progress._format_tqdm_backend_name("rtlsim-with-cache"), "rtl-cache")
        self.assertEqual(progress._format_tqdm_backend_name("rtlsim-no-cache"), "rtl-nocache")
        self.assertEqual(progress._format_tqdm_backend_name("rtlsim-with-cache-gvm"), "rtl-cache-gvm")
        self.assertEqual(progress._format_tqdm_backend_name("rtlsim-no-cache-gvm"), "rtl-nocache-gvm")
        self.assertEqual(progress._format_tqdm_backend_name("spike"), "spike")

    def test_tqdm_bars_use_compact_format(self):
        progress = load_module("progress")
        config = Namespace(name="rtlsim-with-cache", repeat=10)

        with mock.patch.object(progress, "tqdm", return_value=object()) as fake_tqdm:
            progress._create_progress_bars([config], total_reps=10, selected_count=1)

        kwargs = fake_tqdm.call_args.kwargs
        self.assertEqual(kwargs["desc"], "rtl-cache")
        self.assertEqual(kwargs["bar_format"], progress.TQDM_BAR_FORMAT)
        self.assertTrue(kwargs["dynamic_ncols"])

    def test_tqdm_postfix_rolls_flaky_into_fail(self):
        progress = load_module("progress")

        postfix = progress._format_tqdm_status_postfix(
            pass_count=5,
            fail_count=1,
            flaky_count=2,
            running_count=3,
        )

        self.assertEqual(postfix, "ok=5 fail=3 run=3")

    @staticmethod
    def _fake_run_test_job(job):
        runner = load_module("runner")
        return runner.JobResult(job.backend.name, job.testcase_index, job.run_idx, 0, "OK")


if __name__ == "__main__":
    unittest.main()
