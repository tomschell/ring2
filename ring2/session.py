"""Start, stop and restart role sessions: one tmux session per role, holding a
Codex app-server, the Codex TUI attached to it, and the bus follower.

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

from ring2 import bus, config, tracker

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


def running(cfg, label):
    state = config.read_state(cfg, label)
    return bool(state and find(cfg, state['token']))


def wait_for(what, check, seconds=60):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = check()
        if value:
            return value
        time.sleep(0.2)
    raise SystemExit(f'ring2: timed out waiting for {what}')


def startup_prompt(cfg, label, token):
    role = config.role(cfg, label)
    lines = [role['brief'], '',
             f'You are {label} ({role["role"]}). Your working directory is {role["workdir"]}.']
    lines += [f'Your task: {issue["id"]} {issue["title"]}' for issue in tracker.mine(cfg, label)] \
        or ['You hold no task.']
    lines.append(f'Ring2 commands: {sys.executable} {ROOT / "ring2"} <command>')
    lines.append(f'(ring2 session {token})')
    return '\n'.join(lines)


def codex_home(cfg):
    return Path(cfg['env'].get('CODEX_HOME') or os.environ.get('CODEX_HOME') or Path.home() / '.codex')


def find_rollout(cfg, token, since):
    """The rollout file of the Codex thread whose first prompt carries our token."""
    for path in codex_home(cfg).glob('sessions/*/*/*/rollout-*.jsonl'):
        if path.stat().st_mtime >= since and token in path.read_text(errors='replace'):
            return path
    return None


def ready(url):
    try:
        return urllib.request.urlopen(url, timeout=2).status == 200
    except OSError:
        return False


def start(cfg, label):
    role = config.role(cfg, label)
    if running(cfg, label):
        raise SystemExit(f'ring2: {label} is already running')
    token = uuid.uuid4().hex
    name = f'ring2-{label}'
    following = bool(cfg.get('ring_url') and cfg['channels'])
    cursor = config.state_path(cfg, label, 'cursor')
    if following and not cursor.exists():  # a new role starts at the end of the bus, not its history
        cursor.parent.mkdir(parents=True, exist_ok=True)
        cursor.write_text(str(bus.end(cfg)))
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        port = probe.getsockname()[1]
    endpoint = f'ws://127.0.0.1:{port}'
    quiet = ['-c', 'check_for_update_on_startup=false']
    env = [arg for key, value in {**cfg['env'], 'RING2_CONFIG': cfg['path'], 'RING2_ROLE': label}.items()
           for arg in ('-e', f'{key}={value}')]
    since = time.time() - 1
    session_id = tmux(
        cfg, 'new-session', '-d', '-P', '-F', '#{session_id}', '-s', name, '-n', 'server',
        '-x', '160', '-y', '50', '-c', role['workdir'], *env,
        shlex.join([cfg['codex'], 'app-server', '--listen', endpoint, *quiet, *cfg['codex_args']]))
    tmux(cfg, 'set-option', '-t', session_id, '@ring2_token', token)
    state = {'role': label, 'token': token, 'tmux_session': session_id, 'name': name,
             'endpoint': endpoint, 'started': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}
    config.write_state(cfg, label, state)
    wait_for('the Codex app-server', lambda: ready(f'http://127.0.0.1:{port}/readyz'))
    tmux(cfg, 'new-window', '-d', '-t', session_id, '-n', 'tui', '-c', role['workdir'], shlex.join(
        [cfg['codex'], '--remote', endpoint, '-C', role['workdir'], '--no-alt-screen', *quiet,
         '--model', role['model'], *cfg['codex_args'], startup_prompt(cfg, label, token)]))
    rollout = wait_for('the Codex thread', lambda: find_rollout(cfg, token, since))
    state.update(thread=rollout.stem[-36:], rollout=str(rollout))
    config.write_state(cfg, label, state)
    if following:
        tmux(cfg, 'new-window', '-d', '-t', session_id, '-n', 'follow', '-c', role['workdir'], shlex.join(
            [sys.executable, str(ROOT / 'ring2'), '--config', cfg['path'], 'follow', label, token]))
    print(f'started {label}: tmux {name} ({session_id}), thread {state["thread"]}')
    return state


def stop(cfg, label):
    state = config.read_state(cfg, label)
    session_id = state and find(cfg, state['token'])
    if not session_id:
        print(f'{label} is not running')
        return False
    tmux(cfg, 'kill-session', '-t', session_id)
    wait_for('the session to end', lambda: not find(cfg, state['token']))
    print(f'stopped {label}: tmux {state["name"]} ({session_id})')
    return True


def restart(cfg, label):
    """Stop then start. Cut off in between, a rerun finds nothing to stop and starts."""
    stop(cfg, label)
    return start(cfg, label)


def fleet_start(cfg):
    """Start every role, then ask each to answer READY on the bus with its session token.

    Only the session just started knows its token, and the request reaches it
    through ordinary delivery. A role that fails to start or does not answer is
    reported unavailable; the others proceed.
    """
    tokens, channel = {}, cfg['channels'][0]
    for label in cfg['roles']:
        try:
            tokens[label] = start(cfg, label)['token']
        except SystemExit as exc:
            print(f'{label}: did not start ({exc})')
    after = bus.end(cfg)
    for label in tokens:
        bus.post(cfg, channel, f'@{label} fleet start: post "READY {label} <your ring2 session token>" '
                               f'on channel {channel} with the ring2 post command.')
    waiting, deadline = dict(tokens), time.monotonic() + cfg['ready_seconds']
    while waiting and time.monotonic() < deadline:
        for message in bus.events(cfg, after, wait=2):
            after = message['sequence']
            for label, token in list(waiting.items()):
                if f'READY {label} {token}' in message['message']:
                    del waiting[label]
    for label in cfg['roles']:
        print(f'{label}: {"READY" if label in tokens and label not in waiting else "unavailable"}')
    return [label for label in cfg['roles'] if label in tokens and label not in waiting]


def fleet_stop(cfg):
    for label in cfg['roles']:
        stop(cfg, label)
