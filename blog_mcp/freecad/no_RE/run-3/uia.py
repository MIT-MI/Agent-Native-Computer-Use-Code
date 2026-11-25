"""Input/output driver for the CUA window runtime.

All interaction with the application goes through the cua-input socket
(see `cua-input --help`): screenshots, pointer clicks, typed text and keys.
Nothing else reaches the display.
"""
import base64
import io
import json
import socket
import time

import numpy as np
from PIL import Image

SOCKET_PATH = '/opt/cua-world-runtime/input.sock'


class InputError(RuntimeError):
    pass


def _call(tool, **args):
    payload = json.dumps({'tool': tool, 'arguments': args}).encode() + b'\n'
    with socket.socket(socket.AF_UNIX) as sock:
        sock.settimeout(60)
        sock.connect(SOCKET_PATH)
        stream = sock.makefile('rwb')
        stream.write(payload)
        stream.flush()
        line = stream.readline()
    if not line:
        raise InputError('cua-input returned nothing for %s' % tool)
    return json.loads(line)


def screenshot(path=None):
    reply = _call('screenshot')
    if 'image' not in reply:
        raise InputError('screenshot failed: %r' % (reply,))
    data = base64.b64decode(reply['image'])
    img = Image.open(io.BytesIO(data)).convert('RGB')
    if path:
        img.save(path)
    return img


def gray():
    return np.array(screenshot().convert('L'))


def click(x, y, pause=0.35):
    _call('click', x=int(x), y=int(y))
    time.sleep(pause)


def double_click(x, y, pause=0.35):
    _call('double_click', x=int(x), y=int(y))
    time.sleep(pause)


def right_click(x, y, pause=0.35):
    _call('right_click', x=int(x), y=int(y))
    time.sleep(pause)


def drag(x1, y1, x2, y2, pause=0.35):
    _call('drag', x1=int(x1), y1=int(y1), x2=int(x2), y2=int(y2))
    time.sleep(pause)


def type_text(text, pause=0.25):
    _call('type_text', text=text)
    time.sleep(pause)


def key(*keys, pause=0.25):
    _call('key', keys=[str(k) for k in keys])
    time.sleep(pause)


def scroll(direction, count=3, pause=0.25):
    _call('scroll', direction=direction, count=int(count))
    time.sleep(pause)
