"""Delivery: get messages into a running session without interrupting its turn.

It needs a message source and a session input, not the sessions module:
  source(after, wait) -> [{sequence, channel, message}]   newer than `after`
  wanted(message) -> bool                                  which ones to deliver
  target                                                   a session input: send(text),
                                                           started(marker), own()

CodexQueue is the input for a Codex thread served by an app-server, whoever
started it. It is specific to Codex: `codex queue` hands the text to Codex,
which holds it while a turn is active and starts a turn with it when the
thread is idle. So send() returning means ACCEPTED by Codex, not delivered;
started() says whether the message's turn has begun (it then shows in the
thread's rollout file). Another provider needs its own input class.

The saved cursor passes a message only once started() is true. After a restart
the loop begins from the cursor, so a message still waiting in the old
session's queue is sent again to the new one: a message can arrive twice, it
is not lost.

One loop owns a thread's input. A second loop for the same thread refuses to
start (an advisory lock on the thread's rollout file).

  python3 -m ring2.delivery --ring-url URL --channel NAME --as LABEL
        --endpoint ws://HOST:PORT --thread ID --cursor FILE
        [--rollout FILE | --codex-home DIR] [--codex BIN]
"""
import argparse
import fcntl
import os
import subprocess
import time
from pathlib import Path

from ring2 import Refused, need


class CodexQueue:
    def __init__(self, endpoint, thread, rollout=None, codex='codex', codex_home=None):
        self.endpoint, self.thread, self.codex = endpoint, thread, codex
        self.home = Path(codex_home or os.environ.get('CODEX_HOME') or Path.home() / '.codex')
        self.rollout = Path(rollout) if rollout else None

    def send(self, text):
        """Hand `text` to Codex's queue for the thread. Returns once Codex accepted it."""
        subprocess.run([self.codex, 'queue', '--remote', self.endpoint, '--thread', self.thread,
                        '--message', text], check=True, capture_output=True, text=True, timeout=60)
        return 'accepted'

    def _rollout(self):
        if not self.rollout:
            found = list(self.home.glob(f'sessions/*/*/*/rollout-*{self.thread}.jsonl'))
            if not found:
                raise Refused(f'ring2 delivery: no rollout file for thread {self.thread} under {self.home}')
            self.rollout = found[0]
        return self.rollout

    def started(self, marker):
        """Whether a turn carrying `marker` has begun in the thread."""
        return marker in self._rollout().read_text(errors='replace')

    def own(self):
        """Become the one delivery loop for this thread, for as long as this process lives
        (an advisory lock on the thread's rollout file; Codex itself is not affected)."""
        self._lock = open(self._rollout())
        try:
            fcntl.flock(self._lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Refused(f'ring2 delivery: thread {self.thread} already has a delivery loop; not starting a second')


def save_cursor(cursor_path, sequence):
    """Replace the cursor whole (temp file, then rename): a cut leaves the old value, never a partial one."""
    cursor_path = Path(cursor_path)
    cursor_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = cursor_path.with_name(cursor_path.name + '.tmp')
    tmp.write_text(str(sequence))
    os.replace(tmp, cursor_path)


def init_cursor(source, cursor_path):
    """A first-ever cursor starts at the end of the source, not at its history."""
    if not Path(cursor_path).exists():
        save_cursor(cursor_path, max((m['sequence'] for m in source(0, 0)), default=0))
    return int(Path(cursor_path).read_text())


def marker(sequence):
    return f'Ring message {sequence} on '


def follow(source, wanted, target, cursor_path, log=print):
    """Deliver wanted messages after the saved cursor, in order, forever."""
    target.own()
    cursor = sent = init_cursor(source, cursor_path)
    waiting = []  # sequences accepted by the session whose turn has not started yet
    while True:
        try:
            messages = source(sent, 2 if waiting else 25)
        except OSError as exc:
            log(f'ring2 delivery: source unavailable ({exc}); retrying')
            time.sleep(2)
            continue
        for message in messages:
            sequence = message['sequence']
            if wanted(message):
                try:
                    target.send(f'{marker(sequence)}{message["channel"]}:\n{message["message"]}')
                except (subprocess.SubprocessError, OSError) as exc:
                    log(f'ring2 delivery: the session did not take {sequence} ({exc}); retrying')
                    time.sleep(2)
                    break
                log(f'ring2 delivery: accepted {sequence}')
                waiting.append(sequence)
            sent = sequence
        while waiting and target.started(marker(waiting[0])):
            log(f'ring2 delivery: started {waiting.pop(0)}')
        reached = waiting[0] - 1 if waiting else sent
        if reached != cursor:
            cursor = reached
            save_cursor(cursor_path, cursor)


def main(argv=None):
    from ring2 import bus  # the one message source this entry point knows
    parser = argparse.ArgumentParser(prog='ring2.delivery', description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    for flag in ('--ring-url', '--as', '--endpoint', '--thread', '--cursor', '--rollout', '--codex-home'):
        parser.add_argument(flag)
    parser.add_argument('--channel', action='append')
    parser.add_argument('--codex', default='codex')
    opts = parser.parse_args(argv)
    settings = {'ring_url': opts.ring_url, 'channels': opts.channel, 'as': getattr(opts, 'as'),
                'endpoint': opts.endpoint, 'thread': opts.thread, 'cursor': opts.cursor}
    need(settings, 'delivery', 'ring_url', 'channels', 'as', 'endpoint', 'thread', 'cursor')
    target = CodexQueue(opts.endpoint, opts.thread, opts.rollout, opts.codex, opts.codex_home)
    follow(lambda after, wait: bus.events(settings, after, wait),
           lambda message: bus.addressed(settings['as'], message['message']),
           target, opts.cursor, log=lambda line: print(line, flush=True))


if __name__ == '__main__':
    raise SystemExit(main())
