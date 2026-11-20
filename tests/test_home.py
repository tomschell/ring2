"""One fixed fleet home: a shared clone, a worktree per role at a stable path,
a fresh branch per task, and nothing that piles up per start or per test run.

Run: python3 -m unittest tests.test_home -v
"""
import os
import signal
import subprocess
import unittest
from pathlib import Path

from tests.lab import HAVE_TOOLS, Lab, wait


@unittest.skipUnless(HAVE_TOOLS, 'needs codex, tmux and bd')
class HomeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.lab = lab = Lab({'impl': ('implementer', 'HOME-BRIEF', {'headless': True}),
                             'rev': ('reviewer', 'HOME-BRIEF', {'headless': True})})
        cls.addClassCleanup(lab.close)
        cls.home = lab.root / 'fleet'
        roles = {label: {key: value for key, value in entry.items() if key != 'workdir'}
                 for label, entry in lab.settings['roles'].items()}
        lab.config = lab.write_config('home.json', home=str(cls.home), origin=str(lab.origin), roles=roles)
        lab.work = {label: cls.home / 'work' / label for label in roles}

    def git(self, *args, where='impl'):
        return self.lab.run('git', *args, cwd=self.lab.work[where])

    def snapshot(self):
        """Every directory under the fleet home (git's object and log stores aside), the
        file count outside the working trees, the worktrees and the branches."""
        skip = ('/.git/objects', '/.git/logs', '/work/')
        paths = [path for path in self.home.rglob('*') if not any(part in f'{path}/' for part in skip)]
        return (sorted(str(path.relative_to(self.home)) for path in paths if path.is_dir()),
                sum(1 for path in paths if path.is_file() and '/refs/' not in str(path)),
                len(self.lab.run('git', 'worktree', 'list', cwd=self.home / 'repo').splitlines()),
                len(self.lab.run('git', 'branch', '--list', cwd=self.home / 'repo').splitlines()))

    def cycle(self, number):
        lab = self.lab
        for label in lab.work:
            lab.ring2('start', label=label)
        task = lab.task(f'cycle {number}', '--labels', 'queue')
        self.assertIn(f'WON {task}', lab.ring2('claim', label='impl').stdout)
        self.assertEqual(self.git('branch', '--show-current'), f'task/{task}')
        self.assertEqual(self.git('rev-parse', 'HEAD'), lab.origin_main())
        commit = lab.commit('impl', f'cycle-{number}.txt', 'work\n')
        failed = lab.ring2('test', task, commit, 'seq 1 300; false', label='impl', check=False)
        self.assertEqual(failed.returncode, 1)
        self.assertIn('\n300\n', failed.stdout)
        self.assertNotIn('\n100\n', failed.stdout, 'only a bounded tail of the failing output is kept')
        self.assertEqual(len((self.home / 'repo/.git/ring2-test/impl.log').read_text().splitlines()), 200)
        lab.ring2('test', task, commit, f'test -f cycle-{number}.txt', label='impl')
        lab.ring2('review-pass', task, commit, label='rev')   # the shared clone already holds the commit
        self.assertEqual(lab.ring2('publish', task, commit, '--close', label='impl').stdout.split(),
                         ['published', 'closed'])
        lab.ring2('restart', label='impl')
        for label in lab.work:
            lab.ring2('stop', label=label)
        return self.snapshot()

    def test_1_three_cycles_leave_what_one_cycle_leaves(self):
        first = self.cycle(1)
        self.cycle(2)
        third = self.cycle(3)
        self.assertEqual(first, third)
        self.assertEqual(first[2:], (3, 2), 'the clone, two role worktrees; main and one task branch')
        self.assertEqual(sorted(path.name for path in (self.home / 'work').iterdir()), ['impl', 'rev'])

    def test_2_a_claim_refuses_while_the_directory_holds_unsaved_work(self):
        lab = self.lab
        task = lab.task('needs a clean directory', '--labels', 'queue')
        before = self.git('rev-parse', 'HEAD'), self.git('branch', '--show-current')
        (lab.work['impl'] / 'scratch.txt').write_text('not committed\n')
        dirty = lab.ring2('claim', label='impl', check=False)
        self.assertIn('not claimed', dirty.stderr)
        self.assertIn('1 uncommitted change(s)', dirty.stderr)
        commit = lab.commit('impl', 'scratch.txt', 'committed, not pushed\n')
        unpushed = lab.ring2('claim', label='impl', check=False)
        self.assertIn(f'1 commit(s) on no remote ref, newest {commit[:12]}', unpushed.stderr)
        self.assertNotIn('Traceback', dirty.stderr + unpushed.stderr)
        # Nothing changed: the task is still queued, the directory is where it was.
        self.assertIsNone(lab.issue(task).get('assignee'))
        self.assertEqual((self.git('rev-parse', 'HEAD~1'), self.git('branch', '--show-current')), before)
        # Saved on a remote ref, the work no longer blocks, and it is not lost.
        self.git('push', '-q', 'origin', 'HEAD:refs/heads/saved')
        self.assertIn(f'WON {task}', lab.ring2('claim', label='impl').stdout)
        self.assertEqual(self.git('branch', '--show-current'), f'task/{task}')
        self.assertEqual(self.git('rev-parse', 'HEAD'), lab.origin_main())
        self.assertEqual(self.git('rev-parse', 'origin/saved'), commit)

    def test_3_a_killed_test_leaves_one_checkout_that_the_next_test_clears(self):
        lab, checkout = self.lab, self.home / 'repo/.git/ring2-test/impl'
        task, commit = lab.task('killed test'), self.git('rev-parse', 'HEAD')
        run = subprocess.Popen(lab.ring2_argv('test', task, commit, 'sleep 30032', label='impl'), cwd=lab.root,
                               env=lab.env, start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        wait('the test command to be running', lambda: lab.run_any('pgrep', '-f', '^sleep 30032'))
        self.assertIn('already has a test running', lab.ring2('test', task, commit, 'true', label='impl', check=False).stderr)
        os.killpg(run.pid, signal.SIGKILL)
        run.wait()
        self.assertTrue(checkout.exists(), 'expected the killed run to leave its checkout')
        self.assertEqual(lab.ring2('test', task, commit, 'true', label='impl').stdout.strip(), 'TEST PASS')
        self.assertFalse(checkout.exists())
        self.assertEqual(self.snapshot()[2], 3)
        self.assertEqual(sorted(path.name for path in checkout.parent.iterdir()), ['impl.lock', 'impl.log'])
        self.assertEqual(lab.comments(task), [('impl', f'TEST PASS {commit} `true`')])


if __name__ == '__main__':
    unittest.main()
