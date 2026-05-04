import importlib.util
import sys
import unittest
from pathlib import Path
from unittest import mock


PACKAGE_NAME = "ventus_regression_test_numa_tests"
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


class FakeBar:
    def update(self, value):
        pass

    def set_postfix_str(self, value):
        pass

    def close(self):
        pass


class FakeProgress:
    def job_started(self, *args):
        pass

    def job_completed(self, *args):
        pass

    def tick(self, *args):
        pass


def make_job(backend):
    runner = load_module("runner")
    cases = load_module("cases")
    config = runner.BackendRunConfig(backend, backend, {0}, 1)
    return runner.TestJob(config, 0, cases.TEST_CASES[0], 1, 1, 1)


class NumaTests(unittest.TestCase):
    def test_parse_lscpu_builds_whole_node_binding(self):
        numa = load_module("numa")
        output = "\n".join(
            [
                "# CPU,Core,Socket,Node,Online",
                "0,0,0,0,Y",
                "1,1,0,0,Y",
                "2,2,0,0,Y",
                "3,3,0,0,Y",
                "4,0,0,0,Y",
                "5,1,0,0,Y",
                "6,2,0,0,Y",
                "7,3,0,0,Y",
            ]
        )

        bindings = numa._build_bindings(numa.parse_lscpu(output), 4)

        self.assertEqual(bindings, [numa.NumaBinding(node=0, cpus=(0, 1, 2, 3, 4, 5, 6, 7))])

    def test_build_bindings_filters_nodes_smaller_than_worker_threads(self):
        numa = load_module("numa")
        output = "\n".join(
            [
                "0,0,0,0,Y",
                "1,1,0,0,Y",
                "2,2,0,0,Y",
                "3,3,0,0,Y",
                "4,4,0,1,Y",
                "5,5,0,1,Y",
                "6,6,0,1,Y",
                "7,7,0,1,Y",
                "8,8,0,1,Y",
                "9,9,0,1,Y",
                "10,10,0,1,Y",
                "11,11,0,1,Y",
            ]
        )

        bindings = numa._build_bindings(numa.parse_lscpu(output), 8)

        self.assertEqual(bindings, [numa.NumaBinding(node=1, cpus=(4, 5, 6, 7, 8, 9, 10, 11))])

    def test_allocator_returns_none_when_no_node_has_capacity(self):
        numa = load_module("numa")
        node0 = numa.NumaBinding(node=0, cpus=(0, 1, 2, 3))
        node1 = numa.NumaBinding(node=1, cpus=(4, 5, 6, 7))
        allocator = numa.NumaAllocator([node0, node1])

        self.assertEqual(allocator.allocate(4), node0)
        self.assertEqual(allocator.allocate(4), node1)
        self.assertIsNone(allocator.allocate(4))

    def test_allocator_reuses_node_after_release(self):
        numa = load_module("numa")
        node0 = numa.NumaBinding(node=0, cpus=(0, 1, 2, 3))
        allocator = numa.NumaAllocator([node0])

        binding = allocator.allocate(4)
        allocator.release(binding, 4)

        self.assertEqual(allocator.allocate(4), node0)

    def test_allocator_reports_capacity_without_reserving_it(self):
        numa = load_module("numa")
        node0 = numa.NumaBinding(node=0, cpus=(0, 1, 2, 3))
        allocator = numa.NumaAllocator([node0])

        self.assertTrue(allocator.has_capacity(4))
        self.assertEqual(allocator.allocate(4), node0)
        self.assertFalse(allocator.has_capacity(4))

    def test_wrap_command_prefixes_numactl(self):
        numa = load_module("numa")
        binding = numa.NumaBinding(node=0, cpus=(0, 1, 2, 3))

        command = numa.wrap_command(["./matadd"], binding)

        self.assertEqual(command, ["numactl", "-m", "0", "-C", "0,1,2,3", "--", "./matadd"])

    def test_auto_disables_binding_when_probe_is_not_permitted(self):
        numa = load_module("numa")
        binding = numa.NumaBinding(node=0, cpus=(0, 1, 2, 3))

        with mock.patch.object(numa, "discover_bindings", return_value=[binding]):
            with mock.patch.object(numa, "probe_binding", side_effect=numa.NumaBindingError("setting membind: Operation not permitted")):
                allocator, warning = numa.create_allocator(numa.NUMACTL_AUTO, 4)

        self.assertFalse(allocator.enabled)
        self.assertEqual(
            warning,
            "numactl binding probe failed: setting membind: Operation not permitted",
        )

    def test_require_fails_when_probe_is_not_permitted(self):
        numa = load_module("numa")
        binding = numa.NumaBinding(node=0, cpus=(0, 1, 2, 3))

        with mock.patch.object(numa, "discover_bindings", return_value=[binding]):
            with mock.patch.object(numa, "probe_binding", side_effect=numa.NumaBindingError("setting membind: Operation not permitted")):
                with self.assertRaisesRegex(numa.NumaBindingError, "numactl binding probe failed"):
                    numa.create_allocator(numa.NUMACTL_REQUIRE, 4)

    def test_scheduler_leaves_second_single_node_rtl_job_unbound(self):
        runner = load_module("runner")
        numa = load_module("numa")
        jobs = [make_job("rtlsim-with-cache"), make_job("gvm-no-cache"), make_job("spike")]
        pool = FakePool()
        allocator = numa.NumaAllocator([numa.NumaBinding(node=0, cpus=(0, 1, 2, 3, 4, 5, 6, 7))])

        with mock.patch.object(runner, "run_test_job", self._fake_run_test_job):
            scheduler = runner.WeightedJobScheduler(pool, jobs, worker_thread_budget=16, numa_allocator=allocator)
            scheduler.submit_ready()

        self.assertEqual(
            [job.backend.env_backend for job in pool.submitted],
            ["rtlsim-with-cache", "gvm-no-cache"],
        )
        self.assertEqual(pool.submitted[0].numa_binding, numa.NumaBinding(node=0, cpus=(0, 1, 2, 3, 4, 5, 6, 7)))
        self.assertIsNone(pool.submitted[1].numa_binding)

    def test_scheduler_does_not_overfill_physical_numa_nodes(self):
        runner = load_module("runner")
        numa = load_module("numa")
        jobs = [
            make_job("rtlsim-with-cache"),
            make_job("gvm-no-cache"),
            make_job("rtlsim-no-cache"),
        ]
        pool = FakePool()
        node0 = numa.NumaBinding(node=0, cpus=tuple(range(0, 28, 2)))
        node1 = numa.NumaBinding(node=1, cpus=tuple(range(1, 28, 2)))
        allocator = numa.NumaAllocator([node0, node1])

        with mock.patch.object(runner, "run_test_job", self._fake_run_test_job):
            scheduler = runner.WeightedJobScheduler(pool, jobs, worker_thread_budget=24, numa_allocator=allocator)
            scheduler.submit_ready()

        self.assertEqual(
            [job.backend.env_backend for job in pool.submitted],
            ["rtlsim-with-cache", "gvm-no-cache", "rtlsim-no-cache"],
        )
        self.assertEqual(pool.submitted[0].numa_binding, node0)
        self.assertEqual(pool.submitted[1].numa_binding, node1)
        self.assertIsNone(pool.submitted[2].numa_binding)

    def test_scheduler_requires_numa_capacity_before_submitting_heavy_job(self):
        runner = load_module("runner")
        numa = load_module("numa")
        jobs = [
            make_job("rtlsim-with-cache"),
            make_job("gvm-no-cache"),
            make_job("rtlsim-no-cache"),
        ]
        pool = FakePool()
        node0 = numa.NumaBinding(node=0, cpus=tuple(range(0, 28, 2)))
        node1 = numa.NumaBinding(node=1, cpus=tuple(range(1, 28, 2)))
        allocator = numa.NumaAllocator([node0, node1])

        with mock.patch.object(runner, "run_test_job", self._fake_run_test_job):
            scheduler = runner.WeightedJobScheduler(
                pool,
                jobs,
                worker_thread_budget=24,
                numa_allocator=allocator,
                numactl_policy=numa.NUMACTL_REQUIRE,
            )
            scheduler.submit_ready()
            self.assertEqual(
                [job.backend.env_backend for job in pool.submitted],
                ["rtlsim-with-cache", "gvm-no-cache"],
            )

            self._collect_finished_jobs(runner, scheduler, jobs)
            scheduler.submit_ready()

        self.assertEqual(
            [job.backend.env_backend for job in pool.submitted],
            ["rtlsim-with-cache", "gvm-no-cache", "rtlsim-no-cache"],
        )
        self.assertEqual(pool.submitted[2].numa_binding, node0)

    def test_collect_plan_results_passes_numactl_policy_to_scheduler(self):
        runner = load_module("runner")
        numa = load_module("numa")
        cases = load_module("cases")
        config = runner.BackendRunConfig("spike", "spike", {0}, 1)
        jobs = [runner.TestJob(config, 0, cases.TEST_CASES[0], 1, 1, 1)]
        results_by_backend = {"spike": [None] * len(cases.TEST_CASES)}
        captured_policies = []

        class CapturingScheduler(runner.WeightedJobScheduler):
            def __init__(self, *args, **kwargs):
                captured_policies.append(kwargs["numactl_policy"])
                super().__init__(*args, **kwargs)

        with mock.patch.object(runner, "_pool", FakePool()), \
             mock.patch.object(runner, "create_progress_output", return_value=FakeProgress()), \
             mock.patch.object(runner, "close_progress_output"), \
             mock.patch.object(runner, "_drain_job_events"), \
             mock.patch.object(runner, "run_test_job", self._fake_run_test_job), \
             mock.patch.object(runner, "WeightedJobScheduler", CapturingScheduler):
            runner._collect_plan_results(
                [config],
                jobs,
                results_by_backend,
                selected_count=1,
                worker_thread_budget=1,
                numa_allocator=numa.NumaAllocator([]),
                numactl_policy=numa.NUMACTL_REQUIRE,
                progress_mode=runner.PROGRESS_TQDM,
            )

        self.assertEqual(captured_policies, [numa.NUMACTL_REQUIRE])

    @staticmethod
    def _fake_run_test_job(job):
        runner = load_module("runner")
        return runner.JobResult(job.backend.name, job.testcase_index, job.run_idx, 0, "OK")

    @staticmethod
    def _collect_finished_jobs(runner, scheduler, jobs):
        backend_names = {job.backend.name for job in jobs}
        results_by_backend = {name: [None] for name in backend_names}
        repeat_by_backend = {name: 1 for name in backend_names}
        started_by_backend = {name: [[True]] for name in backend_names}
        scheduler.collect_ready_results(results_by_backend, repeat_by_backend, started_by_backend, FakeProgress())


if __name__ == "__main__":
    unittest.main()
