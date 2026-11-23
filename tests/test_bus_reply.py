"""Cut-off and malformed bus replies are retried without passing their cursor."""
import json
import os
import select
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

from ring2 import bus, delivery
from tests.lab import REPO, wait


class BusReplyTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.after = []
        after = self.after
        message = {'sequence': 8, 'channel': 'team', 'message': 'coord: @controller RECOVERED'}

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                cursor = int(parse_qs(urlparse(self.path).query)['after'][0])
                after.append(cursor)
                body = json.dumps({'messages': [message] if cursor < 8 else []}).encode()
                if len(after) == 2:
                    body = b'{"messages":['  # complete HTTP body, incomplete JSON
                self.send_response(200)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body[:5] if len(after) == 1 else body)
                self.wfile.flush()
                self.close_connection = True

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.settings = {'state_dir': str(self.root / 'state'),
                         'ring_url': f'http://127.0.0.1:{server.server_port}', 'channels': ['team'],
                         'roles': {'controller': {'provider': 'external', 'role': 'controller'}}}
        self.cursor = self.root / 'state/controller.cursor'
        delivery.save_cursor(self.cursor, 7)

    def test_delivery_retries_short_and_invalid_bus_replies(self):
        delivered, logs = [], []

        def source(after, wait):
            if after == 8:
                raise StopIteration
            return bus.events(self.settings, after, wait)

        target = SimpleNamespace(own=lambda: None, send=delivered.append, started=lambda marker: True)
        with self.assertRaises(StopIteration):
            delivery.follow(source, lambda message: True, target, self.cursor, log=logs.append)
        self.assertEqual(self.after, [7, 7, 7])
        self.assertEqual(int(self.cursor.read_text()), 8)
        self.assertEqual(delivered, ['Ring message 8 on team:\ncoord: @controller RECOVERED'])
        self.assertEqual(sum('source unavailable' in line and 'retrying' in line for line in logs), 2)

    def test_channel_retries_short_and_invalid_bus_replies(self):
        config = self.root / 'ring2.json'
        config.write_text(json.dumps(self.settings))
        # Unbuffered reads keep select useful when init and notification arrive together.
        receiver = subprocess.Popen([sys.executable, '-B', '-m', 'ring2.channel', '--config',
                                     str(config), '--as', 'controller'],
                                    cwd=self.root, env={**os.environ, 'PYTHONPATH': str(REPO)},
                                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    bufsize=0)
        try:
            receiver.stdin.write(b'{"id":1,"method":"initialize"}\n')
            self.assertTrue(select.select([receiver.stdout], [], [], 10)[0])
            self.assertEqual(json.loads(receiver.stdout.readline())['id'], 1)
            self.assertTrue(select.select([receiver.stdout], [], [], 10)[0])
            notification = json.loads(receiver.stdout.readline())
            self.assertEqual(notification['params']['content'],
                             'Ring message 8 on team: coord: @controller RECOVERED')
            wait('channel cursor', lambda: int(self.cursor.read_text()) == 8)
            self.assertEqual(self.after[:3], [7, 7, 7])
            self.assertIsNone(receiver.poll())
            receiver.stdin.close()
            receiver.wait(timeout=5)
            self.assertEqual(receiver.returncode, 0)
            self.assertEqual(receiver.stderr.read().count(b'retrying'), 2)
        finally:
            if receiver.poll() is None:
                receiver.kill()
                receiver.wait(timeout=5)
            for stream in (receiver.stdin, receiver.stdout, receiver.stderr):
                stream.close()
