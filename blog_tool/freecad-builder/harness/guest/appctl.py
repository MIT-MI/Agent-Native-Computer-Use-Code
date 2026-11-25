#!/usr/bin/python3
"""Fixed application lifecycle and runtime groups; no arbitrary commands."""
import argparse
import base64
import configparser
import grp
import json
import os
from pathlib import Path
import pwd
import select
import shlex
import signal
import socket
import socketserver
import subprocess
import threading
import urllib.request
import urllib.parse
import windows

SOCKET = '/opt/cua-world-runtime/app.sock'


def menu(spec, account, ready):
    # Forked by the root helper before it starts threads. This fixed listener
    # runs as the desktop user; it cannot execute commands or inject input.
    os.setgroups([]); os.setgid(account.pw_gid); os.setuid(account.pw_uid)
    os.environ.update(spec['environment'])
    from gi.repository import Gio, GLib
    bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    interface = Gio.DBusNodeInfo.new_for_xml('''<node><interface name="org.freedesktop.Application">
      <method name="Activate"><arg type="a{sv}" direction="in"/></method>
    </interface></node>''').interfaces[0]
    def activate(binary, invocation):
        try:
            result = request({'action': 'start', 'application': binary})
            if 'error' in result: raise RuntimeError(result['error'])
            invocation.return_value(None)
        except Exception as exc:
            invocation.return_dbus_error('org.cuaworld.LaunchError', str(exc))
    for app in spec['applications']:
        name = app['desktop_id']
        bus.register_object('/' + name.replace('.', '/'), interface,
            lambda connection, sender, path, interface, method, parameters, invocation, binary=app['binary']:
                activate(binary, invocation), None, None)
        bus.call_sync('org.freedesktop.DBus', '/org/freedesktop/DBus', 'org.freedesktop.DBus',
            'RequestName', GLib.Variant('(su)', (name, 0)), None, Gio.DBusCallFlags.NONE, -1, None)
    with os.fdopen(ready, 'w') as stream: stream.write('ready\n')
    GLib.MainLoop().run()


def request(value):
    with socket.socket(socket.AF_UNIX) as client:
        client.connect(SOCKET)
        stream = client.makefile('rwb')
        stream.write(json.dumps(value).encode() + b'\n'); stream.flush()
        return json.loads(stream.readline())


def application_groups(app):
    if not app.get('runtime_programs'): return []
    name = app['runtime_group']
    try: group = grp.getgrnam(name)
    except KeyError:
        subprocess.run(['groupadd', '--system', name], check=True)
        group = grp.getgrnam(name)
    return [group.gr_gid]


def launch(app, account, environment, logs, launcher=()):
    # launcher: an optional program that prepares the process and then execs the application unchanged.
    with (logs / (Path(app['binary']).name + '.log')).open('ab') as log:
        return subprocess.Popen([*launcher, *app['launch']], stdin=subprocess.DEVNULL, stdout=log, stderr=log,
            user=account.pw_uid, group=account.pw_gid, extra_groups=application_groups(app),
            start_new_session=True, env=environment)

