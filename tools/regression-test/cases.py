from dataclasses import dataclass
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parents[2]
LOG_DIR = SCRIPT_DIR / "regression-test-logs"
POCL_DIR = SCRIPT_DIR / "pocl"
RODINIA_DIR = SCRIPT_DIR / "rodinia"
VENTUS_TESTCASE_DIR = SCRIPT_DIR / "testcases"


@dataclass(frozen=True)
class TestCase:
    name: str
    path: Path
    cmd: list[str]
    # default 600s: RTL Verilator under -j 32 高并发抢占下，单 rep wall time 容易飘到
    # 几百秒；旧的 120s 只够 cyclesim/spike，对 rtlsim 来说太紧（默认会让 bfs/kmeans/
    # hotspot* 等 case 全部 wall-clock timeout 而不是真功能 fail）。
    timeout: int = 600
    need_make: bool = True


@dataclass(frozen=True)
class BackendCaseSet:
    backends: frozenset[str]
    cases: tuple[TestCase, ...]


DEFAULT_TEST_CASES = [
    TestCase(name="matadd", path=POCL_DIR / "build/examples/matadd", cmd=["./matadd"], need_make=False),
    TestCase(name="vecadd_4096", path=POCL_DIR / "build/examples/vecadd", cmd=["./vecadd", "4096", "128"], need_make=False),
    TestCase(name="gaussian_16", path=RODINIA_DIR / "opencl/gaussian", cmd=["./gaussian.out", "-p", "0", "-d", "0", "-f", "../../data/gaussian/matrix16.txt", "-v"], timeout=300),
    TestCase(name="b+tree_128", path=RODINIA_DIR / "opencl/b+tree", cmd=["./b+tree.out", "file", "../../data/b+tree/mil.txt", "command", "../../data/b+tree/command_128.txt", "--ref", "output_128.nvidia.txt"]),
    TestCase(name="backprop_1024", path=RODINIA_DIR / "opencl/backprop", cmd=["./backprop.out", "-n", "1024", "--ref", "nvidia-result-n1024"], timeout=900),
    TestCase(name="bfs_4096", path=RODINIA_DIR / "opencl/bfs", cmd=["./bfs.out", "../../data/bfs/graph4096.txt"]),
    TestCase(name="nn_1024", path=RODINIA_DIR / "opencl/nn", cmd=["./nn.out", "../../data/nn/list1k.txt", "-r", "20", "-lat", "13", "-lng", "27", "-f", "../../data/nn", "-t", "-p", "0", "-d", "0", "--ref", "nvidia-result-1k-lat13-lng27"]),
    TestCase(name="nn_64k", path=RODINIA_DIR / "opencl/nn", cmd=["./nn.out", "../../data/nn/list64k.txt", "-r", "20", "-lat", "30", "-lng", "90", "-f", "../../data/nn", "-t", "-p", "0", "-d", "0", "--ref", "nvidia-result-64k-lat30-lng90"], timeout=900),
    TestCase(name="kmeans_512", path=RODINIA_DIR / "opencl/kmeans", cmd=["./kmeans.out", "-o", "-r", "-i", "../../data/kmeans/512_34f.txt", "-g", "nvidia_result_512_34f_k5", "-p", "0", "-d", "0"]),
    TestCase(name="pathfinder_32x64_h4", path=RODINIA_DIR / "opencl/pathfinder", cmd=["./pathfinder.out", "-c", "64", "-r", "32", "-h", "4", "-p", "0", "-d", "0"]),
    TestCase(name="hotspot_64_4_4", path=RODINIA_DIR / "opencl/hotspot", cmd=["./hotspot.out", "64", "4", "4", "../../data/hotspot/temp_64", "../../data/hotspot/power_64", "output.txt", "-p", "0", "-d", "0", "--ref", "nvidia-ref-64-4-4.txt"]),
    TestCase(name="hotspot3D_64x8_i1", path=RODINIA_DIR / "opencl/hotspot3D", cmd=["./hotspot3D.out", "-n", "64", "-l", "8", "-i", "1", "-f", "../../data/hotspot3D/power_64x8", "../../data/hotspot3D/temp_64x8", "output.txt", "-p", "0", "-d", "0"]),
    TestCase(name="hotspot3D_512x2_i1", path=RODINIA_DIR / "opencl/hotspot3D", cmd=["./hotspot3D.out", "-n", "512", "-l", "2", "-i", "1", "-f", "../../data/hotspot3D/power_512x2", "../../data/hotspot3D/temp_512x2", "output.txt", "-p", "0", "-d", "0"]),
    TestCase(name="nw_80", path=RODINIA_DIR / "opencl/nw", cmd=["./nw.out", "80", "10", "./nw.cl", "-p", "0", "-d", "0"]),
    # Pressure variants kept out of default regression:
    # TestCase(name="hotspot3D_512x4_i1", path=RODINIA_DIR / "opencl/hotspot3D", cmd=["./hotspot3D.out", "-n", "512", "-l", "4", "-i", "1", "-f", "../../data/hotspot3D/power_512x4", "../../data/hotspot3D/temp_512x4", "output.txt", "-p", "0", "-d", "0"], timeout=900),
    # TestCase(name="nw_256", path=RODINIA_DIR / "opencl/nw", cmd=["./nw.out", "256", "10", "./nw.cl", "-p", "0", "-d", "0"], timeout=900),
    TestCase(name="heartwall_1", path=RODINIA_DIR / "opencl/heartwall", cmd=["./run"], timeout=900),
    TestCase(name="srad_1_1_64", path=RODINIA_DIR / "opencl/srad", cmd=["./run"], timeout=600),
    TestCase(name="lud_64", path=RODINIA_DIR / "opencl/lud", cmd=["./lud.out", "-v", "-i", "../../data/lud/64.dat", "-p", "0", "-d", "0"], timeout=600),
    TestCase(name="streamcluster_256", path=RODINIA_DIR / "opencl/streamcluster", cmd=["./run"], timeout=600),
    # Not supported yet
    # TestCase(name="hybridsort_4096", path=RODINIA_DIR / "opencl/hybridsort", cmd=["./run"], timeout=600),
    # TestCase(name="lavaMD_box1", path=RODINIA_DIR / "opencl/lavaMD", cmd=["./run"], timeout=600),
    # TestCase(name="leukocyte_1", path=RODINIA_DIR / "opencl/leukocyte", cmd=["./run"], timeout=600),
    # TestCase(name="myocyte_1", path=RODINIA_DIR / "opencl/myocyte", cmd=["./run"], timeout=600),
    # TestCase(name="particlefilter_16x16x3_np100", path=RODINIA_DIR / "opencl/particlefilter", cmd=["./run"], timeout=600),
    TestCase(name="mnist_conv_small", path=VENTUS_TESTCASE_DIR / "_get_case/MNIST_conv_small", cmd=["./conv.out"]),
    TestCase(name="mnist", path=VENTUS_TESTCASE_DIR / "_get_case/MNIST", cmd=["./nn_forward.out"]),
    TestCase(name="lds_corruption", path=VENTUS_TESTCASE_DIR / "others/lds_corruption", cmd=["./run"], timeout=180),
]


