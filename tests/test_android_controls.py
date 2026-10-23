"""A broken test harness must never count as detecting a deliberate defect."""
import unittest

from scripts.test_android_controls import validate_control


class AndroidMutationEvidence(unittest.TestCase):
    def check(self, *, counts=None, evidence=None, log='Expected reading assertion', code=1):
        validate_control('reading', 'reading_test', 'Expected reading assertion', code,
                         counts or {'run': 1, 'failures': 1, 'errors': 0, 'skipped': 0},
                         [{'test': 'reading_test'}] if evidence is None else evidence, log)

    def test_only_the_intended_assertion_is_accepted(self):
        self.check()
        with self.assertRaisesRegex(ValueError, 'exit=0'):
            self.check(code=0)
        with self.assertRaisesRegex(ValueError, 'Browser startup failed'):
            self.check(log='AssertionError: Browser startup failed')

    def test_driver_errors_and_skips_are_reported_as_harness_failures(self):
        for values in ({'run': 1, 'failures': 1, 'errors': 1, 'skipped': 0},
                       {'run': 1, 'failures': 1, 'errors': 0, 'skipped': 1}):
            with self.subTest(values=values), self.assertRaisesRegex(ValueError, 'counts='):
                self.check(counts=values)

    def test_stale_evidence_and_zero_test_runs_cannot_satisfy_a_control(self):
        for evidence in ([], [{'test': 'a_different_test'}], [{'test': 'reading_test'}] * 2):
            with self.subTest(evidence=evidence), self.assertRaisesRegex(ValueError, 'executions='):
                self.check(evidence=evidence)
        with self.assertRaisesRegex(ValueError, 'counts='):
            self.check(counts={'run': 0, 'failures': 1, 'errors': 0, 'skipped': 0})
