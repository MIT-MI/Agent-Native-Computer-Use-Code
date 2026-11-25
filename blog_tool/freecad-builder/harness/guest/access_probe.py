"""Try each way of reaching the application, as whoever runs this, and print what happened.

Used by the driver's model-free checks: it decides which attempts should have succeeded. Every
attempt is harmless: reads, a signal 0, an attach that ends with the attempt.
Rows: {"probe", "kind", "succeeded", "detail"}; kinds are files, process, other (processes that are
not the application), capability, modules, display, bus, tool, typing (code entered through the
window helper), door, ocr and writable.
"""
import base64
import ctypes
import json
import os
from pathlib import Path
import shutil
import socket
import struct
import subprocess
import sys

rows = []


def attempt(kind, probe, action):
    try: succeeded, detail = True, action()
    except BaseException as exc: succeeded, detail = False, type(exc).__name__ + ': ' + str(exc)
    if isinstance(detail, tuple): succeeded, detail = detail
    rows.append({'probe': probe, 'kind': kind, 'succeeded': bool(succeeded), 'detail': str(detail)[:400]})


def run(argv, **options):
    row = subprocess.run(argv, capture_output=True, text=True, timeout=60, **options)
    return row.returncode == 0, 'exit %d: %s' % (row.returncode, (row.stdout + row.stderr).strip()[-300:])


def read_some(path):
    with open(path, 'rb') as stream: return '%d bytes read' % len(stream.read(64))


def readable(paths):
    opened = []
    for path in paths:
        try:
            with open(path, 'rb') as stream: stream.read(1)
            opened.append(path)
        except OSError: pass
    return bool(opened), '%d of %d readable%s' % (len(opened), len(paths), ': ' + ', '.join(opened[:5]) if opened else '')


def first_mapping(pid):
    with open('/proc/%d/maps' % pid) as stream: return int(stream.readline().split('-')[0], 16)


def read_memory(pid, address):
    fd = os.open('/proc/%d/mem' % pid, os.O_RDONLY)
    try: return '%d bytes read at 0x%x' % (len(os.pread(fd, 64, address or first_mapping(pid))), address or first_mapping(pid))
    finally: os.close(fd)


def read_vm(pid, address):
    libc = ctypes.CDLL(None, use_errno=True)
    class IOVec(ctypes.Structure): _fields_ = [('base', ctypes.c_void_p), ('length', ctypes.c_size_t)]
    if not address:
        try: address = first_mapping(pid)
        except OSError: address = 0x400000  # the kernel checks permission before the address
    buffer = ctypes.create_string_buffer(64)
    local, remote = IOVec(ctypes.cast(buffer, ctypes.c_void_p), 64), IOVec(address, 64)
    libc.process_vm_readv.restype = ctypes.c_ssize_t
    count = libc.process_vm_readv(pid, ctypes.byref(local), 1, ctypes.byref(remote), 1, 0)
    if count < 0: raise OSError(ctypes.get_errno(), os.strerror(ctypes.get_errno()))
    return '%d bytes read' % count


def capabilities():
    with open('/proc/self/status') as stream:
        held = dict(line.split() for line in stream if line.startswith(('CapInh', 'CapPrm', 'CapEff', 'CapAmb')))
    return any(int(value, 16) for value in held.values()), held


def window(tool, arguments):
    with socket.socket(socket.AF_UNIX) as client:
        client.connect('/opt/cua-world-runtime/input.sock')
        stream = client.makefile('rwb')
        stream.write(json.dumps({'tool': tool, 'arguments': arguments}).encode() + b'\n'); stream.flush()
        return json.loads(stream.readline())


def read_known_text(expected='OCR CHECK 4821'):
    """OCR a picture of known text, drawn here."""
    from PIL import Image, ImageDraw
    import tesserocr
    image = Image.new('RGB', (220, 24), 'white')
    ImageDraw.Draw(image).text((4, 6), expected, fill='black')
    text = tesserocr.image_to_text(image.resize((image.width * 4, image.height * 4), Image.LANCZOS)).strip()
    return text == expected, repr(text)


