"""Commands outlive the requested wait and remain controllable with exec."""
import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / 'harness/guest/exec.py'
sys.path[:0] = [str(ROOT / 'harness'), str(ROOT / 'agent')]
from builder import Builder, OUTPUT_LIMIT
from types import SimpleNamespace


def run(command, wait_seconds):
    with tempfile.NamedTemporaryFile(delete=False) as log: path = log.name
    result = subprocess.run([sys.executable, str(HELPER), json.dumps(
        {'command': command, 'wait_seconds': wait_seconds, 'log_path': path, 'output_limit': OUTPUT_LIMIT})], capture_output=True, text=True, check=True)
    return json.loads(result.stdout)


def test_wait_returns_live_command_and_log_then_exec_can_stop_it():
    start = time.monotonic()
    result = run('printf ready; exec sleep 1000', 0.1)
    try:
        assert time.monotonic() - start < 10
        assert result['finished'] is False and result['returncode'] is None
        assert result['head'] == 'ready'
        os.kill(result['pid'], 0)
        read = run('cat ' + result['log_path'], 1)
        assert read['finished'] and read['head'] == 'ready'
        stopped = run('kill ' + str(result['pid']), 1)
        assert stopped['finished'] and stopped['returncode'] == 0
    finally:
        try: os.killpg(result['pid'], signal.SIGKILL)
        except ProcessLookupError: pass
        Path(result['log_path']).unlink()


def test_completion_and_wait_are_explicit():
    result = run('printf out; printf err >&2; exit 7', 1)
    assert result['finished'] and result['returncode'] == 7
    assert result['head'] == 'outerr'
    schema = next(t for t in Builder.tools(SimpleNamespace(budgets=json.loads((ROOT / 'config.json').read_text())['budgets'])) if t['name'] == 'exec')['inputSchema']
    assert schema['required'] == ['command', 'wait_seconds']
    assert schema['properties']['wait_seconds'] == {'type': 'number'}
    spec = importlib.util.spec_from_file_location('desktop_exec', HELPER)
    helper = importlib.util.module_from_spec(spec); spec.loader.exec_module(helper)
    for invalid in (-1, float('inf'), float('nan'), True):
        try: helper.execute('false', invalid, '/not-created', OUTPUT_LIMIT)
        except ValueError: pass
        else: raise AssertionError('invalid wait launched a command')


def test_large_exec_returns_guest_head_tail_and_keeps_full_log():
    import shlex
    content = 'HEAD' + 'x' * (OUTPUT_LIMIT * 3) + 'TAIL'
    result = run(shlex.join([sys.executable, '-c', 'print(' + repr(content) + ', end="")']), 1)
    try:
        assert result['finished'] and result['size'] == len(content)
        assert result['head'].startswith('HEAD') and result['tail'].endswith('TAIL')
        assert len(result['head']) + len(result['tail']) == OUTPUT_LIMIT
        assert Path(result['log_path']).read_text() == content
        assert 'output' not in result
    finally: Path(result['log_path']).unlink()
