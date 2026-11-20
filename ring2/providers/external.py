"""A person or outside session: ring2 never starts or stops it.
listen prints addressed messages and passes the same role cursor as a runner.
The existing start lock is held while listening; no session handle is needed.
"""
import fcntl
from pathlib import Path

from ring2 import Refused, bus, delivery, need


class Output:
    def own(self):
        pass  # listen holds the role's start lock

    def send(self, text):
        print(text.replace('\n', ' '), flush=True)

    def started(self, marker):
        return True  # printing acknowledged the message


def listen(settings, name):
    need(settings, 'listen', 'ring_url', 'channels', 'state_dir')
    directory = Path(settings['state_dir'])
    directory.mkdir(parents=True, exist_ok=True)
    with open(directory / f'{name}.start.lock', 'w') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Refused(f'ring2: {name} is already listening') from None
        cursor = directory / f'{name}.cursor'
        if not cursor.exists():
            delivery.save_cursor(cursor, 0)
        delivery.follow(lambda after, wait: bus.events(settings, after, wait),
                        lambda message: bus.addressed(name, message['message']),
                        Output(), cursor, log=lambda _: None)


def status(settings, name):
    directory, listening = Path(settings['state_dir']), False
    try:
        with open(directory / f'{name}.start.lock') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                listening = True
    except FileNotFoundError:
        pass
    cursor = directory / f'{name}.cursor'
    row = {'session': listening, 'loop': listening,
           'delivered': int(cursor.read_text()) if cursor.exists() else 0}
    if cursor.exists():
        row.update(active=int(cursor.stat().st_mtime), active_source='cursor')
    return row
