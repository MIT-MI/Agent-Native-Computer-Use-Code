"""Real-time play controller for Fruit Ninja HD.

Runs a tight observe->decide->slash loop so play quality does not depend on
the caller's latency. All decisions use fresh memory observations.
"""
import time, math
from game import Game, w2sx, w2sy, detect_screen

SY_MIN = 60.0
SY_MAX = 762.0
SX_MIN = 10.0
SX_MAX = 1014.0

VEL_PXS = 160.0         # screen px/s per world velocity unit (measured)
GRAV_PXS2 = 2200.0      # screen px/s^2 downward (gravity, measured)
LEAD_TIME = 0.09        # seconds to lead fruit motion
RADIUS_FACTOR = 0.55    # effective collision radius = type diameter * scale * this
BOMB_MARGIN = 100.0     # extra px kept away from bomb centres
SLASH_COOLDOWN = 0.02   # seconds between slashes


def seg_point_dist(ax, ay, bx, by, px, py):
    dx, dy = bx - ax, by - ay
    L2 = dx * dx + dy * dy
    if L2 == 0:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / L2))
    cx, cy = ax + t * dx, ay + t * dy
    return math.hypot(px - cx, py - cy)


class Controller:
    def __init__(self):
        self.g = Game()
        self.last_slash = 0.0
        self.stats = {'slashes': 0, 'fruits_hit': 0, 'combos': 0}
        self.bomb_margin = BOMB_MARGIN
        self.pending = {}  # slot -> time slashed (awaiting slice confirmation)

    def hittable(self, fruits):
        out = []
        for f in fruits:
            if f['sliced']:
                continue
            sx, sy = f['sx'], f['sy']
            if not (SX_MIN <= sx <= SX_MAX and SY_MIN <= sy <= SY_MAX):
                continue
            out.append(f)
        return out

    def lead(self, f, dt=LEAD_TIME):
        sx = f['sx'] + f['vx'] * VEL_PXS * dt
        sy = f['sy'] - f['vy'] * VEL_PXS * dt + 0.5 * GRAV_PXS2 * dt * dt
        return sx, sy

    def radius_px(self, f):
        return f['radius'] * 2.1333333 * RADIUS_FACTOR

    def seg_safe(self, ax, ay, bx, by, bombs):
        # avoid the pause button (bottom-left corner)
        if seg_point_dist(ax, ay, bx, by, 20.0, 740.0) < 75.0:
            return False
        for b in bombs:
            danger = b['radius'] * 2.1333333 * RADIUS_FACTOR + self.bomb_margin
            # check the bomb's position now and led over the whole slash duration
            for dt in (0.0, LEAD_TIME, 2 * LEAD_TIME):
                bsx = b['sx'] + b['vx'] * VEL_PXS * dt
                bsy = b['sy'] - b['vy'] * VEL_PXS * dt + 0.5 * GRAV_PXS2 * dt * dt
                if seg_point_dist(ax, ay, bx, by, bsx, bsy) <= danger:
                    return False
        return True

    def deadline(self, f):
        """Seconds until the fruit falls below the hittable area (ballistic)."""
        # sy(t) = sy - vy*VEL*t + 0.5*GRAV*t^2 = SY_MAX
        sy = f['sy']
        vy = -f['vy'] * VEL_PXS  # screen px/s down+
        a = 0.5 * GRAV_PXS2
        # solve a t^2 + vy t + (sy - SY_MAX) = 0
        b = vy
        cc = sy - SY_MAX
        disc = b * b - 4 * a * cc
        if disc < 0:
            return 1e9
        t = (-b + math.sqrt(disc)) / (2 * a)
        return t if t > 0 else 0.0

    def urgency(self, f):
        """Higher for fruits about to fall off the bottom."""
        d = self.deadline(f)
        if d > 1.5:
            return 0.0
        return 4.0 / (0.2 + d)

    def group_value(self, group):
        val = sum(g.get('score', 1) for g in group)
        if len(group) >= 3:
            val += 3 * len(group)  # combo bonus (game gives +n; strong incentive)
        elif len(group) == 2:
            val += 0.5
        val += sum(self.urgency(g) for g in group)
        return val

    def best_slash(self, fruits, bombs):
        """Return (ax,ay,bx,by,group) for the best bomb-safe slash, or None."""
        if not fruits:
            return None
        pred = {f['slot']: self.lead(f) for f in fruits}
        # candidate lines: through each fruit at several angles, and through each fruit pair
        cands = []
        for f in fruits:
            cx, cy = pred[f['slot']]
            cands.append((cx - 100.0, cy, cx + 100.0, cy))
            for ang in (35, 70, 110, 145):
                r = math.radians(ang)
                ux, uy = math.cos(r), math.sin(r)
                cands.append((cx - ux * 100.0, cy - uy * 100.0, cx + ux * 100.0, cy + uy * 100.0))
        for i in range(len(fruits)):
            for j in range(i + 1, len(fruits)):
                (x1, y1), (x2, y2) = pred[fruits[i]['slot']], pred[fruits[j]['slot']]
                dx, dy = x2 - x1, y2 - y1
                L = math.hypot(dx, dy) or 1.0
                ux, uy = dx / L, dy / L
                ext = 70.0
                cands.append((x1 - ux * ext, y1 - uy * ext, x2 + ux * ext, y2 + uy * ext))
        best = None
        best_val = -1e9
        for (ax, ay, bx, by) in cands:
            if not self.seg_safe(ax, ay, bx, by, bombs):
                continue
            real_hits = [f for f in fruits
                         if seg_point_dist(ax, ay, bx, by, *pred[f['slot']]) <= self.radius_px(f)]
            if not real_hits:
                continue
            val = self.group_value(real_hits)
            if val > best_val:
                best_val = val
                best = (ax, ay, bx, by, real_hits)
        return best

    def tick(self):
        # state indicator 2 == arcade (bombs only cost points) -> smaller margin;
        # classic/zen (bombs fatal or absent) -> large margin
        self.bomb_margin = 50.0 if self.g.mode() == 2 else 100.0
        fruits, bombs = self.g.read_objects()
        fruits = self.hittable(fruits)
        bombs = [b for b in bombs if not b['sliced']]
        now = time.time()
        # un-pend fruits that are confirmed sliced, or that we slashed a while ago (retry)
        for f in fruits:
            if f['sliced']:
                self.pending.pop(f['slot'], None)
        for slot, t in list(self.pending.items()):
            if now - t > 0.22:
                self.pending.pop(slot, None)
        # un-pend any pending fruit that is now falling fast toward the exit (must retry)
        for f in fruits:
            if f['slot'] in self.pending and not f['sliced'] and self.deadline(f) < 0.35:
                self.pending.pop(f['slot'], None)
        # don't re-target pending fruits unless nothing else
        avail = [f for f in fruits if f['slot'] not in self.pending]
        targets = avail if avail else fruits
        if not targets:
            return 0
        if now - self.last_slash < SLASH_COOLDOWN:
            return 0
        s = self.best_slash(targets, bombs)
        if not s:
            return 0
        ax, ay, bx, by, hits = s
        self.g.x.slash(ax, ay, bx, by, steps=10, step_delay=0.015)
        self.last_slash = now
        for h in hits:
            self.pending[h['slot']] = now
        self.stats['slashes'] += 1
        self.stats['fruits_hit'] += len(hits)
        if len(hits) >= 3:
            self.stats['combos'] += 1
        return len(hits)

    def play(self, seconds, stop_score=None):
        t0 = time.time()
        start_score = self.g.score()
        peak = start_score
        round_active = True
        idle_since = None
        while time.time() - t0 < seconds:
            fruits, bombs = self.g.read_objects()
            scr = detect_screen(fruits, bombs)
            if scr == 'game_over':
                round_active = False
                break
            # backstop: if nothing is moving (no live gameplay) for a while, the round is over
            moving = [f for f in fruits if not f['sliced'] and (abs(f['vx']) > 0.5 or abs(f['vy']) > 0.5)]
            moving += [b for b in bombs if not b['sliced'] and (abs(b['vx']) > 0.5 or abs(b['vy']) > 0.5)]
            if moving:
                idle_since = None
            else:
                if idle_since is None:
                    idle_since = time.time()
                elif time.time() - idle_since > 2.0:
                    round_active = False
                    break
            self.tick()
            sc = self.g.score()
            if sc > peak:
                peak = sc
            if stop_score is not None and sc >= stop_score:
                break
            time.sleep(0.012)
        return {
            'start_score': start_score,
            'end_score': self.g.score(),
            'peak_score': peak,
            'round_active': round_active,
            'stats': dict(self.stats),
        }
