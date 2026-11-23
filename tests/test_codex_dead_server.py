"""A fake app-server: dead connections stop delivery; live RPC errors retry."""
import json
import os
import signal
import tempfile
import threading
import unittest
from pathlib import Path

from ring2 import app, bus, busd, read_handle, session, watch
from ring2.providers import codex
from tests.lab import wait


class DeadServerTest(unittest.TestCase):
    def test_dead_server_and_closed_socket_exit_but_live_errors_retry(self):
        for mode in ('killed', 'eof', 'close', 'reset', 'transient'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                program = root / 'codex'
                program.write_text('''#!/usr/bin/env python3
import json, os, socket, struct, sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass
    def do_GET(self):
        if self.path == '/readyz':
            self.send_response(200)
            self.end_headers()
            return
        self.send_response(101)
        self.send_header('Upgrade', 'websocket')
        self.send_header('Connection', 'Upgrade')
        self.end_headers()
        def frame(value):
            payload = json.dumps(value).encode()
            self.wfile.write(bytes([0x81, len(payload)]) + payload)
            self.wfile.flush()
        turn = 0
        while True:
            header = self.rfile.read(2)
            if not header:
                return
            size = header[1] & 127
            if size == 126:
                size = struct.unpack('!H', self.rfile.read(2))[0]
            elif size == 127:
                size = struct.unpack('!Q', self.rfile.read(8))[0]
            mask = self.rfile.read(4)
            payload = self.rfile.read(size)
            message = json.loads(bytes(byte ^ mask[i % 4] for i, byte in enumerate(payload)))
            if 'id' not in message:
                continue
            method = message['method']
            result = {}
            if method == 'thread/start':
                result = {'thread': {'id': 'fake-thread'}}
            if method == 'turn/start':
                turn += 1
                mode = os.environ['FAKE_CODEX_MODE']
                if turn == 2:
                    if mode == 'close':
                        self.wfile.write(b'\\x88\\x00')
                        self.wfile.flush()
                        return
                    if mode == 'reset':
                        self.connection.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack('ii', 1, 0))
                        self.connection.close()
                        return
                    if mode == 'eof':
                        return
                    if mode == 'transient':
                        frame({'id': message['id'], 'error': {'message': 'try again'}})
                        continue
                result = {'turn': {'id': str(turn)}}
            frame({'id': message['id'], 'result': result})
            if method == 'turn/start':
                frame({'method': 'turn/completed', 'params': {'turn': {'id': str(turn)}}})
endpoint = urlparse(sys.argv[sys.argv.index('--listen') + 1])
ThreadingHTTPServer((endpoint.hostname, endpoint.port), Handler).serve_forever()
''')
                program.chmod(0o755)
                server = busd.RingServer(('127.0.0.1', 0), busd.RingApp(root / 'bus.db'))
                server.handle_error = lambda *args: None
                threading.Thread(target=server.serve_forever, daemon=True).start()
                cfg = {'state_dir': str(root / 'state'), 'codex': str(program),
                       'ring_url': f'http://127.0.0.1:{server.server_port}', 'channels': ['team'],
                       'roles': {'worker': {'provider': 'codex', 'role': 'implementer'}},
                       'env': {'FAKE_CODEX_MODE': mode}}
                cursor = root / 'state/worker.cursor'
                spec = {'model': 'fake-model', 'workdir': directory, 'prompt': 'first',
                        'source': {**cfg, 'label': 'worker', 'cursor': str(cursor)}}
                try:
                    state = session.start(cfg, 'worker', spec, codex)
                    wait('initial cursor', cursor.exists)
                    if mode == 'killed':
                        os.kill(state['server_pid'], signal.SIGKILL)
                    sequence = bus.post(cfg, 'team', '@worker NEXT')['sequence']
                    if mode == 'transient':
                        wait('retry delivered', lambda: int(cursor.read_text()) == sequence, seconds=10)
                        row = app.status(cfg)[0]
                        self.assertTrue(row['loop'])
                        self.assertFalse(watch.due(row))
                        log = (root / 'state/worker.log').read_text()
                        self.assertIn('retrying', log)
                        self.assertIn('try again', log)
                    else:
                        wait('runner down', lambda: not codex.status(cfg, 'worker')['loop'], seconds=10)
                        self.assertLess(int(cursor.read_text()), sequence)
                        self.assertTrue(watch.due(app.status(cfg)[0]))
                finally:
                    session.stop(cfg, 'worker', codex.KIND)
                    server.shutdown()
                    server.server_close()
