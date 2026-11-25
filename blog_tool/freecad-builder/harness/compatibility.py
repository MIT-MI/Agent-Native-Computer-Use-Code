"""Model-free check that every Builder tool works on this software's desktop.

It calls the Builder's own tools with a tiny hand-written MCP and records what
each returned, as either Builder variant. Observations only; nothing here gates a run.
"""
import json
from builder import Builder
from code_guard import REJECTED
from records import write_json

LAUNCH = '#!/bin/sh\nexec /usr/bin/python3 "$(dirname "$0")/server.py"\n'
SERVER = r'''"""Compatibility probe MCP: application data in, observations out."""
import json, subprocess, sys

TOOLS = [
    {'name': 'freecad_version', 'description': 'Report the FreeCAD version from its own command-line build.',
     'inputSchema': {'type': 'object', 'properties': {}, 'additionalProperties': False}},
    {'name': 'save_box', 'description': 'Create a document with one box of the given edge length (mm) and save it to path.',
     'inputSchema': {'type': 'object', 'properties': {'path': {'type': 'string'}, 'length': {'type': 'number'}},
                     'required': ['path', 'length'], 'additionalProperties': False}},
    {'name': 'list_windows', 'description': 'List desktop window titles.',
     'inputSchema': {'type': 'object', 'properties': {}, 'additionalProperties': False}}]

def run(argv):
    row = subprocess.run(argv, capture_output=True, text=True)
    return {'returncode': row.returncode, 'stdout': row.stdout[-4000:], 'stderr': row.stderr[-4000:]}

def call(name, args):
    if name == 'freecad_version':
        return run(['freecadcmd', '-c', 'import FreeCAD; print(FreeCAD.Version())'])
    if name == 'save_box':
        source = ('import FreeCAD\ndoc = FreeCAD.newDocument("Probe")\nbox = doc.addObject("Part::Box", "Box")\n'
                  'box.Length = box.Width = box.Height = ' + json.dumps(float(args['length'])) + '\n'
                  'doc.recompute()\ndoc.saveAs(' + json.dumps(str(args['path'])) + ')\n')
        return run(['freecadcmd', '-c', source])
    if name == 'list_windows':
        return run(['wmctrl', '-l'])
    raise ValueError('unknown tool')

LOOP
'''
# The no-re probe has only what a closed desktop offers: the window and lifecycle helpers.
NO_RE_SERVER = r'''"""Compatibility probe MCP for a closed desktop: application data in, observations out."""
import base64, json, socket, struct, subprocess, sys

TOOLS = [
    {'name': 'application_status', 'description': 'Report whether the application is running.',
     'inputSchema': {'type': 'object', 'properties': {}, 'additionalProperties': False}},
    {'name': 'window_size', 'description': 'Report the size in pixels of the desktop the application shows.',
     'inputSchema': {'type': 'object', 'properties': {}, 'additionalProperties': False}},
    {'name': 'enter_text', 'description': 'Type text into the focused field of the application.',
     'inputSchema': {'type': 'object', 'properties': {'text': {'type': 'string'}}, 'required': ['text'], 'additionalProperties': False}}]

def window(tool, arguments):
    with socket.socket(socket.AF_UNIX) as client:
        client.connect('/opt/cua-world-runtime/input.sock')
        stream = client.makefile('rwb')
        stream.write(json.dumps({'tool': tool, 'arguments': arguments}).encode() + b'\n'); stream.flush()
        return json.loads(stream.readline())

def call(name, args):
    if name == 'application_status':
        row = subprocess.run(['cua-app', 'status'], capture_output=True, text=True)
        return {'returncode': row.returncode, 'stdout': row.stdout[-4000:], 'stderr': row.stderr[-4000:]}
    if name == 'window_size':
        width, height = struct.unpack('>II', base64.b64decode(window('screenshot', {})['image'])[16:24])
        return {'returncode': 0, 'width': width, 'height': height}
    if name == 'enter_text':
        reply = window('type_text', {'text': str(args['text'])})
        return {'returncode': 0 if reply.get('executed') else 1, **reply}
    raise ValueError('unknown tool')

LOOP
'''
LOOP = r'''for line in sys.stdin:
    message = json.loads(line)
    if 'id' not in message: continue
    method, params = message['method'], message.get('params', {})
    try:
        if method == 'initialize':
            result = {'protocolVersion': '2024-11-05', 'capabilities': {'tools': {}},
                      'serverInfo': {'name': 'compatibility-probe', 'version': '1'},
                      'instructions': 'Probe only.'}
        elif method == 'tools/list': result = {'tools': TOOLS}
        elif method == 'tools/call':
            value = call(params['name'], params.get('arguments', {}))
            result = {'content': [{'type': 'text', 'text': json.dumps(value)}], 'isError': value['returncode'] != 0}
        else: raise ValueError('unknown method')
        reply = {'jsonrpc': '2.0', 'id': message['id'], 'result': result}
    except Exception as exc:
        reply = {'jsonrpc': '2.0', 'id': message['id'], 'error': {'code': -32000, 'message': str(exc)}}
    print(json.dumps(reply), flush=True)
'''


def brief(value):
    """Keep reports readable: drop image bytes, keep everything else."""
    if isinstance(value, dict):
        return {k: ('<%d base64 chars>' % len(v) if k == 'image' else brief(v)) for k, v in value.items()}
    if isinstance(value, list): return [brief(v) for v in value]
    return value


