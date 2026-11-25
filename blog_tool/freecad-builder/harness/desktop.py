"""Direct docker exec access to a systemd desktop container. No model credentials enter it."""
import concurrent.futures
import hashlib
import json
import math
from pathlib import Path
import shlex
import subprocess
import threading
import time
import uuid
from container import Process, docker
from gui import action_commands
from records import write_json
from transfers import write_stream

ROOT = Path(__file__).resolve().parents[1]
GUEST = Path(__file__).with_name('guest')
CONFIG = json.loads((ROOT / 'config.json').read_text())
ISOLATION = CONFIG['isolation']


def collect(process):
    with concurrent.futures.ThreadPoolExecutor() as pool:
        out = pool.submit(process.stdout.read)
        err = pool.submit(process.stderr.read)
        rc = process.wait()
        return {'returncode': rc, 'stdout': out.result(), 'stderr': err.result()}


class Desktop:
    """variant: what the Builder, its reviewer and its MCP run as. 're': the desktop user, whom the
    application lets debug its own process. 'no-re': a separate user, closed off from the application
    and under the GUI arm's desktop policy. None: the desktop user, as the GUI arm and the checks use."""
    def __init__(self, image, software, folder, run_id, *, gui=False, variant=None, register=None):
        self.software, self.folder = software, Path(folder)
        self.variant, self.locked = variant, gui or variant == 'no-re'
        self.display_lock = threading.RLock()
        self.folder.mkdir(parents=True, exist_ok=True)
        self.spec = json.loads((ROOT / software['environment']).read_text())
        self.user = self.spec['user_accounts'][0]['name']
        self.home = '/home/' + self.user
        self.resolution = software['resolution'] if 'resolution' in software else self.spec['observation'][0]['resolution']
        self.environment = {'DISPLAY': self.spec['user_accounts'][0]['permissions']['env_vars']['DISPLAY'],
                            'HOME': self.home, 'XAUTHORITY': self.home + '/.Xauthority',
                            'PATH': '/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin', 'LANG': 'C.UTF-8',
                            'PYTHONDONTWRITEBYTECODE': '1'}
        self.agent, self.agent_home, self.scratch = self.user, self.home, '/tmp'
        self.agent_environment = self.environment
        if variant == 'no-re':
            # No X authority, session bus or accessibility bus: the window is reached through cua-input.
            root = ISOLATION['root']
            self.agent, self.agent_home, self.scratch = ISOLATION['user'], root + '/home', root + '/tmp'
            self.agent_environment = {'HOME': self.agent_home, **{k: self.environment[k] for k in ('PATH', 'LANG', 'PYTHONDONTWRITEBYTECODE')}}
        self.deadline = None
        self.container = 'cua-world-' + run_id + '-' + self.folder.name + '-' + uuid.uuid4().hex[:8]
        from gui_allowlist import LOCKDOWN
        boot = (LOCKDOWN if self.locked else '') + '\nexec /usr/bin/python3 /opt/cua-world-runtime/boot.py init'
        # systemd needs a private cgroup namespace it may write; the desktop stays offline.
        docker('run', '-d', '--name', self.container, '--hostname', 'desktop', '--privileged',
               '--cgroupns=private', '--tmpfs', '/run', '--tmpfs', '/run/lock', '--network=none',
               '--cpus', str(self.spec['resources']['cpu']), '--memory', str(self.spec['resources']['mem_gb']) + 'g',
               '--label', 'cua-world-run=' + run_id, '--label', 'cua-world-role=' + self.folder.name,
               '--entrypoint', '/bin/bash', image.object_id, '-c', boot)
        if register: register(self)
        write_json(self.folder / 'resource.json', {'container': self.container, 'image_id': image.object_id,
                   'network': 'none', 'ports': []})
        logs = subprocess.Popen(['docker', 'logs', '-f', self.container], stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True)
        stream = ''
        try:
            for line in logs.stdout:
                stream += line
                if line == 'CUA_WORLD_NAMESPACE_READY\n': break
            else: raise RuntimeError('desktop init did not start: ' + stream)
        finally: logs.terminate(); logs.wait()
        ready = self.require(self.command('/usr/bin/python3 /opt/cua-world-runtime/ready.py wait', user='root')).strip()
        if ready != 'ready':
            self.command('journalctl -b --no-pager -u cua-desktop.service', user='root')
            raise RuntimeError(ready)
        # Yama is not namespaced: record the host setting instead of changing it.
        write_json(self.folder / 'ptrace-scope.json', self.command('cat /proc/sys/kernel/yama/ptrace_scope', user='root'))
        for name in ('appctl.py', 'mcp_boundary.py'):
            self.copy_to(GUEST / name, '/opt/cua-world-runtime/' + name)
        for path, document in software.get('policy_files', {}).items():
            self.write(path, (json.dumps(document, indent=2) + '\n').encode())
        if variant == 'no-re': self.identify()
        # With analysis, the application opts in to being debugged by its own user; nobody gains a capability.
        if variant == 're': self.copy_to(GUEST / 'debuggable.py', '/opt/cua-world-runtime/debuggable.py')
        self.connect_session()
        if variant == 'no-re':
            self.input = self.service('/usr/bin/python3 /opt/cua-world-runtime/cua_input.py serve /opt/cua-world-runtime/applications.json '
                                      + 'x'.join(map(str, self.resolution)) + ' ' + self.agent, 'CUA_WORLD_INPUT_READY')
        self.binary_hashes = self.hash_binaries()
        write_json(self.folder / 'binary-hashes.json', self.binary_hashes)
        self.audit = self._exec_process('/opt/cua-world-runtime/fanotify-collector', user='root', ignore_budget=True)
        self.audit_threads = []
        for channel in ('stdout', 'stderr'):
            def record(channel=channel):
                with (self.folder / ('audit-' + channel + '.jsonl')).open('w') as out:
                    for chunk in getattr(self.audit, channel): out.write(chunk); out.flush()
            thread = threading.Thread(target=record, daemon=True); thread.start(); self.audit_threads.append(thread)

    def identify(self):
        """Close this desktop to a separate agent user, with its own folders and window helper."""
        root = ISOLATION['root']
        for source in (GUEST / 'isolate.py', GUEST / 'cua_input.py', GUEST.parent / 'gui.py', GUEST.parent / 'code_guard.py'):
            self.copy_to(source, '/opt/cua-world-runtime/' + source.name)
        config = {'user': self.agent, 'uid': ISOLATION['uid'], 'root': root, 'application_user': self.user,
                  'packages': sorted(self.software['packages']),
                  'install_roots': [r for app in self.software['applications'] for r in app.get('install_roots', [])],
                  'closed': [self.home, '/opt/cua-world-runtime/application-logs', *ISOLATION['closed_paths']],
                  'analysis_tools': ISOLATION['analysis_tools']}
        row = self.command('/usr/bin/python3 /opt/cua-world-runtime/isolate.py ' + shlex.quote(json.dumps(config)), user='root')
        write_json(self.folder / 'isolation.json', json.loads(self.require(row)))
        self.require(self.command('chmod 0755 /opt/cua-world-runtime/cua_input.py && '
                                  'ln -sf /opt/cua-world-runtime/cua_input.py /usr/local/bin/cua-input', user='root'))
        identity = {'user': self.agent, 'hidden': [root + '/output'],
                    'private': [[root + '/tmp', '/tmp'], [root + '/var-tmp', '/var/tmp'], [None, '/run/lock']]}
        self.write('/opt/cua-world-runtime/identity.json', json.dumps(identity).encode())

    def service(self, command, ready, *, ignore_budget=False):
        """A root helper that runs for the desktop's lifetime and announces itself on stdout."""
        process = self._exec_process(command, user='root', ignore_budget=ignore_budget)
        seen = ''
        for chunk in process.stdout:
            seen += chunk
            if ready + '\n' in seen: return process
        raise RuntimeError('desktop helper did not start: ' + process.stderr.read())

    def connect_session(self, *, ignore_budget=False):
        bus = self.python(r"""
import pathlib, subprocess, json
pid = subprocess.check_output(['pgrep', '-u', USER, '-x', 'gnome-shell'], text=True).split()[0]
env = dict(x.split('=', 1) for x in pathlib.Path('/proc/' + pid + '/environ').read_text().split(chr(0)) if '=' in x)
print(json.dumps({k: env[k] for k in ('XDG_RUNTIME_DIR', 'DBUS_SESSION_BUS_ADDRESS') if k in env}))
""".replace('USER', repr(self.user)), ignore_budget=ignore_budget)
        self.environment.update(json.loads(self.require(bus)))
        spec = {'user': self.user, 'applications': self.software['applications'], 'services': self.software['services'],
                'environment': self.environment}
        # The executable policy leaves a separate agent's programs and folders alone.
        if self.variant == 'no-re': spec['agent'] = {'user': self.agent, 'untouched': [ISOLATION['root'], '/opt/frozen-mcp']}
        if self.variant == 're': spec['launcher'] = ['/usr/bin/python3', '/opt/cua-world-runtime/debuggable.py']
        self.write('/opt/cua-world-runtime/applications.json', json.dumps(spec).encode(), ignore_budget=ignore_budget)
        self.lifecycle = self.service('/usr/bin/python3 /opt/cua-world-runtime/appctl.py serve --config /opt/cua-world-runtime/applications.json',
                                      'CUA_WORLD_LIFECYCLE_READY', ignore_budget=ignore_budget)

    def remaining(self):
        return None if self.deadline is None else max(0, self.deadline - time.monotonic())

    def _exec_process(self, command, *, user=None, timeout=None, text=True, ignore_budget=False):
        if timeout is None and not ignore_budget: timeout = self.remaining()
        if timeout is not None and timeout <= 0: raise TimeoutError('owner wall-clock budget expired')
        argv = ['docker', 'exec', '-i', self.container]
        # The guest's own timeout stops the command; killing the docker client would not.
        if timeout is not None: argv += ['timeout', '--kill-after=5', str(math.ceil(timeout))]
        environment = self.environment
        if self.variant == 'no-re' and user == self.agent:
            argv += ['/usr/bin/python3', '/opt/cua-world-runtime/mcp_boundary.py', '--exec', user]
            environment = self.agent_environment
        elif user and user != 'root': argv += ['runuser', '-u', user, '--']
        argv += ['/usr/bin/env', '-i', *[k + '=' + v for k, v in environment.items()],
                 '/bin/bash', '--noprofile', '--norc', '-c', command]
        return Process(argv, text=text)

    def command(self, command, *, user=None, timeout=None, ignore_budget=False):
        row = collect(self._exec_process(command, user=user or self.agent, timeout=timeout, ignore_budget=ignore_budget))
        write_json(self.folder / ('exec-' + uuid.uuid4().hex + '.json'), {'command': command, **row})
        return row

    def exec(self, command, wait_seconds):
        self.require(self.command('mkdir -p /tmp/cua-world-exec'))
        args = json.dumps({'command': command, 'wait_seconds': wait_seconds,
            'output_limit': CONFIG['technical_limits']['tool_output_characters']['value'],
            'log_path': '/tmp/cua-world-exec/exec-' + uuid.uuid4().hex + '.log'})
        return json.loads(self.require(self.command('/usr/bin/python3 /opt/cua-world-runtime/exec.py ' + shlex.quote(args))))

    def python(self, source, *, user='root', ignore_budget=False):
        return self.command('/usr/bin/python3 -c ' + shlex.quote(source), user=user, ignore_budget=ignore_budget)

    def require(self, row):
        if row['returncode']: raise RuntimeError(row['stderr'] or row['stdout'])
        return row['stdout']

    def copy_to(self, source, destination, *, user='root'):
        with Path(source).open('rb') as stream:
            self.write(destination, stream, user=user)

    def write(self, destination, data, *, user='root', ignore_budget=False):
        command = 'mkdir -p ' + shlex.quote(str(Path(destination).parent)) + ' && cat > ' + shlex.quote(destination)
        process = self._exec_process(command, user=user, text=False, ignore_budget=ignore_budget)
        write_stream(process.stdin, data, eof=True)
        row = collect(process)
        if row['returncode']: raise RuntimeError(str(row['stderr']))

    def install(self, source, path):
        """Add a command to this disposable desktop; saved image bytes stay unchanged."""
        self.copy_to(source, path)
        self.require(self.command('chmod 0755 ' + path + ' && ln -sf ' + path + ' /usr/local/bin/' + Path(path).name, user='root'))

    def copy_from(self, source, destination):
        digest = self.require(self.command('sha256sum -- ' + shlex.quote(source), user='root', ignore_budget=True)).split()[0]
        path = Path(destination); path.parent.mkdir(parents=True, exist_ok=True)
        docker('cp', self.container + ':' + source, str(path))
        with path.open('rb') as stream:
            if hashlib.file_digest(stream, 'sha256').hexdigest() != digest:
                raise RuntimeError('Copied file differs from guest file: ' + source)

    def capture_screenshot(self, path):
        self.ensure_display()
        row = collect(self._exec_process('import -window root png:-', user='root', text=False))
        if row['returncode']: raise RuntimeError(str(row['stderr']))
        path = Path(path); path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(row['stdout'])

    def inject_action(self, action):
        return self.inject_actions([action])

    def inject_actions(self, actions):
        commands = [command for action in actions for command in action_commands(action, self.resolution)]
        if not commands: return
        self.ensure_display()
        self.require(self.command(' && '.join(commands), user='root', timeout=self.remaining()))

    def ensure_display(self):
        with self.display_lock:
            if not self.command('wmctrl -m', user='root')['returncode']: return False
            ready = self.require(self.command('systemctl restart cua-desktop.service && /usr/bin/python3 /opt/cua-world-runtime/ready.py wait', user='root')).strip()
            if ready != 'ready': raise RuntimeError(ready)
            self.require(self.python(r"""
import os, pathlib, select, signal
for name in ('menu', 'lifecycle'):
    path = pathlib.Path('/run/cua-world-' + name + '.pid')
    if not path.exists(): continue
    try:
        descriptor = os.pidfd_open(int(path.read_text()))
        signal.pidfd_send_signal(descriptor, signal.SIGTERM)
        select.select([descriptor], [], [])
        os.close(descriptor)
    except ProcessLookupError: pass
    path.unlink()
"""))
            self.connect_session(ignore_budget=True)
            if self.locked:
                from gui_allowlist import activate
                activate(self)
            write_json(self.folder / ('display-restored-' + uuid.uuid4().hex + '.json'), {'restored': True})
            return True

    def restart(self):
        rows = []
        rows.append(self.command('cua-app restart', user='root', timeout=self.remaining()))
        self.require(rows[-1])
        for service in self.software['services']:
            unit = 'cua-world-service-' + hashlib.sha256(json.dumps(service, sort_keys=True).encode()).hexdigest()
            rows.append(self.command('systemctl stop ' + unit, user='root'))
            rows.append(self.command('systemd-run --collect --unit=' + unit +
                                    ' --uid=' + self.user + ' ' + shlex.join(service['start']), user='root'))
            self.require(rows[-1])
        return rows

    def scan(self):
        row = self.command('/usr/bin/python3 /opt/cua-world-runtime/scan.py ' + shlex.join([self.agent_home, self.scratch]), user='root')
        return json.loads(self.require(row))

    def window(self, tool, arguments):
        """Operate the window through cua-input, as the agent's own programs do on a closed desktop."""
        source = ('import json, sys; sys.path.insert(0, "/opt/cua-world-runtime"); import cua_input\n'
                  'print(json.dumps(cua_input.request(json.loads(sys.argv[1]))))')
        row = self.command('/usr/bin/python3 -c ' + shlex.quote(source) + ' ' + shlex.quote(json.dumps({'tool': tool, 'arguments': arguments})),
                           user='root', timeout=self.remaining())
        return json.loads(self.require(row))

    def refused_inputs(self):
        """Inputs cua-input refused because the line being entered looked like code."""
        if self.variant != 'no-re': return []
        text = self.require(self.command('cat /opt/cua-world-runtime/input-refused.jsonl', user='root', ignore_budget=True))
        return [json.loads(line) for line in text.splitlines()]

    def hash_binaries(self):
        row = self.python('import hashlib,json\nprint(json.dumps({p:hashlib.sha256(open(p,"rb").read()).hexdigest() for p in '
                          + repr([a['binary'] for a in self.software['applications']]) + '}))')
        return json.loads(self.require(row))

    def commit(self):
        """Stop applications and keep the container filesystem as an image (runtime mounts excluded)."""
        self.require(self.command('cua-app stop', user='root', ignore_budget=True))
        return docker('commit', self.container)

    def close(self):
        self.deadline = None
        try:
            write_json(self.folder / 'final-binary-hashes.json', self.hash_binaries())
        except Exception as exc:
            write_json(self.folder / 'final-binary-hash-error.json', {'error': str(exc)})
        for name, parent, source in [('exec-logs', self.scratch, 'cua-world-exec'),
                                      ('application-logs', '/opt/cua-world-runtime', 'application-logs')]:
            remote = '/opt/cua-world-runtime/' + name + '.tar'
            try:
                row = self.command('mkdir -p ' + parent + '/' + source + ' && tar -cf ' + remote +
                                   ' -C ' + parent + ' ' + source, user='root', ignore_budget=True)
                # Retain even a partial archive if a running app changed its log.
                self.copy_from(remote, self.folder / (name + '.tar'))
                self.require(row)
            except Exception as exc: write_json(self.folder / (name + '-archive-error.json'), {'error': str(exc)})
            finally:
                try: self.require(self.command('rm -f -- ' + remote, user='root', ignore_budget=True))
                except Exception as exc: write_json(self.folder / (name + '-cleanup-error.json'), {'error': str(exc)})
        docker('rm', '-f', self.container, check=False)
        for thread in getattr(self, 'audit_threads', []): thread.join()
