"""ring2 start|stop|restart|status|task|post|follow <config.json>"""
import argparse
import json

from ring2 import bus, config, session, task


def main(argv=None):
    parser = argparse.ArgumentParser(prog='ring2', description=__doc__)
    parser.add_argument('command', choices=['start', 'stop', 'restart', 'status', 'task', 'post', 'follow'])
    parser.add_argument('config')
    parser.add_argument('args', nargs='*')
    args = parser.parse_args(argv)
    cfg = config.load(args.config)
    if args.command == 'start':
        session.start(cfg)
    elif args.command == 'stop':
        session.stop(cfg)
    elif args.command == 'restart':
        session.restart(cfg)
    elif args.command == 'status':
        print(json.dumps({'running': session.running(cfg), 'state': config.read_state(cfg)}, indent=1))
    elif args.command == 'task':
        print(json.dumps(task.mine(cfg), indent=1))
    elif args.command == 'post':
        channel, text = args.args
        print(json.dumps(bus.post(cfg, channel, text)))
    elif args.command == 'follow':
        return bus.follow(cfg, args.args[0])
    return 0
