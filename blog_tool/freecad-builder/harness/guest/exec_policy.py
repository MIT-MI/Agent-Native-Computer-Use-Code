"""Guest-root-only executable and launcher policy (also run in the desktop VM)."""
import json
import ctypes
import grp
import os
from pathlib import Path, PurePosixPath
import pwd
import re
import shlex
import shutil
import stat
import struct
import signal

SKIP = ('/__modal', '/proc', '/sys', '/dev', '/root')
# Session infrastructure, not interactive applications or general interpreters.
SESSION = {
    'env',  # Frozen export launches the allowlisted application via runuser/env.
    'gnome-shell', 'gnome-session-binary', 'gnome-session-ctl',
    'gnome-session-check-accelerated', 'gnome-session-check-accelerated-gl-helper',
    'gnome-session-check-accelerated-gles-helper', 'gnome-shell-calendar-server',
    'Xtigervnc', 'Xvnc', 'Xorg', 'Xwayland',
    'dbus-daemon', 'dbus-broker', 'dbus-broker-launch', 'dbus-launch', 'systemd',
    'dconf-service', 'gnome-keyring-daemon', 'gcr-ssh-agent',
    'at-spi-bus-launcher', 'at-spi2-registryd',
    'xdg-desktop-portal', 'xdg-desktop-portal-gtk', 'xdg-desktop-portal-gnome',
    'ibus-daemon', 'ibus-dconf', 'ibus-engine-simple', 'ibus-extension-gtk3',
    'ibus-x11', 'ibus-portal', 'pulseaudio', 'pipewire', 'pipewire-pulse', 'wireplumber',
    'gvfsd', 'gvfsd-fuse', 'gvfsd-metadata', 'gvfsd-trash', 'gvfsd-network',
    'gvfsd-computer', 'gvfsd-recent', 'gvfsd-burn', 'gvfsd-dnssd',
    'gvfs-udisks2-volume-monitor', 'gvfs-goa-volume-monitor',
    'gvfs-mtp-volume-monitor', 'gvfs-gphoto2-volume-monitor', 'gvfs-afc-volume-monitor',
    'ld-linux-x86-64.so.2', 'ld-linux-aarch64.so.1',
}


def unescape(value):
    return re.sub(r'\\([0-7]{3})', lambda m: chr(int(m[1], 8)), value)


def mounts(text):
    rows = []
    for line in text.splitlines():
        fields = line.split(); separator = fields.index('-')
        rows.append({'id': int(fields[0]), 'device': fields[2], 'path': unescape(fields[4]),
                     'readonly': 'ro' in fields[5].split(',') or 'ro' in fields[separator+3].split(',')})
    return rows


def on_guest_root(path, table):
    path = PurePosixPath(path)
    if not path.is_absolute() or '..' in path.parts: return False
    if any(path.is_relative_to(p) for p in SKIP): return False
    containing = [m for m in table if path.is_relative_to(m['path'])]
    if not containing: return False
    owner = max(containing, key=lambda m: (len(PurePosixPath(m['path']).parts), m['id']))
    # A bind mount on the same device is still a non-root mount.
    return owner['path'] == '/' and not owner['readonly']


def session_binary(path):
    path = PurePosixPath(path)
    # Fixed root entry points used by VNC and the driver, not general executors.
    if path in {Path(__file__).with_name(name) for name in ('session.py', 'appctl.py')}: return True
    if not (str(path).startswith('/usr/') or str(path).startswith('/lib')): return False
    return path.name in SESSION or (path.parent == PurePosixPath('/usr/libexec') and path.name.startswith('gsd-'))


def hide_launcher(text):
    commands = [shlex.split(line.partition('=')[2]) for line in text.splitlines() if line.startswith('Exec=')]
    session = any(argv and session_binary(shutil.which(argv[0]) or argv[0]) for argv in commands)
    text = re.sub(r'^(Hidden|NoDisplay)=.*\n?', '', text, flags=re.M)
    # GNOME loads its own required components from desktop entries. NoDisplay
    # keeps those out of menus; Hidden would prevent the next session starting.
    return text.replace('[Desktop Entry]', '[Desktop Entry]\nNoDisplay=true' +
                        ('' if session else '\nHidden=true'), 1)


