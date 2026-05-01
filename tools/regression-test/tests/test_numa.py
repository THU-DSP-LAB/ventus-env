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

    def test_allocator_reuses_nodes_round_robin(self):
        numa = load_module("numa")
        node0 = numa.NumaBinding(node=0, cpus=(0, 1, 2, 3))
        node1 = numa.NumaBinding(node=1, cpus=(4, 5, 6, 7))
        allocator = numa.NumaAllocator([node0, node1])

        self.assertEqual(allocator.allocate(), node0)
        self.assertEqual(allocator.allocate(), node1)
        self.assertEqual(allocator.allocate(), node0)

    def test_wrap_command_prefixes_numactl(self):
        numa = load_module("numa")
        binding = numa.NumaBinding(node=0, cpus=(0, 1, 2, 3))

        command = numa.wrap_command(["./matadd"], binding)

        self.assertEqual(command, ["numactl", "-m", "0", "-C", "0,1,2,3", "--", "./matadd"])

    def test_scheduler_reuses_numa_nodes_without_blocking_second_rtl_job(self):
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
        self.assertEqual(pool.submitted[1].numa_binding, numa.NumaBinding(node=0, cpus=(0, 1, 2, 3, 4, 5, 6, 7)))

    @staticmethod
    def _fake_run_test_job(job):
        runner = load_module("runner")
        return runner.JobResult(job.backend.name, job.testcase_index, job.run_idx, 0, "OK")


if __name__ == "__main__":
    unittest.main()
