# ring2

ring2 runs a small fleet of AI coding sessions and gives them a message bus and a task queue.
A role claims a task, commits, has the commit tested and reviewed, and publishes it with plain git.
Sessions run headless, with no terminal. Tasks live in a `bd` tracker.

Status: an experiment with a working name. Codex is supported; a Claude Code adapter exists and has run only against a local stand-in, not yet with a real Claude model or sign-in. In a real-provider trial
(November 2026, Linux, three and four Codex roles) the roles published ten tasks with no hand
repair, two of them changes to ring2 itself.

## The one sandbox rule

Sessions always run with the provider's own permission checks off. Run ring2 inside a VM, or on a
machine you are willing to let the sessions use freely. ring2 never manages, answers or works around
a provider's sandbox or permission prompts; the machine boundary is the sandbox.
ring2 always marks the working folder of a role as trusted for the Codex app-server it starts (a
`-c projects=...` setting on that process; the Codex config file is not edited), so Codex never asks
"Trust this folder?".

## How ring2 is put together

Three kinds of file. The rule between them is what keeps it small.

| Kind | Files | May depend on | Counts against the core budget |
|---|---|---|---|
| **Core** | `bus` (post and read messages), `tasks` (queue, atomic claim, close; needs `bd`), `ship` (test result, reviewer PASS, fast-forward publish; needs `git`), `delivery` (each message into a session once), `session` (shared session lifecycle), `app` (all of it from one config file; `status`) | the core only | yes (3,000 lines) |
| **Adapter** | one per model harness: `providers/codex.py` (Codex; needs `codex`), `providers/claude.py` (Claude Code; needs `claude`), `providers/external.py` (a person or outside session). | the core | no |
| **Optional** | `busd` (the bus server, one SQLite file), `watch` (restarts a role whose delivery died while a message waits), `console` (one read-only page), `channel.py` (Claude Code controller channel server) | the core | no |

Adapters live under `ring2/providers/` and load only when a role needs them. Shared start locks,
tokens, handles, spec/log files, runner launch and process cleanup live in `ring2/session.py`.
Each adapter supplies `status(settings, name)`. Managed adapters also supply `KIND` (the existing
handle kind), `WORD` (the started-line provider word), and `run(spec_path, token)`. Providers without
a runner are left alone by lifecycle commands; they can supply `listen(settings, name)` instead.
Status supplies `session`, `loop`, optional
`busy` (absent when unknown), and optional `active`/`active_source`. Adding an adapter needs no
core edit; removing one leaves other providers usable. The standalone session entry point is now
`python3 -m ring2.providers.codex --config FILE start|stop|restart|status NAME`.

Only the Python standard library is imported. There is nothing to install: run from a clone.

Rules:

- **The core never names a harness.** A role's `provider` field picks its adapter. Nothing specific to one harness
  (its process names, log files, flags, thread ids) appears in a core file.
- **An adapter does five things and nothing else:** start a session in a folder with a first prompt; give it a message
  as a turn and say whether the turn started; say whether it is alive and busy; stop it and everything it started;
  optionally resume a conversation and let a person attach to watch.
- **Anything removable is removed without breaking the rest.** Delete any adapter or optional file and ring2 still
  runs with what is left.
- **Optional files read the core's public commands** (`status`, the channel). The core never imports them.

What goes in:

- A change needs a failure that was actually seen, with a pointer to where. Not "could happen".
- The fix is the smallest thing that works. First check whether git, the tracker or the harness already solves it.
- No new record type, no new state, no new approval step.
- Anything bigger than a few lines in the failing file is an optional file, not core.
- Code that never ran in real use is deleted; git keeps it.

What ring2 deliberately does not do: per-start folders, kept test checkouts, handoff notes, grants, a role identity
that changes on restart, or guidance a role must read before it can work.

## Settings

JSON keys below belong to the app config unless marked as role fields or standalone inputs.
Service options use `module.option`; uppercase names are environment variables. Command arguments
(such as a task, commit, sender or close reason) are not saved settings.

