"""The Ring bus over HTTP, and the follower that hands a role its messages.

Delivery uses `codex queue`: Codex holds a queued message while a turn is
active and starts a turn with it once the thread is idle.
"""
import json
import re
import subprocess
import time
import urllib.parse
import urllib.request

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


def addressed(role, text):
    return re.search(r'(?<![\w@])@(?:%s|all)(?![\w-])' % re.escape(role), text) is not None


def queue(cfg, state, text):
    subprocess.run([cfg['codex'], 'queue', '--remote', state['endpoint'], '--thread', state['thread'],
                    '--message', text], check=True, capture_output=True, text=True, timeout=60)


def follow(cfg, token):
    """Deliver addressed messages after the saved cursor to the session `token` names."""
    cursor_path = config.state_path(cfg, 'cursor')
    cursor = int(cursor_path.read_text()) if cursor_path.exists() else 0
    while True:
        state = config.read_state(cfg)
        if not state or state['token'] != token:
            return 0  # a successor owns the role now
        try:
            messages = events(cfg, cursor)
        except OSError as exc:
            print(f'ring2 follow: bus unavailable ({exc}); retrying', flush=True)
            time.sleep(2)
            continue
        for message in messages:
            if addressed(cfg['role'], message['message']):
                queue(cfg, state, f'Ring message {message["sequence"]} on {message["channel"]}:\n'
                                  f'{message["message"]}')
                print(f'ring2 follow: queued {message["sequence"]}', flush=True)
            cursor = message['sequence']
            cursor_path.write_text(str(cursor))
