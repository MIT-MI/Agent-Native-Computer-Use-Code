"""Close a desktop to a separate agent user, once, as root, before the application starts.

The agent gets its own account and folders. The application's package files, install roots, home,
work folder and logs stay open to the application's user and closed to everyone else; binary-analysis
and debugging tools are removed. Nothing here names an application: the caller passes the data.
Prints one JSON report; fails if an analysis tool is still present.
"""
import grp
import json
import os
from pathlib import Path
import pwd
import re
import shutil
import stat
import subprocess
import sys

PATH = '/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin'


def account(name, uid, home):
    try: return pwd.getpwnam(name)
    except KeyError: pass
    subprocess.run(['groupadd', '--gid', str(uid), name], check=True)
    subprocess.run(['useradd', '--uid', str(uid), '--gid', str(uid), '--home-dir', home, '--no-create-home',
                    '--shell', '/bin/bash', name], check=True)
    return pwd.getpwnam(name)


def folders(root, agent):
    """Everything the agent writes lives under one folder only it and root can enter."""
    root = Path(root)
    root.mkdir(exist_ok=True); os.chown(root, 0, agent.pw_gid); root.chmod(0o750)
    for name in ('home', 'output', 'tmp', 'var-tmp', 'frozen-mcp'):
        (root / name).mkdir(exist_ok=True); os.chown(root / name, agent.pw_uid, agent.pw_gid)
    for link, name in (('/output', 'output'), ('/opt/frozen-mcp', 'frozen-mcp')):
        link = Path(link)
        if link.is_symlink(): continue
        if link.is_dir(): shutil.rmtree(link)
        link.symlink_to(root / name)


def package_owners(info=Path('/var/lib/dpkg/info')):
    """Path -> names of the installed packages that list it."""
    owners = {}
    for listing in info.glob('*.list'):
        name = listing.stem.split(':')[0]
        for line in listing.read_text(errors='surrogateescape').splitlines():
            owners.setdefault(line, set()).add(name)
    return owners


def package_paths(packages, owners):
    """The packages' own files, and the folders no other package shares."""
    packages = set(packages)
    files, directories = [], []
    for path, names in owners.items():
        if not names & packages: continue
        try: mode = os.lstat(path).st_mode
        except OSError: continue
        if stat.S_ISREG(mode): files.append(path)
        elif stat.S_ISDIR(mode) and names <= packages: directories.append(path)
    return sorted(files), sorted(directories)


def close(path, gid):
    """Keep the owner and the application user's group; remove everyone else's access."""
    info = os.lstat(path)
    if stat.S_ISLNK(info.st_mode): return False
    group = (info.st_mode >> 3) & 7 | (info.st_mode & 7)
    os.chown(path, -1, gid)
    os.chmod(path, stat.S_IMODE(info.st_mode) & ~0o077 | group << 3)
    return True


def tool_files(names, roots=('/',)):
    names = set(names)
    prefixed = re.compile(r'^[a-z0-9_]+-linux-gnu[a-z0-9]*-(.+)$')
    found = []
    for root in roots:
        device = os.stat(root).st_dev
        for folder, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs if os.lstat(os.path.join(folder, d)).st_dev == device]
            if os.path.basename(folder) not in ('bin', 'sbin'): continue
            for name in files:
                match = prefixed.match(name)
                if name in names or match and match[1] in names: found.append(os.path.join(folder, name))
    return sorted(found)


def remove_tools(names):
    removed = tool_files(names)
    for path in removed: os.unlink(path)
    left = tool_files(names) + [p for p in (shutil.which(n, path=PATH) for n in names) if p]
    if left: raise SystemExit('analysis tools still present: ' + ', '.join(sorted(set(left))))
    return removed


def main(config):
    agent = account(config['user'], config['uid'], config['root'] + '/home')
    folders(config['root'], agent)
    gid = pwd.getpwnam(config['application_user']).pw_gid
    files, directories = package_paths(config['packages'], package_owners())
    for path in files + directories: close(path, gid)
    closed = [path for path in [*config['install_roots'], *config['closed']] if os.path.lexists(path) and close(path, gid)]
    report = {'agent': {'user': agent.pw_name, 'uid': agent.pw_uid, 'groups': [g.gr_name for g in grp.getgrall() if agent.pw_name in g.gr_mem]},
              'root': config['root'], 'package_files_closed': len(files), 'package_directories_closed': directories,
              'paths_closed': closed, 'tools_removed': remove_tools(config['analysis_tools'])}
    print(json.dumps(report))


if __name__ == '__main__': main(json.loads(sys.argv[1]))
