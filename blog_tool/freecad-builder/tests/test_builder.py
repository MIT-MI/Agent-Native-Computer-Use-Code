"""Advisory tools retain evidence and never enforce the Builder's method."""
import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'agent'), str(ROOT / 'harness')]
from builder import Builder, changes
from unittest.mock import Mock
import pytest


class Desktop:
    user = agent = 'test'
    software = {'name': 'Example'}
    def __init__(self): self.restarts = 0; self.scans = 0
    def command(self, *args, **kwargs): return {'returncode': 0, 'stdout': '', 'stderr': ''}
    def require(self, result): return result['stdout']
    def scan(self):
        self.scans += 1
        return {'/home/test/.cache/state': {'version': self.scans}, '/tmp/socket': {'special': True}}
    def restart(self): self.restarts += 1; return [{'started': True}]


def build(tmp_path):
    budgets = json.loads((ROOT / 'config.json').read_text())['smoke_budgets']
    builder = Builder(Desktop(), tmp_path, 'test-key', budgets)
    builder.gui.screenshot = lambda: {'observed': True}
    builder.finish_confirmed = True  # the early-finish confirmation has its own test
    return builder


def test_try_keeps_last_started_after_startup_failure_and_reports_caches(tmp_path):
    builder = build(tmp_path)
    builder.last_started = {'sha256': 'previous'}
    with patch.object(builder, 'snapshot', return_value={'command': '/new/mcp/launch.sh'}), \
         patch('builder.MCP', side_effect=RuntimeError('startup failed')):
        result = builder.try_build(restart=True, wait_seconds=1)
    assert builder.last_started == {'sha256': 'previous'}
    assert builder.desktop.restarts == 1
    report = json.loads(Path(result['report']).read_text())
    assert '/home/test/.cache/state' in report['changes_after_calls']
    assert result['changed_after_calls'] == ['/home/test/.cache/state']
    assert result['changed_after_restart'] == ['/home/test/.cache/state']
    assert 'startup failed' in report['errors']
    assert report['screenshot'] == {'observed': True}


def test_started_snapshot_survives_schema_rejection_and_call_failure(tmp_path):
    builder = build(tmp_path)
    server = SimpleNamespace(initialized={'ready': True}, tools={'tools': [{'name': 'edit', 'inputSchema': {}}]},
                             call=lambda *args: {'isError': True}, close=lambda: None, remaining=lambda: 1)
    with patch.object(builder, 'snapshot', return_value={'command': '/new/mcp/launch.sh', 'sha256': 'new'}), \
         patch('builder.MCP', return_value=server), \
         patch.object(builder, 'schema_request', return_value={'http_status': 400, 'response': 'schema rejected'}) as schema:
        result = builder.try_build([{'name': 'edit', 'arguments': {'value': 'literal'}}], restart=True, wait_seconds=1)
    assert schema.call_count == 1
    assert builder.last_started['sha256'] == 'new'
    assert result['schema_observation']['response'] == 'schema rejected'
    assert builder.desktop.restarts == 1


def test_finish_does_not_require_try_or_review(tmp_path):
    builder = build(tmp_path)
    current = {'command': '/mcp', 'sha256': 'current'}
    server = Mock(initialized={'ready': True}, tools={'tools': []})
    with patch.object(builder, 'snapshot', return_value=current), patch('builder.MCP', return_value=server):
        result = builder.invoke('finish', {'notes': 'done'})
    assert result['accepted'] is True and result['last_started'] == current
    server.close.assert_called_once()
    assert builder.reviews == [] and builder.trials == []
    assert {'try', 'review', 'finish'} <= {t['name'] for t in builder.tools()}


