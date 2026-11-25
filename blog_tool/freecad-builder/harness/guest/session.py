#!/usr/bin/python3
"""Root-owned VNC startup; report every early session failure to the driver."""
import os
import pwd
import subprocess
from ready import report

watcher = None
try:
    account = pwd.getpwnam(os.environ['CUA_WORLD_USER'])
    os.chown(os.environ['XAUTHORITY'], account.pw_uid, account.pw_gid)
    os.environ.update(XDG_SESSION_TYPE='x11', XKL_XMODMAP_DISABLE='1')
    for key in ('SESSION_MANAGER', 'DBUS_SESSION_BUS_ADDRESS', 'XDG_RUNTIME_DIR'):
        os.environ.pop(key, None)
    subprocess.run(['xset', 's', 'off'], check=True)
    watcher = subprocess.Popen(['/usr/bin/python3', '/opt/cua-world-runtime/ready.py', 'watch'])
    session = subprocess.run(['systemd-run', '--wait', '--collect', '--pipe',
        '--uid=' + account.pw_name, '--property=PAMName=login',
        '--property=BindsTo=cua-desktop.service', '--property=After=cua-desktop.service',
        '--setenv=DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/' + str(account.pw_uid) + '/bus',
        *['--setenv=' + key + '=' + os.environ[key] for key in
          ('HOME', 'DISPLAY', 'XAUTHORITY', 'XDG_SESSION_TYPE', 'XKL_XMODMAP_DISABLE')],
        '/usr/libexec/gnome-session-binary'])
    report('Desktop session exited: ' + str(session.returncode))
except Exception as exc:
    report('Desktop session failed: ' + str(exc))
    raise
finally:
    if watcher and watcher.poll() is None: watcher.terminate(); watcher.wait()
