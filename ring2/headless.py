"""Headless sessions: a Codex app-server with no TUI and no tmux, driven by a
runner process that owns the thread. A second kind of session beside
`ring2.sessions`, with its own kind of delivery; needs only the codex CLI.

The runner starts `codex app-server`, opens a thread over the app-server's
JSON-RPC (WebSocket), runs the startup prompt as the first turn, then takes
one message at a time from the source: start a turn, wait for it to end, take
the next. Nothing is queued inside Codex. A message that arrives during a turn
stays in the source until the turn ends, so send() returning means the turn
has STARTED with that text (the app-server said so), and the cursor moves then.

Starting a turn while another is active does not wait in Codex: it cuts into
the active turn. The runner therefore never calls turn/start before the turns
it has seen start have completed.

Identity: a random token, saved in the handle before anything is started. The
runner's command line carries it, and so does the app-server's (as a config
setting Codex ignores), so stop finds either even when the other died first.

A person can look at a running session and leave again without disturbing it:
  codex resume <thread> --remote <endpoint>

Settings: state_dir, codex, env. Codex's own approval prompts and sandbox are always off.
Spec: {model, workdir, prompt, resume (a thread id, optional),
       source: {ring_url, channels, label, cursor} (optional)}.

  python3 -m ring2.headless --config FILE start|stop|restart|status NAME
  (FILE: the settings above plus "sessions": {NAME: spec})
"""
import argparse
import base64
import fcntl
import json
import os
import signal
import socket
import struct
import subprocess
import sys
import time
import urllib.request
import uuid
from pathlib import Path
from urllib.parse import urlparse

from ring2 import Refused, need, read_handle, stop_processes, token_setting, write_handle

ROOT = Path(__file__).resolve().parent.parent


class RpcError(OSError):
    pass


class Rpc:
    """JSON-RPC over one WebSocket connection to a Codex app-server (text frames only)."""

    def __init__(self, endpoint):
        url = urlparse(endpoint)
        self.sock = socket.create_connection((url.hostname, url.port), timeout=30)
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall((f'GET / HTTP/1.1\r\nHost: {url.hostname}:{url.port}\r\nUpgrade: websocket\r\n'
                           f'Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n'
                           'Sec-WebSocket-Version: 13\r\n\r\n').encode())
        self.buffer, self.events, self.ids = b'', [], 0
        while b'\r\n\r\n' not in self.buffer:
            self._fill()
        head, self.buffer = self.buffer.split(b'\r\n\r\n', 1)
        if b' 101 ' not in head.split(b'\r\n')[0]:
            raise RpcError(f'WebSocket handshake refused: {head[:80]!r}')
        self.call('initialize', {'clientInfo': {'name': 'ring2', 'version': '1'},
                                 'capabilities': {'experimentalApi': True}})
        self._frame(json.dumps({'method': 'initialized', 'params': {}}).encode())

    def _fill(self):
        data = self.sock.recv(65536)
        if not data:
            raise RpcError('the app-server closed the connection')
        self.buffer += data

    def _take(self, count):
        while len(self.buffer) < count:
            self._fill()
        taken, self.buffer = self.buffer[:count], self.buffer[count:]
        return taken

    def _frame(self, payload, opcode=1):
        size, mask = len(payload), os.urandom(4)
        length = (bytes([0x80 | size]) if size < 126 else
                  bytes([0x80 | 126]) + struct.pack('!H', size) if size < 65536 else
                  bytes([0x80 | 127]) + struct.pack('!Q', size))
        self.sock.sendall(bytes([0x80 | opcode]) + length + mask
                          + bytes(byte ^ mask[i % 4] for i, byte in enumerate(payload)))

    def receive(self, deadline):
        """The next JSON message; RpcError if none arrives by `deadline`."""
        text = b''
        while True:
            self.sock.settimeout(max(0.05, deadline - time.monotonic()))
            try:
                first, second = self._take(2)
                size = second & 0x7F
                if size == 126:
                    size = struct.unpack('!H', self._take(2))[0]
                elif size == 127:
                    size = struct.unpack('!Q', self._take(8))[0]
                payload = self._take(size)
            except socket.timeout:
                raise RpcError('no message from the app-server in time')
            opcode = first & 0x0F
            if opcode == 9:
                self._frame(payload, 10)
            elif opcode == 8:
                raise RpcError('the app-server closed the connection')
            elif opcode in (0, 1):
                text += payload
                if first & 0x80:
                    return json.loads(text)

    def call(self, method, params, seconds=60):
        self.ids += 1
        self._frame(json.dumps({'id': self.ids, 'method': method, 'params': params}).encode())
        deadline = time.monotonic() + seconds
        while True:
            message = self.receive(deadline)
            if message.get('id') == self.ids and 'method' not in message:
                if 'error' in message:
                    raise RpcError(f'{method}: {message["error"]}')
                return message['result']
            self.events.append(message)


class CodexTurns:
    """A session input for delivery.follow: one turn per message, never two at once."""

    def __init__(self, rpc, thread):
        self.rpc, self.thread, self.active = rpc, thread, set()

    def own(self):
        """The runner created the thread on its own app-server; it is the only loop."""

    def _settle(self):
        """Wait until every turn this connection has seen start has completed."""
        while True:
            for message in self.rpc.events:
                turn = message.get('params', {}).get('turn', {}).get('id')
                if message.get('method') == 'turn/started':
                    self.active.add(turn)
                elif message.get('method') == 'turn/completed':
                    self.active.discard(turn)
            self.rpc.events.clear()
            if not self.active:
                return
            self.rpc.events.append(self.rpc.receive(time.monotonic() + 3600))

    def send(self, text):
        """Start a turn with `text` once the thread is idle. Returns when the app-server
        reports the turn in progress: the message is in the thread."""
        self._settle()
        turn = self.rpc.call('turn/start', {'threadId': self.thread, 'input': [{'type': 'text', 'text': text}]})
        self.active.add(turn['turn']['id'])
        return 'started'

    def started(self, marker):
        return True  # send() already waited for it


