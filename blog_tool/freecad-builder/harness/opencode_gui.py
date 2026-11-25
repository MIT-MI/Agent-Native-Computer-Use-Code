"""GUI solve with the agent of the earlier FreeCAD GUI rounds (Kimi K2.6).

That agent was OpenCode with its built-in shell, file, search, web and sub-agent tools removed and all
permissions denied except the screenshot desktop tools, normalized to a 0-1000 grid, with 1000 actions
(src/evaluation/desktop.ts and harbor/agents/reef_opencode.py at tag freecad-showcase-builder-2).
The tools act on this harness's solve desktop. OpenCode reaches the model through a local relay
that adds the key and records every request, so the key never reaches OpenCode.
"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import base64
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time

import requests

# The version the earlier rounds pinned (agent-package-lock.json), fetched once and checked by integrity.
PACKAGE = {'name': 'opencode-linux-x64', 'version': '1.19.4',
           'resolved': 'https://registry.npmjs.org/opencode-linux-x64/-/opencode-linux-x64-1.19.4.tgz',
           'integrity': 'sha512-rQ/l3jmJoxf8ZYnBargg9alURlz6buEilysGODtzkcmcYtcZ91LOgF034oUUHHQl8/y1hbRTDvg6ujqUjQsMXw=='}
CACHE = Path.home() / '.cache/freecad-builder-opencode'
OPENCODE = CACHE / 'opencode-1.19.4/package/bin/opencode'
MAX_ACTIONS = 1000
OUTPUT_LIMIT = 32768
CONTEXT_LIMIT = 200000
BUILTIN = ['bash', 'edit', 'write', 'read', 'grep', 'glob', 'list', 'patch', 'webfetch', 'websearch',
           'codesearch', 'task', 'todowrite', 'todoread', 'lsp', 'skill']
UNIT = (" Coordinates are normalized: x and y run from 0 to 1000 across the screenshot's width and height,"
        " with (0,0) at the top-left.")
GRID = {'type': 'integer', 'minimum': 0, 'maximum': 1000}
MODIFIERS = {'type': 'array', 'items': {'type': 'string', 'enum': ['ctrl', 'shift', 'alt', 'super']},
             'maxItems': 4, 'default': []}


def schema(properties, required=()):
    return {'type': 'object', 'properties': properties, 'required': list(required), 'additionalProperties': False}


TOOLS = [
    {'name': 'screenshot', 'description': 'Observe the actual desktop. Use its pixels to choose coordinates. '
     'No OCR, DOM or application state is exposed.' + UNIT, 'inputSchema': schema({})},
    {'name': 'click', 'description': 'Click a visible screen coordinate and observe the resulting screenshot. Supports '
     'double-click (count=2) and held ctrl/shift/alt/super modifiers. Modifiers are released after the click.' + UNIT,
     'inputSchema': schema({'x': GRID, 'y': GRID,
                            'button': {'type': 'string', 'enum': ['left', 'middle', 'right'], 'default': 'left'},
                            'count': {'type': 'integer', 'minimum': 1, 'maximum': 2, 'default': 1},
                            'modifiers': MODIFIERS}, ['x', 'y'])},
    {'name': 'type_text', 'description': 'Type ordinary document or form text into the focused UI control. Never enter '
     'scripts, code, console commands or developer-console payloads.',
     'inputSchema': schema({'text': {'type': 'string', 'minLength': 1, 'maxLength': 6000}}, ['text'])},
    {'name': 'key_press', 'description': 'Press a keyboard shortcut in the application, such as ctrl+s, ctrl+Home, '
     'shift+End or Return. The result includes a screenshot.',
     'inputSchema': schema({'keys': {'type': 'string', 'pattern': '^[A-Za-z0-9_+]+$', 'maxLength': 100}}, ['keys'])},
    {'name': 'scroll', 'description': 'Scroll the visible UI at the pointer position and observe it.',
     'inputSchema': schema({'direction': {'type': 'string', 'enum': ['up', 'down']},
                            'amount': {'type': 'integer', 'minimum': 1, 'maximum': 12, 'default': 3}}, ['direction'])},
    {'name': 'drag', 'description': 'Drag between visible screen coordinates and observe the final screenshot.' + UNIT,
     'inputSchema': schema({'from_x': GRID, 'from_y': GRID, 'to_x': GRID, 'to_y': GRID, 'modifiers': MODIFIERS},
                           ['from_x', 'from_y', 'to_x', 'to_y'])},
    {'name': 'wait_for_screen', 'description': 'Wait briefly for an in-progress UI operation, then take another screenshot.',
     'inputSchema': schema({'milliseconds': {'type': 'integer', 'minimum': 100, 'maximum': 3000, 'default': 500}})},
]
ALIASES = {'ctrl': 'ctrl', 'control': 'ctrl', 'control_l': 'ctrl', 'shift': 'shift', 'shift_l': 'shift', 'alt': 'alt',
           'alt_l': 'alt', 'super': 'super', 'meta': 'super', 'enter': 'Return', 'return': 'Return', 'escape': 'Escape',
           'esc': 'Escape', 'home': 'Home', 'end': 'End', 'left': 'Left', 'arrowleft': 'Left', 'right': 'Right',
           'arrowright': 'Right', 'up': 'Up', 'arrowup': 'Up', 'down': 'Down', 'arrowdown': 'Down', 'tab': 'Tab',
           'space': 'space', 'backspace': 'BackSpace', 'delete': 'Delete', 'pageup': 'Page_Up', 'pagedown': 'Page_Down',
           'page_up': 'Page_Up', 'page_down': 'Page_Down', 'controlleft': 'ctrl', 'shiftleft': 'shift', 'altleft': 'alt'}


def shortcut(keys):
    """The earlier driver's key names, as a list for xdotool."""
    out = []
    for key in keys.split('+'):
        if key.lower() in ALIASES: out.append(ALIASES[key.lower()])
        elif len(key) == 1 and key.isalnum(): out.append(key.lower())
        elif key[:1] in 'fF' and key[1:].isdigit() and 1 <= int(key[1:]) <= 12: out.append(key.upper())
        else: raise ValueError('Unsupported key ' + key + '; use standard names such as ctrl, shift, ArrowUp, Home, Return or F1')
    return out


