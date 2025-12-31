#!/bin/python3
import multiprocessing
import subprocess
import signal
import os
import sys
import time
from pathlib import Path
from dataclasses import dataclass
from typing import Tuple, Optional, List, Type
from tqdm import tqdm
import argparse  # 新增：解析命令行参数

SCRIPT_DIR = Path(__file__).parent
LOG_DIR = SCRIPT_DIR / "regression-test-logs"
POCL_DIR = SCRIPT_DIR / "pocl"
RODINIA_DIR = SCRIPT_DIR / "rodinia"
VENTUS_TESTCASE_DIR = SCRIPT_DIR / "testcases"

# If your computer is not fast enough, change this value larger
TIMEOUT_SCALE = 1
# Run how many test processes simultaneously
MULTIPROCESS_NUM = 8

# 进程池引用，便于信号处理时安全终止
pool = None  # 在创建前保持为 None，避免信号处理中 NameError

def suggest_default_jobs(max_cap: int = 32) -> int:
    cpu = os.cpu_count() or multiprocessing.cpu_count() or 1
    ventus_backend = os.environ.get('VENTUS_BACKEND', 'spike')
    if ventus_backend in ['rtl', 'rtlsim', 'gpgpu']:
        # rtl backend has 8-multithread for each process 
        cpu = max(1, cpu // 8)
    return max(1, min(max_cap, cpu * 3 // 2))

@dataclass
class TestCase:
    name: str
    path: Path
    cmd: list
    timeout: int = 120 # seconds
    need_make: bool = True  # 是否需要编译

# 约定的结果标签
TAG_OK = "ok"
TAG_FAIL = "failed"
TAG_TIMEOUT = "timeout"
TAG_COMPILE_FAIL = "compile_failed"

test_cases = [
    TestCase(name="matadd"       , path=POCL_DIR/"build/examples/matadd", cmd=["./matadd"], need_make=False),
    TestCase(name="vecadd_4096"  , path=POCL_DIR/"build/examples/vecadd", cmd=["./vecadd", "4096", "128"], need_make=False),
    TestCase(name="gaussian_16"  , path=RODINIA_DIR/"opencl/gaussian"   , cmd=["./gaussian.out", "-p", "0", "-d", "0", "-f", "../../data/gaussian/matrix16.txt", "-v"]),
   #TestCase(name="backprop_64"  , path=RODINIA_DIR/"opencl/backprop"   , cmd=["./backprop.out", "-n", "64", "--ref", "nvidia-result-n64"]),
    TestCase(name="b+tree_128"   , path=RODINIA_DIR/"opencl/b+tree"     , cmd=["./b+tree.out", "file", "../../data/b+tree/mil.txt", "command", "../../data/b+tree/command_128.txt", "--ref", "output_128.nvidia.txt"]),
    TestCase(name="backprop_1024", path=RODINIA_DIR/"opencl/backprop"   , cmd=["./backprop.out", "-n", "1024", "--ref", "nvidia-result-n1024"]),
    TestCase(name="bfs_4096"     , path=RODINIA_DIR/"opencl/bfs"        , cmd=["./bfs.out", "../../data/bfs/graph4096.txt"]),
    TestCase(name="nn_1024"      , path=RODINIA_DIR/"opencl/nn"         , cmd=["./nn.out", "../../data/nn/list1k.txt", "-r", "20", "-lat", "13", "-lng", "27", "-f", "../../data/nn", "-t", "-p", "0", "-d", "0", "--ref", "nvidia-result-1k-lat13-lng27"]),
    TestCase(name="nn_64k"       , path=RODINIA_DIR/"opencl/nn"         , cmd=["./nn.out", "../../data/nn/list64k.txt", "-r", "20", "-lat", "30", "-lng", "90", "-f", "../../data/nn", "-t", "-p", "0", "-d", "0", "--ref", "nvidia-result-64k-lat30-lng90"]),
    TestCase(name="kmeans_512"   , path=RODINIA_DIR/"opencl/kmeans"     , cmd=["./kmeans.out", "-o", "-r", "-i", "../../data/kmeans/512_34f.txt", "-g", "nvidia_result_512_34f_k5", "-p", "0", "-d", "0"]),
    TestCase(name="mnist_conv_small", path=VENTUS_TESTCASE_DIR/"_get_case/MNIST_conv_small", cmd=["./conv.out"]),
    TestCase(name="mnist"           , path=VENTUS_TESTCASE_DIR/"_get_case/MNIST"           , cmd=["./nn_forward.out"]),
    # 以下测例可以跑通，但十分缓慢
    # TestCase(name="bfs_65536"    , path=RODINIA_DIR/"opencl/bfs"        , cmd=["./bfs.out", "../../data/bfs/graph65536.txt"], timeout=1000),
    # TestCase(name="b+tree_1024"  , path=RODINIA_DIR/"opencl/b+tree"     , cmd=["./b+tree.out", "file", "../../data/b+tree/mil.txt", "command", "../../data/b+tree/command_1024.txt", "--ref", "output_1024.nvidia.txt"], timeout=300),
    # TestCase(name="kmeans_4096"  , path=RODINIA_DIR/"opencl/kmeans"     , cmd=["./kmeans.out", "-o", "-r", "-i", "../../data/kmeans/4096_34f.txt", "-g", "nvidia_result_4096_34f_k5", "-p", "0", "-d", "0"], timeout=1000),
    # TestCase(name="mnist_conv"      , path=VENTUS_TESTCASE_DIR/"_get_case/MNIST_conv"      , cmd=["./conv.out"]),
]

REQUIRED_PRESETS = {
    "all": list(range(len(test_cases))),
    "cycle": [0,1,2,4,5,6,7,8,9,10],
    "rtl-with-cache": [0,1,2,6,9,10],
}

manager = multiprocessing.Manager()
compile_paths = manager.dict()  # 用于存储已编译的路径，避免重复编译
compile_path_locks = manager.dict()  # 保护对compile_paths的访问

def get_compile_path_lock(path: Path):
    """获取指定路径的锁，确保对compile_paths的访问是线程安全的"""
    # 注意：锁必须在主进程启动 pool 前预先创建，否则多进程并发创建锁会有竞争
    return compile_path_locks[path]

def format_list_with_quotes(lst):
    formatted = []
    for s in lst:
        if ' ' in s:
            formatted.append(repr(s))  # repr 会自动加上引号，并处理转义
        else:
            formatted.append(s)
    return ' '.join(formatted)

def run_test_case(arg: Tuple[int, TestCase]) -> Tuple[int, Tuple[int, str]]:
    """运行单个测试用例，返回 (索引, (返回码, 标签))"""
    index, testcase = arg
    log_file = LOG_DIR/f"{testcase.name}.log"
    
    with open(log_file, "w") as f:
        # 先编译
        if testcase.need_make:
            f.write("=== Compile Testcase ===\n")
            lock = get_compile_path_lock(testcase.path)
            with lock:
                if (testcase.path not in compile_paths) or (not compile_paths[testcase.path]):
                    try:
                        result = subprocess.run(["make"], stdout=f, stderr=f, timeout=60, cwd=testcase.path)
                        if result.returncode != 0:
                            f.write("Compile Failed\n")
                            return index, (2, TAG_COMPILE_FAIL)
                        f.write("Compile OK\n")
                        compile_paths[testcase.path] = True  # 标记为已编译
                    except subprocess.TimeoutExpired:
                        f.write("Compile Timeout, Failed\n")
                        return index, (2, TAG_COMPILE_FAIL)
                    except Exception as e:
                        f.write(f"Compile Failed: {e}\n")
                        return index, (2, TAG_COMPILE_FAIL)
                else:
                    f.write("Already Compiled, Skipping...\n")
        
        # 运行测试
        f.write("=== Run Test ===\n")
        f.flush()
        try:
            f.write(f"TestCase {index}: {testcase.name} begin...\nCOMMAND: ")
            f.write(format_list_with_quotes(testcase.cmd));
            f.write("\n")
            f.flush()
            result = subprocess.run(testcase.cmd, stdout=f, stderr=f, timeout=testcase.timeout * TIMEOUT_SCALE, cwd=testcase.path)
            rc = result.returncode
            tag = TAG_OK if rc == 0 else TAG_FAIL
            return index, (rc, tag)
        except subprocess.TimeoutExpired:
            f.write("\nTestcase execution timeout, Failed\n")
            # 用一个超出常规范围的返回码以避免与被测程序冲突
            return index, (9999, TAG_TIMEOUT)
        except Exception as e:
            f.write(f"\nTestcase execution failed with exception: {e}\n")
            return index, (9998, TAG_FAIL)

def signal_handler(signum, frame):
    """处理外部中断信号，终止所有子进程"""
    print("Interrupt received, terminating all test cases...")
    global pool
    if pool is not None:
        pool.terminate()
    exit(1)

if __name__ == "__main__":
    # 解析命令行参数
    parser = argparse.ArgumentParser(description="Ventus regression test runner")
    parser.add_argument("-t", "--timeout-scale", type=float, default=None, help="Timeout scale (default: 1)")
    parser.add_argument("-j", "--jobs", type=int, default=None, help="Parallel multiprocess num (default: auto)")
    parser.add_argument(
        "--checklist",
        type=str,
        default="all",
        help=(
            "Which testcases must pass. Accepts a comma-separated index list (e.g. 0,2,5) "
            "or a preset name: " + ", ".join(sorted(REQUIRED_PRESETS.keys())) + ". Default: all"
        ),
    )
    args = parser.parse_args()

    # 解析必须通过的 TestCase 列表
    checklist = (args.checklist or "all").strip().lower()
    if checklist in REQUIRED_PRESETS:
        checklist_set = set(REQUIRED_PRESETS[checklist])
    else:
        try:
            parts = [p.strip() for p in checklist.split(",") if p.strip()]
            if not parts:
                parser.error("--checklist is empty")
            checklist_set = set(int(p) for p in parts)
        except ValueError:
            parser.error(
                "Invalid --checklist value. Use preset name ({}), or comma-separated integers like 0,1,2".format(
                    ", ".join(sorted(REQUIRED_PRESETS.keys()))
                )
            )

    invalid_indices = sorted(i for i in checklist_set if i < 0 or i >= len(test_cases))
    if invalid_indices:
        parser.error(f"--checklist contains invalid indices: {invalid_indices} (valid range: 0..{len(test_cases)-1})")

    # 应用命令行参数或自动检测默认值
    if args.timeout_scale is not None:
        TIMEOUT_SCALE = args.timeout_scale
    # 自动检测合理并发数，除非用户显式指定
    if args.jobs is not None and args.jobs > 0:
        MULTIPROCESS_NUM = args.jobs
    else:
        MULTIPROCESS_NUM = suggest_default_jobs(len(test_cases))

    # 创建日志目录
    if not os.path.exists(LOG_DIR):
        os.makedirs(LOG_DIR)

    # 设置信号处理器以处理外部中断
    signal.signal(signal.SIGINT, signal_handler)

    # 预先为所有涉及编译的路径创建锁，避免多进程竞争创建锁导致的 race condition
    for tc in test_cases:
        if tc.need_make and tc.path not in compile_path_locks:
            compile_path_locks[tc.path] = manager.Lock()

    # 创建进程池，控制并行度
    pool = multiprocessing.Pool(processes=MULTIPROCESS_NUM)  # 可根据需要调整并行进程数

    results: List[Optional[Tuple[int, str]]] = [None] * len(test_cases)
    total = len(test_cases)

    # 并行运行测试用例并用进度条显示
    print(f"Running test with multiprocess num = {MULTIPROCESS_NUM}, timeout scale = {TIMEOUT_SCALE}")
    with tqdm(total=total, desc="Running", unit="test") as pbar:
        for index, payload in pool.imap_unordered(run_test_case, enumerate(test_cases)):
            rc, tag = payload
            results[index] = (rc, tag)
            # 更新进度条（统计通过/失败）
            pass_count = sum(1 for r in results if (r is not None and r[0] == 0))
            fail_count = sum(1 for r in results if (r is not None and r[0] != 0))
            pbar.set_postfix_str(f"pass={pass_count}, fail={fail_count}")
            pbar.update(1)

    # 等待所有进程完成并关闭进程池
    pool.close()
    pool.join()

    # 打印每个测试用例的结果
    print("\nTest result: ")
    for i, testcase in enumerate(test_cases):
        res = results[i]
        if res is None:
            rc, tag = (-1, "not_run")
        else:
            rc, tag = res
        if rc == 0:
            status = "\033[92mPassed\033[0m"  # 绿
        elif tag == TAG_COMPILE_FAIL:
            status = "\033[93mCompile Failed\033[0m"  # 黄
        elif tag == TAG_TIMEOUT:
            status = "\033[91mTime Exceeded\033[0m"  # 红
        else:
            status = "\033[91mFailed\033[0m"  # 红
        print(f"{i:2d} {testcase.name}: {status}")

    # 打印总结（Failed + TimeExceeded + Compile Failed 都算 Fail）
    pass_count = sum(1 for r in results if (r is not None and r[0] == 0))
    fail_count = total - pass_count
    checklist_failed: List[int] = []
    for i in checklist_set:
        res = results[i]
        if res is None or res[0] != 0:
            checklist_failed.append(i)
    checklist_failed.sort()
    exit_code = 0 if not checklist_failed else 1

    if exit_code == 0:
        result_descript = f"All required testcases in checklist({checklist_set}) passed."
    else:
        result_descript = f"Some testcases in checklist({checklist_set}) failed."
    unicode_symbol = "\033[92m✔\033[0m" if exit_code == 0 else "\033[91m✘\033[0m"
    print(f"\nSummary: {pass_count} passed, {fail_count} failed. \n{result_descript} {unicode_symbol}")

    # 如果指定的必须通过测例未全部通过，则以非0退出码告知CI失败
    if exit_code != 0:
        print(f"Required testcases not all passed (--checklist={args.checklist}): {checklist_failed}")

    if 'NOTEBOOK_BASH_KERNEL_CAPABILITIES' not in os.environ: # not in JupyterNotebook bash_kernel
        os.system("stty echo") # spike sometimes messes up terminal echo

    sys.exit(exit_code)
