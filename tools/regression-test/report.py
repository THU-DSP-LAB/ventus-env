import os
from dataclasses import dataclass

from .cases import TEST_CASES
from .status import TAG_COMPILE_FAIL, TAG_FLAKY, TAG_HANG, TAG_OK, TAG_TIMEOUT, summarize_runs


GITHUB_ACTIONS_ENV = "GITHUB_ACTIONS"
GITHUB_STEP_SUMMARY_ENV = "GITHUB_STEP_SUMMARY"
GITHUB_ACTIONS_TRUE = "true"
ANNOTATION_ERROR = "error"
ANNOTATION_WARNING = "warning"


@dataclass(frozen=True)
class AnnotationContext:
    backend_name: str
    selected_indices: tuple[int, ...]
    results: list[list[tuple[int, str]] | None]
    checklist_set: frozenset[int]


@dataclass(frozen=True)
class AnnotationCase:
    backend_name: str
    testcase_index: int
    testcase_name: str
    runs: list[tuple[int, str]] | None
    in_checklist: bool
    is_gvm_backend: bool


@dataclass(frozen=True)
class AnnotationGroup:
    level: str
    title: str
    cases: tuple[AnnotationCase, ...]


def format_status(rc: int, tag: str) -> str:
    if rc == 0:
        return "\033[92mPassed\033[0m"
    if tag == TAG_COMPILE_FAIL:
        return "\033[93mCompile Failed\033[0m"
    if tag == TAG_TIMEOUT:
        return "\033[91mTime Exceeded\033[0m"
    if tag == TAG_HANG:
        return "\033[91mHang\033[0m"
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
    if tag == TAG_HANG:
        return f"\033[91mHang {passed}/{total}\033[0m"
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


def print_github_annotation_summary(mode_outputs: list[tuple]) -> None:
    if not _github_annotations_enabled():
        return

    annotation_cases = _collect_annotation_cases(mode_outputs)
    groups = _build_annotation_groups(annotation_cases)
    for group in groups:
        print(_format_github_annotation(group.level, group.title, _annotation_group_message(group)))
    _append_github_step_summary(groups)


def _collect_annotation_cases(mode_outputs: list[tuple]) -> tuple[AnnotationCase, ...]:
    cases: list[AnnotationCase] = []
    for mode_output in mode_outputs:
        context = _annotation_context_from_mode_output(mode_output)
        for annotation_case in _failed_annotation_cases(context):
            if _annotation_level(annotation_case) is not None:
                cases.append(annotation_case)
    return tuple(cases)


def _annotation_context_from_mode_output(mode_output: tuple) -> AnnotationContext:
    backend_name, _, selected_indices, results, checklist_set, _, _ = mode_output
    return AnnotationContext(
        backend_name=backend_name,
        selected_indices=tuple(selected_indices),
        results=results,
        checklist_set=frozenset(checklist_set),
    )


def _build_annotation_groups(annotation_cases: tuple[AnnotationCase, ...]) -> tuple[AnnotationGroup, ...]:
    error_cases: list[AnnotationCase] = []
    warning_cases: list[AnnotationCase] = []
    for annotation_case in annotation_cases:
        level = _annotation_level(annotation_case)
        if level == ANNOTATION_ERROR:
            error_cases.append(annotation_case)
        elif level == ANNOTATION_WARNING:
            warning_cases.append(annotation_case)

    groups: list[AnnotationGroup] = []
    if error_cases:
        groups.append(AnnotationGroup(
            level=ANNOTATION_ERROR,
            title="GVM checklist failed",
            cases=tuple(error_cases),
        ))
    if warning_cases:
        groups.append(AnnotationGroup(
            level=ANNOTATION_WARNING,
            title="Non-checklist tests failed",
            cases=tuple(warning_cases),
        ))
    return tuple(groups)


