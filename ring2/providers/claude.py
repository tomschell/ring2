"""Claude Code stream-json sessions. The runner owns stdin; each replayed user
message acknowledges receipt before delivery advances its cursor. Only one
turn is sent at a time. Handles, tokens and process ownership match headless.
Requires Claude Code 2.1.302; uses only the standard library.
"""
import json
import os
import signal
import subprocess
import sys
import threading
import uuid
from pathlib import Path

from ring2 import read_handle, session, write_handle

KIND, WORD = 'claude', 'claude'


class ClaudeTurns:
    def __init__(self, process, settings, state):
        self.process, self.settings, self.state = process, settings, state
        self.condition = threading.Condition()
        self.pending, self.busy, self.dead = None, False, False
        threading.Thread(target=self.read, daemon=True).start()

    def read(self):
        try:
            for line in self.process.stdout:
                print(line.rstrip(), flush=True)
                event = json.loads(line)
                with self.condition:
                    if event.get('type') == 'system' and event.get('subtype') == 'init':
                        if event.get('mcp_servers') or event.get('permissionMode') != 'bypassPermissions':
                            raise RuntimeError('Claude started with MCP servers or permission checks')
                    if event.get('type') == 'user' and event.get('isReplay') and event.get('uuid') == self.pending:
                        self.pending = None
                    if event.get('type') == 'result':
                        self.busy = False
                        self.state['busy'] = False
                        write_handle(self.settings, self.state)
                    self.condition.notify_all()
        finally:
            with self.condition:
                self.dead = True
                self.condition.notify_all()
            # A dead input must make watch see a dead runner, including while it polls the bus.
            os.kill(os.getpid(), signal.SIGTERM)

    def own(self):
        pass  # only this runner owns its child's stdin

    def send(self, text):
        with self.condition:
            while self.busy and not self.dead:
                self.condition.wait()
            if self.dead:
                raise OSError('Claude exited')
            self.pending, self.busy = str(uuid.uuid4()), True
            self.state['busy'] = True
            write_handle(self.settings, self.state)
            self.process.stdin.write(json.dumps({'type': 'user', 'uuid': self.pending,
                'message': {'role': 'user', 'content': text}, 'parent_tool_use_id': None}) + '\n')
            self.process.stdin.flush()
            while self.pending and not self.dead:
                self.condition.wait()
            if self.dead:
                raise OSError('Claude exited before acknowledging the message')

    def started(self, marker):
        return True  # send waited for this exact user UUID to be replayed


def run(spec_path, token):
    from ring2 import bus, delivery
    spec = json.loads(Path(spec_path).read_text())
    settings = spec['settings']
    thread = spec.get('resume') or str(uuid.uuid4())
    argv = [settings.get('claude') or 'claude', '-p', '--input-format', 'stream-json',
            '--output-format', 'stream-json', '--verbose', '--replay-user-messages',
            '--permission-mode', 'bypassPermissions', '--strict-mcp-config',
            '--mcp-config', '{"mcpServers":{}}', '--setting-sources', '', '--no-chrome',
            '--disallowedTools', 'mcp__*', '--model', spec['model'],
            '--append-system-prompt', f'(ring2 session {token})',
            '--resume' if spec.get('resume') else '--session-id', thread]
    env = {**os.environ, **settings.get('env', {}), **spec.get('env', {}),
           'CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC': '1', 'DISABLE_AUTOUPDATER': '1',
           'DISABLE_TELEMETRY': '1', 'DISABLE_ERROR_REPORTING': '1',
           'ENABLE_CLAUDEAI_MCP_SERVERS': 'false',
           'CLAUDE_CODE_DISABLE_BACKGROUND_TASKS': '1'}
    process = subprocess.Popen(argv, cwd=spec['workdir'], env=env, stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, text=True, bufsize=1)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    state = {'name': spec['name'], 'kind': 'claude', 'token': token, 'pid': os.getpid(),
             'server_pid': process.pid, 'thread': thread, 'busy': False}
    try:
        target = ClaudeTurns(process, settings, state)
        target.send(f'{spec["prompt"]}\n(ring2 session {token})')
        source = spec.get('source')
        if not source:
            while True:
                signal.pause()
        delivery.follow(lambda after, wait: bus.events(source, after, wait),
                        lambda message: bus.addressed(source['label'], message['message']),
                        target, source['cursor'], log=lambda line: print(line, flush=True))
    finally:
        process.terminate()



def status(settings, name):
    state = read_handle(settings, name) or {}
    alive = session.running(settings, name, KIND)
    row = {'session': alive, 'loop': alive, 'busy': state.get('busy', False) and alive}
    cursor = Path(settings['state_dir']) / f'{name}.cursor'
    if cursor.exists():
        row.update(active=int(cursor.stat().st_mtime), active_source='cursor')
    return row


if __name__ == '__main__':
    run(*sys.argv[2:])
