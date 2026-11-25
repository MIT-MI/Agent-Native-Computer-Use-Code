"""Read text that the application paints on screen.

The application is a Qt program using DejaVu Sans at 12 px for its widgets.
Glyphs are rendered from the same font file and matched against binarised
screen crops; each connected ink blob is scored by ink-bounding-box IoU so that
per-glyph rounding of the advance width never accumulates error.

Accuracy is good for digits and ordinary words but not perfect: similar shapes
(c/e, o/p, i/l/1) are occasionally confused, so callers should treat the
returned text as an observation, not as an authoritative value.  A per-line
`conf` score is returned (1.0 = perfect overlap).
"""
import json
import os

import numpy as np
from PIL import Image, ImageDraw, ImageFont

FONT_PATH = '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf'
FONT_PATH_BOLD = '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf'

ALL_CHARS = (" !\"#$%&'()*+,-./0123456789:;<=>?@"
             "ABCDEFGHIJKLMNOPQRSTUVWXYZ[\\]^_`"
             "abcdefghijklmnopqrstuvwxyz{|}~")
DIGITS = '0123456789.-+ '
WORDS = 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-. &()[]'

CANVAS_H = 24
BASELINE = 17


def ink_bbox(m):
    rows = np.where(m.any(axis=1))[0]
    cols = np.where(m.any(axis=0))[0]
    if len(rows) == 0 or len(cols) == 0:
        return None
    return m[rows[0]:rows[-1] + 1, cols[0]:cols[-1] + 1]


def _up(m, up):
    if up == 1:
        return m
    im = Image.fromarray((m.astype(np.uint8) * 255))
    im = im.resize((m.shape[1] * up, m.shape[0] * up), Image.BILINEAR)
    return np.array(im) > 127


def _iou_fine(a, b, up=2, shift=2):
    """IoU after upsampling, so sub-pixel rendering phases still line up."""
    A = _up(a, up)
    B = _up(b, up)
    ah, aw = A.shape
    bh, bw = B.shape
    H = max(ah, bh) + 2 * shift
    W = max(aw, bw) + 2 * shift
    P = np.zeros((H, W), bool)
    P[shift:shift + ah, shift:shift + aw] = A
    best = 0.0
    for dy in range(0, 2 * shift + 1):
        for dx in range(0, 2 * shift + 1):
            Q = np.zeros((H, W), bool)
            Q[dy:dy + bh, dx:dx + bw] = B
            union = (P | Q).sum()
            if union == 0:
                continue
            v = (P & Q).sum() / union
            if v > best:
                best = v
    return best


def _iou(a, b, shift=1):
    ah, aw = a.shape
    bh, bw = b.shape
    H = max(ah, bh) + 2 * shift
    W = max(aw, bw) + 2 * shift
    A = np.zeros((H, W), bool)
    A[shift:shift + ah, shift:shift + aw] = a
    best = 0.0
    for dy in range(0, 2 * shift + 1):
        for dx in range(0, 2 * shift + 1):
            B = np.zeros((H, W), bool)
            B[dy:dy + bh, dx:dx + bw] = b
            union = (A | B).sum()
            if union == 0:
                continue
            v = (A & B).sum() / union
            if v > best:
                best = v
    return best


def ink_mask(rgb, thr=150, sat=None):
    """Dark pixels of a light-on-dark UI.

    `sat` (if given) additionally rejects pixels whose channel spread is above
    it, which removes the coloured object icons FreeCAD draws in the model tree
    while keeping the grey text."""
    a = rgb.astype(np.int16)
    mx = a.max(axis=2)
    m = mx < thr
    if sat is not None:
        m = m & ((mx - a.min(axis=2)) < sat)
    return m


