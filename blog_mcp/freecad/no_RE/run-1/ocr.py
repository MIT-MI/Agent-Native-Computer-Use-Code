"""OCR for FreeCAD's Qt UI.

Glyph samples were captured from FreeCAD itself (see templates/samples.npz), so
characters are recognised by comparing an unknown glyph bitmap against the real
rendered samples rather than by re-rendering a font.  Each character has several
samples at different sub-pixel positions.
"""
import os
import numpy as np
from PIL import Image

_DIR = os.path.dirname(os.path.abspath(__file__))
SAMPLE_FILE = os.path.join(_DIR, 'templates', 'samples.npz')
_SAMPLES = None


def samples():
    global _SAMPLES
    if _SAMPLES is None:
        data = np.load(SAMPLE_FILE)
        d = {}
        for k in data.files:
            ch = k.split('|')[0]
            if ch == '':
                continue
            d.setdefault(ch, []).append(data[k].astype(np.float32))
        _SAMPLES = d
    return _SAMPLES


def tight(a, th=0.15):
    ys, xs = np.where(a > th)
    if len(xs) == 0:
        return None
    return a[ys.min():ys.max() + 1, xs.min():xs.max() + 1]


def segment(arr, th=0.35):
    ink = (arr > th).sum(axis=0)
    segs, start = [], None
    for x, c in enumerate(ink):
        if c > 0 and start is None:
            start = x
        elif c == 0 and start is not None:
            segs.append((start, x)); start = None
    if start is not None:
        segs.append((start, len(ink)))
    return segs


CANVAS = (22, 22)
_BIG = None
_LAB = None
_DIM = None


def _pad_native(a, size=CANVAS):
    if a.shape[0] > size[0]:
        a = a[:size[0]]
    if a.shape[1] > size[1]:
        a = a[:, :size[1]]
    p = np.zeros(size, np.float32)
    oy = (size[0] - a.shape[0]) // 2
    ox = (size[1] - a.shape[1]) // 2
    p[oy:oy + a.shape[0], ox:ox + a.shape[1]] = a
    return p


def _signature(a):
    return (a.shape, tuple(np.round(a, 1).flatten()[:60].tolist()))


def _build():
    global _BIG, _LAB, _DIM
    if _BIG is not None:
        return
    seen, labels, arrs, dims = set(), [], [], []
    for c, lst in samples().items():
        for a in lst:
            k = (c,) + _signature(a)
            if k in seen:
                continue
            seen.add(k)
            labels.append(c)
            arrs.append(_pad_native(a))
            dims.append(a.shape)
    _BIG = np.stack(arrs)
    _LAB = np.array(labels)
    _DIM = np.array(dims, np.float32)


def classify_scored(sub):
    _build()
    t = tight(sub, 0.3)
    if t is None:
        return '?', 9.9
    n = _pad_native(t)
    penalty = (np.abs(_DIM[:, 0] - t.shape[0]) + np.abs(_DIM[:, 1] - t.shape[1])) * 0.012
    nmask = n > 0.15
    best, bi = 1e9, 0
    for dy in (-1, 0, 1):
        for dx in (-1, 0, 1):
            Bs = np.roll(np.roll(_BIG, dy, axis=1), dx, axis=2)
            diff2 = (Bs - n[None]) ** 2
            union = ((Bs > 0.15) | nmask[None]).sum(axis=(1, 2))
            d = diff2.sum(axis=(1, 2)) / np.maximum(union, 1) + penalty
            i = int(np.argmin(d))
            if d[i] < best:
                best, bi = float(d[i]), i
    return str(_LAB[bi]), best


def classify(sub):
    return classify_scored(sub)[0]


def read_text(arr, th=0.35, space_gap=None):
    """Read one line of text from a grayscale ink array (ink = high)."""
    segs = segment(arr, th)
    if not segs:
        return ''
    gaps = [segs[i + 1][0] - segs[i][1] for i in range(len(segs) - 1)]
    if space_gap is None:
        med = sorted(gaps)[len(gaps) // 2] if gaps else 1
        space_gap = max(3, med * 2)
    out = []
    for i, (x0, x1) in enumerate(segs):
        if i > 0 and (x0 - segs[i - 1][1]) >= space_gap:
            out.append(' ')
        out.append(classify(arr[:, x0:x1]))
    return ''.join(out)


def text_rows(arr, th=0.35):
    """Return (y0, y1) bands of consecutive ink rows within a region."""
    ink = (arr > th).sum(axis=1)
    rows, start = [], None
    for y, c in enumerate(ink):
        if c > 0 and start is None:
            start = y
        elif c == 0 and start is not None:
            rows.append((start, y)); start = None
    if start is not None:
        rows.append((start, len(ink)))
    return rows


def read_image(im, box, th=0.35):
    """OCR a box=(x0,y0,x1,y1) of a PIL image."""
    arr = 1.0 - np.asarray(im.crop(box).convert('L')).astype(np.float32) / 255.0
    return read_text(arr, th)
