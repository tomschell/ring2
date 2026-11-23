"""Gemini CLI sessions: one stream-json process per turn."""
import json
import os
import signal
import subprocess
import sys
from pathlib import Path

from ring2 import read_handle, session, write_handle

KIND, WORD = 'gemini', 'gemini'


class GeminiTurns:
    def __init__(self, spec, token):
        self.spec, self.settings = spec, spec['settings']
        self.state = {'name': spec['name'], 'kind': KIND, 'token': token,
                      'pid': os.getpid(), 'thread': spec.get('resume'), 'busy': False}

    def own(self):
        pass

    def send(self, text):
        argv = ['gemini', '-p', text, '--output-format', 'stream-json',
                '--approval-mode', 'yolo', '-m', self.spec['model']]
        if self.state['thread']:
            argv += ['-r', self.state['thread']]
        self.state['busy'] = True
        if (read_handle(self.settings, self.state['name']) or {}).get('pid') == os.getpid():
            write_handle(self.settings, self.state)
        env = {**os.environ, **self.settings.get('env', {}), **self.spec.get('env', {})}
        process = subprocess.Popen(argv, cwd=self.spec['workdir'], env=env,
                                   stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   text=True, bufsize=1)
        result, initialized = False, False
        try:
            for line in process.stdout:
                print(line.rstrip(), flush=True)
                event = json.loads(line)
                if event.get('type') == 'init':
                    if event.get('mcp_servers'):
                        raise RuntimeError('Gemini started with MCP servers')
                    self.state.update(thread=event['session_id'], server_pid=process.pid)
                    write_handle(self.settings, self.state)
                    initialized = True
                if event.get('type') == 'result':
                    result = True
            code = process.wait()
            if code or not initialized or not result:
                raise RuntimeError('Gemini exited without a successful complete turn')
            self.state['busy'] = False
            write_handle(self.settings, self.state)
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait()
            process.stdout.close()

    def started(self, marker):
        return True  # send waited for the run to finish after its result


def run(spec_path, token):
    from ring2 import bus, delivery
    spec = json.loads(Path(spec_path).read_text())
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    target = GeminiTurns(spec, token)
    source = spec.get('source')
    if source:
        delivery.init_cursor(lambda after, wait: bus.events(source, after, wait), source['cursor'])
    target.send(f'{spec["prompt"]}\n(ring2 session {token})')
    if not source:
        while True:
            signal.pause()
    delivery.follow(lambda after, wait: bus.events(source, after, wait),
                    lambda message: bus.addressed(source['label'], message['message']),
                    target, source['cursor'], log=lambda line: print(line, flush=True))


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
