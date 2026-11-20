"""ring2 [--config FILE] [--as LABEL] COMMAND ...   the five modules assembled.

Sessions:  start | stop | restart | status          (the --as role)
           fleet start | fleet stop                  (every role)
Tasks:     task | claim | give-back TASK
           review-pass TASK COMMIT | test TASK COMMIT COMMAND
           publish TASK COMMIT [--close] | close TASK [COMMIT] [--reason TEXT] [--confirm]
Bus:       post CHANNEL TEXT

One JSON config file serves every module; each command needs only its own
module's settings (a task command needs tracker_dir and nothing about Codex).
  home, origin     optional: one fixed directory holding a shared clone of `origin`
                   (home/repo), one linked worktree per role at home/work/<label>
                   and the state files. Made if missing, reused by every start.
                   A role with its own `workdir` is left alone.
  roles            {label: {role, provider, model, brief, workdir}}; role is
                   "implementer" or "reviewer"; "resume": true keeps its thread over a restart.
                   An existing "headless" value is accepted and ignored.
  state_dir, codex, env                 sessions (provider checks always off)
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

from ring2 import Refused, bus, delivery, headless, need, read_handle, ship, tasks

ROOT = Path(__file__).resolve().parent.parent


def load(path):
    need({'config': path}, 'app', 'config')
    path = Path(path).resolve()
    cfg = json.loads(path.read_text())
    cfg['path'] = str(path)
    if cfg.get('home'):  # one fixed fleet home: shared clone, a worktree per role, state; reused by every start
        cfg['home'] = str((path.parent / cfg['home']).resolve())
        cfg.setdefault('state_dir', str(Path(cfg['home']) / 'state'))
    cfg.setdefault('state_dir', str(path.parent / 'state'))
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
    return {**cfg, 'repo': workdir(cfg, label), 'reviewers': reviewers}


def workdir(cfg, label):
    """The role's working directory: its own `workdir`, or a stable one under `home` named by the role."""
    entry = role(cfg, label)
    if entry.get('workdir') or not cfg.get('home'):
        need(entry, f'role {label}', 'workdir')
        return str(Path(entry['workdir']).resolve())
    return str(Path(cfg['home']) / 'work' / label)


def task_branch(cfg, label, task):
    """Put the role's `home` directory on the branch of the task it holds. The tracker's assignee and
    the directory's own branch are the whole state, so a setup that was cut short is finished here."""
    settings = ship_settings(cfg, label)
    try:
        ship.task_branch(settings, f'task/{task}')
    except Refused as exc:
        raise Refused(f'{exc}\nring2: {label} holds {task}, but {settings["repo"]} is not on its branch '
                      f'task/{task}; settle the above, then run claim again') from None


def claim(cfg, label):
    """Claim the next task. In a `home` directory a won task starts on a fresh
    branch from the published one, so the directory must hold no unsaved work.
    A held task whose directory is not on its branch yet is put there the same way."""
    managed = cfg.get('home') and not role(cfg, label).get('workdir')
    settings = ship_settings(cfg, label)
    if managed and not tasks.mine(cfg, label) and (lost := ship.unsaved(settings)):
        raise Refused(f'ring2: not claimed: {settings["repo"]} has unsaved work ({"; ".join(lost)}); '
                      f'commit and push it, or discard it yourself')
    answer = tasks.run(cfg, label, 'claim', [])
    if managed and answer != 'EMPTY':  # WON, or HELD: an earlier claim may have been cut before the branch was made
        task_branch(cfg, label, answer.split()[1])
    return answer


def cursor_path(cfg, label):
    return Path(cfg['state_dir']) / f'{label}.cursor'


