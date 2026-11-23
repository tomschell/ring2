"""Ship: a test result for a commit, a reviewer PASS, and a fast-forward of a
chosen git branch. Needs only git.

Settings: repo, remote (default origin), branch (default main), reviewers
(labels whose PASS counts), review_required and test_required (default true), test_command (optional),
pass_carries_rebase (default false).

Evidence is prose lines, each with an author:
  TEST PASS <commit> `<command>` (run <time>)   |   TEST FAIL (exit N) <commit> ...
  PASS <commit>
An evidence store has read() -> [(author, text)] and add(author, text). The
default keeps the lines as git notes on the commit (ref `ring2`) in `repo`;
with a tracker the caller passes a store of task comments instead.

publish returns 'published' or 'already published'. Refused means the branch
was not touched; Unconfirmed means the push was sent and its outcome is not
known. Either way a rerun never moves the branch twice.

  python3 -m ring2.ship --repo DIR --as LABEL [--reviewer LABEL] [--no-review] [--no-test]
        test COMMIT COMMAND | pass COMMIT | publish COMMIT | published COMMIT
"""
import argparse
import fcntl
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from ring2 import Refused, Unconfirmed, effect, need


def git(settings, *args, check=True):
    need(settings, 'ship', 'repo')
    result = subprocess.run(['git', '-C', settings['repo'], *args], capture_output=True, text=True)
    if check and result.returncode:
        raise Refused(f'ring2: git {args[0]}: {result.stderr.strip()}')
    return result.stdout.strip() if check else result


def full(settings, commit):
    return git(settings, 'rev-parse', '--verify', f'{commit}^{{commit}}')


def target(settings):
    return settings.get('remote') or 'origin', settings.get('branch') or 'main'


class GitNotes:
    """Evidence kept as git notes on the commit: one `author: text` line each."""

    def __init__(self, settings, commit):
        self.settings, self.commit = settings, full(settings, commit)

    def read(self):
        shown = git(self.settings, 'notes', '--ref', 'ring2', 'show', self.commit, check=False)
        if shown.returncode and 'no note found' not in shown.stderr:
            raise SystemExit(f'ring2 ship: could not read the notes on {self.commit}: {shown.stderr.strip()}')
        return [tuple(line.split(': ', 1)) for line in shown.stdout.splitlines() if ': ' in line]

    def add(self, author, text):
        if (author, text) in self.read():
            return 'already recorded'
        effect(f'note on {self.commit}', ['git', '-C', self.settings['repo'], 'notes', '--ref', 'ring2',
                                          'append', '-m', f'{author}: {text}', self.commit])
        return 'recorded'


def test(settings, commit, command, evidence, author):
    """Run `command` in a clean checkout of `commit` and record the result. True if it passed.

    One checkout and one output file per author, at a fixed place inside the
    repository's git directory, replaced on every run: nothing piles up, and
    whatever a killed run left behind is cleared by the next one.
    """
    configured = settings.get('test_command')
    if configured is not None:
        if command is not None and command != configured:
            raise Refused('ring2: the test command is set in the config')
        command = configured
    need({'command': command}, 'test', 'command')
    commit = full(settings, commit)
    remote, branch = target(settings)
    if git(settings, 'ls-remote', remote, f'refs/heads/{branch}'):
        git(settings, 'fetch', '--quiet', remote, branch)
        head = full(settings, 'FETCH_HEAD')
        if git(settings, 'merge-base', '--is-ancestor', head, commit, check=False).returncode:
            raise Refused(f'ring2: not tested: {commit} does not contain {remote} {branch} {head}; rebase onto it first')
    checkout = Path(git(settings, 'rev-parse', '--path-format=absolute', '--git-common-dir')) / 'ring2-test' / author
    checkout.parent.mkdir(exist_ok=True)
    output = checkout.with_name(f'{author}.log')
    with open(checkout.with_name(f'{author}.lock'), 'w') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise Refused(f'ring2: not tested: {author} already has a test running in this repository') from None
        try:
            shutil.rmtree(checkout, ignore_errors=True)  # ring2's own checkout of a commit: nothing to save
            git(settings, 'worktree', 'prune')
            git(settings, 'worktree', 'add', '--quiet', '--detach', str(checkout), commit)
            with open(output, 'w') as log:
                code = subprocess.run(command, shell=True, cwd=checkout, stdout=log, stderr=subprocess.STDOUT).returncode
        finally:
            shutil.rmtree(checkout, ignore_errors=True)
            git(settings, 'worktree', 'prune')
        # Still under the lock: the output file is this run's, and results are recorded in the order the runs ended.
        tail = output.read_text(errors='replace').splitlines()[-200:]
        output.write_text('\n'.join(tail) + '\n' if tail else '')
        if code:
            print('\n'.join(tail + [f'(the last {len(tail)} lines of output; kept in {output} until the next test)']))
        if git(settings, 'ls-remote', remote, f'refs/heads/{branch}'):
            git(settings, 'fetch', '--quiet', remote, branch)
            head = full(settings, 'FETCH_HEAD')
            if git(settings, 'merge-base', '--is-ancestor', head, commit, check=False).returncode:
                if code:
                    return False
                raise Refused(f'ring2: not tested: {remote} {branch} moved to {head} while the test ran; rebase onto it and test again')
        result = 'PASS' if code == 0 else f'FAIL (exit {code})'
        # Each execution is its own line, so the same outcome after a different one is recorded again.
        line = f'TEST {result} {commit} `{command}` (run {datetime.now(timezone.utc):%Y-%m-%dT%H:%M:%S.%fZ})'
        try:
            evidence.add(author, line)
        except Unconfirmed:  # the reply may be what was lost: adding this same line again never records it twice
            evidence.add(author, line)
    return code == 0


