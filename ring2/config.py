"""One role's configuration: a JSON file.

Required: role, provider, model, brief, workdir.
Operational: state_dir, tmux_socket, ring_url, channels, tracker_dir,
codex (binary), codex_args (extra argv), env (session environment).
"""
import json
import os
from pathlib import Path

REQUIRED = ('role', 'provider', 'model', 'brief', 'workdir')


def load(path):
    path = Path(path).resolve()
    cfg = json.loads(path.read_text())
    missing = [key for key in REQUIRED if not cfg.get(key)]
    if missing:
        raise SystemExit(f'{path}: missing {", ".join(missing)}')
    if cfg['provider'] != 'codex':
        raise SystemExit(f'{path}: provider {cfg["provider"]!r} is not built in this slice')
    cfg['path'] = str(path)
    cfg['workdir'] = str(Path(cfg['workdir']).resolve())
    cfg.setdefault('state_dir', str(path.parent / 'state'))
    cfg.setdefault('tmux_socket', 'ring2')
    cfg.setdefault('channels', [])
    cfg.setdefault('codex', 'codex')
    cfg.setdefault('codex_args', [])
    cfg.setdefault('env', {})
    return cfg


def state_path(cfg, suffix='json'):
    return Path(cfg['state_dir']) / f'{cfg["role"]}.{suffix}'


def read_state(cfg):
    try:
        return json.loads(state_path(cfg).read_text())
    except FileNotFoundError:
        return None


def write_state(cfg, state):
    path = state_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(state, indent=1))
    os.replace(tmp, path)
