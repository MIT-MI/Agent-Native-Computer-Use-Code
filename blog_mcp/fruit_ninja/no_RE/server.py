#!/usr/bin/env python3
"""Fruit Ninja HD MCP server.

Plays the Windows game Fruit Ninja HD (under Wine on X11) in real time by
watching the screen and sending mouse blade-strokes. Speaks MCP JSON-RPC
over stdin/stdout (newline-delimited).
"""
import sys, os, json, subprocess, threading, time, math
import numpy as np
from scipy import ndimage

HERE = os.path.dirname(os.path.abspath(__file__))
HELPER = os.path.join(HERE, 'deps', 'swipehelper')
DIGITS = os.path.join(HERE, 'deps', 'digits')
DISPLAY = os.environ.get('DISPLAY', ':7')
W, H = 1024, 768
GRAV = 1600.0

def log(*a):
    print('[fn]', *a, file=sys.stderr, flush=True)
    try:
        with open('/tmp/fn_nav.log', 'a') as fh:
            fh.write('%.2f %s\n' % (time.time(), ' '.join(str(x) for x in a)))
    except Exception:
        pass

# ---------------------------------------------------------------- grabber
class Grabber:
    """Streams the screen as half-res (512x384) frames via ffmpeg."""
    def __init__(self, fps=30):
        self.proc = subprocess.Popen([
            'ffmpeg', '-hide_banner', '-loglevel', 'error',
            '-f', 'x11grab', '-video_size', f'{W}x{H}', '-framerate', str(fps),
            '-i', DISPLAY, '-vf', 'scale=512:384', '-f', 'image2pipe',
            '-vcodec', 'ppm', '-'],
            stdout=subprocess.PIPE, bufsize=10**6)
        self.frame = None
        self.lock = threading.Lock()
        self.seq = 0
        self.running = True
        self.t = threading.Thread(target=self._read, daemon=True)
        self.t.start()
    def _read(self):
        f = self.proc.stdout
        while self.running:
            magic = f.readline()
            if not magic: break
            dims = f.readline(); f.readline()
            w, h = map(int, dims.split())
            data = f.read(w*h*3)
            if len(data) < w*h*3: break
            arr = np.frombuffer(data, np.uint8).reshape(h, w, 3)
            with self.lock:
                self.frame = arr; self.seq += 1
    def get(self):
        with self.lock: return self.frame, self.seq
    def wait_next(self, last_seq, timeout=1.0):
        t0 = time.time()
        while time.time() - t0 < timeout:
            fr, sq = self.get()
            if sq > last_seq and fr is not None: return fr, sq
            time.sleep(0.001)
        return self.get()

# ---------------------------------------------------------------- swiper
class Swiper:
    """Issues blade strokes through a persistent XTest helper process."""
    def __init__(self):
        self.proc = subprocess.Popen([HELPER], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE,
                                     env={'DISPLAY': DISPLAY})
        self.lock = threading.Lock()
        self.busy_until = 0.0
        self.recent = []
        self.mouse = (512, 384)
    def swipe(self, x1, y1, x2, y2, steps=8, step_ms=25, wait=False):
        now = time.time()
        with self.lock:
            if now < self.busy_until:
                if not wait: return False
            dur = steps*step_ms/1000.0
            self.busy_until = max(self.busy_until, now) + dur + 0.03
            self.recent.append((x1, y1, x2, y2, now + dur + 0.45))
            self.recent = [r for r in self.recent if r[4] > now]
            self.mouse = (x2, y2)
        self.proc.stdin.write(
            f's {x1:.0f} {y1:.0f} {x2:.0f} {y2:.0f} {steps} {step_ms}\n'.encode())
        self.proc.stdin.flush()
        return True
    def swipe_poly(self, points, step_ms=22):
        now = time.time()
        with self.lock:
            if now < self.busy_until: return False
            dur = len(points)*step_ms/1000.0
            self.busy_until = now + dur + 0.03
            for i in range(len(points)-1):
                self.recent.append((points[i][0], points[i][1],
                                    points[i+1][0], points[i+1][1], now+dur+0.45))
            self.recent = [r for r in self.recent if r[4] > now]
            self.mouse = points[-1]
        cmd = ('p ' + str(len(points)) + ' ' +
               ' '.join(f'{x:.0f} {y:.0f}' for x, y in points) + f' {step_ms}\n')
        self.proc.stdin.write(cmd.encode())
        self.proc.stdin.flush()
        return True
    def busy(self):
        return time.time() < self.busy_until
    def drag_down(self):
        self.proc.stdin.write(b'w\n'); self.proc.stdin.flush()
    def drag_up(self):
        self.proc.stdin.write(b'u\n'); self.proc.stdin.flush()
    def drag_move(self, x, y, delay_ms=25):
        self.mouse = (x, y)
        self.proc.stdin.write(f'm {x:.0f} {y:.0f} {delay_ms}\n'.encode())
        self.proc.stdin.flush()
    def near_recent(self, x, y, r=95):
        now = time.time()
        if math.hypot(x-self.mouse[0], y-self.mouse[1]) < 55:
            return True
        for x1, y1, x2, y2, te in self.recent:
            if te < now: continue
            if seg_point_dist(x1, y1, x2, y2, x, y) < r: return True
        return False

def seg_point_dist(ax, ay, bx, by, px, py):
    dx, dy = bx-ax, by-ay
    L2 = dx*dx+dy*dy
    if L2 == 0: return math.hypot(px-ax, py-ay)
    t = max(0, min(1, ((px-ax)*dx+(py-ay)*dy)/L2))
    return math.hypot(px-(ax+t*dx), py-(ay+t*dy))

# ---------------------------------------------------------------- detector
HUD = np.zeros((192, 256), bool)
HUD[0:23, 0:55] = True
HUD[0:23, 210:256] = True
HUD[172:192, 0:18] = True

