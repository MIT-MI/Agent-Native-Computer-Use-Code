"""The two Builder conditions in the brief and the Builder: with analysis (re) and without it (no-re)."""
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
import builder as builder_module
from builder import Builder, brief, variant

BUDGETS = json.loads((ROOT / 'config.json').read_text())['smoke_budgets']
SOFTWARE = json.loads((ROOT / 'software/freecad.json').read_text())


class Desktop:
    user = 'app'; agent = 'agent'
    software = {'name': 'Example', 'route_evidence': {'documents': 'examplecmd'}}
    def __init__(self): self.commands = []
    def command(self, command, **kwargs):
        self.commands.append(command)
        found = '/opt/frozen-mcp/deps/lib/input.py\n/opt/frozen-mcp/server.py\n' if 'grep' in command and 'cua-input' in command else ''
        return {'returncode': 0, 'stdout': found, 'stderr': ''}
    def require(self, result): return result['stdout']
    def scan(self): return {}
    def restart(self): return []
    def window(self, tool, arguments):
        self.entered = getattr(self, 'entered', []) + [(tool, arguments)]
        return {'executed': True}


def test_the_two_variants_and_the_default():
    assert [variant(name) for name in ('re', None, 'no-re')] == ['re', 're', 'no-re']
    for retired in ('full', 'without_re'):
        try: variant(retired)
        except KeyError: continue
        raise AssertionError(retired + ' is still accepted')


def test_each_variant_gets_only_its_own_brief_blocks():
    with_analysis, without = brief('re'), brief('no-re')
    for text in (with_analysis, without):
        assert '<!--' not in text and '{tool_output_characters}' not in text and '\n\n\n' not in text
        assert 'what a fresh agent achieves with it' in text and 'use the whole budget' in text
        assert 'the first finish only\nasks you to confirm' in text
    assert 'this is the expected way to\nbuild it' in with_analysis and 'is a fallback' in with_analysis
    assert 'apptool' in with_analysis and 'bundled in `mcp/deps`' in with_analysis
    # What the identity really has: the application's own process, and no privilege.
    assert 'with no further privileges' in with_analysis and 'attach to its process, and to no other' in with_analysis
    assert 'whether it arrives at once, in parts or key by key' in without
    assert 'apptool' not in without and 'gdb' not in without and '/opt/cua-world-reference' not in without
    assert 'cua-input' in without and 'Do not analyse the application' in without
    assert 'cua-input' not in with_analysis


def test_ocr_is_in_the_tool_list_only_when_the_run_has_it():
    for profile in ('re', 'no-re'):
        off, on = brief(profile), brief(profile, ocr=True)
        assert 'OCR' not in off and 'tesseract' not in off.lower()
        assert 'Offline OCR is also available while you build and at solve time' in on and '`tesserocr`' in on
        assert '<!--' not in on and on.replace(on[on.index('Offline OCR'):on.index('Deliver this layout')], '') == off


def test_brief_teaches_method_without_naming_the_application():
    names = ['FreeCAD', 'freecad', 'FCStd', 'OpenCASCADE', 'Coin3D', '/usr/lib', '.so', 'Mod/', '0x']
    names += [name for app in SOFTWARE['applications'] for name in (app['binary'], app['window_title'])]
    for text in (brief('re'), brief('no-re')): assert not [name for name in names if name in text]
    tool = (ROOT / 'harness/re_tools/apptool.py').read_text()
    assert not [name for name in ('FreeCAD', 'freecad', 'FCStd') if name in tool]


def test_first_finish_with_much_budget_left_only_asks_to_confirm(tmp_path):
    builder = Builder(Desktop(), tmp_path, 'test-key', BUDGETS)
    current = {'command': '/mcp', 'sha256': 'current'}
    server = Mock(initialized={'ready': True}, tools={'tools': []})
    with patch.object(builder, 'snapshot', return_value=current) as snapshot, patch('builder.MCP', return_value=server):
        first = builder.invoke('finish', {'notes': 'done'})
        assert first['accepted'] is False and first['run_continues'] is True and first['remaining_minutes'] > 0
        assert 'routes' in first and builder.last_started is None
        snapshot.assert_not_called()
        second = builder.invoke('finish', {'notes': 'done'})
    assert second['accepted'] is True and second['last_started'] == current


