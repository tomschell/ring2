"""Keep the one settings inventory aligned with configuration reads, not runtime records."""
import ast
import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def owner(node):
    while isinstance(node, (ast.Subscript, ast.Attribute)):
        if isinstance(node, ast.Attribute) and node.attr == 'settings':
            return 'settings'
        node = node.value
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == 'role':
        return 'entry'
    return node.id if isinstance(node, ast.Name) else None


class SettingsDocumentationTest(unittest.TestCase):
    def test_missing_roster_role_refuses_before_starting_anything(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / 'ring2.json'
            roles = {'impl': {'role': 'implementer', 'provider': 'codex', 'model': 'unused'},
                     'rev': {'role': 'reviewer', 'provider': 'codex', 'model': 'unused'},
                     'controller': {'provider': 'external'}}
            config.write_text(json.dumps({'roles': roles}))
            commands = [['--as', label, 'start'] for label in roles]
            commands += [['fleet', 'start'], ['status'], ['status', '--json']]
            for command in commands:
                with self.subTest(command=command):
                    result = subprocess.run([sys.executable, '-B', '-m', 'ring2',
                                             '--config', str(config), *command],
                                            cwd=ROOT, capture_output=True, text=True, timeout=10)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertEqual(result.stderr.strip(), 'ring2: role controller needs a role')
                    self.assertEqual(result.stdout, '')
                    self.assertEqual(list(root.iterdir()), [config])

    def test_settings_inventory_matches_source_reads(self):
        read = set()
        inputs = {'cfg', 'settings', 'entry', 'spec', 'source', 'environ'}
        for path in (ROOT / 'ring2').rglob('*.py'):
            for node in ast.walk(ast.parse(path.read_text())):
                key = source = None
                if isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Load) and isinstance(node.slice, ast.Constant):
                    key, source = node.slice.value, owner(node.value)
                elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                      and node.func.attr in ('get', 'pop', 'setdefault') and node.args
                      and isinstance(node.args[0], ast.Constant)):
                    key, source = node.args[0].value, owner(node.func.value)
                    if ast.unparse(node.func.value) == 'os.environ':
                        source = 'environ'
                if source in inputs and isinstance(key, str):
                    # These are derived by the app/runner, not configurable inputs.
                    if not (source == 'cfg' and key == 'path' or source == 'spec' and key in ('name', 'settings')):
                        read.add(f'source.{key}' if source == 'source' and key in ('label', 'cursor') else key)
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == 'need'
                        and node.args and owner(node.args[0]) in inputs):
                    read.update(arg.value for arg in node.args[2:] if isinstance(arg, ast.Constant) and isinstance(arg.value, str))
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                        and node.func.attr == 'add_argument' and path.stem in ('busd', 'watch', 'console')
                        and node.args and isinstance(node.args[0], ast.Constant)):
                    flag = node.args[0].value
                    if flag != '--config':
                        read.add(f'{path.stem}.{flag.removeprefix("--").replace("-", "_")}')
        section = (ROOT / 'README.md').read_text().split('## Settings\n', 1)[1].split('\n## ', 1)[0]
        documented = set(re.findall(r'^\| `([^`]+)` \|', section, re.M))
        self.assertEqual(documented, read)


if __name__ == '__main__':
    unittest.main()