| Setting | Default | Use |
|---|---|---|
| `home` | unset | Stable fleet root containing `repo`, `work/<label>` and `state`; relative to the config file. |
| `origin` | unset | Repository URL/path needed to create managed home worktrees and their shared clone. |
| `state_dir` | `home/state`, otherwise `state` beside the config | Session handles, locks, logs, specs and delivery cursors. |
| `roles` | empty map | Role entries keyed by their stable labels. |
| `role` | required for managed roles | Role field: job type; `reviewer` identifies whose PASS counts. |
| `provider` | required | Role field: adapter filename under `ring2/providers/`, without `.py`. |
| `model` | required for managed roles | Role field (also standalone session input): provider model name. |
| `brief` | shipped file for the role | Role field: first-prompt instructions; required if no shipped brief exists. |
| `workdir` | `home/work/<label>`, otherwise required | Role field (also standalone session input): working directory; outside roles need none. |
| `fresh_conversation` | `false` | Role field: Codex starts a fresh conversation for each message when it holds no task; requires `tracker_dir`. |
| `resume` | false | Role field: keep the previous conversation on restart; standalone sessions take a thread ID instead. |
| `env` | empty map | Extra provider environment; standalone session specs can also override individual values. |
| `codex` | `codex` | Codex adapter executable. |
| `claude` | `claude` | Claude Code adapter executable. |
| `ring_url` | unset | Bus server URL; needed by bus commands, delivery, listen and fleet start. |
| `channels` | unset | Bus channels followed by delivery, listen and fleet start. |
| `ready_seconds` | 60 | Seconds fleet start waits for READY replies. |
| `tracker_dir` | unset | bd tracker directory; needed by task commands. |
| `queue_label` | `queue` | Label selecting unassigned open tasks for claim. |
| `remote` | `origin` | Git publication remote; standalone ship uses `--remote`. |
| `branch` | `main` | Git publication branch; standalone ship uses `--branch`. |
| `test_command` | unset | Fix the test command; a CLI override must match, and publish accepts only results for this command. |
| `test_required` | true | Require a passing test before publish; standalone ship's `--no-test` disables only this check. |
| `review_required` | true | Require a reviewer PASS before publish; standalone ship's `--no-review` disables only this check. |
| `pass_carries_rebase` | false | Accept a PASS from the same task when the whole diff from the published branch's fork point has the same stable git patch ID; standalone ship uses `--pass-carries-rebase`. |
| `repo` | role workdir in the app; required standalone | Standalone ship repository (`--repo`). |
| `reviewers` | app's reviewer roles; empty standalone | Standalone ship labels whose PASS counts (repeat `--reviewer`). |
| `sessions` | empty map | Standalone session entries keyed by name. |
| `prompt` | required standalone | Standalone session's first prompt; the app builds it from the role brief and task. |
| `source` | unset | Optional standalone session bus input mapping; absent means no delivery. |
| `source.label` | required with `source` | Standalone delivery recipient label. |
| `source.cursor` | required with `source` | Standalone delivery cursor file. |
| `busd.host` | `RING_HOST` or `127.0.0.1` | Bus server bind address (`--host`). |
| `busd.port` | `RING_PORT` or 8770 | Bus server port (`--port`). |
| `busd.db` | `RING_DB` or `~/.local/share/ring2/bus.db` | Bus SQLite file (`--db`). |
| `watch.interval` | 5 | Seconds between watch checks (`--interval`). |
| `watch.backoff` | 30 | Initial restart delay in seconds, doubling up to 600 (`--backoff`). |
| `console.port` | 8780 | Loopback console port (`--port`). |
| `RING2_CONFIG` | unset | App's default `--config`; set inside managed sessions. |
| `RING2_ROLE` | unset | App's default `--as`; set inside managed sessions. |
| `RING_HOST` | unset | Overrides the bus server's default bind address. |
| `RING_PORT` | unset | Overrides the bus server's default port. |
| `RING_DB` | unset | Overrides the bus server's default database file. |

## Quick start

All commands run from the clone's top directory. Each part also runs alone.

### Bus

```
python3 -m ring2.busd --port 8770 --db /tmp/bus.db &
python3 -m ring2.bus --url http://127.0.0.1:8770 --channel team post "@ann hello"
python3 -m ring2.bus --url http://127.0.0.1:8770 --channel team watch --as ann
```

A message is one line of at most 4096 bytes. Each channel keeps its newest 64 messages.
Messages are coordination between sessions, never authority to run a command.

### Tasks (only bd)

