"""Each module on its own, with only its own settings and no shared config.

Run: python3 -m unittest tests.test_standalone -v
  tasks     a queue claim with no fleet, bus or sessions (only bd)
  ship      test, PASS and publication with no tracker (only git)
"""
import contextlib
import io
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from ring2 import Refused, Unconfirmed, ship
from tests.lab import REPO


class Plain(unittest.TestCase):
    """A temp directory and a clean environment: no config file, no bus, no sessions."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix='ring2-')).resolve()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        (self.root / 'home').mkdir()
        self.env = {'PATH': os.environ['PATH'], 'HOME': str(self.root / 'home'), 'PYTHONPATH': str(REPO),
                    'BD_NON_INTERACTIVE': '1', 'GIT_AUTHOR_NAME': 'ring2', 'GIT_COMMITTER_NAME': 'ring2',
                    'GIT_AUTHOR_EMAIL': 'ring2@example.invalid', 'GIT_COMMITTER_EMAIL': 'ring2@example.invalid'}

    def run_(self, *argv, cwd=None, check=True):
        result = subprocess.run(argv, cwd=cwd or self.root, env=self.env, capture_output=True, text=True)
        if check and result.returncode:
            raise AssertionError(f'{argv}: {result.stdout}{result.stderr}')
        return result

    def module(self, name, *args, check=True):
        return self.run_(sys.executable, '-m', f'ring2.{name}', *args, check=check)


@unittest.skipUnless(shutil.which('bd'), 'needs bd')
class TasksAloneTest(Plain):
    def test_claim_with_only_a_tracker(self):
        tracker = self.root / 'tracker'
        tracker.mkdir()
        self.run_('bd', 'init', '--prefix', 'solo', '--non-interactive', cwd=tracker)
        self.run_('bd', 'create', 'the only queued task', '--labels', 'queue', cwd=tracker)
        tasks = ('tasks', '--tracker', str(tracker))
        queued = self.module(*tasks, 'queue').stdout.strip()
        self.assertIn('the only queued task', queued)
        task = queued.split()[0]
        self.assertEqual(self.module(*tasks, '--as', 'ann', 'claim').stdout.strip(), f'WON {queued}')
        self.assertEqual(self.module(*tasks, '--as', 'ann', 'claim').stdout.strip(), f'HELD {queued}')
        self.assertEqual(self.module(*tasks, '--as', 'bob', 'claim').stdout.strip(), 'EMPTY')
        self.assertEqual(self.module(*tasks, '--as', 'ann', 'comment', task, 'a note').stdout.strip(), 'recorded')
        self.module(*tasks, 'label', task, 'owes:docs')
        self.assertIn('still carries owes:docs',
                      self.module(*tasks, '--as', 'ann', 'close', task, '--reason', 'done', check=False).stderr)
        self.module(*tasks, 'unlabel', task, 'owes:docs')
        self.assertEqual(self.module(*tasks, '--as', 'ann', 'give-back', task).stdout.strip(), 'given back')
        self.assertEqual(self.module(*tasks, '--as', 'bob', 'claim').stdout.strip(), f'WON {queued}')
        self.assertEqual(self.module(*tasks, '--as', 'bob', 'close', task, '--reason', 'done').stdout.strip(), 'closed')

    def test_missing_settings_are_named(self):
        self.assertIn('ring2 tasks: missing setting tracker_dir', self.module('tasks', 'claim', check=False).stderr)
        self.assertIn('ring2 ship: missing setting repo', self.module('ship', 'publish', 'abc', check=False).stderr)
        self.assertIn('ring2 bus: missing setting ring_url, channels', self.module('bus', 'post', 'x', check=False).stderr)
        # The assembled tool with a config that knows only the tracker: a task command asks for nothing else.
        config = self.root / 'ring2.json'
        config.write_text('{"tracker_dir": "%s"}' % (self.root / 'nowhere'))
        start = self.run_(sys.executable, '-m', 'ring2', '--config', str(config), '--as', 'ann', 'start', check=False)
        self.assertIn("no role 'ann'", start.stderr)


class ShipAloneTest(Plain):
    def clone(self):
        """(origin, repo, commits): a bare origin holding the first of two commits made in its clone."""
        origin, repo = self.root / 'origin.git', self.root / 'repo'
        self.run_('git', 'init', '-q', '--bare', '-b', 'trunk', str(origin))
        self.run_('git', 'clone', '-q', str(origin), str(repo))
        commits = []
        for name in ('one.txt', 'two.txt'):
            (repo / name).write_text(name)
            self.run_('git', 'add', name, cwd=repo)
            self.run_('git', 'commit', '-q', '-m', name, cwd=repo)
            commits.append(self.run_('git', 'rev-parse', 'HEAD', cwd=repo).stdout.strip())
        self.run_('git', 'push', '-q', 'origin', f'{commits[0]}:refs/heads/trunk', cwd=repo)
        return origin, repo, commits

    def in_process(self):
        """ring2.ship called directly, with git under the test's clean environment and its output kept quiet."""
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(mock.patch.dict(os.environ, self.env, clear=True))
        stack.enter_context(contextlib.redirect_stdout(io.StringIO()))

    def test_the_newest_execution_decides(self):
        _, repo, (commit, other) = self.clone()
        self.in_process()
        settings, flag = {'repo': str(repo)}, self.root / 'flag'

        def run(commit, *outcomes):
            notes = ship.GitNotes(settings, commit)
            for passing in outcomes:
                flag.touch() if passing else flag.unlink(missing_ok=True)
                self.assertEqual(ship.test(settings, commit, f'test -f {flag}', notes, 'ann'), passing)
            return ship.tested(commit, notes), [text.split()[1] for _, text in notes.read()]
        # The same command by the same author on the same commit: every execution is its own line.
        self.assertEqual(run(commit, False, True, False), (False, ['FAIL', 'PASS', 'FAIL']))
        self.assertEqual(run(other, True, False, True), (True, ['PASS', 'FAIL', 'PASS']))

    def test_a_result_whose_reply_was_lost_is_recorded_once(self):
        _, repo, (commit, _) = self.clone()
        self.in_process()
        settings = {'repo': str(repo)}

        class LosesOneReply(ship.GitNotes):
            lost = 0

            def add(self, author, text):
                answer = super().add(author, text)
                if not self.lost:
                    self.lost += 1
                    raise Unconfirmed('the note was written; its reply was lost')
                return answer
        notes = LosesOneReply(settings, commit)
        self.assertTrue(ship.test(settings, commit, 'true', notes, 'ann'))
        self.assertEqual((notes.lost, len(notes.read())), (1, 1))

    def test_a_result_is_recorded_before_the_next_test_may_start(self):
        _, repo, (commit, _) = self.clone()
        self.in_process()
        settings, recording, go_on = {'repo': str(repo)}, threading.Event(), threading.Event()

        class Paused(ship.GitNotes):
            def add(self, author, text):
                recording.set()
                go_on.wait(60)
                return super().add(author, text)
        notes, log = ship.GitNotes(settings, commit), repo / '.git' / 'ring2-test' / 'ann.log'
        older = threading.Thread(target=ship.test, args=(settings, commit, 'echo OLDER', Paused(settings, commit), 'ann'))
        older.start()
        self.addCleanup(lambda: (go_on.set(), older.join()))
        self.assertTrue(recording.wait(60))
        # The older run has ended and is about to record: a newer one cannot overtake it or replace its output.
        with self.assertRaises(Refused) as refused:
            ship.test(settings, commit, 'echo NEWER; false', notes, 'ann')
        self.assertIn('already has a test running', str(refused.exception))
        self.assertEqual((notes.read(), log.read_text()), ([], 'OLDER\n'))
        go_on.set()
        older.join()
        self.assertFalse(ship.test(settings, commit, 'echo NEWER; false', notes, 'ann'))
        self.assertEqual([text.split()[1] for _, text in notes.read()], ['PASS', 'FAIL'])
        self.assertFalse(ship.tested(commit, notes))

    def test_publish_with_no_tracker(self):
        origin, repo, commits = self.clone()
        commit = commits[1]
        ship = ('ship', '--repo', str(repo), '--branch', 'trunk', '--reviewer', 'rae')
        tip = lambda: self.run_('git', '--git-dir', str(origin), 'rev-parse', 'refs/heads/trunk').stdout.strip()

        self.assertIn('no passing test result', self.module(*ship, 'publish', commit, check=False).stderr)
        self.assertEqual(self.module(*ship, '--as', 'ann', 'test', commit, 'test -f two.txt').stdout.strip(), 'TEST PASS')
        self.assertIn('no reviewer PASS', self.module(*ship, 'publish', commit, check=False).stderr)
        self.assertIn('ann is not a reviewer', self.module(*ship, '--as', 'ann', 'pass', commit, check=False).stderr)
        self.assertEqual(self.module(*ship, '--as', 'rae', 'pass', commit).stdout.strip(), 'recorded')
        self.assertEqual(self.module(*ship, '--as', 'rae', 'pass', commit).stdout.strip(), 'already recorded')
        self.assertEqual(tip(), commits[0])
        self.assertEqual(self.module(*ship, 'publish', commit).stdout.strip(), 'published')
        self.assertEqual(tip(), commit)
        self.assertEqual(self.module(*ship, 'publish', commit).stdout.strip(), 'already published')
        self.assertEqual(self.module(*ship, 'published', commits[0]).stdout.strip(), 'published')
        # The evidence is two plain lines in a git note on the commit.
        notes = self.run_('git', 'notes', '--ref', 'ring2', 'show', commit, cwd=repo).stdout.split('\n')
        self.assertEqual([line.split(' (run ')[0] for line in notes if line],
                         [f'ann: TEST PASS {commit} `test -f two.txt`', f'rae: PASS {commit}'])
        self.assertFalse((self.root / 'home' / '.beads').exists())


if __name__ == '__main__':
    unittest.main()