BACKEND_CASE_SETS = (
    BackendCaseSet(
        backends=frozenset({"spike", "sbt"}),
        cases=(
            TestCase(name="cfd_i1", path=RODINIA_DIR / "opencl/cfd", cmd=["./run"], timeout=600),
            TestCase(name="dwt2d_192", path=RODINIA_DIR / "opencl/dwt2d", cmd=["./run"], timeout=600),
        ),
    ),
)


TEST_CASES = DEFAULT_TEST_CASES + [
    test_case
    for case_set in BACKEND_CASE_SETS
    for test_case in case_set.cases
]


def _build_case_index(test_cases: list[TestCase]) -> dict[str, int]:
    case_index: dict[str, int] = {}
    for index, test_case in enumerate(test_cases):
        if test_case.name in case_index:
            raise ValueError(f"Duplicate testcase name: {test_case.name}")
        case_index[test_case.name] = index
    return case_index


def _case_indices(case_names: tuple[str, ...]) -> list[int]:
    return [TEST_CASE_INDEX_BY_NAME[name] for name in case_names]


def _build_backend_case_names(case_sets: tuple[BackendCaseSet, ...]) -> dict[str, tuple[str, ...]]:
    case_names_by_backend: dict[str, list[str]] = {}
    for case_set in case_sets:
        for backend in case_set.backends:
            case_names_by_backend.setdefault(backend, []).extend(test_case.name for test_case in case_set.cases)
    return {
        backend: tuple(case_names)
        for backend, case_names in case_names_by_backend.items()
    }


