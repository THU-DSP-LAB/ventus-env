"""Guard against treating unrelated failures as successful negative controls."""
import unittest
from .workflow import validate_numeric


class AcceptanceTests(unittest.TestCase):
    def test_crash_is_not_a_negative_pass(self):
        item = {'name': 'wrong-neighbor', 'args': [], 'expected_returncode': 1}
        with self.assertRaises(RuntimeError):
            validate_numeric(item, 1, {'status': 'FAIL', 'summary': {}}, 'driver crashed')

    def test_timeout_is_not_a_negative_pass(self):
        item = {'name': 'wrong-neighbor', 'args': [], 'expected_returncode': 1}
        with self.assertRaises(RuntimeError):
            validate_numeric(item, 1, {'status': 'FAIL', 'timeout': True,
                                     'summary': {'quad_gradient_failed': '240'}}, '')

    def test_explicit_rejection_is_required(self):
        item = {'name': 'no-opt-in', 'args': ['--quad-without-opt-in'], 'expected_returncode': 1}
        with self.assertRaises(RuntimeError):
            validate_numeric(item, 1, {'status': 'FAIL'}, 'missing shared library')
        validate_numeric(item, 1, {'status': 'FAIL'},
                         'requires the private complete same-warp quad experiment')

    def test_coverage_loss_fails_even_with_pass_marker(self):
        item = {'name': 'positive', 'args': [], 'expected_summary': {'components': '512'}}
        with self.assertRaises(RuntimeError):
            validate_numeric(item, 0, {'status': 'PASS', 'summary': {'components': '0'}}, '')

    def test_real_negative_signal(self):
        item = {'name': 'wrong-neighbor', 'args': [], 'expected_returncode': 1}
        validate_numeric(item, 1, {'status': 'FAIL', 'summary': {'quad_gradient_failed': '240'}}, '')


if __name__ == '__main__':
    unittest.main()
