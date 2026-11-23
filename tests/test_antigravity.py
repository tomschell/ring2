"""Antigravity delivery with a fake agy on PATH; no provider requests."""
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path

from ring2 import bus, busd, read_handle, session
from ring2.providers import antigravity
from tests.lab import alive, wait


class AntigravityTest(unittest.TestCase):
    def test_turns_resume_and_mid_turn_exit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            program = root / 'agy'
            program.write_text('''#!/usr/bin/env python3
import json, os, sys, time
from pathlib import Path
root = Path(os.environ['AGY_TEST'])
def record(value):
    with (root / 'calls').open('a') as out:
        out.write(json.dumps(value) + '\\n')
record({'argv': sys.argv[1:], 'pid': os.getpid()})
conversation = sys.argv[sys.argv.index('--conversation') + 1] if '--conversation' in sys.argv else 'saved-conversation'
initialized = False
for line in sys.stdin:
    message = json.loads(line)
    record({'input': message, 'pid': os.getpid()})
    if not initialized:
        print(json.dumps({'event': 'init', 'conversation_id': conversation}), flush=True)
        initialized = True
    text = message['message']['content']
    if 'EXIT-MID-TURN' in text:
        sys.exit(0)
    if text.startswith('FIRST-TURN'):
        while not (root / 'release').exists():
            time.sleep(.01)
    print(json.dumps({'event': 'result'}), flush=True)
    if 'CLOSE-STDIN' in text:
        os.close(0)
        (root / 'stdin-closed').touch()
        time.sleep(300)
''')
            program.chmod(0o755)
            server = busd.RingServer(('127.0.0.1', 0), busd.RingApp(root / 'bus.db'))
            server.handle_error = lambda *args: None  # a stopped runner may leave a long poll
            threading.Thread(target=server.serve_forever, daemon=True).start()
            settings = {'state_dir': str(root / 'state'),
                        'ring_url': f'http://127.0.0.1:{server.server_port}', 'channels': ['team'],
                        'env': {'PATH': str(root) + os.pathsep + os.environ['PATH'],
                                'AGY_TEST': directory}}
            cursor = root / 'state/worker.cursor'
            spec = {'model': 'agy-test', 'workdir': directory, 'prompt': 'FIRST-TURN',
                    'source': {**settings, 'label': 'worker', 'cursor': str(cursor)}}

            def calls():
                return [json.loads(line) for line in (root / 'calls').read_text().splitlines()]

            def inputs():
                return [call for call in calls() if 'input' in call]

            def idle():
                return not antigravity.status(settings, 'worker')['busy']

            try:
                first = session.start(settings, 'worker', spec, antigravity)
                wait('first turn', lambda: inputs())
                self.assertEqual(first['thread'], 'saved-conversation')
                self.assertTrue(antigravity.status(settings, 'worker')['busy'])
                self.assertEqual(inputs()[0]['input'], {'event': 'user', 'message': {
                    'content': f'FIRST-TURN\n(ring2 session {first["token"]})'}})
                sequence = bus.post(settings, 'team', '@worker SECOND-TURN')['sequence']
                self.assertLess(int(cursor.read_text()), sequence)
                (root / 'release').touch()
                wait('second turn result and cursor', lambda: int(cursor.read_text()) == sequence and idle())
                self.assertEqual(len(inputs()), 2)
                self.assertEqual(inputs()[1]['pid'], inputs()[0]['pid'])
                self.assertIn('SECOND-TURN', inputs()[1]['input']['message']['content'])
                argv = ['--input-format', 'stream-json', '--output-format', 'stream-json',
                        '--dangerously-skip-permissions', '--model', 'agy-test']
                self.assertEqual(calls()[0]['argv'], argv)
                row = antigravity.status(settings, 'worker')
                self.assertEqual(row, {'session': True, 'loop': True, 'busy': False,
                                       'active': int(cursor.stat().st_mtime), 'active_source': 'cursor'})

                session.stop(settings, 'worker', antigravity.KIND)
                resumed = session.start(settings, 'worker', {**spec, 'prompt': 'RESUMED-TURN',
                                        'resume': read_handle(settings, 'worker')['thread']}, antigravity)
                wait('resumed turn', lambda: len(inputs()) == 3 and idle())
                self.assertNotEqual(resumed['server_pid'], first['server_pid'])
                self.assertEqual(resumed['thread'], first['thread'])
                self.assertEqual([call['argv'] for call in calls() if 'argv' in call],
                                 [argv, argv + ['--conversation', 'saved-conversation']])
                closed = bus.post(settings, 'team', '@worker CLOSE-STDIN')['sequence']
                wait('closed stdin after result', lambda: (root / 'stdin-closed').exists()
                     and int(cursor.read_text()) == closed and idle())
                self.assertTrue(alive(resumed['server_pid']))
                pending = bus.post(settings, 'team', '@worker AFTER-CLOSED-STDIN')['sequence']
                wait('runner exits on broken stdin',
                     lambda: not antigravity.status(settings, 'worker')['session'], seconds=5)
                wait('child cleaned up', lambda: not alive(resumed['server_pid']))
                self.assertEqual(int(cursor.read_text()), closed)
                self.assertEqual(len(inputs()), 4)

                session.start(settings, 'worker', {**spec, 'prompt': 'REPLAY-START',
                              'resume': read_handle(settings, 'worker')['thread']}, antigravity)
                wait('pending message replayed', lambda: int(cursor.read_text()) == pending and idle())
                self.assertEqual(len(inputs()), 6)
                self.assertIn('AFTER-CLOSED-STDIN', inputs()[-1]['input']['message']['content'])
                handle_tmp = root / 'state/worker.tmp'
                handle_tmp.mkdir()  # make the next handle write fail before stdin is touched
                blocked = bus.post(settings, 'team', '@worker HANDLE-WRITE-FAILED')['sequence']
                wait('runner exits on handle write failure',
                     lambda: not antigravity.status(settings, 'worker')['session'], seconds=5)
                wait('child cleaned up after handle failure',
                     lambda: not alive(read_handle(settings, 'worker')['server_pid']))
                self.assertEqual(int(cursor.read_text()), pending)
                self.assertEqual(len(inputs()), 6)
                handle_tmp.rmdir()
                session.start(settings, 'worker', {**spec, 'prompt': 'HANDLE-REPLAY-START',
                              'resume': read_handle(settings, 'worker')['thread']}, antigravity)
                wait('handle failure replayed', lambda: int(cursor.read_text()) == blocked and idle())
                self.assertEqual(len(inputs()), 8)
                self.assertIn('HANDLE-WRITE-FAILED', inputs()[-1]['input']['message']['content'])
                failed = bus.post(settings, 'team', '@worker EXIT-MID-TURN')['sequence']
                wait('dead runner', lambda: not antigravity.status(settings, 'worker')['session'])
                self.assertEqual(len(inputs()), 9)
                self.assertLess(int(cursor.read_text()), failed)
                self.assertFalse(antigravity.status(settings, 'worker')['busy'])
            finally:
                (root / 'release').touch()
                session.stop(settings, 'worker', antigravity.KIND)
                server.shutdown()
                server.server_close()
