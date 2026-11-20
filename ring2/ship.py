"""Ship: a test result for a commit, a reviewer PASS, and a fast-forward of a
chosen git branch. Needs only git.

Settings: repo, remote (default origin), branch (default main), reviewers
(labels whose PASS counts), review_required (default true).

Evidence is prose lines, each with an author:
  TEST PASS <commit> `<command>`   |   TEST FAIL (exit N) <commit> `<command>`
  PASS <commit>
An evidence store has read() -> [(author, text)] and add(author, text). The
default keeps the lines as git notes on the commit (ref `ring2`) in `repo`;
with a tracker the caller passes a store of task comments instead.

publish returns 'published' or 'already published'. Refused means the branch
was not touched; Unconfirmed means the push was sent and its outcome is not
known. Either way a rerun never moves the branch twice.

  python3 -m ring2.ship --repo DIR --as LABEL [--reviewer LABEL] [--no-review]
        test COMMIT COMMAND | pass COMMIT | publish COMMIT | published COMMIT
"""
import argparse
import re
import subprocess
import tempfile
from pathlib import Path

from ring2 import Refused, effect, need


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
        return [tuple(line.split(': ', 1)) for line in shown.stdout.splitlines() if ': ' in line]

    def add(self, author, text):
        if (author, text) in self.read():
            return 'already recorded'
        effect(f'note on {self.commit}', ['git', '-C', self.settings['repo'], 'notes', '--ref', 'ring2',
                                          'append', '-m', f'{author}: {text}', self.commit])
        return 'recorded'


def test(settings, commit, command, evidence, author):
    """Run `command` in a clean checkout of `commit` and record the result. True if it passed."""
    commit = full(settings, commit)
    with tempfile.TemporaryDirectory(prefix='ring2-test-') as tmp:
        checkout = str(Path(tmp) / 'checkout')
        git(settings, 'worktree', 'add', '--quiet', '--detach', checkout, commit)
        try:
            code = subprocess.run(command, shell=True, cwd=checkout).returncode
        finally:
            git(settings, 'worktree', 'remove', '--force', checkout)
    result = 'PASS' if code == 0 else f'FAIL (exit {code})'
    evidence.add(author, f'TEST {result} {commit} `{command}`')
    return code == 0


def tested(commit, evidence):
    """The newest test result for this commit decides."""
    results = [text for _, text in evidence.read() if text.startswith('TEST ') and commit in text]
    return bool(results) and results[-1].startswith('TEST PASS ')


def review_pass(settings, commit, evidence, author):
    if author not in (settings.get('reviewers') or []):
        raise Refused(f'ring2: not recorded: {author} is not a reviewer')
    return evidence.add(author, f'PASS {full(settings, commit)}')


def has_pass(settings, commit, evidence):
    """A reviewer's line with PASS and the full commit (a test result is not a review)."""
    return any(author in (settings.get('reviewers') or []) and commit in text
               and re.search(r'\bPASS\b', text) and not text.startswith('TEST ')
               for author, text in evidence.read())


def publish(settings, commit, evidence):
    """Fast-forward the target branch to exactly `commit`."""
    commit, (remote, branch) = full(settings, commit), target(settings)
    if not tested(commit, evidence):
        raise Refused(f'ring2: not published: no passing test result for {commit}')
    if settings.get('review_required', True) and not has_pass(settings, commit, evidence):
        raise Refused(f'ring2: not published: no reviewer PASS for {commit}')
    current = git(settings, 'ls-remote', remote, f'refs/heads/{branch}').split('\t')[0]
    if current == commit:
        return 'already published'
    if current and git(settings, 'merge-base', '--is-ancestor', current, commit, check=False).returncode:
        raise Refused(f'ring2: not published: {commit} is not a fast-forward of {remote} {branch} {current}')
    effect(f'publish {commit}', ['git', '-C', settings['repo'], 'push', '--quiet', remote,
                                 f'{commit}:refs/heads/{branch}'])
    return 'published'


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
    parser.add_argument('--as', dest='label')
    parser.add_argument('command', choices=['test', 'pass', 'publish', 'published'])
    parser.add_argument('commit')
    parser.add_argument('test_command', nargs='?')
    opts = parser.parse_args(argv)
    settings = {'repo': opts.repo, 'remote': opts.remote, 'branch': opts.branch,
                'reviewers': opts.reviewer, 'review_required': not opts.no_review}
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
