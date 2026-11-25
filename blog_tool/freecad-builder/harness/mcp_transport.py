"""MCP stdio over direct exec; waits use the caller's remaining budget."""
import json
from pathlib import Path, PurePosixPath
import shlex
import tarfile
import threading
import time
import uuid
from transfers import write_stream


class MCPProtocolError(RuntimeError):
    pass


def startup_errors(initialized, tools):
    """Describe protocol errors without repairing responses or rejecting delivery."""
    errors = []
    for method, result, fields in (
        ('initialize', initialized, {'protocolVersion': str, 'capabilities': dict, 'serverInfo': dict}),
        ('tools/list', tools, {'tools': list}),
    ):
        for field, kind in fields.items():
            if not isinstance(result, dict) or not isinstance(result.get(field), kind):
                errors.append(f'MCP protocol error: {method} result.{field} must be {kind.__name__}.')
    if isinstance(initialized, dict) and 'instructions' in initialized and not isinstance(initialized['instructions'], str):
        errors.append('MCP protocol error: initialize result.instructions must be str.')
    if isinstance(tools, dict):
        if tools.get('error'): errors.append('MCP tools/list error: ' + str(tools['error']))
        if isinstance(tools.get('tools'), list):
            for tool in tools['tools']:
                if not isinstance(tool, dict) or not isinstance(tool.get('name'), str) or not isinstance(tool.get('inputSchema'), dict):
                    errors.append('MCP protocol error: each tools/list tool needs a name string and inputSchema object.')
    return errors


def stderr_observation(folder):
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / 'stderr.log'
    path.touch(exist_ok=True)
    limit = json.loads((Path(__file__).resolve().parents[1] / 'config.json').read_text())['technical_limits']['tool_output_characters']['value']
    tail = ''
    if path.exists():
        with path.open('rb') as stream:
            stream.seek(max(0, path.stat().st_size - limit))
            tail = stream.read().decode(errors='replace')
    return {'stderr_tail': tail, 'stderr_log': str(path)}


def boundary_command(runner, unit, command):
    """The MCP's runtime: a transient root unit that enters the boundary as the desktop's Builder identity."""
    command = shlex.join(['/usr/bin/python3', '/opt/cua-world-runtime/mcp_boundary.py',
                          runner.agent, '/bin/sh', '-c', command])
    return shlex.join(['systemd-run', '--unit=' + unit, '--pipe', '--wait', '--quiet',
        '--property=User=root', '--property=NoNewPrivileges=yes',
        '--property=KillMode=control-group', '--', '/usr/bin/env', '-i',
        *[k + '=' + v for k, v in runner.agent_environment.items()], '/bin/sh', '-c', command])


class MCP:
    def __init__(self, runner, command, folder, *, lifetime_seconds=None):
        self.folder = folder; folder.mkdir(parents=True, exist_ok=True)
        self.runner = runner
        self.unit = 'cua-world-mcp-' + uuid.uuid4().hex
        self.deadline = None if lifetime_seconds is None else time.monotonic() + lifetime_seconds
        if lifetime_seconds is not None and lifetime_seconds <= 0:
            raise TimeoutError('MCP was not started: the requested wait or remaining budget is zero.')
        self.process = runner._exec_process(boundary_command(runner, self.unit, command), user='root',
                                            ignore_budget=True, timeout=lifetime_seconds)
        self.error, self.responses, self.next_id = None, {}, 0
        self.condition = threading.Condition()
        self.readers = [threading.Thread(target=target, daemon=True)
                        for target in (self.read_stdout, self.read_stderr)]
        for reader in self.readers: reader.start()
        try:
            self.initialized = self.call('initialize', {'protocolVersion': '2024-11-05',
                'capabilities': {}, 'clientInfo': {'name': 'cua-world', 'version': '1'}})
            self.send({'jsonrpc': '2.0', 'method': 'notifications/initialized'})
            try:
                self.tools = self.call('tools/list', {})
            except Exception as exc:
                self.tools = {'tools': [], 'error': str(exc)}
        except Exception:
            try: self.close()
            except Exception as exc: self.log('close_error', str(exc))
            raise

    def remaining(self):
        return None if self.deadline is None else max(0, self.deadline - time.monotonic())

    def log(self, kind, value):
        with self.condition, (self.folder / 'stdio.jsonl').open('a') as out:
            out.write(json.dumps({'kind': kind, 'value': value, 'monotonic_ns': time.monotonic_ns()}) + '\n')

    def send(self, message):
        if self.remaining() is not None and self.remaining() <= 0:
            raise TimeoutError('MCP did not answer within the requested wait or remaining owner budget')
        self.log('send', message)
        write_stream(self.process.stdin, json.dumps(message) + '\n')

    def read_stdout(self):
        buffer = []
        try:
            for chunk in self.process.stdout:
                self.log('stdout', chunk)
                parts = chunk.split('\n'); buffer.append(parts[0])
                for part in parts[1:]:
                    line, buffer = ''.join(buffer), [part]
                    try: value = json.loads(line)
                    except json.JSONDecodeError:
                        self.log('non_json_stdout', line); continue
                    if not isinstance(value, dict):
                        self.log('non_protocol_stdout', value); continue
                    with self.condition:
                        if 'id' in value: self.responses[value['id']] = value
                        self.condition.notify_all()
            self.error = 'MCP stdout closed' + (' with an incomplete record' if any(buffer) else '')
        except Exception as exc:
            self.error = str(exc)
        finally:
            with self.condition: self.condition.notify_all()

    def read_stderr(self):
        try:
            with (self.folder / 'stderr.log').open('a') as out:
                for chunk in self.process.stderr:
                    out.write(chunk); out.flush()
        except Exception as exc:
            self.log('stderr_error', str(exc))

    def call(self, method, params):
        self.next_id += 1; key = self.next_id
        self.send({'jsonrpc': '2.0', 'id': key, 'method': method, 'params': params})
        with self.condition:
            ready = self.condition.wait_for(lambda: key in self.responses or self.error is not None,
                                            timeout=self.remaining())
            if not ready: raise TimeoutError('MCP did not answer within the requested wait or remaining owner budget')
            if key not in self.responses: raise RuntimeError(self.error)
            value = self.responses.pop(key)
        if value.get('jsonrpc') != '2.0' or ('result' in value) == ('error' in value):
            raise MCPProtocolError(f'MCP protocol error: {method} response needs jsonrpc 2.0 and exactly one result or error.')
        if 'error' in value: raise RuntimeError(json.dumps(value['error']))
        return value['result']

    def close(self):
        if self.process.poll() is None:
            try:
                self.process.stdin.write_eof(); self.process.stdin.drain()
            except Exception as exc:
                self.log('close_error', str(exc))
        try:
            self.log('stop', stop_mcp(self.runner, self))
        except Exception as exc:
            self.log('stop_error', str(exc))
            proc = self.runner._exec_process('systemctl kill --kill-whom=all --signal=KILL ' + shlex.quote(self.unit),
                user='root', ignore_budget=True)
            self.log('fallback_stop', {'returncode': proc.wait(), 'stdout': proc.stdout.read(), 'stderr': proc.stderr.read()})
        self.process.wait()
        for reader in self.readers: reader.join()


