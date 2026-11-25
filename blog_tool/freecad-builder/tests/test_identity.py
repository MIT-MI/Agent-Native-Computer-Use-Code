"""What the Builder, its reviewer and its MCP run as: attach permission (re) or a closed user (no-re)."""
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
import cua_input
import exec_policy
import isolate
import mcp_boundary


ACCOUNT = SimpleNamespace(pw_dir='/home/app', pw_uid=1000, pw_gid=1000)


def test_no_identity_keeps_a_capability():
    with patch('pwd.getpwnam', return_value=ACCOUNT):
        server = mcp_boundary.boundary(['app', '/opt/frozen-mcp/launch.sh'], {'DISPLAY': ':1'}, {})
    assert '--bounding-set=-all' in server and '--ambient-caps=-all' in server and '--no-new-privs' in server
    assert not any('ptrace' in argument.lower() for argument in server)
    assert [server[i + 1] for i, flag in enumerate(server) if flag == '--cap-add'] == ['CAP_SETUID', 'CAP_SETGID', 'CAP_SETPCAP']


def test_application_opts_in_to_its_own_users_debugger_and_runs_unchanged():
    import appctl
    import subprocess
    launcher = [sys.executable, str(ROOT / 'harness/guest/debuggable.py')]
    account = SimpleNamespace(pw_uid=1, pw_gid=1)
    with patch('appctl.subprocess.Popen') as start, patch('appctl.application_groups', return_value=[]):
        appctl.launch({'binary': '/opt/app/bin/app', 'launch': ['/opt/app/bin/app', 'file']}, account, {}, Path('/tmp'), launcher)
        assert start.call_args.args[0] == [*launcher, '/opt/app/bin/app', 'file']
        appctl.launch({'binary': '/opt/app/bin/app', 'launch': ['/opt/app/bin/app']}, account, {}, Path('/tmp'))
        assert start.call_args.args[0] == ['/opt/app/bin/app']
    scope = Path('/proc/sys/kernel/yama/ptrace_scope')
    if not scope.exists() or scope.read_text().strip() != '1': pytest.skip('needs Yama ptrace_scope 1, as on the experiment host')
    # A process that is not an ancestor may read the memory of an opted-in process only, also after its exec.
    target = [sys.executable, '-c', 'import time; print("ready", flush=True); time.sleep(60)']
    reader = 'import sys; open("/proc/" + sys.argv[1] + "/mem", "rb").close()'
    for prefix, readable in ((launcher, True), ([], False)):
        child = subprocess.Popen(prefix + target, stdout=subprocess.PIPE, text=True)
        try:
            assert child.stdout.readline() == 'ready\n'
            assert Path('/proc/%d/exe' % child.pid).resolve() == Path(sys.executable).resolve()
            assert (subprocess.run([sys.executable, '-c', reader, str(child.pid)], capture_output=True).returncode == 0) is readable
        finally: child.kill(); child.wait()


def test_no_re_identity_is_closed_with_private_folders_and_no_display():
    identity = {'user': 'agent', 'hidden': ['/srv/agent/output'],
                'private': [['/srv/agent/tmp', '/tmp'], [None, '/run/lock']]}
    with patch('pwd.getpwnam', return_value=SimpleNamespace(pw_dir='/srv/agent/home', pw_uid=10002, pw_gid=10002)):
        command = mcp_boundary.boundary(['--exec', 'agent', '/bin/bash', '-c', 'id'], {}, identity)
        server = mcp_boundary.boundary(['agent', '/opt/frozen-mcp/launch.sh'], {}, identity)
    for argv in (command, server):
        assert argv[0] == 'bwrap' and '--reuid=10002' in argv and '--no-new-privs' in argv
        assert '--bounding-set=-all' in argv and '--ambient-caps=-all' in argv and 'CAP_SYS_PTRACE' not in argv
        binds = [argv[i + 1:i + 3] for i, flag in enumerate(argv) if flag == '--bind']
        assert binds == [['/', '/'], ['/srv/agent/tmp', '/tmp']]
    # A Builder command outlives its call and keeps the desktop's network namespace; the MCP does not.
    assert not {'--die-with-parent', '--unshare-net', '--new-session', '--clearenv'} & set(command)
    assert [command[i + 1] for i, flag in enumerate(command) if flag == '--tmpfs'] == ['/run/lock']
    assert [server[i + 1] for i, flag in enumerate(server) if flag == '--tmpfs'] == ['/run/lock', '/srv/agent/output']
    passed = {server[i + 1] for i, flag in enumerate(server) if flag == '--setenv'}
    assert 'HOME' in passed and not {'DISPLAY', 'XAUTHORITY', 'DBUS_SESSION_BUS_ADDRESS', 'AT_SPI_BUS_ADDRESS'} & passed