def _build_case_backends(case_sets: tuple[BackendCaseSet, ...]) -> dict[str, frozenset[str]]:
    case_backends: dict[str, frozenset[str]] = {}
    for case_set in case_sets:
        for test_case in case_set.cases:
            if test_case.name in case_backends:
                raise ValueError(f"Duplicate backend-specific testcase name: {test_case.name}")
            case_backends[test_case.name] = case_set.backends
    return case_backends


def required_case_names_for_backend(backend: str) -> tuple[str, ...]:
    return ALL_REQUIRED_CASE_NAMES + BACKEND_CASE_NAMES_BY_BACKEND.get(backend, ())


def required_case_indices_for_backend(backend: str) -> list[int]:
    return _case_indices(required_case_names_for_backend(backend))


def case_enabled_for_backend(testcase_index: int, backend: str) -> bool:
    allowed_backends = CASE_BACKENDS.get(TEST_CASES[testcase_index].name)
    return allowed_backends is None or backend in allowed_backends


def selected_case_indices_for_backend(backend: str, selected_indices: list[int] | tuple[int, ...]) -> tuple[int, ...]:
    return tuple(
        index
        for index in selected_indices
        if case_enabled_for_backend(index, backend)
    )


TEST_CASE_INDEX_BY_NAME = _build_case_index(TEST_CASES)
ALL_REQUIRED_CASE_NAMES = tuple(test_case.name for test_case in DEFAULT_TEST_CASES)
BACKEND_CASE_NAMES_BY_BACKEND = _build_backend_case_names(BACKEND_CASE_SETS)
CASE_BACKENDS = _build_case_backends(BACKEND_CASE_SETS)

# Keep cache preset exclusions named so TEST_CASES insertions do not shift numeric checklists.
RTL_WITH_CACHE_EXCLUDED_CASE_NAMES = frozenset({
    "b+tree_128",
    "bfs_4096",
    "srad_1_1_64",
    "lud_64",
    "nw_80",
})
RTL_WITH_CACHE_CASE_NAMES = tuple(
    name for name in ALL_REQUIRED_CASE_NAMES if name not in RTL_WITH_CACHE_EXCLUDED_CASE_NAMES
)


REQUIRED_PRESETS = {
    "all": _case_indices(ALL_REQUIRED_CASE_NAMES),
    "isa": required_case_indices_for_backend("spike"),
    "sbt": required_case_indices_for_backend("sbt"),
    "cycle": _case_indices(ALL_REQUIRED_CASE_NAMES),
    "rtl-with-cache": _case_indices(RTL_WITH_CACHE_CASE_NAMES),
    "rtl-no-cache": _case_indices(ALL_REQUIRED_CASE_NAMES),
}


MATRIX_PRESETS = {
    "all": [
        ("isa", "isa"),
        ("sbt", "sbt"),
        ("cycle", "cycle"),
        ("rtl-no-cache", "rtl-no-cache"),
        ("rtl-with-cache", "rtl-with-cache"),
        ("gvm-no-cache", "rtl-no-cache"),
        ("gvm-with-cache", "rtl-with-cache"),
    ],
    "ci": [
        ("cycle", "cycle"),
        ("rtl-no-cache", "rtl-no-cache"),
        ("rtl-with-cache", "rtl-with-cache"),
        ("gvm-no-cache", "rtl-no-cache"),
        ("gvm-with-cache", "rtl-with-cache"),
    ],
    "rtl-both": [
        ("rtl-withcache", "rtl-with-cache"),
        ("rtl-nocache", "rtl-no-cache"),
    ],
}