def run(desktop, folder, budgets, profile='re', ocr=False):
    builder = Builder(desktop, folder, '', budgets, profile=profile, ocr=ocr)
    builder.schema_request = lambda tools: 'Not requested: this check makes no model calls.'
    home = desktop.home
    if builder.variant == 're':
        server, look = SERVER, [
            ('exec', {'command': 'id; freecadcmd --version 2>&1 | head -2; ls /opt/cua-world-reference /opt/cua-world-reference/source; '
                      'kuna --help 2>&1 | head -1; java -version 2>&1 | head -1; rizin -v | head -1; frida --version; '
                      'gdb --version | head -1; atspi-tree | head -c 300; apptool pid; grep ^CapAmb /proc/self/status', 'wait_seconds': 120}),
            ('exec', {'command': 'sleep 30; echo late', 'wait_seconds': 1}),
            ('read_file', {'path': '/opt/cua-world-reference/index.json'})]
        calls = [{'name': 'freecad_version', 'arguments': {}},
                 {'name': 'save_box', 'arguments': {'path': home + '/compatibility-box.FCStd', 'length': 12}},
                 {'name': 'list_windows', 'arguments': {}}]
    else:
        server, look = NO_RE_SERVER, [
            ('exec', {'command': 'id; python3 -c "import numpy, PIL; print(numpy.__version__, PIL.__version__)"; cua-app status; '
                      'cua-input screenshot /tmp/look.png; ls ' + home + ' /opt/cua-world-reference; command -v gdb strings; '
                      'echo note > /output/intermediate/notes.txt', 'wait_seconds': 120}),
            ('exec', {'command': 'sleep 30; echo late', 'wait_seconds': 1}),
            ('read_file', {'path': '/tmp/look.png'}),
            ('type_text', {'text': 'import os'})]
        calls = [{'name': 'application_status', 'arguments': {}}, {'name': 'window_size', 'arguments': {}},
                 {'name': 'enter_text', 'arguments': {'text': 'import os'}}, {'name': 'enter_text', 'arguments': {'text': '1'}}]
    if ocr: look.append(('exec', {'command': 'tesseract --version 2>&1 | head -1; python3 -c "import tesserocr; print(tesserocr.tesseract_version().splitlines()[0])"', 'wait_seconds': 60}))
    steps = [
        *look,
        ('screenshot', {}),
        ('click', {'x': 640, 'y': 400}),
        ('key', {'keys': ['Escape']}),
        ('type_text', {'text': 'x'}),
        ('scroll', {'direction': 'down', 'count': 0}),
        ('write_file', {'path': '/output/mcp/launch.sh', 'content': LAUNCH}),
        ('write_file', {'path': '/output/mcp/server.py', 'content': server.replace('LOOP\n', LOOP)}),
        ('exec', {'command': 'chmod +x /output/mcp/launch.sh', 'wait_seconds': 10}),
        ('try', {'restart': True, 'wait_seconds': 300, 'calls': calls}),
        ('screenshot', {}),
        ('finish', {'notes': 'model-free compatibility check'}),  # with the whole budget left: asks to confirm
        ('finish', {'notes': 'model-free compatibility check'}),
    ]
    rows = []
    for name, args in steps:
        try: result = builder.invoke(name, args); error = None
        except Exception as exc: result, error = None, type(exc).__name__ + ': ' + str(exc)
        rows.append({'tool': name, 'arguments': args, 'error': error, 'result': brief(result)})
        write_json(folder / 'compatibility.json', rows)
    trial = next(r['result'] for r in rows if r['tool'] == 'try')
    calls = {c['call']['name']: c['result'] for c in (trial or {}).get('calls', [])}
    def text(name):
        content = (calls.get(name) or {}).get('content') or [{}]
        return content[0].get('text', '')
    finish = rows[-1]['result'] or {}
    report = json.loads(open(trial['report']).read()) if trial and trial.get('report') else {}
    summary = {
        'variant': builder.variant, 'ocr': ocr, 'identity': (rows[0]['result'] or {}).get('head', '').split('\n')[0],
        'tools_raised': [r['tool'] for r in rows if r['error']],
        'mcp_started': bool(trial and trial.get('startup')),
        'mcp_tools': [t['name'] for t in (report.get('tool_list') or {}).get('tools', [])],
        'call_errors': [c['call'] for c in (trial or {}).get('calls', []) if not isinstance(c['result'], dict) or c['result'].get('isError') or c['result'].get('error')],
        **({'saved_file_seen': any('compatibility-box.FCStd' in path for path in (trial or {}).get('changed_after_calls') or []),
            'freecad_window_listed': 'FreeCAD' in text('list_windows')} if builder.variant == 're' else
           {'window_seen_through_helper': text('window_size'),
            'typed_code_refused': [r['result'] for r in rows if r['tool'] == 'type_text' and r['result'] and r['result'].get('error') == REJECTED] != []}),
        'restart_errors': [e for e in (trial or {}).get('errors', []) if 'restart' in e.lower()],
        'background_exec_kept_running': rows[1]['result'] and rows[1]['result'].get('finished') is False,
        'delivered_from': finish.get('delivered_from'),
        'first_finish_asked_to_confirm': (rows[-2]['result'] or {}).get('accepted') is False,
        'routes': (trial or {}).get('routes'),
        'not_exercised': ['review and schema requests (they call the model)'],
    }
    write_json(folder / 'compatibility-summary.json', summary)
    return {'summary': summary, 'report': str(folder / 'compatibility.json')}