class Reader:
    def __init__(self, size=12, bold=False):
        """Glyph templates in both weights: the application paints some values
        with the bold face, and the extra variant makes matching unambiguous."""
        self.size = size
        self.font = ImageFont.truetype(FONT_PATH, size)
        self.templates = {}
        self.adv = {}
        self.by_width = {}
        self.variants = {}
        for weight, path in (('regular', FONT_PATH), ('bold', FONT_PATH_BOLD)):
            font = ImageFont.truetype(path, size)
            for ch in ALL_CHARS:
                w = max(2, int(round(font.getlength(ch))))
                img = Image.new('L', (w + 10, CANVAS_H), 255)
                ImageDraw.Draw(img).text((5, BASELINE), ch, font=font, fill=0, anchor='ls')
                m = np.array(img) < 150
                bb = ink_bbox(m)
                if bb is None:
                    bb = np.zeros((1, 1), bool)
                self.variants.setdefault(ch, []).append(bb)
                if weight == 'regular':
                    self.templates[ch] = bb
                    self.adv[ch] = w
                    self.by_width.setdefault(bb.shape[1], []).append(ch)
        self._load_app_glyphs()

    def _load_app_glyphs(self):
        """Add glyphs harvested from the application itself, if present.

        The application hints its text: a 12 px '3' is drawn with a flat top
        that no PIL rendering of DejaVu Sans reproduces, so the digits read
        from the running program are stored alongside the font templates.
        """
        here = os.path.dirname(os.path.abspath(__file__))
        for name in sorted(os.listdir(here)) if os.path.isdir(here) else []:
            if not (name.startswith('glyphs_') and name.endswith('.json')):
                continue
            try:
                data = json.load(open(os.path.join(here, name)))
            except Exception:
                continue
            for ch, bm in data.items():
                arr = np.array(bm, bool)
                if arr.size:
                    self.variants.setdefault(ch, []).append(arr)

    # ---------------------------------------------------------------- helpers
    def _candidates(self, width, charset, tol=2):
        cs = charset.replace(' ', '')
        allowed = set(cs)
        out = set()
        for ch in allowed:
            for t in self.variants.get(ch, []):
                if abs(t.shape[1] - width) <= tol:
                    out.add(ch)
                    break
        if not out:
            out = set(cs)
        return sorted(out)

    def _best(self, blob, charset):
        best = (0.0, ' ')
        for ch in self._candidates(blob.shape[1], charset):
            for t in self.variants.get(ch, [self.templates[ch]]):
                v = _iou_fine(blob, t)
                if v > best[0]:
                    best = (v, ch)
        return best

    def _dp(self, line, charset):
        H, W = line.shape
        INF = 1e18
        dp = np.full(W + 1, INF)
        back = [None] * (W + 1)
        dp[0] = 0.0
        for x in range(W):
            if dp[x] >= INF:
                continue
            for ch in charset:
                base = self.adv[ch]
                for w in (base - 1, base, base + 1):
                    if w < 2:
                        continue
                    nx = x + w
                    if nx > W:
                        continue
                    seg = line[:, x:nx]
                    sb = ink_bbox(seg)
                    sc = 0.0 if sb is None else _iou(sb, self.templates[ch])
                    v = dp[x] + (1.0 - sc) * w + 0.2
                    if v < dp[nx] - 1e-9:
                        dp[nx] = v
                        back[nx] = (x, ch, sc)
        if dp[W] >= INF:
            return '', 0.0
        out, scores = [], []
        x = W
        while x > 0:
            px, ch, sc = back[x]
            out.append(ch)
            scores.append(sc)
            x = px
        out.reverse()
        return ''.join(out), float(np.mean(scores)) if scores else 0.0

    # ------------------------------------------------------------------- API
    def read_line(self, ink, charset=None, max_height=None):
        charset = charset or ALL_CHARS
        if not ink.any():
            return '', 1.0
        cols = np.where(ink.any(axis=0))[0]
        x0, x1 = int(cols[0]), int(cols[-1]) + 1
        line = ink[:, x0:x1]
        prof = line.any(axis=0)
        W = line.shape[1]
        blobs = []
        x = 0
        while x < W:
            if prof[x]:
                s0 = x
                while x < W and prof[x]:
                    x += 1
                blobs.append([s0, x])
            else:
                x += 1
        merged = []
        for b in blobs:
            if merged and b[0] <= merged[-1][1]:
                merged[-1][1] = b[1]
            else:
                merged.append(b)
        maxadv = max(self.adv[c] for c in charset if c != ' ')
        texts, confs = [], []
        for (b0, b1) in merged:
            seg = line[:, b0:b1]
            sb = ink_bbox(seg)
            if sb is None:
                continue
            if max_height is not None and sb.shape[0] > max_height:
                continue
            if b1 - b0 <= maxadv + 3:
                v, ch = self._best(sb, charset)
                texts.append(ch)
                confs.append(v)
            else:
                t, c = self._dp(seg, charset)
                texts.append(t)
                confs.append(c)
        return ''.join(texts), (float(np.mean(confs)) if confs else 0.0)

    def read_region(self, rgb, charset=None, split_gap=6, thr=150, sat=None, max_height=None):
        """rgb: HxWx3 uint8.  Returns list of dicts with text and boxes.

        Text rows are found from the darkest pixels, so rows painted with a
        selection highlight (white on blue) are located too and read inverted.
        """
        a = rgb.astype(np.int16)
        gray = a.mean(axis=2)
        blue_sel = (a[:, :, 2] - a[:, :, 0] > 40) & (a[:, :, 2] > 90)
        ink_dark = ink_mask(rgb, thr, sat)
        ink_light = blue_sel & (gray > 165)
        blue_rows = blue_sel.sum(axis=1) > 30
        bright_rows = (gray > 165).any(axis=1)
        textrows = (gray < 120).any(axis=1) | (blue_rows & bright_rows)
        # join rows that belong to the same text line (dots of i/j, descenders)
        bands = []
        y = 0
        H = len(textrows)
        while y < H:
            if textrows[y]:
                y0 = y
                gap = 0
                while y < H and gap <= 2:
                    if textrows[y]:
                        gap = 0
                        y += 1
                    else:
                        gap += 1
                        y += 1
                bands.append((y0, min(H, y - gap)))
            else:
                y += 1
        out = []
        for (y0, y1) in bands:
            row_gray = gray[y0:y1]
            row_blue = blue_sel[y0:y1].mean() > 0.4
            bg = float(np.percentile(row_gray, 85))
            if row_blue:
                band = row_gray > 165
            elif bg < 175:
                # row painted with a (grey) highlight: text is darker than the fill
                band = row_gray < max(60.0, bg - 35.0)
            else:
                band = ink_dark[y0:y1]
            xs = np.where(band.any(axis=0))[0]
            if len(xs) == 0:
                continue
            groups = []
            start = xs[0]
            prev = xs[0]
            for x in xs[1:]:
                if x - prev >= split_gap:
                    groups.append((start, prev + 1))
                    start = x
                prev = x
            groups.append((start, prev + 1))
            for gx0, gx1 in groups:
                txt, conf = self.read_line(band[:, gx0:gx1], charset, max_height=max_height)
                out.append({'text': txt, 'conf': round(conf, 3),
                            'x0': int(gx0), 'x1': int(gx1), 'y0': int(y0), 'y1': int(y1)})
        return out

    def read_lines(self, rgb, charset=None, gap=6, thr=150, sat=None, max_height=None):
        """One entry per detected text line, top to bottom."""
        rows = self.read_region(rgb, charset=charset, split_gap=gap, thr=thr, sat=sat,
                                max_height=max_height)
        lines = {}
        for r in rows:
            lines.setdefault(r['y0'] // 5, []).append(r)
        out = []
        for key in sorted(lines):
            parts = sorted(lines[key], key=lambda r: r['x0'])
            out.append({'y0': min(p['y0'] for p in parts),
                        'y1': max(p['y1'] for p in parts),
                        'x0': min(p['x0'] for p in parts),
                        'text': ' '.join(p['text'] for p in parts),
                        'conf': round(float(np.mean([p['conf'] for p in parts])), 3)})
        return out
