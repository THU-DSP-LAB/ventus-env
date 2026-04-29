from .cases import TEST_CASES
from .status import TAG_COMPILE_FAIL, TAG_OK, TAG_TIMEOUT, summarize_runs


def format_status(rc: int, tag: str) -> str:
    if rc == 0:
        return "\033[92mPassed\033[0m"
    if tag == TAG_COMPILE_FAIL:
        return "\033[93mCompile Failed\033[0m"
    if tag == TAG_TIMEOUT:
        return "\033[91mTime Exceeded\033[0m"
    return "\033[91mFailed\033[0m"


def format_run_status(runs: list[tuple[int, str]]) -> str:
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


def print_mode_report(
    backend_name: str,
    selected_indices: list[int],
    results: list[list[tuple[int, str]] | None],
    checklist_set: set[int],
    checklist_failed: list[int],
    exit_code: int,
) -> None:
    print(f"\n=== Mode: {backend_name} - Test result ===")
    for index in selected_indices:
        testcase = TEST_CASES[index]
        runs = results[index]
        status = format_status(-1, "not_run") if runs is None else format_run_status(runs)
        print(f"{index:2d} {testcase.name}: {status}")

    pass_count, fail_count, flaky_count = _count_report_statuses(results)
    symbol = "\033[92mOK\033[0m" if exit_code == 0 else "\033[91mFAIL\033[0m"
    result_description = _result_description(checklist_set, checklist_failed, results, exit_code)
    print(
        f"Summary [{backend_name}]: {pass_count} passed, {fail_count} failed, "
        f"{flaky_count} flaky. {result_description} {symbol}"
    )

    for testcase_name in _extra_passed(selected_indices, checklist_set, results):
        print(f"\033[92mNote: testcase {testcase_name} passed but is not in checklist for backend [{backend_name}].\033[0m")


def _count_report_statuses(results: list[list[tuple[int, str]] | None]) -> tuple[int, int, int]:
    pass_count = fail_count = flaky_count = 0
    for runs in results:
        if runs is None:
            continue
        _, _, tag = summarize_runs(runs)
        if tag == TAG_OK:
            pass_count += 1
        elif tag == "flaky":
            flaky_count += 1
        else:
            fail_count += 1
    return pass_count, fail_count, flaky_count


def _result_description(
    checklist_set: set[int],
    checklist_failed: list[int],
    results: list[list[tuple[int, str]] | None],
    exit_code: int,
) -> str:
    checklist_sorted = sorted(checklist_set)
    if exit_code == 0:
        return f"All required testcases in checklist({checklist_sorted}) passed all runs."

    flaky_in_checklist = sorted(
        index
        for index in checklist_failed
        if results[index] is not None and summarize_runs(results[index])[2] == "flaky"
    )
    hard_fail_in_checklist = sorted(index for index in checklist_failed if index not in flaky_in_checklist)
    bits = []
    if hard_fail_in_checklist:
        bits.append(f"hard-fail={hard_fail_in_checklist}")
    if flaky_in_checklist:
        bits.append(f"flaky={flaky_in_checklist}")
    return f"Some testcases in checklist({checklist_sorted}) failed. " + ", ".join(bits)


def _extra_passed(
    selected_indices: list[int],
    checklist_set: set[int],
    results: list[list[tuple[int, str]] | None],
) -> list[str]:
    return [
        TEST_CASES[index].name
        for index in selected_indices
        if index not in checklist_set
        and results[index] is not None
        and summarize_runs(results[index])[2] == TAG_OK
    ]