def _annotation_group_message(group: AnnotationGroup) -> str:
    case_word = "case" if len(group.cases) == 1 else "cases"
    lines = [f"{len(group.cases)} {case_word} failed"]
    lines.extend(_annotation_case_line(annotation_case) for annotation_case in group.cases)
    return "\n".join(lines)


def _append_github_step_summary(groups: tuple[AnnotationGroup, ...]) -> None:
    if not groups:
        return

    summary_path = os.environ.get(GITHUB_STEP_SUMMARY_ENV)
    if not summary_path:
        return

    with open(summary_path, "a", encoding="utf-8") as summary_file:
        summary_file.write("\n## Regression Test Annotations\n\n")
        summary_file.write("| Severity | Backend | Testcase | Status | Checklist |\n")
        summary_file.write("|---|---|---|---|---|\n")
        for group in groups:
            for annotation_case in group.cases:
                summary_file.write(_summary_table_row(group.level, annotation_case))


def _summary_table_row(level: str, annotation_case: AnnotationCase) -> str:
    checklist_value = "yes" if annotation_case.in_checklist else "no"
    testcase = f"{annotation_case.testcase_index} {annotation_case.testcase_name}"
    return (
        f"| {level} | {_escape_markdown_table_cell(annotation_case.backend_name)} "
        f"| {_escape_markdown_table_cell(testcase)} "
        f"| {_escape_markdown_table_cell(_annotation_status(annotation_case.runs))} "
        f"| {checklist_value} |\n"
    )


def _escape_markdown_table_cell(value: str) -> str:
    return value.replace("\\", "\\\\").replace("|", "\\|").replace("\n", "<br>")


def _failed_annotation_cases(context: AnnotationContext) -> list[AnnotationCase]:
    is_gvm_backend = _is_gvm_backend_name(context.backend_name)
    annotation_cases: list[AnnotationCase] = []
    for index in context.selected_indices:
        runs = context.results[index]
        if _case_passed(runs):
            continue
        annotation_cases.append(AnnotationCase(
            backend_name=context.backend_name,
            testcase_index=index,
            testcase_name=TEST_CASES[index].name,
            runs=runs,
            in_checklist=index in context.checklist_set,
            is_gvm_backend=is_gvm_backend,
        ))
    return annotation_cases


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


def _github_annotations_enabled() -> bool:
    return os.environ.get(GITHUB_ACTIONS_ENV, "").lower() == GITHUB_ACTIONS_TRUE


def _is_gvm_backend_name(backend_name: str) -> bool:
    return backend_name.startswith("gvm-") or backend_name.endswith("-gvm")


def _case_passed(runs: list[tuple[int, str]] | None) -> bool:
    return runs is not None and summarize_runs(runs)[2] == TAG_OK


def _annotation_level(annotation_case: AnnotationCase) -> str | None:
    if annotation_case.is_gvm_backend and annotation_case.in_checklist:
        return ANNOTATION_ERROR
    if not annotation_case.in_checklist:
        return ANNOTATION_WARNING
    return None


def _annotation_case_line(annotation_case: AnnotationCase) -> str:
    checklist_value = "yes" if annotation_case.in_checklist else "no"
    status = _annotation_status(annotation_case.runs)
    return f"{annotation_case.backend_name}: {annotation_case.testcase_index} {annotation_case.testcase_name} - {status}, checklist={checklist_value}"


def _annotation_status(runs: list[tuple[int, str]] | None) -> str:
    if runs is None:
        return "not_run 0/0"
    passed, total, tag = summarize_runs(runs)
    if tag == TAG_FLAKY:
        return f"flaky {passed}/{total}"
    return f"{tag} {passed}/{total}"


def _format_github_annotation(level: str, title: str, message: str) -> str:
    return f"::{level} title={_escape_annotation_property(title)}::{_escape_annotation_data(message)}"


def _escape_annotation_property(value: str) -> str:
    return _escape_annotation_data(value).replace(":", "%3A").replace(",", "%2C")


def _escape_annotation_data(value: str) -> str:
    return value.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


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
