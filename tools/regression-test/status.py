TAG_OK = "ok"
TAG_FAIL = "failed"
TAG_TIMEOUT = "timeout"
TAG_HANG = "hang"
TAG_COMPILE_FAIL = "compile_failed"
TAG_FLAKY = "flaky"


def summarize_runs(runs: list[tuple[int, str]]) -> tuple[int, int, str]:
    total = len(runs)
    passed = sum(1 for rc, _ in runs if rc == 0)
    if passed == total:
        return passed, total, TAG_OK
    if passed == 0:
        tags = {tag for rc, tag in runs if rc != 0}
        if len(tags) == 1:
            return passed, total, tags.pop()
        return passed, total, TAG_FAIL
    return passed, total, TAG_FLAKY
