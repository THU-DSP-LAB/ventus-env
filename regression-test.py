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

# 带 cache 的 RTL 后端默认强制重跑次数。
# 自 b1bce67 起 GVM/RTL 引入随机初始化，with-cache 路径上观察到测例通过率不稳定
# （X 态在某些初始路径上传播导致选择器死锁/写丢失等）。
#
# 为什么是 10 而不是 5：2026-04-25 backprop_1024 在 5 次循环里凑巧 5/5 通过，
# 但跑 10 次只过 7/10——5 次循环对 70% 通过率级别的 flaky 测例容易误判为稳定。
# 10 次循环把"5/5 误判稳态"的概率从 ~17% (0.7^5) 压到 ~3% (0.7^10)，更可靠。
# 更高重复数（如 20）收益边际递减、wall-clock 翻倍，不划算。
WITHCACHE_DEFAULT_REPEAT = 10

def detect_default_repeat(backend: Optional[str]) -> int:
    """根据后端推断默认 repeat 次数。
    含 'withcache' 的后端默认 5 次；其它后端 1 次。matrix / 单模式都按这个规则各自判一次。
    """
    effective = backend if backend else os.environ.get('VENTUS_BACKEND', '')
    return WITHCACHE_DEFAULT_REPEAT if 'withcache' in effective.lower() else 1

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
    TestCase(name="pathfinder_4x32_h1", path=RODINIA_DIR/"opencl/pathfinder", cmd=["./pathfinder.out", "-c", "32", "-r", "4", "-h", "1", "-p", "0", "-d", "0"]),
    TestCase(name="hotspot_64_1_1", path=RODINIA_DIR/"opencl/hotspot", cmd=["./hotspot.out", "64", "1", "1", "../../data/hotspot/temp_64", "../../data/hotspot/power_64", "output.txt", "-p", "0", "-d", "0", "--ref", "nvidia-ref-64-1-1.txt"]),
    TestCase(name="hotspot3D_64x8_i1", path=RODINIA_DIR/"opencl/hotspot3D", cmd=["./hotspot3D.out", "-n", "64", "-l", "8", "-i", "1", "-f", "../../data/hotspot3D/power_64x8", "../../data/hotspot3D/temp_64x8", "output.txt", "-p", "0", "-d", "0"]),
    TestCase(name="nw_16"        , path=RODINIA_DIR/"opencl/nw"         , cmd=["./nw.out", "16", "10", "./nw.cl", "-p", "0", "-d", "0"]),
    TestCase(name="heartwall_1"  , path=RODINIA_DIR/"opencl/heartwall"  , cmd=["./run"], timeout=180),
    TestCase(name="srad_1_1_64"  , path=RODINIA_DIR/"opencl/srad"       , cmd=["./run"], timeout=180),
    TestCase(name="lud_64"       , path=RODINIA_DIR/"opencl/lud"        , cmd=["./lud.out", "-v", "-i", "../../data/lud/64.dat", "-p", "0", "-d", "0"], timeout=180),
    TestCase(name="mnist_conv_small", path=VENTUS_TESTCASE_DIR/"_get_case/MNIST_conv_small", cmd=["./conv.out"]),
    TestCase(name="mnist"           , path=VENTUS_TESTCASE_DIR/"_get_case/MNIST"           , cmd=["./nn_forward.out"]),
    TestCase(name="lds_corruption", path=VENTUS_TESTCASE_DIR/"others/lds_corruption", cmd=["./run"], timeout=180),
    # 以下测例可以跑通，但十分缓慢
    # TestCase(name="bfs_65536"    , path=RODINIA_DIR/"opencl/bfs"        , cmd=["./bfs.out", "../../data/bfs/graph65536.txt"], timeout=1000),
    # TestCase(name="b+tree_1024"  , path=RODINIA_DIR/"opencl/b+tree"     , cmd=["./b+tree.out", "file", "../../data/b+tree/mil.txt", "command", "../../data/b+tree/command_1024.txt", "--ref", "output_1024.nvidia.txt"], timeout=300),
    # TestCase(name="kmeans_4096"  , path=RODINIA_DIR/"opencl/kmeans"     , cmd=["./kmeans.out", "-o", "-r", "-i", "../../data/kmeans/4096_34f.txt", "-g", "nvidia_result_4096_34f_k5", "-p", "0", "-d", "0"], timeout=1000),
    # TestCase(name="mnist_conv"      , path=VENTUS_TESTCASE_DIR/"_get_case/MNIST_conv"      , cmd=["./conv.out"]),
]

