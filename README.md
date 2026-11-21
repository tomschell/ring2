# ring2

ring2 runs a fleet of headless coding agents with a message bus, a `bd` task queue,
and git test/review/publication commands. An experiment with a working name;
Codex and Claude Code roles have both run with real models.
Run from a clone with Python 3, git, bd, and your provider CLI installed and signed in.

## The one sandbox rule

Provider permission checks are off. Use a VM or a machine you let agents use freely;
the machine boundary is the sandbox. ring2 does not handle permission prompts.

## Quick start

Start the bus and create a tracker in an existing directory; replace the model and paths below:

```sh
python3 -m ring2.busd &
(cd /path/to/tracker && bd init --prefix demo)
cat > ring2.json <<'EOF'
{"home":"/path/to/fleet", "origin":"/path/to/repo",
 "ring_url":"http://127.0.0.1:8770", "channels":["team"],
 "tracker_dir":"/path/to/tracker",
 "roles":{
  "ann":{"role":"implementer","provider":"codex","model":"<model>"},
  "rae":{"role":"reviewer","provider":"codex","model":"<model>"},
  "controller":{"role":"controller","provider":"external"}}}
EOF
python3 -m ring2 --config ring2.json fleet start
(cd /path/to/tracker && bd create "First task" --description "What to change" --labels queue)
python3 -m ring2 --config ring2.json --as controller post team "@ann please claim a task"
```

The implementer claims, reads the full task with `bd show`, edits and commits;
these commands show one task's remaining steps (substitute its ID and full commit):

```sh
python3 -m ring2 --config ring2.json --as ann claim
python3 -m ring2 --config ring2.json --as ann test <task> <commit> "make test"
python3 -m ring2 --config ring2.json --as rae review-pass <task> <commit>
python3 -m ring2 --config ring2.json --as ann publish <task> <commit> --close
python3 -m ring2 --config ring2.json status
python3 -m ring2 --config ring2.json fleet stop
```

App evidence lives in task comments. Home claims create task branches and refuse unsaved work.
Project rules belong in `AGENTS.md`; both harnesses read it headless.
Use `provider: "claude"` with a Claude model for Claude Code.
Its adapter disables account connectors and configured MCP servers; startup rejects any loaded MCP server.
With fresh conversations, follow-ups must name the task, full commit and question.
Standalone bus, tasks and ship commands print their own usage.
Contributor architecture and tests: [CONTRIBUTING.md](CONTRIBUTING.md).

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

Use an `external` role. Receive addressed messages with
`python3 -m ring2 --config ring2.json --as controller listen`, or configure Claude Code:

```json
{"mcpServers":{"ring2":{"command":"python3","args":["-m","ring2.channel","--config","/path/to/ring2.json","--as","controller"]}}}
```

Run with this repo on Python's import path and
`--dangerously-load-development-channels server:ring2`. Use one receiver per label;
listener and channel server share the cursor and lock. The server offers no tools;
reply with `python3 -m ring2 --config ring2.json --as controller post team "<text>"`.
Create tasks in the tracker with `bd create`, then notify the coordinator.

## Known gaps

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