def serve(config):
    spec = json.loads(Path(config).read_text())
    account = pwd.getpwnam(spec['user'])
    for app in spec['applications']:
        application_groups(app)
        source = configparser.ConfigParser(interpolation=None); source.optionxform = str
        source.read(app['desktop_file'])
        entry = dict(source['Desktop Entry'])
        for key in ('Exec', 'TryExec', 'Actions', 'Hidden', 'NoDisplay'): entry.pop(key, None)
        entry['DBusActivatable'] = 'true'
        # GNOME still reads executable metadata when launching over D-Bus.
        entry['Exec'] = shlex.join([str(Path(__file__).resolve()), 'start', '--application', app['binary']])
        launcher = configparser.ConfigParser(interpolation=None); launcher.optionxform = str
        launcher['Desktop Entry'] = entry
        with (Path('/usr/share/applications') / (app['desktop_id'] + '.desktop')).open('w') as out:
            launcher.write(out, space_around_delimiters=False)
    Path('/run/cua-world-lifecycle.pid').write_text(str(os.getpid()))
    reader, writer = os.pipe()
    pid = os.fork()
    if not pid:
        os.close(reader)
        try: menu(spec, account, writer)
        except Exception:
            import traceback
            traceback.print_exc()
        finally: os._exit(os.EX_OK)
    os.close(writer)
    with os.fdopen(reader) as stream:
        if stream.readline() != 'ready\n': raise RuntimeError('menu listener did not start')
    Path('/run/cua-world-menu.pid').write_text(str(pid))
    lock = threading.Lock()
    saved = {}
    initial = Path('/opt/cua-world-runtime/start-windows.json')
    if initial.exists():
        for window in json.loads(initial.read_text()): saved.setdefault(window['binary'], []).append(window)
    logs = Path('/opt/cua-world-runtime/application-logs'); logs.mkdir(exist_ok=True)
    def pids(application):
        result = []
        for directory in Path('/proc').iterdir():
            if not directory.name.isdigit(): continue
            try:
                if directory.stat().st_uid == account.pw_uid and (directory / 'exe').resolve() == Path(application['binary']).resolve():
                    result.append(int(directory.name))
            except OSError: pass
        return result
    class Handler(socketserver.StreamRequestHandler):
        def handle(self):
            try:
                request = json.loads(self.rfile.readline())
                action = request['action']
                if action == 'request':
                    url = urllib.parse.urlsplit(request['url'])
                    allowed = [urllib.parse.urlsplit(u) for service in spec.get('services', []) for u in service['urls']]
                    if url.scheme not in ('http', 'https') or not any((url.scheme, url.netloc) == (u.scheme, u.netloc) for u in allowed):
                        raise ValueError('URL origin is not declared in software data')
                    body = request.get('body')
                    req = urllib.request.Request(request['url'], data=None if body is None else body.encode(), method=request.get('method', 'GET'))
                    with urllib.request.urlopen(req) as response:
                        result = {'status': response.status, 'headers': dict(response.headers), 'body_base64': base64.b64encode(response.read()).decode()}
                    self.wfile.write(json.dumps(result).encode() + b'\n')
                    return
                if action not in ('start', 'stop', 'restart', 'status'): raise ValueError('unknown lifecycle operation')
                apps = [a for a in spec['applications'] if request.get('application') in (None, a['binary'])]
                if not apps: raise ValueError('application is not in software data')
                rows = []
                with lock:
                    for app in apps:
                        if action in ('stop', 'restart'):
                            frames = windows.capture(app['binary'])
                            if frames: saved[app['binary']] = frames
                            for pid in pids(app):
                                try:
                                    descriptor = os.pidfd_open(pid)
                                    os.kill(pid, signal.SIGTERM)
                                    lock.release()
                                    try: select.select([descriptor], [], [])
                                    finally: lock.acquire()
                                    os.close(descriptor)
                                except ProcessLookupError: pass
                        if action in ('start', 'restart') and not pids(app):
                            child = launch(app, account, spec['environment'], logs, spec.get('launcher', ()))
                            # External exporters can stop the app. Reap it then,
                            # without waiting for another lifecycle request.
                            threading.Thread(target=child.wait, daemon=True).start()
                            if app.get('window_title'):
                                # --all: xdotool otherwise matches either condition, so a splash window counted as ready.
                                waiter = subprocess.Popen(['xdotool', 'search', '--all', '--onlyvisible', '--sync', '--pid', str(child.pid), '--name', app['window_title']],
                                                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=spec['environment'])
                                child_fd, window_fd = os.pidfd_open(child.pid), os.pidfd_open(waiter.pid)
                                lock.release()
                                try: ready, _, _ = select.select([child_fd, window_fd], [], [])
                                finally: lock.acquire()
                                os.close(child_fd); os.close(window_fd)
                                if child_fd in ready:
                                    waiter.terminate(); waiter.wait()
                                    raise RuntimeError('Application exited during startup; see ' + str(logs))
                                waiter.wait()
                                windows.restore(saved.get(app['binary'], []), app['binary'])
                        rows.append({'binary': app['binary'], 'pids': pids(app)})
                result = {'applications': rows}
            except Exception as exc: result = {'error': str(exc)}
            self.wfile.write(json.dumps(result).encode() + b'\n')
    Path(SOCKET).unlink(missing_ok=True)
    server = socketserver.ThreadingUnixStreamServer(SOCKET, Handler)
    os.chmod(SOCKET, 0o666)
    print('CUA_WORLD_LIFECYCLE_READY', flush=True)
    server.serve_forever()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['start', 'stop', 'restart', 'status', 'serve', 'request'])
    parser.add_argument('--application'); parser.add_argument('--config')
    parser.add_argument('--url'); parser.add_argument('--method', default='GET'); parser.add_argument('--body')
    args = parser.parse_args()
    if args.action == 'serve': serve(args.config); return
    response = request({'action': args.action, 'application': args.application, 'url': args.url,
                        'method': args.method, 'body': args.body})
    print(json.dumps(response))
    if 'error' in response: raise SystemExit(1)

if __name__ == '__main__': main()