def read_window():
    """OCR what the desktop shows, through the window helper where that is the only access: are there words?"""
    import io
    import re
    from PIL import Image
    import tesserocr
    if shutil.which('cua-input'): data = base64.b64decode(window('screenshot', {})['image'])
    else: data = subprocess.run(['import', '-window', 'root', 'png:-'], capture_output=True, check=True, timeout=60).stdout
    image = Image.open(io.BytesIO(data)).convert('RGB')
    words = re.findall(r'[A-Za-z]{4,}', tesserocr.image_to_text(image.resize((image.width * 2, image.height * 2), Image.LANCZOS)))
    return len(words) >= 5, '%d words read: %s' % (len(words), ' '.join(words[:12]))


def enter(inputs):
    """Start a new line, send the inputs in order and end the line: (all were entered, which were refused)."""
    window('key', {'keys': ['Return']})
    refused = [repr(arguments.get('text', '+'.join(arguments.get('keys', [])))) for tool, arguments in inputs
               if not window(tool, arguments).get('executed')]
    window('key', {'keys': ['Return']})
    return not refused, 'refused: ' + ' '.join(refused) if refused else 'all %d inputs entered' % len(inputs)


ATTACH = ('import ctypes, os, sys\nlibc = ctypes.CDLL(None, use_errno=True)\n'
          'if libc.ptrace(0x4206, int(sys.argv[1]), 0, 0): sys.exit(os.strerror(ctypes.get_errno()))\n')  # PTRACE_SEIZE; exit detaches


def x_handshake(address):
    """An X11 connection setup without authorization data: the server's first byte is 1 only if it lets us in."""
    with socket.socket(socket.AF_UNIX) as client:
        client.settimeout(10); client.connect(address)
        client.sendall(struct.pack('<BxHHHH2x', 0x6c, 11, 0, 0, 0))
        reply = client.recv(8)
        return reply[:1] == b'\x01', 'server answered %r' % reply[:1]


def shared_writable(private):
    """Folders this user can write that are not its own private ones."""
    found = []
    for root in ('/', '/run', '/var', '/home', '/opt', '/srv'):
        try: device = os.stat(root).st_dev
        except OSError: continue
        for folder, dirs, _ in os.walk(root):
            dirs[:] = [d for d in dirs if not os.path.islink(os.path.join(folder, d))]
            if any(folder == p or folder.startswith(p + '/') for p in private):
                dirs[:] = []; continue
            try:
                if os.stat(folder).st_dev != device: dirs[:] = []; continue
            except OSError: continue
            if os.access(folder, os.W_OK) and folder not in found: found.append(folder)
    return bool(found), ', '.join(found[:20]) or 'none'


