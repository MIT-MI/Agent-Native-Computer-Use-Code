"""The model-free checks of the two conditions decide what must be closed and what must work."""
import json
import os
from pathlib import Path
import stat
import struct
import sys
from types import SimpleNamespace
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / p) for p in ('agent', 'harness', 'harness/guest', 'harness/re_tools')]
import pytest
import access_check


def rows(**changed):
    """What a probe reports on a closed desktop; `changed` flips one attempt."""
    result = [{'probe': 'read /proc/PID/mem', 'kind': 'process', 'succeeded': False, 'detail': 'PermissionError'},
              {'probe': 'gdb', 'kind': 'tool', 'succeeded': False, 'detail': 'absent'},
              {'probe': 'cua-input screenshot', 'kind': 'door', 'succeeded': True, 'detail': '9 bytes'},
              {'probe': 'write /output', 'kind': 'door', 'succeeded': True, 'detail': 'probe'}]
    return [{**row, 'succeeded': changed.get(row['probe'], row['succeeded'])} for row in result]


class Desktop:
    user, agent_home, home, resolution, variant = 'app', '/srv/agent/home', '/home/app', [1280, 800], 'no-re'
    software = {'packages': {'app': None}, 'applications': [],
                'isolation_probes': {'typed_code': ['import os'], 'typed_values': ['12']}}
    environment = {'DBUS_SESSION_BUS_ADDRESS': 'unix:path=/run/user/1000/bus', 'XDG_RUNTIME_DIR': '/run/user/1000'}
    def __init__(self, folder, command_rows, mcp_rows):
        self.folder, self.command_rows, self.mcp_rows, self.written = folder, command_rows, mcp_rows, {}
        (folder / 'isolation.json').write_text('{}')
    def copy_to(self, source, destination): pass
    def write(self, path, data): self.written[path] = json.loads(data)
    def python(self, source):
        out = '["/opt/app/core.so"]' if 'package_paths' in source else '{"i": {"keys": ["i"], "codes": ["31"]}}' if 'xmodmap' in source else '{"/tmp/handoff.py": "denied"}'
        return {'returncode': 0, 'stdout': out, 'stderr': ''}
    def command(self, command, user=None, **kwargs):
        out = ''
        if 'access_probe.py' in command: out = json.dumps(self.mcp_rows if 'systemd-run' in command else self.command_rows)
        elif command == 'cua-app status': out = '{"applications": [{"pids": [%d]}]}' % (7 + self.restarts)
        elif command == 'cat /run/cua-world-lifecycle.pid': out = '55'
        return {'returncode': 1 if user == 'app' else 0, 'stdout': out, 'stderr': ''}
    def require(self, row): return row['stdout']
    def refused_inputs(self): return [{'tool': 'type_text', 'arguments': {'text': access_check.CODE}, 'line': access_check.CODE}]
    def restart(self): self.restarts += 1; return [{'stdout': 'restarted'}]
    restarts = 0
    agent, agent_environment = 'agent', {'HOME': '/srv/agent/home'}


CONFIG = {'root': '/srv/agent', 'closed_paths': ['/app/work'], 'analysis_tools': ['gdb']}


def test_isolation_passes_when_every_path_is_closed_and_the_helpers_work(tmp_path):
    # Inside a running MCP the workspace is hidden, so writing /output there is expected to fail.
    desktop = Desktop(tmp_path, rows(), rows(**{'write /output': False}))
    report = access_check.isolation(desktop, CONFIG, files=['/app/work/input'])
    assert report['passed'] and report['unexpected'] == []
    config = desktop.written['/opt/cua-world-runtime/access-probe.json']
    assert config['pid'] == 7 and config['package_files'] == ['/opt/app/core.so'] and config['files'] == ['/app/work/input']
    assert config['others'] == {'PID 1 (root)': 1, 'the application helper (root)': 55}
    assert config['typed_code'] == [{'text': 'import os', 'pieces': ['import', ' os']}] and config['typed_values'] == ['12']
    assert access_check.pieces('App.newDocument()') == ['App.', 'newDocumen', 't', '()'] and access_check.pieces('run(1)') == ['run', '(1)']
    assert config['chords'] == {'i': {'keys': ['i'], 'codes': ['31']}}
    assert config['closed'] == ['/home/app', '/opt/cua-world-runtime/application-logs', '/app/work']


