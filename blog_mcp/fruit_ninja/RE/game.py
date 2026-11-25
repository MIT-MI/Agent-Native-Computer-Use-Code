"""Fruit Ninja HD live game access: objects, score, state, and input.

All game facts are read from the running process's memory (read-only).
Input is injected through X11 XTest (the game's own mouse input path).

Coordinate systems
------------------
World coordinates: the game world is orthographic, origin at screen centre,
x right, y UP. Visible range roughly x in [-240, 240], y in [-180, 180].
Screen coordinates: pixels, origin top-left, x right, y DOWN, 1024x768.
Transform (exact, from the game's projection matrix):
    sx = 512 + 2.133333 * wx
    sy = 384 - 2.133333 * wy
"""
import struct, time, ctypes, os
from fnmem import get

# ---- fixed global addresses (inside the exe image / .data; stable across restarts) ----
MGR = 0x5bfaf0            # entity manager (fruit/bomb pools + active lists)
GAME_PTR = 0x5b2b94       # pointer to Game object
SCORE = 0x5b308c          # live round score (int)
MODE = 0x5b3074           # game mode (0=classic, 1=zen, 2=arcade)
WAVEMGR_PTR = 0x5b30c0    # pointer to wave manager
TYPE_TABLE_PTR = 0x5b2adc # pointer to fruit-type table
TYPE_COUNT = 0x5b2ad8     # number of fruit types
PROJ = 0x577384           # projection matrix

STRIDE = 0x2dc
BOMB_OFF = 0x19c
NSLOTS = 30

# fruit object field offsets
F_FLAGS = 0x0c
F_X = 0x10
F_Y = 0x14
F_Z = 0x18
F_VX = 0x1c
F_VY = 0x20
F_VZ = 0x24
F_TYPE = 0x3c
F_SLICED = 0xb8

SCALE = 2.1333333

def w2sx(wx):
    return 512.0 + SCALE * wx

def w2sy(wy):
    return 384.0 - SCALE * wy

def s2wx(sx):
    return (sx - 512.0) / SCALE

def s2wy(sy):
    return (384.0 - sy) / SCALE


class XInput:
    """Low-latency mouse injection via XTest."""
    def __init__(self, display=b':7'):
        self.xtst = ctypes.CDLL('libXtst.so.6')
        self.x11 = ctypes.CDLL('libX11.so.6')
        self.dpy = self.x11.XOpenDisplay(display)
        if not self.dpy:
            raise RuntimeError('cannot open display')

    def move(self, x, y):
        self.xtst.XTestFakeMotionEvent(self.dpy, -1, int(x), int(y), 0)

    def button(self, down):
        self.xtst.XTestFakeButtonEvent(self.dpy, 1, bool(down), 0)

    def flush(self):
        self.x11.XFlush(self.dpy)

    def slash(self, x1, y1, x2, y2, steps=10, step_delay=0.010):
        """A single straight slash from (x1,y1) to (x2,y2), screen pixels."""
        self.move(x1, y1)
        self.flush()
        self.button(True)
        self.flush()
        for i in range(1, steps + 1):
            self.move(x1 + (x2 - x1) * i / steps, y1 + (y2 - y1) * i / steps)
            self.flush()
            if step_delay:
                time.sleep(step_delay)
        self.button(False)
        self.flush()

    def slash_path(self, points, step_delay=0.010):
        """Slash through a list of (x,y) screen points."""
        if len(points) < 2:
            return
        self.move(points[0][0], points[0][1])
        self.flush()
        self.button(True)
        self.flush()
        for p in points[1:]:
            self.move(p[0], p[1])
            self.flush()
            if step_delay:
                time.sleep(step_delay)
        self.button(False)
        self.flush()