def test_desktop_runs_builder_commands_as_its_identity():
    import desktop as desktop_module
    seen = []
    with patch.object(desktop_module, 'Process', side_effect=lambda argv, text=True: seen.append(argv)):
        for name, user, expected in (('re', 'app', False), ('no-re', 'agent', True), ('no-re', 'app', False), (None, 'app', False)):
            desktop = object.__new__(desktop_module.Desktop)
            desktop.container, desktop.variant, desktop.deadline = 'c', name, None
            desktop.user, desktop.agent = 'app', 'agent' if name == 'no-re' else 'app'
            desktop.environment = {'DISPLAY': ':1', 'HOME': '/home/app'}
            desktop.agent_environment = {'HOME': '/srv/agent/home'} if name == 'no-re' else desktop.environment
            desktop._exec_process('true', user=user)
            argv = seen[-1]
            assert ('/opt/cua-world-runtime/mcp_boundary.py' in argv and '--exec' in argv) is expected
            assert ('runuser' in argv) is not expected
            assert ('DISPLAY=:1' in argv) is not (name == 'no-re' and user == 'agent')


def test_policy_keeps_ordinary_programs_for_the_agent_group_only():
    # A program blocked to the desktop user: root only, plus the agent's group.
    assert exec_policy.agent_entries(0o100755, 0o700, 10002) == ['u::rwx', 'g::---', 'g:10002:r-x', 'm::r-x', 'o::---']
    # A script loses its execute bits for everyone else; the agent keeps what everyone had.
    assert exec_policy.agent_entries(0o100775, 0o664, 10002) == ['u::rw-', 'g::rw-', 'g:10002:r-x', 'm::rwx', 'o::r--']
    # Nothing is opened that was not open to everyone before.
    assert exec_policy.agent_entries(0o100750, 0o700, 10002) is None


def test_isolation_closes_package_paths_and_finds_analysis_tools(tmp_path):
    info = tmp_path / 'info'; info.mkdir()
    own, shared = tmp_path / 'opt/app', tmp_path / 'usr/share'
    own.mkdir(parents=True); shared.mkdir(parents=True)
    (own / 'core.so').write_text('x'); (shared / 'app.desktop').write_text('x'); (shared / 'other.txt').write_text('x')
    (info / 'app:amd64.list').write_text('\n'.join(map(str, [own, own / 'core.so', shared, shared / 'app.desktop'])) + '\n')
    (info / 'other.list').write_text('\n'.join(map(str, [shared, shared / 'other.txt'])) + '\n')
    files, directories = isolate.package_paths(['app'], isolate.package_owners(info))
    assert files == sorted(map(str, [own / 'core.so', shared / 'app.desktop'])) and directories == [str(own)]
    os.chmod(own / 'core.so', 0o604)
    assert isolate.close(str(own / 'core.so'), os.getgid())
    assert stat.S_IMODE(os.stat(own / 'core.so').st_mode) == 0o640  # the application's group keeps what everyone had
    tools = tmp_path / 'usr/bin'; tools.mkdir()
    for name in ('objdump', 'x86_64-linux-gnu-objdump', 'gdb', 'python3', 'font-size'): (tools / name).write_text('x')
    (tmp_path / 'usr/share/gdb').write_text('x')
    found = isolate.tool_files(['objdump', 'gdb', 'size'], roots=[str(tmp_path)])
    assert [Path(path).name for path in found] == ['gdb', 'objdump', 'x86_64-linux-gnu-objdump']


