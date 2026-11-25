"""GUI observations and structured input over direct exec."""
import base64
import hashlib
import shlex
import uuid

KEYS = {'CTRL': 'ctrl', 'CONTROL': 'ctrl', 'ALT': 'alt', 'SHIFT': 'shift', 'SUPER': 'super',
        'ENTER': 'Return', 'RETURN': 'Return', 'ESC': 'Escape', 'ESCAPE': 'Escape',
        'TAB': 'Tab', 'BACKSPACE': 'BackSpace', 'DELETE': 'Delete', 'SPACE': 'space',
        'UP': 'Up', 'DOWN': 'Down', 'LEFT': 'Left', 'RIGHT': 'Right', 'HOME': 'Home', 'END': 'End',
        'PAGEUP': 'Prior', 'PAGEDOWN': 'Next'}


class GUI:
    def __init__(self, runner, folder):
        self.runner, self.folder = runner, folder
        self.steps = 0

    def screenshot(self):
        path = self.folder / ('screen-' + uuid.uuid4().hex + '.png')
        self.runner.capture_screenshot(path)
        data = path.read_bytes()
        if not data.startswith(b'\x89PNG\r\n\x1a\n'):
            raise RuntimeError('screenshot is not PNG')
        return {'image': base64.b64encode(data).decode(), 'mimeType': 'image/png',
                'path': str(path), 'sha256': hashlib.sha256(data).hexdigest(), 'step': self.steps}

    def input(self, action):
        self.steps += 1
        self.runner.inject_action(action)
        return {'executed': True, 'step': self.steps}

    def click(self, x, y):
        return self.input({'mouse': {'left_click': [x, y]}})

    def right_click(self, x, y):
        return self.input({'mouse': {'right_click': [x, y]}})

    def double_click(self, x, y):
        return self.input({'mouse': {'double_click': [x, y]}})

    def drag(self, x1, y1, x2, y2):
        return self.input({'mouse': {'left_click_drag': [[x1,y1],[x2,y2]]}})

    def type_text(self, text):
        return self.input({'keyboard': {'text': text}})

    def scroll(self, direction, count):
        return self.input({'mouse': {'scroll': [direction, count]}})

    def key(self, keys):
        normalized = [KEYS.get(k.upper(), k) for k in keys]
        return self.input({'keyboard': {'keys': normalized}})

    def tools(self):
        return {name: getattr(self, name) for name in ('screenshot', 'click', 'right_click', 'double_click', 'drag', 'type_text', 'scroll', 'key')}


def action_commands(action, resolution):
    """Only structured literals reach exec; never model-authored shell/Python."""
    import math
    def point(value):
        if not isinstance(value,list) or len(value)!=2 or any(type(v) is not int or not 0<=v<resolution[i] for i,v in enumerate(value)):
            raise ValueError('invalid pointer coordinates')
        return f'{value[0]} {value[1]}'
    if set(action)=={'action','time'} and action['action']=='wait':
        value=float(action['time'])
        if not math.isfinite(value) or value<0:raise ValueError('invalid wait')
        return ['sleep '+str(value)]
    if set(action)=={'mouse'}:
        mouse=action['mouse']
        if len(mouse)!=1:raise ValueError('one mouse action required')
        key,value=next(iter(mouse.items()))
        if key=='move':return ['xdotool mousemove --sync '+point(value)]
        if key in ('left_click','right_click','middle_click'):
            return ['xdotool mousemove --sync '+point(value)+' click '+str({'left_click':1,'middle_click':2,'right_click':3}[key])]
        if key=='double_click':return ['xdotool mousemove --sync '+point(value)+' click --repeat 2 1']
        if key=='buttons':
            commands=[]
            for button,pressed in value.items():
                if pressed is not True or button not in {b+'_'+s for b in ('left','middle','right') for s in ('up','down')}:raise ValueError('invalid button state')
                b,s=button.split('_');commands.append('xdotool mouse'+s+' '+str({'left':1,'middle':2,'right':3}[b]))
            return commands
        if key=='left_click_drag':
            if not isinstance(value,list) or not value:raise ValueError('invalid drag')
            points=[point(p) for p in value]
            return ['xdotool mousemove --sync '+points[0]+' mousedown 1 '+ ' '.join('mousemove --sync '+p for p in points[1:])+' mouseup 1']
        if key=='scroll':
            if type(value) is int:direction,count=('down' if value>=0 else 'up'),abs(value)
            else:direction,count=value
            if direction not in ('up','down','left','right') or type(count) is not int or count<0:raise ValueError('invalid scroll')
            if count == 0: return []
            return [f'xdotool click --repeat {count} '+str({'up':4,'down':5,'left':6,'right':7}[direction])]
    if set(action)=={'keyboard'} and len(action['keyboard'])==1:
        key,value=next(iter(action['keyboard'].items()))
        if key=='text':
            if not isinstance(value,str) or '\0' in value:raise ValueError('invalid text')
            return ['xdotool type --clearmodifiers -- '+shlex.quote(value)]
        if key in ('keys','keys_down','keys_up'):
            if not isinstance(value,list) or not value or not all(isinstance(k,str) and '\0' not in k for k in value):raise ValueError('invalid keys')
            verb={'keys':'key','keys_down':'keydown','keys_up':'keyup'}[key]
            return ['xdotool '+verb+' -- '+shlex.quote('+'.join(value))]
    raise ValueError('unsupported GUI input action')
