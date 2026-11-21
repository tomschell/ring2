# ring2

ring2 runs a small team of coding agents on one repository. You file tasks; the agents claim
them, make the change, test it, review each other's work and publish it to a git branch.

A fleet has four kinds of role:

- **Implementers** claim a task, change the code and run the tests.
- **A reviewer** reads each change and passes it or sends it back.
- **A coordinator** tells free implementers when work is queued.
- **A controller** is the session you work in. It sits outside the fleet.

Every task takes the same path: claim, commit, test, review, publish, close. ring2 records the
test result and the review against the exact commit, and refuses to publish a commit that lacks
either.

It is small on purpose: Python's standard library only, one JSON config, and no service beyond
a message bus. It is young. Codex and Claude Code roles have both run real tasks with it; the
known gaps are listed at the end.

You need Python 3, git, [`bd`](https://github.com/steveyegge/beads) (the task tracker), and the Codex or Claude Code CLI,
installed and signed in.

## The one sandbox rule

Provider permission checks are off. Use a VM or a machine you let agents use freely;
the machine boundary is the sandbox. ring2 does not handle permission prompts.

## Quick start

Run these from a clone of this repository. Replace the paths and the model:

```sh
python3 -m ring2.busd &
(cd /path/to/tracker && bd init --prefix demo)
cat > ring2.json <<'EOF'
{"home":"/path/to/fleet", "origin":"/path/to/repo",
 "ring_url":"http://127.0.0.1:8770", "channels":["team"],
 "tracker_dir":"/path/to/tracker",
 "test_command":"make test",
 "roles":{
  "coord":{"role":"coordinator","provider":"codex","model":"<model>"},
  "ann":{"role":"implementer","provider":"codex","model":"<model>"},
  "rae":{"role":"reviewer","provider":"codex","model":"<model>"},
  "controller":{"role":"controller","provider":"external"}}}
EOF
python3 -m ring2 --config ring2.json fleet start
python3 -m ring2.console --config ring2.json &
```

`origin` is the git repository the fleet works on. The console at http://127.0.0.1:8780 shows
each role and the messages between them.

Give the fleet a task:

```sh
(cd /path/to/tracker && bd create "First task" --description "What to change" --labels queue)
python3 -m ring2 --config ring2.json --as controller post team "@coord one task is queued"
```

From here the roles do the rest themselves, with the commands their briefs give them: `claim`,
`test`, `review-pass` and `publish --close`. You watch the console, and the change arrives on
the `main` branch of `origin`. To stop:

```sh
python3 -m ring2 --config ring2.json fleet stop
```

Project rules for the agents belong in an `AGENTS.md` in your repository; both Codex and Claude
Code read it. For a Claude Code role, use `"provider": "claude"` with a Claude model. Each part
of ring2 also runs alone and prints its own usage. How it is put together, and its tests:
[CONTRIBUTING.md](CONTRIBUTING.md).

## Settings

App JSON keys unless marked otherwise; role fields belong inside each role. Service options use module.option; uppercase names are environment variables.

| Setting | Default | Use |
|---|---|---|
| `home` | unset | Stable fleet root; relative to config. |
| `origin` | unset | Repository to clone for a fleet home. |
| `state_dir` | `home/state`, otherwise `state` beside the config | Handles, locks, logs, specs, cursors. |
| `roles` | empty map | Entries keyed by stable label. |
| `role` | required for every roster entry, including roles ring2 does not start | Role field; reviewer identifies whose PASS counts. |
| `provider` | required | Role field; adapter filename without .py. |
| `model` | required for managed roles | Role field; provider model name. |
| `brief` | shipped file for the role | Role field; first instructions. |
| `workdir` | `home/work/<label>`, otherwise required | Role field; outside roles need none. |
| `fresh_conversation` | `false` | Role field; fresh idle Codex conversations; needs tracker_dir. |
| `resume` | false | Role field; resume on restart; standalone takes thread ID. |
| `env` | empty map | Provider environment overrides. |
| `codex` | `codex` | Codex executable. |
| `claude` | `claude` | Claude Code executable. |
| `ring_url` | unset | Bus URL. |
| `channels` | unset | Followed bus channels. |
| `ready_seconds` | 60 | Fleet READY timeout, seconds. |
| `tracker_dir` | unset | bd tracker directory. |
| `queue_label` | `queue` | Claim queue label. |
| `remote` | `origin` | Publication remote. |
| `branch` | `main` | Publication branch. |
| `test_command` | unset | Required test command; override must match. |
| `test_required` | true | Require candidate test PASS; standalone --no-test disables. |
| `review_required` | true | Require reviewer PASS; standalone --no-review disables. |
| `pass_carries_rebase` | false | Same-task PASS for identical stable patch ID; standalone --pass-carries-rebase. |
| `repo` | role workdir in the app; required standalone | Standalone ship repository. |
| `reviewers` | app's reviewer roles; empty standalone | Standalone reviewer labels; app uses reviewer roles. |
| `sessions` | empty map | Standalone sessions by name. |
| `prompt` | required standalone | Standalone first prompt. |
| `source` | unset | Standalone bus input mapping. |
| `source.label` | required with `source` | Standalone recipient. |
| `source.cursor` | required with `source` | Standalone cursor file. |
| `busd.host` | `RING_HOST` or `127.0.0.1` | Bus bind address. |
| `busd.port` | `RING_PORT` or 8770 | Bus port. |
| `busd.db` | `RING_DB` or `~/.local/share/ring2/bus.db` | Bus database. |
| `watch.interval` | 5 | Watch interval, seconds. |
| `watch.backoff` | 30 | Restart delay, seconds; doubles to 600. |
| `console.port` | 8780 | Loopback console port. |
| `RING2_CONFIG` | unset | Default --config; set in managed sessions. |
| `RING2_ROLE` | unset | Default --as; set in managed sessions. |
| `RING_HOST` | unset | Bus bind override. |
| `RING_PORT` | unset | Bus port override. |
| `RING_DB` | unset | Bus database override. |

## The controller

To try ring2, a terminal and the console are enough; no controller session is needed.
To run a fleet for real, start a Claude Code or Codex session in a folder of its own, not in
a clone of the repository the fleet works on, and copy `ring2/briefs/controller.md` into that
folder's `AGENTS.md`. Keep a working copy of the repository there for reading the fleet's changes.

The controller files tasks, reads what the roles publish, and decides what leaves the machine.
The recommended setup is the fleet inside a VM (or a machine given over to agents) and the
controller on a different machine, outside that sandbox. Permission checks are off for the
fleet, so its agents can change anything on their own machine; the controller's copy of the
work is out of their reach only when it is on another machine.
We recommend keeping it outside the fleet, because it is the session a person types into, it
checks the fleet's work from a copy the fleet cannot change, and it survives a fleet restart.

Add it to the roster as an `external` role. ring2 never starts or stops it.
The channel server and the `listen` command run on the fleet's machine and can be started
from the controller's machine over ssh.

With Claude Code, turn on the channel so messages addressed to the controller arrive in the
session by themselves:

```json
{"mcpServers":{"ring2":{"command":"python3","args":["-m","ring2.channel","--config","/path/to/ring2.json","--as","controller"]}}}
```

Start Claude Code with this repository on Python's import path and
`--dangerously-load-development-channels server:ring2`. Channels are an experimental Claude
Code feature. The server offers no tools; reply with the `post` command.

A listener on any machine is visible to `status` and the console: each fetch tells the bus the
sequence it has read up to, and the bus remembers the latest report per label, in memory only.
`status` shows the controller as `alive` when the bus heard from it in the last 90 seconds,
`silent` with the time it last heard when longer, and `not heard from` when it has no record
(for example after a bus restart, until the listener's next fetch).

For a Codex controller, hand each message to its interactive session with:

```sh
python3 -m ring2 --config /path/to/ring2.json --as controller listen -- codex queue --thread ID --message
```

Get `ID` from the controller session's `/status` display. This was verified on Codex 0.163.0 and 0.171.0;
`codex queue` is Codex's own command and may change. Failed commands are reported on standard
error and retried before the cursor advances.

With any other harness, or by hand, `listen` prints the same messages in a terminal when no
command is given. Use one receiver per label. The channel setup is verified with Claude Code.

## Known gaps

- An outside role's state comes from what the bus last heard from its listener, so a listener that has gone quiet shows as `silent`, not dead, and the bus forgets all of it when it restarts.
- Typing in an attached Codex viewer during a channel turn can drop the channel request (3/3 trials). Watching, or typing while idle, was unaffected.
- `test` and `publish` do not check task ownership; `close` refuses only another holder.
- Tracker read errors prevent `status` from showing sessions.
- Simultaneous claims by one label can hold two tasks; different labels did not duplicate a claim (0/60).
- Roles see task titles only; read descriptions with `bd show`.
- Account connectors stayed out in one real signed-in Claude run (2026-11-20); that is one run, not a guarantee. Claude startup ends the role if an MCP server loads.
- Claude stream sessions cannot use `claude attach`; stop before interactive resume.
- Claude delivery waits for turns; stalled turns delay messages.
- `status` cannot distinguish stopped from dead or detect stuck sessions. `watch` restarts stopped roles with waiting messages; stop watch first. Nothing restarts watch.
- Stopping does not release tasks (`give-back` does); removing roles leaves folders.
- After give-back, unpushed commits block claiming.
- The bus retains 64 messages per channel; laggards miss older messages.
- bd leases last five minutes; ring2 neither renews nor reclaims; expiry behaviour is untested.
- Later objections do not revoke PASS.
- Codex session files grow without pruning.

With `pass_carries_rebase`, the reviewer has not seen the change on its new base; only rerun tests cover integration.
