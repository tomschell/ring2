"""The watch and console slices, one case each, against a real headless session:
the runner is killed while a message waits; the console shows it dead; watch
restarts it; the message is delivered; the console shows it alive.

Run: python3 -m unittest tests.test_watch_console -v
"""
import json
import os
import re
import signal
import subprocess
import sys
import time
import unittest
import urllib.request

from tests.lab import HAVE_TOOLS, Lab, wait

ROLE = 'watched'


@unittest.skipUnless(HAVE_TOOLS, 'needs codex and bd')
class WatchConsoleTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.lab = lab = Lab({ROLE: ('implementer', 'WATCH-BRIEF', {'headless': True})})
        cls.addClassCleanup(lab.close)
        cls.port = lab.free_port()

    def background(self, module, *args):
        lab = self.lab
        process = subprocess.Popen([sys.executable, '-m', module, '--config', str(lab.config), *args],
                                   cwd=lab.root, env=lab.env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(lambda: (process.kill(), process.wait()))
        return process

    def row(self):
        """The role's row on the page, as the words in its cells."""
        with urllib.request.urlopen(f'http://127.0.0.1:{self.port}/', timeout=30) as response:
            text = response.read().decode()
        found = re.search(rf'<tr id="{ROLE}">(.*?)</tr>', text, re.S)
        return re.findall(r'<td[^>]*>(.*?)</td>', found.group(1)) if found else None

    def status(self):
        return json.loads(self.lab.ring2('status', '--json').stdout)[0]

    def test_a_dead_runner_with_a_message_waiting_is_seen_shown_and_restarted(self):
        lab = self.lab
        task = lab.task('held by the watched role', '--assignee', ROLE)
        lab.bd('update', task, '--status', 'in_progress')
        lab.ring2('start', label=ROLE)
        self.background('ring2.console', '--port', str(self.port))
        wait('the console', lambda: self.safe_row())
        self.assertEqual(self.row()[:4], [f'{ROLE} (implementer)', 'alive', 'alive', task])
        healthy = self.status()
        self.assertEqual((healthy['session'], healthy['loop'], healthy['tasks']), (True, True, [task]))
        self.assertIn(f'{ROLE}: session alive, loop alive, tasks {task}', lab.ring2('status').stdout)

        # The runner dies right after it took one message, and another arrives with nobody to deliver it.
        first = lab.post(f'@{ROLE} TAKEN-BEFORE-THE-DEATH')
        wait('the first message to be delivered', lambda: lab.requests_with('TAKEN-BEFORE-THE-DEATH'))
        wait('the cursor to pass it', lambda: self.status()['delivered'] == first)
        os.kill(healthy_pid := json.loads(lab.ring2('status', label=ROLE).stdout)['state']['pid'], signal.SIGKILL)
        wait('the runner to be gone', lambda: not self.status()['loop'])
        waiting = lab.post(f'@{ROLE} WAITING-FOR-A-DEAD-RUNNER')
        dead = self.status()
        self.assertEqual((dead['loop'], dead['delivered'], dead['addressed']), (False, first, waiting))
        self.assertEqual(self.row()[2], 'dead')
        time.sleep(3)
        self.assertEqual(lab.requests_with('WAITING-FOR-A-DEAD-RUNNER'), [], 'nothing delivers it without watch')

        self.background('ring2.watch', '--interval', '1', '--backoff', '600')
        wait('watch to restart the role and the message to arrive', lambda: lab.requests_with('WAITING-FOR-A-DEAD-RUNNER'))
        wait('the console to show it alive', lambda: self.row()[1:3] == ['alive', 'alive'])
        wait('the cursor to pass the waiting message', lambda: self.status()['delivered'] >= waiting)
        said = [message['message'] for message in lab.messages() if message['message'].startswith('watch: ')]
        self.assertEqual(said, [f'watch: restarted {ROLE}; its delivery was not running and message {waiting} '
                                f'was waiting (delivered up to {first})'])
        self.assertEqual(len(lab.requests_with('TAKEN-BEFORE-THE-DEATH')), 1, 'the delivered message is not sent again')
        self.assertNotEqual(json.loads(lab.ring2('status', label=ROLE).stdout)['state']['pid'], healthy_pid)
        self.assertIn('watch: restarted', self.page_text())

        # Backoff: a second death straight away is not answered by a second restart.
        os.kill(json.loads(lab.ring2('status', label=ROLE).stdout)['state']['pid'], signal.SIGKILL)
        wait('the runner to be gone', lambda: not self.status()['loop'])
        lab.post(f'@{ROLE} SECOND-DEATH')
        time.sleep(5)
        self.assertFalse(self.status()['loop'])
        self.assertEqual(len([m for m in lab.messages() if m['message'].startswith('watch: ')]), 1)

    def safe_row(self):
        try:
            return self.row()
        except OSError:
            return None

    def page_text(self):
        with urllib.request.urlopen(f'http://127.0.0.1:{self.port}/', timeout=30) as response:
            return response.read().decode()


if __name__ == '__main__':
    unittest.main()
