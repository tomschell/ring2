# ring2 Phase 1 report: the vertical slice

Written by the controller's helper, 2026-11-20; saved by the controller, who reran the tests (8 tests, 24 s, OK). Code and tests
are commit fa3ac62e on `controller/ring2-experiment`. Brief: `EXPERIMENT-BRIEF.txt`.

## Reproduce

```
python3 -m unittest tests.ring2.test_slice tests.ring2.test_bus -v
RING2_KEEP=1 python3 -m unittest tests.ring2.test_slice     # keeps the temp root for inspection
python3 -m ring2 start|stop|restart|status|task <role.json>
python3 -m ring2 post <role.json> <channel> "@exp-role text"
```

## What the slice is

- **Start:** one tmux session `ring2-<role>` with three windows:
  - `server`: `codex app-server --listen ws://127.0.0.1:<free port>`
  - `tui`: `codex --remote <endpoint> -C <workdir> --no-alt-screen ... <startup prompt>`
  - `follow`: the bus follower
- **Startup prompt:** the brief, the role, the working directory and the role's task.
- **Identity:** a random token stored in `state/<role>.json` and on the tmux session as `@ring2_token`. Stop kills only the
  session carrying the state file's token. tmux reused session id `$0` after its server restarted, so an id alone is not enough.
- **Delivery:** the follower long-polls `GET /events` and, for each message mentioning `@<role>` or `@all`, runs
  `codex queue --remote <endpoint> --thread <id> --message <text>`. Codex itself holds the message while a turn is active; ring2
  has no turn detection.
- **Cursor:** `state/<role>.cursor` outlives the session, so the successor's follower continues from it.
- **This role's task:** tracker issues with assignee equal to the role label and status `in_progress`
  (`bd list --assignee <role> --status in_progress --json`). Nothing is claimed or written at restart.
- **Tracker:** real `bd` 1.2.1, initialised in the temp root with a throwaway `HOME`. No stand-in was needed.

## What worked (all ran with real Codex 0.163.0, a mock model and a private tmux socket)

| Step | Evidence |
|---|---|
| 1. Start from minimal config | `test_1`: session running with three windows; the mock receives brief, role and workdir; the turn completes. |
| 2. Message during an active turn | `test_2`: the mock holds a turn open and a second addressed message is handed to Codex. For 4 s no model request carries it and the rollout shows 2 turns started, 1 complete. After release, the next request shows the held turn's own reply, then the message. No `turn_aborted`; a message for another role never arrives. |
| 3. Restart with one command | `test_3`: `ring2 restart` gives a new token and thread, all three old pane processes exit, and exactly one session carries the new token. `test_6`: with a stranger's session on the same tmux name, `restart` and `stop` leave it alive and `start` refuses with "duplicate session". |
| 4. Second message to the new session | `test_4`: the request carries the new token and none of the old thread's history; the old rollout does not contain it. |
| 5. Successor finds task and dirty workdir | `test_5`: the startup prompt names our task id and neither unrelated task (one unassigned, one in progress for another role). The successor runs `cat NOTES.txt; git status --short` through Codex's `exec_command`; the output shows the untracked file's content, ` M tracked.txt` and `?? NOTES.txt`. Tracker assignees and statuses are unchanged. |

## What failed or is not covered

Nothing in the five steps failed. These gaps were not exercised:

- **Queued message lost at restart:** a message Codex has queued but not started is lost if the role restarts before the turn
  ends, because the cursor has already advanced. Inferred, not tested.
- **Unconfirmed delivery:** `codex queue` exit 0 is taken as delivered; ring2 does not confirm the turn started.
- **Follower exit:** if `codex queue` fails, the follower exits and nothing restarts it until the next `restart`. The message is
  delivered then, because the cursor did not move.
- **First cursor:** a first-ever follower starts at 0 and delivers every retained message addressed to the role.
- **Message size:** the text goes on the `codex queue` command line with no size check.
- **Scope:** one role only; no fleet start, commissioning, handoffs or status.
- **Not verified:** real sign-in, or a Codex home shared by several roles; permission and sandbox flags (`codex_args` passes
  them through, unexercised); a role posting to the bus or writing files from inside the Codex sandbox; Linux, or any Codex
  version other than 0.163.0; the Claude path. The old test suite was not run.
- **One slip during exploration:** a plain `codex` launch (no `--remote`) started a managed app-server daemon with an updater
  loop under the throwaway `CODEX_HOME`. The updater failed on a too-long socket path and the helper killed the daemon. Network
  activity was not checked. The slice avoids this by launching an explicit app-server.

## New lines (`wc -l`)

| | Lines |
|---|---|
| Code: `session.py` 131, `bus.py` 60, `config.py` 49, `cli.py` 30, `task.py` 12, `__main__.py` 3, `__init__.py` 1 | 286 |
| Tests: `test_slice.py` 273, `mock_model.py` 81, `test_bus.py` 21 | 375 |

