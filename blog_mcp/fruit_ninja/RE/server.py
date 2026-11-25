#!/usr/bin/env python3
"""MCP server for playing Fruit Ninja HD (Windows game under Wine) in real time.

Speaks MCP JSON-RPC 2.0 on stdin/stdout. Diagnostics go to stderr.
All game facts are read live from the game's memory (read-only); input is
injected through X11 XTest, the game's own mouse input path.
"""
import sys, json, time, traceback

sys.stderr.write('fn-mcp starting\n')

from game import Game, w2sx, w2sy, s2wx, s2wy, detect_screen, find_menu_fruit
from controller import Controller

GAME = None
CTRL = None
STARTED_MODE = None

def game():
    global GAME
    if GAME is None:
        GAME = Game()
    return GAME

def controller():
    global CTRL
    if CTRL is None:
        CTRL = Controller()
    return CTRL

MODE_NAMES = {0: 'classic', 1: 'zen', 2: 'arcade', 3: 'mode3', 4: 'menu'}

TOOLS = [
    {
        'name': 'get_state',
        'description': (
            'Read the live game state from memory. Returns the current mode, score, '
            'whether a round is actively running, and every object in flight. '
            'Objects are reported in SCREEN pixel coordinates (origin top-left, x right, '
            'y down, 1024x768) with screen velocity (px/s), kind (fruit type name or "bomb"), '
            'collision radius (px), point value, and whether already sliced. '
            'Fruits you can still slice have sliced=false. Bombs end a Classic round (or cost '
            'points in Arcade) if your slash touches them.'
        ),
        'inputSchema': {'type': 'object', 'properties': {}, 'required': []},
    },
    {
        'name': 'slash',
        'description': (
            'Perform one straight mouse slash from (x1,y1) to (x2,y2) in SCREEN pixels. '
            'Any unsliced fruit whose centre lies within its collision radius of the segment '
            'is sliced; slicing multiple fruits in one slash scores a combo bonus. '
            'The slash also touches bombs if they are within their radius of the segment, so '
            'keep segments clear of bombs. Returns immediately after the slash is delivered.'
        ),
        'inputSchema': {
            'type': 'object',
            'properties': {
                'x1': {'type': 'number', 'description': 'start x, screen px'},
                'y1': {'type': 'number', 'description': 'start y, screen px'},
                'x2': {'type': 'number', 'description': 'end x, screen px'},
                'y2': {'type': 'number', 'description': 'end y, screen px'},
            },
            'required': ['x1', 'y1', 'x2', 'y2'],
        },
    },
    {
        'name': 'auto_play',
        'description': (
            'Let the server play autonomously in real time for the given number of seconds. '
            'A fast control loop reads fresh object positions from memory many times per second, '
            'leads fruit motion, seeks multi-fruit combos, avoids bombs, and delivers slashes '
            'through the game\'s mouse input. Use this to score without your own latency mattering. '
            'Call it while a round is running. Returns the score before and after plus slash stats. '
            'The game keeps running in real time; this call blocks for the requested duration.'
        ),
        'inputSchema': {
            'type': 'object',
            'properties': {
                'seconds': {'type': 'number', 'description': 'how long to play, seconds (1-600)'},
                'mode': {'type': 'string', 'enum': ['classic', 'zen', 'arcade'], 'description': 'optional: if no round is currently running, start this mode first'},
                'stop_score': {'type': 'number', 'description': 'optional: stop early once score reaches this'},
            },
            'required': ['seconds'],
        },
    },
    {
        'name': 'start_mode',
        'description': (
            'Navigate the menus to start a round of the given mode ("classic", "zen" or "arcade"). '
            'Works from the main menu, the mode-select screen, or a game-over screen. '
            'Slices the appropriate menu fruit through the game\'s input. Returns the resulting state. '
            'If a round is already running it is left untouched and the current state is returned.'
        ),
        'inputSchema': {
            'type': 'object',
            'properties': {
                'mode': {'type': 'string', 'enum': ['classic', 'zen', 'arcade']},
            },
            'required': ['mode'],
        },
    },
    {
        'name': 'restart_round',
        'description': (
            'From a game-over screen, slice the RETRY fruit to start the same mode again. '
            'Returns the resulting state. Does nothing harmful if a round is already running.'
        ),
        'inputSchema': {'type': 'object', 'properties': {}, 'required': []},
    },
]


