"""Fresh conversations use the real Codex CLI and the existing local model stand-in."""
import json
import threading
import time
import unittest
from unittest.mock import patch

from ring2 import Refused, app
from tests.lab import HAVE_TOOLS, Lab, wait
from tests.test_fleet import answer_ready


class FreshSettingTest(unittest.TestCase):
    def test_start_requires_tracker_and_provider_support(self):
        cfg = {'path': 'unused', 'roles': {'review': {'role': 'reviewer', 'provider': 'claude',
               'model': 'unused', 'fresh_conversation': True}}}
        with patch.object(app.session, 'start') as start:
            with self.assertRaisesRegex(Refused, 'tracker_dir'):
                app.start(cfg, 'review')
            cfg['tracker_dir'] = 'unused'
            with self.assertRaisesRegex(Refused, 'fresh_conversation is not built for this provider'):
                app.start(cfg, 'review')
            start.assert_not_called()


@unittest.skipUnless(HAVE_TOOLS, 'needs codex and bd')
class FreshConversationTest(unittest.TestCase):
    def test_idle_replacements_wait_for_turns_and_preserve_delivery_identity(self):
        lab = Lab({'fresh': ('reviewer', 'FRESH-BRIEF', {'fresh_conversation': True}),
                   'normal': ('reviewer', 'NORMAL-BRIEF')}, ready_seconds=20)
        self.addCleanup(lab.close)
        hold = threading.Event()
        self.addCleanup(hold.set)
        lab.model.rules += [('fleet start: post', 'fn', answer_ready), ('HELD-REVIEW', 'hold', hold)]
        lab.ring2('fleet', 'start')

        def state(label):
            return json.loads((lab.root / f'state/{label}.json').read_text())

        def cursor(label):
            return int((lab.root / f'state/{label}.cursor').read_text())

        def received(label, text):
            sequence = lab.post(f'@{label} {text}')
            request = wait(text, lambda: lab.requests_with(text))[0]
            wait('cursor passed ' + text, lambda: cursor(label) >= sequence)
            return request

        # With the option off, both messages stay in the existing conversation.
        normal = state('normal')['thread']
        received('normal', 'NORMAL-FIRST')
        second = received('normal', 'NORMAL-SECOND')
        self.assertEqual(state('normal')['thread'], normal)
        self.assertIn('NORMAL-FIRST', json.dumps(second))

        ready = state('fresh')
        posted = [m['message'] for m in lab.messages()]
        self.assertIn(f'fresh: READY fresh {ready["token"]}', posted)
        received('fresh', 'FIRST-REVIEW')
        first_thread = state('fresh')['thread']
        second = received('fresh', 'SECOND-REVIEW')
        self.assertNotEqual(state('fresh')['thread'], first_thread)
        self.assertNotIn('FIRST-REVIEW', json.dumps(second))
        text = '\n'.join(lab.model.user_texts(second))
        for expected in ('FRESH-BRIEF', 'You are fresh (reviewer)', str(lab.work['fresh']),
                         'You hold no task.', 'Ring2 commands:', f'(ring2 session {ready["token"]})'):
            self.assertIn(expected, text)
        self.assertNotIn('fleet start: post', json.dumps(second))

        received('fresh', 'HELD-REVIEW')
        busy = state('fresh')
        queued = [lab.post('@fresh QUEUED-ONE'), lab.post('@fresh QUEUED-TWO')]
        time.sleep(2)
        self.assertEqual(state('fresh')['thread'], busy['thread'])
        self.assertLess(cursor('fresh'), queued[0])
        self.assertFalse(lab.requests_with('QUEUED-ONE'))
        self.assertFalse(lab.requests_with('QUEUED-TWO'))
        hold.set()
        one = wait('first queued message', lambda: lab.requests_with('QUEUED-ONE'))[0]
        two = wait('second queued message', lambda: lab.requests_with('QUEUED-TWO'))[0]
        wait('queued cursor', lambda: cursor('fresh') == queued[-1])
        self.assertNotIn('HELD-REVIEW', json.dumps(one))
        self.assertNotIn('QUEUED-ONE', json.dumps(two))
        self.assertNotEqual(state('fresh')['thread'], busy['thread'])
        # Each isolated request has a distinct rollout/thread, including the two queued requests.
        rollouts = list((lab.home / 'codex/sessions').glob('*/*/*/rollout-*.jsonl'))
        threads = []
        for marker in ('HELD-REVIEW', 'QUEUED-ONE', 'QUEUED-TWO'):
            matches = [path for path in rollouts if marker in path.read_text()]
            self.assertEqual(len(matches), 1)
            threads.append(matches[0].name)
        self.assertEqual(len(set(threads)), 3)
        after = state('fresh')
        for field in ('pid', 'server_pid', 'token', 'endpoint', 'started'):
            self.assertEqual(after[field], ready[field])

        task = lab.task('held task', '--assignee', 'fresh')
        lab.bd('update', task, '--status', 'in_progress')
        received('fresh', 'WHILE-HOLDING-TASK')
        self.assertEqual(state('fresh')['thread'], after['thread'])
        lab.bd('update', task, '--status', 'closed')

        # A failed tracker read must never acknowledge the waiting message.
        bd = lab.shims / 'bd'
        original = bd.read_text()
        bd.write_text('#!/bin/sh\necho unreadable-tracker >&2\nexit 1\n')
        try:
            sequence = lab.post('@fresh UNREADABLE-TRACKER')
            wait('runner to report tracker failure', lambda: not json.loads(
                lab.ring2('status', label='fresh').stdout)['running'])
            self.assertLess(cursor('fresh'), sequence)
            self.assertFalse(lab.requests_with('UNREADABLE-TRACKER'))
        finally:
            bd.write_text(original)