def pixel(value, size):
    return min(size - 1, int(math.floor(value * size / 1000 + 0.5)))


def inputs(name, p, resolution):
    """One tool call as this harness's structured GUI actions, and the modifiers to release after it."""
    width, height = resolution
    point = lambda x, y: [pixel(x, width), pixel(y, height)]
    held = p.get('modifiers') or []
    down = [{'keyboard': {'keys_down': held}}] if held else []
    if name == 'click':
        button, count = p.get('button', 'left'), p.get('count', 1)
        if button == 'left' and count == 2: clicks = [{'mouse': {'double_click': point(p['x'], p['y'])}}]
        else: clicks = [{'mouse': {button + '_click': point(p['x'], p['y'])}}] * count
        return down + clicks, held
    if name == 'type_text': return [{'keyboard': {'text': p['text']}}], []
    if name == 'key_press': return [{'keyboard': {'keys': shortcut(p['keys'])}}], []
    if name == 'scroll': return [{'mouse': {'scroll': [p['direction'], p.get('amount', 3)]}}], []
    if name == 'drag':
        return down + [{'mouse': {'left_click_drag': [point(p['from_x'], p['from_y']), point(p['to_x'], p['to_y'])]}}], held
    return [], []


def serve_mcp(interface):
    """Stdio MCP server run by OpenCode; every desktop effect goes through the driver's interface."""
    for line in sys.stdin:
        if not line.strip(): continue
        message = json.loads(line)
        if 'id' not in message: continue
        method, params = message.get('method'), message.get('params') or {}
        if method == 'initialize':
            result = {'protocolVersion': params.get('protocolVersion', '2024-11-05'), 'capabilities': {'tools': {}},
                      'serverInfo': {'name': 'desktop', 'version': '1'}}
        elif method == 'tools/list': result = {'tools': TOOLS}
        elif method == 'tools/call':
            reply = requests.post(interface + '/act', json={'name': params.get('name'), 'arguments': params.get('arguments') or {}},
                                  timeout=120).json()
            if 'error' in reply: result = {'isError': True, 'content': [{'type': 'text', 'text': reply['error']}]}
            else: result = {'content': [{'type': 'text', 'text': json.dumps(reply['meta'])},
                                        {'type': 'image', 'mimeType': 'image/png', 'data': reply['png']}]}
        elif method == 'ping': result = {}
        else:
            sys.stdout.write(json.dumps({'jsonrpc': '2.0', 'id': message['id'],
                                         'error': {'code': -32601, 'message': 'unknown method'}}) + '\n'); sys.stdout.flush()
            continue
        sys.stdout.write(json.dumps({'jsonrpc': '2.0', 'id': message['id'], 'result': result}) + '\n'); sys.stdout.flush()


