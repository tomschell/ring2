# ring2

ring2 runs AI coding sessions (Codex) and gives them a message bus and a task queue.
It is five small modules; each works alone, and they meet only at plain data.
Sessions run in tmux with a TUI, or headless with no terminal at all.
Tasks live in a `bd` tracker; evidence and publication are plain comments and plain git.
It is an experiment: small, tested against a mock model, not yet run against a real provider.

`ring2` is a working name.

## The one sandbox rule

Sessions always run with the provider's own permission checks off. Run ring2 inside a VM, or on a
machine you are willing to let the sessions use freely. ring2 never manages, answers or works around
a provider's sandbox or permission prompts; the machine boundary is the sandbox.

## Modules

| Module | What it does | Needs installed | Needs running |
|---|---|---|---|
| `ring2.busd` | the bus server: retained one-line messages per channel, over HTTP, in one SQLite file | Python 3 (tested with 3.12) | nothing |
| `ring2.bus` | post a message; watch for messages naming `@label` or `@all` | Python | the bus server |
| `ring2.sessions` | start, stop, restart named Codex sessions in tmux (app-server + TUI) | tmux, codex | nothing |
| `ring2.headless` | the same with no tmux and no TUI; a runner owns the thread and takes one message per turn | codex | nothing (a bus server if it should receive messages) |
| `ring2.delivery` | gets bus messages into a running Codex session without interrupting its turn | codex | the bus server; a Codex app-server with a thread (yours or one `ring2.sessions` started) |
| `ring2.tasks` | queue, atomic claim, give-back, comments, labels, close | bd | embedded bd: nothing. Server-mode bd: the Dolt sql-server the tracker points at |
| `ring2.ship` | test result for a commit, reviewer PASS, fast-forward of a chosen branch | git | nothing; the remote is any git URL or path |
| `ring2` (`app.py`) | the modules assembled, from one JSON config file | all of the above | the bus server |

Only the Python standard library is imported. There is nothing to install: run from a clone.

Both bd modes were tested. In server mode a bd call takes about 0.15 s and a claim about 0.5 s;
embedded, about 0.4 s and 1.3 s. The claim was atomic in both (one winner in every one of 20
six-process races per mode).

## Quick start

All commands run from the clone's top directory.

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

The test runs in a clean checkout of exactly that commit. Evidence is kept as git notes (ref
`ring2`) on the commit. `--remote` and `--branch` choose the target; `--no-review` skips the PASS.
An error starting "not published" means the branch was not touched; "could not confirm whether
this happened" means the push was sent and its outcome is unknown. Rerunning is safe either way.

### Sessions (tmux + codex)

```
cat > sessions.json <<'EOF'
{"tmux_socket": "demo", "state_dir": "/tmp/demo-state",
 "sessions": {"ann": {"model": "<model>", "workdir": "/path/to/repo", "prompt": "Your brief."}}}
EOF
python3 -m ring2.sessions --config sessions.json start ann     # prints the handle: endpoint, thread, token
tmux -L demo attach -t ring2-ann
python3 -m ring2.sessions --config sessions.json restart ann
python3 -m ring2.sessions --config sessions.json stop ann
```

`python3 -m ring2.headless --config sessions.json start ann` does the same without tmux. To look
at a headless session and leave again: `codex resume <thread> --remote <endpoint>`.

### Delivery (attach to a Codex session you already run)

```
python3 -m ring2.delivery --ring-url http://127.0.0.1:8770 --channel team --as ann \
    --endpoint ws://127.0.0.1:<port> --thread <thread id> --cursor /tmp/ann.cursor
```

It hands each message to `codex queue`, which holds it while a turn is active. The cursor passes a
message only once its turn has started, so after a restart a message may arrive twice but is not
lost. One loop per thread: a second one refuses to start.

### The assembled app

```
cat > ring2.json <<'EOF'
{"ring_url": "http://127.0.0.1:8770", "channels": ["team"], "tracker_dir": "/path/to/tracker",
 "roles": {
  "ann": {"role": "implementer", "provider": "codex", "model": "<model>", "brief": "...", "workdir": "/path/to/clone-a"},
  "rae": {"role": "reviewer",    "provider": "codex", "model": "<model>", "brief": "...", "workdir": "/path/to/clone-r"}}}
EOF
python3 -m ring2 --config ring2.json fleet start          # each role answers READY on the bus
python3 -m ring2 --config ring2.json post team "@ann please claim a task"
python3 -m ring2 --config ring2.json --as ann claim
python3 -m ring2 --config ring2.json --as ann test <task> <commit> "make test"
python3 -m ring2 --config ring2.json --as rae review-pass <task> <commit>
python3 -m ring2 --config ring2.json --as ann publish <task> <commit> --close
python3 -m ring2 --config ring2.json fleet stop
```

Here the evidence is kept as comments on the task. Add `"headless": true` to a role to run it
without tmux. Inside a session, `RING2_CONFIG` and `RING2_ROLE` are set, so the session can run the
same commands without flags.

## Tests

```
python3 -m unittest discover -s tests -t .
```

The suite needs `codex`, `tmux`, `bd` and `git`; the server-mode tracker tests also need `dolt`.
It runs the real Codex binary against a local mock model server with a throwaway Codex home, a
private tmux socket, throwaway trackers and a local bare git origin. It makes no provider request
and takes about ten minutes.

## Known gaps

- Never run against a real provider: sign-in, a Codex home shared by several roles, long real turns.
- Only Codex. A Claude session needs its own session and delivery classes.
- Nothing restarts a dead delivery loop or a dead headless runner; `restart` of the role does.
- The bus keeps 64 messages per channel. A session that falls further behind misses the rest.
- A message goes on the `codex queue` command line with no size check beyond the bus's 4096 bytes.
- bd claims carry a five-minute lease. ring2 never renews or reclaims it; behaviour after expiry is untested.
- A later objection by a reviewer does not revoke an earlier PASS for the same commit.
- A role's own bus posts are not filtered out of its delivery.
- In headless mode, a person typing into an attached TUI at the instant a bus message starts a turn can be cut into.
- Tested on macOS with codex-cli 0.163.0, bd 1.2.1 and Dolt 2.3.0.

More detail is in `docs/`.
