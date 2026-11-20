"""ring2 [--config FILE] [--as LABEL] COMMAND ...   the five modules assembled.

Sessions:  start | stop | restart | status          (the --as role)
           fleet start | fleet stop                  (every role)
Tasks:     task | claim | give-back TASK
           review-pass TASK COMMIT | test TASK COMMIT COMMAND
           publish TASK COMMIT [--close] | close TASK [COMMIT] [--reason TEXT] [--confirm]
Bus:       post CHANNEL TEXT

One JSON config file serves every module; each command needs only its own
module's settings (a task command needs tracker_dir and nothing about Codex).
  roles            {label: {role, provider, model, brief, workdir}}; role is
                   "implementer" or "reviewer"; "headless": true runs the role
                   without tmux or a TUI, "resume": true keeps its thread over a restart
  state_dir, tmux_socket, codex, env, checks_off     sessions
  ring_url, channels, ready_seconds                  bus, delivery, fleet start
  tracker_dir, queue_label                           tasks
  remote, branch, review_required                    ship (repo is the role's workdir)
--config defaults to $RING2_CONFIG and --as to $RING2_ROLE; both are set
inside a session.
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path

from ring2 import Refused, bus, delivery, headless, need, sessions, ship, tasks

ROOT = Path(__file__).resolve().parent.parent


def load(path):
    need({'config': path}, 'app', 'config')
    path = Path(path).resolve()
    cfg = json.loads(path.read_text())
    cfg['path'] = str(path)
    cfg.setdefault('state_dir', str(path.parent / 'state'))
    cfg.setdefault('tmux_socket', 'ring2')
    return cfg


def role(cfg, label):
    need({'as': label}, 'app', 'as')
    if label not in cfg.get('roles', {}):
        raise Refused(f'ring2: no role {label!r} in {cfg["path"]}')
    return cfg['roles'][label]


class TaskEvidence:
    """Ship's evidence kept as comments on the task."""

    def __init__(self, cfg, task):
        self.read = lambda: tasks.comments(cfg, task)
        self.add = lambda author, text: tasks.comment(cfg, task, author, text)


def ship_settings(cfg, label):
    reviewers = [name for name, entry in cfg.get('roles', {}).items() if entry.get('role') == 'reviewer']
    return {**cfg, 'repo': role(cfg, label).get('workdir'), 'reviewers': reviewers}


def cursor_path(cfg, label):
    return Path(cfg['state_dir']) / f'{label}.cursor'


def start(cfg, label):
    """Start the role's session, tell it its task, and attach delivery from the bus."""
    entry = role(cfg, label)
    need(entry, f'role {label}', 'role', 'provider', 'model', 'brief', 'workdir')
    if entry['provider'] != 'codex':
        raise Refused(f'ring2: provider {entry["provider"]!r} is not built')
    workdir = str(Path(entry['workdir']).resolve())
    mine = tasks.mine(cfg, label) if cfg.get('tracker_dir') else []
    prompt = '\n'.join([
        entry['brief'], '', f'You are {label} ({entry["role"]}). Your working directory is {workdir}.',
        *([f'Your task: {tasks.brief(issue)}' for issue in mine] or ['You hold no task.']),
        f'Ring2 commands: {sys.executable} {ROOT / "ring2"} <command>'])
    following = bool(cfg.get('ring_url') and cfg.get('channels'))
    if following:  # fixed before the session exists, so nothing posted from now on is skipped
        delivery.init_cursor(lambda after, wait: bus.events(cfg, after, wait), cursor_path(cfg, label))
    spec = {'model': entry['model'], 'workdir': workdir, 'prompt': prompt,
            'env': {'RING2_CONFIG': cfg['path'], 'RING2_ROLE': label}}
    if entry.get('headless'):  # the runner is session and delivery loop in one
        previous = sessions.handle(cfg, label) or {}
        state = headless.start(session_settings(cfg), label, {
            **spec, 'resume': entry.get('resume') and previous.get('thread'),
            'source': following and {'ring_url': cfg['ring_url'], 'channels': cfg['channels'], 'label': label,
                                     'cursor': str(cursor_path(cfg, label))}})
        print(f'started {label}: headless pid {state["pid"]}, thread {state["thread"]}')
        return state
    state = sessions.start(cfg, label, spec)
    if following:
        sessions.add_window(cfg, state, 'follow', [
            'env', f'PYTHONPATH={ROOT}', sys.executable, '-m', 'ring2.delivery', '--ring-url', cfg['ring_url'],
            *[arg for channel in cfg['channels'] for arg in ('--channel', channel)], '--as', label,
            '--endpoint', state['endpoint'], '--thread', state['thread'], '--rollout', state['rollout'],
            '--codex', state['codex'], '--cursor', str(cursor_path(cfg, label))])
    print(f'started {label}: tmux ring2-{label} ({state["tmux_session"]}), thread {state["thread"]}')
    return state