def config(pin, interface):
    model = pin['model']
    return {
        '$schema': 'https://opencode.ai/config.json',
        'model': 'pinned/' + model, 'small_model': 'pinned/' + model, 'enabled_providers': ['pinned'],
        'provider': {'pinned': {'npm': '@ai-sdk/openai-compatible',
                                'options': {'baseURL': interface + '/v1', 'apiKey': 'held-by-the-driver'},
                                # As the earlier Kimi rounds declared: images reach the model. temperature lets
                                # OpenCode send the pinned value instead of leaving it to the endpoint.
                                'models': {model: {'tool_call': True, 'reasoning': True, 'temperature': True,
                                                   'modalities': {'input': ['text', 'image'], 'output': ['text']},
                                                   'limit': {'context': CONTEXT_LIMIT, 'output': OUTPUT_LIMIT},
                                                   'options': {'reasoningEffort': pin['reasoning_effort']}}}}},
        'agent': {'build': {'temperature': pin['temperature'], 'top_p': pin['top_p']}},
        'permission': {'*': 'deny', **{t: 'deny' for t in BUILTIN}, 'desktop_*': 'allow'},
        'tools': {t: False for t in BUILTIN},
        'mcp': {'desktop': {'type': 'local', 'enabled': True,
                            'command': [sys.executable, str(Path(__file__).resolve()), 'mcp', interface]}},
    }


