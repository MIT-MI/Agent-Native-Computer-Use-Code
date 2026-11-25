"""Start a systemd PID/mount namespace, with readiness on stdout, not polling."""
import os
from pathlib import Path
import socket
import sys

root = Path('/run/cua-world-host')
if len(sys.argv) == 1:
    root.mkdir(exist_ok=True)
    (root / 'parent').write_text(str(os.getpid()))
    os.execv('/usr/bin/unshare', ['unshare', '--fork', '--pid', '--mount', '--mount-proc',
        '/usr/bin/python3', __file__, 'init'])
os.system('mount -t tmpfs -o mode=0755,nosuid,nodev tmpfs /run')
os.mkfifo('/run/cua-world-ready')
os.chmod('/run/cua-world-ready', 0o666)
hosts = Path('/etc/hosts')
names = {socket.gethostname()}
configured = Path('/etc/hostname')
if configured.exists(): names.update(configured.read_text().split())
for name in sorted(names - set(hosts.read_text().split())):
    with hosts.open('a') as out: out.write('\n127.0.0.1 ' + name + '\n')
for unit in ('acpid.path', 'acpid.service', 'acpid.socket'):
    path = Path('/etc/systemd/system') / unit
    path.unlink(missing_ok=True)
    path.symlink_to('/dev/null')
print('CUA_WORLD_NAMESPACE_READY', flush=True)
os.execv('/sbin/init', ['/sbin/init'])