def start(cfg, label):
    """Start the role's session, tell it its task, and attach delivery from the bus."""
    entry = role(cfg, label)
    need(entry, f'role {label}', 'role', 'provider', 'model', 'brief')
    if entry['provider'] != 'codex':
        raise Refused(f'ring2: provider {entry["provider"]!r} is not built')
    where = workdir(cfg, label)
    if cfg.get('home') and not entry.get('workdir'):
        need(cfg, 'app (home)', 'origin')
        ship.worktree({**cfg, 'repo': str(Path(cfg['home']) / 'repo')}, cfg['origin'], where)
    mine, unsettled = tasks.mine(cfg, label) if cfg.get('tracker_dir') else [], []
    if mine and cfg.get('home') and not entry.get('workdir'):
        try:
            task_branch(cfg, label, mine[0]['id'])
        except Refused as exc:  # the role still starts, and is told
            unsettled = [str(exc)]
            print(exc)
    prompt = '\n'.join([
        entry['brief'], '', f'You are {label} ({entry["role"]}). Your working directory is {where}.',
        *([f'Your task: {tasks.brief(issue)}' for issue in mine] or ['You hold no task.']), *unsettled,
        f'Ring2 commands: {sys.executable} {ROOT / "ring2"} <command>'])
    following = bool(cfg.get('ring_url') and cfg.get('channels'))
    if following:  # fixed before the session exists, so nothing posted from now on is skipped
        delivery.init_cursor(lambda after, wait: bus.events(cfg, after, wait), cursor_path(cfg, label))
    spec = {'model': entry['model'], 'workdir': where, 'prompt': prompt,
            'env': {'RING2_CONFIG': cfg['path'], 'RING2_ROLE': label}}
    previous = read_handle(cfg, label) or {}
    state = headless.start(session_settings(cfg), label, {
        **spec, 'resume': entry.get('resume') and previous.get('thread'),
        'source': following and {'ring_url': cfg['ring_url'], 'channels': cfg['channels'], 'label': label,
                                 'cursor': str(cursor_path(cfg, label))}})
    print(f'started {label}: headless pid {state["pid"]}, thread {state["thread"]}')
    return state


def session_settings(cfg):
    return {key: cfg[key] for key in ('state_dir', 'codex', 'env') if key in cfg}


def stop(cfg, label):
    role(cfg, label)
    print(f'stopped {label}' if headless.stop(cfg, label) else f'{label} is not running')


def status(cfg):
    """One plain dict per role, read through the modules' public functions; no store of its own.

      session    the Codex app-server is running
      loop       the runner delivering bus messages is running
      tasks      ids the tracker says the role holds (absent with no tracker)
      active     when the session's rollout file, else the delivery cursor, last changed (unix time), and
      active_source  which of the two that was
      delivered, addressed   the cursor, and the newest retained bus sequence naming the role (absent with no bus)
    """
    from ring2 import owned_processes, token_setting
    following = bool(cfg.get('ring_url') and cfg.get('channels'))
    retained = bus.events(cfg, 0, wait=0) if following else []
    rows = []
    for label, entry in cfg.get('roles', {}).items():
        state, cursor = read_handle(cfg, label) or {}, cursor_path(cfg, label)
        row = {'session': bool(state.get('token') and owned_processes([token_setting(state['token'])])),
               'loop': headless.running(cfg, label)}
        row = {'label': label, 'role': entry.get('role'), **row}
        if cfg.get('tracker_dir'):
            row['tasks'] = [issue['id'] for issue in tasks.mine(cfg, label)]
        for source, path in (('rollout', state.get('rollout')), ('cursor', cursor)):
            if path and Path(path).exists():
                row.update(active=int(Path(path).stat().st_mtime), active_source=source)
                break
        if following:
            row['delivered'] = int(cursor.read_text()) if cursor.exists() else None
            row['addressed'] = max((message['sequence'] for message in retained
                                    if bus.addressed(label, message['message'])), default=0)
        rows.append(row)
    return rows


def waiting(row):
    """'yes' when a retained bus message naming the role lies beyond its cursor, else 'no'.
    `delivered` and `addressed` are bus sequence numbers, not counts, so the text shows neither."""
    return 'yes' if row.get('delivered') is not None and row.get('addressed', 0) > row['delivered'] else 'no'


def status_text(rows):
    lines = []
    for row in rows:
        age = f'{int(time.time()) - row["active"]}s ago ({row["active_source"]})' if 'active' in row else 'unknown'
        lines.append(f'{row["label"]}: session {"alive" if row["session"] else "dead"}, '
                     f'loop {"alive" if row["loop"] else "dead"}, tasks {" ".join(row.get("tasks", [])) or "-"}, '
                     f'active {age}, waiting: {waiting(row)}')
    return '\n'.join(lines)


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
    parser.add_argument('--json', action='store_true')
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
    elif command == 'status' and (not label or opts.json):
        rows = status(cfg)
        print(json.dumps(rows, indent=1) if opts.json else status_text(rows))
    elif command == 'status':
        role(cfg, label)
        print(json.dumps({'running': headless.running(cfg, label), 'state': read_handle(cfg, label)}, indent=1))
    elif command == 'claim':
        print(claim(cfg, label))
    elif command in ('task', 'give-back'):
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