def unsaved(settings):
    """What would be lost if this directory were reset or removed; [] if nothing.
    Saved means committed and reachable from a ref of some remote."""
    found, dirty = [], git(settings, 'status', '--porcelain')
    if dirty:
        found.append(f'{len(dirty.splitlines())} uncommitted change(s)')
    ahead = git(settings, 'rev-list', 'HEAD', '--not', '--remotes').split()
    if ahead:
        found.append(f'{len(ahead)} commit(s) on no remote ref, newest {ahead[0][:12]}')
    return found


def task_branch(settings, name):
    """Put the directory on branch `name`. Already on it: nothing is touched. Otherwise start it at the
    published branch; refuses, changing nothing, while the directory holds unsaved work, and never
    resets a branch that already exists."""
    left = git(settings, 'branch', '--show-current')
    if left == name:
        return name
    remote, branch = target(settings)
    git(settings, 'fetch', '--quiet', remote)
    if lost := unsaved(settings):
        raise Refused(f'ring2: {settings["repo"]} has unsaved work ({"; ".join(lost)}); '
                      f'commit and push it, or discard it yourself')
    git(settings, 'switch', '--quiet', '--no-track', '-c', name, f'{remote}/{branch}')
    if left != name and '/' in name and left.startswith(name.split('/')[0] + '/'):
        # the previous branch of the same family; everything on it was just seen to be on a remote ref
        git(settings, 'branch', '--quiet', '-D', left)
    return name


def worktree(settings, origin, path, label):
    """A working directory at a stable path: one shared clone of `origin` at
    settings['repo'] and a linked worktree at `path`, each made only if missing.
    The worktree commits as `label`, set in its own config, never the shared clone's."""
    remote, branch = target(settings)
    if not Path(settings['repo'], '.git').exists():
        effect(f'clone {origin}', ['git', 'clone', '--quiet', '--origin', remote, origin, settings['repo']])
    if not Path(path, '.git').exists():
        git(settings, 'worktree', 'prune')
        git(settings, 'worktree', 'add', '--quiet', '--detach', str(path), f'{remote}/{branch}')
    git(settings, 'config', 'extensions.worktreeConfig', 'true')
    for key, value in (('user.name', label), ('user.email', f'{label}@ring2.local')):
        subprocess.run(['git', '-C', str(path), 'config', '--worktree', key, value], check=True)
    return str(path)


TEST_LINE = re.compile(r'TEST (PASS|FAIL \(exit -?\d+\)) ([0-9a-f]{40})(?![0-9a-f])')
PASS_LINE = re.compile(r'PASS ([0-9a-f]{40})(?![0-9a-f])')


def tested(commit, evidence, command=None):
    """The newest matching result for this commit decides. With a command set,
    only results naming exactly that command count; otherwise keep the existing grammar."""
    results = []
    for _, text in evidence.read():
        found = TEST_LINE.match(text)
        if found and found.group(2) == commit and (command is None or re.fullmatch(
                re.escape(f' `{command}`') + r'(?: \(run [0-9T:.-]+Z\))?', text[found.end():])):
            results.append(found.group(1))
    return bool(results) and results[-1] == 'PASS'


def review_pass(settings, commit, evidence, author):
    if author not in (settings.get('reviewers') or []):
        raise Refused(f'ring2: not recorded: {author} is not a reviewer')
    return evidence.add(author, f'PASS {full(settings, commit)}')


