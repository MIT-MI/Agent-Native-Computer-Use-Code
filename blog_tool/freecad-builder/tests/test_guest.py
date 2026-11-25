from pathlib import Path
import sys
from unittest.mock import patch
sys.path[:0] = [str(Path(__file__).resolve().parents[1] / p) for p in ('harness', 'harness/guest')]
from exec_policy import blocked_processes, session_ancestors, session_binary, hide_launcher
import os
import threading
import runpy
import pytest
import ready


def test_existing_process_cleanup_follows_the_executable_allowlist(tmp_path):
    import os
    proc = tmp_path / 'proc'; proc.mkdir()
    allowed = tmp_path / 'new-application'; allowed.touch()
    blocked = tmp_path / 'unlisted-application'; blocked.touch()
    for pid, executable in [(101, allowed), (202, blocked)]:
        directory = proc / str(pid); directory.mkdir(); (directory / 'exe').symlink_to(executable)
    assert list(blocked_processes([str(blocked)], os.getuid(), proc)) == [(202, str(blocked))]


def test_session_supervisor_survives_without_allowing_interpreter_launches(tmp_path):
    proc = tmp_path / 'proc'; proc.mkdir()
    for pid, parent, name in [(10, 0, 'perl'), (11, 10, 'Xtigervnc'), (12, 0, 'perl')]:
        executable = tmp_path / name; executable.touch()
        directory = proc / str(pid); directory.mkdir()
        (directory / 'exe').symlink_to(executable)
        (directory / 'status').write_text('PPid:\t' + str(parent) + '\n')
    with patch('exec_policy.session_binary', side_effect=lambda p: p.name == 'Xtigervnc'):
        assert session_ancestors(proc) == {10, 11}
    assert not session_binary('/usr/bin/perl')


def test_session_desktop_entries_can_restart_but_stay_out_of_menus():
    old = '[Desktop Entry]\nHidden=true\nNoDisplay=true\nExec=/usr/bin/gnome-shell\n'
    session = hide_launcher(old)
    assert 'Hidden=true' not in session and 'NoDisplay=true' in session
    other = hide_launcher(old.replace('/usr/bin/gnome-shell', '/usr/bin/gnome-terminal'))
    assert 'Hidden=true' in other and 'NoDisplay=true' in other


@pytest.mark.parametrize('report_first', [False, True])
def test_readiness_failure_is_durable_and_wakes_fifo_waiter(tmp_path, report_first):
    fifo = tmp_path / 'fifo'; os.mkfifo(fifo)
    result = []
    with patch.object(ready, 'FIFO', str(fifo)), patch.object(ready, 'STATUS', tmp_path / 'status'):
        if report_first: ready.report('Desktop unit stopped: exit-code')
        thread = threading.Thread(target=lambda: result.append(ready.wait()), daemon=True); thread.start()
        if not report_first: ready.report('Desktop unit stopped: exit-code')
        thread.join(timeout=1)
    assert result == ['Desktop unit stopped: exit-code']
    assert [p.name for p in tmp_path.iterdir() if p.is_file()] == ['status']


def test_session_preparation_failure_reports_to_readiness():
    with patch('pwd.getpwnam', side_effect=KeyError('account unavailable')), patch.dict(os.environ, {'CUA_WORLD_USER': 'missing'}), \
         patch.object(ready, 'report') as report:
        with pytest.raises(KeyError): runpy.run_path(str(Path(ready.__file__).with_name('session.py')))
    assert 'Desktop session failed' in report.call_args.args[0] and 'account unavailable' in report.call_args.args[0]


def test_readiness_observer_failure_reports_to_fifo():
    with patch('ready.subprocess.check_call', side_effect=OSError('display unavailable')), patch.object(ready, 'report') as report:
        ready.watch()
    report.assert_called_once_with('Desktop readiness failed: display unavailable')


def test_mcp_namespace_preserves_desktop_access_and_network_isolation(tmp_path):
    import pwd
    from types import SimpleNamespace
    home = tmp_path / 'home'; home.mkdir(); (home / '.Xauthority').touch()
    account = SimpleNamespace(pw_dir=str(home), pw_uid=1000, pw_gid=1000)
    environment = {'DISPLAY': ':0', 'XAUTHORITY': str(home / '.Xauthority'),
                   'DBUS_SESSION_BUS_ADDRESS': 'unix:path=/run/user/1000/bus', 'XDG_RUNTIME_DIR': '/run/user/1000'}
    with patch.object(sys, 'argv', ['mcp_boundary.py', 'desktop', '/mcp/launch.sh']), \
         patch.dict(os.environ, environment), patch.object(pwd, 'getpwnam', return_value=account), patch('os.execvp') as execute:
        runpy.run_path(str(Path(ready.__file__).with_name('mcp_boundary.py')), run_name='__main__')
    argv = execute.call_args.args[1]
    assert argv[argv.index('--bind') + 1:argv.index('--bind') + 3] == ['/', '/']
    assert '--init-groups' in argv and '--clear-groups' not in argv
    assert all(flag in argv for flag in ('--unshare-net', '--no-new-privs', '--bounding-set=-all'))
    assert '--unshare-pid' not in argv and '--unshare-ipc' not in argv and '--ro-bind' not in argv
    assert [argv[i + 1] for i, flag in enumerate(argv) if flag == '--tmpfs'] == ['/output']
    passed = {argv[i + 1]: argv[i + 2] for i, flag in enumerate(argv) if flag == '--setenv'}
    assert all(passed[key] == value for key, value in environment.items())
    assert argv[argv.index('--dev') + 1] == '/dev'
