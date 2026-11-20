"""Review PASS, test result, publish and close: plain comments and plain git."""
import re
import subprocess
import tempfile
from pathlib import Path

from ring2 import config, tracker
from ring2.config import effect


def git(workdir, *args, check=True):
    result = subprocess.run(['git', '-C', workdir, *args], capture_output=True, text=True)
    if check and result.returncode:
        raise SystemExit(f'ring2: git {args[0]}: {result.stderr.strip()}')
    return result.stdout.strip() if check else result


def full_commit(workdir, commit):
    return git(workdir, 'rev-parse', '--verify', f'{commit}^{{commit}}')


def review_pass(cfg, label, task, commit):
    if config.role(cfg, label)['role'] != 'reviewer':
        raise SystemExit(f'ring2: {label} is not a reviewer')
    commit = full_commit(config.role(cfg, label)['workdir'], commit)
    return tracker.comment_once(cfg, task, label, f'PASS {commit}')


def has_pass(cfg, task, commit):
    return any(cfg['roles'].get(c['author'], {}).get('role') == 'reviewer' and commit in c['text']
               and re.search(r'\bPASS\b', c['text']) and not c['text'].startswith('TEST ')
               for c in tracker.comments(cfg, task))


def test(cfg, label, task, commit, command):
    """Run `command` in a clean checkout of `commit`; record the result. True if it passed."""
    workdir = config.role(cfg, label)['workdir']
    commit = full_commit(workdir, commit)
    with tempfile.TemporaryDirectory(prefix='ring2-test-') as tmp:
        checkout = str(Path(tmp) / 'checkout')
        git(workdir, 'worktree', 'add', '--quiet', '--detach', checkout, commit)
        try:
            code = subprocess.run(command, shell=True, cwd=checkout).returncode
        finally:
            git(workdir, 'worktree', 'remove', '--force', checkout)
    result = 'PASS' if code == 0 else f'FAIL (exit {code})'
    tracker.comment_once(cfg, task, label, f'TEST {result} {commit} `{command}`')
    return code == 0


def tested(cfg, task, commit):
    """The newest test result for this commit decides."""
    results = [c['text'] for c in tracker.comments(cfg, task)
               if c['text'].startswith('TEST ') and commit in c['text']]
    return bool(results) and results[-1].startswith('TEST PASS ')


def origin_main(workdir):
    return git(workdir, 'ls-remote', 'origin', 'refs/heads/main').split('\t')[0]


def publish(cfg, label, task, commit):
    """Fast-forward origin main to exactly `commit`. False if it already was."""
    workdir = config.role(cfg, label)['workdir']
    commit = full_commit(workdir, commit)
    if not tested(cfg, task, commit):
        raise SystemExit(f'ring2: not published: {task} has no passing test result for {commit}')
    if cfg['review_required'] and not has_pass(cfg, task, commit):
        raise SystemExit(f'ring2: not published: {task} has no reviewer PASS for {commit}')
    current = origin_main(workdir)
    if current == commit:
        return False
    if current and git(workdir, 'merge-base', '--is-ancestor', current, commit, check=False).returncode:
        raise SystemExit(f'ring2: not published: {commit} is not a fast-forward of origin main {current}')
    effect(f'publish {commit}', ['git', '-C', workdir, 'push', '--quiet', 'origin', f'{commit}:refs/heads/main'], None)
    return True


def close(cfg, label, task, commit=None, reason=None, confirm=False):
    """Close a published task (or a non-code task with a reason). False if already closed."""
    issue = tracker.show(cfg, task)
    if issue['status'] == 'closed':
        return False
    if issue.get('assignee') not in (None, '', label):
        raise SystemExit(f'ring2: not closed: {task} is held by {issue["assignee"]}, not {label}')
    if commit:
        workdir = config.role(cfg, label)['workdir']
        commit = full_commit(workdir, commit)
        git(workdir, 'fetch', '--quiet', 'origin')
        if git(workdir, 'merge-base', '--is-ancestor', commit, 'origin/main', check=False).returncode:
            raise SystemExit(f'ring2: not closed: {commit} is not on origin main')
    elif not reason:
        raise SystemExit('ring2: not closed: name the published commit, or give --reason for a non-code task')
    owes = [name for name in issue.get('labels') or [] if name.startswith('owes:')]
    if owes and not confirm:
        raise SystemExit(f'ring2: not closed: {task} still carries {", ".join(owes)}; '
                         'settle them or rerun with --confirm')
    tracker.close(cfg, label, task, reason or f'published {commit}')
    return True
