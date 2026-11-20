"""fleet start and stop: several real Codex sessions, each answering READY on
the bus by running the ring2 post command from inside its own session.

Run: python3 -m unittest tests.test_fleet -v
"""
import json
import re
import sys
import unittest

from tests.lab import HAVE_TOOLS, REPO, Lab
from tests.mock_model import MockModel

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


@unittest.skipUnless(HAVE_TOOLS, 'needs codex and bd')
class FleetTest(unittest.TestCase):
    def test_fleet_start_and_stop(self):
        lab = Lab({'alpha': ('implementer', 'brief'), 'taken': ('implementer', 'brief'),
                   'mute': ('implementer', 'brief'), 'beta': ('reviewer', 'brief'),
                   'outside': ('controller', 'unused', {'provider': 'external'})},
                  ready_seconds=20, test_command='python3 -m unittest')
        self.addCleanup(lab.close)
        del lab.settings['roles']['alpha']['brief']
        lab.write_config('ring2.json')
        lab.model.rules.append(('fleet start: post', 'fn', answer_ready))
        lab.ring2('start', label='taken')  # this role is already running

        result = lab.ring2('fleet', 'start', check=False)
        lines = result.stdout.splitlines()
        self.assertEqual(lines[-4:], ['alpha: READY', 'taken: unavailable', 'mute: unavailable', 'beta: READY'],
                         result.stdout + result.stderr)
        self.assertEqual(result.returncode, 1)
        prompt = json.loads((lab.root / 'state/alpha.spec.json').read_text())['prompt']
        shipped = (REPO / 'ring2/briefs/implementer.md').read_text().strip()
        self.assertTrue(prompt.startswith(shipped + '\n'))
        for line in ('Channel: exp',
                     'Team: alpha (implementer), taken (implementer), mute (implementer), beta (reviewer), outside (controller)',
                     f'Read a task: bd show <task> (run in {lab.tracker}).',
                     f'List the queue: bd list --label queue --no-assignee --status open --limit 0 (run in {lab.tracker}).',
                     'Test command: python3 -m unittest',
                     'Post only when your role says to or when you are blocked; when you have nothing to do, stop and wait for a message.'):
            self.assertIn(line, prompt.splitlines())
        self.assertIn('taken: did not start', result.stdout)
        # Each READY carries the token of the session that fleet start just created.
        posted = lab.ring2('post', 'exp', 'end of test')
        bus = [m['message'] for m in json.loads(lab.run(
            'curl', '-s', f'{lab.settings["ring_url"]}/messages?channel=exp'))['messages']]
        for label in ('alpha', 'beta'):
            state = json.loads(lab.ring2('status', label=label).stdout)
            self.assertTrue(state['running'])
            self.assertIn(f'{label}: READY {label} {state["state"]["token"]}', bus)
        self.assertIn(f'mute: READY mute {STALE}', bus)
        self.assertFalse(any('@outside fleet start:' in message for message in bus))
        self.assertFalse((lab.root / 'state/outside.json').exists())
        self.assertTrue(posted)

        lab.ring2('fleet', 'stop')
        self.assertFalse(json.loads(lab.ring2('status', label='taken').stdout)['running'])


if __name__ == '__main__':
    unittest.main()