@pytest.mark.parametrize('action', ['try', 'finish'])
def test_malformed_startup_is_reported_without_blocking_delivery(tmp_path, action):
    builder = build(tmp_path)
    envelope = {'content': [{'type': 'text', 'text': '{}'}], 'isError': False}
    server = Mock(initialized=envelope, tools=envelope)
    server.remaining.return_value = 1
    artifact = {'command': '/mcp', 'sha256': 'malformed'}
    with patch.object(builder, 'snapshot', return_value=artifact), patch('builder.MCP', return_value=server):
        result = builder.try_build(restart=False, wait_seconds=1) if action == 'try' else builder.finish()
    assert builder.last_started == artifact
    assert any('initialize result.protocolVersion' in error for error in result['errors'])
    assert any('tools/list result.tools' in error for error in result['errors'])
    if action == 'finish': assert result['accepted'] is True
    server.close.assert_called_once()


def test_snapshot_timeout_and_failed_observations_return_evidence(tmp_path):
    builder = build(tmp_path)
    builder.last_started = {'sha256': 'previous'}
    with patch.object(builder, 'snapshot', side_effect=TimeoutError('snapshot timeout')), \
         patch.object(builder.desktop, 'scan', side_effect=TimeoutError('scan timeout')):
        result = builder.try_build(restart=True, wait_seconds=1)
    report = json.loads(Path(result['report']).read_text())
    assert 'snapshot timeout' in report['errors']
    assert report['snapshot_duration_seconds'] >= 0
    assert report['changes_after_calls'] is None
    assert builder.invoke('finish', {})['last_started'] == {'sha256': 'previous'}


def test_review_is_fresh_advice_on_same_desktop_and_may_be_incomplete(tmp_path):
    builder = build(tmp_path)
    seen = {}
    class Reviewer:
        def __init__(self, job, invoke, folder, key, **kwargs):
            seen.update(job=job, invoke=invoke)
            self.folder = folder; folder.mkdir(parents=True)
        def run(self):
            return {'terminal_reason': 'time_budget', 'terminal_result': {'notes': 'Explore another setting.'},
                    'transcript': str(self.folder / 'transcript.jsonl'), 'conversation': str(self.folder / 'conversation.jsonl')}
    with patch('builder.Loop', Reviewer): result = builder.review()
    assert seen['job']['purpose'] == 'reviewer'
    assert result['incomplete'] is True
    assert result['advice']['notes'] == 'Explore another setting.'
    assert builder.desktop.scans == 2
    assert seen['job']['seconds'] <= builder.budgets['review_seconds']


def test_scan_keeps_cache_links_and_special_entries(tmp_path):
    spec = importlib.util.spec_from_file_location('state_scan', ROOT / 'harness/guest/scan.py')
    scan = importlib.util.module_from_spec(spec); spec.loader.exec_module(scan)
    cache = tmp_path / '.cache'; cache.mkdir(); (cache / 'state').write_text('before')
    (tmp_path / 'link').symlink_to(cache / 'state')
    before = scan.scan([tmp_path]); (cache / 'state').write_text('after')
    after = scan.scan([tmp_path])
    assert str(cache / 'state') in changes(before, after)
    assert before[str(tmp_path / 'link')]['target'] == str(cache / 'state')



def test_image_file_command_preserves_newlines_without_splitting_docker_run(tmp_path):
    import subprocess
    from images import write_command
    target = tmp_path / 'desktop.service'
    content = '[Unit]\nAfter=dbus.service\n[Service]\nType=simple\n'
    command = write_command(str(target), content)
    assert '\n' not in command
    subprocess.run(command, shell=True, check=True)
    assert target.read_text() == content


def test_all_desktop_and_toolbox_run_instructions_are_single_lines():
    import images
    commands = []
    class Image:
        def env(self, *args, **kwargs): return self
        def apt_install(self, *args, **kwargs): return self
        def add_local_dir(self, *args, **kwargs): return self
        def add_local_file(self, *args, **kwargs): return self
        def run_commands(self, *args): commands.extend(args); return self
    with patch.object(images.Image, 'from_registry', return_value=Image()):
        image = images.desktop_image(images.desktop_base_image(), json.loads((ROOT / 'software/freecad.json').read_text()))
        images.runtime_image(image, json.loads((ROOT / 'software/freecad.json').read_text()), toolbox=True)
    assert commands
    assert all('\n' not in command for command in commands)


