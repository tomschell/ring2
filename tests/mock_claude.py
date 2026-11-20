"""Local Messages API stand-in for the real Claude Code CLI; never calls a provider."""
import http.server
import json
import threading


class MockClaude:
    def __init__(self):
        self.requests, self.rules = [], []
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                data = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                if 'count_tokens' in self.path:
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(b'{"input_tokens": 10}')
                    return
                outer.requests.append(data)
                number = len(outer.requests)
                content = [{'type': 'text', 'text': f'REPLY-{number}'}]
                last = data['messages'][-1]
                if last['role'] == 'user' and not any(p.get('type') == 'tool_result' for p in last['content'] if isinstance(p, dict)):
                    for needle, action, value in outer.rules:
                        if needle not in json.dumps(last['content']):
                            continue
                        if action == 'call':
                            content = [outer.call(number, value)]
                        elif action == 'fn':
                            content = [value(number, data)]
                        elif action == 'hold':
                            content = value
                        break
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.end_headers()
                self.event('message_start', {'message': {'id': f'msg_{number}', 'type': 'message',
                    'role': 'assistant', 'model': data['model'], 'content': [], 'stop_reason': None,
                    'stop_sequence': None, 'usage': {'input_tokens': 10, 'output_tokens': 0}}})
                if isinstance(content, threading.Event):
                    while not content.wait(0.2):
                        if not self.event('ping', {}):
                            return
                    content = [{'type': 'text', 'text': 'released'}]
                for i, block in enumerate(content):
                    initial = {**block, 'input': {}} if block['type'] == 'tool_use' else {**block, 'text': ''}
                    self.event('content_block_start', {'index': i, 'content_block': initial})
                    delta = ({'type': 'input_json_delta', 'partial_json': json.dumps(block['input'])}
                             if block['type'] == 'tool_use' else {'type': 'text_delta', 'text': block['text']})
                    self.event('content_block_delta', {'index': i, 'delta': delta})
                    self.event('content_block_stop', {'index': i})
                self.event('message_delta', {'delta': {'stop_reason': 'tool_use' if content[0]['type'] == 'tool_use' else 'end_turn',
                                                      'stop_sequence': None}, 'usage': {'output_tokens': 1}})
                self.event('message_stop', {})

            def event(self, kind, payload):
                try:
                    self.wfile.write(f'event: {kind}\ndata: {json.dumps({"type": kind, **payload})}\n\n'.encode())
                    self.wfile.flush()
                    return True
                except OSError:
                    return False

            def log_message(self, *args):
                pass

        self.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.port = self.server.server_port
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @staticmethod
    def call(number, command):
        return {'type': 'tool_use', 'id': f'tool_{number}', 'name': 'Bash', 'input': {'command': command}}

    def env(self, config_dir):
        return {'ANTHROPIC_BASE_URL': f'http://127.0.0.1:{self.port}', 'ANTHROPIC_API_KEY': 'offline-test-key',
                'CLAUDE_CONFIG_DIR': str(config_dir), 'CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC': '1',
                'CLAUDE_CODE_DISABLE_BACKGROUND_TASKS': '1'}

    def close(self):
        self.server.shutdown()
        self.server.server_close()