def session_settings(cfg):
    return {key: cfg[key] for key in ('state_dir', 'codex', 'env', 'checks_off') if key in cfg}


def stop(cfg, label):
    kind = headless if role(cfg, label).get('headless') else sessions
    print(f'stopped {label}' if kind.stop(cfg, label) else f'{label} is not running')


def fleet_start(cfg):
    """Start every role, then ask each to answer READY on the bus with its session token.

    Only the session just started knows its token, and the request reaches it
    through ordinary delivery. A role that fails to start or does not answer is
    reported unavailable; the others proceed. Returns the ready labels.
    """
    need(cfg, 'fleet', 'roles', 'ring_url', 'channels')
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
    waiting, deadline = dict(tokens), time.monotonic() + cfg.get('ready_seconds', 60)
    while waiting and time.monotonic() < deadline:
        for message in bus.events(cfg, after, wait=2):
            after = message['sequence']
            for label, token in list(waiting.items()):
                if f'READY {label} {token}' in message['message']:
                    del waiting[label]
    ready = [label for label in cfg['roles'] if label in tokens and label not in waiting]
    for label in cfg['roles']:
        print(f'{label}: {"READY" if label in ready else "unavailable"}')
    return ready


def close(cfg, label, task, commit=None, reason=None, confirm=False):
    """Close a published task, or a non-code task with a reason."""
    if commit and not ship.published(ship_settings(cfg, label), commit):
        raise Refused(f'ring2: not closed: {commit} is not on the published branch')
    if not commit and not reason:
        raise Refused('ring2: not closed: name the published commit, or give --reason for a non-code task')
    return tasks.close(cfg, task, label, reason or f'published {commit}', confirm)


def main(argv=None):
    parser = argparse.ArgumentParser(prog='ring2', description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--config', default=os.environ.get('RING2_CONFIG'))
    parser.add_argument('--as', dest='label', default=os.environ.get('RING2_ROLE'))
    parser.add_argument('--reason')
    parser.add_argument('--confirm', action='store_true')
    parser.add_argument('--close', action='store_true')
    parser.add_argument('command')
    parser.add_argument('args', nargs='*')
    opts = parser.parse_args(argv)
    cfg, label, command, args = load(opts.config), opts.label, opts.command, opts.args
    if command == 'fleet' and args == ['start']:
        return 0 if len(fleet_start(cfg)) == len(cfg['roles']) else 1
    elif command == 'fleet' and args == ['stop']:
        for name in cfg.get('roles', {}):
            stop(cfg, name)
    elif command == 'post':
        print(json.dumps(bus.post(cfg, *args)))
    elif command == 'start':
        start(cfg, label)
    elif command == 'stop':
        stop(cfg, label)
    elif command == 'restart':
        stop(cfg, label)
        start(cfg, label)
    elif command == 'status':
        kind = headless if role(cfg, label).get('headless') else sessions
        print(json.dumps({'running': kind.running(cfg, label), 'state': sessions.handle(cfg, label)}, indent=1))
    elif command in ('task', 'claim', 'give-back'):
        need({'as': label}, 'app', 'as')
        print(tasks.run(cfg, label, 'mine' if command == 'task' else command, args))
    elif command == 'review-pass':
        task, commit = args
        print(ship.review_pass(ship_settings(cfg, label), commit, TaskEvidence(cfg, task), label))
    elif command == 'test':
        task, commit, test_command = args
        passed = ship.test(ship_settings(cfg, label), commit, test_command, TaskEvidence(cfg, task), label)
        print('TEST PASS' if passed else 'TEST FAIL')
        return 0 if passed else 1
    elif command == 'publish':
        task, commit = args
        print(ship.publish(ship_settings(cfg, label), commit, TaskEvidence(cfg, task)))
        if opts.close:
            try:
                print(close(cfg, label, task, commit, confirm=opts.confirm))
            except SystemExit as exc:
                print(f'published, task still open ({exc})')
                return 1
    elif command == 'close':
        print(close(cfg, label, *args, reason=opts.reason, confirm=opts.confirm))
    else:
        parser.error(f'unknown command {command} {" ".join(args)}')
    return 0