def test_try_does_not_restart_when_builder_declines(tmp_path):
    builder = build(tmp_path)
    with patch.object(builder, 'snapshot', side_effect=RuntimeError('No artifact yet')):
        result = builder.try_build(restart=False, wait_seconds=1)
    assert builder.desktop.restarts == 0 and builder.desktop.scans == 2
    assert result['changed_after_calls'] == ['/home/test/.cache/state']
    assert result['changed_after_restart'] is None
    schema = next(t['inputSchema'] for t in builder.tools() if t['name'] == 'try')
    assert schema['required'] == ['restart', 'wait_seconds']


def test_incomplete_review_returns_observations_without_finish(tmp_path):
    builder = build(tmp_path)
    class Reviewer:
        def __init__(self, job, invoke, folder, key, **kwargs):
            self.folder = folder; folder.mkdir(parents=True)
        def run(self):
            conversation = self.folder / 'conversation.jsonl'
            conversation.write_text(json.dumps({'role': 'assistant', 'content': 'Native settings persisted after reopen.'}) + '\n' +
                json.dumps({'role': 'tool', 'tool_call_id': 'inspect', 'content': 'config found in /home/test/.config/app'}) + '\n')
            return {'terminal_reason': 'time_budget', 'terminal_result': None,
                    'transcript': str(self.folder / 'transcript.jsonl'), 'conversation': str(conversation)}
    with patch('builder.Loop', Reviewer): result = builder.review()
    assert result['incomplete']
    assert 'persisted after reopen' in str(result['advice'])
    assert '/home/test/.config/app' not in str(result['advice'])
    assert all(m['role'] == 'assistant' for m in result['advice']['observations'])
    assert '/home/test/.config/app' in Path(result['conversation']).read_text()
    assert result['transcript'].endswith('transcript.jsonl')


def test_schema_evidence_is_reused_until_tool_list_changes(tmp_path):
    from unittest.mock import Mock
    builder = build(tmp_path)
    first = [{'name': 'inspect', 'inputSchema': {}}]
    changed = [{'name': 'edit', 'inputSchema': {}}]
    with patch('builder.requests.post', return_value=Mock(status_code=400, text='schema rejection')) as post:
        evidence = builder.schema_request(first)
        assert builder.schema_request(json.loads(json.dumps(first))) == evidence
        assert post.call_count == 1
        assert Path(evidence['report']).exists()
        assert builder.schema_request(changed)['http_status'] == 400
        assert post.call_count == 2



def test_finish_attempts_latest_workspace_once_and_falls_back_without_gating(tmp_path):
    from unittest.mock import Mock
    for error in (None, RuntimeError('startup failed'), TimeoutError('owner budget expired during MCP call')):
        builder = build(tmp_path)
        old = builder.last_started = {'sha256': 'previous'}
        current = {'command': '/current/mcp/launch.sh', 'sha256': 'current'}
        server = Mock(initialized={'ready': True}, tools={'tools': []})
        with patch.object(builder, 'snapshot', return_value=current) as snapshot, \
             patch('builder.MCP', return_value=server, side_effect=error) as start:
            result = builder.invoke('finish', {'notes': 'finished'})
        assert snapshot.call_count == start.call_count == 1
        assert 0 < start.call_args.kwargs['lifetime_seconds'] <= builder.budgets['builder_seconds']
        assert result['last_started'] == (old if error else current)
        assert result['delivered_from'] == ('last_started' if error else 'current_workspace')
        assert result['notes'] == 'finished'
        assert result['accepted'] is True
        if error: assert result['errors'] == [str(error)]
        else: server.close.assert_called_once()


