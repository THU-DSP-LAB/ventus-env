#!/bin/python3
import multiprocessing
import subprocess
import signal
import os
import sys
import time
from pathlib import Path
from dataclasses import dataclass
from typing import Tuple, Optional, List, Type, Dict
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

def suggest_default_jobs(max_cap: int = 32, backend: Optional[str] = None) -> int:
    """估计默认并行度。
    rtl 类后端每进程自带 8 线程 Verilator，需要按 cpu//8 收缩；其他后端用 cpu*3/2。
    backend=None 时读当前进程的 VENTUS_BACKEND；matrix 模式下会按候选后端分别评估，
    最终取最小值作为整体并发数。
    """
    cpu = os.cpu_count() or multiprocessing.cpu_count() or 1
    ventus_backend = backend if backend else os.environ.get('VENTUS_BACKEND', 'spike')
    # 剥掉 cache 后缀（如 rtl-withcache -> rtl），只按主 backend 判定并发策略
    ventus_backend = ventus_backend.split('-')[0]
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
    TestCase(name="nn_64k"       , path=RODINIA_DIR/"opencl/nn"         , cmd=["./nn.out", "../../data/nn/list64k.txt", "-r", "20", "-lat", "30", "-lng", "90", "-f", "../../data/nn", "-t", "-p", "0", "-d", "0", "--ref", "nvidia-result-64k-lat30-lng90"], timeout=600),
    TestCase(name="kmeans_512"   , path=RODINIA_DIR/"opencl/kmeans"     , cmd=["./kmeans.out", "-o", "-r", "-i", "../../data/kmeans/512_34f.txt", "-g", "nvidia_result_512_34f_k5", "-p", "0", "-d", "0"]),
    TestCase(name="mnist_conv_small", path=VENTUS_TESTCASE_DIR/"_get_case/MNIST_conv_small", cmd=["./conv.out"]),
    TestCase(name="mnist"           , path=VENTUS_TESTCASE_DIR/"_get_case/MNIST"           , cmd=["./nn_forward.out"]),
    # 以下测例可以跑通，但十分缓慢
    # TestCase(name="bfs_65536"    , path=RODINIA_DIR/"opencl/bfs"        , cmd=["./bfs.out", "../../data/bfs/graph65536.txt"], timeout=1000),
    # TestCase(name="b+tree_1024"  , path=RODINIA_DIR/"opencl/b+tree"     , cmd=["./b+tree.out", "file", "../../data/b+tree/mil.txt", "command", "../../data/b+tree/command_1024.txt", "--ref", "output_1024.nvidia.txt"], timeout=300),
    # TestCase(name="kmeans_4096"  , path=RODINIA_DIR/"opencl/kmeans"     , cmd=["./kmeans.out", "-o", "-r", "-i", "../../data/kmeans/4096_34f.txt", "-g", "nvidia_result_4096_34f_k5", "-p", "0", "-d", "0"], timeout=1000),
    # TestCase(name="mnist_conv"      , path=VENTUS_TESTCASE_DIR/"_get_case/MNIST_conv"      , cmd=["./conv.out"]),
]

# Checklist 预设：一次运行中"必须通过"的 TestCase 索引子集。
# 任一索引对应的测例未 pass 就以非零退出码返回，用于 CI 卡控。
# - rtl-with-cache: 带 Cache 版本 RTL 目前能稳定 pass 的 8 个
#                   （b+tree_128 / bfs_4096 / kmeans_512 在带 Cache 时尚未通过，故未纳入）
# - rtl-no-cache:   不带 Cache 版本 RTL 目前全部 11 个都能通过
REQUIRED_PRESETS = {
    "all":            list(range(len(test_cases))),
    "cycle":          [0,1,2,4,5,6,7,8,9,10],
    "rtl-with-cache": [0,1,2,4,6,7,9,10],
    "rtl-no-cache":   [0,1,2,3,4,5,6,7,8,9,10],
}

