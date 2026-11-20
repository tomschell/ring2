"""The evidence line grammar, with no git, tracker or session.

Run: python3 -m unittest tests.test_evidence
"""
import unittest

from ring2 import ship

CANDIDATE, OTHER = 'a' * 40, 'b' * 40
SETTINGS = {'reviewers': ['rae']}


class Lines:
    def __init__(self, *lines):
        self.lines = list(lines)

    def read(self):
        return self.lines


class EvidenceGrammarTest(unittest.TestCase):
    def passed(self, *lines):
        return ship.has_pass(SETTINGS, CANDIDATE, Lines(*lines))

    def result(self, *texts):
        return ship.tested(CANDIDATE, Lines(*[('ann', text) for text in texts]))

    def test_a_pass_is_a_line_that_starts_with_pass_and_the_full_commit(self):
        self.assertTrue(self.passed(('rae', f'PASS {CANDIDATE}')))
        self.assertTrue(self.passed(('rae', f'PASS {CANDIDATE} with one nit, see below')))
        self.assertFalse(self.passed(('rae', f'Do NOT PASS {CANDIDATE}: defect remains')))
        self.assertFalse(self.passed(('rae', f'I would not PASS this. Candidate {CANDIDATE}')))
        self.assertFalse(self.passed(('rae', f'PASS {OTHER} (supersedes {CANDIDATE})')))
        self.assertFalse(self.passed(('rae', f'PASS {CANDIDATE[:12]}')))
        self.assertFalse(self.passed(('rae', f'PASS {CANDIDATE}0')))
        self.assertFalse(self.passed(('ann', f'PASS {CANDIDATE}')))
        self.assertFalse(self.passed(('rae', f'TEST PASS {CANDIDATE} `true`')))

    def test_a_later_objection_does_not_revoke_a_pass(self):
        self.assertTrue(self.passed(('rae', f'PASS {CANDIDATE}'), ('rae', f'Do NOT PASS {CANDIDATE}: second thoughts')))

    def test_a_test_result_names_its_commit_right_after_the_outcome(self):
        self.assertTrue(self.result(f'TEST PASS {CANDIDATE} `make test`'))
        self.assertFalse(self.result(f'TEST PASS {OTHER} `echo {CANDIDATE}`'))
        self.assertFalse(self.result(f'TEST FAIL (exit 2) {CANDIDATE} `make test`'))
        self.assertFalse(self.result(f'note: TEST PASS {CANDIDATE} `make test`'))
        # The newest result for the candidate decides; results for other commits do not count.
        self.assertFalse(self.result(f'TEST PASS {CANDIDATE} `t`', f'TEST FAIL (exit 1) {CANDIDATE} `t`'))
        self.assertTrue(self.result(f'TEST FAIL (exit 1) {CANDIDATE} `t`', f'TEST PASS {CANDIDATE} `t`',
                                    f'TEST FAIL (exit 1) {OTHER} `echo {CANDIDATE}`'))


if __name__ == '__main__':
    unittest.main()
