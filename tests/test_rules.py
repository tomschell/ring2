"""Rules the code must keep, checked without starting anything.

Run: python3 -m unittest tests.test_rules
"""
import re
import unittest
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1] / 'ring2'


class RulesTest(unittest.TestCase):
    def test_provider_checks_are_always_off_and_not_a_setting(self):
        """Sessions always launch with the provider's checks off; nothing can turn them back on."""
        for path in SOURCE.glob('*.py'):
            self.assertNotIn('checks_off', path.read_text(), f'{path.name} still reads a checks setting')
        sessions, headless = (SOURCE / 'sessions.py').read_text(), (SOURCE / 'headless.py').read_text()
        self.assertRegex(sessions, r"'--remote', endpoint,[^\n]*\n?[^\n]*'--dangerously-bypass-approvals-and-sandbox'")
        self.assertRegex(headless, r"'approvalPolicy': 'never', 'sandbox': 'danger-full-access'")

    def test_nothing_reaches_into_another_repository(self):
        for path in SOURCE.glob('*.py'):
            code = re.sub(r'""".*?"""', '', path.read_text(), flags=re.S)
            self.assertIsNone(re.search(r'^\s*(from|import) ring(\.|\s)', code, re.M), path.name)
            self.assertNotIn('ringd', code, path.name)


if __name__ == '__main__':
    unittest.main()
