"""An outside role listens on the normal bus/cursor without a managed session."""
import json
import os
import select
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from ring2 import app, busd, console, watch
from tests.lab import REPO, wait


class ExternalTest(unittest.TestCase):
    def test_waiting_message_is_printed_once_when_listen_starts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            server = busd.RingServer(('127.0.0.1', 0), busd.RingApp(root / 'bus.db'))
            server.handle_error = lambda *args: None  # a listener may leave during a long poll
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            config = root / 'ring2.json'
            config.write_text(json.dumps({'state_dir': str(root / 'state'),
                'ring_url': f'http://127.0.0.1:{server.server_port}', 'channels': ['team'],
                'roles': {'controller': {'provider': 'external', 'role': 'controller'}}}))
            cfg = app.load(config)
            env = {**os.environ, 'PYTHONPATH': str(REPO)}
            for name in ('RING2_ROLE', 'RING2_CONFIG'):
                env.pop(name, None)

            def argv(command, *args, label=None):
                return [sys.executable, '-B', '-m', 'ring2', '--config', str(config),
                        *(['--as', label] if label else []), command, *args]

            def run(command, *args, label=None):
                result = subprocess.run(argv(command, *args, label=label), env=env, cwd=root,
                                        capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr)
                return result.stdout

            posted = json.loads(run('post', 'team', '@controller WAITING-BEFORE-LISTEN', label='coord'))
            self.assertIn('waiting: yes', run('status'))
            self.assertIn('<td>yes</td>', console.page(cfg))
            self.assertEqual(watch.once(cfg, {}, 1), [])
            for command in ('start', 'stop', 'restart'):
                self.assertEqual(run(command, label='controller'), '')
            self.assertEqual(run('fleet', 'start'), '')
            self.assertEqual(run('fleet', 'stop'), '')
            self.assertFalse((root / 'state').exists())

            listener = subprocess.Popen(argv('listen', label='controller'), env=env, cwd=root,
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                self.assertTrue(select.select([listener.stdout], [], [], 10)[0], 'listener printed nothing')
                line = listener.stdout.readline()
                self.assertEqual(line, f'Ring message {posted["sequence"]} on team: coord: @controller WAITING-BEFORE-LISTEN\n')
                cursor = root / 'state/controller.cursor'
                wait('message passed', lambda: cursor.exists() and int(cursor.read_text()) == posted['sequence'])
                row = json.loads(run('status', '--json'))[0]
                self.assertEqual((row['session'], row['loop']), (True, True))
                self.assertIn('waiting: no', run('status'))
                self.assertIn('class="alive">alive', console.page(cfg))
                duplicate = subprocess.run(argv('listen', label='controller'), env=env, cwd=root,
                                           capture_output=True, text=True, timeout=10)
                self.assertNotEqual(duplicate.returncode, 0)
                self.assertIn('already listening', duplicate.stderr)
                for command in ('start', 'stop', 'restart'):
                    run(command, label='controller')
                run('fleet', 'start')
                run('fleet', 'stop')
                self.assertIsNone(listener.poll(), 'lifecycle command stopped the listener')
                self.assertFalse((root / 'state/controller.json').exists())
                own = json.loads(run('post', 'team', '@controller OWN-POST', label='controller'))
                wait('own post passed', lambda: int(cursor.read_text()) == own['sequence'])
                self.assertFalse(select.select([listener.stdout], [], [], 0.2)[0], 'a message was printed twice or to its sender')
            finally:
                listener.terminate()
                listener.communicate(timeout=10)
            run('post', 'team', '@controller WAITING-AFTER-LISTEN', label='coord')
            self.assertIn('waiting: yes', run('status'))
            self.assertEqual(watch.once(cfg, {}, 1), [])
            self.assertFalse(json.loads(run('status', '--json'))[0]['loop'])


if __name__ == '__main__':
    unittest.main()
