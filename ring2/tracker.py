"""Tasks in the bd tracker: assignee, status, labels and prose comments.

A role's task is the issue assigned to its label and in progress. A queued
task is open, unassigned and carries the queue label.
"""
import json
import subprocess

from ring2.config import UNCONFIRMED, effect


def read(cfg, *args):
    out = subprocess.run(['bd', *args, '--json'], cwd=cfg['tracker_dir'],
                         capture_output=True, text=True, check=True).stdout
    return json.loads(out or '[]')


def mine(cfg, label):
    if not cfg.get('tracker_dir'):
        return []
    return read(cfg, 'list', '--assignee', label, '--status', 'in_progress')


def show(cfg, task):
    return read(cfg, 'show', task)[0]


def queue(cfg):
    """Queued tasks nobody holds: priority first, then oldest."""
    issues = read(cfg, 'list', '--label', cfg['queue_label'], '--no-assignee', '--status', 'open', '--limit', '0')
    return sorted(issues, key=lambda issue: (issue['priority'], issue['created_at'], issue['id']))


def claim(cfg, label):
    """The task this role holds, claiming the first queued one if it holds none.

    `bd update --claim` is the atomic step: bd sets assignee and status only
    if the issue is still unclaimed, and refuses with "already claimed by"
    otherwise. Losing that race moves on to the next queued task.
    """
    held = mine(cfg, label)
    if held:
        return held[0]
    for issue in queue(cfg):
        result = subprocess.run(['bd', 'update', issue['id'], '--claim', '--actor', label, '--json'],
                                cwd=cfg['tracker_dir'], capture_output=True, text=True)
        if result.returncode == 0:
            return issue
        if 'already claimed by' not in result.stdout + result.stderr:
            raise SystemExit(f'ring2: claim {issue["id"]}: {UNCONFIRMED}; rerun the command')
    return None


def give_back(cfg, label, task):
    issue = show(cfg, task)
    if issue['status'] == 'open' and not issue.get('assignee'):
        return False
    if issue.get('assignee') != label:
        raise SystemExit(f'ring2: {task} is held by {issue.get("assignee")}, not {label}')
    effect(f'give back {task}', ['bd', 'unclaim', task, '--actor', label], cfg['tracker_dir'])
    return True


def comments(cfg, task):
    return read(cfg, 'comments', task)


def comment_once(cfg, task, author, text):
    """Add the comment unless this author already made exactly this comment."""
    if any(c['author'] == author and c['text'] == text for c in comments(cfg, task)):
        return False
    effect(f'comment on {task}', ['bd', 'comment', task, text, '--actor', author], cfg['tracker_dir'])
    return True


def close(cfg, label, task, reason):
    effect(f'close {task}', ['bd', 'close', task, '--reason', reason, '--actor', label], cfg['tracker_dir'])
