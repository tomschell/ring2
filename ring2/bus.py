"""The Ring bus over HTTP, and the follower that hands a role its messages.

Delivery uses `codex queue`: Codex holds a queued message while a turn is
active and starts a turn with it once the thread is idle.

The saved cursor only passes a message once its turn shows in the session's
rollout file. A successor's follower starts from that cursor, so a message
still waiting in the old session's queue is queued again for the new one.
A message can therefore arrive twice; it is not lost.
"""
import json
import re
import subprocess
import time
import urllib.parse
import urllib.request
from pathlib import Path

from ring2 import config


def post(cfg, channel, text):
    request = urllib.request.Request(
        f'{cfg["ring_url"]}/channels/{urllib.parse.quote(channel, safe="")}',
        data=json.dumps({'message': text}).encode(), headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)['message']


def events(cfg, after, wait=25):
    query = urllib.parse.urlencode(
        [('channel', channel) for channel in cfg['channels']] + [('after', after), ('wait', wait)])
    with urllib.request.urlopen(f'{cfg["ring_url"]}/events?{query}', timeout=wait + 30) as response:
        return json.load(response)['messages']


def end(cfg):
    """The newest sequence on the configured channels (0 when empty)."""
    return max((message['sequence'] for message in events(cfg, 0, wait=0)), default=0)


def addressed(label, text):
    return re.search(r'(?<![\w@])@(?:%s|all)(?![\w-])' % re.escape(label), text) is not None


def marker(sequence):
    return f'Ring message {sequence} on '


def follow(cfg, label, token):
    """Deliver addressed messages after the saved cursor to the session `token` names."""
    cursor_path = config.state_path(cfg, label, 'cursor')
    cursor = sent = int(cursor_path.read_text())
    waiting = []  # sequences handed to Codex whose turn has not started yet
    while True:
        state = config.read_state(cfg, label)
        if not state or state['token'] != token:
            return 0  # a successor owns the role now
        try:
            messages = events(cfg, sent, wait=2 if waiting else 25)
        except OSError as exc:
            print(f'ring2 follow: bus unavailable ({exc}); retrying', flush=True)
            time.sleep(2)
            continue
        for message in messages:
            sequence = message['sequence']
            if addressed(label, message['message']):
                text = f'{marker(sequence)}{message["channel"]}:\n{message["message"]}'
                try:
                    subprocess.run([cfg['codex'], 'queue', '--remote', state['endpoint'], '--thread',
                                    state['thread'], '--message', text],
                                   check=True, capture_output=True, text=True, timeout=60)
                except (subprocess.SubprocessError, OSError) as exc:
                    print(f'ring2 follow: codex queue did not take {sequence} ({exc}); retrying', flush=True)
                    time.sleep(2)
                    break
                print(f'ring2 follow: queued {sequence}', flush=True)
                waiting.append(sequence)
            sent = sequence
        if waiting:
            rollout = Path(state['rollout']).read_text(errors='replace')
            while waiting and marker(waiting[0]) in rollout:
                waiting.pop(0)
        reached = waiting[0] - 1 if waiting else sent
        if reached != cursor:
            cursor = reached
            cursor_path.write_text(str(cursor))
