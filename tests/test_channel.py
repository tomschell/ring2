"""The optional MCP channel shares the external listener's bus cursor and lock."""
import json
import os
import select
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from ring2 import bus, busd
from tests.lab import REPO, wait


class ChannelTest(unittest.TestCase):
    def test_notifications_and_listener_exclusion(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            server = busd.RingServer(('127.0.0.1', 0), busd.RingApp(root / 'bus.db'))
            server.handle_error = lambda *args: None
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            settings = {'state_dir': str(root / 'state'),
                        'ring_url': f'http://127.0.0.1:{server.server_port}', 'channels': ['team'],
                        'roles': {'controller': {'role': 'controller', 'provider': 'external'}}}
            config = root / 'ring2.json'
            config.write_text(json.dumps(settings))
            env = {**os.environ, 'PYTHONPATH': str(REPO)}
            argv = [sys.executable, '-B', '-m', 'ring2.channel', '--config', str(config), '--as', 'controller']
            listen = [sys.executable, '-B', '-m', 'ring2', '--config', str(config), '--as', 'controller', 'listen']
            receiver = subprocess.Popen(argv, env=env, cwd=root, stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                receiver.stdin.write(b'{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26"}}\n')
                receiver.stdin.flush()
                self.assertTrue(select.select([receiver.stdout], [], [], 10)[0])
                initialized = json.loads(receiver.stdout.readline())
                self.assertEqual(initialized['id'], 1)
                self.assertEqual(initialized['result']['protocolVersion'], '2025-03-26')
                self.assertEqual(initialized['result']['capabilities'], {'experimental': {'claude/channel': {}}})
                refused = subprocess.run(listen, env=env, cwd=root, capture_output=True, timeout=10)
                self.assertNotEqual(refused.returncode, 0)
                self.assertIn(b'already listening', refused.stderr)

                posted = bus.post(settings, 'team', 'coord: @controller ONE-REQUEST')
                bus.post(settings, 'team', 'coord: @someone-else ANOTHER-REQUEST')
                own = bus.post(settings, 'team', 'controller: @controller OWN-POST')
                self.assertTrue(select.select([receiver.stdout], [], [], 10)[0])
                notification = json.loads(receiver.stdout.readline())
                self.assertEqual(notification, {'jsonrpc': '2.0', 'method': 'notifications/claude/channel',
                    'params': {'content': f'Ring message {posted["sequence"]} on team: coord: @controller ONE-REQUEST',
                               'meta': {'channel': 'team', 'sequence': str(posted['sequence'])}}})
                self.assertTrue(all(isinstance(value, str) for value in notification['params']['meta'].values()))
                cursor = root / 'state/controller.cursor'
                wait('cursor passes all messages', lambda: int(cursor.read_text()) == own['sequence'])
                self.assertFalse(select.select([receiver.stdout], [], [], 0.2)[0])
                receiver.stdin.close()
                receiver.wait(timeout=5)
                self.assertEqual(receiver.returncode, 0, receiver.stderr.read())
            finally:
                if receiver.poll() is None:
                    receiver.kill()
                    receiver.wait(timeout=5)
                for stream in (receiver.stdin, receiver.stdout, receiver.stderr):
                    stream.close()

            # listen resumes the same cursor, without replaying the server's notification.
            listener = subprocess.Popen(listen, env=env, cwd=root, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                self.assertFalse(select.select([listener.stdout], [], [], 0.2)[0])
                next_message = bus.post(settings, 'team', 'coord: @controller AFTER-CHANNEL')
                self.assertTrue(select.select([listener.stdout], [], [], 10)[0])
                self.assertEqual(listener.stdout.readline().decode(),
                    f'Ring message {next_message["sequence"]} on team: coord: @controller AFTER-CHANNEL\n')
                refused = subprocess.run(argv, env=env, cwd=root, input=b'', capture_output=True, timeout=10)
                self.assertNotEqual(refused.returncode, 0)
                self.assertIn(b'already listening', refused.stderr)
            finally:
                listener.terminate()
                listener.communicate(timeout=5)
