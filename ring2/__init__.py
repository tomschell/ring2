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
import signal
import subprocess
import time
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


def _processes():
    """{pid: (parent pid, start time, command line)} for every process."""
    table = {}
    for line in subprocess.run(['ps', '-axo', 'pid=,ppid=,lstart=,command='],
                               capture_output=True, text=True).stdout.splitlines():
        fields = line.split(None, 7)
        if len(fields) == 8:
            table[int(fields[0])] = (int(fields[1]), ' '.join(fields[2:7]), fields[7])
    return table


def token_setting(token):
    """What an app-server's command line carries so that it is found by its session's token:
    a `-c` config setting Codex ignores. An endpoint is no identity; its port gets reused."""
    return f'ring2_session="{token}"'


def owned_processes(identities):
    """{pid: start time} of the processes whose command line carries one of the
    identities (a session token, never an endpoint) and of their descendants."""
    table, identities = _processes(), [identity for identity in identities if identity]
    mine, pid = set(), os.getpid()
    while pid in table:  # never this process or what started it
        mine.add(pid)
        pid = table[pid][0]
    found = {pid for pid, (_, _, command) in table.items()
             if pid not in mine and any(identity in command for identity in identities)}
    grew = True
    while grew:
        children = {pid for pid, (parent, _, _) in table.items() if parent in found and pid not in mine}
        grew = not children <= found
        found |= children
    return {pid: table[pid][1] for pid in found}


def stop_processes(identities, grace=5):
    """End a session's processes and see them gone. True if there were any.

    A pid is a location, and so is an endpoint; the identity is the token on the
    command line, and for a descendant its pid together with its start time. Each
    signal goes only to a process just seen to still be the same one.
    """
    targets = owned_processes(identities)
    if not targets:
        return False

    def remaining():
        targets.update(owned_processes(identities))
        table = _processes()
        return [pid for pid, started in targets.items() if pid in table and table[pid][1] == started]

    def signal_all(number):
        for pid in remaining():
            try:
                os.kill(pid, number)
            except ProcessLookupError:
                pass
    signal_all(signal.SIGTERM)
    deadline = time.monotonic() + grace
    while remaining() and time.monotonic() < deadline:
        time.sleep(0.2)
    signal_all(signal.SIGKILL)  # an app-server with a turn in flight does not leave on SIGTERM
    deadline = time.monotonic() + 30
    while remaining():
        if time.monotonic() > deadline:
            raise SystemExit(f'ring2: processes {remaining()} did not end')
        time.sleep(0.2)
    return True
