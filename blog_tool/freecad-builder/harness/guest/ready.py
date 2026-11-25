"""Durable readiness result plus FIFO notification, without a polling timer."""
import errno
import os
from pathlib import Path
import select
import subprocess
import sys
import uuid

FIFO = '/run/cua-world-ready'
STATUS = Path('/run/cua-world-ready-status')


def report(message):
    pending = STATUS.with_name(STATUS.name + '-' + uuid.uuid4().hex)
    pending.write_text(message)
    pending.replace(STATUS)
    try: fd = os.open(FIFO, os.O_WRONLY | os.O_NONBLOCK)
    except OSError as exc:
        if exc.errno in (errno.ENXIO, errno.ENOENT): return
        raise
    try: os.write(fd, b'\n')
    finally: os.close(fd)


def wait():
    fd = os.open(FIFO, os.O_RDWR)
    try:
        if not STATUS.exists(): select.select([fd], [], [])
        return STATUS.read_text()
    finally: os.close(fd)


def watch():
    process = None
    try:
        subprocess.check_call(['xdotool', 'search', '--sync', '--class', 'gnome-shell'], stdout=subprocess.DEVNULL)
        process = subprocess.Popen(['stdbuf', '-oL', 'xprop', '-root', '-spy', '_NET_CLIENT_LIST'], stdout=subprocess.PIPE, text=True)
        for line in process.stdout:
            if line.startswith('_NET_CLIENT_LIST(WINDOW)'):
                report('ready'); return
        raise RuntimeError('window readiness observer exited')
    except Exception as exc: report('Desktop readiness failed: ' + str(exc))
    finally:
        if process: process.terminate(); process.wait()


if __name__ == '__main__':
    action = sys.argv[1]
    if action == 'reset': STATUS.unlink(missing_ok=True)
    elif action == 'wait': print(wait())
    elif action == 'stopped': report('Desktop unit stopped: ' + ' '.join(os.environ.get(k, '') for k in ('SERVICE_RESULT', 'EXIT_CODE', 'EXIT_STATUS')))
    elif action == 'watch': watch()
