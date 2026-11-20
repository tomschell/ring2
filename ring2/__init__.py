"""ring2: five small modules that meet only at plain data.

  bus       post and watch by @label              (a Ring server)
  sessions  start/stop/restart Codex in tmux      (tmux, codex)
  delivery  messages into a running session       (codex; any message source)
  tasks     queue, atomic claim, notes, close     (bd)
  ship      test result, PASS, fast-forward       (git)

Each has its own entry point (`python3 -m ring2.<module> --help`) and takes
only its own settings. `python3 -m ring2` (app.py) is the assembled tool that
reads one shared config file. This file holds the little they all share.
"""
import subprocess

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
