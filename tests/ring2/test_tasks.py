"""Claim, give-back, review PASS, test result, publish and close on a throwaway
bd tracker and a local bare origin. No Codex session is involved.

Run: python3 -m unittest tests.ring2.test_tasks -v
"""
import subprocess
import time
import unittest

from tests.ring2.lab import HAVE_TOOLS, Lab

RACERS = [f'racer-{n}' for n in range(6)]


@unittest.skipUnless(HAVE_TOOLS, 'needs codex, tmux and bd')
class TasksTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        roles = {'impl': ('implementer', 'b'), 'rev': ('reviewer', 'b'),
                 **{label: ('implementer', 'b') for label in RACERS}}
        cls.lab = Lab(roles)
        cls.addClassCleanup(cls.lab.close)
        cls.no_review = cls.lab.write_config('no-review.json', review_required=False)

    def queued(self, title, priority='2'):
        return self.lab.task(title, '--labels', 'queue', '-p', priority)

    def drain_queue(self):
        for issue in self.lab.bd('list', '--label', 'queue', '--status', 'open,in_progress', '--limit', '0'):
            self.lab.bd('close', issue['id'], '--reason', 'test tidy-up', '--force')

    def race(self, argvs):
        """Start every command at once, as separate processes; return (stdout, stderr, code) each."""
        procs = [subprocess.Popen(argv, cwd=self.lab.tracker, env=self.lab.env, text=True,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE) for argv in argvs]
        return [(*proc.communicate(), proc.returncode) for proc in procs]

    def holder(self, task):
        issue = self.lab.issue(task)
        return issue['status'], issue.get('assignee')

    # 1. claim

    def test_1a_bd_claim_is_atomic(self):
        """The primitive itself: six concurrent `bd update --claim` on one issue, one winner."""
        task = self.queued('one for six')
        results = self.race([['bd', 'update', task, '--claim', '--actor', label, '--json'] for label in RACERS])
        winners = [label for label, (_, _, code) in zip(RACERS, results) if code == 0]
        self.assertEqual(len(winners), 1, results)
        self.assertEqual(self.holder(task), ('in_progress', winners[0]))
        for label, (out, err, code) in zip(RACERS, results):
            if code:
                self.assertIn(f'already claimed by {winners[0]}', out + err)
        self.drain_queue()

    def test_1b_queue_order_and_idle_rule(self):
        low = self.queued('low priority, oldest', '3')
        first = self.queued('high priority, older', '1')
        time.sleep(1.1)  # bd stamps created_at to the second; within one second the order falls to the id
        second = self.queued('high priority, newer', '1')
        self.lab.task('open but not queued', '-p', '0')
        held = self.lab.task('queued label but held', '--labels', 'queue', '-p', '0', '--assignee', 'someone')
        self.assertIn(first, self.lab.ring2('claim', label='impl').stdout)
        self.assertEqual(self.holder(first), ('in_progress', 'impl'))
        # A role that holds a task gets that task again, and takes nothing more.
        self.assertIn(first, self.lab.ring2('claim', label='impl').stdout)
        self.assertEqual([self.holder(task) for task in (second, low)], [('open', None)] * 2)
        self.assertIn(second, self.lab.ring2('claim', label='racer-0').stdout)
        self.assertIn(low, self.lab.ring2('claim', label='racer-1').stdout)
        self.assertEqual(self.lab.ring2('claim', label='racer-2').stdout.strip(), 'EMPTY')
        self.assertEqual(self.holder(held), ('open', 'someone'))
        self.drain_queue()

    def test_1c_racing_claims_one_winner_each(self):
        only = self.queued('one task, six idle roles')
        results = self.race([self.lab.ring2_argv('claim', label=label) for label in RACERS])
        self.assertEqual([code for _, _, code in results], [0] * 6, results)
        outs = sorted(out.strip() for out, _, _ in results)
        self.assertEqual(outs.count('EMPTY'), 5, outs)
        winner = next(label for label, (out, _, _) in zip(RACERS, results) if only in out)
        self.assertEqual(self.holder(only), ('in_progress', winner))
        self.drain_queue()

        # Three tasks: three winners with different tasks, three EMPTY; a loser moved on to the next task.
        tasks = [self.queued(f'task {n} of three') for n in range(3)]
        results = self.race([self.lab.ring2_argv('claim', label=label) for label in RACERS])
        won = {label: out.split()[0] for label, (out, _, _) in zip(RACERS, results) if out.strip() != 'EMPTY'}
        self.assertEqual(sorted(won.values()), sorted(tasks), results)
        for label, task in won.items():
            self.assertEqual(self.holder(task), ('in_progress', label))
        self.drain_queue()

    def test_1d_give_back(self):
        task = self.queued('to give back')
        self.lab.ring2('claim', label='impl')
        refused = self.lab.ring2('give-back', task, label='racer-0', check=False)
        self.assertIn('is held by impl', refused.stderr)
        self.assertEqual(self.lab.ring2('give-back', task, label='impl').stdout.strip(), 'given back')
        issue = self.lab.issue(task)
        self.assertEqual((issue['status'], issue.get('assignee'), issue['labels']), ('open', None, ['queue']))
        self.assertEqual(self.lab.ring2('give-back', task, label='impl').stdout.strip(), 'already given back')
        self.assertIn(task, self.lab.ring2('claim', label='racer-0').stdout)  # queued again for anyone
        self.drain_queue()

    # 2-4. review, test result, publish, close

    def test_2_review_test_publish_close(self):
        lab = self.lab
        base = lab.origin_main()
        task_a, task_b = lab.task('code task A'), lab.task('code task B')
        commit_a = lab.commit('impl', 'a.txt', 'a\n')
        commit_b = lab.commit('impl', 'b.txt', 'b\n')
        (lab.work['impl'] / 'DIRTY.txt').write_text('not committed\n')
        lab.run('git', 'fetch', '-q', str(lab.work['impl']), commit_b, cwd=lab.work['rev'])  # the reviewer has the commits

        # Publish needs a passing test result for exactly this commit.
        self.assertIn('no passing test result', lab.ring2('publish', task_b, commit_b, label='impl', check=False).stderr)
        failed = lab.ring2('test', task_b, commit_b, 'test -f missing.txt', label='impl', check=False)
        self.assertEqual((failed.returncode, failed.stdout.strip()), (1, 'TEST FAIL'))
        self.assertIn('no passing test result', lab.ring2('publish', task_b, commit_b, label='impl', check=False).stderr)
        # The clean checkout has the commit's files and none of the working directory's dirt.
        check = 'test -f a.txt && test -f b.txt && test ! -e DIRTY.txt'
        self.assertEqual(lab.ring2('test', task_b, commit_b, check, label='impl').stdout.strip(), 'TEST PASS')
        self.assertEqual(lab.comments(task_b), [('impl', f'TEST FAIL (exit 1) {commit_b} `test -f missing.txt`'),
                                                ('impl', f'TEST PASS {commit_b} `{check}`')])
        self.assertTrue((lab.work['impl'] / 'DIRTY.txt').exists())

        # Publish needs a reviewer's PASS naming exactly this commit.
        no_pass = lab.ring2('publish', task_b, commit_b, label='impl', check=False)
        self.assertIn('no reviewer PASS', no_pass.stderr)
        self.assertIn('is not a reviewer', lab.ring2('review-pass', task_b, commit_b, label='impl', check=False).stderr)
        lab.bd('comment', task_b, f'PASS {commit_b}', '--actor', 'impl')    # not a reviewer's comment
        lab.bd('comment', task_b, f'PASS {commit_a}', '--actor', 'rev')     # another commit
        lab.bd('comment', task_b, f'PASS {commit_b[:12]}', '--actor', 'rev')  # not the full commit
        self.assertIn('no reviewer PASS', lab.ring2('publish', task_b, commit_b, label='impl', check=False).stderr)
        self.assertEqual(lab.origin_main(), base)

        # review_required false skips only the review check (task A, commit A).
        self.assertIn('no passing test result',
                      lab.ring2('publish', task_a, commit_a, label='impl', config=self.no_review, check=False).stderr)
        lab.ring2('test', task_a, commit_a, 'test -f a.txt', label='impl')
        self.assertEqual(lab.ring2('publish', task_a, commit_a, label='impl', config=self.no_review).stdout.strip(),
                         'published')
        self.assertEqual(lab.origin_main(), commit_a)

        # With the PASS, origin main moves to exactly the candidate.
        self.assertEqual(lab.ring2('review-pass', task_b, commit_b, label='rev').stdout.strip(), 'recorded')
        self.assertEqual(lab.ring2('review-pass', task_b, commit_b, label='rev').stdout.strip(), 'already recorded')
        self.assertEqual(lab.comments(task_b).count(('rev', f'PASS {commit_b}')), 1)
        self.assertEqual(lab.ring2('publish', task_b, commit_b, label='impl').stdout.strip(), 'published')
        self.assertEqual(lab.origin_main(), commit_b)
        self.assertEqual(lab.ring2('publish', task_b, commit_b, label='impl').stdout.strip(), 'already published')

        # Only a fast-forward: A is now behind origin main.
        behind = lab.ring2('publish', task_a, commit_a, label='impl', config=self.no_review, check=False)
        self.assertIn('not a fast-forward', behind.stderr)
        self.assertEqual(lab.origin_main(), commit_b)

        # Close: the commit must be on origin main; owes:* labels stop it until --confirm.
        unpublished = lab.commit('impl', 'c.txt', 'c\n')
        self.assertIn('is not on origin main', lab.ring2('close', task_b, unpublished, label='impl', check=False).stderr)
        self.assertIn('name the published commit', lab.ring2('close', task_b, label='impl', check=False).stderr)
        lab.bd('label', 'add', task_b, 'owes:docs')
        owes = lab.ring2('close', task_b, commit_b, label='impl', check=False)
        self.assertNotEqual(owes.returncode, 0)
        self.assertIn('still carries owes:docs', owes.stderr)
        self.assertEqual(lab.issue(task_b)['status'], 'open')
        self.assertEqual(lab.ring2('close', task_b, commit_b, '--confirm', label='impl').stdout.strip(), 'closed')
        self.assertEqual(lab.issue(task_b)['status'], 'closed')
        self.assertEqual(lab.ring2('close', task_b, commit_b, label='impl').stdout.strip(), 'already closed')
        self.assertEqual(lab.ring2('close', task_a, commit_a, label='impl').stdout.strip(), 'closed')  # an ancestor
        note = lab.task('a non-code task')
        self.assertEqual(lab.ring2('close', note, '--reason', 'answered on the bus', label='impl').stdout.strip(), 'closed')
        self.assertEqual(lab.issue(note)['close_reason'], 'answered on the bus')

    # 5a. the effect happened, its reply was lost

    def assert_unconfirmed(self, result):
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('could not confirm whether this happened', result.stderr)
        self.assertNotIn('fail', result.stderr.lower())

    def test_3_lost_reply_is_not_repeated(self):
        lab = self.lab
        self.drain_queue()
        # Claim: the claim lands, the reply is lost; the rerun reports the same task and takes no second one.
        mine, other = self.queued('claimed with a lost reply', '1'), self.queued('must stay queued')
        lab.lose_reply('bd', '--claim')
        self.assert_unconfirmed(lab.ring2('claim', label='impl', check=False))
        self.assertEqual(self.holder(mine), ('in_progress', 'impl'))
        self.assertIn(mine, lab.ring2('claim', label='impl').stdout)
        self.assertEqual(self.holder(other), ('open', None))

        # Comment: one PASS and one test result, however often the command is rerun.
        commit = lab.commit('impl', 'd.txt', 'd\n')
        lab.run('git', 'fetch', '-q', str(lab.work['impl']), commit, cwd=lab.work['rev'])
        lab.lose_reply('bd', 'comment')
        self.assert_unconfirmed(lab.ring2('review-pass', mine, commit, label='rev', check=False))
        self.assertEqual(lab.ring2('review-pass', mine, commit, label='rev').stdout.strip(), 'already recorded')
        lab.lose_reply('bd', 'comment')
        self.assert_unconfirmed(lab.ring2('test', mine, commit, 'test -f d.txt', label='impl', check=False))
        self.assertEqual(lab.ring2('test', mine, commit, 'test -f d.txt', label='impl').stdout.strip(), 'TEST PASS')
        self.assertEqual(lab.comments(mine), [('rev', f'PASS {commit}'), ('impl', f'TEST PASS {commit} `test -f d.txt`')])

        # Publish: origin moved, the reply was lost; the rerun sees it is already there.
        lab.lose_reply('git', 'push')
        self.assert_unconfirmed(lab.ring2('publish', mine, commit, label='impl', check=False))
        self.assertEqual(lab.origin_main(), commit)
        self.assertEqual(lab.ring2('publish', mine, commit, label='impl').stdout.strip(), 'already published')

        # Give-back and close, the same way.
        lab.lose_reply('bd', 'close')
        self.assert_unconfirmed(lab.ring2('close', mine, commit, label='impl', check=False))
        self.assertEqual(lab.ring2('close', mine, commit, label='impl').stdout.strip(), 'already closed')
        self.assertIn(other, lab.ring2('claim', label='impl').stdout)
        lab.lose_reply('bd', 'unclaim')
        self.assert_unconfirmed(lab.ring2('give-back', other, label='impl', check=False))
        self.assertEqual(lab.ring2('give-back', other, label='impl').stdout.strip(), 'already given back')


if __name__ == '__main__':
    unittest.main()
