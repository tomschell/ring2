"""Gemini turns use a fake executable on PATH; no provider requests."""
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from ring2 import read_handle
from ring2.providers.gemini import GeminiTurns, status


class GeminiTest(unittest.TestCase):
    def test_turns_resume_and_failed_run(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            program = root / 'gemini'
            program.write_text('''#!/usr/bin/env python3
import json, os, sys, time
from pathlib import Path
root = Path(os.environ['GEMINI_TEST'])
with (root / 'calls').open('a') as out:
    out.write(json.dumps(sys.argv[1:]) + '\\n')
text = sys.argv[sys.argv.index('-p') + 1]
print(json.dumps({'type': 'init', 'session_id': 'saved-session',
                  'mcp_servers': ['unexpected'] if text == 'mcp' else []}), flush=True)
if text == 'missing':
    sys.exit(0)
print(json.dumps({'type': 'result', 'status': 'success'}), flush=True)
if text == 'first':
    (root / 'result').touch()
    while not (root / 'release').exists():
        time.sleep(.01)
''')
            program.chmod(0o755)
            settings = {'state_dir': str(root / 'state'), 'env': {
                'PATH': str(root) + os.pathsep + os.environ['PATH'], 'GEMINI_TEST': str(root)}}
            spec = {'settings': settings, 'workdir': directory, 'name': 'gm', 'model': 'gemini-test'}
            target = GeminiTurns(spec, 'token')
            failures = []

            def first():
                try:
                    target.send('first')
                except BaseException as exc:
                    failures.append(exc)

            worker = threading.Thread(target=first)
            worker.start()
            try:
                deadline = time.monotonic() + 10
                while not (root / 'result').exists() and time.monotonic() < deadline:
                    time.sleep(.01)
                self.assertTrue((root / 'result').exists())
                state = read_handle(settings, 'gm')
                self.assertEqual(state['thread'], 'saved-session')
                self.assertTrue(state['busy'])  # result alone does not finish a turn
                with patch('ring2.providers.gemini.session.running', return_value=True):
                    self.assertEqual(status(settings, 'gm'), {'session': True, 'loop': True, 'busy': True})
            finally:
                (root / 'release').touch()
                worker.join(10)
            self.assertFalse(worker.is_alive())
            self.assertEqual(failures, [])
            self.assertFalse(read_handle(settings, 'gm')['busy'])
            self.assertTrue(target.started('unused'))

            resumed = GeminiTurns({**spec, 'resume': read_handle(settings, 'gm')['thread']}, 'new-token')
            resumed.send('second')
            calls = [json.loads(line) for line in (root / 'calls').read_text().splitlines()]
            self.assertEqual(calls[0], ['-p', 'first', '--output-format', 'stream-json',
                                      '--approval-mode', 'yolo', '-m', 'gemini-test'])
            self.assertEqual(calls[1], ['-p', 'second', '--output-format', 'stream-json',
                                      '--approval-mode', 'yolo', '-m', 'gemini-test', '-r', 'saved-session'])
            with self.assertRaisesRegex(RuntimeError, 'complete turn'):
                resumed.send('missing')
            with self.assertRaisesRegex(RuntimeError, 'MCP servers'):
                GeminiTurns(spec, 'mcp-token').send('mcp')
