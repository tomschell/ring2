"""The project configuration: one JSON file.

  roles            {label: {role, provider, model, brief, workdir}}; role is
                   "implementer" or "reviewer"
  state_dir, tmux_socket, ring_url, channels, tracker_dir   where things live
  queue_label      label that marks a queued task (default "queue")
  review_required  publish needs a reviewer PASS (default true)
  ready_seconds    how long fleet start waits for READY answers (default 60)
  codex, codex_args, env   Codex binary, extra argv, session environment
"""
import json
import os
import subprocess
from pathlib import Path

REQUIRED = ('role', 'provider', 'model', 'brief', 'workdir')
UNCONFIRMED = 'could not confirm whether this happened'


def load(path):
    path = Path(path).resolve()
    cfg = json.loads(path.read_text())
    for label, role in cfg.get('roles', {}).items():
        missing = [key for key in REQUIRED if not role.get(key)]
        if missing:
            raise SystemExit(f'{path}: role {label} is missing {", ".join(missing)}')
        if role['provider'] != 'codex':
            raise SystemExit(f'{path}: provider {role["provider"]!r} is not built')
        role['workdir'] = str(Path(role['workdir']).resolve())
    cfg['path'] = str(path)
    for key, default in (('state_dir', str(path.parent / 'state')), ('tmux_socket', 'ring2'),
                         ('channels', []), ('queue_label', 'queue'), ('review_required', True), ('ready_seconds', 60),
                         ('codex', 'codex'), ('codex_args', []), ('env', {})):
        cfg.setdefault(key, default)
    return cfg


def role(cfg, label):
    if label not in cfg['roles']:
        raise SystemExit(f'ring2: no role {label!r} in {cfg["path"]} (use --as or RING2_ROLE)')
    return cfg['roles'][label]


def state_path(cfg, label, suffix='json'):
    return Path(cfg['state_dir']) / f'{label}.{suffix}'


def read_state(cfg, label):
    try:
        return json.loads(state_path(cfg, label).read_text())
    except FileNotFoundError:
        return None


def write_state(cfg, label, state):
    path = state_path(cfg, label)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(state, indent=1))
    os.replace(tmp, path)


def effect(what, argv, cwd):
    """Run one command that changes the tracker or origin.

    A failure here does not show the change was not made (the reply may be
    what was lost), so the error never says it failed. Every caller first
    checks whether the change is already there, so rerunning is safe.
    """
    try:
        result = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        raise SystemExit(f'ring2: {what}: {UNCONFIRMED} (no reply); rerun the command')
    if result.returncode:
        detail = (result.stderr or result.stdout).strip().splitlines()[-1:]
        raise SystemExit(f'ring2: {what}: {UNCONFIRMED} ({" ".join(detail)}); rerun the command')
    return result.stdout