def elf_info(path):
    """ELF ABI metadata; ET_DYN covers both shared libraries and PIE programs."""
    with open(path, 'rb') as stream:
        ident = stream.read(16)
        if ident[:4] != b'\x7fELF': return None
        endian = '<' if ident[5] == 1 else '>'
        wide = ident[4] == 2
        def read(format):
            format = endian + format
            return struct.unpack(format, stream.read(struct.calcsize(format)))
        header = read('HHIQQQIHHHHHH' if wide else 'HHIIIIIHHHHHH')
        result = {'type': header[0], 'interpreter': None, 'soname': False, 'pie': False}
        for index in range(header[9]):
            stream.seek(header[4] + index * header[8])
            entry = read('IIQQQQQQ' if wide else 'IIIIIIII')
            offset, size = (entry[2], entry[5]) if wide else (entry[1], entry[4])
            if entry[0] == 3:  # PT_INTERP
                stream.seek(offset)
                result['interpreter'] = stream.read(size).rstrip(b'\0').decode()
            elif entry[0] == 2:  # PT_DYNAMIC
                stream.seek(offset)
                end = offset + size
                while stream.tell() < end:
                    tag, value = read('QQ' if wide else 'II')
                    if tag == 0: break  # DT_NULL
                    if tag == 14: result['soname'] = True  # DT_SONAME
                    if tag == 0x6ffffffb: result['pie'] = bool(value & 0x08000000)  # DT_FLAGS_1 / DF_1_PIE
        return result


def executable_mode(mode, elf):
    # Preserve readable data and shared objects, but deny programs read access
    # too so an allowed ELF loader cannot bypass their missing execute bits.
    library = elf and elf['type'] == 3 and not elf['pie'] and (not elf['interpreter'] or elf['soname'])
    program = elf and (elf['type'] == 2 or elf['type'] == 3 and not library)
    return 0o700 if program else stat.S_IMODE(mode) & ~0o111


def runtime_access(applications):
    programs = {}
    for app in applications:
        for path in app.get('runtime_programs', []):
            programs.setdefault(str(Path(path).resolve()), set()).add(grp.getgrnam(app['runtime_group']).gr_gid)
    return programs


def set_acl(path, entries, library=[]):
    if not library:
        acl = ctypes.CDLL('libacl.so.1', use_errno=True)
        acl.acl_from_text.argtypes = [ctypes.c_char_p]; acl.acl_from_text.restype = ctypes.c_void_p
        acl.acl_set_file.argtypes = [ctypes.c_char_p, ctypes.c_int, ctypes.c_void_p]
        acl.acl_free.argtypes = [ctypes.c_void_p]
        library.append(acl)
    acl = library[0]
    value = acl.acl_from_text(','.join(entries).encode())
    if not value: raise OSError(ctypes.get_errno(), 'acl_from_text', path)
    try:
        if acl.acl_set_file(os.fsencode(path), 0x8000, value):  # ACL_TYPE_ACCESS
            raise OSError(ctypes.get_errno(), 'acl_set_file', path)
    finally: acl.acl_free(value)


def grant_runtime_access(programs):
    # POSIX ACLs also handle a program shared by multiple application groups.
    for path, groups in programs.items():
        os.chown(path, 0, 0); os.chmod(path, 0o700)
        set_acl(path, ['u::rwx', 'g::---', 'm::r-x', 'o::---'] + ['g:' + str(gid) + ':r-x' for gid in sorted(groups)])


def has_acl(path):
    try: os.getxattr(path, 'system.posix_acl_access', follow_symlinks=False)
    except OSError: return False
    return True


def agent_entries(original, blocked, gid):
    """ACL for a file blocked to the desktop user: the agent's group keeps what everyone had before."""
    bits = lambda value: ''.join(letter if value & flag else '-' for letter, flag in (('r', 4), ('w', 2), ('x', 1)))
    kept = original & 0o005
    if not kept: return None
    group = blocked >> 3 & 7
    return ['u::' + bits(blocked >> 6 & 7), 'g::' + bits(group), 'g:' + str(gid) + ':' + bits(kept),
            'm::' + bits(group | kept), 'o::' + bits(blocked & 7)]


def blocked_processes(blocked, uid, proc=Path('/proc')):
    blocked = set(blocked)
    for directory in proc.iterdir():
        if not directory.name.isdigit(): continue
        try:
            if directory.stat().st_uid != uid: continue
            executable = str((directory / 'exe').resolve(strict=True))
            if executable in blocked: yield int(directory.name), executable
        except (FileNotFoundError, ProcessLookupError): pass


def session_ancestors(proc=Path('/proc')):
    parents, sessions = {}, []
    for directory in proc.iterdir():
        if not directory.name.isdigit(): continue
        try:
            pid = int(directory.name)
            fields = dict(line.split(':', 1) for line in (directory / 'status').read_text().splitlines())
            parents[pid] = int(fields['PPid'])
            if session_binary((directory / 'exe').resolve(strict=True)): sessions.append(pid)
        except (FileNotFoundError, ProcessLookupError): pass
    ancestors = set()
    for pid in sessions:
        while pid in parents and pid not in ancestors:
            ancestors.add(pid); pid = parents[pid]
    return ancestors


