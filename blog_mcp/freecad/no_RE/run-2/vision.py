#!/usr/bin/env python3
"""Screen reading: capture the desktop, find the FreeCAD window, OCR text.

The application draws its interface with DejaVu Sans (the system default sans
font).  Text is recognised by rendering candidate strings with Pillow's FreeType
backend at the same pixel size and comparing the resulting ink masks with the
ink on screen, searching over sub-pixel placements.  Sizes in this UI are 12 px
(panels, tree, dialogs, spreadsheet) and 13 px (menu bar).
"""
import base64
import io
import json
import socket

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view
from PIL import Image, ImageDraw, ImageFont

SOCKET = '/opt/cua-world-runtime/input.sock'
SS = 8  # supersampling factor used when rendering templates
FONT_REGULAR = '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf'
FONT_BOLD = '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf'
ASCII = ''.join(chr(c) for c in range(32, 127))
NUMCHARS = '0123456789.,-+eE'


# --------------------------------------------------------------------------
# raw input channel
# --------------------------------------------------------------------------
def call(tool, **args):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(30)
    s.connect(SOCKET)
    s.sendall((json.dumps({'tool': tool, 'arguments': args}) + '\n').encode())
    data = b''
    while not data.endswith(b'\n'):
        chunk = s.recv(1 << 20)
        if not chunk:
            break
        data += chunk
    s.close()
    return json.loads(data)


def grab():
    """Return the desktop as a PIL RGB image."""
    r = call('screenshot')
    return Image.open(io.BytesIO(base64.b64decode(r['image']))).convert('RGB')


def click(x, y):
    return call('click', x=int(x), y=int(y))


def right_click(x, y):
    return call('right_click', x=int(x), y=int(y))


def double_click(x, y):
    return call('double_click', x=int(x), y=int(y))


def drag(x1, y1, x2, y2):
    return call('drag', x1=int(x1), y1=int(y1), x2=int(x2), y2=int(y2))


def type_text(t):
    return call('type_text', text=t)


def key(*keys):
    return call('key', keys=list(keys))


def scroll(direction, count):
    return call('scroll', direction=direction, count=int(count))


# --------------------------------------------------------------------------
# window detection
# --------------------------------------------------------------------------
def window_box(img=None, dark=150):
    """Bounding box of the application window, in screen pixels."""
    if img is None:
        img = grab()
    g = np.asarray(img.convert('L'))
    small = (g > dark)[::4, ::4]
    H, W = small.shape
    lab = np.zeros(small.shape, np.int32)
    cur = 0
    best = (0, None)
    for y0 in range(H):
        for x0 in range(W):
            if small[y0, x0] and lab[y0, x0] == 0:
                cur += 1
                stack = [(y0, x0)]
                lab[y0, x0] = cur
                n = 0
                ymin = ymax = y0
                xmin = xmax = x0
                while stack:
                    y, x = stack.pop()
                    n += 1
                    ymin = min(ymin, y); ymax = max(ymax, y)
                    xmin = min(xmin, x); xmax = max(xmax, x)
                    for dy, dx in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                        ny, nx = y + dy, x + dx
                        if 0 <= ny < H and 0 <= nx < W and small[ny, nx] and lab[ny, nx] == 0:
                            lab[ny, nx] = cur
                            stack.append((ny, nx))
                if n > best[0]:
                    best = (n, (xmin * 4, ymin * 4, (xmax + 1) * 4, (ymax + 1) * 4))
    return best[1]


# --------------------------------------------------------------------------
# OCR
# --------------------------------------------------------------------------
_tmpl_cache = {}
THR = 128


def _tight(m):
    rows = np.where(m.any(axis=1))[0]
    cols = np.where(m.any(axis=0))[0]
    if len(rows) == 0:
        return None
    return m[rows.min():rows.max() + 1, cols.min():cols.max() + 1]


def _render(ch, size, font=FONT_REGULAR):
    """Ink masks of `ch`, tight-cropped, as a list of (mask, h, w).

    Two renderings are produced for every glyph: one drawn straight at the
    nominal pixel size (which matches the grid-fitted stems of the toolkit) and
    a set of supersampled sub-pixel placements (which matches glyphs the
    toolkit positions at a fractional offset).
    """
    key = (ch, size, font)
    if key in _tmpl_cache:
        return _tmpl_cache[key]
    out = []
    for s in (size - 1, size, size + 1):
        f = ImageFont.truetype(font, s)
        w = int(len(ch) * s * 1.7) + 24
        im = Image.new('L', (max(w, 24), s * 3 + 24), 255)
        ImageDraw.Draw(im).text((10, 10), ch, font=f, fill=0)
        m = _tight(np.asarray(im) < THR)
        if m is not None:
            out.append((m, m.shape[0], m.shape[1]))
    f = ImageFont.truetype(font, size * SS)
    pad = 6
    w = int(len(ch) * size * SS * 1.7) + 2 * pad * SS
    im = Image.new('L', (max(w, size * SS), size * SS * 2 + 2 * pad * SS), 255)
    ImageDraw.Draw(im).text((pad * SS, pad * SS), ch, font=f, fill=0)
    a = np.asarray(im).astype(np.float32)
    step = 2 if len(ch) > 4 else SS       # long strings need fewer sub-pixel phases
    for oy in range(0, SS, step):
        for ox in range(0, SS, step):
            m = _tight(a[oy::SS, ox::SS] < THR)
            if m is not None:
                out.append((m, m.shape[0], m.shape[1]))
    _tmpl_cache[key] = out
    return out