class Detector:
    """Quarter-res background-subtraction moving-object detector.
    Returns full-res coordinates."""
    def __init__(self):
        self.bg = None
    def reset(self):
        self.bg = None
    def update(self, half):
        fr = half[::2, ::2].astype(np.int16)
        if self.bg is None:
            self.bg = fr.astype(np.int32)
            return []
        d = np.abs(fr - self.bg).sum(2)
        self.bg = self.bg + np.floor_divide(fr - self.bg, 150)
        fg = (d > 60) & ~HUD
        op = ndimage.binary_dilation(fg, iterations=1)
        lab, nl = ndimage.label(op)
        out = []
        for i, sl in enumerate(ndimage.find_objects(lab)):
            if sl is None: continue
            sub = lab[sl] == i+1
            area = int(sub.sum())
            if area < 15 or area > 3000: continue
            ys, xs = np.nonzero(sub)
            cx = (xs.mean()+sl[1].start)*4
            cy = (ys.mean()+sl[0].start)*4
            reg = fr[sl][sub]
            vv = reg.max(1)
            v = float(vv.mean())
            mx = reg.max(1); mn = reg.min(1)
            sat = float(((mx-mn)/np.maximum(mx, 1)).mean())
            dark = float((vv < 70).mean())
            darkcount = int((vv < 70).sum())
            # dark-core centroid (accurate bomb-body position, trail-free)
            if darkcount > 20:
                dmask = vv < 70
                dcx = (xs[dmask].mean()+sl[1].start)*4
                dcy = (ys[dmask].mean()+sl[0].start)*4
            else:
                dcx, dcy = cx, cy
            out.append({'x': cx, 'y': cy, 'area': area*16, 'v': v, 'sat': sat,
                        'dark': dark, 'darkcount': darkcount,
                        'dcx': dcx, 'dcy': dcy})
        return out

# ---------------------------------------------------------------- state
def classify(half):
    """half: 384x512x3 uint8. Return one of
    'round', 'game_over', 'main_menu', 'unknown' (menus/transitions)."""
    f = half.astype(int)
    if f.mean() > 135:
        return 'unknown'   # white flash transition
    pan = f[60:230, 180:470]
    beige = ((pan[:,:,0] > 170) & (pan[:,:,0] < 235) &
             (pan[:,:,1] > 150) & (pan[:,:,1] < 215) &
             (pan[:,:,2] > 100) & (pan[:,:,2] < 170)).sum()
    if beige > 5000:
        return 'game_over'
    # paused screen: white PLAY and RESTART icons in grey boxes, center
    pb = f[185:245, 185:245]
    rb = f[185:245, 270:332]
    if ((pb > 200).all(2)).sum() > 400 and ((rb > 200).all(2)).sum() > 250:
        return 'paused'
    pa = f[357:378, 5:23]
    pause = ((pa > 150).all(2)).sum() > 50
    # score HUD: yellow digit(s) in a dark box, top-left
    sc = f[8:33, 30:110]
    score_hud = ((sc[:,:,0] > 150) & (sc[:,:,1] > 110) &
                 (sc[:,:,2] < 100)).sum() > 25
    if pause or score_hud:
        return 'round'
    logo = f[15:90, 100:210]
    if ((logo[:,:,0] > 120) & (logo[:,:,0] > logo[:,:,2]+50) &
        (logo[:,:,1] < logo[:,:,0]-30)).sum() > 800:
        return 'main_menu'
    return 'unknown'

def round_mode(half):
    # During a round: 'classic' if life X marks top-right, else 'timer'.
    f = half.astype(int)
    la = f[6:31, 430:508]
    blue = ((la[:,:,2] > 90) & (la[:,:,2] > la[:,:,0]+30)).sum()
    if blue > 30:
        return 'classic'
    return 'timer'

