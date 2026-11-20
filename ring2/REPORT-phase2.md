# ring2 report: Phase 2, modular steering, Phase 2b (headless)

Written by the controller's helper, 2026-11-20; saved by the controller. Head 080bdd23 on `controller/ring2-experiment`. The
helper's own final run: 31 tests, all green, about 4.5 minutes, with real Codex 0.163.0, a mock model, private tmux sockets and
throwaway bd trackers.

## Reproduce

```
python3 -m unittest discover -s tests/ring2 -t . -p 'test_*.py'
python3 -m unittest tests.ring2.test_tasks        # claim, review, test, publish, close, lost replies (~2 min, bd is slow)
python3 -m unittest tests.ring2.test_slice        # one tmux role: deliver, restart, delivery gaps
python3 -m unittest tests.ring2.test_fleet        # fleet start/stop with READY
python3 -m unittest tests.ring2.test_standalone   # each module alone
python3 -m unittest tests.ring2.test_headless     # Phase 2b
```

## Phase 2 items: ran vs inferred

| Item | Status | Evidence |
|---|---|---|
| 1. Atomic claim | ran | Six concurrent `bd update --claim` processes on one issue: one exits 0, five get "already claimed by <winner>". Six concurrent `ring2 claim` on one task: one WON, five EMPTY. Six on three tasks: three winners with different tasks, three EMPTY. |
| 1. Queue order, idle rule, give-back | ran | Priority first, then oldest. A role that already holds a task gets HELD and takes nothing more. Give-back clears the assignee, sets open, keeps the `queue` label; another role's give-back is refused. |
| 2. Review PASS | ran | Counts only from a label whose role is `reviewer`, with PASS and the full 40-hex commit. A non-reviewer's PASS, a PASS for another commit and a 12-hex PASS were each rejected. `review_required: false` skips only the review check. |
| 3. Test result | ran | Runs the command in a detached git worktree of the exact commit and records `TEST PASS <commit>` or `TEST FAIL (exit N) <commit>` as a comment. The checkout had the commit's files and not the working directory's dirty file. The newest result for a commit decides. |
| 4. Publish and close | ran | Refused without a passing test, without a PASS, and when not a fast-forward. Otherwise origin main moved to exactly the commit; a rerun says "already published". Close refuses a commit not on the branch, stops on `owes:*` until `--confirm`, and closes a non-code task with `--reason`. |
| 5a. Lost reply | ran | A shim runs the real `bd` or `git`, then reports an error. Claim, comment (PASS and test result), publish, close and give-back each said "could not confirm whether this happened", never "fail", and the rerun did not repeat the effect. `publish --close` with a refused close printed "published, task still open"; the rerun printed "already published" and "already closed". |
| 5b. Restart cut off after stop | ran | `stop`, then `restart`: nothing to stop, starts. A restart cut off part-way through start was not tested. |
| 5c. Dirty files survive restart | ran | Kept from Phase 1, in both the tmux and headless forms. |
| 6i. Message queued but not started at restart | ran, fixed | The cursor now passes a message only once its turn shows in the rollout. A message queued behind a held turn was redelivered to the successor. Duplicates are possible, loss is not. |
| 6ii. Follower exits when `codex queue` fails | ran, fixed | The loop logs, waits two seconds and retries the same message; a later message was not delivered ahead of it. |
| 6iii. First-ever cursor | ran, fixed | Starts at the bus's current end, fixed before the session is created. Retained history was not delivered. |
| 7. Fleet start/stop | ran | Four roles: two answered READY by running the ring2 post command from inside their Codex sessions. One whose tmux name was taken "did not start". One answering with a stale token was "unavailable". The rest proceeded; exit code 1. |

Inferred only: a bd claim carries a five-minute lease. From bd's help, only `bd reclaim` reverts one; ring2 never heartbeats or
reclaims. What happens after a lease expires was not tested.

