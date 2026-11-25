"""Model-free checks of the two Builder conditions, run before experiments on real desktops.

isolation: as the no-re agent, try every path to the application; all must be closed, every
analysis tool absent, code refused however it is entered, and the window and application
helpers must work.
analysis: as the re identity, read the application's memory, attach a debugger and an
instrumentation tool, and run apptool; no other process may be attached to or read, and
the identity holds no capability.
Each check tries the paths both as a Builder command and inside the MCP's own runtime.
"""
import json
import shlex
import uuid
from pathlib import Path
from code_guard import looks_like_code
from mcp_transport import boundary_command
from records import write_json

GUEST = Path(__file__).with_name('guest')
PROBE = '/opt/cua-world-runtime/access_probe.py'
CODE = 'import os'  # text the typed-code filter refuses
# In a closed desktop every attempt of these kinds must fail; doors must work.
CLOSED = ('files', 'process', 'other', 'capability', 'modules', 'display', 'bus', 'tool', 'typing', 'writable', 'hidden')
# With analysis, exactly the application's own process is open to its user.
ANALYSIS = {'process': True, 'other': False, 'capability': False, 'ocr': True}
LAYOUT = '''import json, subprocess, sys
sys.path.insert(0, "/opt/cua-world-runtime"); import cua_input
codes, symbols = cua_input.parse_layout(subprocess.run(["xmodmap", "-pke"], capture_output=True, text=True, check=True).stdout)
names = {char: name for name, char in cua_input.PUNCTUATION.items()}
chords = {}
for char in CHARACTERS:
    code, level = symbols[names.get(char, char)]
    shift = ["shift"] if level else []
    chords[char] = {"keys": shift + [codes[code][0]], "codes": shift + [str(code)]}
print(json.dumps(chords))
'''


def pieces(text):
    """Split text so that no piece looks like code by itself: the longest plain pieces, in order."""
    parts = ['']
    for char in text:
        if looks_like_code(parts[-1] + char): parts.append('')
        parts[-1] += char
    return parts


def other_processes(desktop):
    """Processes that are not the application: the desktop's root, its root helper and its user's own session."""
    read = lambda command: desktop.command(command, user='root')['stdout'].split()
    found = {'PID 1 (root)': ['1'], 'the application helper (root)': read('cat /run/cua-world-lifecycle.pid'),
             'the desktop shell (the desktop user)': read('pgrep -u ' + desktop.user + ' -x gnome-shell')}
    return {label: int(pids[0]) for label, pids in found.items() if pids}


def application_pid(desktop):
    status = json.loads(desktop.require(desktop.command('cua-app status', user='root')))
    pids = [pid for app in status['applications'] for pid in app['pids']]
    if not pids: raise RuntimeError('The application is not running')
    return pids[0]