def stop_blocked_processes(blocked, user):
    stopped = []
    # Existing session supervisors can be scripts (for example the VNC server).
    # Keep their processes, without granting execution of their interpreters.
    keep = session_ancestors()
    # This root-recorded PID is the fixed menu listener, forked by appctl before
    # dropping privileges. Its interpreter remains unavailable for new execs.
    keep.add(int(Path('/run/cua-world-menu.pid').read_text()))
    for pid, executable in blocked_processes(blocked, pwd.getpwnam(user).pw_uid):
        if pid in keep: continue
        try:
            descriptor = os.pidfd_open(pid)
            try:
                if str((Path('/proc') / str(pid) / 'exe').resolve(strict=True)) != executable: continue
                signal.pidfd_send_signal(descriptor, signal.SIGTERM)
            finally: os.close(descriptor)
            stopped.append({'pid': pid, 'executable': executable})
        except (FileNotFoundError, ProcessLookupError): pass
    return stopped


def apply(allowed, launchers, install_roots=(), runtime_programs=None, agent=None):
    """agent: a separate account the policy does not apply to. Its group keeps running ordinary
    programs, and its own files and the listed folders are left as they are."""
    runtime_programs = runtime_programs or {}
    account = pwd.getpwnam(agent['user']) if agent else None
    untouched = set(agent['untouched']) if agent else set()
    table = mounts(Path('/proc/self/mountinfo').read_text())
    assert on_guest_root('/usr/bin', table), 'guest root is not writable'
    allowed = {str(Path(p).resolve()) for p in allowed}
    assert allowed and all(Path(p).is_file() and on_guest_root(p, table) for p in allowed)
    assert all(Path(p).is_file() and on_guest_root(p, table) for p in runtime_programs), 'invalid runtime program'
    roots = [Path(p).resolve() for p in install_roots]
    assert all(p.is_dir() and on_guest_root(str(p), table) for p in roots), 'invalid application install root'
    assert not any(Path('/usr/bin').resolve().is_relative_to(p) for p in roots), 'install roots must not unlock /usr/bin'
    loaders = set()
    for app in allowed:
        elf = elf_info(app)
        if elf and elf['interpreter']: loaders.add(str(Path(elf['interpreter']).resolve()))
    root_device = Path('/').stat().st_dev
    blocked, preserved, hidden, seen = [], [], [], set()
    for folder, dirs, files in os.walk('/', followlinks=False):
        dirs[:] = [d for d in dirs if on_guest_root(str(Path(folder)/d), table) and str(Path(folder)/d) not in untouched]
        for name in files:
            p = Path(folder)/name
            if not on_guest_root(str(p), table): continue
            try:
                s = p.lstat()
                if s.st_dev != root_device or not stat.S_ISREG(s.st_mode): continue
                if account and s.st_uid == account.pw_uid: continue
                resolved = str(p.resolve())
                if not on_guest_root(resolved, table): continue
                if any(Path(resolved).is_relative_to(root) for root in roots): continue
                if p.suffix == '.desktop' and '/applications/' in str(p):
                    text = p.read_text(errors='replace')
                    if str(p) not in launchers:
                        p.write_text(hide_launcher(text)); hidden.append(str(p))
                        # Root's, as before; a launcher already closed to other users stays closed.
                        os.chown(p, 0, s.st_gid if account else 0); p.chmod(0o644 & s.st_mode if account else 0o644)
                if not s.st_mode & 0o111: continue
                inode = (s.st_dev, s.st_ino)
                if inode in seen: continue
                seen.add(inode)
                keep = resolved in allowed or resolved in loaders or resolved in runtime_programs or session_binary(resolved)
                if keep:
                    preserved.append(resolved); continue
                if account and has_acl(p):  # Blocked by an earlier run, which also set the agent's access.
                    blocked.append(str(p)); continue
                mode = executable_mode(s.st_mode, elf_info(p))
                if mode == 0o700: os.chown(p, 0, 0)  # Programs only; data keeps its owner.
                p.chmod(mode)
                entries = agent_entries(s.st_mode, mode, account.pw_gid) if account else None
                if entries: set_acl(p, entries)
                blocked.append(str(p))
            except FileNotFoundError: continue
    grant_runtime_access(runtime_programs)
    return {'allowed': sorted(allowed), 'install_roots': sorted(map(str, roots)), 'session_binaries': sorted(preserved),
            'runtime_program_groups': {p: sorted(gids) for p, gids in runtime_programs.items()},
            'blocked': sorted(blocked), 'hidden_launchers': sorted(hidden),
            'skipped_mounts': [m for m in table if m['path'] != '/' or m['readonly']],
            'scope': 'writable guest root filesystem only'}


if __name__ == '__main__':
    import sys
    spec = json.loads(Path(sys.argv[1]).read_text())
    result = apply([app['binary'] for app in spec['applications']],
                   ['/usr/share/applications/' + app['desktop_id'] + '.desktop' for app in spec['applications']],
                   [root for app in spec['applications'] for root in app.get('install_roots', [])],
                   runtime_access(spec['applications']), spec.get('agent'))
    result['stopped_processes'] = stop_blocked_processes(result['blocked'], spec['user'])
    print(json.dumps(result))
