"""Changing managed providers stops the saved session and keeps thread ids local."""
import json
import os
import tempfile
import unittest
from pathlib import Path

from ring2 import app, owned_processes, read_handle
from tests.lab import wait


class ProviderSwitchTest(unittest.TestCase):
    def test_stop_and_start_after_provider_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script = '''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
provider = Path(sys.argv[0]).name
with Path(os.environ['SWITCH_TEST']).open('a') as out:
    out.write(json.dumps({'provider': provider, 'argv': sys.argv[1:]}) + '\\n')
if provider == 'claude':
    print(json.dumps({'type': 'system', 'subtype': 'init', 'mcp_servers': [],
                      'permissionMode': 'bypassPermissions'}), flush=True)
else:
    print(json.dumps({'event': 'init', 'conversation_id': 'agy-conversation'}), flush=True)
for line in sys.stdin:
    message = json.loads(line)
    if provider == 'claude':
        print(json.dumps({'type': 'user', 'isReplay': True, 'uuid': message['uuid']}), flush=True)
        print(json.dumps({'type': 'result'}), flush=True)
    else:
        print(json.dumps({'event': 'result'}), flush=True)
'''
            for name in ('claude', 'agy'):
                program = root / name
                program.write_text(script)
                program.chmod(0o755)
            cfg = {'path': str(root / 'config.json'), 'state_dir': str(root / 'state'),
                   'claude': str(root / 'claude'),
                   'env': {'PATH': str(root) + os.pathsep + os.environ['PATH'],
                           'SWITCH_TEST': str(root / 'calls')},
                   'roles': {'worker': {'provider': 'claude', 'role': 'implementer',
                                       'model': 'fake', 'workdir': directory, 'brief': 'first', 'resume': True}}}

            def state():
                return read_handle(cfg, 'worker')

            def idle():
                return not app.adapter(cfg, 'worker').status(cfg, 'worker')['busy']

            def calls():
                return [json.loads(line) for line in (root / 'calls').read_text().splitlines()]

            try:
                app.start(cfg, 'worker')
                wait('first idle', idle)
                first = state()
                cfg['roles']['worker']['provider'] = 'antigravity'
                app.stop(cfg, 'worker')
                self.assertFalse(owned_processes([first['token']]))
                app.start(cfg, 'worker')
                wait('second idle', idle)
                second = state()
                self.assertEqual(second['kind'], 'antigravity')
                self.assertNotEqual(first['token'], second['token'])
                self.assertNotIn('--conversation', calls()[1]['argv'])
                self.assertFalse(owned_processes([first['token']]))
                self.assertTrue(owned_processes([second['token']]))

                cfg['roles']['worker']['provider'] = 'claude'
                app.start(cfg, 'worker')  # start itself must clean up the other provider
                wait('third idle', idle)
                third = state()
                self.assertFalse(owned_processes([second['token']]))
                self.assertTrue(owned_processes([third['token']]))
                self.assertNotIn('--resume', calls()[2]['argv'])
                self.assertIn('--session-id', calls()[2]['argv'])

                app.stop(cfg, 'worker')
                app.start(cfg, 'worker')
                wait('same provider resumed', idle)
                self.assertEqual(state()['thread'], third['thread'])
                self.assertEqual(calls()[3]['argv'][-2:], ['--resume', third['thread']])

                managed = state()
                cfg['roles']['worker']['provider'] = 'external'
                app.stop(cfg, 'worker')
                self.assertFalse(owned_processes([managed['token']]))

                cfg['roles']['worker']['provider'] = 'claude'
                app.start(cfg, 'worker')
                wait('managed role before external start', idle)
                managed = state()
                cfg['roles']['worker']['provider'] = 'external'
                app.start(cfg, 'worker')
                self.assertFalse(owned_processes([managed['token']]))

                external = {'roles': {'outside': {'provider': 'external', 'role': 'controller'}}}
                app.stop(external, 'outside')
                app.start(external, 'outside')
                external['state_dir'] = str(root / 'outside-state')
                app.stop(external, 'outside')
                app.start(external, 'outside')
                self.assertFalse((root / 'outside-state').exists())
            finally:
                app.stop(cfg, 'worker')
