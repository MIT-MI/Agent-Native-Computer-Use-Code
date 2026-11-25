"""Task-application execution allowlist for the desktop user.

The desktop is initialized first. Existing GNOME/session services stay alive;
only task binaries may subsequently be executed by the desktop user. Driver operations
remain root-owned, outside the agent's GUI surface.
"""
import json
from pathlib import Path

LOCKDOWN = r'''
set -eu
mkdir -p /etc/dconf/profile /etc/dconf/db/local.d/locks
printf 'user-db:user\nsystem-db:local\n' > /etc/dconf/profile/user
printf '[org/gnome/desktop/lockdown]\ndisable-command-line=true\n' > /etc/dconf/db/local.d/00-cua-world
printf '/org/gnome/desktop/lockdown/disable-command-line\n' > /etc/dconf/db/local.d/locks/cua-world
dconf update
'''



def activate(desktop):
    # Apply the current control to a disposable desktop; keep saved image bytes unchanged.
    desktop.copy_to(Path(__file__).with_name('guest') / 'exec_policy.py', '/opt/cua-world-runtime/exec_policy.py')
    # Verify the pre-session system policy as the configured desktop user.
    desktop.require(desktop.python( r'''
import pathlib,subprocess,json,os,pwd
from gi.repository import Gio
pid=subprocess.check_output(['pgrep','-u',USER,'-x','gnome-shell'],text=True).strip()
env=dict(x.split('=',1) for x in pathlib.Path('/proc/'+pid+'/environ').read_text().split(chr(0)) if '=' in x)
os.environ['DBUS_SESSION_BUS_ADDRESS']=env['DBUS_SESSION_BUS_ADDRESS']
account=pwd.getpwnam(USER);os.setgroups([]);os.setgid(account.pw_gid);os.setuid(account.pw_uid)
settings=Gio.Settings.new('org.gnome.desktop.lockdown')
value=settings.get_boolean('disable-command-line')
assert value and not settings.is_writable('disable-command-line')
print(json.dumps({'disable-command-line':value}))
'''.replace('USER', repr(desktop.user))))
    return json.loads(desktop.require(desktop.command('/usr/bin/python3 /opt/cua-world-runtime/exec_policy.py /opt/cua-world-runtime/applications.json', user='root')))
