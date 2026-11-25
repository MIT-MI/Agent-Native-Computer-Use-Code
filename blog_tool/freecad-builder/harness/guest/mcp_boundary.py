"""Offline MCP runtime with desktop access and private deployment output.

A desktop closed to its agent has an identity file: the separate user the Builder, its reviewer
and its MCP run as, with private writable folders. `--exec` runs a Builder command as that
identity, without the MCP's own limits: its network namespace, session and hidden /output.
No identity ever keeps a capability.
"""
import json
import os
from pathlib import Path
import pwd
import sys

IDENTITY = Path(__file__).with_name('identity.json')


def boundary(arguments, environ, identity):
    command_mode = arguments[:1] == ['--exec']
    user, command, *args = arguments[1:] if command_mode else arguments
    account = pwd.getpwnam(user)
    private = identity.get('private', [])
    drop = ['/usr/bin/setpriv', '--reuid=' + str(account.pw_uid), '--regid=' + str(account.pw_gid), '--init-groups',
            '--bounding-set=-all', '--inh-caps=-all', '--ambient-caps=-all', '--no-new-privs', command, *args]
    argv = ['bwrap'] + ([] if command_mode else ['--die-with-parent', '--new-session', '--unshare-net'])
    argv += ['--bind', '/', '/', '--dev', '/dev']
    # Folders only this identity writes: nothing it leaves there is visible to the desktop user.
    for source, target in private:
        argv += ['--bind', source, target] if source else ['--tmpfs', target]
    if not command_mode:
        for path in identity.get('hidden', ['/output']): argv += ['--tmpfs', path]
    argv += ['--cap-drop', 'ALL', '--cap-add', 'CAP_SETUID', '--cap-add', 'CAP_SETGID', '--cap-add', 'CAP_SETPCAP']
    if not command_mode:
        argv += ['--clearenv', '--setenv', 'HOME', account.pw_dir,
                 '--setenv', 'PATH', '/usr/local/bin:/usr/bin:/bin',
                 '--setenv', 'LANG', 'C.UTF-8', '--setenv', 'PYTHONDONTWRITEBYTECODE', '1']
        for key in ('DISPLAY', 'XAUTHORITY', 'XDG_RUNTIME_DIR', 'DBUS_SESSION_BUS_ADDRESS', 'AT_SPI_BUS_ADDRESS'):
            if key in environ: argv += ['--setenv', key, environ[key]]
    # Build the namespaces as root, then drop to the real UID. Bubblewrap's
    # --uid changes a user-namespace mapping; it does not switch the host identity.
    return argv + ['--'] + drop


if __name__ == '__main__':
    identity = json.loads(IDENTITY.read_text()) if IDENTITY.exists() else {}
    argv = boundary(sys.argv[1:], os.environ, identity)
    os.execvp(argv[0], argv)