def opencode_gui_solve(desktop, folder, task, pin, budgets, key):
    """Run the earlier rounds' OpenCode agent on this desktop until it stops or the task deadline passes."""
    from code_guard import REJECTED, looks_like_code
    from gui import GUI
    from records import write_json
    folder = Path(folder); gui = GUI(desktop, folder); start = time.monotonic()
    desktop.deadline = start + budgets['solve_seconds']
    shots = folder / 'desktop' / 'screenshots'; shots.mkdir(parents=True, exist_ok=True)
    state = {'sequence': 0, 'observed': False, 'blocked': [], 'errors': []}
    lock = threading.Lock()

    def act(name, arguments):
        if name not in {t['name'] for t in TOOLS}: raise ValueError('unknown tool ' + str(name))
        if name == 'type_text' and looks_like_code(arguments.get('text')):
            state['blocked'].append(arguments); write_json(folder / 'blocked-code-inputs.json', state['blocked'])
            raise ValueError(REJECTED)
        with lock:
            if state['sequence'] >= MAX_ACTIONS: raise ValueError('Desktop action budget exhausted')
            actions, held = inputs(name, arguments, desktop.resolution)
            if actions and not state['observed']: raise ValueError('Observe the desktop screenshot before acting')
            if name == 'wait_for_screen': time.sleep(arguments.get('milliseconds', 500) / 1000)
            state['sequence'] += 1; number = state['sequence']; started = time.time()
            try:
                if actions: desktop.inject_actions(actions)
            finally:
                if held: desktop.inject_actions([{'keyboard': {'keys_up': held}}])
            if actions: time.sleep(0.12)
            shot = gui.screenshot()
            path = shots / ('%04d.png' % number)
            shutil.copyfile(shot['path'], path); Path(shot['path']).unlink(missing_ok=True)
            with (folder / 'desktop' / 'actions.jsonl').open('a') as log:
                log.write(json.dumps({'id': number, 'action': name, 'parameters': arguments, 'inputs': actions,
                                      'started_at': started, 'duration_seconds': time.time() - started,
                                      'screenshot': str(path)}) + '\n')
            state['observed'] = True
            return {'png': shot['image'], 'meta': {'action_id': number, 'width': desktop.resolution[0],
                                                   'height': desktop.resolution[1], 'coordinates': 'normalized-1000'}}

    def relay(handler, body):
        """Forward the request bytes unchanged with the key added; stream the answer back and record it."""
        row = {'started_at': time.time()}
        request = json.loads(body)
        row['request'] = {k: v for k, v in request.items() if k not in ('messages', 'tools')}
        row['request'].update(messages=len(request.get('messages', [])), tools=len(request.get('tools', [])))
        path = folder / 'model-requests' / ('%06d.json' % next(counter))
        try:
            timeout = min(600, desktop.deadline - time.monotonic())
            if timeout <= 0: raise TimeoutError('solve deadline passed')
            upstream = requests.post(pin['endpoint'] + '/chat/completions', data=body, stream=True, timeout=timeout,
                                     headers={'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json'})
            handler.send_response(upstream.status_code)
            handler.send_header('Content-Type', upstream.headers.get('Content-Type', 'application/json'))
            handler.end_headers(); handler.answered = True
            received = []
            for chunk in upstream.iter_content(None):
                received.append(chunk); handler.wfile.write(chunk); handler.wfile.flush()
            row.update(http_status=upstream.status_code, response=b''.join(received).decode(errors='replace'))
        except Exception as exc:
            row['error'] = str(exc); state['errors'].append('model request: ' + str(exc))
            raise
        finally:
            row['latency_seconds'] = time.time() - row['started_at']
            write_json(path, row)

    import itertools
    counter = itertools.count(1)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def reply(self, data, status=200):
            self.send_response(status); self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(data))); self.end_headers(); self.wfile.write(data)
        def do_POST(self):
            body = self.rfile.read(int(self.headers.get('Content-Length', 0)))
            try:
                if self.path == '/v1/chat/completions':
                    relay(self, body); return
                if self.path != '/act': raise ValueError('unknown interface path')
                request = json.loads(body)
                self.reply(json.dumps(act(request['name'], request['arguments'])).encode())
            except Exception as exc:
                if self.path == '/act': self.reply(json.dumps({'error': str(exc)}).encode()); return
                if not getattr(self, '_headers_buffer', None) and not getattr(self, 'answered', False):
                    try: self.reply(json.dumps({'error': str(exc)}).encode(), 502)
                    except OSError: pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    interface = 'http://127.0.0.1:' + str(server.server_port)
    # OpenCode's home and working folder live outside the evidence, so its prompt names no run path;
    # its session data is copied back afterwards.
    home = Path(tempfile.mkdtemp(prefix='opencode-')); (home / '.config/opencode').mkdir(parents=True)
    (home / 'work').mkdir(); CACHE.mkdir(parents=True, exist_ok=True)
    settings = config(pin, interface)
    (home / '.config/opencode/opencode.json').write_text(json.dumps(settings, indent=2))
    environment = {'HOME': str(home), 'PATH': os.environ.get('PATH', '/usr/bin:/bin'), 'LANG': 'C.UTF-8',
                   'XDG_CACHE_HOME': str(CACHE), 'XDG_DATA_HOME': str(home / 'data'), 'XDG_STATE_HOME': str(home / 'state'),
                   'XDG_CONFIG_HOME': str(home / '.config'), 'OPENCODE_EXPERIMENTAL_OUTPUT_TOKEN_MAX': str(OUTPUT_LIMIT),
                   'OPENCODE_DISABLE_DEFAULT_PLUGINS': 'true', 'OPENCODE_DISABLE_AUTOUPDATE': 'true',
                   'OPENCODE_DISABLE_MODELS_FETCH': 'true'}
    receipt = {'agent': 'opencode', 'opencode': str(OPENCODE), 'opencode_package': PACKAGE, 'config': settings,
               'start': time.time()}
    process = None
    try:
        version = subprocess.run([str(OPENCODE), '--version'], env=environment, capture_output=True, text=True, timeout=60)
        receipt['opencode_version'] = version.stdout.strip()
        if receipt['opencode_version'] != PACKAGE['version']: raise RuntimeError('OpenCode is not the pinned version')
        with (folder / 'opencode.jsonl').open('w') as out, (folder / 'opencode-stderr.log').open('w') as err:
            process = subprocess.Popen([str(OPENCODE), 'run', '--model', 'pinned/' + pin['model'],
                                        '--format', 'json', task['description']], cwd=home / 'work', env=environment,
                                       stdout=out, stderr=err, stdin=subprocess.DEVNULL, start_new_session=True)
            receipt['returncode'] = process.wait(timeout=max(0, desktop.deadline - time.monotonic()))
        receipt['terminal_reason'] = 'returned'
    except subprocess.TimeoutExpired:
        receipt['terminal_reason'] = 'time_budget'
    except Exception as exc:
        state['errors'].append(str(exc)); receipt['terminal_reason'] = 'runtime_error'
    finally:
        if process is not None and process.poll() is None:
            os.killpg(process.pid, 9); process.wait()
        server.shutdown(); server.server_close(); thread.join()
        for name in ('data', 'state', '.config'):
            if (home / name).exists(): shutil.copytree(home / name, folder / 'opencode-home' / name, dirs_exist_ok=True)
        shutil.rmtree(home, ignore_errors=True)
        usage = {'prompt_tokens': 0, 'completion_tokens': 0}; requests_made = 0
        for path in (folder / 'model-requests').glob('*.json'):
            row = json.loads(path.read_text()); requests_made += 1
            for line in (row.get('response') or '').splitlines():
                text = line[5:].strip() if line.startswith('data:') else line.strip()
                if not text.startswith('{'): continue
                try: found = json.loads(text).get('usage') or {}
                except ValueError: continue
                for name in usage: usage[name] += found.get(name) or 0
        receipt.update(steps=state['sequence'], blocked_code_inputs=len(state['blocked']), errors=state['errors'],
                       model_requests=requests_made, tokens=usage, elapsed_seconds=time.monotonic() - start)
        receipt['status'] = 'infrastructure_failure' if receipt['terminal_reason'] == 'runtime_error' or (
            receipt.get('returncode') not in (None, 0)) or requests_made == 0 else 'completed'
        write_json(folder / 'run.json', receipt)
    return receipt


if __name__ == '__main__' and sys.argv[1:2] == ['mcp']:
    serve_mcp(sys.argv[2])