@pytest.mark.parametrize('flipped,where', [({'read /proc/PID/mem': True}, 'as_command'), ({'gdb': True}, 'as_command'),
                                           ({'cua-input screenshot': False}, 'as_command'), ({'write /output': True}, 'in_mcp_runtime')])
def test_isolation_fails_when_a_closed_path_opens_or_a_helper_breaks(tmp_path, flipped, where):
    command_rows = rows(**flipped) if where == 'as_command' else rows()
    mcp_rows = rows(**{'write /output': False, **(flipped if where == 'in_mcp_runtime' else {})})
    report = access_check.isolation(Desktop(tmp_path, command_rows, mcp_rows), CONFIG)
    assert not report['passed']
    assert [(row['probe'], row['where']) for row in report['unexpected']] == [(next(iter(flipped)), where)]


def test_with_ocr_on_the_identity_must_read_the_window(tmp_path):
    ocr = {'probe': 'read the window with OCR', 'kind': 'ocr', 'succeeded': True, 'detail': '9 words read'}
    closed = rows(**{'write /output': False})
    desktop = Desktop(tmp_path, rows() + [ocr], closed + [ocr])
    assert access_check.isolation(desktop, CONFIG, ocr=True)['passed']
    assert desktop.written['/opt/cua-world-runtime/access-probe.json']['ocr'] is True
    report = access_check.isolation(Desktop(tmp_path, rows() + [ocr], closed + [{**ocr, 'succeeded': False}]), CONFIG, ocr=True)
    assert [(row['probe'], row['where']) for row in report['unexpected']] == [('read the window with OCR', 'in_mcp_runtime')]
    # Off: the probe is not asked for.
    desktop = Desktop(tmp_path, rows(), closed)
    assert access_check.isolation(desktop, CONFIG)['passed'] and 'ocr' not in desktop.written['/opt/cua-world-runtime/access-probe.json']


def analysis_rows(**changed):
    """What a probe reports for the re identity: the application open, everything else closed."""
    result = [{'probe': 'read /proc/PID/mem', 'kind': 'process', 'succeeded': True, 'detail': '64 bytes'},
              {'probe': 'ptrace attach to PID 1 (root)', 'kind': 'other', 'succeeded': False, 'detail': 'EPERM'},
              {'probe': 'hold a capability', 'kind': 'capability', 'succeeded': False, 'detail': '{}'},
              {'probe': 'xdotool on the display', 'kind': 'display', 'succeeded': True, 'detail': 'the desktop user has the display'}]
    return [{**row, 'succeeded': changed.get(row['probe'], row['succeeded'])} for row in result]


class AnalysisDesktop(Desktop):
    variant = 're'
    def command(self, command, user=None, **kwargs):
        if 'access_probe.py' in command or command.startswith(('cua-app', 'cat ', 'pgrep', 'ls ')): return super().command(command, user, **kwargs)
        return {'returncode': 0, 'stdout': '9 exe =  modules seen from inside the process classes with vtables in Qt classes in system calls', 'stderr': ''}


@pytest.mark.parametrize('flipped', [{}, {'read /proc/PID/mem': False}, {'ptrace attach to PID 1 (root)': True}, {'hold a capability': True}])
def test_analysis_needs_the_application_open_and_every_other_process_closed(tmp_path, flipped):
    (tmp_path / 'builder').mkdir(); (tmp_path / 'task').mkdir()
    builder = AnalysisDesktop(tmp_path / 'builder', analysis_rows(), analysis_rows())
    builder.restarts = 2  # its application is process 9, which the canned command output names
    task = AnalysisDesktop(tmp_path / 'task', analysis_rows(), analysis_rows(**flipped))
    report = access_check.analysis(builder, task, CONFIG)
    assert report['passed'] is not bool(flipped)
    assert (report['task_desktop']['pid'], report['task_desktop']['after_restart']['pid']) == (7, 8)
    # Before and after the restart, inside the MCP runtime.
    assert [(row['probe'], row['where']) for row in report['unexpected']] == [
        (next(iter(flipped)), where) for where in ('task_desktop in_mcp_runtime', 'task_desktop after_restart in_mcp_runtime') if flipped]


def test_cli_accepts_the_two_variants_and_the_new_checks():
    source = (ROOT / 'cli.py').read_text()
    assert "choices=['re', 'no-re'], default='re'" in source
    assert "'isolation-check', 'analysis-check'" in source
    assert "p.add_argument('--ocr', action='store_true'" in source and "'start_images', 'ocr_images')" in source

