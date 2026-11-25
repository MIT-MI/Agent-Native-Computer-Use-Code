#!/usr/bin/python3
"""cua-input: see and operate the desktop's window without access to the display itself.

  cua-input screenshot [FILE]          save a PNG of the desktop (default: screenshot.png)
  cua-input click X Y                  also right_click, double_click
  cua-input drag X1 Y1 X2 Y2
  cua-input type_text TEXT
  cua-input key KEY [KEY ...]          keys pressed together, e.g. ctrl s, or Escape
  cua-input scroll up|down|left|right COUNT

Text that looks like a script or code is refused and not typed. That holds for the line being
entered, however it arrives: in one type_text, in several, or key by key. A line ends at Enter.
Lock, latch, dead and compose keys are not passed on, because they change what later keys type.

Each call prints one JSON object. A program may also use the socket directly: send one JSON line
{"tool": NAME, "arguments": {...}} to /opt/cua-world-runtime/input.sock and read one JSON line; the
tools and arguments are the ones above (x, y; x1, y1, x2, y2; text; keys; direction, count), and
a screenshot answers {"image": <base64 PNG>, "mimeType": "image/png"}.
"""
import base64
import json
import os
from pathlib import Path
import re
import socket
import socketserver
import subprocess
import sys
import threading

SOCKET = '/opt/cua-world-runtime/input.sock'
REFUSED = '/opt/cua-world-runtime/input-refused.jsonl'
ARGUMENTS = {'screenshot': (), 'click': ('x', 'y'), 'right_click': ('x', 'y'), 'double_click': ('x', 'y'),
             'drag': ('x1', 'y1', 'x2', 'y2'), 'type_text': ('text',), 'key': ('keys',), 'scroll': ('direction', 'count')}


# What key symbols type. Letters and digits are their own names.
PUNCTUATION = dict(zip('space exclam quotedbl numbersign dollar percent ampersand apostrophe parenleft parenright asterisk plus '
                       'comma minus period slash colon semicolon less equal greater question at bracketleft backslash '
                       'bracketright asciicircum underscore grave braceleft bar braceright asciitilde'.split(),
                       ' !"#$%&\'()*+,-./:;<=>?@[\\]^_`{|}~'))
TYPED = {**PUNCTUATION, 'quoteright': "'", 'quoteleft': '`', 'Tab': '\t', 'KP_Tab': '\t', 'ISO_Left_Tab': '\t',
         'KP_Space': ' ', 'KP_Add': '+', 'KP_Subtract': '-', 'KP_Multiply': '*', 'KP_Divide': '/', 'KP_Decimal': '.',
         'KP_Separator': ',', 'KP_Equal': '=', **{'KP_%d' % digit: str(digit) for digit in range(10)}}
ENTER = {'Return', 'KP_Enter', 'ISO_Enter', 'Linefeed'}
# xdotool's own names for keys, which it matches without regard to case.
ALIASES = {'alt': 'Alt_L', 'ctrl': 'Control_L', 'control': 'Control_L', 'meta': 'Meta_L', 'super': 'Super_L',
           'shift': 'Shift_L', 'enter': 'Return', 'return': 'Return'}
SHIFT, LEVELS = {'Shift_L', 'Shift_R'}, {'Mode_switch': 2, 'ISO_Level3_Shift': 4}
COMMAND = ('Control_', 'Alt_', 'Meta_', 'Super_', 'Hyper_')
STATEFUL = re.compile(r'_Lock$|Latch|Group|^dead_|^Multi_key$')
LOCKED = 'Lock, latch, dead and compose keys are not passed on: they change what later keys type.'
LINE = 512  # characters of the line being entered that are kept; every pattern of the detector is far shorter


def character(symbol):
    """The character a key symbol types, if it types one."""
    if len(symbol) == 1: return symbol
    if symbol in TYPED: return TYPED[symbol]
    code = re.fullmatch(r'(?:U\+?|0x)([0-9A-Fa-f]{2,8})', symbol)
    if not code: return None
    value = int(code[1], 16)
    if symbol.startswith('0x') and value >= 0x01000000: value -= 0x01000000  # the X form of a Unicode key symbol
    return chr(value) if 0x20 <= value < 0x110000 and value != 0x7f else None


def parse_layout(text):
    """`xmodmap -pke` as (key code -> symbols by level, symbol -> (key code, level))."""
    codes, symbols = {}, {}
    for line in text.splitlines():
        match = re.match(r'keycode\s+(\d+)\s*=(.*)', line)
        if not match: continue
        codes[int(match[1])] = match[2].split()
        for level, symbol in enumerate(codes[int(match[1])]): symbols.setdefault(symbol, (int(match[1]), level))
    return codes, symbols


def entered(keys, layout):
    """What a chord of keys types on the desktop's keyboard layout: (text, whether it ends the line).

    keys are key symbol names or key codes, as xdotool takes them. Shift and the other level keys pick
    the symbol a key gives; a chord with Control, Alt or Super is a shortcut and types nothing.
    """
    codes, symbols = layout
    names = [ALIASES.get(key.lower(), key) for key in keys]
    if any(STATEFUL.search(name) for name in names): raise ValueError(LOCKED)
    if any(name.startswith(COMMAND) for name in names): return '', False
    level = max([LEVELS.get(name, 0) for name in names]) + bool(SHIFT & set(names))
    text, ends = '', False
    for name in names:
        if name in SHIFT or name in LEVELS: continue
        symbol = name
        if name.isdigit() and len(name) > 1: row = codes.get(int(name), [])  # a key code
        else: row = codes[symbols[name][0]] if name in symbols and symbols[name][1] == 0 else None
        if row is not None:
            # A level the key does not have falls back to its shifted or plain symbol.
            choice = [row[index] for index in (level, level % 2, 0) if index < len(row) and row[index] != 'NoSymbol']
            symbol = choice[0] if choice else ''
        if symbol in ENTER: ends = True
        else: text += character(symbol) or ''
    return text, ends


