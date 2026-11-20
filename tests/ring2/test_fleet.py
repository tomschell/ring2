"""fleet start and stop: several real Codex sessions, each answering READY on
the bus by running the ring2 post command from inside its own session.

Run: python3 -m unittest tests.ring2.test_fleet -v
"""
import json
import re
import sys
import unittest

from tests.ring2.lab import HAVE_TOOLS, REPO, Lab
from tests.ring2.mock_model import MockModel

STALE = '0' * 32


def answer_ready(number, request):
    """What a model does with the READY request: read its own session token and post it."""
    text = json.dumps(request['input'])
    label = re.search(r'You are (\S+) \(', text).group(1)
    token = re.search(r'\(ring2 session ([0-9a-f]{32})\)', text).group(1)
    if label == 'mute':
        token = STALE  # as a leftover older session would answer
    return MockModel.call(number, 'exec_command',
                          {'cmd': f'{sys.executable} {REPO}/ring2 post exp "READY {label} {token}"'})


@unittest.skipUnless(HAVE_TOOLS, 'needs codex, tmux and bd')
class FleetTest(unittest.TestCase):
    def test_fleet_start_and_stop(self):
        lab = Lab({'alpha': ('implementer', 'brief'), 'taken': ('implementer', 'brief'),
                   'mute': ('implementer', 'brief'), 'beta': ('reviewer', 'brief')},
                  ready_seconds=20)
        self.addCleanup(lab.close)
        lab.model.rules.append(('fleet start: post', 'fn', answer_ready))
        lab.tmux('new-session', '-d', '-s', 'ring2-taken', 'sleep 600')  # this role cannot start

        result = lab.ring2('fleet', 'start', check=False)
        lines = result.stdout.splitlines()
        self.assertEqual(lines[-4:], ['alpha: READY', 'taken: unavailable', 'mute: unavailable', 'beta: READY'],
                         result.stdout + result.stderr)
        self.assertEqual(result.returncode, 1)
        self.assertIn('taken: did not start', result.stdout)
        # Each READY carries the token of the session that fleet start just created.
        posted = lab.ring2('post', 'exp', 'end of test')
        bus = [m['message'] for m in json.loads(lab.run(
            'curl', '-s', f'{lab.settings["ring_url"]}/messages?channel=exp'))['messages']]
        for label in ('alpha', 'beta'):
            state = json.loads(lab.ring2('status', label=label).stdout)
            self.assertTrue(state['running'])
            self.assertIn(f'READY {label} {state["state"]["token"]}', bus)
        self.assertIn(f'READY mute {STALE}', bus)
        self.assertTrue(posted)

        lab.ring2('fleet', 'stop')
        self.assertEqual(lab.tmux('list-sessions', '-F', '#{session_name}'), 'ring2-taken')


if __name__ == '__main__':
    unittest.main()
