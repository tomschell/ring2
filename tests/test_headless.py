"""The same slice with a headless Codex: an app-server with no TUI and no tmux,
driven by ring2's runner. Real Codex binary, mock model.

Run: python3 -m unittest tests.test_headless -v
"""
import json
import os
import shlex
import signal
import subprocess
import sys
import threading
import time
import unittest

from tests.lab import CODEX, HAVE_TOOLS, Lab, alive, wait

ROLE = 'exp-role'


@unittest.skipUnless(HAVE_TOOLS, 'needs codex, tmux and bd')
class HeadlessTest(unittest.TestCase):
    """The steps in order; each later step builds on the earlier ones."""

    @classmethod
    def setUpClass(cls):
        cls.lab = lab = Lab({ROLE: ('implementer', 'FIRST-BRIEF: you are the experiment. CHECK-WORKDIR',
                                    {'headless': True})})
        cls.addClassCleanup(lab.close)
        cls.work = lab.work[ROLE]
        cls.resuming = lab.write_config('resume.json', roles={
            ROLE: {**lab.settings['roles'][ROLE], 'resume': True}})
        cls.mine = lab.task('Slice task of the experimental role', '--assignee', ROLE)
        lab.bd('update', cls.mine, '--status', 'in_progress')
        cls.unassigned = lab.task('Unrelated queued task', '--labels', 'queue')
        cls.holds = {name: threading.Event() for name in ('HOLD-THIS-TURN', 'HOLD-UNTIL-RESTART')}
        cls.addClassCleanup(lambda: [event.set() for event in cls.holds.values()])
        lab.model.rules += [(name, 'hold', event) for name, event in cls.holds.items()]
        lab.model.rules.append(('CHECK-WORKDIR', 'call', 'exec_command',
                                {'cmd': 'cat NOTES.txt; git status --short; touch WRITTEN-BY-THE-SESSION'}))
        lab.post(f'@{ROLE} OLD-HISTORY from before this role ever started')

    def state(self):
        return json.loads(self.lab.ring2('status', label=ROLE).stdout)

    def rollout(self, state):
        found = list((self.lab.home / 'codex' / 'sessions').glob(f'*/*/*/rollout-*{state["thread"]}.jsonl'))
        return found[0].read_text() if found else ''

    def cursor(self):
        return int((self.lab.root / 'state' / f'{ROLE}.cursor').read_text())

    def turns_done(self, state, count):
        wait(f'{count} completed turns', lambda: self.rollout(state).count('"type":"task_complete"') >= count)

    def test_1_start_without_tmux_or_tui(self):
        self.lab.ring2('start', label=ROLE)
        status = self.state()
        self.assertTrue(status['running'])
        self.assertEqual(status['state']['kind'], 'headless')
        self.assertEqual(self.lab.tmux('list-sessions'), '', 'no tmux session is involved')
        first = wait('the startup turn to reach the model', lambda: self.lab.requests_with('FIRST-BRIEF'))[0]
        self.assertIn(f'You are {ROLE} (implementer)', self.lab.model.user_texts(first)[-1])
        self.turns_done(status['state'], 1)
        # Checks are off: the session's own command wrote into the working directory.
        self.assertTrue((self.work / 'WRITTEN-BY-THE-SESSION').exists())
        (self.work / 'WRITTEN-BY-THE-SESSION').unlink()
        type(self).first = status['state']

    def test_2_message_during_a_turn_waits_on_the_bus(self):
        lab = self.lab
        held = lab.post(f'@{ROLE} HOLD-THIS-TURN (the mock keeps this turn active)')
        wait('the held turn to start', lambda: lab.requests_with('HOLD-THIS-TURN'))
        wait('the cursor to pass the started message', lambda: self.cursor() == held)
        sequence = lab.post(f'@{ROLE} SECOND-MESSAGE sent while the turn is active')
        time.sleep(4)  # ample time for a wrong delivery to show up
        rollout = self.rollout(self.first)
        self.assertEqual(lab.requests_with('SECOND-MESSAGE'), [], 'delivered into the active turn')
        self.assertEqual((rollout.count('"type":"task_started"'), rollout.count('"type":"task_complete"')), (2, 1))
        self.assertEqual(self.cursor(), held, 'the waiting message is still only on the bus')
        self.holds['HOLD-THIS-TURN'].set()
        delivered = wait('delivery after the turn ended', lambda: lab.requests_with('SECOND-MESSAGE'))[0]
        items = [(item.get('role'), json.dumps(item.get('content'))) for item in delivered['input']
                 if item.get('type') == 'message' and item.get('role') in ('user', 'assistant')]
        at = next(i for i, (role, text) in enumerate(items) if role == 'user' and 'HOLD-THIS-TURN' in text)
        self.assertEqual((items[at + 1][0], items[at + 2][0]), ('assistant', 'user'))
        self.assertIn('SECOND-MESSAGE', items[at + 2][1])
        wait('the cursor to pass it', lambda: self.cursor() == sequence)
        self.turns_done(self.first, 3)
        self.assertNotIn('turn_aborted', self.rollout(self.first))

    def test_3_restart_with_one_command(self):
        (self.work / 'NOTES.txt').write_text('dirty-marker\n')
        (self.work / 'tracked.txt').write_text('committed\nedited\n')
        self.lab.ring2('restart', label=ROLE)
        new = self.state()
        self.assertTrue(new['running'])
        self.assertNotEqual(new['state']['token'], self.first['token'])
        self.assertNotEqual(new['state']['thread'], self.first['thread'])
        self.assertFalse(alive(self.first['pid']) or alive(self.first['server_pid']))
        type(self).second = new['state']

    def test_4_second_message_and_task_and_dirty_workdir(self):
        lab = self.lab
        self.turns_done(self.second, 1)
        lab.post(f'@{ROLE} THIRD-MESSAGE for the successor')
        delivered = wait('delivery to the successor', lambda: lab.requests_with('THIRD-MESSAGE'))[0]
        texts = '\n'.join(lab.model.user_texts(delivered))
        self.assertIn(f'(ring2 session {self.second["token"]})', texts)
        self.assertNotIn('SECOND-MESSAGE', texts, 'a fresh thread, not the old one')
        self.assertIn(f'Your task: {self.mine} ', texts)
        self.assertNotIn(self.unassigned, texts)
        outputs = [item['output'] for item in delivered['input'] if item.get('type') == 'function_call_output']
        self.assertIn('dirty-marker', str(outputs[0]))
        self.assertIn(' M tracked.txt', str(outputs[0]))
        self.assertIn('?? NOTES.txt', str(outputs[0]))

    def test_5_a_person_attaches_a_tui_and_leaves(self):
        lab, state = self.lab, self.second
        self.turns_done(state, 2)
        lab.tmux('new-session', '-d', '-s', 'look', '-x', '150', '-y', '50', '-c', str(self.work),
                 '-e', f'CODEX_HOME={lab.home / "codex"}', shlex.join(
                     [CODEX, 'resume', state['thread'], '--remote', state['endpoint'], '--no-alt-screen',
                      '-c', 'check_for_update_on_startup=false']))
        screen = lambda: lab.tmux('capture-pane', '-p', '-S', '-200', '-t', 'look')
        wait('the attached TUI to show the session history', lambda: 'THIRD-MESSAGE' in screen())
        lab.post(f'@{ROLE} WHILE-SOMEONE-WATCHES')
        wait('delivery while attached', lambda: lab.requests_with('WHILE-SOMEONE-WATCHES'))
        wait('the TUI to show the new turn', lambda: 'WHILE-SOMEONE-WATCHES' in screen())
        lab.tmux('kill-session', '-t', 'look')  # the person leaves
        lab.post(f'@{ROLE} AFTER-THEY-LEFT')
        after = wait('delivery after detaching', lambda: lab.requests_with('AFTER-THEY-LEFT'))[0]
        self.assertIn('WHILE-SOMEONE-WATCHES', '\n'.join(lab.model.user_texts(after)))
        self.assertTrue(self.state()['running'])
        self.assertNotIn('turn_aborted', self.rollout(state))

    def test_6_restart_during_a_turn_loses_no_waiting_message(self):
        lab = self.lab
        lab.post(f'@{ROLE} HOLD-UNTIL-RESTART (this turn never ends in the old session)')
        wait('the held turn to start', lambda: lab.requests_with('HOLD-UNTIL-RESTART'))
        sequence = lab.post(f'@{ROLE} WAITING-WHEN-THE-ROLE-RESTARTED')
        time.sleep(2)
        self.assertLess(self.cursor(), sequence)
        lab.ring2('restart', label=ROLE, config=self.resuming)   # this time keeping the thread
        third = self.state()['state']
        self.assertEqual(third['thread'], self.second['thread'])
        delivered = wait('delivery to the successor', lambda: lab.requests_with('WAITING-WHEN-THE-ROLE-RESTARTED'))[0]
        texts = '\n'.join(lab.model.user_texts(delivered))
        self.assertIn(f'(ring2 session {third["token"]})', texts)
        self.assertIn('THIRD-MESSAGE', texts, 'the resumed thread kept its context')
        wait('the cursor to pass it', lambda: self.cursor() == sequence)
        self.assertEqual(len(lab.requests_with('WAITING-WHEN-THE-ROLE-RESTARTED')), 1)
        type(self).third = third

    def test_7_a_killed_runner_is_a_dead_session_until_restart(self):
        lab = self.lab
        os.kill(self.third['pid'], signal.SIGKILL)
        wait('the runner to be gone', lambda: not self.state()['running'])
        self.assertTrue(alive(self.third['server_pid']), 'the app-server is left behind')
        sequence = lab.post(f'@{ROLE} POSTED-WHILE-NOBODY-LISTENED')
        time.sleep(2)
        self.assertEqual(lab.requests_with('POSTED-WHILE-NOBODY-LISTENED'), [])
        lab.ring2('restart', label=ROLE)
        wait('the left-behind app-server to be stopped', lambda: not alive(self.third['server_pid']))
        wait('delivery to the successor', lambda: lab.requests_with('POSTED-WHILE-NOBODY-LISTENED'))
        wait('the cursor to pass it', lambda: self.cursor() == sequence)
        self.assertEqual(lab.requests_with('OLD-HISTORY'), [])

    def test_8_stop_spares_a_process_that_reuses_the_pid(self):
        lab = self.lab
        lab.ring2('stop', label=ROLE)
        stranger = subprocess.Popen(['sleep', '600'])
        self.addCleanup(lambda: (stranger.kill(), stranger.wait()))
        handle = lab.root / 'state' / f'{ROLE}.json'
        state = json.loads(handle.read_text())
        handle.write_text(json.dumps({**state, 'pid': stranger.pid, 'server_pid': stranger.pid}))
        self.assertIn('is not running', lab.ring2('stop', label=ROLE).stdout)
        self.assertFalse(self.state()['running'])
        self.assertIsNone(stranger.poll())

    def test_8a_stop_spares_a_session_that_reuses_the_port(self):
        lab, handle = self.lab, self.lab.root / 'state' / f'{ROLE}.json'
        lab.ring2('stop', label=ROLE, check=False)
        self.addCleanup(lambda: self.solo_run('stop'))
        self.assertEqual(self.solo_run('start').returncode, 0)
        other = json.loads(self.solo_run('status').stdout)['handle']
        # The stopped role's handle is kept, and its port is free for the next session to be given.
        handle.write_text(json.dumps({**json.loads(handle.read_text()), 'endpoint': other['endpoint']}))
        self.assertIn('is not running', lab.ring2('stop', label=ROLE).stdout)
        self.assertTrue(alive(other['pid']) and alive(other['server_pid']), 'stop ended the session that reused the port')
        row = next(row for row in json.loads(lab.ring2('status', '--json').stdout) if row['label'] == ROLE)
        self.assertFalse(row['session'], 'status took the other session for this role')
        self.assertIn('stopped', self.solo_run('stop').stdout)
        self.assertFalse(alive(other['server_pid']))

    # one start per name (the module alone: no tracker or bus call narrows the race)

    def solo(self, command):
        lab = self.lab
        config = lab.write_config('solo.json', state_dir=str(lab.root / 'state'), sessions={'solo': {
            'model': lab.settings['roles'][ROLE]['model'], 'workdir': str(self.work), 'prompt': 'SOLO-BRIEF'}})
        return [sys.executable, '-m', 'ring2.headless', '--config', str(config), command, 'solo']

    def solo_run(self, command):
        return subprocess.run(self.solo(command), cwd=self.lab.root, env=self.lab.env, capture_output=True, text=True)

    def runners(self):
        spec = self.lab.root / 'state' / 'solo.spec.json'
        return self.lab.run_any('pgrep', '-f', f'ring2.headless run {spec}').split()

    def test_9a_two_starts_at_once_leave_one_session(self):
        lab = self.lab
        lab.ring2('stop', label=ROLE, check=False)
        self.addCleanup(lambda: self.solo_run('stop'))
        starts = [subprocess.Popen(self.solo('start'), cwd=lab.root, env=lab.env,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(2)]
        outputs = [start.communicate(timeout=120) for start in starts]
        self.assertEqual(len(self.runners()), 1, 'two runners for one name')
        self.assertEqual(sorted(start.returncode != 0 for start in starts), [False, True], outputs)
        refused = outputs[[start.returncode != 0 for start in starts].index(True)]
        self.assertRegex(refused[1], 'is being started|is already running')
        status = json.loads(self.solo_run('status').stdout)
        self.assertTrue(status['running'])
        self.assertEqual(self.runners(), [str(status['handle']['pid'])])
        self.assertIn('stopped', self.solo_run('stop').stdout)
        self.assertEqual(self.runners(), [])

    def test_9b_a_start_cut_short_can_still_be_stopped(self):
        lab = self.lab
        self.addCleanup(lambda: self.solo_run('stop'))
        start = subprocess.Popen(self.solo('start'), cwd=lab.root, env=lab.env,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        wait('the runner to exist', self.runners)
        start.kill()   # cut before the session is up
        start.wait()
        self.solo_run('stop')
        self.assertEqual(self.runners(), [], 'the runner of the interrupted start was left behind')
        self.assertEqual(self.solo_run('start').returncode, 0)   # and the name is usable again
        self.assertEqual(len(self.runners()), 1)
        self.solo_run('stop')
        self.assertEqual(self.runners(), [])

    def test_9c_a_runner_killed_before_it_saved_its_server_leaves_one_stop_can_end(self):
        lab, handle = self.lab, self.lab.root / 'state' / 'solo.json'
        self.addCleanup(lambda: self.solo_run('stop'))
        start = subprocess.Popen(self.solo('start'), cwd=lab.root, env=lab.env,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        runner = int(wait('the runner to exist', self.runners)[0])
        server = int(wait('the runner to launch its app-server', lambda: lab.run_any('pgrep', '-P', str(runner)),
                          interval=0.01).split()[0])
        os.kill(runner, signal.SIGKILL)   # after the launch, before the handle names the server
        self.addCleanup(lambda: [os.kill(int(pid), signal.SIGKILL)
                                 for pid in [*lab.run_any('pgrep', '-P', str(server)).split(), server] if alive(int(pid))])
        start.wait()
        state = json.loads(handle.read_text())
        self.assertEqual((state.get('pid'), state.get('endpoint'), state.get('server_pid')), (None, None, None),
                         'the runner was killed too late for this test')
        self.assertTrue(alive(server), 'expected the app-server to outlive its runner')
        self.assertIn('stopped', self.solo_run('stop').stdout)
        self.assertFalse(alive(server), 'stop left the app-server of the killed runner running')
        self.assertEqual(lab.run_any('pgrep', '-f', state['token']), '')


if __name__ == '__main__':
    unittest.main()
