"""A local stand-in for the model provider (Responses API over SSE).

Every reply is canned, so the real Codex binary runs with no paid request.
A rule (substring, action) applies when the newest input item is a user
message containing the substring:
  ('hold', threading.Event)  keep the turn active until the event is set
  ('call', name, arguments)  answer with one function call
  ('fn', function)           answer with function(number, request), or plainly if it returns None
After a function call's output comes back the reply is plain text.
"""
import http.server
import json
import threading


# Codex asks the model for a thread title in a side request that quotes the prompt.
TITLE_REQUEST = 'Generate a concise, single-line task title'


class MockModel:
    def __init__(self):
        self.requests = []
        self.rules = []
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                data = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                outer.requests.append(data)
                number = len(outer.requests)
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.end_headers()
                item = {'type': 'message', 'role': 'assistant', 'id': f'msg{number}',
                        'content': [{'type': 'output_text', 'text': f'REPLY-{number}'}]}
                last = (data.get('input') or [{}])[-1]
                if last.get('role') == 'user':
                    text = json.dumps(last.get('content'))
                    for needle, action, *rest in outer.rules:
                        if needle not in text or TITLE_REQUEST in text:
                            continue
                        if action == 'hold':
                            self.event({'type': 'response.created', 'response': {'id': str(number)}})
                            rest[0].wait(120)
                        elif action == 'call':
                            item = outer.call(number, rest[0], rest[1])
                        elif action == 'fn':
                            item = rest[0](number, data) or item
                        break
                self.event({'type': 'response.created', 'response': {'id': str(number)}})
                self.event({'type': 'response.output_item.done', 'item': item})
                self.event({'type': 'response.completed', 'response': {
                    'id': str(number), 'usage': {'input_tokens': 1, 'output_tokens': 1, 'total_tokens': 2}}})

            def event(self, payload):
                try:
                    self.wfile.write(('data: ' + json.dumps(payload) + '\n\n').encode())
                    self.wfile.flush()
                except OSError:
                    pass

            def log_message(self, *args):
                pass

        self.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.port = self.server.server_port
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @staticmethod
    def call(number, name, arguments):
        return {'type': 'function_call', 'id': f'fc{number}', 'call_id': f'call{number}',
                'name': name, 'arguments': json.dumps(arguments)}

    def codex_config(self, workdirs, extra=''):
        return (f'model="gpt-6-astra"\nmodel_provider="mock"\n{extra}[model_providers.mock]\nname="offline"\n'
                f'base_url="http://127.0.0.1:{self.port}/v1"\nwire_api="responses"\n'
                f'requires_openai_auth=false\n[features]\nremote_models=false\n'
                f'responses_websockets=false\nresponses_websockets_v2=false\n'
                + ''.join(f'[projects."{workdir}"]\ntrust_level="trusted"\n' for workdir in workdirs))

    def user_texts(self, request):
        """The user message texts of one request, oldest first."""
        return [part.get('text', '') for item in request.get('input', []) if item.get('role') == 'user'
                for part in item.get('content') or []]

    def close(self):
        self.server.shutdown()
        self.server.server_close()
