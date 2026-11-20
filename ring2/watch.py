"""Watch: restart a role whose delivery has died while a message waits for it.

  python3 -m ring2.watch --config FILE [--interval 5] [--backoff 30]

Optional: nothing else in ring2 imports this file. One loop, one check, one
action. Every `interval` seconds it reads `status` (ring2.app.status). A role
that has a session handle, whose runner is not running, and that has a bus message addressed to it beyond
its cursor, is restarted through the ordinary restart path; the successor
delivers the message, and watch posts one line on the first channel.

It acts on what status shows, never on what a message says. After restarting
a role it leaves that role alone for `backoff` seconds, doubling up to ten
minutes while the role keeps needing it; that count lives in memory only.

It cannot tell a role that died from one that was stopped on purpose: stop
watch before stopping a role you want to stay down.
"""
import argparse
import time

from ring2 import app, bus


def due(row):
    """The one check: delivery is down and a message is waiting."""
    waiting = row.get('delivered') is not None and row.get('addressed', 0) > row['delivered']
    return waiting and not row['loop']


def once(cfg, memory, backoff, now=None, log=print):
    """Look once; restart what is due. `memory` is {label: (not before, next delay)}."""
    now = time.monotonic() if now is None else now
    acted = []
    for row in app.status(cfg):
        label = row['label']
        if not due(row):
            if row['loop'] and now >= memory.get(label, (0, 0))[0]:
                memory.pop(label, None)  # healthy past its quiet period: the next failure starts from the short delay
            continue
        not_before, delay = memory.get(label, (0, backoff))
        if now < not_before:
            continue
        memory[label] = (now + delay, min(delay * 2, 600))
        try:
            app.stop(cfg, label)
            app.start(cfg, label)
            line = (f'watch: restarted {label}; its delivery was not running and message '
                    f'{row["addressed"]} was waiting (delivered up to {row["delivered"]})')
        except SystemExit as exc:
            line = f'watch: could not restart {label} ({exc}); next try in {delay} s'
        log(line)
        bus.post(cfg, cfg['channels'][0], line)
        acted.append(label)
    return acted


def main(argv=None):
    parser = argparse.ArgumentParser(prog='ring2.watch', description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--config', required=True)
    parser.add_argument('--interval', type=float, default=5)
    parser.add_argument('--backoff', type=float, default=30)
    opts = parser.parse_args(argv)
    cfg, memory = app.load(opts.config), {}
    while True:
        try:
            once(cfg, memory, opts.backoff, log=lambda line: print(line, flush=True))
        except (OSError, SystemExit) as exc:  # the bus or the tracker could not be read: look again later
            print(f'watch: could not look ({exc})', flush=True)
        time.sleep(opts.interval)


if __name__ == '__main__':
    raise SystemExit(main())
