"""ring2 [--config FILE] [--as LABEL] COMMAND ...

Sessions:  start | stop | restart | status          (the --as role)
           fleet start | fleet stop                  (every role)
Tasks:     task | claim | give-back TASK
           review-pass TASK COMMIT | test TASK COMMIT COMMAND
           publish TASK COMMIT | close TASK [COMMIT] [--reason TEXT] [--confirm]
Bus:       post CHANNEL TEXT

--config defaults to $RING2_CONFIG and --as to $RING2_ROLE; both are set
inside a session.
"""
import argparse
import json
import os

from ring2 import bus, config, session, tracker, work


def brief(issue):
    return f'{issue["id"]} {issue["title"]}'


def main(argv=None):
    parser = argparse.ArgumentParser(prog='ring2', description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--config', default=os.environ.get('RING2_CONFIG'), required='RING2_CONFIG' not in os.environ)
    parser.add_argument('--as', dest='label', default=os.environ.get('RING2_ROLE'))
    parser.add_argument('--reason')
    parser.add_argument('--confirm', action='store_true')
    parser.add_argument('command')
    parser.add_argument('args', nargs='*')
    opts = parser.parse_args(argv)
    cfg, label, command, args = config.load(opts.config), opts.label, opts.command, opts.args
    if command == 'fleet':
        if args == ['start']:
            return 0 if len(session.fleet_start(cfg)) == len(cfg['roles']) else 1
        session.fleet_stop(cfg)
    elif command == 'post':
        print(json.dumps(bus.post(cfg, *args)))
    elif command == 'follow':
        return bus.follow(cfg, *args)
    elif command == 'start':
        session.start(cfg, label)
    elif command == 'stop':
        session.stop(cfg, label)
    elif command == 'restart':
        session.restart(cfg, label)
    elif command == 'status':
        print(json.dumps({'running': session.running(cfg, label), 'state': config.read_state(cfg, label)}, indent=1))
    elif command == 'task':
        print('\n'.join(map(brief, tracker.mine(cfg, label))) or 'EMPTY')
    elif command == 'claim':
        issue = tracker.claim(cfg, label)
        print(brief(issue) if issue else 'EMPTY')
    elif command == 'give-back':
        print('given back' if tracker.give_back(cfg, label, *args) else 'already given back')
    elif command == 'review-pass':
        print('recorded' if work.review_pass(cfg, label, *args) else 'already recorded')
    elif command == 'test':
        passed = work.test(cfg, label, *args)
        print('TEST PASS' if passed else 'TEST FAIL')
        return 0 if passed else 1
    elif command == 'publish':
        print('published' if work.publish(cfg, label, *args) else 'already published')
    elif command == 'close':
        print('closed' if work.close(cfg, label, *args, reason=opts.reason, confirm=opts.confirm)
              else 'already closed')
    else:
        parser.error(f'unknown command {command}')
    return 0
