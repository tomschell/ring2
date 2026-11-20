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
| **Adapter** | one per model harness: `providers/codex.py` (Codex; needs `codex`), `providers/claude.py` (Claude Code; needs `claude`). | the core | no |
| **Optional** | `busd` (the bus server, one SQLite file), `watch` (restarts a role whose delivery died while a message waits), `console` (one read-only page) | the core | no |

Adapters live under `ring2/providers/` and load only when a role needs them. Shared start locks,
tokens, handles, spec/log files, runner launch and process cleanup live in `ring2/session.py`.
Each adapter supplies `KIND` (the existing handle kind), `WORD` (the started-line provider word),
`run(spec_path, token)` and `status(settings, name)`. Status supplies `session`, `loop`, optional
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
and ends with the time of that run, so every run is its own line and the newest decides. `--remote` and `--branch` choose the target; `--no-review` skips the PASS.
An error starting "not published" means the branch was not touched; "could not confirm whether
this happened" means the push was sent and its outcome is unknown. Rerunning is safe either way.

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

The app's `post` requires a sender (`--as` or `RING2_ROLE`) and stores `<sender>: <text>`.
A role's own posts remain on the bus but are passed over by its delivery cursor.

While it runs:

```
python3 -m ring2 --config ring2.json status            # per role: session, delivery, task, last activity, waiting: yes|no
python3 -m ring2.watch --config ring2.json &
python3 -m ring2.console --config ring2.json --port 8780 &
```

Instead of a `workdir` per role you can give the fleet one fixed home:

```
{"home": "/path/to/fleet", "origin": "/path/or/url/of/the/repository", ...}
```

`home/repo` is one shared clone, `home/work/<label>` a linked git worktree per role, `home/state` the
state files. They are made if missing and reused by every start; nothing is created per start. In a
home, a won `claim` puts the role's directory on a fresh branch `task/<id>` from the published
branch. It refuses first, changing nothing, while the directory has uncommitted changes or commits
that are on no remote ref. That is the one lost-work check; ring2 has no other. A claim cut before the
branch was made leaves the task held; the next `claim`, or the role's next start, puts the directory
on the branch under the same check, and leaves a task branch it is already on untouched.

Here the evidence is kept as comments on the task. A role keeps its label, folder and task across a
restart (`--as ann restart`); to add a role, add its entry and start it; nothing else is restarted.
Inside a session, `RING2_CONFIG` and `RING2_ROLE` are set, so the session can run the same commands
without flags. To look at a session and leave again: `codex resume <thread> --remote <endpoint>`.

A role with `"provider": "claude"` and a Claude model (for example `"model": "claude-sonnet-4-6"`)
runs Claude Code headless (tested with 2.1.302) using an existing subscription sign-in. Set `claude`
to override the binary or `env` to set `CLAUDE_CONFIG_DIR`; `"resume": true` keeps its conversation
over restarts. The adapter disables permission checks, configured MCP servers, updates and telemetry,
and sets `ENABLE_CLAUDEAI_MCP_SERVERS=false` to keep account connectors from loading,
and ignores user/project settings. It ends the role if startup reports an MCP server or permission
checks. Status JSON includes `busy`. It waits for the CLI's
user-message acknowledgment before advancing delivery. `claude attach` only finds background
jobs, not these stream-driven sessions: stop the role before using `claude --resume <thread>`.

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
