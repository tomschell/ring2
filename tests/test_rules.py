"""Rules the code must keep, checked without starting anything.

Run: python3 -m unittest tests.test_rules
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1] / 'ring2'


class RulesTest(unittest.TestCase):
    def test_provider_checks_are_always_off_and_not_a_setting(self):
        """Sessions always launch with the provider's checks off; nothing can turn them back on."""
        for path in SOURCE.rglob('*.py'):
            self.assertNotIn('checks_off', path.read_text(), f'{path.name} still reads a checks setting')
        headless = (SOURCE / 'providers/codex.py').read_text()
        self.assertRegex(headless, r"'approvalPolicy': 'never', 'sandbox': 'danger-full-access'")

    def test_core_never_names_a_harness(self):
        for name in ('bus', 'tasks', 'ship', 'delivery', 'session', 'app', '__init__'):
            self.assertIsNone(re.search(r'\b(codex|claude|rollout|app-server)\b',
                                       (SOURCE / f'{name}.py').read_text(), re.I), name)

    def test_removing_an_adapter_leaves_the_other_usable(self):
        for removed, kept in (('codex', 'claude'), ('claude', 'codex')):
            with self.subTest(removed=removed), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                shutil.copytree(SOURCE, root / 'ring2', ignore=shutil.ignore_patterns('__pycache__'))
                (root / 'ring2/providers' / f'{removed}.py').unlink()
                script = f"""
from ring2 import Refused, app
cfg = {{'state_dir': {directory!r}, 'roles': {{'r': {{'provider': {kept!r}}}}}}}
assert app.status(cfg)[0]['loop'] is False
assert app.adapter(cfg, 'r').WORD == {kept!r}
cfg['roles']['r']['provider'] = {removed!r}
try:
    app.adapter(cfg, 'r')
except Refused as exc:
    assert str(exc) == "ring2: provider {removed!r} is not built"
else:
    raise AssertionError('missing adapter was accepted')
"""
                result = subprocess.run([sys.executable, '-B', '-c', script], cwd=root,
                                        env={**os.environ, 'PYTHONPATH': directory},
                                        capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_nothing_reaches_into_another_repository(self):
        for path in SOURCE.rglob('*.py'):
            code = re.sub(r'""".*?"""', '', path.read_text(), flags=re.S)
            self.assertIsNone(re.search(r'^\s*(from|import) ring(\.|\s)', code, re.M), path.name)
            self.assertNotIn('ringd', code, path.name)


if __name__ == '__main__':
    unittest.main()