# Checklist 预设：一次运行中"必须通过"的 TestCase 索引子集。
# 任一索引对应的测例未 pass 就以非零退出码返回，用于 CI 卡控。
# - isa/sbt:        旧回归集沿用当前稳定基线；本轮新补入的 Rodinia case 只把已验证通过者加入
# - cycle:          目前能稳定通过的 case；新补入中仅 pathfinder / hotspot3D 已验证通过
# - rtl-with-cache: 带 Cache 版本 RTL 稳定通过集合；不包含 b+tree_128 / bfs_4096 / kmeans_512 / lud_64
# - rtl-no-cache:   不带 Cache 版本 RTL 稳定通过集合；不包含 lud_64
REQUIRED_PRESETS = {
    "all":            list(range(len(test_cases))),
    "isa":            [0,1,2,3,4,5,6,7,8,9,11,12,13,15,16,17,18],
    "sbt":            [0,1,2,4,5,6,7,8,9,10,11,13,14,16,17,18],
    "cycle":          [0,1,2,3,4,5,6,7,8,9,11,16,17,18],
    "rtl-with-cache": [0,1,2,4,6,7,9,10,11,12,13,14,16,17,18],
    "rtl-no-cache":   [0,1,2,3,4,5,6,7,8,9,10,11,12,13,14,16,17,18],
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

def run_test_case(arg: Tuple[int, TestCase, Dict[str, str], Path, int]) -> Tuple[int, List[Tuple[int, str]]]:
    """运行单个测试用例 repeat 次，返回 (索引, [(rc1, tag1), (rc2, tag2), ...])
    arg 解包：
      - env: 注入给 subprocess 的环境变量副本，matrix 模式下会被覆写 VENTUS_BACKEND；
             父进程 os.environ 不受影响，避免跨模式污染或泄漏到调用方 shell。
      - log_dir: 该模式下写日志的目录（matrix 模式下每个后端一个子目录）。
      - repeat: 重跑次数。repeat==1 时日志写到 <name>.log（保持旧布局）；
                repeat>1 时每次单独写到 <name>.run<i>.log，方便定位是哪一次失败。
    编译只做一次。若编译失败，把同一条 (2, TAG_COMPILE_FAIL) 复制 repeat 份返回，
    保持外层"按 run 数计数"的统计口径一致。
    """
    index, testcase, env, log_dir, repeat = arg
    multi = repeat > 1

    # 编译输出：单次跑时与 run 日志合并到 <name>.log（保留旧行为）；
    # 多次跑时单独写到 <name>.compile.log，避免被某次 run 的 stdout 淹没。
    if multi:
        compile_log_path = log_dir / f"{testcase.name}.compile.log"
    else:
        compile_log_path = log_dir / f"{testcase.name}.log"

    if testcase.need_make:
        with open(compile_log_path, "w") as f:
            f.write("=== Compile Testcase ===\n")
            lock = get_compile_path_lock(testcase.path)
            with lock:
                if (testcase.path not in compile_paths) or (not compile_paths[testcase.path]):
                    try:
                        result = subprocess.run(["make"], stdout=f, stderr=f, timeout=60, cwd=testcase.path, env=env)
                        if result.returncode != 0:
                            f.write("Compile Failed\n")
                            return index, [(2, TAG_COMPILE_FAIL)] * repeat
                        f.write("Compile OK\n")
                        compile_paths[testcase.path] = True  # 标记为已编译
                    except subprocess.TimeoutExpired:
                        f.write("Compile Timeout, Failed\n")
                        return index, [(2, TAG_COMPILE_FAIL)] * repeat
                    except Exception as e:
                        f.write(f"Compile Failed: {e}\n")
                        return index, [(2, TAG_COMPILE_FAIL)] * repeat
                else:
                    f.write("Already Compiled, Skipping...\n")

    # 运行测试 repeat 次。每次单独 open log，多次 run 时各 run 之间日志互不覆盖。
    runs: List[Tuple[int, str]] = []
    for run_idx in range(1, repeat + 1):
        if multi:
            run_log_path = log_dir / f"{testcase.name}.run{run_idx}.log"
            mode = "w"
        else:
            run_log_path = compile_log_path  # 复用 <name>.log，保留旧布局
            mode = "a" if testcase.need_make else "w"

        with open(run_log_path, mode) as f:
            f.write(f"=== Run Test ({run_idx}/{repeat}) ===\n")
            f.flush()
            try:
                f.write(f"TestCase {index}: {testcase.name} begin (run {run_idx}/{repeat})...\nCOMMAND: ")
                f.write(format_list_with_quotes(testcase.cmd))
                f.write("\n")
                # 把本次实际生效的 VENTUS_BACKEND 写到日志头，便于事后按后端排查
                f.write(f"VENTUS_BACKEND: {env.get('VENTUS_BACKEND', '<unset>')}\n")
                f.flush()
                result = subprocess.run(testcase.cmd, stdout=f, stderr=f, timeout=testcase.timeout * TIMEOUT_SCALE, cwd=testcase.path, env=env)
                rc = result.returncode
                tag = TAG_OK if rc == 0 else TAG_FAIL
                runs.append((rc, tag))
            except subprocess.TimeoutExpired:
                f.write("\nTestcase execution timeout, Failed\n")
                # 用一个超出常规范围的返回码以避免与被测程序冲突
                runs.append((9999, TAG_TIMEOUT))
            except Exception as e:
                f.write(f"\nTestcase execution failed with exception: {e}\n")
                runs.append((9998, TAG_FAIL))
    return index, runs

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

def summarize_runs(runs: List[Tuple[int, str]]) -> Tuple[int, int, str]:
    """把多次运行结果聚合成 (passed, total, tag)。
    tag 用于颜色判定：
      - 全 pass → TAG_OK
      - 全失败且原因一致 → 该原因（compile_failed / timeout / failed）
      - 部分通过部分失败 → "flaky"（黄色，对应随机初始化下的不稳定测例）
    """
    total = len(runs)
    passed = sum(1 for rc, _ in runs if rc == 0)
    if passed == total:
        return passed, total, TAG_OK
    if passed == 0:
        # 全部失败：若所有失败原因一致，沿用原 tag；否则归为 failed
        tags = {tag for rc, tag in runs if rc != 0}
        if len(tags) == 1:
            return passed, total, tags.pop()
        return passed, total, TAG_FAIL
    return passed, total, "flaky"

def format_run_status(runs: List[Tuple[int, str]]) -> str:
    """渲染 'Passed K/N' / 'Flaky K/N' / 'Failed 0/N' 等带颜色字符串。"""
    passed, total, tag = summarize_runs(runs)
    if tag == TAG_OK:
        return f"\033[92mPassed {passed}/{total}\033[0m"
    if tag == "flaky":
        return f"\033[93mFlaky {passed}/{total}\033[0m"
    if tag == TAG_COMPILE_FAIL:
        return f"\033[93mCompile Failed {passed}/{total}\033[0m"
    if tag == TAG_TIMEOUT:
        return f"\033[91mTime Exceeded {passed}/{total}\033[0m"
    return f"\033[91mFailed {passed}/{total}\033[0m"

def run_mode(backend: Optional[str], selected_indices: List[int], checklist_set: set,
             log_subdir: Optional[str], repeat: int) \
        -> Tuple[int, List[Optional[List[Tuple[int, str]]]], List[int]]:
    """在指定后端下跑 selected_indices 指定的测例子集，每个测例重跑 repeat 次。
    返回 (exit_code, results, checklist_failed)。

    参数：
      - backend: None 表示沿用父进程的 VENTUS_BACKEND（单模式行为）；否则将其
                 写入传给子进程的 env 副本，父进程 os.environ 不被修改。
      - selected_indices: 本模式下实际要运行的测例索引列表。
                 matrix 模式下等于 checklist（每个模式只跑自己那套测集）；
                 单模式下等于全部索引（保留旧脚本"跑全、checklist 判"的行为）。
      - checklist_set: 必须全部 run 都 pass 才算通过的测例索引集合。
      - log_subdir: None 表示日志写到 LOG_DIR 根下（单模式）；
                    非 None 时日志写到 LOG_DIR/<log_subdir>/（matrix 模式，一般传 backend 名）。
      - repeat: 每个测例重跑次数。任何一次失败即把该测例判 fail（用于过滤随机初始化下的偶发通过）。

    results 形状：List[Optional[List[(rc, tag)]]]，每项是该测例 repeat 次运行的全部结果；
    未选中的索引保持 None。
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
    results: List[Optional[List[Tuple[int, str]]]] = [None] * len(test_cases)
    # 只把 selected_indices 对应的测例丢进池子，其余跳过（matrix 模式下节省 kmeans_512
    # 这种已知 timeout 的白等时间）
    iterable = [(i, test_cases[i], env, mode_log_dir, repeat) for i in selected_indices]
    total = len(iterable)

    print(f"\n>>> Running mode [{effective_backend}] "
          f"(jobs={MULTIPROCESS_NUM}, timeout scale={TIMEOUT_SCALE}, log dir={mode_log_dir}, "
          f"cases={total}/{len(test_cases)}, repeat={repeat})")
    with tqdm(total=total, desc=f"Running [{effective_backend}]", unit="test") as pbar:
        for index, runs in pool.imap_unordered(run_test_case, iterable):
            results[index] = runs
            # 更新进度条：以测例为单位计数（每个 testcase 算一个 unit），
            # 区分稳定通过 / 失败 / flaky（部分通过）三档，让 with-cache 偶发问题立刻可见。
            pass_count = 0
            fail_count = 0
            flaky_count = 0
            for r in results:
                if r is None:
                    continue
                p, t, tag = summarize_runs(r)
                if tag == TAG_OK:
                    pass_count += 1
                elif tag == "flaky":
                    flaky_count += 1
                else:
                    fail_count += 1
            pbar.update(1)
            pbar.set_postfix_str(f"pass={pass_count}, fail={fail_count}, flaky={flaky_count}")

    # 等待所有进程完成并关闭进程池，并把全局 pool 置空，让 signal_handler 在模式间不误触
    pool.close()
    pool.join()
    pool = None

    # checklist 判定口径：必须全部 run 都 pass 才算过；任意一次失败 / 未跑到 都判 fail
    def _all_passed(idx: int) -> bool:
        r = results[idx]
        return r is not None and all(rc == 0 for rc, _ in r)

    checklist_failed = sorted(i for i in checklist_set if not _all_passed(i))
    exit_code = 0 if not checklist_failed else 1
    return exit_code, results, checklist_failed

def print_mode_report(backend_name: str, selected_indices: List[int],
                      results: List[Optional[List[Tuple[int, str]]]],
                      checklist_set: set, checklist_failed: List[int], exit_code: int):
    """打印单个模式的详细结果表 + checklist 结论。matrix 和单模式都走这里。
    只列本模式实际跑过的测例（selected_indices），未运行的索引不会出现在报告里。
    每行展示 'Passed K/N' / 'Flaky K/N' / 'Failed 0/N'，K=通过次数，N=总跑数。
    """
    print(f"\n=== Mode: {backend_name} — Test result ===")
    for i in selected_indices:
        testcase = test_cases[i]
        runs = results[i]
        if runs is None:
            status = format_status(-1, "not_run")
        else:
            status = format_run_status(runs)
        print(f"{i:2d} {testcase.name}: {status}")

    # 三档统计：稳定通过 / 失败 / flaky；只统计本模式跑过的测例
    pass_count = 0
    fail_count = 0
    flaky_count = 0
    for r in results:
        if r is None:
            continue
        _, _, tag = summarize_runs(r)
        if tag == TAG_OK:
            pass_count += 1
        elif tag == "flaky":
            flaky_count += 1
        else:
            fail_count += 1

    symbol = "\033[92m✔\033[0m" if exit_code == 0 else "\033[91m✘\033[0m"
    cls_sorted = sorted(checklist_set)
    if exit_code == 0:
        result_descript = f"All required testcases in checklist({cls_sorted}) passed all runs."
    else:
        # 标注 flaky / fail 各占 checklist 多少，便于一眼定位是哪类失败
        flaky_in_cls = sorted(i for i in checklist_failed
                              if results[i] is not None
                              and summarize_runs(results[i])[2] == "flaky")
        hard_fail_in_cls = sorted(i for i in checklist_failed if i not in flaky_in_cls)
        bits = []
        if hard_fail_in_cls:
            bits.append(f"hard-fail={hard_fail_in_cls}")
        if flaky_in_cls:
            bits.append(f"flaky={flaky_in_cls}")
        result_descript = (f"Some testcases in checklist({cls_sorted}) failed. "
                           + ", ".join(bits))
    print(f"Summary [{backend_name}]: {pass_count} passed, {fail_count} failed, "
          f"{flaky_count} flaky. {result_descript} {symbol}")

    extra_passed = [
        test_cases[i].name for i in selected_indices
        if i not in checklist_set
        and results[i] is not None
        and summarize_runs(results[i])[2] == TAG_OK
    ]
    for testcase_name in extra_passed:
        print(f"\033[92mNote: testcase {testcase_name} passed but is not in checklist for backend [{backend_name}].\033[0m")


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
    parser.add_argument(
        "--repeat",
        type=int,
        default=None,
        help=(
            "Repeat each testcase N times; the case is considered passed only if all N "
            "runs pass (any failure marks it as failed/flaky). Per-iteration logs are "
            "written to <name>.run<i>.log so a flaky run can be located. "
            f"Default: {WITHCACHE_DEFAULT_REPEAT} for backends containing 'withcache' "
            "(applied even without this flag), 1 otherwise. Explicitly setting this flag "
            "overrides the default for all modes."
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

    # 校验 --repeat：必须 >= 1
    if args.repeat is not None and args.repeat < 1:
        parser.error(f"--repeat must be >= 1, got {args.repeat}")

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
    mode_outputs: List[Tuple[str, int, List[int], List[Optional[List[Tuple[int, str]]]], set, List[int], int]] = []
    overall_exit = 0
    for backend, cls in matrix:
        # matrix 模式下按后端名分子目录；单模式保持旧行为（日志直接写 LOG_DIR 根）。
        log_subdir = backend if matrix_mode else None
        # matrix 只跑 checklist 子集；单模式跑全部
        selected_indices = sorted(cls) if matrix_mode else list(range(len(test_cases)))
        # 每模式独立判 repeat：显式 --repeat 覆盖全局；否则 with-cache 后端默认 5 次，其它 1 次。
        # 这样 matrix 同时跑 withcache + nocache 时，前者重跑、后者单跑，开销可控。
        repeat = args.repeat if args.repeat is not None else detect_default_repeat(backend)
        exit_code, results, checklist_failed = run_mode(backend, selected_indices, cls, log_subdir, repeat)
        # 展示名：matrix 用设定的 backend；单模式用当前 env 里的 VENTUS_BACKEND
        backend_name = backend if backend is not None else os.environ.get("VENTUS_BACKEND", "default")
        mode_outputs.append((backend_name, exit_code, selected_indices, results, cls, checklist_failed, repeat))
        overall_exit = overall_exit or exit_code

    # 报告：每个模式打印一段；多模式再追加 Overall 汇总。
    for backend_name, exit_code, selected_indices, results, cls, checklist_failed, repeat in mode_outputs:
        if repeat > 1:
            print(f"\n[{backend_name}] repeat={repeat} per testcase")
        print_mode_report(backend_name, selected_indices, results, cls, checklist_failed, exit_code)

    if len(mode_outputs) > 1:
        overall_symbol = "\033[92m✔\033[0m" if overall_exit == 0 else "\033[91m✘\033[0m"
        overall_desc = "All modes passed." if overall_exit == 0 else "Some modes failed."
        print(f"\n=== Overall ===\n{overall_desc} {overall_symbol}")

    if 'NOTEBOOK_BASH_KERNEL_CAPABILITIES' not in os.environ and sys.stdin.isatty():  # not in JupyterNotebook bash_kernel
        os.system("stty echo")  # spike sometimes messes up terminal echo

    sys.exit(overall_exit)