## Old `ring/` modules used

- **Python imports:** none, direct or indirect; ring2 uses only the standard library.
- **Bus server, as a separate process:** `bin/ringd` runs `ring.ring.server_main`. ring2 speaks its HTTP protocol
  (`POST /channels/<name>`, `GET /events?channel=&after=&wait=`).
- **Size to count:** the server part is the top of `ring/ring.py` through `RingServer` plus `server_main`, about 530 of 5,573
  lines. Importing `ring.ring` loads no other `ring` module (checked in `sys.modules`).
- **Not used:** `bin/ring-agent post/watch`; ring2 has its own small `post`.

## Old delivery and continuation behaviour needed

None of it.

- **`listener_continuation.py`** (imports `task_work`, uses the launch journal): not needed. Startup context is the brief plus
  one tracker query.
- **`codex_bridge.py` → `interactive.wait_direct_queue` → `queue_delivery.py`:** not needed. Old and new use the same
  mechanism, `codex queue`. From its messages and call site, the old path also confirms the queued message was accepted as a
  turn and refuses to retry uncertain outcomes; `queue_delivery.py` was not read.
- **`model_turn_visibility` / `visibility_retention`:** not needed. ring2 never observes turns; the test reads the rollout only
  as proof.

The mechanism is the same as the old one. What the old modules add around it, and whether it answers failures actually seen,
was not studied; this slice reproduced none.

## Codex quirks taken from the old code

1. `codex queue --remote <endpoint> --thread <id> --message <text>` (`ring/codex_bridge.py`).
2. A separate `codex app-server --listen <endpoint>` with the TUI attached by
   `codex --remote <endpoint> -C <workdir> --no-alt-screen` (`ring/state_checks.py`).
3. `-c check_for_update_on_startup=false` on both (`ring/codex_cli_selection.py`).
4. The mock provider config and the three SSE reply events, with `wire_api="responses"`, websocket features off and the workdir
   trusted (`tests/interactive_smoke.py`).
5. A throwaway `CODEX_HOME` and no `OPENAI_API_KEY` (`tests/interactive_smoke.py`).

Found by running Codex, not from the old code:

- A plain `codex` TUI starts a managed daemon that outlives the tmux session; the explicit app-server avoids it.
- `codex queue` waits for the active turn natively.
- The thread id is the UUID at the end of the rollout file name under `CODEX_HOME/sessions/`.
- Codex sends a side request for a thread title that quotes the prompt; the mock must not treat it as the turn.
- With the mock model no tool list is sent, but a `function_call` named `exec_command` with `{"cmd": ...}` is executed.
  `shell`, `shell_command` and `local_shell` return "unsupported call".
- The slice uses `ws://127.0.0.1:<port>`: with the scratchpad `CODEX_HOME` the managed daemon failed with "path must be shorter
  than SUN_LEN". An explicit `unix://` endpoint was not tried.

## How a Claude role would fit (not built)

Start, stop, restart, the token, the state file, the task query and the working directory are provider-independent.
`session.start` would branch only on the command lines, and a Claude session needs one window instead of an app-server plus TUI.

Delivery differs. No outside command like `codex queue` is known for Claude Code (not checked), so the follower would not push.
The repository's direction is a channel Claude Code reads itself: the `ring-channel` MCP channel, or today's per-session monitor
running `watch`. In ring2 that would be the same follower loop packaged as the channel process the Claude session launches,
keeping the same cursor file.

Whether Claude Code holds channel events until the turn ends, as `codex queue` does, was not checked and needs its own step-2
test.

## Recommendation

Rebuild is viable at this boundary; continue to Phase 2 rather than cut around the old listener.

- The slice needed no old module.
- Identity, restart and delivery-after-turn came to about 200 lines, because tmux and Codex provide the hard parts.
- ring2 imports none of the old delivery stack named in the brief.

The caution: this proves the happy path on one machine with a mock. Before any cutover decision, replay the delivery and restart
failures actually seen in the fleet against ring2. The first three gaps above are where they are expected to land.

Rough estimate for the rest (estimates, not measurements):

| Piece | Code | Tests |
|---|---|---|
| Atomic claim on `bd` (two callers, one winner) | 60-100 | 80 |
| Reviewer PASS naming the commit | 60-100 | 60 |
| Test result recorded for a commit | 80-150 | 80 |
| Publish (fast-forward origin main) | 60-100 | 100 |
| Close (warn on `owes:*`) | 40-60 | 50 |
| Lost-reply and cut-off-restart cases, "could not confirm" errors | 100-200 | 150 |
| Claude role (launch plus channel delivery) | 150-250 | 150 |
| Permission flags, multi-role start/stop, status | 200-400 | 150 |

With the 286 lines already written, that puts the core at roughly 1,000 to 1,650 lines of code plus the 530-line bus server,
inside the 5k aim. A guess of four to six working days for Phase 2 and the Claude role. The larger uncertainty is which old
guards answer real failures, not line count.
