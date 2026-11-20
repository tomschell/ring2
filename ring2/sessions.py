"""Sessions: start, stop and restart named Codex sessions in tmux. Needs tmux
and the codex CLI.

Settings: tmux_socket, state_dir, codex (binary), env, checks_off (default true).
Sessions run with Codex's own approval prompts and sandbox off
(--dangerously-bypass-approvals-and-sandbox); the machine or VM is the
boundary. ring2 does not manage or answer provider permission prompts.
A session spec is {model, workdir, prompt}. Each session is one tmux session
holding a Codex app-server and the Codex TUI attached to it.

A tmux name is a location, not an identity. Each start makes a random token,
kept in the state file and on the tmux session (@ring2_token); stop kills only
the tmux session carrying the state file's token, so another session that
reuses the name is never the target.

start returns the session's handle: {name, token, tmux_session, endpoint,
thread, rollout, codex}. endpoint + thread is what `ring2.delivery` (or
`codex queue`) needs to reach the session.

  python3 -m ring2.sessions --config FILE start|stop|restart|status NAME
  (FILE: the settings above plus "sessions": {NAME: {model, workdir, prompt}})
"""
import argparse
import json
import os
import shlex
import signal
import socket
import subprocess
import time
import urllib.request
import uuid
from pathlib import Path

from ring2 import Refused, need, read_handle, write_handle


def tmux(settings, *args, check=True):
    need(settings, 'sessions', 'tmux_socket')
    result = subprocess.run(['tmux', '-L', settings['tmux_socket'], *args], capture_output=True, text=True)
    if check and result.returncode:
        raise Refused(f'ring2: tmux {args[0]}: {result.stderr.strip()}')
    return result.stdout.strip()


def handle(settings, name):
    """The handle of the last session started under this name, or None."""
    return read_handle(settings, name)


def _find(settings, token):
    """The tmux session id carrying `token`, or None."""
    listing = tmux(settings, 'list-sessions', '-F', '#{session_id} #{@ring2_token}', check=False)
    for line in listing.splitlines():
        session_id, _, found = line.partition(' ')
        if token and found == token:
            return session_id
    return None


def running(settings, name):
    state = handle(settings, name)
    return bool(state and _find(settings, state['token']))


def _wait(what, check, seconds=60):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = check()
        if value:
            return value
        time.sleep(0.2)
    raise SystemExit(f'ring2: timed out waiting for {what}')


def _ready(url):
    try:
        return urllib.request.urlopen(url, timeout=2).status == 200
    except OSError:
        return False


def _rollout(settings, token, since):
    """The rollout file of the Codex thread whose first prompt carries our token."""
    home = Path(settings.get('env', {}).get('CODEX_HOME') or os.environ.get('CODEX_HOME') or Path.home() / '.codex')
    for path in home.glob('sessions/*/*/*/rollout-*.jsonl'):
        if path.stat().st_mtime >= since and token in path.read_text(errors='replace'):
            return path
    return None


def start(settings, name, spec, token=None):
    need(spec, f'sessions ({name})', 'model', 'workdir', 'prompt')
    if running(settings, name):
        raise Refused(f'ring2: {name} is already running')
    codex = settings.get('codex') or 'codex'
    extra = ['--dangerously-bypass-approvals-and-sandbox'] if settings.get('checks_off', True) else []
    token = token or uuid.uuid4().hex
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        port = probe.getsockname()[1]
    endpoint = f'ws://127.0.0.1:{port}'
    quiet = ['-c', 'check_for_update_on_startup=false']
    env = [arg for key, value in {**settings.get('env', {}), **spec.get('env', {})}.items()
           for arg in ('-e', f'{key}={value}')]
    since = time.time() - 1
    session_id = tmux(
        settings, 'new-session', '-d', '-P', '-F', '#{session_id}', '-s', f'ring2-{name}', '-n', 'server',
        '-x', '160', '-y', '50', '-c', spec['workdir'], *env,
        shlex.join([codex, 'app-server', '--listen', endpoint, *quiet]))
    tmux(settings, 'set-option', '-t', session_id, '@ring2_token', token)
    state = {'name': name, 'token': token, 'tmux_session': session_id, 'endpoint': endpoint, 'codex': codex,
             'started': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}
    write_handle(settings, state)
    _wait('the Codex app-server', lambda: _ready(f'http://127.0.0.1:{port}/readyz'))
    tmux(settings, 'new-window', '-d', '-t', session_id, '-n', 'tui', '-c', spec['workdir'], shlex.join(
        [codex, '--remote', endpoint, '-C', spec['workdir'], '--no-alt-screen', *quiet,
         '--model', spec['model'], *extra, f'{spec["prompt"]}\n(ring2 session {token})']))
    rollout = _wait('the Codex thread', lambda: _rollout(settings, token, since))
    state.update(thread=rollout.stem[-36:], rollout=str(rollout))
    write_handle(settings, state)
    return state


def add_window(settings, state, window, argv, cwd=None):
    """Run a helper beside the session, in its tmux session; it ends when the session does."""
    tmux(settings, 'new-window', '-d', '-t', state['tmux_session'], '-n', window,
         *(['-c', cwd] if cwd else []), shlex.join(argv))


def stop(settings, name):
    """True if a session was stopped, False if none was running."""
    state = handle(settings, name)
    session_id = state and _find(settings, state['token'])
    if not session_id:
        return False
    panes = [int(pid) for pid in tmux(settings, 'list-panes', '-s', '-t', session_id, '-F', '#{pane_pid}').split()]
    tmux(settings, 'kill-session', '-t', session_id)
    _wait('the session to end', lambda: not _find(settings, state['token']))
    # An app-server with a turn in flight outlives its pane until the turn ends; do not leave it working.
    deadline = time.monotonic() + 5
    while any(_alive(pid) for pid in panes) and time.monotonic() < deadline:
        time.sleep(0.2)
    for pid in panes:
        if _alive(pid):
            os.killpg(pid, signal.SIGKILL)  # each pane is its own process group
    return True


def _alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def restart(settings, name, spec):
    """Stop then start. Cut off in between, a rerun finds nothing to stop and starts."""
    stop(settings, name)
    return start(settings, name, spec)


def main(argv=None):
    parser = argparse.ArgumentParser(prog='ring2.sessions', description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--config', required=True)
    parser.add_argument('command', choices=['start', 'stop', 'restart', 'status'])
    parser.add_argument('name')
    opts = parser.parse_args(argv)
    settings = json.loads(Path(opts.config).read_text())
    spec = settings.get('sessions', {}).get(opts.name)
    if opts.command in ('start', 'restart'):
        need({'session': spec}, f'sessions ({opts.name})', 'session')
        state = (start if opts.command == 'start' else restart)(settings, opts.name, spec)
        print(json.dumps(state, indent=1))
    elif opts.command == 'stop':
        print('stopped' if stop(settings, opts.name) else 'not running')
    else:
        print(json.dumps({'running': running(settings, opts.name), 'handle': handle(settings, opts.name)}, indent=1))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
