"""Desktop and Builder profiles. Provision online, run desktops offline."""
import base64
import json
from pathlib import Path
import shlex
from container import Image

ROOT = Path(__file__).resolve().parents[1]
ASSETS = Path(__file__).with_name('guest')
CONFIG = json.loads((ROOT / 'config.json').read_text())
PACKAGE_MANIFEST = '/opt/cua-world-runtime/packages.txt'
PACKAGE_QUERY = "dpkg-query -W -f='${binary:Package}\\t${Version}\\n' | LC_ALL=C sort"


def write_command(path, content):
    # Docker RUN instructions cannot contain literal newlines, even in quotes.
    encoded = base64.b64encode(content.encode()).decode()
    return 'printf %s ' + shlex.quote(encoded) + ' | base64 -d > ' + shlex.quote(path)


def apt_retries(image):
    return image.run_commands(write_command('/etc/apt/apt.conf.d/80-cua-world-retries',
        'Acquire::Retries "' + str(CONFIG['platform']['apt_retries']['value']) + '";\n'))


def package_manifest(image):
    return image.run_commands('mkdir -p ' + str(Path(PACKAGE_MANIFEST).parent),
                              PACKAGE_QUERY + ' > ' + PACKAGE_MANIFEST)


def guest_image(image, *, toolbox=False):
    image = image.add_local_dir(ASSETS, '/opt/cua-world-runtime', copy=True,
                               ignore=['**/__pycache__', '**/*.pyc'])
    scripts = ['appctl.py', 'session.py'] + (['atspi.py', 'cdp.py'] if toolbox else [])
    return image.run_commands('chmod 0755 ' + ' '.join('/opt/cua-world-runtime/' + name for name in scripts))


def runtime_image(image, software, *, toolbox=False):
    spec = json.loads((ROOT / software['environment']).read_text())
    account = spec['user_accounts'][0]
    user = account['name']; home = '/home/' + user
    display = account['permissions']['env_vars']['DISPLAY']
    geometry = 'x'.join(map(str, software['resolution'] if 'resolution' in software else spec['observation'][0]['resolution']))
    # Only the fixed supervisor is root. GNOME and declared apps run as the
    # desktop user, whose shell/interpreters stay blocked across session restart.
    unit = '\n'.join(['[Unit]', 'After=dbus.service', '[Service]', 'Type=simple',
        'Environment=HOME=' + home + ' XAUTHORITY=' + home + '/.Xauthority CUA_WORLD_USER=' + user,
        'ExecStartPre=-/usr/bin/vncserver -kill ' + display,
        'ExecStartPre=/usr/bin/python3 /opt/cua-world-runtime/ready.py reset',
        'ExecStopPost=/usr/bin/python3 /opt/cua-world-runtime/ready.py stopped',
        'ExecStart=/usr/bin/vncserver ' + display + ' -geometry ' + geometry +
        ' -localhost yes -fg -xstartup /opt/cua-world-runtime/session.py',
        '[Install]', 'WantedBy=multi-user.target', ''])
    if toolbox: image = reference_image(toolbox_image(image), software)
    image = guest_image(image, toolbox=toolbox)
    image = image.run_commands(
        'ln -sf /opt/cua-world-runtime/appctl.py /usr/local/bin/cua-app',
        write_command('/etc/systemd/system/cua-desktop.service', unit),
        'systemctl enable cua-desktop.service',
        # GNOME's packaged unit recommends this native systemd condition in
        # place of its shell test. Keep the desktop user's /bin/sh blocked.
        'mkdir -p /etc/systemd/user/org.gnome.Shell@.service.d',
        write_command('/etc/systemd/user/org.gnome.Shell@.service.d/cua-world.conf',
            '[Unit]\nConditionEnvironment=XDG_SESSION_TYPE=%I\n[Service]\nExecCondition=\n'),
        # Shared environment settings: disable GNOME's hidden idle timers, not
        # agent work. Zero is the GSettings value for no idle blanking.
        write_command('/etc/dconf/db/local.d/desktop',
            '[org/gnome/desktop/screensaver]\nlock-enabled=false\nubuntu-lock-on-suspend=false\n'
            '[org/gnome/desktop/session]\nidle-delay=uint32 0\n') + ' && dconf update')

    if toolbox:
        image = image.run_commands('ln -s /opt/cua-world-runtime/atspi.py /usr/local/bin/atspi-tree',
            'ln -s /opt/cua-world-runtime/cdp.py /usr/local/bin/cdp',
            'chmod 0700 /root',
            'python3 -m pip freeze > /opt/cua-world-runtime/python-packages.txt')
    return package_manifest(image)