```
cd /path/to/tracker && bd init --prefix demo        # once; any existing bd tracker works
bd create "first task" --labels queue
python3 -m ring2.tasks --tracker /path/to/tracker queue
python3 -m ring2.tasks --tracker /path/to/tracker --as ann claim       # WON <id> <title> | HELD ... | EMPTY
python3 -m ring2.tasks --tracker /path/to/tracker --as ann give-back <id>
python3 -m ring2.tasks --tracker /path/to/tracker --as ann close <id> --reason "done"
```

A queued task is open, unassigned and labelled `queue`. Order is priority, then oldest. A role that
already holds a task gets that task back instead of a second one. An `owes:*` label stops a close
until you pass `--confirm`.

### Ship (only git)

```
python3 -m ring2.ship --repo /path/to/clone --as ann test <commit> "make test"
python3 -m ring2.ship --repo /path/to/clone --as rae --reviewer rae pass <commit>
python3 -m ring2.ship --repo /path/to/clone --reviewer rae publish <commit>    # fast-forwards origin main
```

The test runs in a clean checkout of exactly that commit: one checkout and one output file per
author inside the repository's git directory, replaced by every run, keeping the last 200 lines of
output. Evidence is kept as git notes (ref `ring2`) on the commit. A PASS is a reviewer's line that
starts `PASS <full commit>`; a test result is a line that starts `TEST PASS <full commit>`
and ends with the time of that run, so every run is its own line and the newest matching result decides.
With a configured test command, the app also accepts `test <task> <commit>` without a command argument.
An error starting "not published" means the branch was not touched; "could not confirm whether
this happened" means the push was sent and its outcome is unknown. Rerunning is safe either way.
With `pass_carries_rebase`, the reviewer has not seen the change on its new base; only the rerun of the tests covers how it combines with what landed.

### The assembled app

```
cat > ring2.json <<'EOF'
{"ring_url": "http://127.0.0.1:8770", "channels": ["team"], "tracker_dir": "/path/to/tracker",
 "roles": {
  "ann": {"role": "implementer", "provider": "codex", "model": "<model>", "brief": "...", "workdir": "/path/to/clone-a"},
  "rae": {"role": "reviewer",    "provider": "codex", "model": "<model>", "brief": "...", "workdir": "/path/to/clone-r"}}}
EOF
python3 -m ring2 --config ring2.json fleet start          # each role answers READY on the bus
python3 -m ring2 --config ring2.json --as controller post team "@ann please claim a task"
python3 -m ring2 --config ring2.json --as ann claim
python3 -m ring2 --config ring2.json --as ann test <task> <commit> "make test"
python3 -m ring2 --config ring2.json --as rae review-pass <task> <commit>
python3 -m ring2 --config ring2.json --as ann publish <task> <commit> --close
python3 -m ring2 --config ring2.json fleet stop
```

With `fresh_conversation`, follow-ups also land in a new conversation. Every request to such a role
must name the task, the full commit and the question. This setting is refused for other managed providers.

Project rules belong in the project's own `AGENTS.md`. Both harnesses read it when run headless
(seen once each, 2026-11-20). Do not add a `CLAUDE.md` that imports it: with one present,
a Claude role missed a rule on its first turn.

The app's `post` requires a sender (`--as` or `RING2_ROLE`) and stores `<sender>: <text>`.
A role's own posts remain on the bus but are passed over by its delivery cursor.

While it runs:

```
python3 -m ring2 --config ring2.json status            # per role: session, delivery, task, last activity, waiting: yes|no
python3 -m ring2.watch --config ring2.json &
python3 -m ring2.console --config ring2.json --port 8780 &
```

In a fleet home, a won `claim` puts the role's directory on a fresh branch `task/<id>` from the published
branch. It refuses first, changing nothing, while the directory has uncommitted changes or commits
that are on no remote ref. That is the one lost-work check; ring2 has no other. A claim cut before the
branch was made leaves the task held; the next `claim`, or the role's next start, puts the directory
on the branch under the same check, and leaves a task branch it is already on untouched.

Here the evidence is kept as comments on the task. A role keeps its label, folder and task across a
restart (`--as ann restart`); to add a role, add its entry and start it; nothing else is restarted.
To look at a session and leave again: `codex resume <thread> --remote <endpoint>`.

