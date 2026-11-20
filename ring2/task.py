"""This role's task: tracker issues assigned to the role label and in progress."""
import json
import subprocess


def mine(cfg):
    if not cfg.get('tracker_dir'):
        return []
    out = subprocess.run(
        ['bd', 'list', '--assignee', cfg['role'], '--status', 'in_progress', '--json'],
        cwd=cfg['tracker_dir'], capture_output=True, text=True, check=True).stdout
    return [{'id': issue['id'], 'title': issue['title']} for issue in json.loads(out or '[]')]
