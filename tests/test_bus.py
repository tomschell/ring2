"""Which bus messages a role receives. Run: python3 -m unittest tests.test_bus"""
import unittest

from ring2.bus import addressed


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