def request(value):
    with socket.socket(socket.AF_UNIX) as client:
        client.connect(SOCKET)
        stream = client.makefile('rwb')
        stream.write(json.dumps(value).encode() + b'\n'); stream.flush()
        return json.loads(stream.readline())


class Display:
    """The root side: the GUI tools' own translation to X input, and their typed-code filter.

    The filter sees what is being entered rather than one call. `line` is the text typed and the
    characters keyed since the last Enter; `run` is its part since the pointer or cursor last moved,
    because what follows such a move may be entered somewhere else. An input is refused if it looks
    like code by itself, or would make the run or the line look like code.
    """
    def __init__(self, environment, resolution):
        self.environment, self.resolution = environment, resolution
        self.line, self.run, self.keyboard = '', '', None

    def layout(self):
        if self.keyboard is None:
            self.keyboard = parse_layout(subprocess.run(['xmodmap', '-pke'], env=self.environment, capture_output=True,
                                                        text=True, check=True).stdout)
        return self.keyboard

    def capture_screenshot(self):
        return subprocess.run(['import', '-window', 'root', 'png:-'], env=self.environment, capture_output=True, check=True).stdout

    def inject_action(self, action):
        from gui import action_commands
        for command in action_commands(action, self.resolution):
            subprocess.run(['/bin/sh', '-c', command], env=self.environment, capture_output=True, check=True)

    def call(self, tool, arguments):
        from code_guard import REJECTED, looks_like_code
        from gui import GUI, KEYS
        if tool not in ARGUMENTS or set(arguments) != set(ARGUMENTS[tool]): raise ValueError('usage: see cua-input --help')
        if tool == 'screenshot':
            return {'image': base64.b64encode(self.capture_screenshot()).decode(), 'mimeType': 'image/png'}
        text, ends, keys = '', False, []
        if tool == 'type_text': text = arguments['text']
        elif tool == 'key':
            keys = [KEYS.get(key.upper(), key) for key in arguments['keys']]
            text, ends = entered(keys, self.layout())
        if text and any(looks_like_code(before + text) for before in ('', self.run, self.line)):
            with open(REFUSED, 'a') as log: log.write(json.dumps({'tool': tool, 'arguments': arguments, 'line': self.line + text}) + '\n')
            return {'executed': False, 'error': REJECTED}
        result = GUI(self, None).tools()[tool](**arguments)
        if ends: self.line = self.run = ''
        elif text: self.line, self.run = (self.line + text)[-LINE:], (self.run + text)[-LINE:]
        elif keys == ['BackSpace'] and self.run: self.line, self.run = self.line[:-1], self.run[:-1]
        else: self.run = ''
        return {'executed': result['executed']}


def serve(config, resolution, group):
    import grp
    spec = json.loads(Path(config).read_text())
    environment = {**spec['environment'], 'PATH': '/usr/local/bin:/usr/bin:/bin'}
    display = Display(environment, resolution)
    lock = threading.Lock()
    class Handler(socketserver.StreamRequestHandler):
        def handle(self):
            try:
                value = json.loads(self.rfile.readline())
                with lock: result = display.call(value['tool'], value.get('arguments', {}))
            except Exception as exc: result = {'error': type(exc).__name__ + ': ' + str(exc)}
            self.wfile.write(json.dumps(result).encode() + b'\n')
    Path(SOCKET).unlink(missing_ok=True)
    server = socketserver.ThreadingUnixStreamServer(SOCKET, Handler)
    os.chown(SOCKET, 0, grp.getgrnam(group).gr_gid); os.chmod(SOCKET, 0o660)
    Path(REFUSED).touch(); os.chmod(REFUSED, 0o600)
    print('CUA_WORLD_INPUT_READY', flush=True)
    server.serve_forever()


def arguments(tool, words):
    if tool == 'key': return {'keys': words}
    names = ARGUMENTS[tool]
    if len(words) != len(names): raise SystemExit(__doc__)
    return {name: word if name in ('text', 'direction') else int(word) for name, word in zip(names, words)}


def main(argv):
    if not argv or argv[0] in ('-h', '--help') or argv[0] not in (*ARGUMENTS, 'serve'): raise SystemExit(__doc__)
    if argv[0] == 'serve':
        serve(argv[1], [int(v) for v in argv[2].split('x')], argv[3]); return
    if argv[0] == 'screenshot':
        path = argv[1] if len(argv) > 1 else 'screenshot.png'
        response = request({'tool': 'screenshot', 'arguments': {}})
        if 'image' in response:
            Path(path).write_bytes(base64.b64decode(response['image']))
            response = {'saved': path, 'mimeType': response['mimeType']}
    else: response = request({'tool': argv[0], 'arguments': arguments(argv[0], argv[1:])})
    print(json.dumps(response))
    if 'error' in response: raise SystemExit(1)


if __name__ == '__main__': main(sys.argv[1:])
