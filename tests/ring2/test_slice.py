"""Phase 1 slice, end to end: the real Codex binary in tmux, a mock model,
the local Ring server (bin/ringd), a throwaway bd tracker and a bare origin.

Run: python3 -m unittest tests.ring2.test_slice -v
Nothing here reaches a model provider, the user's Codex home, the default
tmux server or any tracker outside the temporary root. RING2_KEEP=1 keeps
the temporary root for inspection.
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path

from tests.ring2.mock_model import MockModel

REPO = Path(__file__).resolve().parents[2]
CODEX = os.environ.get('RING2_CODEX') or shutil.which('codex')
ROLE = 'exp-role'


def free_port():
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        return probe.getsockname()[1]


def wait(what, check, seconds=45):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = check()
        if value:
            return value
        time.sleep(0.1)
    raise AssertionError(f'timed out waiting for {what}')


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


@unittest.skipUnless(CODEX and shutil.which('tmux') and shutil.which('bd'), 'needs codex, tmux and bd')
class SliceTest(unittest.TestCase):
    """The five steps in order; each later step builds on the earlier ones."""

    @classmethod
    def setUpClass(cls):
        cls.root = root = Path(tempfile.mkdtemp(prefix='ring2-')).resolve()
        cls.addClassCleanup(cls.cleanup)
        cls.home, cls.work, cls.tracker = root / 'home', root / 'work', root / 'tracker'
        cls.home.mkdir()
        cls.tracker.mkdir()
        cls.env = dict(os.environ, HOME=str(cls.home), CODEX_HOME=str(cls.home / 'codex'),
                       BD_NON_INTERACTIVE='1', PYTHONPATH=str(REPO))
        cls.env.pop('OPENAI_API_KEY', None)
        cls.socket = f'ring2-test-{uuid.uuid4().hex[:8]}'

        # A bare origin and the role's clone of it, with one tracked file.
        git = ['git', '-c', 'user.name=ring2', '-c', 'user.email=ring2@example.invalid']
        cls.run_ok([*git, 'init', '-q', '--bare', '-b', 'main', str(root / 'origin.git')])
        cls.run_ok([*git, 'clone', '-q', str(root / 'origin.git'), str(cls.work)])
        (cls.work / 'tracked.txt').write_text('committed\n')
        cls.run_ok([*git, 'add', 'tracked.txt'], cwd=cls.work)
        cls.run_ok([*git, 'commit', '-q', '-m', 'first'], cwd=cls.work)
        cls.run_ok([*git, 'push', '-q', 'origin', 'HEAD:main'], cwd=cls.work)

        # The throwaway tracker: one task of ours, two that are not.
        cls.run_ok(['bd', 'init', '--prefix', 'exp', '--non-interactive'], cwd=cls.tracker)
        cls.mine = cls.bd('create', 'Slice task of the experimental role', '--assignee', ROLE)
        cls.bd('update', cls.mine, '--status', 'in_progress')
        cls.unassigned = cls.bd('create', 'Unrelated queued task')
        cls.others = cls.bd('create', 'Unrelated task of another role', '--assignee', 'other-role')
        cls.bd('update', cls.others, '--status', 'in_progress')

        cls.model = MockModel()
        cls.hold = threading.Event()
        cls.model.rules += [('HOLD-THIS-TURN', 'hold', cls.hold),
                            ('CHECK-WORKDIR', 'call', 'exec_command', {'cmd': 'cat NOTES.txt; git status --short'})]
        (cls.home / 'codex').mkdir()
        (cls.home / 'codex' / 'config.toml').write_text(cls.model.codex_config(cls.work))

        ring_port = free_port()
        cls.ringd = subprocess.Popen([str(REPO / 'bin' / 'ringd'), '--port', str(ring_port),
                                      '--db', str(root / 'ring.db')], env=cls.env,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        cls.config = root / 'role.json'
        cls.config.write_text(json.dumps({
            'role': ROLE, 'provider': 'codex', 'model': 'gpt-6-astra',
            'brief': 'FIRST-BRIEF: you are the experiment. CHECK-WORKDIR', 'workdir': str(cls.work),
            'tmux_socket': cls.socket, 'ring_url': f'http://127.0.0.1:{ring_port}', 'channels': ['exp'],
            'tracker_dir': str(cls.tracker), 'codex': CODEX, 'env': {'CODEX_HOME': str(cls.home / 'codex')},
        }, indent=1))
        wait('ringd', lambda: cls.ring2('post', 'exp', 'bus is up', check=False).returncode == 0)

    @classmethod
    def cleanup(cls):
        if hasattr(cls, 'config'):
            cls.ring2('stop', check=False)
        subprocess.run(['tmux', '-L', cls.socket, 'kill-server'], capture_output=True)
        socket_file = Path(f'/tmp/tmux-{os.getuid()}') / cls.socket
        if socket_file.is_socket():
            socket_file.unlink()
        if hasattr(cls, 'ringd'):
            cls.ringd.terminate()
            cls.ringd.wait()
        if hasattr(cls, 'model'):
            cls.hold.set()
            cls.model.close()
        if os.environ.get('RING2_KEEP'):
            print(f'kept {cls.root}', file=sys.stderr)
        else:
            shutil.rmtree(cls.root, ignore_errors=True)

    # helpers

    @classmethod
    def run_ok(cls, argv, cwd=None):
        return subprocess.run(argv, cwd=cwd, env=cls.env, capture_output=True, text=True, check=True).stdout

    @classmethod
    def bd(cls, *args):
        out = json.loads(cls.run_ok(['bd', *args, '--json'], cwd=cls.tracker))
        return (out[0] if isinstance(out, list) else out)['id']

    @classmethod
    def ring2(cls, command, *args, check=True):
        result = subprocess.run([sys.executable, '-m', 'ring2', command, str(cls.config), *args],
                                cwd=cls.root, env=cls.env, capture_output=True, text=True)
        if check and result.returncode:
            raise AssertionError(f'ring2 {command} failed: {result.stdout}{result.stderr}')
        return result

    def tmux(self, *args):
        return subprocess.run(['tmux', '-L', self.socket, *args], capture_output=True, text=True).stdout.strip()

    def state(self):
        return json.loads(self.ring2('status').stdout)

    def post(self, text):
        return json.loads(self.ring2('post', 'exp', text).stdout)['sequence']

    def requests_with(self, needle):
        return [request for request in list(self.model.requests)
                if any(needle in text for text in self.model.user_texts(request))
                and not any('single-line task title' in text for text in self.model.user_texts(request))]

    def rollout(self, thread):
        path, = (self.home / 'codex' / 'sessions').glob(f'*/*/*/rollout-*{thread}.jsonl')
        return path.read_text()

    def pane_pids(self):
        return [int(pid) for pid in self.tmux('list-panes', '-s', '-t', f'ring2-{ROLE}', '-F', '#{pane_pid}').split()]

    # the five steps

    def test_1_start_from_minimal_config(self):
        self.ring2('start')
        status = self.state()
        self.assertTrue(status['running'])
        self.assertEqual(self.tmux('list-windows', '-t', f'ring2-{ROLE}', '-F', '#{window_name}').split(),
                         ['server', 'tui', 'follow'])
        first = wait('the startup turn to reach the model', lambda: self.requests_with('FIRST-BRIEF'))[0]
        prompt = self.model.user_texts(first)[-1]
        self.assertIn(f'You are role {ROLE}', prompt)
        self.assertIn(str(self.work), prompt)
        wait('the startup turn to end', lambda: '"task_complete"' in self.rollout(status['state']['thread']))
        type(self).first = status['state']

    def test_2_message_during_a_turn_waits_for_its_end(self):
        thread = self.first['thread']
        self.post(f'@{ROLE} HOLD-THIS-TURN (the mock keeps this turn active)')
        wait('the held turn to start', lambda: self.requests_with('HOLD-THIS-TURN'))
        self.post('@other-role NOT-FOR-EXP-ROLE')
        sequence = self.post(f'@{ROLE} SECOND-MESSAGE sent while the turn is active')
        cursor = self.root / 'state' / f'{ROLE}.cursor'
        wait('the follower to hand the message to Codex', lambda: cursor.read_text() == str(sequence))
        time.sleep(4)  # ample time for a wrong delivery to show up
        rollout = self.rollout(thread)
        self.assertEqual(self.requests_with('SECOND-MESSAGE'), [], 'delivered into the active turn')
        self.assertEqual((rollout.count('"type":"task_started"'), rollout.count('"type":"task_complete"')),
                         (2, 1), 'the held turn should still be the active one')
        self.hold.set()
        delivered = wait('delivery after the turn ended', lambda: self.requests_with('SECOND-MESSAGE'))[0]
        # The request that carries the message shows the held turn finished first, with its own reply.
        items = [(item.get('role'), json.dumps(item.get('content'))) for item in delivered['input']
                 if item.get('type') == 'message' and item.get('role') in ('user', 'assistant')]
        held = next(i for i, (role, text) in enumerate(items) if role == 'user' and 'HOLD-THIS-TURN' in text)
        self.assertEqual(items[held + 1][0], 'assistant')
        self.assertIn('REPLY-', items[held + 1][1])
        self.assertEqual(items[held + 2][0], 'user')
        self.assertIn('SECOND-MESSAGE', items[held + 2][1])
        wait('the delivered turn to end', lambda: self.rollout(thread).count('"type":"task_complete"') == 3)
        rollout = self.rollout(thread)
        self.assertNotIn('turn_aborted', rollout)
        self.assertNotIn('NOT-FOR-EXP-ROLE', rollout)

    def test_3_restart_with_one_command(self):
        (self.work / 'NOTES.txt').write_text('dirty-marker\n')        # untracked
        (self.work / 'tracked.txt').write_text('committed\nedited\n')  # modified
        old_pids = self.pane_pids()
        self.assertEqual(len(old_pids), 3)
        self.ring2('restart')
        new = self.state()
        self.assertTrue(new['running'])
        self.assertNotEqual(new['state']['token'], self.first['token'])
        self.assertNotEqual(new['state']['thread'], self.first['thread'])
        wait('the old session processes to exit', lambda: not any(alive(pid) for pid in old_pids))
        self.assertEqual(self.tmux('list-sessions', '-F', '#{session_name} #{@ring2_token}'),
                         f'ring2-{ROLE} {new["state"]["token"]}')
        type(self).second = new['state']

    def test_4_second_message_reaches_the_new_session(self):
        wait('the successor startup turn to end',
             lambda: '"task_complete"' in self.rollout(self.second['thread']))
        self.post(f'@{ROLE} THIRD-MESSAGE for the successor')
        delivered = wait('delivery to the successor', lambda: self.requests_with('THIRD-MESSAGE'))[0]
        texts = '\n'.join(self.model.user_texts(delivered))
        self.assertIn(f'(ring2 session {self.second["token"]})', texts)
        self.assertNotIn('SECOND-MESSAGE', texts, 'the old thread is not the target')
        wait('the turn to be recorded on the new thread',
             lambda: 'THIRD-MESSAGE' in self.rollout(self.second['thread']))
        self.assertNotIn('THIRD-MESSAGE', self.rollout(self.first['thread']))

    def test_5_successor_finds_its_task_and_dirty_workdir(self):
        self.assertEqual(json.loads(self.ring2('task').stdout),
                         [{'id': self.mine, 'title': 'Slice task of the experimental role'}])
        token = f'(ring2 session {self.second["token"]})'
        startup = self.requests_with(token)[0]
        prompt = self.model.user_texts(startup)[-1]
        self.assertIn(f'Your task: {self.mine} ', prompt)
        self.assertNotIn(self.unassigned, prompt)
        self.assertNotIn(self.others, prompt)
        # The successor itself ran `cat NOTES.txt; git status --short` in its working directory.
        outputs = [item['output'] for request in self.requests_with(token) for item in request['input']
                   if item.get('type') == 'function_call_output']
        self.assertTrue(outputs, 'the successor made no tool call')
        self.assertIn('dirty-marker', str(outputs[0]))
        self.assertIn(' M tracked.txt', str(outputs[0]))
        self.assertIn('?? NOTES.txt', str(outputs[0]))
        # Nothing was claimed or changed in the tracker by the restart.
        listing = json.loads(self.run_ok(['bd', 'list', '--status', 'open,in_progress', '--json'], cwd=self.tracker))
        self.assertEqual({issue['id']: (issue['status'], issue.get('assignee')) for issue in listing},
                         {self.mine: ('in_progress', ROLE), self.unassigned: ('open', None),
                          self.others: ('in_progress', 'other-role')})

    def test_6_restart_spares_a_session_that_reuses_the_name(self):
        self.ring2('stop')
        self.assertFalse(self.state()['running'])
        self.tmux('new-session', '-d', '-s', f'ring2-{ROLE}', 'sleep 600')  # someone else, same name
        stranger = self.pane_pids()
        result = self.ring2('restart', check=False)
        self.assertIn('is not running', result.stdout)
        self.assertNotEqual(result.returncode, 0, 'start must refuse the taken name')
        self.assertIn('duplicate session', result.stderr)
        self.assertEqual(self.pane_pids(), stranger)
        self.assertTrue(alive(stranger[0]))
        self.ring2('stop')
        self.assertTrue(alive(stranger[0]))


if __name__ == '__main__':
    unittest.main()
