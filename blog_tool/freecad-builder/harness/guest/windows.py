"""Capture and restore window frames when a filesystem image is booted.

This preserves setup state and existing frames across application restarts;
it exposes no keys, pointer input, text entry or arbitrary window operations.
"""
import json
from pathlib import Path
import subprocess
import sys


def capture(binary=None):
    rows = []
    text = subprocess.check_output(['wmctrl', '-lpG'], text=True)
    for line in text.splitlines():
        fields = line.split(None, 8)
        if len(fields) != 9: continue
        try: executable = str((Path('/proc') / fields[2] / 'exe').resolve(strict=True))
        except OSError: continue
        if binary and executable != str(Path(binary).resolve()): continue
        state = subprocess.check_output(['xprop', '-id', fields[0], '_NET_WM_STATE'], text=True)
        rows.append({'id': fields[0], 'binary': executable, 'title': fields[8],
                     'geometry': list(map(int, fields[3:7])),
                     'maximized': all(s in state for s in ('_NET_WM_STATE_MAXIMIZED_VERT', '_NET_WM_STATE_MAXIMIZED_HORZ'))})
    return rows


def restore(saved, binary):
    current = capture(binary)
    remaining = list(current)
    for old in saved:
        if not remaining: break
        window = next((w for w in remaining if w['title'] == old['title']), remaining[0])
        remaining.remove(window)
        subprocess.run(['wmctrl', '-ir', window['id'], '-e', ','.join(map(str, [0, *old['geometry']]))], check=True)
        if old['maximized']:
            subprocess.run(['wmctrl', '-ir', window['id'], '-b', 'add,maximized_vert,maximized_horz'], check=True)
    return capture(binary)


if __name__ == '__main__':
    print(json.dumps(capture(sys.argv[1] if len(sys.argv) > 1 else None)))