class Game:
    def __init__(self):
        self.m = get()
        self.x = XInput()
        self._type_table = None

    # ---------- memory helpers ----------
    def pool_base(self):
        return self.m.u32(MGR + 8)

    def type_table(self):
        addr = self.m.u32(TYPE_TABLE_PTR)
        n = self.m.u32(TYPE_COUNT)
        if self._type_table is None or self._type_table[0] != addr:
            d = self.m.read(addr, n * 0x338)
            types = []
            for i in range(n):
                e = d[i * 0x338:(i + 1) * 0x338]
                name = e[0x278:0x278 + 16].split(b'\0')[0].decode('latin1', 'replace')
                radius = struct.unpack('<f', e[0x244:0x248])[0]
                score = struct.unpack('<i', e[0x314:0x318])[0]
                special = e[0x330]
                types.append({'name': name, 'radius': radius, 'score': score, 'special': special})
            self._type_table = (addr, types)
        return self._type_table[1]

    # ---------- state ----------
    def read_objects(self):
        """Read all fruit and bomb pool slots. Returns (fruits, bombs) lists of dicts."""
        pool = self.pool_base()
        reqs = [(pool + i * STRIDE, 0x120) for i in range(NSLOTS)]
        reqs += [(pool + BOMB_OFF + i * STRIDE, 0x120) for i in range(NSLOTS)]
        data = self.m.read_batch(reqs)
        types = self.type_table()

        def parse(d, is_bomb, slot):
            flags = struct.unpack('<I', d[F_FLAGS:F_FLAGS + 4])[0]
            if flags & 0x11:
                return None
            x, y, z = struct.unpack('<3f', d[F_X:F_X + 12])
            vx, vy, vz = struct.unpack('<3f', d[F_VX:F_VX + 12])
            sliced = d[F_SLICED]
            o = {'slot': slot, 'x': x, 'y': y, 'vx': vx, 'vy': vy,
                 'sx': w2sx(x), 'sy': w2sy(y), 'sliced': sliced, 'bomb': is_bomb}
            if is_bomb:
                o['kind'] = 'bomb'
                o['radius'] = 55.0
            else:
                t = d[F_TYPE]
                o['type'] = t
                ti = types[t] if 0 <= t < len(types) else None
                o['kind'] = ti['name'] if ti else 'fruit'
                o['radius'] = ti['radius'] if ti else 60.0
                o['score'] = ti['score'] if ti else 1
                o['special'] = bool(ti['special']) if ti else False
            return o

        fruits = [o for i, d in enumerate(data[:NSLOTS]) if (o := parse(d, False, i))]
        bombs = [o for i, d in enumerate(data[NSLOTS:]) if (o := parse(d, True, i))]
        return fruits, bombs

    def score(self):
        return self.m.i32(SCORE)

    def mode(self):
        return self.m.u32(MODE)

    def wavemgr(self):
        return self.m.u32(WAVEMGR_PTR)

    def state(self):
        fruits, bombs = self.read_objects()
        return {
            'mode': self.mode(),
            'score': self.score(),
            'fruits': fruits,
            'bombs': bombs,
        }


# ---- screen detection from pool objects ----
# signatures: (kind, approx sx, approx sy, tolerance)
SCREEN_SIGS = {
    'game_over': [('sml_ap', 341, 631, 60)],
    'main_menu': [('sml_wm', 546, 525, 60), ('sml_mn', 205, 523, 60)],
    'mode_select': [('sml_wm', 363, 233, 60), ('sml_ap', 700, 282, 60), ('sml_bn', 512, 546, 60)],
}

MENU_ITEMS = {
    'main_new_game': ('sml_wm', 546, 525),
    'main_dojo': ('sml_mn', 205, 523),
    'mode_classic': ('sml_wm', 363, 233),
    'mode_zen': ('sml_ap', 700, 282),
    'mode_arcade': ('sml_bn', 512, 546),
    'gameover_retry': ('sml_ap', 341, 631),
}


def detect_screen(fruits, bombs):
    def has_still(kind, sx, sy, tol):
        # a STATIONARY menu fruit of this kind near (sx,sy)
        for f in fruits:
            if f['sliced']:
                continue
            if f['kind'] == kind and abs(f['sx'] - sx) < tol and abs(f['sy'] - sy) < tol \
                    and abs(f['vx']) < 0.5 and abs(f['vy']) < 0.5:
                return True
        return False
    # game over: the RETRY apple and QUIT bomb at their spots (any velocity; a real
    # round essentially never has an apple AND a bomb parked at both spots at once)
    apple = any((not f['sliced']) and f['kind'] == 'sml_ap' and abs(f['sx'] - 341) < 70 and abs(f['sy'] - 631) < 70 for f in fruits)
    qbomb = any((not b['sliced']) and abs(b['sx'] - 683) < 90 and abs(b['sy'] - 631) < 90 for b in bombs)
    if apple and qbomb:
        return 'game_over'
    for screen in ('main_menu', 'mode_select'):
        if all(has_still(*s) for s in SCREEN_SIGS[screen]):
            return screen
    # a real round has moving objects (flying fruit/bombs)
    for f in fruits:
        if not f['sliced'] and (abs(f['vx']) > 0.5 or abs(f['vy']) > 0.5):
            return 'round'
    for b in bombs:
        if not b['sliced'] and (abs(b['vx']) > 0.5 or abs(b['vy']) > 0.5):
            return 'round'
    return 'unknown'


def find_menu_fruit(fruits, item):
    kind, sx, sy = MENU_ITEMS[item]
    best = None
    bd = 1e9
    for f in fruits:
        if f['sliced'] or f['kind'] != kind:
            continue
        d = (f['sx'] - sx) ** 2 + (f['sy'] - sy) ** 2
        if d < bd:
            bd = d
            best = f
    return best