def main(config):
    pid, display = config['pid'], config['environment'].get('DISPLAY', '')
    for app in config['applications']:
        attempt('files', 'read application binary ' + app['binary'], lambda: read_some(app['binary']))
        attempt('modules', 'run application binary ' + app['binary'], lambda: run([app['binary'], '--version']))
        for root in app.get('install_roots', []): attempt('files', 'list ' + root, lambda: '%d entries' % len(os.listdir(root)))
    attempt('files', 'read package files', lambda: readable(config['package_files']))
    for path in config['closed']: attempt('files', 'list ' + path, lambda: '%d entries' % len(os.listdir(path)))
    for path in config['files']: attempt('files', 'read ' + path, lambda: read_some(path))
    attempt('process', 'read /proc/PID/mem', lambda: read_memory(pid, config.get('address')))
    for name in ('maps', 'environ', 'smaps'): attempt('process', 'read /proc/PID/' + name, lambda: read_some('/proc/%d/%s' % (pid, name)))
    for name in ('exe', 'cwd', 'root'): attempt('process', 'readlink /proc/PID/' + name, lambda: os.readlink('/proc/%d/%s' % (pid, name)))
    attempt('process', 'list /proc/PID/fd', lambda: '%d entries' % len(os.listdir('/proc/%d/fd' % pid)))
    attempt('process', 'ptrace attach', lambda: run([sys.executable, '-c', ATTACH, str(pid)]))
    attempt('process', 'process_vm_readv', lambda: read_vm(pid, config.get('address')))
    attempt('process', 'signal 0', lambda: os.kill(pid, 0))
    for label, other in config.get('others', {}).items():
        attempt('other', 'read memory of ' + label, lambda: read_memory(other, None))
        attempt('other', 'ptrace attach to ' + label, lambda: run([sys.executable, '-c', ATTACH, str(other)]))
        attempt('other', 'process_vm_readv of ' + label, lambda: read_vm(other, None))
    attempt('capability', 'hold a capability', capabilities)
    imports = config.get('python_imports', {})
    for module in imports.get('modules', []):
        source = 'import sys; sys.path[:0] = %r; import %s' % (imports.get('paths', []), module)
        attempt('modules', 'python import ' + module, lambda: run([sys.executable, '-c', source]))
    for argv in config.get('commands', []): attempt('modules', 'run ' + ' '.join(argv), lambda: run(argv))
    number = display.lstrip(':').split('.')[0]
    for address in ('/tmp/.X11-unix/X' + number, '\0/tmp/.X11-unix/X' + number):
        attempt('display', 'connect to X at ' + address.replace('\0', '@'), lambda: x_handshake(address))
    attempt('display', 'xdotool on the display', lambda: run(['xdotool', 'getmouselocation'], env={**os.environ, 'DISPLAY': display}))
    attempt('display', 'xdotool with the desktop user\'s authority file',
            lambda: run(['xdotool', 'getmouselocation'], env={**os.environ, **config['environment']}))
    for name, address in config['buses'].items():
        attempt('bus', 'call the bus: ' + name, lambda: run(['dbus-send', '--bus=' + address, '--print-reply',
                '--dest=org.freedesktop.DBus', '/org/freedesktop/DBus', 'org.freedesktop.DBus.ListNames']))
    for tool in config['analysis_tools']: attempt('tool', tool, lambda: (bool(shutil.which(tool)), shutil.which(tool) or 'absent'))
    if config.get('ocr'):
        attempt('ocr', 'read known text with OCR', read_known_text)
        attempt('ocr', 'read the window with OCR', read_window)
    attempt('door', 'cua-app status', lambda: (lambda row: (row[0] and '"pids": [' in row[1] and '"pids": []' not in row[1], row[1]))(run(['cua-app', 'status'])))
    if shutil.which('cua-input'):
        shot = os.path.join(os.environ.get('HOME', '/tmp'), 'access-probe.png')
        attempt('door', 'cua-input screenshot', lambda: (run(['cua-input', 'screenshot', shot])[0] and Path(shot).read_bytes()[:8] == b'\x89PNG\r\n\x1a\n',
                                                         '%d bytes' % Path(shot).stat().st_size))
        attempt('door', 'cua-input key', lambda: run(['cua-input', 'key', 'Escape']))
        attempt('door', 'cua-input click', lambda: run(['cua-input', 'click', *map(str, config['click'])]))
        attempt('door', 'cua-input type_text', lambda: run(['cua-input', 'type_text', '1']))
        attempt('door', 'cua-input scroll', lambda: run(['cua-input', 'scroll', 'down', '1']))
        attempt('door', 'cua-input refuses typed code', lambda: (lambda row: (not row[0], row[1]))(enter([('type_text', {'text': config['code']})])))
        # The filter must hold for the line being entered, however the text arrives.
        chords = config['chords']
        for sample in config['typed_code']:
            text = sample['text']
            attempt('typing', 'enter %r key by key' % text, lambda: enter([('key', {'keys': chords[c]['keys']}) for c in text]))
            attempt('typing', 'enter %r by key codes' % text, lambda: enter([('key', {'keys': chords[c]['codes']}) for c in text]))
            attempt('typing', 'enter %r as %r' % (text, sample['pieces']), lambda: enter([('type_text', {'text': p}) for p in sample['pieces']]))
        for text in config['typed_values']:
            attempt('door', 'type %r' % text, lambda: enter([('type_text', {'text': text})]))
            attempt('door', 'enter %r key by key' % text, lambda: enter([('key', {'keys': chords[c]['keys']}) for c in text]))
        fields = [step for text in config['typed_values'] for step in (('type_text', {'text': text}), ('key', {'keys': ['Tab']}))]
        attempt('door', 'type the values into successive fields', lambda: enter(fields))
    for folder in config['writable']:
        path = os.path.join(folder, 'access-probe.txt')
        attempt('door', 'write ' + folder, lambda: (Path(path).write_text('probe'), Path(path).read_text())[1])
    attempt('door', 'run ordinary programs', lambda: run(['/bin/sh', '-c', 'python3 -c "print(1)" && ls / > /dev/null']))
    if 'private' in config: attempt('writable', 'shared writable folders', lambda: shared_writable(config['private']))
    print(json.dumps(rows))


if __name__ == '__main__': main(json.loads(Path(sys.argv[1]).read_text()))
