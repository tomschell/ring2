"""Tasks: a queue with an atomic claim, on the bd tracker. Needs only bd.

Settings: tracker_dir, queue_label (default "queue").
A queued task is open, unassigned and carries the queue label. A role's task
is the one assigned to its label and in progress. Everything is plain bd
data: assignee, status, labels, prose comments.

  python3 -m ring2.tasks --tracker DIR --as LABEL queue | mine | claim | give-back TASK
        | comment TASK TEXT | label TASK NAME | unlabel TASK NAME
        | close TASK --reason TEXT [--confirm]
"""
import argparse
import json
import subprocess

from ring2 import UNCONFIRMED, Refused, Unconfirmed, effect, need


def _read(settings, *args):
    """What the tracker says. A read that fails or comes back unparsable is an
    error, never an empty answer: "no evidence" must mean the tracker said so."""
    need(settings, 'tasks', 'tracker_dir')
    result = subprocess.run(['bd', *args, '--json'], cwd=settings['tracker_dir'], capture_output=True, text=True)
    try:
        if result.returncode:
            raise ValueError(result.stderr.strip())
        return json.loads(result.stdout)
    except ValueError as exc:
        raise SystemExit(f'ring2 tasks: could not read the tracker (bd {args[0]}): {exc}; nothing was changed') from None


def _write(settings, what, *args):
    need(settings, 'tasks', 'tracker_dir')
    effect(what, ['bd', *args], settings['tracker_dir'])


def show(settings, task):
    return _read(settings, 'show', task)[0]


def mine(settings, label):
    return _read(settings, 'list', '--assignee', label, '--status', 'in_progress')


def queue(settings):
    """Queued tasks nobody holds: priority first, then oldest."""
    issues = _read(settings, 'list', '--label', settings.get('queue_label', 'queue'), '--no-assignee',
                   '--status', 'open', '--limit', '0')
    return sorted(issues, key=lambda issue: (issue['priority'], issue['created_at'], issue['id']))


def claim(settings, task, label):
    """'won' or 'lost'. bd's `update --claim` is the atomic step: it sets assignee
    and status only while the issue is unclaimed and refuses otherwise."""
    need(settings, 'tasks', 'tracker_dir')
    result = subprocess.run(['bd', 'update', task, '--claim', '--actor', label, '--json'],
                            cwd=settings['tracker_dir'], capture_output=True, text=True)
    if result.returncode == 0:
        return 'won'
    if 'already claimed by' in result.stdout + result.stderr:
        return 'lost'
    raise Unconfirmed(f'ring2: claim {task}: {UNCONFIRMED}; rerun the command')


def claim_next(settings, label):
    """('held', task) if the role already holds one, ('won', task) for the first
    queued task it wins, ('empty', None) when it won none."""
    held = mine(settings, label)
    if held:
        return 'held', held[0]
    for issue in queue(settings):
        if claim(settings, issue['id'], label) == 'won':
            return 'won', issue
    return 'empty', None


def give_back(settings, task, label):
    """Clear the assignee and leave the task queued."""
    issue = show(settings, task)
    if issue['status'] == 'open' and not issue.get('assignee'):
        return 'already given back'
    if issue.get('assignee') != label:
        raise Refused(f'ring2: not given back: {task} is held by {issue.get("assignee")}, not {label}')
    _write(settings, f'give back {task}', 'unclaim', task, '--actor', label)
    return 'given back'


def comments(settings, task):
    return [(comment['author'], comment['text']) for comment in _read(settings, 'comments', task)]


def comment(settings, task, author, text):
    """Add the comment unless this author already made exactly this comment."""
    if (author, text) in comments(settings, task):
        return 'already recorded'
    _write(settings, f'comment on {task}', 'comment', task, text, '--actor', author)
    return 'recorded'


def set_label(settings, task, name):
    if name not in (show(settings, task).get('labels') or []):
        _write(settings, f'label {task}', 'label', 'add', task, name)


def clear_label(settings, task, name):
    if name in (show(settings, task).get('labels') or []):
        _write(settings, f'unlabel {task}', 'label', 'remove', task, name)


def close(settings, task, label, reason, confirm=False):
    """Close with a reason. An owes:* label stops the close unless confirmed."""
    issue = show(settings, task)
    if issue['status'] == 'closed':
        return 'already closed'
    if issue.get('assignee') not in (None, '', label):
        raise Refused(f'ring2: not closed: {task} is held by {issue["assignee"]}, not {label}')
    owes = [name for name in issue.get('labels') or [] if name.startswith('owes:')]
    if owes and not confirm:
        raise Refused(f'ring2: not closed: {task} still carries {", ".join(owes)}; '
                      'settle them or rerun with --confirm')
    _write(settings, f'close {task}', 'close', task, '--reason', reason, '--actor', label)
    return 'closed'


def brief(issue):
    return f'{issue["id"]} {issue["title"]}'


def main(argv=None):
    parser = argparse.ArgumentParser(prog='ring2.tasks', description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--tracker')
    parser.add_argument('--queue-label', default='queue')
    parser.add_argument('--as', dest='label')
    parser.add_argument('--reason')
    parser.add_argument('--confirm', action='store_true')
    parser.add_argument('command', choices=['queue', 'mine', 'claim', 'give-back', 'comment', 'label',
                                            'unlabel', 'close'])
    parser.add_argument('args', nargs='*')
    opts = parser.parse_args(argv)
    settings = {'tracker_dir': opts.tracker, 'queue_label': opts.queue_label}
    need(settings, 'tasks', 'tracker_dir')
    if opts.command in ('mine', 'claim', 'give-back', 'comment', 'close'):
        need({'as': opts.label}, 'tasks', 'as')
    print(run(settings, opts.label, opts.command, opts.args, opts.reason, opts.confirm))
    return 0


def run(settings, label, command, args, reason=None, confirm=False):
    """One task command, as text for a person or a session to read."""
    if command == 'queue':
        return '\n'.join(map(brief, queue(settings))) or 'EMPTY'
    if command == 'mine':
        return '\n'.join(map(brief, mine(settings, label))) or 'EMPTY'
    if command == 'claim':
        result, issue = claim_next(settings, label)
        return f'{result.upper()} {brief(issue)}' if issue else 'EMPTY'
    if command == 'give-back':
        return give_back(settings, args[0], label)
    if command == 'comment':
        return comment(settings, args[0], label, args[1])
    if command in ('label', 'unlabel'):
        (set_label if command == 'label' else clear_label)(settings, *args)
        return 'done'
    need({'reason': reason}, 'tasks', 'reason')
    return close(settings, args[0], label, reason, confirm)


if __name__ == '__main__':
    raise SystemExit(main())
