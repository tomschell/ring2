"""Which bus messages a role receives. Run: python3 -m unittest tests.test_bus"""
import unittest

from ring2 import app
from ring2.bus import addressed


class StatusTextTest(unittest.TestCase):
    def line(self, **row):
        return app.status_text([{'label': 'ann', 'session': True, 'loop': True, **row}])

    def test_the_line_says_whether_a_message_waits_not_two_sequence_numbers(self):
        # The cursor is at 14 and the newest message naming the role is 12: nothing waits.
        self.assertTrue(self.line(delivered=14, addressed=12).endswith(', waiting: no'))
        self.assertTrue(self.line(delivered=12, addressed=12).endswith(', waiting: no'))
        self.assertTrue(self.line(delivered=12, addressed=14).endswith(', waiting: yes'))
        self.assertTrue(self.line().endswith(', waiting: no'))   # no bus, or no cursor yet
        self.assertNotIn('14', self.line(delivered=12, addressed=14))


class AddressedTest(unittest.TestCase):
    def test_exact_label_or_all(self):
        self.assertTrue(addressed('exp-role', '@exp-role please look'))
        self.assertTrue(addressed('exp-role', 'cc @exp-role.'))
        self.assertTrue(addressed('exp-role', '@all stop'))

    def test_other_labels_and_lookalikes(self):
        self.assertFalse(addressed('exp-role', '@other-role please look'))
        self.assertFalse(addressed('exp-role', '@exp-role-2 please look'))
        self.assertFalse(addressed('exp-role', 'mail me at x@exp-role'))
        self.assertFalse(addressed('exp-role', 'exp-role without a mention'))


if __name__ == '__main__':
    unittest.main()