def deploy(desktop, snapshot):
    """Send only mcp/; preserve tar members for guest GNU tar, including symlinks."""
    payload = Path(snapshot['archive']).with_suffix('.mcp.tar')
    with tarfile.open(snapshot['archive']) as source, tarfile.open(payload, 'w') as target:
        for member in source:
            if PurePosixPath(member.name).parts[:1] == ('mcp',):
                target.addfile(member, source.extractfile(member) if member.isfile() else None)
    stage = '/opt/cua-world-runtime/deploy-' + uuid.uuid4().hex
    remote = stage + '.tar'
    try:
        desktop.copy_to(payload, remote)
        # /output and /opt/frozen-mcp may be links into a closed agent's own folder: fill them in place.
        desktop.require(desktop.command('mkdir -p ' + stage + ' /output /opt/frozen-mcp && tar -xf ' + remote +
            ' -C ' + stage + ' ./mcp && find /opt/frozen-mcp/ -mindepth 1 -delete && cp -a ' + stage + '/mcp/. /opt/frozen-mcp/', user='root'))
    finally:
        desktop.require(desktop.command('rm -rf -- ' + stage + ' ' + remote, user='root', ignore_budget=True))
    return {**snapshot, 'directory': '/opt/frozen-mcp', 'command': '/opt/frozen-mcp/launch.sh'}


STOP_CODE = r'''
import pathlib,subprocess,json
unit=UNIT
assert unit.startswith('cua-world-mcp-') and all(c.isalnum() or c=='-' for c in unit)
# A collected transient unit has no ControlGroup. Check its deterministic path
# too, rather than treating a missing systemctl result as proof of shutdown.
query=subprocess.run(['systemctl','show',unit,'--property=ControlGroup','--value'],text=True,capture_output=True)
relative=query.stdout.strip() or '/system.slice/'+unit+'.service'
assert relative in ('/system.slice/'+unit, '/system.slice/'+unit+'.service')
group=pathlib.Path('/sys/fs/cgroup'+relative)
def read(name):
 try:return (group/name).read_text()
 except FileNotFoundError:return ''
before=read('cgroup.procs').split()
if group.exists():
 try:(group/'cgroup.kill').write_text('1')
 except FileNotFoundError:pass
# cgroup.events supports readiness notification; no polling interval or retry cap.
import select
try:
 with (group/'cgroup.events').open() as events:
  watch=select.poll(); watch.register(events,select.POLLPRI|select.POLLERR)
  while True:
   events.seek(0); state=events.read()
   if 'populated 1' not in state and not read('cgroup.procs').split():break
   watch.poll()
except FileNotFoundError: pass
print(json.dumps({'unit':unit,'path':str(group),'before_pids':before,'populated':False,'after_pids':[], 'already_exited':not before}))
'''


def stop_mcp(desktop, server):
    if not server.unit:
        return {'already_exited': server.process.poll() is not None, 'tracked': False}
    return json.loads(desktop.require(desktop.python(STOP_CODE.replace('UNIT', repr(server.unit)), ignore_budget=True)))
