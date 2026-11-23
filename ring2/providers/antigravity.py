"""Antigravity stream-json sessions, one process per role and one turn at a time."""
import json
import os
import signal
import subprocess
import sys
import threading
from pathlib import Path

from ring2 import read_handle, session, write_handle

KIND, WORD = 'antigravity', 'antigravity'


class AntigravityTurns:
    def __init__(self, process, settings, state):
        self.process, self.settings, self.state = process, settings, state
        self.condition = threading.Condition()
        self.busy, self.dead = False, False
        threading.Thread(target=self.read, daemon=True).start()

    def read(self):
        try:
            for line in self.process.stdout:
                print(line.rstrip(), flush=True)
                event = json.loads(line)
                with self.condition:
                    if event.get('event') == 'init':
                        self.state['thread'] = event['conversation_id']
                        write_handle(self.settings, self.state)
                    if event.get('event') == 'result':
                        self.busy = self.state['busy'] = False
                        write_handle(self.settings, self.state)
                    self.condition.notify_all()
        finally:
            with self.condition:
                self.dead = True
                self.condition.notify_all()
            # A dead process must also end a runner that is polling the bus.
            os.kill(os.getpid(), signal.SIGTERM)

    def own(self):
        pass  # only this runner owns its child's stdin

    def send(self, text):
        with self.condition:
            while self.busy and not self.dead:
                self.condition.wait()
            if self.dead:
                raise OSError('Antigravity exited')
            self.busy = self.state['busy'] = True
            try:
                if self.state['thread']:
                    write_handle(self.settings, self.state)
                self.process.stdin.write(json.dumps({'event': 'user', 'message': {'content': text}}) + '\n')
                self.process.stdin.flush()
            except OSError as exc:
                raise RuntimeError('Antigravity handle or input write failed') from exc
            while self.busy and not self.dead:
                self.condition.wait()
            if self.dead:
                raise OSError('Antigravity exited before its result')

    def started(self, marker):
        return True  # send waited for this turn's result


def run(spec_path, token):
    from ring2 import bus, delivery
    spec = json.loads(Path(spec_path).read_text())
    settings = spec['settings']
    argv = ['agy', '--input-format', 'stream-json', '--output-format', 'stream-json',
            '--dangerously-skip-permissions', '--model', spec['model']]
    if spec.get('resume'):
        argv += ['--conversation', spec['resume']]
    env = {**os.environ, **settings.get('env', {}), **spec.get('env', {})}
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    process = subprocess.Popen(argv, cwd=spec['workdir'], env=env, stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, text=True, bufsize=1)
    state = {'name': spec['name'], 'kind': KIND, 'token': token, 'pid': os.getpid(),
             'server_pid': process.pid, 'thread': None, 'busy': False}
    try:
        target = AntigravityTurns(process, settings, state)
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
    finally:
        process.terminate()
        process.wait()


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
