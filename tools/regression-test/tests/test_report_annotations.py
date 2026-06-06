import importlib.util
import io
import os
import sys
import tempfile
import unittest
from dataclasses import dataclass
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock


PACKAGE_NAME = "ventus_regression_test_report_annotation_tests"
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


@dataclass(frozen=True)
class RenderConfig:
    mode_outputs: tuple[tuple, ...]
    github_actions: bool = True
    step_summary: bool = False


class GithubAnnotationTests(unittest.TestCase):
    def test_failures_are_aggregated_by_severity(self):
        output, summary = self._render_annotations(RenderConfig(
            mode_outputs=(
                self._mode_output(
                    backend_name="rtlsim-no-cache-gvm",
                    checklist={0},
                    results_by_index={
                        0: [(1, "failed")],
                        1: [(9999, "timeout")],
                    },
                    checklist_failed=[0],
                    exit_code=1,
                ),
                self._mode_output(
                    backend_name="rtlsim-with-cache-gvm",
                    checklist={2},
                    results_by_index={2: [(0, "ok"), (1, "failed")]},
                    checklist_failed=[2],
                    exit_code=1,
                ),
            ),
            step_summary=True,
        ))

        self.assertEqual(output.count("::error title=GVM checklist failed::"), 1)
        self.assertIn("2 cases failed%0A", output)
        self.assertIn("rtlsim-no-cache-gvm: 0 matadd - failed 0/1, checklist=yes", output)
        self.assertIn("rtlsim-with-cache-gvm: 2 gaussian_16 - flaky 1/2, checklist=yes", output)

        self.assertEqual(output.count("::warning title=Non-checklist tests failed::"), 1)
        self.assertIn("rtlsim-no-cache-gvm: 1 vecadd_4096 - timeout 0/1, checklist=no", output)

        self.assertIn("| error | rtlsim-no-cache-gvm | 0 matadd | failed 0/1 | yes |", summary)
        self.assertIn("| error | rtlsim-with-cache-gvm | 2 gaussian_16 | flaky 1/2 | yes |", summary)
        self.assertIn("| warning | rtlsim-no-cache-gvm | 1 vecadd_4096 | timeout 0/1 | no |", summary)

    def test_non_gvm_checklist_failure_is_not_annotated_by_report(self):
        output, _ = self._render_annotations(RenderConfig(
            mode_outputs=(self._mode_output(
                backend_name="rtlsim-no-cache",
                checklist={0},
                results_by_index={
                    0: [(1, "failed")],
                    1: [(1, "failed")],
                },
                checklist_failed=[0],
                exit_code=1,
            ),),
        ))

        self.assertNotIn("::error", output)
        self.assertIn("::warning title=Non-checklist tests failed::", output)
        self.assertIn("rtlsim-no-cache: 1 vecadd_4096 - failed 0/1, checklist=no", output)

    def test_annotations_are_only_emitted_in_github_actions(self):
        output, _ = self._render_annotations(RenderConfig(
            mode_outputs=(self._mode_output(
                backend_name="rtlsim-no-cache-gvm",
                checklist={0},
                results_by_index={0: [(1, "failed")]},
                checklist_failed=[0],
                exit_code=1,
            ),),
            github_actions=False,
        ))

        self.assertNotIn("::error", output)
        self.assertNotIn("::warning", output)

    def _mode_output(
        self,
        *,
        backend_name: str,
        checklist: set[int],
        results_by_index: dict[int, list[tuple[int, str]]],
        checklist_failed: list[int],
        exit_code: int,
    ) -> tuple:
        cases = load_module("cases")
        results = [None] * len(cases.TEST_CASES)
        for index, runs in results_by_index.items():
            results[index] = runs
        selected_indices = sorted(results_by_index)
        return backend_name, exit_code, selected_indices, results, checklist, checklist_failed, 1

    def _render_annotations(self, config: RenderConfig) -> tuple[str, str]:
        report = load_module("report")
        output = io.StringIO()
        env = {"GITHUB_ACTIONS": "true"} if config.github_actions else {}
        with tempfile.TemporaryDirectory() as tmpdir:
            summary_path = Path(tmpdir) / "summary.md"
            if config.step_summary:
                env["GITHUB_STEP_SUMMARY"] = str(summary_path)
            with mock.patch.dict(os.environ, env, clear=True), redirect_stdout(output):
                report.print_github_annotation_summary(list(config.mode_outputs))
            summary = summary_path.read_text(encoding="utf-8") if summary_path.exists() else ""
        return output.getvalue(), summary


if __name__ == "__main__":
    unittest.main()
