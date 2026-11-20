"""One role session end to end: the real Codex binary in tmux, a mock model,
the local Ring server, a throwaway bd tracker and a bare origin.

Run: python3 -m unittest tests.ring2.test_slice -v
"""
import json
import threading
import time
import unittest
from pathlib import Path

from tests.ring2.lab import HAVE_TOOLS, Lab, alive, wait

ROLE = 'exp-role'


@unittest.skipUnless(HAVE_TOOLS, 'needs codex, tmux and bd')
class SliceTest(unittest.TestCase):
    """The steps in order; each later step builds on the earlier ones."""

    @classmethod
    def setUpClass(cls):
        cls.lab = lab = Lab({ROLE: ('implementer', 'FIRST-BRIEF: you are the experiment. CHECK-WORKDIR')})
        cls.addClassCleanup(lab.close)
        cls.work = lab.work[ROLE]
        # The tracker: one task of ours, two that are not.
        cls.mine = lab.task('Slice task of the experimental role', '--assignee', ROLE)
        lab.bd('update', cls.mine, '--status', 'in_progress')
        cls.unassigned = lab.task('Unrelated queued task', '--labels', 'queue')
        cls.others = lab.task('Unrelated task of another role', '--assignee', 'other-role')
        lab.bd('update', cls.others, '--status', 'in_progress')
        cls.holds = {name: threading.Event() for name in ('HOLD-THIS-TURN', 'HOLD-UNTIL-RESTART')}
        cls.addClassCleanup(lambda: [event.set() for event in cls.holds.values()])
        lab.model.rules += [(name, 'hold', event) for name, event in cls.holds.items()]
        lab.model.rules.append(('CHECK-WORKDIR', 'call', 'exec_command', {'cmd': 'cat NOTES.txt; git status --short'}))
        lab.post(f'@{ROLE} OLD-HISTORY from before this role ever started')

    # helpers

    def state(self):
        return json.loads(self.lab.ring2('status', label=ROLE).stdout)

    def rollout(self, state):
        return Path(state['rollout']).read_text()

    def cursor(self):
        return int((self.lab.root / 'state' / f'{ROLE}.cursor').read_text())

    def follower(self):
        return self.lab.tmux('capture-pane', '-p', '-t', f'ring2-{ROLE}:follow')

    def pane_pids(self):
        return [int(pid) for pid in
                self.lab.tmux('list-panes', '-s', '-t', f'ring2-{ROLE}', '-F', '#{pane_pid}').split()]

    def startup_done(self, state):
        wait('the startup turn to end', lambda: '"task_complete"' in self.rollout(state))

    # start, deliver, restart

    def test_1_start_from_minimal_config(self):
        self.lab.ring2('start', label=ROLE)
        status = self.state()
        self.assertTrue(status['running'])
        self.assertEqual(self.lab.tmux('list-windows', '-t', f'ring2-{ROLE}', '-F', '#{window_name}').split(),
                         ['server', 'tui', 'follow'])
        first = wait('the startup turn to reach the model', lambda: self.lab.requests_with('FIRST-BRIEF'))[0]
        prompt = self.lab.model.user_texts(first)[-1]
        self.assertIn(f'You are {ROLE} (implementer)', prompt)
        self.assertIn(str(self.work), prompt)
        self.startup_done(status['state'])
        type(self).first = status['state']

    def test_2_message_during_a_turn_waits_for_its_end(self):
        lab = self.lab
        lab.post(f'@{ROLE} HOLD-THIS-TURN (the mock keeps this turn active)')
        wait('the held turn to start', lambda: lab.requests_with('HOLD-THIS-TURN'))
        lab.post('@other-role NOT-FOR-EXP-ROLE')
        sequence = lab.post(f'@{ROLE} SECOND-MESSAGE sent while the turn is active')
        wait('the follower to hand the message to Codex', lambda: f'queued {sequence}' in self.follower())
        time.sleep(4)  # ample time for a wrong delivery to show up
        rollout = self.rollout(self.first)
        self.assertEqual(lab.requests_with('SECOND-MESSAGE'), [], 'delivered into the active turn')
        self.assertEqual((rollout.count('"type":"task_started"'), rollout.count('"type":"task_complete"')),
                         (2, 1), 'the held turn should still be the active one')
        self.assertNotIn('SECOND-MESSAGE', rollout)
        self.assertLess(self.cursor(), sequence, 'the cursor must not pass a message whose turn has not started')
        self.holds['HOLD-THIS-TURN'].set()
        delivered = wait('delivery after the turn ended', lambda: lab.requests_with('SECOND-MESSAGE'))[0]
        # The request that carries the message shows the held turn finished first, with its own reply.
        items = [(item.get('role'), json.dumps(item.get('content'))) for item in delivered['input']
                 if item.get('type') == 'message' and item.get('role') in ('user', 'assistant')]
        held = next(i for i, (role, text) in enumerate(items) if role == 'user' and 'HOLD-THIS-TURN' in text)
        self.assertEqual(items[held + 1][0], 'assistant')
        self.assertIn('REPLY-', items[held + 1][1])
        self.assertEqual(items[held + 2][0], 'user')
        self.assertIn('SECOND-MESSAGE', items[held + 2][1])
        wait('the delivered turn to end', lambda: self.rollout(self.first).count('"type":"task_complete"') == 3)
        wait('the cursor to pass the delivered message', lambda: self.cursor() == sequence)
        rollout = self.rollout(self.first)
        self.assertNotIn('turn_aborted', rollout)
        self.assertNotIn('NOT-FOR-EXP-ROLE', rollout)

    def test_3_restart_with_one_command(self):
        (self.work / 'NOTES.txt').write_text('dirty-marker\n')        # untracked
        (self.work / 'tracked.txt').write_text('committed\nedited\n')  # modified
        old_pids = self.pane_pids()
        self.assertEqual(len(old_pids), 3)
        self.lab.ring2('restart', label=ROLE)
        new = self.state()
        self.assertTrue(new['running'])
        self.assertNotEqual(new['state']['token'], self.first['token'])
        self.assertNotEqual(new['state']['thread'], self.first['thread'])
        wait('the old session processes to exit', lambda: not any(alive(pid) for pid in old_pids))
        self.assertEqual(self.lab.tmux('list-sessions', '-F', '#{session_name} #{@ring2_token}'),
                         f'ring2-{ROLE} {new["state"]["token"]}')
        type(self).second = new['state']

    def test_4_second_message_reaches_the_new_session(self):
        lab = self.lab
        self.startup_done(self.second)
        lab.post(f'@{ROLE} THIRD-MESSAGE for the successor')
        delivered = wait('delivery to the successor', lambda: lab.requests_with('THIRD-MESSAGE'))[0]
        texts = '\n'.join(lab.model.user_texts(delivered))
        self.assertIn(f'(ring2 session {self.second["token"]})', texts)
        self.assertNotIn('SECOND-MESSAGE', texts, 'the old thread is not the target')
        wait('the turn to be recorded on the new thread', lambda: 'THIRD-MESSAGE' in self.rollout(self.second))
        self.assertNotIn('THIRD-MESSAGE', self.rollout(self.first))

    def test_5_successor_finds_its_task_and_dirty_workdir(self):
        lab = self.lab
        self.assertEqual(lab.ring2('task', label=ROLE).stdout.strip(),
                         f'{self.mine} Slice task of the experimental role')
        token = f'(ring2 session {self.second["token"]})'
        startup = lab.requests_with(token)[0]
        prompt = lab.model.user_texts(startup)[-1]
        self.assertIn(f'Your task: {self.mine} ', prompt)
        self.assertNotIn(self.unassigned, prompt)
        self.assertNotIn(self.others, prompt)
        # The successor itself ran `cat NOTES.txt; git status --short` in its working directory.
        outputs = [item['output'] for request in lab.requests_with(token) for item in request['input']
                   if item.get('type') == 'function_call_output']
        self.assertTrue(outputs, 'the successor made no tool call')
        self.assertIn('dirty-marker', str(outputs[0]))
        self.assertIn(' M tracked.txt', str(outputs[0]))
        self.assertIn('?? NOTES.txt', str(outputs[0]))
        # Nothing was claimed or changed in the tracker by the restart.
        listing = lab.bd('list', '--status', 'open,in_progress')
        self.assertEqual({issue['id']: (issue['status'], issue.get('assignee')) for issue in listing},
                         {self.mine: ('in_progress', ROLE), self.unassigned: ('open', None),
                          self.others: ('in_progress', 'other-role')})

    # delivery gaps

    def test_6_follower_survives_a_failing_codex_queue(self):
        lab = self.lab
        (lab.shims / 'refuse-codex').write_text('queue')
        sequence = lab.post(f'@{ROLE} QUEUE-REFUSED-AT-FIRST')
        later = lab.post(f'@{ROLE} SENT-AFTER-THE-REFUSED-ONE')
        wait('the follower to report the refusal', lambda: f'did not take {sequence}' in self.follower())
        time.sleep(3)
        self.assertEqual(lab.requests_with('SENT-AFTER-THE-REFUSED-ONE'), [], 'the refused message was skipped')
        self.assertLess(self.cursor(), sequence)
        (lab.shims / 'refuse-codex').unlink()
        wait('both messages to arrive', lambda: lab.requests_with('SENT-AFTER-THE-REFUSED-ONE'))
        rollout = self.rollout(self.second)
        self.assertLess(rollout.index('QUEUE-REFUSED-AT-FIRST'), rollout.index('SENT-AFTER-THE-REFUSED-ONE'))
        wait('the cursor to pass both', lambda: self.cursor() == later)
        self.assertIn('follow', self.lab.tmux('list-windows', '-t', f'ring2-{ROLE}', '-F', '#{window_name}'))

    def test_7_message_queued_but_not_started_survives_a_restart(self):
        lab = self.lab
        lab.post(f'@{ROLE} HOLD-UNTIL-RESTART (this turn never ends in the old session)')
        wait('the held turn to start', lambda: lab.requests_with('HOLD-UNTIL-RESTART'))
        sequence = lab.post(f'@{ROLE} QUEUED-WHEN-THE-ROLE-RESTARTED')
        wait('the follower to hand the message to Codex', lambda: f'queued {sequence}' in self.follower())
        time.sleep(2)
        self.assertLess(self.cursor(), sequence)
        self.lab.ring2('restart', label=ROLE)
        third = self.state()['state']
        delivered = wait('delivery to the successor', lambda: lab.requests_with('QUEUED-WHEN-THE-ROLE-RESTARTED'))[0]
        self.assertIn(f'(ring2 session {third["token"]})', '\n'.join(lab.model.user_texts(delivered)))
        self.assertNotIn('QUEUED-WHEN-THE-ROLE-RESTARTED', self.rollout(self.second))
        wait('the cursor to pass it', lambda: self.cursor() == sequence)

    def test_8_first_start_skipped_the_retained_history(self):
        self.assertEqual(self.lab.requests_with('OLD-HISTORY'), [])
        self.assertEqual(self.lab.requests_with('bus is up'), [])

    # restart edge cases

    def test_9a_restart_cut_off_after_stop_is_finished_by_a_rerun(self):
        self.lab.ring2('stop', label=ROLE)          # the first half of a restart, then nothing
        self.assertFalse(self.state()['running'])
        result = self.lab.ring2('restart', label=ROLE)
        self.assertIn('is not running', result.stdout)
        self.assertTrue(self.state()['running'])
        self.startup_done(self.state()['state'])
        self.assertTrue((self.work / 'NOTES.txt').exists())

    def test_9b_restart_spares_a_session_that_reuses_the_name(self):
        self.lab.ring2('stop', label=ROLE)
        self.assertFalse(self.state()['running'])
        self.lab.tmux('new-session', '-d', '-s', f'ring2-{ROLE}', 'sleep 600')  # someone else, same name
        stranger = self.pane_pids()
        result = self.lab.ring2('restart', label=ROLE, check=False)
        self.assertIn('is not running', result.stdout)
        self.assertNotEqual(result.returncode, 0, 'start must refuse the taken name')
        self.assertIn('duplicate session', result.stderr)
        self.assertEqual(self.pane_pids(), stranger)
        self.lab.ring2('stop', label=ROLE)
        self.assertTrue(alive(stranger[0]))


if __name__ == '__main__':
    unittest.main()