## The atomic claim

- **Primitive:** `bd update <id> --claim --actor <label>`. bd sets assignee and status in one conditional write and refuses with
  "already claimed by X" otherwise.
- **Loop:** ring2 lists the queue itself (open, unassigned, label `queue`, sorted by priority then `created_at`) and tries each
  task in order. Losing one moves to the next.
- **Why it is atomic:** the list is only a hint; the outcome rests entirely on bd's conditional write, which the six-process
  test exercised directly.
- **Limit:** bd stamps `created_at` to the second, so two tasks created in the same second fall back to id order.

## Modular steering

Five modules plus the assembled app, each with its own entry point (`python3 -m ring2.<module>`) and only its own settings. The
three standalone cases all ran:

- **(a) Delivery attached to a Codex session ring2 did not start:** ran. The test starts an app-server and TUI by hand, then
  runs `python3 -m ring2.delivery` with only bus URL, endpoint and thread. The message was delivered exactly once.
- **One owner per session:** a second delivery loop for the same thread refused with "already has a delivery loop" (advisory
  lock on the thread's rollout file).
- **(b) Queue claim with no fleet, bus or sessions:** ran. Only a bd tracker in a temp directory: WON, HELD, EMPTY, comment,
  label, give-back, close.
- **(c) Publication with no tracker:** ran. Only a git clone and bare origin; evidence kept as git notes (ref `ring2`); target
  branch `trunk`.
- **Missing settings:** each module names what is missing, e.g. `ring2 tasks: missing setting tracker_dir`. A task command
  needs nothing about Codex.

Only proposed: a second input class for Claude (delivery keeps the Codex queue behaviour explicit in `CodexQueue`); sharing
git-notes evidence between clones (notes are local to the clone unless the ref is fetched).

Return values: claim returns `won`, `lost`, `held` or `empty`. `CodexQueue.send` means accepted by Codex; `started()` means the
turn began. Publish returns `published` or `already published`; its errors are either Refused (branch untouched) or Unconfirmed
("could not confirm whether this happened").

| Module | Lines | Installed dependencies | Required services |
|---|---|---|---|
| `bus.py` | 78 | Python stdlib | a Ring server (`bin/ringd`, ~530 lines of old `ring/ring.py`) |
| `sessions.py` | 185 | tmux, codex CLI | none |
| `delivery.py` | 136 | codex CLI | a message source (the entry point uses the bus) and a running Codex app-server |
| `tasks.py` | 164 | bd | none (embedded tracker directory) |
| `ship.py` | 156 | git | none; the git remote is a path or URL |
| `headless.py` | 302 | codex CLI | none |
| `app.py` (assembled) | 206 | all of the above | Ring server |
| `__init__.py`, `__main__.py` | 76 | Python stdlib | none |

- **Totals:** 1,303 lines of code, 1,136 lines of tests.
- **Old `ring/` dependency added:** none. Still no import from `ring/`; the only retained piece is the bus server process.
- **Module coupling:** the `delivery` and `headless` entry points import the bus's public functions as their one known message
  source. `app.py` imports all modules.

## Checks-off rule

- **tmux form:** the TUI is launched with `--dangerously-bypass-approvals-and-sandbox`. One setting, `checks_off`, default
  true; `codex_args` is removed.
- **Exercised with the mock:** yes. Before the flag, a session's `ring2 post` to localhost failed. With the flag and no sandbox
  config it worked (the fleet READY test relies on it).
- **Headless form:** the app-server has no such flag, so the thread is started with `approvalPolicy: never` and
  `sandbox: danger-full-access`. Exercised: the session's command wrote a file in the working directory.
- **Not exercised:** an actual permission prompt.

## Phase 2b: headless Codex

All ran on codex-cli 0.163.0. The first five are in `tests.ring2.test_headless`; the last two were one-off exploration runs with
no automated test.

- **No TUI, no tmux:** `codex app-server` alone takes `thread/start` and `turn/start` over its WebSocket JSON-RPC. The test
  asserts no tmux session exists.
- **Context:** the thread keeps context across turns.
- **Restart:** a fresh thread finds the task and dirty files. With `"resume": true`, `thread/resume` keeps the thread and its
  context.
- **Attach on demand:** worked. `codex resume <thread> --remote <endpoint>` in a tmux pane showed the history and a live turn
  delivered while attached. After killing the pane, the session kept working with no aborted turn.
- **Stop identity:** stop signals a pid only if its command line carries the session token (or the endpoint, for the
  app-server). A stranger process on the recorded pid was spared.
- **`codex queue` headless (exploration only):** it works with no TUI and with no client connected, and waits for the active
  turn.
- **`turn/start` during an active turn (exploration only):** it does not wait; it cut into the held turn, which got no reply. A
  Ring-owned loop must therefore do its own waiting, and it does.

The three delivery gaps with Ring owning the loop:

| Gap | Headless result |
|---|---|
| Queued message lost at restart | Removed. Nothing is queued in Codex; a waiting message stays on the bus and went to the successor exactly once. |
| Unconfirmed delivery | Removed. The cursor moves on the app-server's `turn/start` reply; no rollout reading. A crash between that reply and the cursor write would redeliver; not tested. |
| Follower exiting | Moved. The runner is both session and loop. Killing it left the app-server behind and the role deaf until `restart`, which stopped the leftover and delivered the message posted meanwhile. Nothing restarts a dead runner on its own. |

- **Lines added for 2b:** 302 code (`headless.py`, about 90 of them a WebSocket client), roughly 20 in `app.py`, 182 tests.
- **Simpler than tmux:** no tmux, no rollout scanning to find the thread or confirm delivery, no Codex-side queue, resume is one
  call.
- **Harder than tmux:** a WebSocket client to own; Ring must track active turns itself (a person typing in an attached TUI at
  the same instant could be cut into; not tested); the runner is a single point of failure; nothing to look at unless someone
  attaches.
- **Not verifiable with a mock:** real sign-in, permission prompts, billing or rate-limit differences between TUI and app-server
  turns, long real turns with many tool calls, and the Claude headless path.
- **Claude (not built, not tested):** Claude Code's non-interactive mode with a resumable session id would be a third input
  class. The loop would run one invocation per message and wait for it to exit, which gives the same "Ring owns the loop" shape.
  Whether a person can attach to such a session and leave is the open question.

## Harder than the Phase 1 estimate suggested

- **Stop left the app-server working.** With a turn in flight, the app-server outlives `tmux kill-session`, and SIGTERM, until
  the turn ends. Phase 1 only checked stop while idle. The orphan also held the thread's writer lock, so resume failed. Both
  forms now kill the process group after a five-second grace; tested mid-turn.
- **bd is slow:** about 0.5 s per call, about 2 s per claim. The tasks suite takes two minutes.
- **bd has its own rules:** only the assignee may close or release a claimed issue; ring2 reports these as clean refusals.
- **Size:** the modular split and per-module entry points roughly doubled the code against the single-app form (594 to 956
  lines before headless).

## Gaps to close before a real-provider trial

- **Runner supervision:** nothing restarts a dead headless runner or a dead tmux-form delivery loop.
- **Message size:** text still goes on the `codex queue` command line unchecked.
- **Real-provider checks:** real sign-in, a Codex home shared by several roles, Linux.
- **bd lease expiry:** behaviour after the five-minute lease is unknown.
- **Review revocation:** a later reviewer objection does not revoke an earlier PASS for the same commit (operator's choice).
- **Test-result authorship:** anyone's test-result comment counts.
- **Self-posts:** a role's own bus posts are not filtered out of its delivery.
- **Start cut off part-way:** not tested.
- **Fleet start:** sequential, about three to five seconds per role.
