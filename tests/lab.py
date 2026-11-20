"""A throwaway lab for ring2 tests: temp root, bare origin with one clone per
role, a bd tracker, the local Ring bus server (ring2.busd), a mock model and a
private tmux socket. Nothing reaches a model provider, the user's Codex home,
the default tmux server or any tracker outside the temp root.

`bd`, `git` and `codex` run through shims in the lab so a test can lose the
reply of one command after its effect (lose_reply) or make `codex queue` fail.
RING2_KEEP=1 keeps the temp root for inspection.
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

from tests.mock_model import MockModel

REPO = Path(__file__).resolve().parents[1]
CODEX = os.environ.get('RING2_CODEX') or shutil.which('codex')
HAVE_TOOLS = bool(CODEX and shutil.which('tmux') and shutil.which('bd'))

SHIM = '''#!/bin/bash
# Stand in front of the real {name}: lose one reply after the effect, or fail outright.
here="$(dirname "$0")"
if [ -f "$here/lose-reply-{name}" ] && [[ " $* " == *" $(cat "$here/lose-reply-{name}") "* ]]; then
  rm "$here/lose-reply-{name}"; "{real}" "$@" >/dev/null 2>&1; echo "connection lost" >&2; exit 1
fi
if [ -f "$here/refuse-{name}" ] && [[ " $* " == *" $(cat "$here/refuse-{name}") "* ]]; then
  echo "refused by the test" >&2; exit 1
fi
exec "{real}" "$@"
'''


def wait(what, check, seconds=45):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = check()
        if value:
            return value
        time.sleep(0.1)
    raise AssertionError(f'timed out waiting for {what}')


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


class Lab:
    def __init__(self, roles, codex_extra='', bd_mode='embedded', **settings):
        """roles: {label: (role, brief[, more role settings])}; settings are merged into the config.
        bd_mode 'server' runs the tracker against a throwaway local Dolt sql-server (as production
        does) with its data under the worktree's .ring2-tmp; 'embedded' needs no server."""
        self.root = root = Path(tempfile.mkdtemp(prefix='ring2-')).resolve()
        self.home, self.tracker, self.shims = root / 'home', root / 'tracker', root / 'shims'
        for path in (self.home / 'codex', self.tracker, self.shims):
            path.mkdir(parents=True)
        for name, real in (('bd', shutil.which('bd')), ('git', shutil.which('git')), ('codex', CODEX)):
            (self.shims / name).write_text(SHIM.format(name=name, real=real))
            (self.shims / name).chmod(0o755)
        self.env = dict(os.environ, HOME=str(self.home), CODEX_HOME=str(self.home / 'codex'),
                        BD_NON_INTERACTIVE='1', PYTHONPATH=str(REPO), PATH=f'{self.shims}:{os.environ["PATH"]}',
                        GIT_AUTHOR_NAME='ring2', GIT_AUTHOR_EMAIL='ring2@example.invalid',
                        GIT_COMMITTER_NAME='ring2', GIT_COMMITTER_EMAIL='ring2@example.invalid')
        for name in ('OPENAI_API_KEY', 'RING2_CONFIG', 'RING2_ROLE'):
            self.env.pop(name, None)
        self.socket = f'ring2-test-{uuid.uuid4().hex[:8]}'
        self.model = self.busd = self.dolt = self.dolt_data = None

        # A bare origin, with one clone per role as its working directory.
        self.origin = root / 'origin.git'
        self.run('git', 'init', '-q', '--bare', '-b', 'main', str(self.origin))
        self.work = {label: root / f'work-{label}' for label in roles}
        first = None
        for label, workdir in self.work.items():
            self.run('git', 'clone', '-q', str(self.origin), str(workdir))
            if first is None:
                first = self.commit(label, 'tracked.txt', 'committed\n')
                self.run('git', 'push', '-q', 'origin', 'HEAD:main', cwd=workdir)
        if bd_mode == 'server':
            self.start_dolt()
            self.run('git', 'init', '-q', str(self.tracker))
            self.run('bd', 'init', '--server', '--external', '--server-host', '127.0.0.1', '--server-port',
                     str(self.dolt_port), '--prefix', 'exp', '--non-interactive', '--skip-hooks', '--skip-agents',
                     cwd=self.tracker)
        else:
            self.run('bd', 'init', '--prefix', 'exp', '--non-interactive', cwd=self.tracker)

        self.model = MockModel()
        (self.home / 'codex' / 'config.toml').write_text(self.model.codex_config(self.work.values(), codex_extra))
        ring_port = self.free_port()
        self.busd = subprocess.Popen([sys.executable, '-m', 'ring2.busd', '--port', str(ring_port),
                                       '--db', str(root / 'ring.db')], env=self.env,
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.settings = {
            'roles': {label: {'role': role, 'provider': 'codex', 'model': 'gpt-6-astra', 'brief': brief,
                              'workdir': str(self.work[label]), **dict(*more)}
                      for label, (role, brief, *more) in roles.items()},
            'tmux_socket': self.socket, 'ring_url': f'http://127.0.0.1:{ring_port}', 'channels': ['exp'],
            'tracker_dir': str(self.tracker), 'codex': str(self.shims / 'codex'),
            'env': {'CODEX_HOME': str(self.home / 'codex')}, **settings}
        self.config = self.write_config('ring2.json')
        wait("the bus server", lambda: self.ring2('post', 'exp', 'bus is up', check=False).returncode == 0)

    def start_dolt(self):
        base = REPO / '.ring2-tmp'
        base.mkdir(exist_ok=True)
        self.dolt_data = Path(tempfile.mkdtemp(prefix='dolt-', dir=base))
        self.env['DOLT_DISABLE_EVENT_FLUSH'] = '1'  # with metrics.disabled: Dolt sends no usage events
        for key, value in (('user.name', 'ring2'), ('user.email', 'ring2@example.invalid'), ('metrics.disabled', 'true')):
            self.run('dolt', 'config', '--global', '--add', key, value)  # global means the lab's HOME
        self.dolt_port = self.free_port()
        self.dolt = subprocess.Popen(['dolt', 'sql-server', '--host', '127.0.0.1', '--port', str(self.dolt_port),
                                      '--data-dir', str(self.dolt_data)], cwd=self.dolt_data, env=self.env,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        def listening():
            with socket.socket() as probe:
                return probe.connect_ex(('127.0.0.1', self.dolt_port)) == 0
        wait('the Dolt sql-server', listening)

    @staticmethod
    def free_port():
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 0))
            return probe.getsockname()[1]

    def write_config(self, name, **changes):
        path = self.root / name
        path.write_text(json.dumps({**self.settings, **changes}, indent=1))
        return path

    def close(self):
        if self.busd:
            for label in self.work:
                self.ring2('stop', label=label, check=False)
            self.busd.terminate()
            self.busd.wait()
        subprocess.run(['tmux', '-L', self.socket, 'kill-server'], capture_output=True)
        socket_file = Path(f'/tmp/tmux-{os.getuid()}') / self.socket
        if socket_file.is_socket():
            socket_file.unlink()
        if self.model:
            self.model.close()
        if self.dolt:
            self.dolt.terminate()
            self.dolt.wait()
        if self.dolt_data:
            shutil.rmtree(self.dolt_data, ignore_errors=True)
            if not any(self.dolt_data.parent.iterdir()):
                self.dolt_data.parent.rmdir()
        if os.environ.get('RING2_KEEP'):
            print(f'kept {self.root}', file=sys.stderr)
        else:
            shutil.rmtree(self.root, ignore_errors=True)

    # commands

    def run(self, *argv, cwd=None):
        return subprocess.run(argv, cwd=cwd, env=self.env, capture_output=True, text=True, check=True).stdout.strip()

    def run_any(self, *argv):
        """Output of a command whatever its exit code (pgrep exits 1 when it finds nothing)."""
        return subprocess.run(argv, env=self.env, capture_output=True, text=True).stdout.strip()

    def ring2_argv(self, command, *args, label=None, config=None):
        return [sys.executable, '-m', 'ring2', '--config', str(config or self.config),
                *(['--as', label] if label else []), command, *args]

    def ring2(self, command, *args, label=None, config=None, check=True):
        result = subprocess.run(self.ring2_argv(command, *args, label=label, config=config),
                                cwd=self.root, env=self.env, capture_output=True, text=True)
        if check and result.returncode:
            raise AssertionError(f'ring2 {command} failed: {result.stdout}{result.stderr}')
        return result

    def bd(self, *args):
        return json.loads(self.run('bd', *args, '--json', cwd=self.tracker))

    def task(self, title, *args):
        return self.bd('create', title, *args)['id']

    def issue(self, task):
        return self.bd('show', task)[0]

    def comments(self, task):
        return [(comment['author'], comment['text']) for comment in self.bd('comments', task)]

    def commit(self, label, name, content):
        workdir = self.work[label]
        (workdir / name).write_text(content)
        self.run('git', 'add', name, cwd=workdir)
        self.run('git', 'commit', '-q', '-m', f'add {name}', cwd=workdir)
        return self.run('git', 'rev-parse', 'HEAD', cwd=workdir)

    def origin_main(self):
        return self.run('git', '--git-dir', str(self.origin), 'rev-parse', 'refs/heads/main')

    def lose_reply(self, name, word):
        """The next `name` command whose arguments include `word` takes effect, then reports an error."""
        (self.shims / f'lose-reply-{name}').write_text(word)

    def post(self, text):
        return json.loads(self.ring2('post', 'exp', text).stdout)['sequence']

    def tmux(self, *args):
        return subprocess.run(['tmux', '-L', self.socket, *args], capture_output=True, text=True).stdout.strip()

    # what the mock model saw

    def requests_with(self, needle):
        return [request for request in list(self.model.requests)
                if any(needle in text for text in self.model.user_texts(request))
                and not any('single-line task title' in text for text in self.model.user_texts(request))]
