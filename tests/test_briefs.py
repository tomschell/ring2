"""First prompts use reusable briefs and only configured facts."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ring2 import Refused, app


class BriefTest(unittest.TestCase):
    def test_shipped_briefs_and_explicit_override(self):
        with tempfile.TemporaryDirectory() as root:
            entry = {'provider': 'codex', 'model': 'test', 'workdir': root}
            cfg = {'path': str(Path(root) / 'config.json'), 'state_dir': root,
                   'roles': {'worker': entry}}
            with patch.object(app.session, 'start', return_value={'pid': 1, 'thread': 'test'}) as start:
                for role in ('implementer', 'reviewer', 'coordinator'):
                    entry['role'] = role
                    app.start(cfg, 'worker')
                    prompt = start.call_args.args[2]['prompt']
                    self.assertTrue(prompt.startswith((app.ROOT / f'ring2/briefs/{role}.md').read_text().strip()))
                    for absent in ('Channel:', 'Read a task:', 'List the queue:', 'Test command:'):
                        self.assertNotIn(absent, prompt)
                entry['role'] = 'custom'
                start.reset_mock()
                with self.assertRaisesRegex(Refused, 'needs a brief'):
                    app.start(cfg, 'worker')
                start.assert_not_called()
                entry['brief'] = 'Custom instructions.'
                app.start(cfg, 'worker')
                self.assertTrue(start.call_args.args[2]['prompt'].startswith('Custom instructions.\n'))
