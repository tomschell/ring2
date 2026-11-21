"""Optional Claude Code channel server: receive addressed bus messages over MCP stdio.

  python3 -m ring2.channel --config FILE --as LABEL
Replies use the ordinary ring2 post command; this server offers no tools.
"""
import argparse
import fcntl
import json
import sys
import threading
from pathlib import Path

from ring2 import Refused, app, bus, delivery, need


def serve(settings, label):
    need(settings, 'channel', 'ring_url', 'channels', 'state_dir')
    app.role(settings, label)
    directory = Path(settings['state_dir'])
    directory.mkdir(parents=True, exist_ok=True)
    with open(directory / f'{label}.start.lock', 'w') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Refused(f'ring2: {label} is already listening') from None
        cursor = directory / f'{label}.cursor'
        if not cursor.exists():
            delivery.save_cursor(cursor, 0)
        stopped, output = threading.Event(), threading.Lock()

        def write(message):
            with output:
                print(json.dumps({'jsonrpc': '2.0', **message}), flush=True)

        def follow():
            after = int(cursor.read_text())
            while not stopped.is_set():
                try:
                    messages = bus.events(settings, after, wait=1)
                    for message in messages:
                        if stopped.is_set():
                            return
                        sequence = message['sequence']
                        if bus.addressed(label, message['message']):
                            content = f'{delivery.marker(sequence)}{message["channel"]}: {message["message"]}'
                            write({'method': 'notifications/claude/channel', 'params': {
                                'content': content, 'meta': {'channel': message['channel'], 'sequence': str(sequence)}}})
                        delivery.save_cursor(cursor, sequence)
                        after = sequence
                except OSError as exc:
                    print(f'ring2 channel: {exc}; retrying', file=sys.stderr, flush=True)
                    stopped.wait(1)

        started = False
        try:
            for line in sys.stdin:
                request = json.loads(line)
                if 'id' not in request:
                    continue
                method = request.get('method')
                if method == 'initialize':
                    write({'id': request['id'], 'result': {
                        'protocolVersion': request.get('params', {}).get('protocolVersion', '2024-11-05'),
                        'capabilities': {'experimental': {'claude/channel': {}}},
                        'serverInfo': {'name': 'ring2', 'version': '1'}}})
                    if not started:
                        threading.Thread(target=follow, daemon=True).start()
                        started = True
                elif method == 'ping':
                    write({'id': request['id'], 'result': {}})
                else:
                    write({'id': request['id'], 'error': {'code': -32601, 'message': 'Method not found'}})
        finally:
            stopped.set()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    parser.add_argument('--as', dest='label', required=True)
    opts = parser.parse_args(argv)
    try:
        serve(app.load(opts.config), opts.label)
    except Refused as exc:
        raise SystemExit(str(exc)) from None


if __name__ == '__main__':
    main()