def probe_config(desktop, config, **extra):
    """Facts the probe needs, written where the identity under test can read them."""
    software = desktop.software
    files = json.loads(desktop.require(desktop.python(
        'import json, sys; sys.path.insert(0, "/opt/cua-world-runtime"); import isolate\n'
        'print(json.dumps(isolate.package_paths(' + repr(sorted(software['packages'])) + ', isolate.package_owners())[0]))')))
    bus = desktop.environment.get('DBUS_SESSION_BUS_ADDRESS', '')
    # The accessibility bus is a second bus of the desktop user's session, next to its other sockets.
    folder = desktop.environment.get('XDG_RUNTIME_DIR', '/run/user/0') + '/at-spi'
    accessibility = desktop.command('ls ' + folder, user='root')['stdout'].split()
    probes = software.get('isolation_probes', {})
    typed = [*probes.get('typed_code', []), *probes.get('typed_values', [])]
    if desktop.variant == 'no-re':
        # How the desktop's keyboard layout types each character: as key symbols and as key codes.
        row = desktop.python(LAYOUT.replace('CHARACTERS', repr(sorted(set(''.join(typed))))))
        extra['chords'] = json.loads(desktop.require(row))
    value = {'pid': application_pid(desktop), 'others': other_processes(desktop),
             'typed_code': [{'text': text, 'pieces': pieces(text)} for text in probes.get('typed_code', [])],
             'typed_values': probes.get('typed_values', []),
             'applications': software['applications'], 'package_files': files,
             'closed': [desktop.home, '/opt/cua-world-runtime/application-logs', *config['closed_paths']],
             'files': extra.pop('files', []), 'environment': desktop.environment,
             'buses': {'session': bus, **{'accessibility (' + name + ')': 'unix:path=' + folder + '/' + name for name in accessibility}},
             'python_imports': probes.get('python_imports', {}), 'commands': probes.get('commands', []),
             'analysis_tools': config['analysis_tools'], 'code': CODE,
             'click': [desktop.resolution[0] // 2, desktop.resolution[1] // 2], 'writable': [], **extra}
    desktop.write('/opt/cua-world-runtime/access-probe.json', json.dumps(value).encode())
    return value


def probe(desktop, *, mcp):
    """Run the probe as the desktop's Builder identity: as a command, or inside the MCP runtime."""
    command = '/usr/bin/python3 ' + PROBE + ' /opt/cua-world-runtime/access-probe.json'
    if mcp: row = desktop.command(boundary_command(desktop, 'cua-world-mcp-check-' + uuid.uuid4().hex, command), user='root')
    else: row = desktop.command(command)
    return json.loads(desktop.require(row))


def as_desktop_user(desktop, paths):
    """Whether the application's user can read each path; the policy leaves it no interpreter, so root drops to it."""
    source = ('import json, os, pwd\naccount = pwd.getpwnam(' + repr(desktop.user) + ')\n'
              'os.setgroups([]); os.setgid(account.pw_gid); os.setuid(account.pw_uid)\nresult = {}\n'
              'for path in ' + repr(paths) + ':\n'
              '    try:\n        with open(path) as stream: stream.read(); result[path] = True\n'
              '    except OSError as exc: result[path] = str(exc)\nprint(json.dumps(result))')
    return json.loads(desktop.require(desktop.python(source)))


def isolation(desktop, config, files=(), ocr=False):
    """ocr: the run has OCR, so the agent must be able to read the desktop with it, through cua-input."""
    for name in ('access_probe.py', 'isolate.py'): desktop.copy_to(GUEST / name, '/opt/cua-world-runtime/' + name)
    root = config['root']
    private = [root, '/tmp', '/var/tmp', '/run/lock']
    probe_config(desktop, config, files=list(files), writable=['/output', desktop.agent_home, '/tmp'], private=private,
                 **({'ocr': True} if ocr else {}))
    report = {'variant': 'no-re', 'setup': json.loads((desktop.folder / 'isolation.json').read_text()),
              'as_command': probe(desktop, mcp=False), 'in_mcp_runtime': probe(desktop, mcp=True), 'driver': []}
    for row in report['in_mcp_runtime']:
        if row['probe'] == 'write /output': row['kind'] = 'hidden'  # the workspace is hidden from a running MCP
    def observe(name, expected, succeeded, detail):
        report['driver'].append({'probe': name, 'expected': expected, 'succeeded': bool(succeeded), 'detail': str(detail)[:400]})
    # What the agent writes must not reach the application's user either: a script it could be made to open.
    folders = ['/tmp', '/var/tmp', '/output', desktop.agent_home, '/dev/shm', '/run/lock']
    desktop.command('for d in ' + shlex.join(folders) + '; do echo probe > "$d/handoff.py"; chmod 0666 "$d/handoff.py"; done')
    seen = as_desktop_user(desktop, [d + '/handoff.py' for d in folders] +
                           [root + '/' + d + '/handoff.py' for d in ('tmp', 'var-tmp', 'output', 'home')])
    observe('desktop user reads files the agent wrote', False, any(v is True for v in seen.values()), seen)
    shell = desktop.command('true', user=desktop.user)
    observe('desktop user runs a shell', False, not shell['returncode'], shell['stderr'][-200:])
    refused = desktop.refused_inputs()
    observe('refused typed code is recorded', True, any(row['arguments'] == {'text': CODE} for row in refused),
            '%d refusals recorded; last: %s' % (len(refused), refused[-1:]))
    try: observe('application restarts under the policy', True, True, desktop.restart()[0]['stdout'][-300:])
    except Exception as exc: observe('application restarts under the policy', True, False, exc)
    unexpected = [{**row, 'where': where} for where in ('as_command', 'in_mcp_runtime') for row in report[where]
                  if row['succeeded'] == (row['kind'] in CLOSED)]
    unexpected += [row for row in report['driver'] if row['succeeded'] != row['expected']]
    report.update(unexpected=unexpected, passed=not unexpected)
    return report


def analysis(builder_desktop, task_desktop, config, ocr=False):
    """builder_desktop has the toolbox and apptool; task_desktop is a solve desktop, without them."""
    report = {'variant': 're', 'builder_desktop': {}, 'task_desktop': {}}
    extra = {'ocr': True} if ocr else {}
    for desktop in (builder_desktop, task_desktop):
        desktop.copy_to(GUEST / 'access_probe.py', PROBE)
        desktop.copy_to(GUEST / 'isolate.py', '/opt/cua-world-runtime/isolate.py')
        probe_config(desktop, config, **extra)
    pid = str(application_pid(builder_desktop))
    # name: (command, text its output must contain)
    commands = {
        'gdb attach': ('gdb -batch -ex "set auto-solib-add off" -ex "attach ' + pid + '" -ex "info proc" -ex detach 2>&1 | tail -n 8', 'exe = '),
        'frida attach': ('python3 -c ' + shlex.quote(
            'import frida\nsession = frida.attach(' + pid + ')\n'
            'script = session.create_script("rpc.exports = { count() { return Process.enumerateModules().length; } };")\n'
            'script.load(); print(script.exports_sync.count(), "modules seen from inside the process"); session.detach()'),
            'modules seen from inside the process'),
        'apptool pid': ('apptool pid', pid),
        'apptool modules': ('apptool modules | tail -n 3', ' modules'),
        'apptool rtti': ('apptool rtti | tail -n 4', 'classes with vtables in'),
        'apptool qt': ('apptool qt | tail -n 3', 'Qt classes in'),
        'apptool trace': ('apptool trace --seconds 3 > /tmp/apptool-trace.txt && head -c 600 /tmp/apptool-trace.txt', 'system calls'),
    }
    report['builder_desktop']['identity'] = builder_desktop.command('id && grep ^Cap /proc/self/status')['stdout']
    report['builder_desktop']['commands'] = {}
    for name, (command, expected) in commands.items():
        row = builder_desktop.command('set -o pipefail; ' + command, timeout=300)
        output = row['stdout'] + row['stderr']
        report['builder_desktop']['commands'][name] = {'command': command, 'returncode': row['returncode'], 'expected_text': expected,
            'succeeded': not row['returncode'] and expected in output, 'output': output[-1500:]}
    report['builder_desktop']['as_command'] = probe(builder_desktop, mcp=False)
    report['task_desktop'].update(pid=application_pid(task_desktop), as_command=probe(task_desktop, mcp=False),
                                  in_mcp_runtime=probe(task_desktop, mcp=True))
    # The application's opt-in must hold for the process that a restart starts, too.
    task_desktop.restart()
    probe_config(task_desktop, config, **extra)
    report['task_desktop']['after_restart'] = {'pid': application_pid(task_desktop), 'as_command': probe(task_desktop, mcp=False),
                                               'in_mcp_runtime': probe(task_desktop, mcp=True)}
    unexpected = [{'probe': name, **row} for name, row in report['builder_desktop']['commands'].items() if not row['succeeded']]
    after = report['task_desktop']['after_restart']
    for where, rows in (('builder_desktop as_command', report['builder_desktop']['as_command']),
                        ('task_desktop as_command', report['task_desktop']['as_command']),
                        ('task_desktop in_mcp_runtime', report['task_desktop']['in_mcp_runtime']),
                        ('task_desktop after_restart as_command', after['as_command']),
                        ('task_desktop after_restart in_mcp_runtime', after['in_mcp_runtime'])):
        unexpected += [{**row, 'where': where} for row in rows if row['kind'] in ANALYSIS and row['succeeded'] != ANALYSIS[row['kind']]]
    if after['pid'] == report['task_desktop']['pid']:
        unexpected.append({'probe': 'restart starts a new application process', 'succeeded': False, 'detail': after['pid']})
    report.update(unexpected=unexpected, passed=not unexpected)
    return report
