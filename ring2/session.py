"""Shared session lifecycle: lock, token, handle, runner, and owned processes."""
import argparse
import fcntl
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

from ring2 import Refused, need, read_handle, stop_processes, write_handle

ROOT = Path(__file__).resolve().parent.parent


def wait(what, check, seconds=60):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = check()
        if value:
            return value
        time.sleep(0.2)
    raise SystemExit(f'ring2: timed out waiting for {what}')


def running(settings, name, kind):
    state = read_handle(settings, name) or {}
    if state.get('kind') != kind or not state.get('pid'):
        return False
    command = subprocess.run(['ps', '-p', str(state['pid']), '-o', 'command='],
                             capture_output=True, text=True).stdout
    return state['token'] in command


def stop(settings, name, kind):
    state = read_handle(settings, name) or {}
    return stop_processes([state['token']]) if state.get('kind') == kind else False


def start(settings, name, spec, provider):
    need(spec, f'{provider.KIND} ({name})', 'model', 'workdir', 'prompt')
    state_dir = Path(settings['state_dir'])
    state_dir.mkdir(parents=True, exist_ok=True)
    with open(state_dir / f'{name}.start.lock', 'w') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise Refused(f'ring2: {name} is being started by another command') from None
        if running(settings, name, provider.KIND):
            raise Refused(f'ring2: {name} is already running')
        stop(settings, name, provider.KIND)
        token = uuid.uuid4().hex
        # Save the token before launching anything, so an interrupted start can be stopped.
        write_handle(settings, {'name': name, 'kind': provider.KIND, 'token': token,
                                'thread': (read_handle(settings, name) or {}).get('thread')})
        spec_path = state_dir / f'{name}.spec.json'
        spec_path.write_text(json.dumps({**spec, 'name': name, 'settings': settings}, indent=1))
        with open(state_dir / f'{name}.log', 'w') as log:
            runner = subprocess.Popen([sys.executable, '-m', provider.__spec__.name, 'run', str(spec_path), token],
                                      env={**os.environ, 'PYTHONPATH': str(ROOT)}, cwd=spec['workdir'],
                                      stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
        def started():
            if runner.poll() is not None:
                raise SystemExit(f'ring2: the runner for {name} exited; see {log.name}')
            state = read_handle(settings, name)
            return state if state and state['token'] == token and state.get('pid') else None
        return wait(f'the {provider.KIND} session', started)


def main(provider, argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ['run']:
        return provider.run(*argv[1:])
    parser = argparse.ArgumentParser(prog=provider.__spec__.name, description=provider.__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--config', required=True)
    parser.add_argument('command', choices=['start', 'stop', 'restart', 'status'])
    parser.add_argument('name')
    opts = parser.parse_args(argv)
    settings = json.loads(Path(opts.config).read_text())
    spec = settings.pop('sessions', {}).get(opts.name)
    if opts.command in ('start', 'restart'):
        need({'session': spec}, f'{provider.KIND} ({opts.name})', 'session')
        if opts.command == 'restart':
            stop(settings, opts.name, provider.KIND)
        print(json.dumps(start(settings, opts.name, spec, provider), indent=1))
    elif opts.command == 'stop':
        print('stopped' if stop(settings, opts.name, provider.KIND) else 'not running')
    else:
        print(json.dumps({'running': provider.status(settings, opts.name)['loop'],
                          'handle': read_handle(settings, opts.name)}, indent=1))
    return 0
