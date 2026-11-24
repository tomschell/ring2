"""Real Claude Code, local Messages stand-in, throwaway config; no sign-in or provider calls."""
import json
import os
import re
import shutil
import signal
import sys
import threading
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ring2.providers import claude

from tests.lab import HAVE_TOOLS, REPO, Lab, alive, wait
from tests.mock_claude import MockClaude
from tests.mock_model import MockModel


class ClaudeCommandTest(unittest.TestCase):
    def test_start_disallows_builtin_agent_tools(self):
        with tempfile.TemporaryDirectory() as directory:
            spec = Path(directory) / 'spec.json'
            spec.write_text(json.dumps({'settings': {}, 'model': 'test', 'workdir': directory}))
            with patch.object(claude.subprocess, 'Popen', side_effect=RuntimeError('stop')) as start:
                with self.assertRaisesRegex(RuntimeError, 'stop'):
                    claude.run(spec, 'token')
            argv = start.call_args.args[0]
            denied = argv[argv.index('--disallowedTools') + 1:argv.index('--model')]
            self.assertEqual(denied, ['mcp__*', 'SendMessage', 'Agent', 'Task'])


@unittest.skipUnless(HAVE_TOOLS and shutil.which('claude'), 'needs claude, codex and bd')
class ClaudeTest(unittest.TestCase):
    def setUp(self):
        self.lab = lab = Lab({'cc': ('implementer', 'CLAUDE-FIRST', {'provider': 'claude', 'model': 'claude-sonnet-4-6'}),
                              'cx': ('reviewer', 'CODEX-FIRST')})
        self.addCleanup(lab.close)
        self.model = MockClaude()
        self.addCleanup(self.model.close)
        self.config_dir = lab.home / 'claude'
        self.config_dir.mkdir()
        lab.settings['env'].update(self.model.env(self.config_dir))
        lab.write_config('ring2.json')
        self.held = []
        self.addCleanup(lambda: [event.set() for event in self.held])

    def state(self):
        return json.loads(self.lab.ring2('status', label='cc').stdout)

    def requests(self, needle):
        return [r for r in list(self.model.requests) if needle in json.dumps(r['messages'][-1])]

    def cursor(self):
        return int((self.lab.root / 'state/cc.cursor').read_text())

    def idle(self):
        wait('Claude idle', lambda: not self.state()['state'].get('busy', True))

    def test_start_ready_and_isolation(self):
        lab = self.lab
        # User and project MCP configurations must be excluded, even if configured.
        (self.config_dir / 'settings.json').write_text(json.dumps({'mcpServers': {
            'user-test': {'command': 'false'}}}))
        (lab.work['cc'] / '.mcp.json').write_text(json.dumps({'mcpServers': {
            'project-test': {'command': 'false'}}}))
        def ready(number, request):
            text = json.dumps(request)
            token = re.search(r'ring2 session ([0-9a-f]{32})', text).group(1)
            return self.model.call(number, f'{sys.executable} {REPO}/ring2 post exp "READY cc {token}"')
        self.model.rules.append(('fleet start: post', 'fn', ready))
        lab.ring2('start', label='cc')
        state = self.state()['state']
        lab.post('@cc fleet start: post READY')
        wait('READY', lambda: any(f'READY cc {state["token"]}' in m['message'] for m in lab.messages()))
        events = [json.loads(line) for line in (lab.root / 'state/cc.log').read_text().splitlines() if line.startswith('{')]
        init = next(e for e in events if e.get('subtype') == 'init')
        self.assertEqual(init['mcp_servers'], [])
        self.assertEqual(init['permissionMode'], 'bypassPermissions')
        self.assertTrue(init['analytics_disabled'])
        self.assertEqual(init['apiKeySource'], 'ANTHROPIC_API_KEY')
        self.assertTrue(self.state()['running'])
        self.idle()
        row = next(r for r in json.loads(lab.ring2('status', '--json').stdout) if r['label'] == 'cc')
        self.assertFalse(row['busy'])

    def test_message_once_across_restart_and_resume(self):
        lab = self.lab
        lab.settings['roles']['cc']['resume'] = True
        lab.write_config('ring2.json')
        lab.ring2('start', label='cc')
        first = self.state()['state']
        seq = lab.post('@cc ONCE-MESSAGE')
        wait('ack cursor', lambda: self.cursor() == seq)
        wait('message answered', lambda: self.requests('ONCE-MESSAGE'))
        self.idle()
        lab.ring2('restart', label='cc')
        self.assertEqual(self.state()['state']['thread'], first['thread'])
        lab.post('@cc AFTER-RESTART')
        request = wait('resumed reply', lambda: self.requests('AFTER-RESTART'))[0]
        self.assertIn('ONCE-MESSAGE', json.dumps(request['messages'][:-1]))
        occurrences = [r for r in self.model.requests if 'ONCE-MESSAGE' in json.dumps(r['messages'][-1])]
        self.assertEqual(len(occurrences), 1)
        self.assertFalse(alive(first['pid']) or alive(first['server_pid']))

    def test_waiting_message_answered_after_active_turn(self):
        hold = threading.Event()
        self.held.append(hold)
        self.model.rules.append(('HOLD-TURN', 'hold', hold))
        lab = self.lab
        lab.ring2('start', label='cc')
        held = lab.post('@cc HOLD-TURN')
        wait('held request', lambda: self.requests('HOLD-TURN'))
        self.assertLessEqual(self.cursor(), held)
        seq = lab.post('@cc WAITING-MESSAGE')
        self.assertTrue(self.state()['state']['busy'])
        self.assertLess(self.cursor(), seq)
        hold.set()
        wait('waiting answered', lambda: self.requests('WAITING-MESSAGE'))
        wait('waiting acknowledged', lambda: self.cursor() == seq)
        self.idle()
        self.assertEqual(len(self.requests('WAITING-MESSAGE')), 1)

    def test_stop_during_command_leaves_no_process(self):
        lab = self.lab
        pidfile = lab.root / 'tool.pid'
        self.model.rules.append(('RUN-LONG', 'call', f'echo $$ > {pidfile}; sleep 30031'))
        lab.ring2('start', label='cc')
        state = self.state()['state']
        lab.post('@cc RUN-LONG')
        wait('tool running', pidfile.exists)
        pid = int(pidfile.read_text())
        self.assertTrue(alive(pid))
        lab.ring2('stop', label='cc')
        self.assertFalse(self.state()['running'])
        wait('tool gone', lambda: not alive(pid))
        self.assertFalse(alive(state['pid']) or alive(state['server_pid']))

    def test_watch_restarts_dead_runner(self):
        lab = self.lab
        lab.ring2('start', label='cc')
        state = self.state()['state']
        self.idle()
        os.kill(state['pid'], signal.SIGKILL)
        wait('runner dead', lambda: not self.state()['running'])
        lab.post('@cc AFTER-WATCH')
        lab.run(sys.executable, '-c',
                'from ring2 import app, watch; '
                f'assert watch.once(app.load({str(lab.config)!r}), {{}}, 1) == ["cc"]')
        wait('watch delivered', lambda: self.requests('AFTER-WATCH'))
        self.assertNotEqual(self.state()['state']['token'], state['token'])
        self.assertFalse(alive(state['server_pid']))

    def test_mixed_fleet_task_end_to_end(self):
        lab = self.lab
        def cc_ready(number, request):
            token = re.search(r'ring2 session ([0-9a-f]{32})', json.dumps(request)).group(1)
            return self.model.call(number, f'{sys.executable} {REPO}/ring2 post exp "READY cc {token}"')
        def cx_ready(number, request):
            token = re.search(r'ring2 session ([0-9a-f]{32})', json.dumps(request)).group(1)
            return MockModel.call(number, 'exec_command', {'cmd': f'{sys.executable} {REPO}/ring2 post exp "READY cx {token}"'})
        self.model.rules.append(('fleet start: post', 'fn', cc_ready))
        lab.model.rules.append(('fleet start: post', 'fn', cx_ready))
        result = lab.ring2('fleet', 'start')
        self.assertIn('cc: READY', result.stdout)
        self.assertIn('cx: READY', result.stdout)
        task = lab.task('Mixed fleet task', '--labels', 'queue')
        ring = f'{sys.executable} {REPO}/ring2'
        sha = lab.root / 'commit.txt'
        command = (f'{ring} claim && echo done > mixed.txt && git add mixed.txt && git commit -m mixed '
                   f'&& git push origin HEAD:refs/heads/work && git rev-parse HEAD > {sha} && {ring} test {task} "$(cat {sha})" "test -f mixed.txt"')
        self.model.rules.append(('DO-TASK', 'call', command))
        lab.post('@cc DO-TASK')
        wait('test evidence', lambda: any('TEST PASS' in text for _, text in lab.comments(task)))
        commit = sha.read_text().strip()
        lab.model.rules.append(('REVIEW-TASK', 'call', 'exec_command', {'cmd': f'git fetch origin work && {ring} review-pass {task} {commit}'}))
        lab.post('@cx REVIEW-TASK')
        wait('review evidence', lambda: any(f'PASS {commit}' == text for _, text in lab.comments(task)))
        self.model.rules.append(('PUBLISH-TASK', 'call', f'{ring} publish {task} {commit} --close'))
        lab.post('@cc PUBLISH-TASK')
        wait('task closed', lambda: lab.issue(task)['status'] == 'closed')
        self.assertEqual(lab.origin_main(), commit)


if __name__ == '__main__':
    unittest.main()
