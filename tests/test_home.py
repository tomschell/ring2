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


@unittest.skipUnless(HAVE_TOOLS, 'needs codex and bd')
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
        self.assertEqual([(author, text.split(' (run ')[0]) for author, text in lab.comments(task)],
                         [('impl', f'TEST PASS {commit} `true`')])

    def cut_claim(self, title):
        """A claim that wins the task and is then cut: the fetch its branch needs fails."""
        lab = self.lab
        for issue in lab.bd('list', '--assignee', 'impl', '--status', 'in_progress'):
            lab.bd('close', issue['id'], '--reason', 'test tidy-up', '--force')
        self.git('push', '-q', '--force', 'origin', 'HEAD:refs/heads/saved')   # what is here is on a remote ref
        task, before = lab.task(title, '--labels', 'queue'), self.git('branch', '--show-current')
        (lab.shims / 'refuse-git').write_text('fetch')
        cut = lab.ring2('claim', label='impl', check=False)
        (lab.shims / 'refuse-git').unlink()
        self.assertIn('git fetch', cut.stderr)
        self.assertIn(f'impl holds {task}, but', cut.stderr)
        self.assertEqual((lab.issue(task).get('assignee'), self.git('branch', '--show-current')), ('impl', before))
        self.assertNotEqual(before, f'task/{task}')
        return task, before

    def test_4_a_held_task_whose_branch_was_never_made_gets_it_on_the_next_claim(self):
        lab = self.lab
        task, before = self.cut_claim('claimed, then cut')
        # Work begun on the old branch is not discarded: a clear refusal, nothing changed.
        begun = lab.work['impl'] / 'begun.txt'
        begun.write_text('begun on the previous branch\n')
        refused = lab.ring2('claim', label='impl', check=False)
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn('1 uncommitted change(s)', refused.stderr)
        self.assertIn(f'impl holds {task}, but', refused.stderr)
        self.assertEqual((self.git('branch', '--show-current'), begun.read_text()),
                         (before, 'begun on the previous branch\n'))
        begun.unlink()
        # With nothing to lose, the retry finishes the setup.
        self.assertIn(f'HELD {task}', lab.ring2('claim', label='impl').stdout)
        self.assertEqual((self.git('branch', '--show-current'), self.git('rev-parse', 'HEAD')),
                         (f'task/{task}', lab.origin_main()))
        # A task branch that is already in use is left exactly as it is.
        commit = lab.commit('impl', 'held.txt', 'work on the held task\n')
        (lab.work['impl'] / 'more.txt').write_text('not committed\n')
        self.assertIn(f'HELD {task}', lab.ring2('claim', label='impl').stdout)
        self.assertEqual((self.git('branch', '--show-current'), self.git('rev-parse', 'HEAD')), (f'task/{task}', commit))
        self.assertTrue((lab.work['impl'] / 'more.txt').exists())
        (lab.work['impl'] / 'more.txt').unlink()

    def test_5_a_role_start_finishes_the_branch_of_the_task_it_holds(self):
        lab = self.lab
        task, before = self.cut_claim('claimed, cut, then the role is started')
        begun = lab.work['impl'] / 'begun.txt'
        begun.write_text('begun on the previous branch\n')
        started = lab.ring2('start', label='impl')   # the role starts all the same, and is told
        self.assertIn(f'impl holds {task}, but', started.stdout)
        wait('the session to be told', lambda: lab.requests_with(f'impl holds {task}, but'))
        self.assertEqual((self.git('branch', '--show-current'), begun.exists()), (before, True))
        begun.unlink()
        lab.ring2('restart', label='impl')
        self.assertEqual((self.git('branch', '--show-current'), self.git('rev-parse', 'HEAD')),
                         (f'task/{task}', lab.origin_main()))
        lab.ring2('stop', label='impl')

    def test_6_each_role_commits_under_its_own_label(self):
        lab = self.lab
        for label in lab.work:
            lab.ring2('start', label=label)
        env = {key: value for key, value in lab.env.items() if not key.startswith('GIT_')}
        for label, workdir in lab.work.items():
            (workdir / f'{label}.txt').write_text('mine\n')
            subprocess.run(['git', 'add', f'{label}.txt'], cwd=workdir, env=env, check=True)
            subprocess.run(['git', 'commit', '-q', '-m', label], cwd=workdir, env=env, check=True)
            self.assertEqual(lab.run('git', 'log', '-1', '--format=%an <%ae> %cn <%ce>', cwd=workdir),
                             f'{label} <{label}@ring2.local> {label} <{label}@ring2.local>')
        shared = subprocess.run(['git', 'config', '--local', '--get-regexp', r'^user\.'], cwd=self.home / 'repo',
                                env=env, capture_output=True, text=True)
        self.assertEqual(shared.stdout, '', 'the shared clone holds no identity')
        for label in lab.work:
            lab.ring2('stop', label=label)


if __name__ == '__main__':
    unittest.main()