LAYOUT = cua_input.parse_layout("""keycode  10 = 1 exclam 1 exclam
keycode  18 = 9 parenleft 9 parenleft
keycode  19 = 0 parenright 0 parenright
keycode  21 = equal plus equal plus
keycode  23 = Tab ISO_Left_Tab Tab ISO_Left_Tab
keycode  36 = Return NoSymbol Return
keycode  60 = period greater period greater
keycode  65 = space NoSymbol space
keycode  87 = KP_End KP_1 KP_End KP_1
""" + ''.join('keycode %d = %s %s %s %s\n' % (100 + i, c, c.upper(), c, c.upper()) for i, c in enumerate('abcdefghijklmnopqrstuvwxyz')))
CODE = {char: str(100 + i) for i, char in enumerate('abcdefghijklmnopqrstuvwxyz')}


def window(tmp_path):
    """A cua-input display whose X commands are recorded instead of run."""
    display = cua_input.Display({'DISPLAY': ':1'}, [1280, 800])
    display.keyboard, display.sent = LAYOUT, []
    display.inject_action = display.sent.append
    refused = patch.object(cua_input, 'REFUSED', str(tmp_path / 'refused.jsonl')); refused.start()
    return display, refused


def chord(char):
    named = {'(': ['shift', '9'], ')': ['shift', '0'], '.': ['period'], ' ': ['space'], '=': ['equal'], '/': ['slash'], '-': ['minus']}
    return named.get(char) or (['shift', char.lower()] if char.isupper() else [char])


def test_keys_type_what_the_layout_gives_them():
    typed = lambda *keys: cua_input.entered(list(keys), LAYOUT)
    assert typed('a') == ('a', False) and typed('shift', 'a') == ('A', False) and typed('A') == ('A', False)
    assert typed('Shift_R', '9') == ('(', False) and typed('parenleft') == ('(', False) and typed('shift', 'parenleft') == ('(', False)
    assert typed('118') == ('s', False) and typed('shift', '18') == ('(', False)  # key codes
    assert typed('U0028') == ('(', False) and typed('0x1000028') == ('(', False) and typed('0x28') == ('(', False)
    assert typed('space') == (' ', False) and typed('Tab') == ('\t', False) and typed('KP_1') == ('1', False)
    assert typed('Return') == ('', True) and typed('enter') == ('', True) and typed('KP_Enter') == ('', True)
    # Shortcuts and navigation enter no text.
    assert typed('ctrl', 'a') == ('', False) and typed('Alt_L', 'F4') == ('', False) and typed('Left') == ('', False)
    for key in ('Caps_Lock', 'Num_Lock', 'ISO_Level3_Latch', 'dead_acute', 'Multi_key', 'ISO_Next_Group'):
        with pytest.raises(ValueError): typed(key)


@pytest.mark.parametrize('text,pieces', [('import os', ['import', ' os']), ('App.newDocument()', ['App.', 'newDocumen', 't', '()']),
                                         ('x = 5', ['x =', ' 5']), ('run(1)', ['run', '(1)'])])
def test_code_is_refused_key_by_key_by_key_code_and_split_across_calls(tmp_path, text, pieces):
    from code_guard import REJECTED, looks_like_code
    display, refused = window(tmp_path)
    try:
        sequences = [[('key', {'keys': chord(char)}) for char in text],
                     [('key', {'keys': [CODE.get(k, {'9': '18', '0': '19', 'period': '60', 'space': '65', 'equal': '21'}.get(k, k)) for k in chord(char.lower())]
                                       if not char.isupper() else ['shift', CODE[char.lower()]]}) for char in text],
                     [('type_text', {'text': piece}) for piece in pieces]]
        assert ''.join(pieces) == text and not any(looks_like_code(piece) for piece in pieces)  # each piece is plain by itself
        for sequence in sequences:
            display.call('key', {'keys': ['Return']})
            results = [display.call(tool, arguments) for tool, arguments in sequence]
            assert {'executed': False, 'error': REJECTED} in results, sequence
            assert not looks_like_code(display.line), display.line  # what did get through is not code
    finally: refused.stop()
    rows = [json.loads(line) for line in (tmp_path / 'refused.jsonl').read_text().splitlines()]
    assert all(set(row) == {'tool', 'arguments', 'line'} for row in rows) and {row['tool'] for row in rows} == {'key', 'type_text'}