A role with `"provider": "claude"` and a Claude model (for example `"model": "claude-sonnet-4-6"`)
runs Claude Code headless (tested with 2.1.302) using an existing subscription sign-in. The adapter
disables permission checks, configured MCP servers, updates and telemetry,
and sets `ENABLE_CLAUDEAI_MCP_SERVERS=false` to keep account connectors from loading,
and ignores user/project settings. It ends the role if startup reports an MCP server or permission
checks. Status JSON includes `busy`. It waits for the CLI's
user-message acknowledgment before advancing delivery. `claude attach` only finds background
jobs, not these stream-driven sessions: stop the role before using `claude --resume <thread>`.

For a person or an outside session, use `"provider": "external"`. Start its listener with
`ring2 --as <label> listen` (and `--config` if needed). It prints one line per addressed message and keeps the role's normal delivery cursor,
including messages waiting before the first listen. Status and the console show whether it is
listening and whether a message waits. Stop the listener itself to leave; start, stop, restart,
fleet and watch leave it alone.

## Tests

```
python3 -m unittest discover -s tests -t .
```

The suite needs `codex`, `bd` and `git`; Claude adapter tests also need `claude`, and server-mode
tracker tests need `dolt`. Only the test that attaches a TUI needs `tmux`; it skips when tmux is
missing. The suite runs real provider CLIs against local stand-ins with throwaway homes and
trackers and a local bare git origin. The attach test uses a private tmux socket. It makes no
provider request and takes several minutes.

## Known gaps

Seen in the real-provider trial and left as they are:

- **Typing into a working role can drop a channel message.** With a viewer attached, input a person submits while a
  channel-started turn is running is added by Codex to that same turn; the model answered only the typed input and
  never acted on the channel message (3 of 3 tries), and ring2 does not send it again. Watching without typing, and
  typing while the role is idle, were unaffected.
- `test` and `publish` do not check that the caller holds the task. `close` refuses only when a different label holds it, so a task nobody holds can be closed by any role.
- `status` stops with the tracker's error when the tracker cannot be read, so it shows nothing about sessions then.
- One label running two claims at the same instant can end up holding two tasks. Two different labels never got the
  same task (0 of 60).
- A role is shown a task's title only. It reads the description with `bd show <task>`.

By design, or not built:

- With a real sign-in it is not yet verified that account connectors stay out; the start-up check ends the role if an MCP server loads.
- Claude stream-driven sessions cannot be watched with `claude attach`; stop before interactive resume.
- Claude delivery waits for the current turn; a stalled turn delays later messages.
- `status` cannot tell a stopped role from a dead one, so `watch` restarts either once a message waits for it. Stop
  watch before stopping a role you want to stay down. Nothing restarts watch itself.
- `status` cannot see a session that is alive but stuck.
- Stopping a role does not release its task; `give-back` does. Removing a role leaves its folder.
- A role that gives a task back with unpushed commits cannot claim again until it pushes or discards them.
- The bus keeps 64 messages per channel. A session that falls further behind misses the rest.
- bd claims carry a five-minute lease. ring2 never renews or reclaims it; behaviour after expiry is untested.
- A later objection by a reviewer does not revoke an earlier PASS for the same commit.
- Codex's own session files grow with every thread; ring2 does not prune them.

Tested with codex-cli 0.163.0: the suite on macOS (bd 1.2.1, Dolt 2.3.0) and Linux (bd 1.3.2); the trial on Linux.
Earlier design notes are in `docs/`.

## The controller

Put the controller on the roster as a role with `"provider": "external"`; ring2 leaves its session
alone. Receive messages in a terminal with `python3 -m ring2 --config /path/to/ring2.json --as controller listen`,
or add this entry to the Claude Code session's `.mcp.json` (run with this repo on Python's import path):

```json
{"mcpServers": {"ring2": {"command": "python3", "args": ["-m", "ring2.channel", "--config", "/path/to/ring2.json", "--as", "controller"]}}}
```

Launch Claude Code with `--dangerously-load-development-channels server:ring2`. The server receives
addressed messages and shares the listener's cursor and lock, so run only one receiver per label.
It offers no tools; reply with the ordinary `python3 -m ring2 --config /path/to/ring2.json --as controller post <channel> "<text>"`.

Queue a task by running `bd create "<title>" --description "<text>" --labels <queue label>` in the
tracker directory, then send one post to the coordinator. To take published work elsewhere, run
`git fetch` from the fleet's repository in your destination checkout, run the configured test command,
then `git push` to your destination remote.