def _iou_best(obs, tmpl, rad=2):
    """Best intersection-over-union of two masks over small shifts.

    The observed mask is padded with background so that a template which only
    partly overlaps it cannot score highly by falling off the edge.
    """
    H, W = tmpl.shape
    padded = np.zeros((obs.shape[0] + 2 * rad, obs.shape[1] + 2 * rad), bool)
    padded[rad:rad + obs.shape[0], rad:rad + obs.shape[1]] = obs
    if H > padded.shape[0] or W > padded.shape[1]:
        return 0.0
    win = sliding_window_view(padded, (H, W))
    inter = np.logical_and(win, tmpl).sum(axis=(2, 3))
    union = np.logical_or(win, tmpl).sum(axis=(2, 3))
    return float((inter / np.maximum(union, 1)).max())


def _segments(ink):
    """Column runs of ink: list of (x0, x1)."""
    colink = ink.any(axis=0)
    out = []
    x = 0
    W = len(colink)
    while x < W:
        if colink[x]:
            x0 = x
            while x < W and colink[x]:
                x += 1
            out.append((x0, x))
        else:
            x += 1
    return out


def _row_bands(ink):
    """Split an ink mask into text lines (bands separated by >=2 blank rows)."""
    rowink = ink.any(axis=1)
    bands = []
    y = 0
    H = len(rowink)
    while y < H:
        if rowink[y]:
            y0 = y
            blank = 0
            while y < H and blank < 2:
                if rowink[y]:
                    blank = 0
                else:
                    blank += 1
                y += 1
            bands.append((y0, y - blank))
        else:
            y += 1
    return bands


def _read_line(a, ink, size, font, chars, tmpls):
    out = []
    prev_end = None
    for (g0, g1) in _segments(ink):
        if prev_end is not None:
            gap = g0 - prev_end
            if gap >= max(4, int(round(size * 0.38))):
                out.append(' ' * max(1, int(round(gap / (size * 0.5)))))
        prev_end = g1
        sub = ink[:, g0:g1]
        r = np.where(sub.any(axis=1))[0]
        sub = sub[r.min():r.max() + 1]
        h, w = sub.shape
        best = (0.0, '?')
        for ch in chars:
            for (m, th, tw) in tmpls[ch]:
                if abs(th - h) > 1 or abs(tw - w) > 1:
                    continue
                v = _iou_best(sub, m)
                if v > best[0]:
                    best = (v, ch)
        out.append(best[1])
    return ''.join(out)


def read_text(img, box, size=12, font=FONT_REGULAR, candidates=None):
    """Recognise the text inside `box` = (x1, y1, x2, y2) in screen pixels.

    Multiple text lines are separated by newlines.  Returns '' when the region
    holds no ink.
    """
    x1, y1, x2, y2 = box
    a = np.asarray(img.convert('L').crop((int(x1), int(y1), int(x2), int(y2)))).astype(np.float32)
    if a.size == 0:
        return ''
    ink = a < THR
    if not ink.any():
        return ''
    chars = list(candidates) if candidates is not None else list(ASCII)
    tmpls = {ch: _render(ch, size, font) for ch in chars}
    lines = []
    for (b0, b1) in _row_bands(ink):
        lines.append(_read_line(a[b0:b1], ink[b0:b1], size, font, chars, tmpls))
    return '\n'.join(lines)


def read_text_scored(img, box, size=12, font=FONT_REGULAR, candidates=None):
    """Like read_text but also returns the mean per-glyph score."""
    txt = read_text(img, box, size=size, font=font, candidates=candidates)
    return txt


def read_number(img, box, size=12):
    """Recognise a numeric value; returns (float_or_None, raw_text)."""
    txt = read_text(img, box, size=size, candidates=NUMCHARS)
    t = txt.replace(',', '').replace(' ', '')
    try:
        return float(t), txt
    except ValueError:
        return None, txt


def contains_text(img, box, text, sizes=(11, 12, 13), font=FONT_REGULAR):
    """Best ink overlap of `text` against the pixels inside `box` (0..1)."""
    obs = np.asarray(img.convert('L').crop(tuple(map(int, box)))).astype(np.float32)
    if obs.size == 0:
        return -2.0
    m = obs < THR
    rows = np.where(m.any(axis=1))[0]
    cols = np.where(m.any(axis=0))[0]
    if len(rows) == 0:
        return -2.0
    m = m[rows.min():rows.max() + 1, cols.min():cols.max() + 1]
    best = 0.0
    for s in sizes:
        for (t, h, w) in _render(text, s, font):
            best = max(best, _iou_best(m, t, rad=3))
    return best