def test_plain_values_are_entered_however_they_arrive(tmp_path):
    values = ['100 mm', 'Standoff1', '/app/work/enclosure-base.step', '12']
    display, refused = window(tmp_path)
    try:
        for value in values:
            assert display.call('type_text', {'text': value}) == {'executed': True}
            assert display.call('key', {'keys': ['Tab']}) == {'executed': True}  # the next field of a form
        display.call('key', {'keys': ['Return']})
        for value in values:
            assert all(display.call('key', {'keys': chord(char)}) == {'executed': True} for char in value)
            display.call('key', {'keys': ['Return']})
    finally: refused.stop()
    assert not (tmp_path / 'refused.jsonl').exists()


def test_the_line_ends_at_enter_and_a_moved_cursor_starts_a_new_run(tmp_path):
    from code_guard import REJECTED
    display, refused = window(tmp_path)
    try:
        enter = lambda text: display.call('type_text', {'text': text})
        # Enter ends the line: what was entered before no longer counts.
        assert enter('import') == {'executed': True} and display.call('key', {'keys': ['Enter']}) == {'executed': True}
        assert enter(' os') == {'executed': True} and display.line == ' os'
        display.call('key', {'keys': ['Return']})
        # A tab is whitespace in an editor.
        assert enter('import') == {'executed': True} and display.call('key', {'keys': ['Tab']})['error'] == REJECTED
        display.call('key', {'keys': ['Return']})
        # Text entered elsewhere before a click does not hide code entered after it.
        assert enter('x') == {'executed': True} and display.call('click', {'x': 5, 'y': 5}) == {'executed': True}
        assert display.run == '' and display.line == 'x'
        assert enter('imp') == {'executed': True} and enter('ort os')['error'] == REJECTED
        assert enter('import os')['error'] == REJECTED  # as before: code in one call is refused whatever came first
        display.call('key', {'keys': ['Return']})
        # A corrected character is forgotten.
        assert enter('impx') == {'executed': True} and display.call('key', {'keys': ['BackSpace']}) == {'executed': True}
        assert display.line == 'imp' and enter('ort os')['error'] == REJECTED
        # The line kept is bounded.
        display.call('key', {'keys': ['Return']})
        assert enter('7' * 600) == {'executed': True} and len(display.line) == cua_input.LINE
        # A lock key is not passed on.
        with pytest.raises(ValueError): display.call('key', {'keys': ['Caps_Lock']})
    finally: refused.stop()


def test_cua_input_offers_the_gui_tools_and_refuses_typed_code(tmp_path):
    display = cua_input.Display({'DISPLAY': ':1'}, [1280, 800])
    display.keyboard = LAYOUT
    ran = []
    with patch('cua_input.subprocess.run', side_effect=lambda argv, **kwargs: ran.append(argv[-1])), \
         patch.object(cua_input, 'REFUSED', str(tmp_path / 'refused.jsonl')):
        assert display.call('click', {'x': 10, 'y': 20}) == {'executed': True}
        assert display.call('key', {'keys': ['ctrl', 's']}) == {'executed': True}
        assert display.call('type_text', {'text': 'Bracket 12'}) == {'executed': True}
        refused = display.call('type_text', {'text': 'import os'})
        with pytest.raises(ValueError): display.call('click', {'x': 99999, 'y': 0})
        with pytest.raises(ValueError): display.call('exec', {'command': 'id'})
    from code_guard import REJECTED
    assert refused == {'executed': False, 'error': REJECTED}
    assert ran == ['xdotool mousemove --sync 10 20 click 1', 'xdotool key -- ctrl+s', "xdotool type --clearmodifiers -- 'Bracket 12'"]
    assert json.loads((tmp_path / 'refused.jsonl').read_text()) == {'tool': 'type_text', 'arguments': {'text': 'import os'}, 'line': 'Bracket 12import os'}
    from builder import GUI_TOOLS
    assert set(cua_input.ARGUMENTS) == {tool['name'] for tool in GUI_TOOLS}
    assert all(set(cua_input.ARGUMENTS[tool['name']]) == set(tool['inputSchema']['properties']) for tool in GUI_TOOLS)
    assert cua_input.arguments('drag', ['1', '2', '3', '4']) == {'x1': 1, 'y1': 2, 'x2': 3, 'y2': 4}
    assert cua_input.arguments('scroll', ['down', '3']) == {'direction': 'down', 'count': 3}