# Matrix 预设：一次调用里按序跑多个 (VENTUS_BACKEND, checklist) 组合。
# - 元组第一项是要注入到子进程 VENTUS_BACKEND 环境变量的字符串。
#   driver/auto_select (driver/driver/auto_select/ventus.cpp) 会按 "-" 拆分，
#   识别 "withcache"/"nocache" 后缀并切换 libVentusRTL-*.so 软链接。
# - 元组第二项是 REQUIRED_PRESETS 的 key，用来判定该模式是否通过。
# 使用示例：
#     ./regression-test.py --matrix rtl-both
# 会先跑 rtl-withcache（比对 rtl-with-cache checklist），
# 再跑 rtl-nocache（比对 rtl-no-cache checklist），
# 两组日志分别写到 regression-test-logs/rtl-withcache/ 和 regression-test-logs/rtl-nocache/ 下。
MATRIX_PRESETS = {
    "rtl-both": [
        ("rtl-withcache", "rtl-with-cache"),
        ("rtl-nocache",   "rtl-no-cache"),
    ],
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

def run_test_case(arg: Tuple[int, TestCase, Dict[str, str], Path]) -> Tuple[int, Tuple[int, str]]:
    """运行单个测试用例，返回 (索引, (返回码, 标签))
    arg 解包：
      - env: 注入给 subprocess 的环境变量副本，matrix 模式下会被覆写 VENTUS_BACKEND；
             父进程 os.environ 不受影响，避免跨模式污染或泄漏到调用方 shell。
      - log_dir: 该模式下写日志的目录（matrix 模式下每个后端一个子目录）。
    """
    index, testcase, env, log_dir = arg
    log_file = log_dir/f"{testcase.name}.log"

    with open(log_file, "w") as f:
        # 先编译。编译过程本身不依赖 VENTUS_BACKEND（backend 只在运行时 dlopen 决定），
        # 因此 matrix 模式下 compile_paths 缓存跨后端共享，第二轮自动跳过重复编译。
        if testcase.need_make:
            f.write("=== Compile Testcase ===\n")
            lock = get_compile_path_lock(testcase.path)
            with lock:
                if (testcase.path not in compile_paths) or (not compile_paths[testcase.path]):
                    try:
                        result = subprocess.run(["make"], stdout=f, stderr=f, timeout=60, cwd=testcase.path, env=env)
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
            # 把本次实际生效的 VENTUS_BACKEND 写到日志头，便于事后按后端排查
            f.write(f"VENTUS_BACKEND: {env.get('VENTUS_BACKEND', '<unset>')}\n")
            f.flush()
            result = subprocess.run(testcase.cmd, stdout=f, stderr=f, timeout=testcase.timeout * TIMEOUT_SCALE, cwd=testcase.path, env=env)
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

def parse_checklist(value: str) -> set:
    """把 "all" / "cycle" / "rtl-with-cache" / "0,1,2" 等解析成 TestCase 索引集合。
    非法输入抛 argparse.ArgumentTypeError，让 __main__ 捕获后以 parser.error 报出。
    """
    checklist = (value or "all").strip().lower()
    if checklist in REQUIRED_PRESETS:
        return set(REQUIRED_PRESETS[checklist])
    parts = [p.strip() for p in checklist.split(",") if p.strip()]
    if not parts:
        raise argparse.ArgumentTypeError("--checklist is empty")
    try:
        return set(int(p) for p in parts)
    except ValueError:
        raise argparse.ArgumentTypeError(
            "Invalid checklist value. Use preset name ({}), or comma-separated integers like 0,1,2".format(
                ", ".join(sorted(REQUIRED_PRESETS.keys()))
            )
        )

def parse_matrix(value: str) -> List[Tuple[str, set]]:
    """把 --matrix 参数解析成 [(backend, checklist_set), ...]。
    支持两种形式：
      1) 预设名，例如 "rtl-both" → 查 MATRIX_PRESETS 展开。
      2) 显式形式 "backend1:checklist1,backend2:checklist2"，
         每段的冒号左侧是要注入 VENTUS_BACKEND 的字符串，右侧是 REQUIRED_PRESETS 的 key。
    """
    value = (value or "").strip().lower()
    if value in MATRIX_PRESETS:
        pairs: List[Tuple[str, str]] = list(MATRIX_PRESETS[value])
    else:
        pairs = []
        for item in value.split(","):
            item = item.strip()
            if not item:
                continue
            if ":" not in item:
                raise argparse.ArgumentTypeError(
                    f"Invalid --matrix entry '{item}'. "
                    f"Expect 'backend:checklist', or a preset name ({', '.join(sorted(MATRIX_PRESETS.keys()))})."
                )
            backend, cl = item.split(":", 1)
            pairs.append((backend.strip(), cl.strip()))
        if not pairs:
            raise argparse.ArgumentTypeError("--matrix is empty")
    # 立即把 checklist 名解析成索引集合，便于上层统一做边界校验
    return [(backend, parse_checklist(checklist_name)) for backend, checklist_name in pairs]

def format_status(rc: int, tag: str) -> str:
    """把 (返回码, 标签) 渲染成带颜色的状态字符串。"""
    if rc == 0:
        return "\033[92mPassed\033[0m"         # 绿
    if tag == TAG_COMPILE_FAIL:
        return "\033[93mCompile Failed\033[0m" # 黄
    if tag == TAG_TIMEOUT:
        return "\033[91mTime Exceeded\033[0m"  # 红
    return "\033[91mFailed\033[0m"             # 红

def run_mode(backend: Optional[str], selected_indices: List[int], checklist_set: set,
             log_subdir: Optional[str]) \
        -> Tuple[int, List[Optional[Tuple[int, str]]], List[int]]:
    """在指定后端下跑 selected_indices 指定的测例子集，返回 (exit_code, results, checklist_failed)。

    参数：
      - backend: None 表示沿用父进程的 VENTUS_BACKEND（单模式行为）；否则将其
                 写入传给子进程的 env 副本，父进程 os.environ 不被修改。
      - selected_indices: 本模式下实际要运行的测例索引列表。
                 matrix 模式下等于 checklist（每个模式只跑自己那套测集）；
                 单模式下等于全部索引（保留旧脚本"跑全、checklist 判"的行为）。
      - checklist_set: 必须全部 pass 才算通过的测例索引集合。通常是 selected_indices
                 的子集；不在 selected_indices 里但在 checklist_set 里的索引会被判 fail
                 （理论上不应出现，作为防御性处理）。
      - log_subdir: None 表示日志写到 LOG_DIR 根下（单模式）；
                    非 None 时日志写到 LOG_DIR/<log_subdir>/（matrix 模式，一般传 backend 名）。
    """
    global pool
    mode_log_dir = LOG_DIR / log_subdir if log_subdir else LOG_DIR
    mode_log_dir.mkdir(parents=True, exist_ok=True)

    # 为子进程准备环境变量副本：在副本上写 VENTUS_BACKEND，保持父进程环境干净，
    # 避免 matrix 内多模式串行时相互污染或泄漏到用户 shell。
    env = os.environ.copy()
    if backend is not None:
        env["VENTUS_BACKEND"] = backend
    effective_backend = env.get("VENTUS_BACKEND", "default")

    pool = multiprocessing.Pool(processes=MULTIPROCESS_NUM)
    # results 用原 test_cases 长度的稀疏列表，未选中的索引保持 None，方便 print/统计统一处理
    results: List[Optional[Tuple[int, str]]] = [None] * len(test_cases)
    # 只把 selected_indices 对应的测例丢进池子，其余跳过（matrix 模式下节省 kmeans_512
    # 这种已知 timeout 的白等时间）
    iterable = [(i, test_cases[i], env, mode_log_dir) for i in selected_indices]
    total = len(iterable)

    print(f"\n>>> Running mode [{effective_backend}] "
          f"(jobs={MULTIPROCESS_NUM}, timeout scale={TIMEOUT_SCALE}, log dir={mode_log_dir}, "
          f"cases={total}/{len(test_cases)})")
    with tqdm(total=total, desc=f"Running [{effective_backend}]", unit="test") as pbar:
        for index, payload in pool.imap_unordered(run_test_case, iterable):
            rc, tag = payload
            results[index] = (rc, tag)
            # 更新进度条（统计通过/失败）
            pass_count = sum(1 for r in results if (r is not None and r[0] == 0))
            fail_count = sum(1 for r in results if (r is not None and r[0] != 0))
            pbar.set_postfix_str(f"pass={pass_count}, fail={fail_count}")
            pbar.update(1)

    # 等待所有进程完成并关闭进程池，并把全局 pool 置空，让 signal_handler 在模式间不误触
    pool.close()
    pool.join()
    pool = None

    # 未跑到的 checklist 项按 fail 处理（防御性；正常情况不会发生）
    checklist_failed = sorted(i for i in checklist_set
                              if results[i] is None or results[i][0] != 0)
    exit_code = 0 if not checklist_failed else 1
    return exit_code, results, checklist_failed

def print_mode_report(backend_name: str, selected_indices: List[int],
                      results: List[Optional[Tuple[int, str]]],
                      checklist_set: set, checklist_failed: List[int], exit_code: int):
    """打印单个模式的详细结果表 + checklist 结论。matrix 和单模式都走这里。
    只列本模式实际跑过的测例（selected_indices），未运行的索引不会出现在报告里。
    """
    print(f"\n=== Mode: {backend_name} — Test result ===")
    for i in selected_indices:
        testcase = test_cases[i]
        res = results[i]
        rc, tag = res if res is not None else (-1, "not_run")
        print(f"{i:2d} {testcase.name}: {format_status(rc, tag)}")

    # pass/fail 只在本模式实际跑过的测例里统计，未跑的索引不计入（results[i] is None）
    pass_count = sum(1 for r in results if (r is not None and r[0] == 0))
    fail_count = sum(1 for r in results if (r is not None and r[0] != 0))
    symbol = "\033[92m✔\033[0m" if exit_code == 0 else "\033[91m✘\033[0m"
    cls_sorted = sorted(checklist_set)
    if exit_code == 0:
        result_descript = f"All required testcases in checklist({cls_sorted}) passed."
    else:
        result_descript = (f"Some testcases in checklist({cls_sorted}) failed. "
                           f"Missing: {checklist_failed}")
    print(f"Summary [{backend_name}]: {pass_count} passed, {fail_count} failed. "
          f"{result_descript} {symbol}")


if __name__ == "__main__":
    # 解析命令行参数
    parser = argparse.ArgumentParser(description="Ventus regression test runner")
    parser.add_argument("-t", "--timeout-scale", type=float, default=None, help="Timeout scale (default: 1)")
    parser.add_argument("-j", "--jobs", type=int, default=None, help="Parallel multiprocess num (default: auto)")
    # 注意两个参数默认值都用 None 作为"未显式指定"的 sentinel：
    # - 都不给 → 单模式，继承父进程 VENTUS_BACKEND 环境变量，checklist 默认 all
    # - 只给 --checklist → 走单模式 checklist，保持旧用法
    # - 给 --matrix → 显式 matrix，覆盖 VENTUS_BACKEND；同时给 --checklist 也被覆盖
    parser.add_argument(
        "--checklist",
        type=str,
        default=None,
        help=(
            "Single-mode regression: which testcases must pass. "
            "Accepts a comma-separated index list (e.g. 0,2,5) "
            "or a preset name: " + ", ".join(sorted(REQUIRED_PRESETS.keys())) + ". "
            "When given without --matrix, disables the default matrix and runs single-mode. "
            "Ignored when --matrix is set."
        ),
    )
    parser.add_argument(
        "--matrix",
        type=str,
        default=None,
        help=(
            "Run multiple (VENTUS_BACKEND, checklist) combinations sequentially in one invocation. "
            "Per-mode logs go to regression-test-logs/<backend>/. "
            "Accepts a preset name (" + ", ".join(sorted(MATRIX_PRESETS.keys())) + ") "
            "or explicit form 'backend1:checklist1,backend2:checklist2'. "
            "When given, overrides VENTUS_BACKEND for each step in the matrix."
        ),
    )
    args = parser.parse_args()

    # 解析 matrix / checklist 成统一的 [(backend, checklist_set), ...] 列表。
    # 优先级：显式 --matrix > 显式 --checklist > 默认单模式（继承 VENTUS_BACKEND）。
    # - matrix 模式：每项显式指定后端 + checklist，日志按后端分子目录，每模式只跑自己那套测集
    # - 单模式退化为 [(None, checklist_set)]：沿用父进程 VENTUS_BACKEND、日志写根目录、跑全部测例
    try:
        if args.matrix is not None:
            matrix: List[Tuple[Optional[str], set]] = [
                (backend, cls) for backend, cls in parse_matrix(args.matrix)
            ]
            matrix_mode = True
        elif args.checklist is not None:
            matrix = [(None, parse_checklist(args.checklist))]
            matrix_mode = False
        else:
            # 默认：单模式，继承环境变量 VENTUS_BACKEND，跑全部测例
            matrix = [(None, parse_checklist("all"))]
            matrix_mode = False
    except argparse.ArgumentTypeError as e:
        parser.error(str(e))

    # 统一校验 checklist 索引范围
    for backend, cls in matrix:
        invalid = sorted(i for i in cls if i < 0 or i >= len(test_cases))
        if invalid:
            parser.error(
                f"checklist for backend '{backend or '<default>'}' contains invalid indices: "
                f"{invalid} (valid range: 0..{len(test_cases)-1})"
            )

    # 应用命令行参数或自动检测默认值
    if args.timeout_scale is not None:
        TIMEOUT_SCALE = args.timeout_scale
    # 自动检测合理并发数，除非用户显式指定。matrix 模式下按所有候选后端分别算一遍，
    # 取最小值，保证 rtl 类后端也能落到 cpu//8 的保守档位。
    if args.jobs is not None and args.jobs > 0:
        MULTIPROCESS_NUM = args.jobs
    else:
        MULTIPROCESS_NUM = min(
            suggest_default_jobs(len(test_cases), backend) for backend, _ in matrix
        )

    # 创建日志目录
    if not os.path.exists(LOG_DIR):
        os.makedirs(LOG_DIR)

    # 设置信号处理器以处理外部中断
    signal.signal(signal.SIGINT, signal_handler)

    # 预先为所有涉及编译的路径创建锁，避免多进程竞争创建锁导致的 race condition
    for tc in test_cases:
        if tc.need_make and tc.path not in compile_path_locks:
            compile_path_locks[tc.path] = manager.Lock()

    # 按 matrix 顺序跑每一个模式：模式间串行（避免两份 rtl 同时把 CPU 占满），
    # 模式内仍按 MULTIPROCESS_NUM 并行。compile_paths 跨模式共享，省重复编译。
    #
    # 关于"跑哪些测例"：
    # - matrix 模式：每个模式只跑自己 checklist 里那套测集；不在 checklist 里的测例
    #   在这个模式下本来就已知不通过（或跑不了），没必要白耗时间和让报告飘红。
    # - 单模式：保留旧脚本"跑全部、checklist 只当 CI 判据"的行为，不破坏既有用法。
    mode_outputs: List[Tuple[str, int, List[int], List[Optional[Tuple[int, str]]], set, List[int]]] = []
    overall_exit = 0
    for backend, cls in matrix:
        # matrix 模式下按后端名分子目录；单模式保持旧行为（日志直接写 LOG_DIR 根）。
        log_subdir = backend if matrix_mode else None
        # matrix 只跑 checklist 子集；单模式跑全部
        selected_indices = sorted(cls) if matrix_mode else list(range(len(test_cases)))
        exit_code, results, checklist_failed = run_mode(backend, selected_indices, cls, log_subdir)
        # 展示名：matrix 用设定的 backend；单模式用当前 env 里的 VENTUS_BACKEND
        backend_name = backend if backend is not None else os.environ.get("VENTUS_BACKEND", "default")
        mode_outputs.append((backend_name, exit_code, selected_indices, results, cls, checklist_failed))
        overall_exit = overall_exit or exit_code

    # 报告：每个模式打印一段；多模式再追加 Overall 汇总。
    for backend_name, exit_code, selected_indices, results, cls, checklist_failed in mode_outputs:
        print_mode_report(backend_name, selected_indices, results, cls, checklist_failed, exit_code)

    if len(mode_outputs) > 1:
        overall_symbol = "\033[92m✔\033[0m" if overall_exit == 0 else "\033[91m✘\033[0m"
        overall_desc = "All modes passed." if overall_exit == 0 else "Some modes failed."
        print(f"\n=== Overall ===\n{overall_desc} {overall_symbol}")

    if 'NOTEBOOK_BASH_KERNEL_CAPABILITIES' not in os.environ and sys.stdin.isatty():  # not in JupyterNotebook bash_kernel
        os.system("stty echo")  # spike sometimes messes up terminal echo

    sys.exit(overall_exit)
