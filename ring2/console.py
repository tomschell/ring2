"""Console: one read-only page showing the roles and the latest bus messages.

  python3 -m ring2.console --config FILE [--port 8780]

Optional: nothing else in ring2 imports this file. It serves on 127.0.0.1
only. Each request reads `status` (ring2.app.status) and the bus afresh; the
page reloads itself every few seconds. No actions, no forms, no sign-in, no
script, nothing stored. To view it from another machine, forward the port
(for example with ssh -L); it must not be bound to a public address as it is.
"""
import argparse
import hashlib
import html
import re
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from ring2 import app, bus

PAGE = '''<!doctype html><html><head><meta charset="utf-8"><meta http-equiv="refresh" content="{refresh}">
<title>ring2</title><style>:root{{color-scheme:dark}}body{{font:14px monospace;margin:2em}}td,th{{padding:2px 12px;text-align:left}}
.dead{{color:#ff6b6b;font-weight:bold}}</style></head><body>
<h1>ring2</h1><p>{now}</p>
<table><tr><th>role</th><th>provider / model</th><th>session</th><th>delivery</th><th>task</th><th>last activity</th><th>waiting</th></tr>
{rows}</table>
<h2>bus</h2><table><tr><th>sequence</th><th>time</th><th>channel</th><th>sender</th><th>message</th></tr>{messages}</table>{problem}</body></html>'''


def alive(flag):
    return '<td class="alive">alive</td>' if flag else '<td class="dead">dead</td>'


def colored_name(name):
    hue = int.from_bytes(hashlib.sha256(name.encode()).digest()[:4], "big") % 360
    return f'<span style="color:hsl({hue},65%,70%)">{html.escape(name)}</span>' if name else ""


def colored_message(text):
    parts, end = [], 0
    for match in re.finditer(r'(?<![\w@])@([A-Za-z0-9_-]+)(?![\w-])', text):
        parts.append(html.escape(text[end:match.start()]))
        parts.append(colored_name(match[1]).replace('>', '>@', 1))
        end = match.end()
    parts.append(html.escape(text[end:]))
    return ''.join(parts)


def page(cfg, refresh=5):
    now, problem, rows, messages = time.time(), '', [], []
    try:
        for row in app.status(cfg):
            entry = cfg.get('roles', {}).get(row['label'], {})
            provider_model = ' '.join(str(entry[key]) for key in ('provider', 'model') if entry.get(key))
            age = f'{int(now - row["active"])} s ago ({row["active_source"]})' if 'active' in row else 'unknown'
            outside = row.get('external')
            heard = html.escape(app.heard_text(row)) if outside else ''
            rows.append(f'<tr id="{html.escape(row["label"])}"><td>{colored_name(row["label"])} ({html.escape(str(row["role"]))})</td>'
                        f'<td>{html.escape(provider_model)}</td>'
                        f'{f"<td>{heard}</td>" * 2 if outside else alive(row["session"]) + alive(row["loop"])}<td>{html.escape(" ".join(row.get("tasks", [])) or "-")}</td>'
                        f'<td>{"-" if outside else age}</td><td>{app.waiting(row)}</td></tr>')
        if cfg.get('ring_url') and cfg.get('channels'):
            for message in reversed(bus.events(cfg, 0, wait=0)[-20:]):
                sender, separator, text = message['message'].partition(': ')
                if not separator or not re.fullmatch(r'[A-Za-z0-9_-]+', sender):
                    sender, text = '', message['message']
                messages.append(f'<tr><td>{message["sequence"]}</td><td>{html.escape(message["created_at"])}</td>'
                                f'<td>{html.escape(message["channel"])}</td><td>{colored_name(sender)}</td><td>{colored_message(text)}</td></tr>')
    except (OSError, SystemExit) as exc:
        problem = f'<p class="dead">could not read: {html.escape(str(exc))}</p>'
    return PAGE.format(refresh=refresh, now=time.strftime('%Y-%m-%d %H:%M:%S %Z', time.localtime(now)),
                       rows='\n'.join(rows), messages='\n'.join(messages), problem=problem)


def server(cfg, port):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path != '/':
                self.send_error(404)
                return
            body = page(cfg).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass
    return ThreadingHTTPServer(('127.0.0.1', port), Handler)


def main(argv=None):
    parser = argparse.ArgumentParser(prog='ring2.console', description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--config', required=True)
    parser.add_argument('--port', type=int, default=8780)
    opts = parser.parse_args(argv)
    print(f'ring2 console on http://127.0.0.1:{opts.port}/', flush=True)
    server(app.load(opts.config), opts.port).serve_forever()


if __name__ == '__main__':
    raise SystemExit(main())