def test_expiry_makes_no_new_startup_attempt(tmp_path):
    builder = build(tmp_path); builder.last_started = {'sha256': 'started'}
    with patch.object(builder, 'remaining', return_value=0), patch.object(builder, 'snapshot') as snapshot, \
         patch('builder.MCP') as start:
        result = builder.finish()
    snapshot.assert_not_called(); start.assert_not_called()
    assert result['last_started'] == builder.last_started
    class Expired:
        def __init__(self, *args, **kwargs): pass
        def run(self): return {'terminal_reason': 'time_budget', 'terminal_result': None}
    with patch('builder.Loop', Expired), patch('builder.MCP') as start, \
         patch.object(builder, 'archive_workspace', return_value={'archive': 'workspace'}):
        result = builder.run()
    start.assert_not_called()
    assert result['delivery']['delivered_from'] == 'last_started'


def test_review_finish_returns_advice_without_delivering(tmp_path):
    builder = build(tmp_path)
    class Reviewer:
        def __init__(self, job, invoke, folder, key, **kwargs): self.invoke = invoke
        def run(self):
            return {'terminal_reason': 'terminated', 'terminal_result': self.invoke('finish', {'notes': 'observed'}),
                    'transcript': 'review-transcript', 'conversation': 'review-conversation'}
    with patch('builder.Loop', Reviewer), patch.object(builder, 'finish') as finish:
        result = builder.review()
    finish.assert_not_called()
    assert result['advice'] == {'notes': 'observed'}


def test_solver_receives_only_budget_left_after_startup(tmp_path):
    import time
    from unittest.mock import Mock
    from solve import mcp_solve
    budgets = json.loads((ROOT / 'config.json').read_text())['smoke_budgets']
    desktop = SimpleNamespace(deadline=time.monotonic())
    server = Mock(tools={'tools': []}, initialized={'instructions': 'Use native files.'})
    loop = Mock(); loop.run.return_value = {'calls': []}
    with patch('kimi_loop.Loop', return_value=loop) as create:
        mcp_solve(desktop, tmp_path, server, {'description': 'task'}, budgets, '')
    assert create.call_args.args[0]['seconds'] == 0
    assert create.call_args.args[0]['max_tools'] == budgets['solve_turns']
    assert create.call_args.args[0]['system_prompt'].endswith('Use native files.')


def test_try_requires_wait_and_stops_on_unanswered_call(tmp_path):
    builder = build(tmp_path)
    schema = next(t['inputSchema'] for t in builder.tools() if t['name'] == 'try')
    assert 'wait_seconds' in schema['required'] and schema['properties']['wait_seconds'] == {'type': 'number'}
    with pytest.raises(TypeError): builder.try_build(restart=False)
    server = Mock(initialized={'protocolVersion': '2024-11-05', 'capabilities': {}, 'serverInfo': {}},
                  tools={'tools': [{'name': 'hang', 'inputSchema': {}}]})
    server.remaining.return_value = 1
    server.call.side_effect = TimeoutError('MCP did not answer within requested wait')
    with patch.object(builder, 'snapshot', return_value={'command': '/mcp/launch.sh'}), \
         patch('builder.MCP', return_value=server) as start, patch.object(builder, 'schema_request') as schema:
        result = builder.try_build([{'name': 'hang'}, {'name': 'later'}], restart=False, wait_seconds=0.1)
    assert start.call_args.kwargs['lifetime_seconds'] == 0.1
    assert 'did not answer' in result['errors'][0]
    server.close.assert_called_once(); server.call.assert_called_once(); schema.assert_not_called()
    assert builder.desktop.restarts == 0 and builder.last_started['command'] == '/mcp/launch.sh'


