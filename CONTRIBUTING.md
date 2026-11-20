# Contributing to ring2

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

## Tests

```
python3 -m unittest discover -s tests -t .
```

The suite needs `codex`, `bd` and `git`; Claude adapter tests also need `claude`, and server-mode
tracker tests need `dolt`. Only the test that attaches a TUI needs `tmux`; it skips when tmux is
missing. The suite runs real provider CLIs against local stand-ins with throwaway homes and
trackers and a local bare git origin. The attach test uses a private tmux socket. It makes no
provider request and takes several minutes.


Run the suite with `RING2_RACES=20` after changing `ring2/tasks.py` or the bd version; the "0 of 60" figure in Known gaps came from the full count.
