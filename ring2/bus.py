"""Bus: post a message, watch for messages by @label. Talks HTTP to the bus
server (`python3 -m ring2.busd`). A message is one line, at most 4096 bytes.

Settings: ring_url, channels.
`post` returning means the server stored the message (it has a sequence); it
says nothing about anyone having read it. Messages are coordination between
sessions, never authority to run a command.

  python3 -m ring2.bus --url URL --channel NAME post TEXT
  python3 -m ring2.bus --url URL --channel NAME watch --as LABEL
"""
import argparse
import json
import re
import urllib.error
import urllib.parse
import urllib.request

from ring2 import need


def post(settings, channel, text):
    need(settings, 'bus', 'ring_url')
    request = urllib.request.Request(
        f'{settings["ring_url"]}/channels/{urllib.parse.quote(channel, safe="")}',
        data=json.dumps({'message': text}).encode(), headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)['message']


def events(settings, after, wait=25, label=None):
    """Messages on the channels with a sequence above `after`, waiting up to `wait` seconds for one.
    A listener passes its `label`, which tells the bus it has read up to `after`."""
    need(settings, 'bus', 'ring_url', 'channels')
    query = urllib.parse.urlencode(
        [('channel', channel) for channel in settings['channels']] + [('after', after), ('wait', wait)]
        + ([('as', label), ('read', after)] if label else []))
    with urllib.request.urlopen(f'{settings["ring_url"]}/events?{query}', timeout=wait + 30) as response:
        return json.load(response)['messages']


def listeners(settings):
    """{label: {'read': sequence, 'age': seconds since the bus last heard from it}}; empty from a bus that keeps none."""
    need(settings, 'bus', 'ring_url')
    try:
        with urllib.request.urlopen(f'{settings["ring_url"]}/listeners', timeout=30) as response:
            return json.load(response)['listeners']
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return {}
        raise


def end(settings):
    """The newest sequence on the channels (0 when empty)."""
    return max((message['sequence'] for message in events(settings, 0, wait=0)), default=0)


def addressed(label, text):
    return (not text.startswith(f'{label}: ')
            and re.search(r'(?<![\w@])@(?:%s|all)(?![\w-])' % re.escape(label), text) is not None)


def watch(settings, label, after=None):
    """Yield each new message that names @label or @all, from the end of the bus on."""
    after = end(settings) if after is None else after
    while True:
        for message in events(settings, after):
            after = message['sequence']
            if addressed(label, message['message']):
                yield message


def main(argv=None):
    parser = argparse.ArgumentParser(prog='ring2.bus', description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--url')
    parser.add_argument('--channel', action='append')
    parser.add_argument('--as', dest='label')
    parser.add_argument('command', choices=['post', 'watch'])
    parser.add_argument('text', nargs='?')
    opts = parser.parse_args(argv)
    settings = {'ring_url': opts.url, 'channels': opts.channel}
    need(settings, 'bus', 'ring_url', 'channels')
    if opts.command == 'post':
        print(json.dumps(post(settings, opts.channel[0], opts.text)))
    else:
        need({'as': opts.label}, 'bus', 'as')
        for message in watch(settings, opts.label):
            print(json.dumps(message), flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