def _ready(url):
    try:
        return urllib.request.urlopen(url, timeout=2).status == 200
    except OSError:
        return False


def _wait(what, check, seconds=60):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = check()
        if value:
            return value
        time.sleep(0.2)
    raise SystemExit(f'ring2: timed out waiting for {what}')


def _command(pid):
    return subprocess.run(['ps', '-p', str(pid), '-o', 'command='], capture_output=True, text=True).stdout


def running(settings, name):
    state = read_handle(settings, name)
    return bool(state and state.get('kind') == 'headless' and state.get('pid')
                and state['token'] in _command(state['pid']))


def run(spec_path, token):
    """The runner: app-server, thread, first turn, then the delivery loop. Never returns."""
    from ring2 import bus, delivery
    spec = json.loads(Path(spec_path).read_text())
    settings, codex = spec['settings'], spec['settings'].get('codex') or 'codex'
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        port = probe.getsockname()[1]
    endpoint = f'ws://127.0.0.1:{port}'
    server = subprocess.Popen([codex, 'app-server', '--listen', endpoint, '-c', 'check_for_update_on_startup=false',
                               '-c', token_setting(token)],  # found by stop even if this runner dies right now
                              cwd=spec['workdir'], env={**os.environ, **settings.get('env', {}), **spec.get('env', {})})
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    try:
        _wait('the Codex app-server', lambda: _ready(f'http://127.0.0.1:{port}/readyz'))
        rpc = Rpc(endpoint)
        # Always with Codex's own checks off: the machine or VM is the sandbox.
        params = {'cwd': spec['workdir'], 'model': spec['model'],
                  'approvalPolicy': 'never', 'sandbox': 'danger-full-access'}
        if spec.get('resume'):
            thread = rpc.call('thread/resume', {'threadId': spec['resume'], **params})['thread']['id']
        else:
            thread = rpc.call('thread/start', params)['thread']['id']
        write_handle(settings, {'name': spec['name'], 'kind': 'headless', 'token': token, 'pid': os.getpid(),
                                'server_pid': server.pid, 'endpoint': endpoint, 'thread': thread, 'codex': codex,
                                'started': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())})
        target = CodexTurns(rpc, thread)
        target.send(f'{spec["prompt"]}\n(ring2 session {token})')
        source = spec.get('source')
        if not source:
            while True:
                signal.pause()
        delivery.follow(lambda after, wait: bus.events(source, after, wait),
                        lambda message: bus.addressed(source['label'], message['message']),
                        target, source['cursor'], log=lambda line: print(line, flush=True))
    finally:
        server.terminate()


def start(settings, name, spec):
    need(spec, f'headless ({name})', 'model', 'workdir', 'prompt')
    state_dir = Path(settings['state_dir'])
    state_dir.mkdir(parents=True, exist_ok=True)
    with open(state_dir / f'{name}.start.lock', 'w') as lock:  # one start per name at a time
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise Refused(f'ring2: {name} is being started by another command') from None
        if running(settings, name):
            raise Refused(f'ring2: {name} is already running')
        stop(settings, name)  # whatever an earlier start that was cut short left behind
        token = uuid.uuid4().hex
        # The handle carries the token before the runner exists, so a start cut at any point can be stopped.
        write_handle(settings, {'name': name, 'kind': 'headless', 'token': token,
                                'thread': (read_handle(settings, name) or {}).get('thread')})
        spec_path = state_dir / f'{name}.spec.json'
        spec_path.write_text(json.dumps({**spec, 'name': name, 'settings': settings}, indent=1))
        log = open(state_dir / f'{name}.log', 'w')  # one log per name, replaced by each start
        runner = subprocess.Popen([sys.executable, '-m', 'ring2.headless', 'run', str(spec_path), token],
                                  env={**os.environ, 'PYTHONPATH': str(ROOT)}, cwd=spec['workdir'],
                                  stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)

        def started():
            if runner.poll() is not None:
                raise SystemExit(f'ring2: the runner for {name} exited; see {log.name}')
            state = read_handle(settings, name)
            return state if state and state['token'] == token and state.get('pid') else None
        return _wait('the headless session', started)


def stop(settings, name):
    """True if a session, or anything left of one, was stopped. The identity is
    the token on the runner's and the app-server's command lines, never the endpoint."""
    state = read_handle(settings, name)
    if not state or state.get('kind') != 'headless':
        return False
    return stop_processes([state['token']])


def restart(settings, name, spec):
    stop(settings, name)
    return start(settings, name, spec)


def main(argv=None):
    if (argv or sys.argv[1:])[:1] == ['run']:
        return run(*(argv or sys.argv[1:])[1:])
    parser = argparse.ArgumentParser(prog='ring2.headless', description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--config', required=True)
    parser.add_argument('command', choices=['start', 'stop', 'restart', 'status'])
    parser.add_argument('name')
    opts = parser.parse_args(argv)
    settings = json.loads(Path(opts.config).read_text())
    spec = settings.pop('sessions', {}).get(opts.name)
    if opts.command in ('start', 'restart'):
        need({'session': spec}, f'headless ({opts.name})', 'session')
        print(json.dumps((start if opts.command == 'start' else restart)(settings, opts.name, spec), indent=1))
    elif opts.command == 'stop':
        print('stopped' if stop(settings, opts.name) else 'not running')
    else:
        print(json.dumps({'running': running(settings, opts.name), 'handle': read_handle(settings, opts.name)}, indent=1))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
