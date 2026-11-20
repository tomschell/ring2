"""Start, stop and restart one role session: a tmux session holding a Codex
app-server, the Codex TUI attached to it, and the bus follower.

A session is identified by a random token stored both in the role's state file
and on the tmux session (@ring2_token). Stop only ever kills the tmux session
carrying the token in the state file, so another session that reuses the name
is never the target.
"""
import os
import shlex
import socket
import subprocess
import sys
import time
import urllib.request
import uuid
from pathlib import Path

from ring2 import config, task

ROOT = Path(__file__).resolve().parent.parent


def tmux(cfg, *args, check=True):
    result = subprocess.run(['tmux', '-L', cfg['tmux_socket'], *args], capture_output=True, text=True)
    if check and result.returncode:
        raise SystemExit(f'ring2: tmux {args[0]} failed: {result.stderr.strip()}')
    return result.stdout.strip()


def find(cfg, token):
    """The tmux session id carrying `token`, or None."""
    listing = tmux(cfg, 'list-sessions', '-F', '#{session_id} #{@ring2_token}', check=False)
    for line in listing.splitlines():
        session_id, _, found = line.partition(' ')
        if token and found == token:
            return session_id
    return None


def running(cfg):
    state = config.read_state(cfg)
    return bool(state and find(cfg, state['token']))


def wait_for(what, check, seconds=60):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = check()
        if value:
            return value
        time.sleep(0.2)
    raise SystemExit(f'ring2: timed out waiting for {what}')


def startup_prompt(cfg, token):
    lines = [cfg['brief'], '', f'You are role {cfg["role"]}. Your working directory is {cfg["workdir"]}.']
    tasks = task.mine(cfg)
    lines += [f'Your task: {item["id"]} {item["title"]}' for item in tasks] or ['You hold no task.']
    lines.append(f'(ring2 session {token})')
    return '\n'.join(lines)


def codex_home(cfg):
    return Path(cfg['env'].get('CODEX_HOME') or os.environ.get('CODEX_HOME') or Path.home() / '.codex')


def find_thread(cfg, token, since):
    """The Codex thread whose first prompt carries our token (the TUI creates it)."""
    for path in codex_home(cfg).glob('sessions/*/*/*/rollout-*.jsonl'):
        if path.stat().st_mtime >= since and token in path.read_text(errors='replace'):
            return path.stem[-36:]
    return None


def ready(url):
    try:
        return urllib.request.urlopen(url, timeout=2).status == 200
    except OSError:
        return False


def start(cfg):
    if running(cfg):
        raise SystemExit(f'ring2: {cfg["role"]} is already running')
    token = uuid.uuid4().hex
    name = f'ring2-{cfg["role"]}'
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        port = probe.getsockname()[1]
    endpoint = f'ws://127.0.0.1:{port}'
    quiet = ['-c', 'check_for_update_on_startup=false']
    env = [arg for key, value in {**cfg['env'], 'RING2_CONFIG': cfg['path']}.items()
           for arg in ('-e', f'{key}={value}')]
    since = time.time() - 1
    session_id = tmux(
        cfg, 'new-session', '-d', '-P', '-F', '#{session_id}', '-s', name, '-n', 'server',
        '-x', '160', '-y', '50', '-c', cfg['workdir'], *env,
        shlex.join([cfg['codex'], 'app-server', '--listen', endpoint, *quiet, *cfg['codex_args']]))
    tmux(cfg, 'set-option', '-t', session_id, '@ring2_token', token)
    state = {'role': cfg['role'], 'token': token, 'tmux_session': session_id, 'name': name,
             'endpoint': endpoint, 'started': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}
    config.write_state(cfg, state)
    wait_for('the Codex app-server', lambda: ready(f'http://127.0.0.1:{port}/readyz'))
    tmux(cfg, 'new-window', '-d', '-t', session_id, '-n', 'tui', '-c', cfg['workdir'], shlex.join(
        [cfg['codex'], '--remote', endpoint, '-C', cfg['workdir'], '--no-alt-screen', *quiet,
         '--model', cfg['model'], *cfg['codex_args'], startup_prompt(cfg, token)]))
    state['thread'] = wait_for('the Codex thread', lambda: find_thread(cfg, token, since))
    config.write_state(cfg, state)
    if cfg.get('ring_url') and cfg['channels']:
        tmux(cfg, 'new-window', '-d', '-t', session_id, '-n', 'follow', '-c', cfg['workdir'], shlex.join(
            ['env', f'PYTHONPATH={ROOT}', sys.executable, '-m', 'ring2', 'follow', cfg['path'], token]))
    print(f'started {cfg["role"]}: tmux {name} ({session_id}), thread {state["thread"]}')
    return state


def stop(cfg):
    state = config.read_state(cfg)
    session_id = state and find(cfg, state['token'])
    if not session_id:
        print(f'{cfg["role"]} is not running')
        return False
    tmux(cfg, 'kill-session', '-t', session_id)
    wait_for('the session to end', lambda: not find(cfg, state['token']))
    print(f'stopped {cfg["role"]}: tmux {state["name"]} ({session_id})')
    return True


def restart(cfg):
    stop(cfg)
    return start(cfg)
