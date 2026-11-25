import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'harness'))
from mcp_transport import STOP_CODE
from mcp_transport import MCP, MCPProtocolError, deploy, startup_errors
from unittest.mock import Mock
import threading
import pytest
import tarfile


def test_missing_and_empty_cgroup_preserve_exited_mcp():
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        source = STOP_CODE.replace('UNIT', repr('cua-world-mcp-test')).replace('/sys/fs/cgroup', str(root))
        for exists in (False, True):
            if exists:
                group = root / 'system.slice/cua-world-mcp-test.service'; group.mkdir(parents=True)
                (group / 'cgroup.procs').write_text('')
                (group / 'cgroup.events').write_text('populated 0\n')
            out = io.StringIO()
            with patch('subprocess.run', return_value=SimpleNamespace(stdout='', returncode=1)), contextlib.redirect_stdout(out):
                exec(source, {})
            assert json.loads(out.getvalue())['already_exited'] is True


def test_transport_keeps_reading_after_non_json_stdout(tmp_path):
    server = object.__new__(MCP)
    server.folder = tmp_path; server.condition = threading.Condition(); server.responses = {}; server.next_id = 0
    server.error = None; server.deadline = None
    server.process = Mock(stdout=['debug output\n{broken\n', '[]\n{"jsonrpc":"2.0","id":1,"result":{"ready":true}}\n'])
    server.read_stdout()
    assert server.call('initialize', {}) == {'ready': True}
    rows = [json.loads(line) for line in (tmp_path / 'stdio.jsonl').read_text().splitlines()]
    assert [r['value'] for r in rows if r['kind'] == 'non_json_stdout'] == ['debug output', '{broken']


@pytest.mark.parametrize('failure', [False, True])
def test_deploy_uses_guest_tar_and_cleans_staging_after_failure(tmp_path, failure):
    workspace = tmp_path / 'workspace.tar'
    with tarfile.open(workspace, 'w') as archive:
        directory = tarfile.TarInfo('./mcp'); directory.type = tarfile.DIRTYPE; archive.addfile(directory)
    desktop = Mock()
    desktop.require.side_effect = [RuntimeError('bad archive') if failure else '', '']
    if failure:
        with pytest.raises(RuntimeError, match='bad archive'): deploy(desktop, {'archive': str(workspace)})
    else:
        assert deploy(desktop, {'archive': str(workspace)})['command'] == '/opt/frozen-mcp/launch.sh'
    commands = desktop.command.call_args_list
    assert 'tar -xf ' in commands[0].args[0] and ' ./mcp && ' in commands[0].args[0]
    assert commands[-1].args[0].startswith('rm -rf -- ') and commands[-1].kwargs['ignore_budget']


def test_deploy_excludes_large_intermediate_and_preserves_absolute_symlink(tmp_path):
    from transfers import MAX_BUFFER_SIZE as TASK_COMMAND_ROUTER_MAX_BUFFER_SIZE
    workspace = tmp_path / 'workspace.tar'
    with tarfile.open(workspace, 'w') as archive:
        for name, data in [('./intermediate/analysis', b'x' * (TASK_COMMAND_ROUTER_MAX_BUFFER_SIZE + 1)),
                           ('./mcp/server.py', b'original server bytes')]:
            info = tarfile.TarInfo(name); info.size = len(data); archive.addfile(info, io.BytesIO(data))
        link = tarfile.TarInfo('./mcp/python'); link.type = tarfile.SYMTYPE; link.linkname = '/usr/bin/python3'
        archive.addfile(link)
    desktop = Mock(); desktop.require.return_value = ''
    deploy(desktop, {'archive': str(workspace)})
    uploaded = desktop.copy_to.call_args.args[0]
    assert workspace.stat().st_size > TASK_COMMAND_ROUTER_MAX_BUFFER_SIZE
    with tarfile.open(uploaded) as archive:
        assert archive.getnames() == ['./mcp/server.py', './mcp/python']
        assert archive.extractfile('./mcp/server.py').read() == b'original server bytes'
        assert archive.getmember('./mcp/python').linkname == '/usr/bin/python3'


def test_large_mcp_request_drains_and_fragmented_response_is_not_quadratic(tmp_path):
    from transfers import MAX_BUFFER_SIZE
    TASK_COMMAND_ROUTER_MAX_BUFFER_SIZE = MAX_BUFFER_SIZE
    server = object.__new__(MCP); server.folder = tmp_path; server.condition = threading.Condition()
    server.responses = {}; server.error = None; server.deadline = None
    message = {'id': 1, 'result': {'text': 'x' * (TASK_COMMAND_ROUTER_MAX_BUFFER_SIZE + 1)}}
    wire = json.dumps(message) + '\n'; writer = Writer()
    server.process = Mock(stdin=writer, stdout=[wire[i:i + MAX_BUFFER_SIZE] for i in range(0, len(wire), MAX_BUFFER_SIZE)])
    server.send(message); server.read_stdout()
    assert bytes(writer.saved).decode() == wire and writer.drains > 1
    assert server.responses[1] == message and server.error == 'MCP stdout closed'


def test_zero_wait_does_not_launch_mcp_or_pass_invalid_timeout_to_sdk(tmp_path):
    runner = Mock()
    with pytest.raises(TimeoutError, match='requested wait or remaining budget is zero'):
        MCP(runner, '/mcp', tmp_path, lifetime_seconds=0)
    runner._exec_process.assert_not_called()


def test_deadline_after_initialize_closes_the_unreturned_mcp(tmp_path):
    runner = Mock(agent='desktop', agent_environment={})
    with patch('mcp_transport.threading.Thread'), patch.object(MCP, 'call', return_value={'ready': True}), \
         patch.object(MCP, 'send', side_effect=TimeoutError('deadline at initialized notification')), \
         patch.object(MCP, 'close') as close:
        with pytest.raises(TimeoutError, match='initialized notification'):
            MCP(runner, '/mcp', tmp_path)
    close.assert_called_once()
    runner._exec_process.assert_called_once()


def test_startup_protocol_errors_describe_envelopes_and_invalid_tools():
    valid = {'protocolVersion': '2024-11-05', 'capabilities': {}, 'serverInfo': {}}
    envelope = {'content': [{'type': 'text', 'text': json.dumps(valid)}], 'isError': False}
    errors = startup_errors(envelope, envelope)
    assert any('initialize result.protocolVersion' in error for error in errors)
    assert any('tools/list result.tools' in error for error in errors)
    assert startup_errors(valid, {'tools': []}) == []
    assert startup_errors(valid, {'tools': [{'name': 'edit'}]})


@pytest.mark.parametrize('response', [{'jsonrpc': '2.0', 'id': 1}, {'id': 1, 'result': {}}])
def test_invalid_rpc_response_returns_protocol_error(tmp_path, response):
    server = object.__new__(MCP)
    server.folder = tmp_path; server.condition = threading.Condition(); server.responses = {1: response}
    server.next_id = 0; server.error = None; server.deadline = None; server.process = Mock()
    with pytest.raises(MCPProtocolError, match='MCP protocol error: initialize response'):
        server.call('initialize', {})


class Writer:
    """Stdin double that fails if one write exceeds the transfer chunk size."""
    def __init__(self): self.pending = bytearray(); self.saved = bytearray(); self.drains = 0; self.eof = False
    def write(self, data):
        from transfers import MAX_BUFFER_SIZE
        assert len(self.pending) + len(data) <= MAX_BUFFER_SIZE
        self.pending.extend(data)
    def drain(self): self.saved.extend(self.pending); self.pending.clear(); self.drains += 1
    def write_eof(self): self.eof = True