def desktop_base_image():
    """Shared package layers, with no application files or account settings."""
    platform = CONFIG['platform']
    image = Image.from_registry(platform['base_image']).env({
        'DEBIAN_FRONTEND': 'noninteractive', 'LANG': 'C.UTF-8', 'LC_ALL': 'C.UTF-8', 'container': 'docker'})
    # The pinned base already uses its standard archive/security sources.
    image = apt_retries(image).apt_install('ca-certificates')
    image = image.apt_install(*platform['desktop_packages'])
    image = image.add_local_file(Path(__file__).with_name('fanotify_collector.c'), '/tmp/fanotify_collector.c', copy=True)
    image = image.run_commands('mkdir -p /opt/cua-world-runtime', 'chmod 1777 /tmp',
        "dpkg-query -W -f='${binary:Package}\\n' | sort > /tmp/cua-before-compiler")
    image = image.apt_install('gcc', 'libc6-dev').run_commands(
        'gcc -Wall -Wextra -Werror /tmp/fanotify_collector.c -o /opt/cua-world-runtime/fanotify-collector',
        "dpkg-query -W -f='${binary:Package}\\n' | sort > /tmp/cua-after-compiler; "
        'comm -13 /tmp/cua-before-compiler /tmp/cua-after-compiler | xargs -r apt-get purge -y',
        'rm /tmp/cua-before-compiler /tmp/cua-after-compiler /tmp/fanotify_collector.c',
        'apt-get purge -y gnome-initial-setup update-notifier || true',
        'mkdir -p /etc/dconf/profile /etc/dconf/db/local.d',
        write_command('/etc/dconf/profile/user', 'user-db:user\nsystem-db:local\n'),
        # Preserve application files under /tmp across boots; x11.conf still cleans sockets.
        write_command('/etc/tmpfiles.d/tmp.conf', 'd /tmp 1777 root root -\n'),
        'rm -f /etc/machine-id && touch /etc/machine-id')
    return package_manifest(image)


def desktop_image(image, software):
    spec = json.loads((ROOT / software['environment']).read_text())
    account = spec['user_accounts'][0]
    user = account['name']; home = '/home/' + user
    image = image.run_commands(
        'userdel -r ubuntu >/dev/null 2>&1 || true',
        'id ' + shlex.quote(user) + ' >/dev/null 2>&1 || useradd -m -s /bin/bash ' + shlex.quote(user),
        'printf %s ' + shlex.quote(user + ':' + account['password']) + ' | chpasswd',
        'install -d -o ' + user + ' -g ' + user + ' ' + home + '/.vnc ' + home + '/.config',
        'printf %s ' + shlex.quote(spec['vnc']['password']) + ' | vncpasswd -f > ' + home + '/.vnc/passwd',
        'chown ' + user + ':' + user + ' ' + home + '/.vnc/passwd && chmod 0600 ' + home + '/.vnc/passwd',
        'touch ' + home + '/.config/gnome-initial-setup-done')
    env_root = (ROOT / software['environment']).parent
    # Only environment setup/config/data enter this provisioning image. Task
    # instructions, verifiers and controls never enter the Builder profile.
    for mount in spec['mounts']:
        source = env_root / Path(mount['target']).name
        if source.name != 'tasks': image = image.add_local_dir(source, mount['target'], copy=True)
    image = runtime_image(image, software)
    return package_manifest(image.run_commands('bash ' + shlex.quote(spec['hooks']['pre_start']),
        # Remove developer tools from the desktop, except any the application's declared packages need.
        'python3 -c ' + shlex.quote("import subprocess; keep=set(" + repr(sorted(software['packages'])) + "); names='gcc g++ binutils gdb strace ltrace curl wget python3-dev python3-pip nodejs npm'.split(); installed=[p for p in names if 'Status: install ok installed' in subprocess.run(['dpkg','-s',p],capture_output=True,text=True).stdout]; removes=lambda p: {l.split()[1].split(':')[0] for l in subprocess.run(['apt-get','-s','purge',p],capture_output=True,text=True).stdout.splitlines() if l.startswith(('Purg ','Remv '))}; safe=[p for p in installed if not removes(p) & keep]; subprocess.check_call(['apt-get','purge','-y',*safe]) if safe else None"),
        'rm -f /etc/sudoers.d/' + shlex.quote(user), 'chmod 0700 /root'))


def toolbox_image(base_image):
    pins = json.loads((ROOT / 'toolbox.json').read_text())
    image = apt_retries(base_image).apt_install(*pins['apt'])
    # Debian marks its Python externally managed; this image is the Builder's own toolbox.
    image = image.run_commands('PIP_BREAK_SYSTEM_PACKAGES=1 python3 -m pip install ' + shlex.join(pins['pip']))
    for tool in pins['archives']:
        target = '/tmp/' + Path(tool['url']).name
        install = 'curl -fL ' + shlex.quote(tool['url']) + ' -o ' + target + ' && '
        install += "printf '%s\\n' " + shlex.quote(tool['sha256'] + '  ' + target) + ' | sha256sum -c - && '
        install += tool['install'].replace('{archive}', target)
        image = image.run_commands(install + ' && rm ' + target)
    return image


def reference_image(image, software):
    root = '/opt/cua-world-reference'
    reference = {**software.get('reference', {}), 'documentation_directory': root + '/documentation'}
    source = reference.get('source', {})
    files = [{**file, 'path': root + '/source/' + Path(file['url']).name} for file in source.get('files', [])]
    reference['source'] = {**source, 'files': files}
    image = image.run_commands('mkdir -p ' + root + '/source ' + root + '/documentation')
    for file in files:
        image = image.run_commands('curl -fL ' + shlex.quote(file['url']) + ' -o ' + shlex.quote(file['path']) +
            " && printf '%s\\n' " + shlex.quote(file['sha256'] + '  ' + file['path']) + ' | sha256sum -c -')
    if reference.get('documentation'):
        image = image.run_commands('cp -aL --parents -- ' + shlex.join(reference['documentation']) +
            ' ' + root + '/documentation && chmod -R a+rX ' + root)
    return image.run_commands(write_command(root + '/index.json', json.dumps(reference, indent=2) + '\n'))