# ---------------------------------------------------------------- OCR
class DigitOCR:
    """Template-matching OCR for the yellow HUD score digits."""
    def __init__(self):
        self.templates = {}  # digit -> list of 24x16 bool arrays (HUD font)
        self.go_templates = {}  # digit -> list of 24x16 bool arrays (big font)
        self.unknown_dir = '/tmp/fn_digits_unknown'
        os.makedirs(self.unknown_dir, exist_ok=True)
        for fn in os.listdir(DIGITS):
            if fn.startswith('hud_') and fn.endswith('.png') and fn[4].isdigit():
                d = fn[4]
                arr = self._load(os.path.join(DIGITS, fn))
                if arr is not None:
                    self.templates.setdefault(d, []).append(arr)
            elif fn.startswith('go_') and fn.endswith('.png') and fn[3].isdigit():
                d = fn[3]
                arr = self._load(os.path.join(DIGITS, fn))
                if arr is not None:
                    self.go_templates.setdefault(d, []).append(arr)
    def _load(self, path):
        from PIL import Image
        a = np.array(Image.open(path).convert('L')) > 0
        return self._norm(a)
    def _norm(self, mask):
        from PIL import Image
        if mask.sum() == 0: return None
        h, w = mask.shape
        nw = max(1, int(round(24.0*w/h)))
        im = Image.fromarray((mask*255).astype('uint8'))
        im = im.resize((nw, 24))
        a = np.array(im) > 100
        # center on a 28-wide canvas (crop if wider)
        canvas = np.zeros((24, 28), bool)
        if nw <= 28:
            off = (28-nw)//2
            canvas[:, off:off+nw] = a
        else:
            st = (nw-28)//2
            canvas = a[:, st:st+28]
        return canvas
    def read_score(self, half):
        """Read the HUD score (top-left yellow digits). Return int or None."""
        f = half.astype(int)
        reg = f[8:33, 30:110]   # half-res score digit area
        mask = (reg[:,:,0] > 170) & (reg[:,:,1] > 120) & (reg[:,:,2] < 100)
        lab, n = ndimage.label(mask)
        boxes = []
        for i, sl in enumerate(ndimage.find_objects(lab)):
            if sl is None: continue
            h = sl[0].stop-sl[0].start; w = sl[1].stop-sl[1].start
            if h > 7 and w > 2:
                boxes.append((sl[1].start, sl))
        boxes.sort()
        if not boxes: return None
        digits = []
        for x, sl in boxes:
            dm = self._norm(mask[sl])
            if dm is None: return None
            best, bs = None, -1
            for d, tmps in self.templates.items():
                for t in tmps:
                    inter = (dm & t).sum(); union = (dm | t).sum()
                    s = inter/union if union else 0
                    if s > bs: bs, best = s, d
            if bs < 0.45:
                self._save_unknown(mask[sl])
                return None
            digits.append(best)
        try:
            return int(''.join(digits))
        except ValueError:
            return None
    def _topfill(self, mask):
        # fraction of ink in the top-left region (a '5' has a solid top bar,
        # a '9' has a gap there). Used to tell 5 from 9.
        h, w = mask.shape
        top = mask[:max(1, h//5), :max(1, int(w*0.6))]
        return top.mean() if top.size else 0.0
    def _match(self, dm, templates):
        best, bs = None, -1
        for d, tmps in templates.items():
            for t in tmps:
                inter = (dm & t).sum(); union = (dm | t).sum()
                s = inter/union if union else 0
                if s > bs: bs, best = s, d
        # disambiguate 5 vs 9 using the top-fill feature
        if best in ('5', '9') and '5' in templates and '9' in templates:
            qf = self._topfill(dm)
            f5 = np.mean([self._topfill(t) for t in templates['5']])
            f9 = np.mean([self._topfill(t) for t in templates['9']])
            best = '5' if abs(qf-f5) < abs(qf-f9) else '9'
        return best, bs
    def read_final_score(self, half):
        # Read the big game-over score digits. Return int or None.
        f = half.astype(int)
        reg = f[115:170, 25:170]   # half-res big digit area
        mask = (reg[:,:,0] > 170) & (reg[:,:,1] > 120) & (reg[:,:,2] < 100)
        lab, n = ndimage.label(mask)
        boxes = []
        for i, sl in enumerate(ndimage.find_objects(lab)):
            if sl is None: continue
            h = sl[0].stop-sl[0].start; w = sl[1].stop-sl[1].start
            if h > 12 and w > 4:
                boxes.append((sl[1].start, sl))
        boxes.sort()
        if not boxes: return None
        digits = []
        for x, sl in boxes:
            dm = self._norm(mask[sl])
            if dm is None: return None
            best, bs = self._match(dm, self.go_templates)
            if bs < 0.45:
                self._save_unknown(mask[sl])
                return None
            digits.append(best)
        try:
            return int(''.join(digits))
        except ValueError:
            return None
    def _save_unknown(self, mask):
        from PIL import Image
        try:
            n = len(os.listdir(self.unknown_dir))
            if n < 200:
                Image.fromarray((mask*255).astype('uint8')).save(
                    os.path.join(self.unknown_dir, f'u{int(time.time())}_{n}.png'))
        except Exception:
            pass

# ---------------------------------------------------------------- engine
class Engine:
    def __init__(self):
        self.grabber = None
        self.swiper = None
        self.det = Detector()
        self.ocr = DigitOCR()
        self.play_stop = threading.Event()
        self.play_thread = None
        self.lock = threading.Lock()
        self._debug = bool(os.environ.get('FN_DEBUG'))
        self._debug_n = 0
    def ensure(self):
        if self.grabber is None:
            self.grabber = Grabber(30)
            t0 = time.time()
            while time.time() - t0 < 3:
                fr, sq = self.grabber.get()
                if fr is not None: break
                time.sleep(0.05)
        if self.swiper is None:
            self.swiper = Swiper()
    def frame(self):
        self.ensure()
        fr, sq = self.grabber.get()
        return fr
    def state(self):
        fr = self.frame()
        if fr is None: return 'unknown', None
        return classify(fr), fr

    # ---- navigation -------------------------------------------------
    def swipe_full(self, x1, y1, x2, y2, steps=8, ms=30):
        self.ensure()
        self.swiper.swipe(x1, y1, x2, y2, steps=steps, step_ms=ms, wait=True)
        time.sleep(steps*ms/1000.0 + 0.1)
    def wait_state(self, want, timeout=4.0):
        t0 = time.time()
        while time.time() - t0 < timeout:
            st, _ = self.state()
            if st == want: return True
            time.sleep(0.2)
        return False
    def start_round(self, mode='classic'):
        # Navigate to start a round of the given mode. Never disturbs a
        # running round. Returns (ok, message).
        self.ensure()
        st, _ = self.state()
        log('start_round: initial state', st)
        if st == 'paused':
            # resume the paused round, then play it
            self.swipe_full(360, 425, 490, 425, steps=6, ms=25)
            time.sleep(0.6)
            st, _ = self.state()
            if st == 'round':
                return True, 'resumed a paused round; playing it'
        if st == 'round':
            return True, 'a round is already running; playing it'
        # get to main menu
        got = False
        for _ in range(10):
            st, _ = self.state()
            if st == 'main_menu':
                got = True; break
            if st == 'round':
                return True, 'a round is already running; playing it'
            if st == 'game_over':
                log('start_round: game_over -> QUIT')
                self.swipe_full(615, 630, 755, 630)
                time.sleep(1.4)
            else:
                time.sleep(0.5)
        if not got:
            return False, 'could not reach main menu'
        # main menu -> NEW GAME (watermelon); retry while still on main menu
        left = False
        for _ in range(4):
            log('start_round: main_menu -> NEW GAME')
            self.swipe_full(470, 470, 620, 580)
            t0 = time.time()
            while time.time() - t0 < 3.0:
                st = self.state()[0]
                if st == 'round':
                    self.det.reset(); return True, 'round started'
                if st not in ('main_menu', 'unknown'):
                    left = True; break
                if st == 'unknown':
                    # maybe transitioned; keep watching
                    pass
                time.sleep(0.15)
            # after the wait, if we are no longer on main menu, we left
            if self.state()[0] != 'main_menu':
                left = True
            if left: break
        if self.state()[0] == 'main_menu':
            return False, 'could not leave main menu'
        if self.state()[0] == 'round':
            self.det.reset(); return True, 'round started'
        time.sleep(0.8)   # let mode select settle
        # mode select -> chosen mode (vertical stroke through the mode fruit)
        targets = {'classic': (360, 230), 'zen': (700, 280), 'arcade': (510, 540)}
        tx, ty = targets.get(mode, targets['classic'])
        for _ in range(5):
            log('start_round: mode_select ->', mode)
            self.swipe_full(tx, ty-80, tx, ty+80)
            if self.wait_state('round', 3.0):
                self.det.reset()
                return True, f'{mode} round started'
        if self.state()[0] == 'round':
            self.det.reset(); return True, 'round started'
        return False, f'could not start {mode}'

    # ---- play loop --------------------------------------------------
    def play(self, duration=120, mode='arcade', rounds=1):
        """Play one or more rounds autonomously in real time; returns the
        best score over the rounds played."""
        t_start = time.time()
        all_scores = []
        last_stats = {}
        for r in range(max(1, int(rounds))):
            remaining = duration - (time.time() - t_start)
            if remaining < 5:
                break
            stats = self._play_round(remaining, mode, use_retry=(r > 0))
            last_stats = stats
            sc = stats.get('score')
            if sc is not None:
                all_scores.append(sc)
            if stats.get('error') and sc is None:
                break
            if self.play_stop.is_set():
                break
        best = max(all_scores) if all_scores else last_stats.get('score')
        return {'best_score': best, 'scores': all_scores,
                'rounds_played': len(all_scores),
                'last_round': last_stats}

    def _retry_round(self):
        # on the game_over screen, slice RETRY (apple) to replay the same mode
        for _ in range(3):
            st, _ = self.state()
            if st == 'round':
                return True
            if st == 'game_over':
                self.swipe_full(270, 630, 410, 630)
                time.sleep(1.2)
            else:
                time.sleep(0.5)
        return self.state()[0] == 'round'

    def _play_round(self, duration=120, mode='arcade', use_retry=False):
        """Play a single round autonomously in real time."""
        self.ensure()
        st, _ = self.state()
        started = False
        if st != 'round':
            if use_retry and st == 'game_over':
                self._retry_round()
                st, _ = self.state()
            if st != 'round':
                ok, msg = self.start_round(mode)
                if not ok:
                    # only proceed if a round genuinely started despite the failure
                    st2, _ = self.state()
                    if st2 != 'round':
                        return {'error': msg}
                started = True
        self.play_stop.clear()
        det = self.det
        det.reset()
        sw = self.swiper
        g = self.grabber
        # wait until the round is actually on screen before playing
        t0w = time.time()
        while time.time() - t0w < 5.0:
            st_, fr_ = self.state()
            if st_ == 'round': break
            time.sleep(0.15)
        fr, seq = g.get()
        rmode = round_mode(fr) if fr is not None else 'timer'
        self._rmode = rmode
        stats = {'swipes': 0, 'frames': 0, 'score': None, 'started_round': started,
                 'mode': rmode}
        t0 = time.time()
        det.update(fr)
        tracks = []
        sliced_recent = []
        seen_round = False
        go_count = 0
        last_score = None
        last_fruit = time.time()
        while time.time() - t0 < duration and not self.play_stop.is_set():
            fr, seq = g.wait_next(seq, 0.5)
            if fr is None: continue
            stats['frames'] += 1
            state = classify(fr)
            dets = det.update(fr)
            now = time.time()
            if fr.mean() < 15:
                stats['ended'] = 'game_window_lost'
                stats['error'] = 'the game window went black (game may have quit)'
                break
            if state != getattr(self, '_last_state', None):
                log('play state ->', state, 'frames', stats['frames'])
                self._last_state = state
            if state == 'paused':
                # require the pause screen to persist (blade trails can cause
                # transient false positives); then resume via the PLAY button
                pause_n = getattr(self, '_pause_n', 0) + 1
                self._pause_n = pause_n
                if pause_n >= 3:
                    log('play: paused, resuming')
                    self.swipe_full(360, 425, 490, 425, steps=6, ms=25)
                    time.sleep(0.4)
                    self._pause_n = 0
                continue
            if state != 'round':
                if state == 'game_over' and seen_round:
                    go_count += 1
                    if go_count >= 4:
                        # let the end screen settle (arcade shows a bonus
                        # tally before the final SCORE screen), then read the
                        # big score digits; fall back to the live HUD score.
                        best_sc = None
                        prev = None
                        # wait for the final SCORE screen (with the RETRY
                        # button) - the arcade bonus tally counts up first.
                        for _ in range(50):
                            st2, fr2 = self.state()
                            if fr2 is None: break
                            if classify(fr2) != 'game_over':
                                time.sleep(0.3); continue
                            f2 = fr2.astype(int)
                            rb = f2[280:350, 135:205]
                            retry = ((rb[:,:,2] > 90) &
                                     (rb[:,:,2] > rb[:,:,0]+30)).sum() > 100
                            sc2 = self.ocr.read_final_score(fr2)
                            if retry and sc2 is not None:
                                # final screen; require two equal reads so the
                                # tally animation has fully settled
                                if sc2 == prev:
                                    best_sc = sc2
                                    break
                                prev = sc2
                            time.sleep(0.3)
                        sc = best_sc if best_sc is not None else last_score
                        if sc is None:
                            sc = self.ocr.read_score(fr)
                        stats['score'] = sc
                        stats['ended'] = 'game_over'
                        break
                    continue
                go_count = 0
                # transition (white flash, confetti, banner); keep waiting.
                # tracks are left to expire naturally so brief occlusions do
                # not wipe the velocity fits.
                continue
            go_count = 0
            self._pause_n = 0
            seen_round = True
            # live score every ~0.5s
            if stats['frames'] % 15 == 0:
                sc = self.ocr.read_score(fr)
                if sc is not None:
                    last_score = sc
                    stats['score'] = sc
            cands = []
            for d in dets:
                if d['v'] > 240 and d['area'] < 2500: continue
                if sw.near_recent(d['x'], d['y']): continue
                skip = False
                for sx, sy, st_ in sliced_recent:
                    if now - st_ < 1.0 and math.hypot(d['x']-sx, d['y']-sy) < 130:
                        skip = True; break
                if skip: continue
                kind = 'bomb' if (d['dark'] > 0.35 and d['darkcount'] > 100) else 'fruit'
                if kind == 'bomb':
                    d['x'], d['y'] = d['dcx'], d['dcy']
                cands.append({**d, 'kind': kind})
            tracks = self._update_tracks(tracks, cands, now)
            if any(t['kind'] == 'fruit' and not t['static'] for t in tracks):
                last_fruit = now
            elif now - last_fruit > 3.0:
                # no fruit for 3s mid-round: the round has ended (timer ran
                # out) even if the end screen has not fully appeared yet.
                stats['ended'] = 'round_timeout'
                sc = self.ocr.read_final_score(fr)
                stats['score'] = sc if sc is not None else last_score
                break
            if not sw.busy():
                self._plan_swipe(tracks, sw, now, sliced_recent, stats)
            if self._debug and stats['frames'] % 3 == 0:
                self._dump_debug(fr, tracks, sw, now)
        if stats['score'] is None:
            stats['score'] = last_score
        stats['duration'] = round(time.time()-t0, 1)
        return stats

    def _update_tracks(self, tracks, cands, now):
        for c in cands:
            best = None; bd = 80
            for tr in tracks:
                if tr['static']: continue
                dt = now - tr['last']
                px = tr['x'] + tr['vx']*dt
                py = tr['y'] + tr['vy']*dt + 0.5*tr.get('g', GRAV)*dt*dt
                dist = math.hypot(px-c['x'], py-c['y'])
                if dist < bd: bd = dist; best = tr
            if best is not None:
                best['x'], best['y'], best['last'] = c['x'], c['y'], now
                best['kind'] = c['kind']; best['v'] = c['v']
                best['n'] += 1
                best['hist'].append((now, c['x'], c['y']))
                if len(best['hist']) > 9:
                    best['hist'] = best['hist'][-9:]
                self._fit(best, now)
                if math.hypot(best['vx'], best['vy']) < 40 and best['n'] > 6:
                    best['static'] = True
            else:
                tracks.append({**c, 'vx': 0.0, 'vy': 0.0, 'last': now,
                               'swiped_at': 0.0, 'born': now, 'n': 1,
                               'static': False, 'g': GRAV,
                               'hist': [(now, c['x'], c['y'])]})
        return [t for t in tracks if now - t['last'] < (3.0 if t['static'] else 0.30)]

    def _fit(self, tr, now):
        # Least-squares fit of trajectory; sets vx, vy, g, fx, fy.
        h = tr['hist']
        n = len(h)
        if n < 2:
            tr['fx'], tr['fy'] = h[-1][1], h[-1][2]
            return
        tau = np.array([p[0]-now for p in h])
        xs = np.array([p[1] for p in h])
        ys = np.array([p[2] for p in h])
        if tau[-1]-tau[0] < 0.04:
            # points too close in time; use simple finite difference
            dt = tau[-1]-tau[0]
            if abs(dt) < 1e-4:
                tr['fx'], tr['fy'] = xs[-1], ys[-1]
                return
            tr['vx'] = max(-1800, min(1800, (xs[-1]-xs[0])/dt))
            tr['vy'] = max(-1800, min(1800, (ys[-1]-ys[0])/dt))
            tr['g'] = GRAV
            tr['fx'], tr['fy'] = xs[-1], ys[-1]
            return
        try:
            import warnings
            warnings.simplefilter('ignore')
            vx, x0 = np.polyfit(tau, xs, 1)
            if n >= 4:
                c2, c1, c0 = np.polyfit(tau, ys, 2)
                g = 2*c2
                if not (400 < g < 4000):
                    g = GRAV
                    c1, c0 = np.polyfit(tau, ys - 0.5*GRAV*tau*tau, 1)
                vy = c1
                y0 = c0
            else:
                g = GRAV
                vy, y0 = np.polyfit(tau, ys - 0.5*GRAV*tau*tau, 1)
            tr['vx'] = max(-1800, min(1800, vx))
            tr['vy'] = max(-1800, min(1800, vy))
            tr['g'] = g
            tr['fx'], tr['fy'] = x0, y0
        except Exception:
            tr['fx'], tr['fy'] = tr['x'], tr['y']

    def _play_sweep(self, duration, stats, t0):
        """Continuous-blade play for timer modes (zen/arcade)."""
        det = self.det; sw = self.swiper; g = self.grabber
        fr, seq = g.get()
        det.update(fr)
        tracks = []
        sliced_recent = []
        blade_down = False
        last_score = None
        while time.time() - t0 < duration and not self.play_stop.is_set():
            fr, seq = g.wait_next(seq, 0.5)
            if fr is None: continue
            stats['frames'] += 1
            state = classify(fr)
            now = time.time()
            if state != 'round':
                if blade_down:
                    sw.drag_up(); blade_down = False
                if state == 'game_over':
                    sc = self.ocr.read_final_score(fr)
                    if sc is None: sc = self.ocr.read_score(fr)
                    stats['score'] = sc; stats['ended'] = 'game_over'
                    break
                tracks = []; sliced_recent = []
                continue
            dets = det.update(fr)
            if stats['frames'] % 15 == 0:
                sc = self.ocr.read_score(fr)
                if sc is not None:
                    last_score = sc; stats['score'] = sc
            cands = []
            for d in dets:
                if d['v'] > 240 and d['area'] < 2500: continue
                if sw.near_recent(d['x'], d['y']): continue
                skip = False
                for sx, sy, st_ in sliced_recent:
                    if now - st_ < 0.8 and math.hypot(d['x']-sx, d['y']-sy) < 120:
                        skip = True; break
                if skip: continue
                kind = 'bomb' if (d['dark'] > 0.35 and d['darkcount'] > 100) else 'fruit'
                if kind == 'bomb':
                    d['x'], d['y'] = d['dcx'], d['dcy']
                cands.append({**d, 'kind': kind})
            tracks = self._update_tracks(tracks, cands, now)
            fruits = [t for t in tracks if t['kind'] == 'fruit' and not t['static']
                      and t['n'] >= 2 and 70 < t['y'] < 700 and 20 < t['x'] < 1004
                      and math.hypot(t['vx'], t['vy']) > 40]
            bombs = [t for t in tracks if t['kind'] == 'bomb' and not t['static']]
            if not fruits:
                continue
            # nearest fruit to current mouse
            mx, my = sw.mouse
            fruits.sort(key=lambda t: math.hypot(t['x']-mx, t['y']-my))
            f = fruits[0]
            dt = min(0.22, (now - f['last']) + 0.05)
            px = f['x'] + f['vx']*dt
            py = f['y'] + f['vy']*dt + 0.5*GRAV*dt*dt
            px = max(20, min(1004, px)); py = max(50, min(745, py))
            # overshoot past the fruit along its velocity
            sp = math.hypot(f['vx'], f['vy'])
            if sp > 40:
                ox, oy = px + f['vx']/sp*60, py + f['vy']/sp*60
            else:
                ox, oy = px, py
            # bomb check along blade path mouse->overshoot
            blocked = False
            for b in bombs:
                bdt = min(0.22, (now - b['last']) + 0.05)
                bpx = b['x'] + b['vx']*bdt
                bpy = b['y'] + b['vy']*bdt + 0.5*GRAV*bdt*bdt
                if seg_point_dist(mx, my, ox, oy, bpx, bpy) < 80:
                    blocked = True; break
            if blocked:
                if blade_down:
                    sw.drag_up(); blade_down = False
                continue
            if not blade_down:
                sw.drag_down(); blade_down = True
            sw.drag_move(ox, oy, 25)
            stats['swipes'] += 1
            sliced_recent.append((f['x'], f['y'], now))
            sliced_recent[:] = [s for s in sliced_recent if now - s[2] < 1.0]
        if blade_down:
            sw.drag_up()
        if stats['score'] is None:
            stats['score'] = last_score

    def _plan_swipe(self, tracks, sw, now, sliced_recent, stats):
        fruits = [t for t in tracks if t['kind'] == 'fruit'
                  and now - t['swiped_at'] > 0.35 and not t['static']
                  and t['n'] >= 3 and 90 < t['y'] < 690 and 30 < t['x'] < 994
                  and not (t['x'] < 150 and t['y'] > 600)
                  and math.hypot(t['vx'], t['vy']) > 60]
        bombs = [t for t in tracks if t['kind'] == 'bomb' and not t['static']]
        if not fruits: return
        HOR = 0.13
        def pred(f):
            dt = min(0.25, (now - f['last']) + HOR)
            bx = f.get('fx', f['x']); by = f.get('fy', f['y'])
            px = bx + f['vx']*dt
            py = by + f['vy']*dt + 0.5*f.get('g', GRAV)*dt*dt
            return (max(20, min(1004, px)), max(60, min(740, py)))
        pts = [(f, pred(f)) for f in fruits[:8]]
        # bomb positions now and over the stroke duration (~0.3s)
        bomb_pts = []
        for b in bombs:
            for h in (0.0, 0.1, 0.2, 0.3):
                dt = min(0.35, (now - b['last']) + h)
                bpx = b.get('fx', b['x']) + b['vx']*dt
                bpy = b.get('fy', b['y']) + b['vy']*dt + 0.5*b.get('g', GRAV)*dt*dt
                bomb_pts.append((bpx, bpy))
        def blocked(ax, ay, bx, by):
            for bpx, bpy in bomb_pts:
                if seg_point_dist(ax, ay, bx, by, bpx, bpy) < 85:
                    return True
            # never stroke through the in-game pause button (bottom-left)
            if seg_point_dist(ax, ay, bx, by, 25, 735) < 135:
                return True
            return False
        def clamp(x, y):
            x = max(10, min(1014, x)); y = max(40, min(750, y))
            if x < 120 and y > 640:
                y = 640   # keep out of the pause-button corner
            return (x, y)
        best = None; best_cnt = 0; best_cov = None
        n = len(pts)
        for i in range(n):
            for j in range(n):
                if i == j: continue
                f1, (x1, y1) = pts[i]
                f2, (x2, y2) = pts[j]
                dx, dy = x2-x1, y2-y1
                L = math.hypot(dx, dy)
                if L < 30 or L > 520: continue
                dx, dy = dx/L, dy/L
                proj = []
                cov = []
                for f, (px, py) in pts:
                    t = (px-x1)*dx + (py-y1)*dy
                    perp = abs((px-x1)*dy - (py-y1)*dx)
                    if perp < 90:
                        proj.append(t)
                        cov.append((t, px, py))
                if len(proj) <= best_cnt: continue
                lo = min(proj+[0.0]); hi = max(proj+[L])
                ax, ay = clamp(x1+dx*(lo-90), y1+dy*(lo-90))
                bx, by = clamp(x1+dx*(hi+90), y1+dy*(hi+90))
                if blocked(ax, ay, bx, by): continue
                best_cnt = len(proj)
                cov.sort()
                best_cov = [(px, py) for (t, px, py) in cov]
                best = (ax, ay, bx, by, f1, dx, dy)
        if best_cnt < 2:
            if getattr(self, '_rmode', 'timer') == 'classic':
                # classic (lives): prefer the easiest target (slowest, near
                # apex); fast-rising fruits are left to slow down.
                def ease(t):
                    lost = t['vy'] > 350 and t['y'] > 600
                    return (lost, abs(t['vy']))
                fruits.sort(key=ease)
                f = fruits[0]
                if f['vy'] < -220:
                    return
            else:
                # timer modes (zen/arcade): swipe the most urgent fruit
                # (falling first, then lowest); let fast-rising ones wait.
                fruits.sort(key=lambda t: (t['vy'] < 100, -t['y']))
                f = fruits[0]
                if f['vy'] < -80:
                    return

            px, py = pred(f)
            sp = math.hypot(f['vx'], f['vy'])
            dx, dy = f['vx']/sp, f['vy']/sp
            L = 150
            ax, ay = clamp(px-dx*L, py-dy*L)
            bx, by = clamp(px+dx*L, py+dy*L)
            if blocked(ax, ay, bx, by): return
            best = (ax, ay, bx, by, f, dx, dy); best_cnt = 1
        if best:
            ax, ay, bx, by, f, dx, dy = best
            done = False
            if best_cnt >= 3 and best_cov:
                # polyline through each covered fruit's predicted position,
                # with rounded ends along the line direction
                path = [(ax, ay)]
                for (px, py) in best_cov:
                    if math.hypot(px-path[-1][0], py-path[-1][1]) > 25:
                        path.append((px, py))
                path.append((bx, by))
                path = [clamp(x, y) for (x, y) in path]
                if len(path) > 12:
                    path = path[:12]
                # check every segment against bombs
                poly_blocked = False
                for k in range(len(path)-1):
                    if blocked(path[k][0], path[k][1], path[k+1][0], path[k+1][1]):
                        poly_blocked = True; break
                if not poly_blocked:
                    done = sw.swipe_poly(path)
            if not done:
                done = sw.swipe(ax, ay, bx, by)
            if done:
                f['swiped_at'] = now
                sliced_recent.append((f['x'], f['y'], now))
                sliced_recent[:] = [s for s in sliced_recent if now - s[2] < 1.2]
                stats['swipes'] += 1
                stats['combo'] = max(stats.get('combo', 0), best_cnt)

    def _dump_debug(self, fr, tracks, sw, now):
        from PIL import Image, ImageDraw
        try:
            os.makedirs('/tmp/fn_debug', exist_ok=True)
            img = Image.fromarray(fr)
            dr = ImageDraw.Draw(img)
            for t in tracks:
                c = 'red' if t['kind'] == 'bomb' else ('gray' if t['static'] else 'lime')
                dr.ellipse([t['x']/2-8, t['y']/2-8, t['x']/2+8, t['y']/2+8], outline=c, width=2)
                dr.text((t['x']/2+10, t['y']/2-6), f"{int(t['vx'])},{int(t['vy'])}", fill=c)
            for x1, y1, x2, y2, te in sw.recent:
                dr.line([x1/2, y1/2, x2/2, y2/2], fill='cyan', width=2)
            n = self._debug_n
            img.save(f'/tmp/fn_debug/d{n:04d}.jpg', quality=60)
            self._debug_n += 1
        except Exception:
            pass

    def stop_play(self):
        self.play_stop.set()

    def _lives(self, half):
        # count remaining (blue) life-X marks, top-right (classic)
        f = half.astype(int)
        la = f[6:31, 430:508]
        blue = ((la[:,:,2] > 90) & (la[:,:,2] > la[:,:,0]+30)).astype('uint8')
        lab, n = ndimage.label(blue)
        cnt = 0
        for i, sl in enumerate(ndimage.find_objects(lab)):
            if sl is None: continue
            if (sl[0].stop-sl[0].start) >= 4 and (sl[1].stop-sl[1].start) >= 4:
                cnt += 1
        return cnt

    def _detect_objects(self):
        # two-frame motion detection; independent of the play loop's detector
        self.ensure()
        fr1, sq = self.grabber.get()
        if fr1 is None: return []
        time.sleep(0.06)
        fr2, sq = self.grabber.wait_next(sq, 0.5)
        if fr2 is None: return []
        a = fr1[::2, ::2].astype(np.int16)
        b = fr2[::2, ::2].astype(np.int16)
        d = np.abs(b - a).sum(2)
        mask = (d > 60) & ~HUD
        mask = ndimage.binary_dilation(mask, iterations=1)
        lab, nl = ndimage.label(mask)
        out = []
        for i, sl in enumerate(ndimage.find_objects(lab)):
            if sl is None: continue
            sub = lab[sl] == i+1
            area = int(sub.sum())
            if area < 15 or area > 3000: continue
            ys, xs = np.nonzero(sub)
            reg = b[sl][sub]
            vv = reg.max(1)
            dark = float((vv < 70).mean())
            kind = 'bomb' if (dark > 0.35 and int((vv < 70).sum()) > 100) else 'fruit'
            out.append({'x': round(float((xs.mean()+sl[1].start)*4)),
                        'y': round(float((ys.mean()+sl[0].start)*4)),
                        'kind': kind})
        return out

    def get_state_report(self):
        st, fr = self.state()
        rep = {'state': st}
        if fr is None:
            return rep
        if st == 'round':
            rep['mode'] = round_mode(fr)
            sc = self.ocr.read_score(fr)
            if sc is not None:
                rep['score'] = sc
            if rep['mode'] == 'classic':
                rep['lives_remaining'] = self._lives(fr)
            try:
                rep['objects'] = self._detect_objects()
            except Exception:
                pass
        elif st == 'game_over':
            sc = self.ocr.read_final_score(fr)
            if sc is not None:
                rep['final_score'] = sc
        return rep

ENGINE = Engine()

# ---------------------------------------------------------------- MCP
TOOLS = [
    {'name': 'get_state',
     'description': 'Observe the game screen. Returns the current screen state '
       '(main_menu, round, game_over, paused, unknown) and, during a round, the '
       'mode (classic or timer), current score, lives remaining (classic), and '
       'the positions of fruits and bombs currently on screen. On the game_over '
       'screen it returns the final score. Coordinates are screen pixels: the '
       'game window is 1024x768, origin top-left, x right, y down.',
     'inputSchema': {'type': 'object', 'properties': {}, 'required': []}},
    {'name': 'start_round',
     'description': 'Navigate the menus to start a round of the given mode. '
       'If a round is already running it is left untouched and reported. '
       'Modes: arcade (60s, bonus bananas - double points/frenzy/freeze, bombs '
       'cost 10 points; usually the highest score), zen (90s, no bombs, safe), '
       'classic (no time limit but 3 missed fruits or one sliced bomb ends it; '
       'highest potential but risky). Returns when the round has started.',
     'inputSchema': {'type': 'object', 'properties': {
        'mode': {'type': 'string', 'enum': ['classic', 'zen', 'arcade'],
                 'default': 'arcade'}}, 'required': []}},
    {'name': 'play',
     'description': 'Autonomously play the current round in real time: watches '
       'the screen, tracks fruits and bombs, and performs blade strokes to slice '
       'fruits (aiming for multi-fruit combos) while avoiding bombs. If no round '
       'is running, one is started (see mode). With rounds > 1 it plays that '
       'many rounds back-to-back (slicing RETRY between them) and reports the '
       'best score - a good way to reach a high score. Blocks until the round(s) '
       'end or duration_seconds elapse, then returns the best score, all round '
       'scores, and statistics (swipes, frames, best planned combo, mode).',
     'inputSchema': {'type': 'object', 'properties': {
        'duration_seconds': {'type': 'number', 'default': 120},
        'mode': {'type': 'string', 'enum': ['classic', 'zen', 'arcade'],
                 'default': 'arcade'},
        'rounds': {'type': 'integer', 'default': 1,
                   'description': 'play this many rounds back-to-back (via '
                   'RETRY) and report the best score; duration_seconds is the '
                   'total time budget across all rounds'}}, 'required': []}},
    {'name': 'swipe',
     'description': 'Perform a single blade stroke from (x1,y1) to (x2,y2) in '
       'screen pixels (1024x768, origin top-left). The stroke lasts about 200ms '
       'so the game registers it. Use for menu navigation or manual play.',
     'inputSchema': {'type': 'object', 'properties': {
        'x1': {'type': 'number'}, 'y1': {'type': 'number'},
        'x2': {'type': 'number'}, 'y2': {'type': 'number'}},
        'required': ['x1', 'y1', 'x2', 'y2']}},
    {'name': 'stop_play',
     'description': 'Stop an ongoing autonomous play loop early.',
     'inputSchema': {'type': 'object', 'properties': {}, 'required': []}},
    {'name': 'game_control',
     'description': 'Control the game process via fn-app. Actions: status, '
       'start, stop, restart. Only use restart/start if the game has crashed or '
       'is not running; never to interrupt a running round.',
     'inputSchema': {'type': 'object', 'properties': {
        'action': {'type': 'string', 'enum': ['status', 'start', 'stop', 'restart']}},
        'required': ['action']}},
]

PLAY_LOCK = threading.Lock()
def tool_call(name, args):
    if name == 'get_state':
        return ENGINE.get_state_report()
    if name == 'start_round':
        with PLAY_LOCK:
            ok, msg = ENGINE.start_round(args.get('mode', 'arcade'))
        return {'ok': ok, 'message': msg, 'state': ENGINE.get_state_report()}
    if name == 'play':
        with PLAY_LOCK:
            return ENGINE.play(float(args.get('duration_seconds', 120)),
                               args.get('mode', 'arcade'),
                               int(args.get('rounds', 1)))
    if name == 'swipe':
        with PLAY_LOCK:
            ENGINE.ensure()
            ENGINE.swiper.swipe(float(args['x1']), float(args['y1']),
                                float(args['x2']), float(args['y2']), wait=True)
        return {'ok': True}
    if name == 'stop_play':
        ENGINE.stop_play()
        return {'ok': True}
    if name == 'game_control':
        r = subprocess.run(['fn-app', args['action']], capture_output=True, text=True)
        return {'stdout': r.stdout.strip(), 'stderr': r.stderr.strip(),
                'code': r.returncode}
    return {'error': f'unknown tool {name}'}

INSTRUCTIONS = """This server plays Fruit Ninja HD (Windows game under Wine) in real time.

Screen coordinates: the game window is 1024x768 pixels, origin at top-left, x increases right, y increases down. All positions and strokes use these pixels.

Typical flow:
1. Call get_state to see what screen the game is on.
2. If no round is running, call start_round with a mode. arcade (60 seconds, with double-points/frenzy/freeze bonus bananas; bombs cost 10 points) usually gives the highest score and is the default. zen (90s, no bombs) is safe. classic (no time limit, but 3 missed fruits or one sliced bomb ends the round) has the highest theoretical potential but is risky - the bot currently survives it only briefly.
3. Call play to let the server play autonomously in real time. play blocks while it plays and returns the score and statistics when the round ends or the duration cap is reached. Because the server makes its own fresh observations many times per second, your response latency does not affect play quality. Call play again to continue a long round. Scores vary from round to round (bonus-banana luck in arcade); playing several rounds and keeping the best is a good way to reach a high score. get_state on the game_over screen reads the exact final score.
4. Use swipe for manual strokes or menu navigation if you want direct control, and get_state to check results.

The server never pauses, restarts or leaves a running round. game_control is only for recovering a crashed game."""

SEND_LOCK = threading.Lock()
def send(msg):
    with SEND_LOCK:
        sys.stdout.write(json.dumps(msg) + '\n')
        sys.stdout.flush()

def main():
    for line in sys.stdin:
        line = line.strip()
        if not line: continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue
        mid = req.get('id')
        method = req.get('method')
        if method == 'initialize':
            send({'jsonrpc': '2.0', 'id': mid, 'result': {
                'protocolVersion': '2024-11-05',
                'capabilities': {'tools': {}},
                'serverInfo': {'name': 'fruit-ninja', 'version': '1.0.0'},
                'instructions': INSTRUCTIONS}})
        elif method == 'notifications/initialized':
            pass
        elif method == 'tools/list':
            send({'jsonrpc': '2.0', 'id': mid, 'result': {'tools': TOOLS}})
        elif method == 'tools/call':
            params = req.get('params', {})
            name = params.get('name')
            args = params.get('arguments', {}) or {}
            def run(name=name, args=args, mid=mid):
                try:
                    result = tool_call(name, args)
                    send({'jsonrpc': '2.0', 'id': mid, 'result': {
                        'content': [{'type': 'text', 'text': json.dumps(result, indent=2)}]}})
                except Exception as e:
                    log('tool error', name, e)
                    send({'jsonrpc': '2.0', 'id': mid, 'result': {
                        'content': [{'type': 'text', 'text': json.dumps({'error': str(e)})}],
                        'isError': True}})
            threading.Thread(target=run, daemon=True).start()
        elif mid is not None:
            send({'jsonrpc': '2.0', 'id': mid, 'error': {
                'code': -32601, 'message': f'method not found: {method}'}})

if __name__ == '__main__':
    main()