def test_reviewer_reuses_builder_metadata_without_sharing_conversation(tmp_path):
    from kimi_loop import Loop, PIN
    from test_kimi_loop import reply, call
    builder = build(tmp_path)
    responses = [reply(call('review')), reply(call('finish')), reply(call('finish'))]
    metadata = Mock(); metadata.json.return_value = {'data': [{'id': PIN['model'], 'context_length': 1048576}]}
    job = {'purpose': 'builder', 'prompt': 'private builder context', 'tools': {'tools': builder.tools()}}
    with patch('kimi_loop.requests.get', return_value=metadata) as get, \
         patch.object(Loop, 'http', side_effect=lambda payload: (200, responses.pop(0))), \
         patch.object(builder, 'finish', return_value={}):
        result = builder.run_loop(job, builder.invoke, builder.folder)
    get.assert_called_once()
    assert result['terminal_reason'] == 'terminated'
    review = json.loads(Path(builder.reviews[0]).read_text())
    assert 'private builder context' not in Path(review['conversation']).read_text()


@pytest.mark.parametrize('failure', [False, True])
def test_workspace_archive_is_removed_from_guest_even_if_copy_fails(tmp_path, failure):
    builder = build(tmp_path)
    def copy(source, destination):
        if failure: raise RuntimeError('copy failed')
        Path(destination).write_bytes(b'workspace')
    builder.desktop.copy_from = Mock(side_effect=copy)
    builder.desktop.command = Mock(return_value={'stdout': ''})
    if failure:
        with pytest.raises(RuntimeError, match='copy failed'): builder.archive_workspace()
    else:
        assert Path(builder.archive_workspace()['archive']).read_bytes() == b'workspace'
    cleanup = builder.desktop.command.call_args
    assert cleanup.args[0].startswith('rm -f -- /opt/cua-world-runtime/workspace-')
    assert cleanup.kwargs['ignore_budget']


@pytest.mark.parametrize('method', ['try', 'finish', 'review'])
def test_startup_failure_returns_stderr_tail_and_existing_log_path(tmp_path, method):
    from kimi_loop import OUTPUT_LIMIT
    builder = build(tmp_path)
    builder.last_started = {'archive': 'previous'} if method == 'review' else None
    stderr = 'start-' + 'detail' * OUTPUT_LIMIT + '-native error'
    def start(desktop, command, folder, **kwargs):
        folder.mkdir(parents=True, exist_ok=True); (folder / 'stderr.log').write_text(stderr)
        raise RuntimeError('MCP stdout closed')
    reviewer = {'terminal_reason': 'terminated', 'terminal_result': {'notes': 'startup failed'},
                'transcript': 'transcript', 'conversation': 'conversation'}
    with patch.object(builder, 'snapshot', return_value={'command': '/mcp'}), \
         patch('builder.deploy', return_value={'command': '/mcp'}), patch('builder.MCP', side_effect=start), \
         patch.object(builder, 'run_loop', return_value=reviewer):
        if method == 'try': result = builder.try_build(restart=False, wait_seconds=1)
        elif method == 'review': result = builder.review()
        else:
            result = builder.finish()
            assert result['accepted'] is False and result['run_continues'] is True
            assert result['delivery_status'] == 'Nothing deliverable: MCP stdout closed'
            assert builder.finish()['accepted'] is True
    assert result['stderr_tail'].endswith('-native error') and len(result['stderr_tail']) <= OUTPUT_LIMIT
    assert Path(result['stderr_log']).read_text() == stderr


def test_builder_schemas_describe_actual_input_types_and_review_budget(tmp_path):
    builder = build(tmp_path); tools = {t['name']: t for t in builder.tools()}
    for name in ('click', 'right_click', 'double_click', 'drag'):
        assert all(v['type'] == 'integer' for v in tools[name]['inputSchema']['properties'].values())
    assert tools['scroll']['inputSchema']['properties']['direction']['enum'] == ['up', 'down', 'left', 'right']
    assert str(builder.budgets['review_seconds']) in tools['review']['description']


