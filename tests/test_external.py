"""An outside role listens on the normal bus/cursor without a managed session."""
import http.server
import json
import os
import select
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from ring2 import app, bus, busd, console, watch
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
            self.assertIn('controller: not heard from, tasks -, active -, waiting: -', run('status'))
            page = console.page(cfg)
            self.assertIn('<td>not heard from</td><td>not heard from</td>', page)
            self.assertNotIn('class="dead"', page)
            self.assertIn('<td>-</td><td>-</td></tr>', page)
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
            self.assertFalse(json.loads(run('status', '--json'))[0]['loop'])
            self.assertEqual(watch.once(cfg, {}, 1), [])

    def test_command_handoff_retries_before_advancing(self):
        for mode in ('success', 'retry', 'missing'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                server = busd.RingServer(('127.0.0.1', 0), busd.RingApp(root / 'bus.db'))
                server.handle_error = lambda *args: None
                threading.Thread(target=server.serve_forever, daemon=True).start()
                config = root / 'ring2.json'
                config.write_text(json.dumps({'state_dir': str(root / 'state'),
                    'ring_url': f'http://127.0.0.1:{server.server_port}', 'channels': ['team'],
                    'roles': {'controller': {'provider': 'external', 'role': 'controller'}}}))
                posted = server.app.post('team', 'coord: @controller HANDOFF')
                script = root / 'receiver.py'
                script.write_text("""import pathlib, sys
root = pathlib.Path(sys.argv[1])
with (root / 'attempts').open('a') as stream:
    stream.write(sys.argv[-1] + '\\n')
if sys.argv[2] == 'retry' and not (root / 'allow').exists():
    sys.exit(7)
with (root / 'received').open('a') as stream:
    stream.write(sys.argv[-1] + '\\n')
""")
                command = ([str(root / 'absent')] if mode == 'missing' else
                           [sys.executable, str(script), str(root), mode, '--message'])
                listener = subprocess.Popen([sys.executable, '-B', '-m', 'ring2',
                    '--config', str(config), '--as', 'controller', 'listen', '--', *command],
                    env={**os.environ, 'PYTHONPATH': str(REPO)}, cwd=root,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                cursor = root / 'state/controller.cursor'
                try:
                    if mode != 'success':
                        self.assertTrue(select.select([listener.stderr], [], [], 10)[0])
                        self.assertIn('retrying', listener.stderr.readline())
                        self.assertEqual(cursor.read_text(), '0')
                        self.assertFalse((root / 'received').exists())
                        if mode == 'missing':
                            self.assertTrue(select.select([listener.stderr], [], [], 10)[0])
                            self.assertIn('retrying', listener.stderr.readline())
                            self.assertEqual(cursor.read_text(), '0')
                            continue
                        (root / 'allow').touch()
                    wait('command acknowledged', lambda: cursor.exists() and
                         cursor.read_text() == str(posted['sequence']))
                    expected = f'Ring message {posted["sequence"]} on team: coord: @controller HANDOFF\n'
                    self.assertEqual((root / 'received').read_text(), expected)
                    attempts = (root / 'attempts').read_text().splitlines()
                    self.assertEqual(attempts, [expected.rstrip()] * (2 if mode == 'retry' else 1))
                    self.assertFalse(select.select([listener.stdout], [], [], 0.2)[0])
                finally:
                    listener.terminate()
                    listener.communicate(timeout=10)
                    server.shutdown()
                    server.server_close()

    def test_bus_remembers_a_listener_the_fleet_cannot_see(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bus_app = busd.RingApp(root / 'bus.db')
            now = [1000.0]
            bus_app.clock = lambda: now[0]
            server = busd.RingServer(('127.0.0.1', 0), bus_app)
            server.handle_error = lambda *args: None
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            url = f'http://127.0.0.1:{server.server_port}'
            roles = {'controller': {'provider': 'external', 'role': 'controller'}}

            def write(name, state):
                path = root / name
                path.write_text(json.dumps({'state_dir': str(root / state), 'ring_url': url,
                                            'channels': ['team'], 'roles': roles}))
                return path
            fleet, outside = write('fleet.json', 'fleet-state'), write('outside.json', 'outside-state')
            cfg = app.load(fleet)
            env = {**os.environ, 'PYTHONPATH': str(REPO)}
            for name in ('RING2_ROLE', 'RING2_CONFIG'):
                env.pop(name, None)
            bus_app.post('team', 'coord: @controller ONE')
            second = bus_app.post('team', 'coord: @controller TWO')['sequence']
            self.assertEqual(app.status(cfg)[0]['heard'], 'not heard from')
            listener = subprocess.Popen([sys.executable, '-B', '-m', 'ring2', '--config', str(outside),
                                         '--as', 'controller', 'listen'], env=env, cwd=root,
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                wait('listener read both', lambda: bus_app.listeners().get('controller', {}).get('read') == second)
                row = app.status(cfg)[0]
                self.assertEqual((row['external'], row['heard'], row['delivered']), (True, 'alive', second))
                self.assertEqual(app.waiting(row), 'no')
                self.assertIn('controller: alive, tasks -, active -, waiting: no', app.status_text([row]))
                self.assertIn('<td>alive</td><td>alive</td>', console.page(cfg))
                self.assertFalse((root / 'fleet-state').exists())
            finally:
                listener.terminate()
                listener.communicate(timeout=10)
            bus_app.post('team', 'coord: @controller THREE')
            now[0] += 91
            row = app.status(cfg)[0]
            self.assertEqual(row['heard'], 'silent')
            self.assertEqual(app.waiting(row), 'yes')
            text = app.status_text([row])
            self.assertIn(f'silent (last heard {time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(row["heard_at"]))})', text)
            self.assertTrue(text.endswith('waiting: yes'))
            page = console.page(cfg)
            self.assertIn('<td>silent (last heard ', page)
            self.assertNotIn('class="dead"', page)
            self.assertAlmostEqual(row['heard_at'], time.time() - 91, delta=5)
            bus_app.heard.clear()  # what a restart of the bus forgets
            row = app.status(cfg)[0]
            self.assertEqual((row['heard'], row['delivered']), ('not heard from', None))
            self.assertEqual(app.waiting(row), '-')

    def test_listener_and_bus_work_without_the_report(self):
        with tempfile.TemporaryDirectory() as directory:
            bus_app = busd.RingApp(Path(directory) / 'bus.db')
            server = busd.RingServer(('127.0.0.1', 0), bus_app)
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            settings = {'ring_url': f'http://127.0.0.1:{server.server_port}', 'channels': ['team']}
            bus_app.post('team', 'coord: hi')
            self.assertEqual(len(bus.events(settings, 0, wait=0)), 1)  # a fetch that does not report
            self.assertEqual(bus_app.listeners(), {})
            self.assertEqual(len(bus.events(settings, 0, wait=0, label='controller')), 1)
            self.assertEqual(bus_app.listeners()['controller']['read'], 0)

            class Old(http.server.BaseHTTPRequestHandler):  # a bus from before: no /listeners
                def do_GET(self):
                    self.send_error(404)

                def log_message(self, *args):
                    pass
            old = http.server.HTTPServer(('127.0.0.1', 0), Old)
            self.addCleanup(old.server_close)
            self.addCleanup(old.shutdown)
            threading.Thread(target=old.serve_forever, daemon=True).start()
            self.assertEqual(bus.listeners({'ring_url': f'http://127.0.0.1:{old.server_port}'}), {})


if __name__ == '__main__':
    unittest.main()
