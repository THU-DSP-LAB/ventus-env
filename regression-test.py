#!/bin/python3
import multiprocessing
import subprocess
import signal
import os
import time
from pathlib import Path
from dataclasses import dataclass
from typing import Tuple, Optional, List, Type

SCRIPT_DIR = Path(__file__).parent
LOG_DIR = SCRIPT_DIR / "regression-test-logs"
POCL_DIR = SCRIPT_DIR / "pocl"
RODINIA_DIR = SCRIPT_DIR / "rodinia"

# If your computer is not fast enough, change this value larger
TIMEOUT_SCALE = 1
# Run how many test processes simultaneously
MULTIPROCESS_NUM = 8

@dataclass
class TestCase:
    name: str
    path: Path
    cmd: list
    timeout: int = 120 # seconds
    need_make: bool = True  # 是否需要编译

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
    # 以下测例可以跑通，但十分缓慢
    # TestCase(name="bfs_65536"    , path=RODINIA_DIR/"opencl/bfs"        , cmd=["./bfs.out", "../../data/bfs/graph65536.txt"], timeout=1000),
    # TestCase(name="b+tree_1024"  , path=RODINIA_DIR/"opencl/b+tree"     , cmd=["./b+tree.out", "file", "../../data/b+tree/mil.txt", "command", "../../data/b+tree/command_1024.txt", "--ref", "output_1024.nvidia.txt"], timeout=300),
    # TestCase(name="kmeans_4096"  , path=RODINIA_DIR/"opencl/kmeans"     , cmd=["./kmeans.out", "-o", "-r", "-i", "../../data/kmeans/4096_34f.txt", "-g", "nvidia_result_4096_34f_k5", "-p", "0", "-d", "0"], timeout=1000),
]

manager = multiprocessing.Manager()
compile_paths = manager.dict()  # 用于存储已编译的路径，避免重复编译
compile_path_locks = manager.dict()  # 保护对compile_paths的访问

def get_compile_path_lock(path: Path):
    """获取指定路径的锁，确保对compile_paths的访问是线程安全的"""
    if path not in compile_path_locks:
        compile_path_locks[path] = manager.Lock()
    return compile_path_locks[path]

def run_test_case(arg: Tuple[int, TestCase]) -> Tuple[int, int]:
    """运行单个测试用例，并返回结果"""
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
                            return index, 2  # 编译失败用返回码2表示
                        f.write("Compile OK\n")
                        compile_paths[testcase.path] = True  # 标记为已编译
                    except subprocess.TimeoutExpired:
                        f.write("Compile Timeout , Failed\n")
                        return index, 2
                    except Exception as e:
                        f.write(f"Compile Failed: {e}\n")
                        return index, 2
                else:
                    f.write("Already Compiled, Skipping...\n")
        
        # 运行测试
        f.write("=== Run Test ===\n")
        f.flush()
        try:
            print(f"TestCase {index}: {testcase.name} begin...")
            result = subprocess.run(testcase.cmd, stdout=f, stderr=f, timeout=testcase.timeout * TIMEOUT_SCALE, cwd=testcase.path)
            return index, result.returncode
        except subprocess.TimeoutExpired:
            f.write("\nTestcase execution timeout, Failed\n")
            return index, 1  # 超时视为失败

def signal_handler(signum, frame):
    """处理外部中断信号，终止所有子进程"""
    print("收到中断信号，正在终止所有测试用例...")
    pool.terminate()
    exit(1)

if __name__ == "__main__":
    # 创建日志目录
    if not os.path.exists(LOG_DIR):
        os.makedirs(LOG_DIR)

    # 设置信号处理器以处理外部中断
    signal.signal(signal.SIGINT, signal_handler)

    # 创建进程池，控制并行度
    pool = multiprocessing.Pool(processes=MULTIPROCESS_NUM)  # 可根据需要调整并行进程数

    results: List[Optional[int]] = [None] * len(test_cases)
    total = len(test_cases)
    completed = 0

    # 并行运行测试用例并监控进度
    for index, result in pool.imap_unordered(run_test_case, enumerate(test_cases)):
        results[index] = result
        completed += 1
        print(f"已完成 {completed}/{total} 个测试用例")

    # 等待所有进程完成并关闭进程池
    pool.close()
    pool.join()

    # 打印每个测试用例的结果
    print("\n测试结果：")
    for i, testcase in enumerate(test_cases):
        if results[i] == 0:
            status = "\033[92mPassed\033[0m"  # 绿
        elif results[i] == 2:
            status = "\033[93mCompile Failed\033[0m"  # 黄
        else:
            status = "\033[91mFailed\033[0m"  # 红
        print(f"{i:2d} {testcase.name}: {status}")

    # 打印总结
    pass_count = sum(1 for r in results if r == 0)
    fail_count = total - pass_count
    unicode_symbol = "\033[92m✔\033[0m" if fail_count == 0 else "\033[91m✘\033[0m"
    print(f"\n总结：{pass_count}个通过，{fail_count}个失败 {unicode_symbol}")