def has_pass(settings, commit, evidence):
    """A reviewer's line that starts `PASS <commit>`; prose may follow. A PASS
    mentioned anywhere else in a comment is not a PASS, and a later objection
    does not take a PASS back."""
    reviewers = settings.get('reviewers') or []
    return any(author in reviewers and (found := PASS_LINE.match(text)) and found.group(1) == commit
               for author, text in evidence.read())


def patch_id(settings, commit, branch):
    base = git(settings, 'merge-base', branch, commit)
    diff = git(settings, 'diff', '--no-ext-diff', '--no-textconv', '--binary', base, commit)
    result = subprocess.run(['git', 'patch-id', '--stable'], input=diff, capture_output=True, text=True)
    if result.returncode:
        raise Refused(f'ring2: git patch-id: {result.stderr.strip()}')
    return result.stdout.split()[0] if result.stdout.strip() else None


def carried_pass(settings, commit, evidence, branch):
    """Only inspect PASS lines in the caller's evidence store (one task in the app)."""
    candidate = patch_id(settings, commit, branch)
    if candidate:
        for author, text in evidence.read():
            found = PASS_LINE.match(text)
            if author in (settings.get('reviewers') or []) and found:
                previous = found.group(1)
                try:
                    if patch_id(settings, previous, branch) == candidate:
                        return previous
                except Refused:  # Missing old commits cannot supply a PASS.
                    continue
    return None


def publish(settings, commit, evidence):
    """Fast-forward the target branch to exactly `commit`."""
    commit, (remote, branch) = full(settings, commit), target(settings)
    if settings.get('test_required', True) and not tested(commit, evidence, settings.get('test_command')):
        raise Refused(f'ring2: not published: no passing test result for {commit}')
    carried = None
    if settings.get('review_required', True) and not has_pass(settings, commit, evidence):
        if settings.get('pass_carries_rebase', False):
            git(settings, 'fetch', '--quiet', remote, branch)
            carried = carried_pass(settings, commit, evidence, 'FETCH_HEAD')
        if not carried:
            raise Refused(f'ring2: not published: no reviewer PASS for {commit}')
    current = git(settings, 'ls-remote', remote, f'refs/heads/{branch}').split('\t')[0]
    if current == commit:
        return 'already published'
    if current and git(settings, 'merge-base', '--is-ancestor', current, commit, check=False).returncode:
        raise Refused(f'ring2: not published: {commit} is not a fast-forward of {remote} {branch} {current}')
    effect(f'publish {commit}', ['git', '-C', settings['repo'], 'push', '--quiet', remote,
                                 f'{commit}:refs/heads/{branch}'])
    return 'published' + (f'\ncarried PASS from {carried} to {commit}' if carried else '')


def published(settings, commit):
    """Whether `commit` is on the target branch."""
    remote, branch = target(settings)
    git(settings, 'fetch', '--quiet', remote, branch)
    return git(settings, 'merge-base', '--is-ancestor', full(settings, commit), 'FETCH_HEAD',
               check=False).returncode == 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog='ring2.ship', description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--repo')
    parser.add_argument('--remote')
    parser.add_argument('--branch')
    parser.add_argument('--reviewer', action='append')
    parser.add_argument('--no-review', action='store_true')
    parser.add_argument('--pass-carries-rebase', action='store_true')
    parser.add_argument('--no-test', action='store_true')
    parser.add_argument('--as', dest='label')
    parser.add_argument('command', choices=['test', 'pass', 'publish', 'published'])
    parser.add_argument('commit')
    parser.add_argument('test_command', nargs='?')
    opts = parser.parse_args(argv)
    settings = {'repo': opts.repo, 'remote': opts.remote, 'branch': opts.branch,
                'pass_carries_rebase': opts.pass_carries_rebase,
                'reviewers': opts.reviewer, 'review_required': not opts.no_review, 'test_required': not opts.no_test}
    need(settings, 'ship', 'repo')
    evidence = GitNotes(settings, opts.commit)
    if opts.command == 'published':
        print('published' if published(settings, opts.commit) else 'not published')
    elif opts.command == 'publish':
        print(publish(settings, opts.commit, evidence))
    else:
        need({'as': opts.label}, 'ship', 'as')
        if opts.command == 'pass':
            print(review_pass(settings, opts.commit, evidence, opts.label))
        else:
            passed = test(settings, opts.commit, opts.test_command, evidence, opts.label)
            print('TEST PASS' if passed else 'TEST FAIL')
            return 0 if passed else 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