def test_read_file_ranges_do_not_fetch_the_whole_desktop_file(tmp_path):
    import subprocess
    from kimi_loop import OUTPUT_LIMIT
    builder = build(tmp_path / 'evidence')
    local = builder.folder / 'output.json'; remote = tmp_path / 'desktop.txt'
    content = '文字' * OUTPUT_LIMIT
    local.write_text(content); remote.write_text(content)
    def command(command, **kwargs):
        import shlex
        args = shlex.split(command)
        row = subprocess.run([sys.executable, *args[1:]], capture_output=True, text=True, check=True)
        return {'returncode': row.returncode, 'stdout': row.stdout, 'stderr': row.stderr}
    builder.desktop.command = Mock(side_effect=command)
    for path in (local, remote):
        result = builder.read_file(str(path), offset=OUTPUT_LIMIT, length=7)
        assert result['text'] == content[OUTPUT_LIMIT:OUTPUT_LIMIT + 7]
        assert result['offset'] == OUTPUT_LIMIT and result['length'] == 7
    assert 'cat --' not in builder.desktop.command.call_args.args[0]


@pytest.mark.parametrize('raised', [False, True])
def test_review_errors_return_advice_and_close_mcp_without_discarding_delivery(tmp_path, raised):
    builder = build(tmp_path); artifact = builder.last_started = {'archive': 'started'}
    folder = tmp_path / 'review-conversation.jsonl'
    folder.write_text(json.dumps({'role': 'assistant', 'content': 'Export is missing.'}) + '\n')
    result = {'status': 'infrastructure_failure', 'terminal_reason': 'endpoint_error: HTTP 400',
              'terminal_result': None, 'transcript': 'transcript', 'conversation': str(folder)}
    server = Mock(initialized={}, tools={})
    with patch('builder.deploy', return_value={'command': '/mcp'}), patch('builder.MCP', return_value=server), \
         patch.object(builder, 'run_loop', side_effect=RuntimeError('review crashed') if raised else None, return_value=result):
        observation = builder.review()
    server.close.assert_called_once()
    assert builder.last_started == artifact and observation['incomplete']
    assert ('review crashed' if raised else 'HTTP 400') in observation['terminal_reason']
    if not raised: assert 'Export is missing.' in str(observation['advice'])
    assert Path(observation['report']).exists()


@pytest.mark.parametrize('characters', ['\"\\\n文字', '\\'])
def test_default_read_ranges_fit_context_and_walk_escaped_unicode_without_gaps(tmp_path, characters):
    from kimi_loop import Loop, OUTPUT_LIMIT
    from test_kimi_loop import call
    builder = build(tmp_path)
    path = tmp_path / 'original.txt'; text = (characters * OUTPUT_LIMIT) + 'end'
    path.write_text(text)
    loop = Loop({'purpose': 'builder', 'prompt': 'brief', 'tools': {'tools': builder.tools()}},
                builder.invoke, tmp_path / 'loop', '')
    pieces = []; offset = 0
    while True:
        loop.call(call('read_file', {'path': str(path), 'offset': offset}))
        content = loop.messages[-2]['content']
        assert len(content) <= OUTPUT_LIMIT and 'Output abbreviated' not in content
        part = json.loads(content); pieces.append(part['text'])
        assert part['next_offset'] == offset + len(part['text'])
        offset = part['next_offset']
        if part['eof']: break
    assert ''.join(pieces) == text
    loop.call(call('read_file', {'path': str(path), 'length': len(text)}))
    assert 'full output: ' + str(path) in loop.messages[-2]['content']


