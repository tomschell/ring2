"""Delivery from a message source into a session input: send, started, own.
The cursor passes a message only once started() is true. A restart replays
messages beyond the cursor; a message can arrive twice, but is not lost.
"""
import os
import subprocess
import time
from pathlib import Path


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
