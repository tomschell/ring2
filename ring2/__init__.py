"""ring2: five small modules that meet only at plain data.

  bus       post and watch by @label              (the bus server, ring2.busd)
  sessions  start/stop/restart Codex in tmux      (tmux, codex)
  headless  the same without tmux or a TUI        (codex)
  delivery  messages into a running session       (codex; any message source)
  tasks     queue, atomic claim, notes, close     (bd)
  ship      test result, PASS, fast-forward       (git)

Each has its own entry point (`python3 -m ring2.<module> --help`) and takes
only its own settings. `python3 -m ring2` (app.py) is the assembled tool that
reads one shared config file. This file holds the little they all share.
"""
import json
import os
import subprocess
from pathlib import Path

UNCONFIRMED = 'could not confirm whether this happened'


class Refused(SystemExit):
    """Nothing was changed."""


class Unconfirmed(SystemExit):
    """A change was attempted and its outcome is not known. Rerunning is safe."""


def need(settings, module, *keys):
    missing = [key for key in keys if not settings.get(key)]
    if missing:
        raise Refused(f'ring2 {module}: missing setting {", ".join(missing)}')


def effect(what, argv, cwd=None):
    """Run one command that changes a tracker or a git remote.

    A failure here does not show the change was not made (the reply may be
    what was lost), so the error never says it failed. Every caller first
    checks whether the change is already there, so rerunning is safe.
    """
    try:
        result = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        raise Unconfirmed(f'ring2: {what}: {UNCONFIRMED} (no reply); rerun the command')
    if result.returncode:
        detail = (result.stderr or result.stdout).strip().splitlines()[-1:]
        raise Unconfirmed(f'ring2: {what}: {UNCONFIRMED} ({" ".join(detail)}); rerun the command')
    return result.stdout


def read_handle(settings, name):
    """The handle of the last session started under this name, or None."""
    need(settings, 'sessions', 'state_dir')
    try:
        return json.loads((Path(settings['state_dir']) / f'{name}.json').read_text())
    except FileNotFoundError:
        return None


def write_handle(settings, state):
    need(settings, 'sessions', 'state_dir')
    path = Path(settings['state_dir']) / f'{state["name"]}.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(state, indent=1))
    os.replace(tmp, path)