def obj_summary(o):
    d = {
        'kind': o['kind'],
        'x': round(o['sx'], 1),
        'y': round(o['sy'], 1),
        'vx': round(o['vx'] * 64.0, 1),
        'vy': round(-o['vy'] * 64.0, 1),
        'radius': round(o['radius'] * 2.1333333, 1),
        'sliced': bool(o['sliced']),
    }
    if not o['bomb']:
        d['score'] = o.get('score', 1)
        if o.get('special'):
            d['special'] = True
    return d


def do_get_state():
    g = game()
    fruits, bombs = g.read_objects()
    mode = g.mode()
    score = g.score()
    active = [f for f in fruits if not f['sliced']] + [b for b in bombs if not b['sliced']]
    in_round = len(active) > 0
    scr = detect_screen(fruits, bombs)
    # mode: 2 is arcade; otherwise report the mode we started (classic/zen) if known
    if mode == 2:
        mode_name = 'arcade'
    elif STARTED_MODE in ('classic', 'zen') and scr == 'round':
        mode_name = STARTED_MODE
    elif scr == 'round':
        mode_name = 'classic_or_zen'
    else:
        mode_name = MODE_NAMES.get(mode, str(mode))
    return {
        'screen': scr,
        'mode': mode_name,
        'mode_code': mode,
        'score': score,
        'objects_in_flight': len(active),
        'round_active_hint': in_round,
        'fruits': [obj_summary(f) for f in fruits],
        'bombs': [obj_summary(b) for b in bombs],
        'note': 'screen px, origin top-left, y down, 1024x768; velocity px/s (vy>0 means moving down)',
    }


def do_slash(a):
    c = controller()
    c.g.x.slash(float(a['x1']), float(a['y1']), float(a['x2']), float(a['y2']))
    return {'ok': True}


def do_auto_play(a):
    global STARTED_MODE
    seconds = max(1.0, min(600.0, float(a['seconds'])))
    stop = a.get('stop_score')
    want = a.get('mode')
    g = game()
    if want in ('classic', 'zen', 'arcade'):
        fruits, bombs = g.read_objects()
        if detect_screen(fruits, bombs) != 'round':
            do_start_mode({'mode': want})
    c = controller()
    c.stats = {'slashes': 0, 'fruits_hit': 0, 'combos': 0}
    res = c.play(seconds, stop_score=stop)
    res['mode'] = STARTED_MODE or MODE_NAMES.get(g.mode(), str(g.mode()))
    return res


# ---- menu navigation ----


def slash_fruit(f):
    """Slash through a menu fruit's actual position."""
    controller().g.x.slash(f['sx'] - 90, f['sy'], f['sx'] + 90, f['sy'], steps=10, step_delay=0.02)


def current_screen():
    g = game()
    fruits, bombs = g.read_objects()
    return detect_screen(fruits, bombs), fruits, bombs


def wait_for_screen(want, timeout=5.0):
    g = game()
    t0 = time.time()
    while time.time() - t0 < timeout:
        fruits, bombs = g.read_objects()
        scr = detect_screen(fruits, bombs)
        if scr == want:
            return scr, fruits, bombs
        time.sleep(0.2)
    fruits, bombs = g.read_objects()
    return detect_screen(fruits, bombs), fruits, bombs


def do_start_mode(a):
    global STARTED_MODE
    target = a['mode']
    g = game()
    # if a round seems active, leave it
    fruits, bombs = g.read_objects()
    moving = [f for f in fruits if not f['sliced'] and (abs(f['vx']) > 0.5 or abs(f['vy']) > 0.5)]
    moving += [b for b in bombs if not b['sliced'] and (abs(b['vx']) > 0.5 or abs(b['vy']) > 0.5)]
    scr = detect_screen(fruits, bombs)
    if scr == 'round':
        return {'started': False, 'reason': 'a round already appears active; left untouched',
                'screen': scr, 'state': do_get_state()}
    path = []
    for _ in range(4):
        fruits, bombs = g.read_objects()
        scr = detect_screen(fruits, bombs)
        path.append(scr)
        if scr == 'round':
            break
        if scr == 'game_over':
            b = [b for b in bombs if not b['sliced'] and abs(b['sx'] - 683) < 80 and abs(b['sy'] - 631) < 80]
            if b:
                controller().g.x.slash(b[0]['sx'] - 90, b[0]['sy'], b[0]['sx'] + 90, b[0]['sy'], steps=10, step_delay=0.02)
            scr, _, _ = wait_for_screen('main_menu', 4.0)
            path.append(scr)
            continue
        if scr == 'main_menu':
            f = find_menu_fruit(fruits, 'main_new_game')
            if f:
                slash_fruit(f)
            scr, _, _ = wait_for_screen('mode_select', 4.0)
            path.append(scr)
            continue
        if scr == 'mode_select':
            f = find_menu_fruit(fruits, 'mode_' + target)
            if f:
                slash_fruit(f)
            scr, _, _ = wait_for_screen('round', 4.0)
            path.append(scr)
            break
        time.sleep(0.8)
    fruits, bombs = g.read_objects()
    scr = detect_screen(fruits, bombs)
    if scr == 'round':
        STARTED_MODE = target
    return {'started': scr == 'round', 'mode': target, 'path': path, 'screen': scr,
            'state': do_get_state()}