def test_toolbox_layers_precede_guest_copy():
    import images
    events = []
    class Image:
        def apt_install(self, *packages): events.append(('apt', packages)); return self
        def run_commands(self, *commands): events.extend(('run', c) for c in commands); return self
        def add_local_file(self, *args, **kwargs): events.append(('file', args)); return self
        def add_local_dir(self, *args, **kwargs): events.append(('guest', args)); return self
    software = json.loads((ROOT / 'software/freecad.json').read_text())
    images.runtime_image(Image(), software, toolbox=True)
    guest = next(i for i, event in enumerate(events) if event[0] == 'guest')
    pins = json.loads((ROOT / 'toolbox.json').read_text())
    assert ('apt', tuple(pins['apt'])) in events[:guest]
    assert all(i < guest for i, event in enumerate(events) if event[0] == 'apt')
    for archive in pins['archives']:
        assert any(event[0] == 'run' and archive['url'] in event[1] and archive['sha256'] in event[1]
                   for event in events[:guest])
    assert any(event[0] == 'run' and 'pip install' in event[1] for event in events[:guest])
    assert any(event[0] == 'run' and event[1].startswith('chmod 0755 ') and
               '/opt/cua-world-runtime/atspi.py' in event[1] and '/opt/cua-world-runtime/cdp.py' in event[1]
               for event in events[guest:])
    assert ('run', 'python3 -m pip freeze > /opt/cua-world-runtime/python-packages.txt') in events[guest:]
    assert events[-1] == ('run', images.PACKAGE_QUERY + ' > ' + images.PACKAGE_MANIFEST)


def test_guest_refresh_only_copies_scripts_and_restores_executable_modes():
    import images
    image = Mock(); image.add_local_dir.return_value = image; image.run_commands.return_value = image
    assert images.guest_image(image, toolbox=True) is image
    image.add_local_dir.assert_called_once_with(images.ASSETS, '/opt/cua-world-runtime', copy=True,
                                                ignore=['**/__pycache__', '**/*.pyc'])
    image.run_commands.assert_called_once_with('chmod 0755 /opt/cua-world-runtime/appctl.py '
        '/opt/cua-world-runtime/session.py /opt/cua-world-runtime/atspi.py /opt/cua-world-runtime/cdp.py')
    image.apt_install.assert_not_called(); image.pip_install.assert_not_called()


@pytest.mark.parametrize('remote,format', [(False, 'PNG'), (True, 'JPEG'), (True, 'TIFF')])
def test_read_file_images_are_visual_inputs_even_without_a_file_extension(tmp_path, remote, format):
    import base64
    import io
    import subprocess
    import shlex
    from PIL import Image
    from kimi_loop import Loop
    builder = build(tmp_path / 'evidence')
    path = (tmp_path if remote else builder.folder) / 'image-without-extension'
    Image.new('RGB', (32, 24), 'blue').save(path, format=format)
    def command(command, **kwargs):
        args = shlex.split(command)
        row = subprocess.run([sys.executable, *args[1:]], capture_output=True, text=True, check=True)
        return {'returncode': row.returncode, 'stdout': row.stdout, 'stderr': row.stderr}
    builder.desktop.command = Mock(side_effect=command)
    result = builder.read_file(str(path))
    assert result['path'] == result['full_content'] == str(path) and result['eof']
    data = base64.b64decode(result['image'])
    with Image.open(io.BytesIO(data)) as image:
        assert image.size == (32, 24) and image.getpixel((0, 0))[2] > 250
        assert image.format == ('PNG' if format == 'TIFF' else format)
    loop = Loop({'purpose': 'builder', 'prompt': 'brief', 'tools': {'tools': builder.tools()}},
                builder.invoke, tmp_path / 'loop', '')
    content = loop.content(result)
    assert len([block for block in content if block['type'] == 'image_url']) == 1
    assert 'base64' not in content[0]['text'] and str(path) in content[0]['text']


def test_screenshot_path_can_be_reopened_as_an_image(tmp_path):
    from PIL import Image
    from gui import GUI
    builder = build(tmp_path)
    builder.gui = GUI(builder.desktop, builder.folder)
    builder.desktop.capture_screenshot = Mock(side_effect=lambda path: Image.new('RGB', (8, 8), 'red').save(path))
    screenshot = builder.gui.screenshot()
    reopened = builder.read_file(screenshot['path'])
    assert reopened['image'] == screenshot['image'] and reopened['mimeType'] == 'image/png'
