"""Thin client for the cua-input socket (the only way the MCP may touch the window).

The socket speaks one JSON request/response per connection:
    {"tool": NAME, "arguments": {...}}
Coordinates are absolute pixels in the 1280x800 desktop.  `mousemove --sync`
inside the runtime blocks ~15 s when the pointer is already at the target, so
`click` nudges the pointer first whenever the target equals the previous one.
"""
import base64
import json
import socket
import time

SOCK = '/opt/cua-world-runtime/input.sock'
TIMEOUT = 30
_last = [None]


def call(tool, **arguments):
    s = socket.socket(socket.AF_UNIX)
    s.settimeout(TIMEOUT)
    s.connect(SOCK)
    f = s.makefile('rwb')
    f.write(json.dumps({'tool': tool, 'arguments': arguments}).encode() + b'\n')
    f.flush()
    line = f.readline()
    s.close()
    if not line:
        raise RuntimeError('cua-input returned nothing for %s' % tool)
    return json.loads(line)


def screenshot(path):
    r = call('screenshot')
    if 'image' not in r:
        raise RuntimeError('screenshot failed: %r' % (r,))
    with open(path, 'wb') as fh:
        fh.write(base64.b64decode(r['image']))
    return path


def _nudge(x, y):
    # Avoid the runtime's 15 s wait when the pointer is already at (x, y).
    if _last[0] == (x, y):
        call('click', x=min(1279, x + 1), y=y)
    _last[0] = (x, y)


def click(x, y):
    _nudge(int(x), int(y))
    return call('click', x=int(x), y=int(y))


def right_click(x, y):
    _nudge(int(x), int(y))
    return call('right_click', x=int(x), y=int(y))


def double_click(x, y):
    _nudge(int(x), int(y))
    return call('double_click', x=int(x), y=int(y))


def drag(x1, y1, x2, y2):
    _last[0] = None
    return call('drag', x1=int(x1), y1=int(y1), x2=int(x2), y2=int(y2))


def type_text(text):
    return call('type_text', text=text)


def key(*keys):
    return call('key', keys=list(keys))


def scroll(direction, count):
    return call('scroll', direction=direction, count=int(count))


def wait(t=0.4):
    time.sleep(t)
