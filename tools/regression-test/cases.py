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


TEST_CASES = [
    TestCase(name="matadd", path=POCL_DIR / "build/examples/matadd", cmd=["./matadd"], need_make=False),
    TestCase(name="vecadd_4096", path=POCL_DIR / "build/examples/vecadd", cmd=["./vecadd", "4096", "128"], need_make=False),
    TestCase(name="gaussian_16", path=RODINIA_DIR / "opencl/gaussian", cmd=["./gaussian.out", "-p", "0", "-d", "0", "-f", "../../data/gaussian/matrix16.txt", "-v"], timeout=300),
    TestCase(name="b+tree_128", path=RODINIA_DIR / "opencl/b+tree", cmd=["./b+tree.out", "file", "../../data/b+tree/mil.txt", "command", "../../data/b+tree/command_128.txt", "--ref", "output_128.nvidia.txt"]),
    TestCase(name="backprop_1024", path=RODINIA_DIR / "opencl/backprop", cmd=["./backprop.out", "-n", "1024", "--ref", "nvidia-result-n1024"], timeout=900),
    TestCase(name="bfs_4096", path=RODINIA_DIR / "opencl/bfs", cmd=["./bfs.out", "../../data/bfs/graph4096.txt"]),
    TestCase(name="nn_1024", path=RODINIA_DIR / "opencl/nn", cmd=["./nn.out", "../../data/nn/list1k.txt", "-r", "20", "-lat", "13", "-lng", "27", "-f", "../../data/nn", "-t", "-p", "0", "-d", "0", "--ref", "nvidia-result-1k-lat13-lng27"]),
    TestCase(name="nn_64k", path=RODINIA_DIR / "opencl/nn", cmd=["./nn.out", "../../data/nn/list64k.txt", "-r", "20", "-lat", "30", "-lng", "90", "-f", "../../data/nn", "-t", "-p", "0", "-d", "0", "--ref", "nvidia-result-64k-lat30-lng90"], timeout=900),
    TestCase(name="kmeans_512", path=RODINIA_DIR / "opencl/kmeans", cmd=["./kmeans.out", "-o", "-r", "-i", "../../data/kmeans/512_34f.txt", "-g", "nvidia_result_512_34f_k5", "-p", "0", "-d", "0"]),
    TestCase(name="pathfinder_4x32_h1", path=RODINIA_DIR / "opencl/pathfinder", cmd=["./pathfinder.out", "-c", "32", "-r", "4", "-h", "1", "-p", "0", "-d", "0"]),
    TestCase(name="hotspot_64_1_1", path=RODINIA_DIR / "opencl/hotspot", cmd=["./hotspot.out", "64", "1", "1", "../../data/hotspot/temp_64", "../../data/hotspot/power_64", "output.txt", "-p", "0", "-d", "0", "--ref", "nvidia-ref-64-1-1.txt"]),
    TestCase(name="hotspot3D_64x8_i1", path=RODINIA_DIR / "opencl/hotspot3D", cmd=["./hotspot3D.out", "-n", "64", "-l", "8", "-i", "1", "-f", "../../data/hotspot3D/power_64x8", "../../data/hotspot3D/temp_64x8", "output.txt", "-p", "0", "-d", "0"]),
    TestCase(name="nw_16", path=RODINIA_DIR / "opencl/nw", cmd=["./nw.out", "16", "10", "./nw.cl", "-p", "0", "-d", "0"]),
    TestCase(name="heartwall_1", path=RODINIA_DIR / "opencl/heartwall", cmd=["./run"], timeout=900),
    TestCase(name="srad_1_1_64", path=RODINIA_DIR / "opencl/srad", cmd=["./run"], timeout=600),
    TestCase(name="lud_64", path=RODINIA_DIR / "opencl/lud", cmd=["./lud.out", "-v", "-i", "../../data/lud/64.dat", "-p", "0", "-d", "0"], timeout=600),
    TestCase(name="mnist_conv_small", path=VENTUS_TESTCASE_DIR / "_get_case/MNIST_conv_small", cmd=["./conv.out"]),
    TestCase(name="mnist", path=VENTUS_TESTCASE_DIR / "_get_case/MNIST", cmd=["./nn_forward.out"]),
    TestCase(name="lds_corruption", path=VENTUS_TESTCASE_DIR / "others/lds_corruption", cmd=["./run"], timeout=180),
]


REQUIRED_PRESETS = {
    "all": list(range(len(TEST_CASES))),
    "isa": [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18],
    "sbt": [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18],
    "cycle": [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 15, 16, 17, 18],
    "rtl-with-cache": [0, 1, 2, 4, 6, 7, 9, 11, 12, 13, 16, 17, 18],
    "rtl-no-cache": [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18],
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
    "rtl-both": [
        ("rtl-withcache", "rtl-with-cache"),
        ("rtl-nocache", "rtl-no-cache"),
    ],
}