def do_restart_round():
    g = game()
    for _ in range(4):
        fruits, bombs = g.read_objects()
        scr = detect_screen(fruits, bombs)
        if scr == 'round':
            break
        if scr == 'game_over':
            f = find_menu_fruit(fruits, 'gameover_retry')
            if f:
                slash_fruit(f)
            time.sleep(1.2)
            continue
        time.sleep(0.8)
    fruits, bombs = g.read_objects()
    return {'restarted': detect_screen(fruits, bombs) == 'round', 'state': do_get_state()}


HANDLERS = {
    'get_state': lambda a: do_get_state(),
    'slash': do_slash,
    'auto_play': do_auto_play,
    'start_mode': do_start_mode,
    'restart_round': lambda a: do_restart_round(),
}

INSTRUCTIONS = (
    'This server plays Fruit Ninja HD in real time, reading the game state straight from the '
    'running process and acting through the game\'s own mouse input. Coordinates are SCREEN '
    'pixels (origin top-left, x right, y down, 1024x768). The game runs continuously in real '
    'time and never pauses for you, and your own replies are slow compared to the action, so '
    'the auto_play tool is the way to score: its control loop observes and slashes many times '
    'per second by itself, leading fruit motion, seeking combos and avoiding bombs. '
    'Classic mode gives the highest scores (no time limit; the round ends only after 3 fruits '
    'fall unsliced or a bomb is sliced), so a good plan is auto_play with mode="classic" and a '
    'long duration, then restart_round and repeat if the round ends. Arcade (60s) and Zen (90s) '
    'are timed. Use get_state to check the screen/mode/score/objects, start_mode to begin a '
    'specific mode, restart_round after a game over, and slash for precise manual actions. '
    'Slicing 3+ fruits in one slash scores a combo bonus; the rare dragonfruit is worth 50.'
)


def send(msg):
    sys.stdout.write(json.dumps(msg) + '\n')
    sys.stdout.flush()


def result(id_, res):
    send({'jsonrpc': '2.0', 'id': id_, 'result': res})


def error(id_, code, message):
    send({'jsonrpc': '2.0', 'id': id_, 'error': {'code': code, 'message': message}})


def tool_result(id_, value):
    text = json.dumps(value, indent=2)
    result(id_, {'content': [{'type': 'text', 'text': text}]})


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except Exception:
            continue
        mid = msg.get('id')
        method = msg.get('method')
        params = msg.get('params') or {}
        try:
            if method == 'initialize':
                result(mid, {
                    'protocolVersion': '2024-11-05',
                    'capabilities': {'tools': {}},
                    'serverInfo': {'name': 'fruit-ninja-mcp', 'version': '1.0.0'},
                    'instructions': INSTRUCTIONS,
                })
            elif method == 'notifications/initialized':
                pass
            elif method == 'tools/list':
                result(mid, {'tools': TOOLS})
            elif method == 'tools/call':
                name = params.get('name')
                args = params.get('arguments') or {}
                if name not in HANDLERS:
                    error(mid, -32602, 'unknown tool ' + str(name))
                else:
                    tool_result(mid, HANDLERS[name](args))
            elif method == 'ping':
                result(mid, {})
            else:
                if mid is not None:
                    error(mid, -32601, 'method not found: ' + str(method))
        except Exception as e:
            traceback.print_exc(file=sys.stderr)
            if mid is not None:
                error(mid, -32000, str(e))


if __name__ == '__main__':
    main()