def test_finish_near_the_end_of_the_budget_delivers_at_once(tmp_path):
    builder = Builder(Desktop(), tmp_path, 'test-key', BUDGETS)
    limit = builder_module.EARLY_FINISH * BUDGETS['builder_seconds']
    server = Mock(initialized={'ready': True}, tools={'tools': []})
    with patch.object(builder, 'remaining', return_value=limit), patch.object(builder, 'snapshot', return_value={'command': '/mcp'}), \
         patch('builder.MCP', return_value=server):
        assert builder.finish()['accepted'] is True


def test_try_reports_the_routes_the_mcp_files_use_as_evidence(tmp_path):
    builder = Builder(Desktop(), tmp_path, 'test-key', BUDGETS)
    builder.gui.screenshot = lambda: {}
    server = Mock(initialized={'ready': True}, tools={'tools': []}); server.remaining.return_value = 1
    with patch.object(builder, 'snapshot', return_value={'command': '/mcp'}), patch('builder.MCP', return_value=server):
        result = builder.try_build(restart=False, wait_seconds=1)
    # The MCP's own files are listed apart from its bundled dependencies.
    assert result['routes']['window'] == {'mcp': ['/opt/frozen-mcp/server.py'], 'deps': ['/opt/frozen-mcp/deps/lib/input.py']}
    assert result['routes']['internals'] == result['routes']['documents'] == {'mcp': [], 'deps': []}
    assert 'internal state or operations' in result['routes']['note'] and 'deps/' in result['routes']['note']
    greps = [command for command in builder.desktop.commands if command.startswith('grep ')]
    assert len(greps) == 3 and any('examplecmd' in command for command in greps)
    # Every access path the desktops provide is covered.
    for path in ('/proc/123/mem', 'process_vm_readv', 'ptrace', 'gdb', 'frida', 'apptool', 'dbus-send'):
        assert __import__('re').search(builder_module.ROUTES['internals'], path), path
    for path in ('cua-input', 'input.sock', 'xdotool', 'screenshot'):
        assert __import__('re').search(builder_module.ROUTES['window'], path), path


def test_route_report_keeps_long_dependency_lists_short(tmp_path):
    builder = Builder(Desktop(), tmp_path, 'test-key', BUDGETS)
    many = ''.join('/opt/frozen-mcp/deps/lib/part%03d.so\n' % index for index in range(70))
    with patch.object(builder.desktop, 'command', return_value={'returncode': 0, 'stdout': many + '/opt/frozen-mcp/server.py\n', 'stderr': ''}):
        routes = builder.routes()
    assert routes['internals']['mcp'] == ['/opt/frozen-mcp/server.py']
    assert len(routes['internals']['deps']) == 51 and routes['internals']['deps'][-1] == '... 20 more'


def test_no_re_builder_gui_tools_go_through_the_window_helper_and_its_filter(tmp_path):
    for profile, helper in (('no-re', True), ('re', False)):
        builder = Builder(Desktop(), tmp_path / profile, 'test-key', BUDGETS, profile=profile)
        builder.gui.type_text = Mock(return_value={'executed': True}); builder.gui.key = Mock(return_value={'executed': True})
        builder.gui.screenshot = Mock(return_value={'image': ''})
        builder.invoke('type_text', {'text': 'imp'}); builder.invoke('key', {'keys': ['o']}); builder.invoke('screenshot', {})
        assert getattr(builder.desktop, 'entered', []) == ([('type_text', {'text': 'imp'}), ('key', {'keys': ['o']})] if helper else [])
        assert builder.gui.type_text.called is not helper and builder.gui.key.called is not helper
        assert builder.gui.screenshot.called


def test_reviewer_prompt_follows_the_variant(tmp_path):
    for profile, expected in (('re', 'drives the window'), ('no-re', 'must not analyse')):
        builder = Builder(Desktop(), tmp_path / profile, 'test-key', BUDGETS, profile=profile)
        seen = {}
        class Reviewer:
            def __init__(self, job, invoke, folder, key, **kwargs): seen.update(job)
            def run(self): return {'terminal_reason': 'terminated', 'terminal_result': {}, 'transcript': '', 'conversation': ''}
        with patch('builder.Loop', Reviewer): builder.review()
        assert expected in seen['prompt']
