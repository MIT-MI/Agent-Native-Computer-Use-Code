"""Optional exploratory tools; method and notes belong to the Builder."""
import hashlib
import inspect
import io
from functools import partial
import json
import math
import re
from pathlib import Path
import shlex
import time
import uuid
import requests
from gui import GUI
from mcp_transport import MCP, deploy, stderr_observation, startup_errors
from records import write_json
from kimi_loop import Loop, PIN, MAX_TOKENS, OUTPUT_LIMIT, LeakError, save, model_payload, record_message

ROOT = Path(__file__).resolve().parents[1]
EARLY_FINISH = json.loads((ROOT / 'config.json').read_text())['early_finish']['budget_fraction']
# With and without analysis of the application.
VARIANTS = {'re': 're', 'no-re': 'no-re'}
# How an MCP's files reach the application: evidence for the Builder and reviewer, never a gate.
# Every access path the desktops provide is listed; software data adds the application's own names.
ROUTES = {
    'internals': r'/proc/[^ "\']*/(mem|maps)|process_vm_(readv|writev)|ptrace|PTRACE_|\bgdb\b|frida|apptool|LD_PRELOAD|dlopen'
                 r'|PyRun_|QMetaObject|invokeMethod|\bg?dbus\b|dbus-send|qdbus',
    'window': r'cua-input|input\.sock|xdotool|XTest|xtest|pyautogui|screenshot|import -window|ImageGrab|scrot|xwd\b|wmctrl|xprop'
              r'|atspi|Atspi',
    'documents': None}
REVIEW = {
    're': 'This build is meant to analyse the application and work through what the analysis recovers: the '
          'application\'s own internal state and operations in the running process. Check that, and where the MCP '
          'drives the window (clicks, keys, typed text, screenshots) or edits document files without the '
          'application although the application holds that state or operation exactly, say so as a next goal. ',
    'no-re': 'This build must not analyse the application, and neither may you: no disassembly, decompilation, '
             'debugging, tracing or hooking, no reading of its process memory, and no inspection of its files. '
             'The application is known only through its window. Report any sign that the MCP or the workspace '
             'does otherwise. '}


def variant(profile):
    return VARIANTS[profile or 're']


def tool(name, description, properties=None, required=()):
    return {'name': name, 'description': description, 'inputSchema': {'type': 'object',
            'properties': properties or {}, 'required': list(required), 'additionalProperties': False}}

STRING = {'type': 'string'}
NUMBER = {'type': 'number'}
INTEGER = {'type': 'integer'}


def read_range(path, offset, length):
    start = offset
    with open(path, encoding='utf-8', errors='replace') as stream:
        while offset > 0:
            part = stream.read(min(offset, io.DEFAULT_BUFFER_SIZE))
            if not part: break
            offset -= len(part)
        text = stream.read(length)
        return {'text': text, 'next_offset': start + len(text), 'eof': not stream.read(1)}


def read_path(path, offset, length):
    import base64
    from PIL import Image, UnidentifiedImageError
    try:
        with Image.open(path) as image:
            if image.format in ('PNG', 'JPEG', 'GIF', 'WEBP'):
                with open(path, 'rb') as stream: data = stream.read()
                mime = Image.MIME[image.format]
            else:
                stream = io.BytesIO(); image.save(stream, format='PNG')
                data, mime = stream.getvalue(), 'image/png'
            return {'image': base64.b64encode(data).decode(), 'mimeType': mime, 'eof': True}
    except UnidentifiedImageError:
        return read_range(path, offset, length)


def changes(before, after):
    if before is None or after is None: return None
    return {path: {'before': before.get(path), 'after': after.get(path)}
            for path in sorted(before.keys() | after.keys()) if before.get(path) != after.get(path)}


def brief(profile, ocr=False):
    """The brief's shared text and its `<!-- only NAME -->` blocks, each running to the next marker.
    A block reaches the Builder if it names its variant, or an option that is on (ocr)."""
    names = {variant(profile), *(['ocr'] if ocr else [])}
    keep, kept = True, []
    for line in (ROOT / 'CONTRACT.md').read_text().split('\n'):
        marker = re.fullmatch(r'<!-- (?:only (\S+)|end) -->', line)
        if marker: keep = not marker[1] or marker[1] in names
        elif keep and not re.fullmatch(r'<!-- .* -->', line): kept.append(line)
    return re.sub(r'\n{3,}', '\n\n', '\n'.join(kept)).replace('{tool_output_characters}', f'{OUTPUT_LIMIT:,}')


# Builder GUI input; also the GUI solver's default tools.
GUI_TOOLS = [
    tool('screenshot', 'Observe the current desktop.'),
    *[tool(n, 'Send desktop ' + n + ' input.', {'x': INTEGER, 'y': INTEGER}, ['x', 'y']) for n in ('click', 'right_click', 'double_click')],
    tool('drag', 'Drag on the desktop.', {k: INTEGER for k in ('x1', 'y1', 'x2', 'y2')}, ['x1', 'y1', 'x2', 'y2']),
    tool('type_text', 'Type literal text.', {'text': STRING}, ['text']),
    tool('key', 'Press keys together.', {'keys': {'type': 'array', 'items': STRING}}, ['keys']),
    tool('scroll', 'Scroll the desktop.', {'direction': {'type': 'string', 'enum': ['up', 'down', 'left', 'right']}, 'count': INTEGER}, ['direction', 'count'])]


class Builder:
    def __init__(self, desktop, folder, key, budgets, profile='re', ocr=False):
        self.desktop, self.folder, self.key, self.budgets = desktop, Path(folder), key, budgets
        self.variant, self.ocr = variant(profile), ocr
        self.folder.mkdir(parents=True, exist_ok=True)
        self.agent = None
        self.save = partial(save, self.folder, key=key)
        self.gui = GUI(desktop, self.folder)
        self.deadline = time.monotonic() + budgets['builder_seconds']
        self.desktop.deadline = self.deadline
        self.action_deadline = self.deadline
        self.last_started = None
        self.reviews = []
        self.trials = []
        self.checked_tools, self.schema_observation = None, None
        self.finish_requested = False
        # The first finish with much of the budget left only asks for confirmation.
        self.finish_confirmed = False
        desktop.require(desktop.command('mkdir -p /output/mcp /output/intermediate', user='root'))
        desktop.require(desktop.command('chown -R ' + desktop.agent + ':' + desktop.agent + ' /output/', user='root'))

    def remaining(self): return max(0, self.deadline - time.monotonic())

    def run_loop(self, job, invoke, folder):
        loop = Loop(job, invoke, folder, self.key,
                    context_window=self.agent.context_window if self.agent else None)
        if self.agent is None: self.agent = loop
        return loop.run()

    def read_file(self, path, offset=0, length=None):
        if type(offset) is not int or offset < 0 or length is not None and (type(length) is not int or length < 0):
            raise ValueError('offset and length must be nonnegative integers')
        default = length is None
        if default: length = OUTPUT_LIMIT
        local = Path(path).resolve()
        if local.is_relative_to(self.folder.resolve()):
            result = read_path(local, offset, length)
        else:
            source = ('import io,json\n' + inspect.getsource(read_range) + '\n' + inspect.getsource(read_path) +
                      '\nprint(json.dumps(read_path(*' + repr([path, offset, length]) + ')))')
            row = self.desktop.command('/usr/bin/python3 -c ' + shlex.quote(source), timeout=max(0, self.action_deadline - time.monotonic()))
            if row['returncode']: return row
            result = json.loads(row['stdout'])
        if 'image' in result: return {**result, 'path': path, 'full_content': path}
        result.update(path=path, offset=offset, length=len(result['text']), full_content=path)
        if default:
            text, eof = result['text'], result['eof']
            def crop(size):
                result.update(text=text[:size], length=size, next_offset=offset + size,
                              eof=eof and size == len(text))
            low, high = 0, len(text)
            while low < high:
                size = (low + high + 1) // 2
                crop(size)
                if len(json.dumps(result, ensure_ascii=False)) <= OUTPUT_LIMIT: low = size
                else: high = size - 1
            crop(low)
        return result

    def write_file(self, path, content):
        self.desktop.write(path, content.encode(), user=self.desktop.agent)
        return {'written': path}

    def routes(self, folder='/opt/frozen-mcp/'):
        """Which of the MCP's files mention each way of reaching the application. Evidence, not a verdict."""
        extra = self.desktop.software.get('route_evidence', {})
        found = {}
        for route, pattern in ROUTES.items():
            pattern = '|'.join(p for p in (pattern, extra.get(route)) if p)
            if not pattern: continue
            row = self.desktop.command('grep -rlaE -e ' + shlex.quote(pattern) + ' ' + folder + ' | sort', user='root', ignore_budget=True)
            files = row['stdout'].split('\n')[:-1]
            bundled = [path for path in files if path.startswith(folder + 'deps/')]
            listed = lambda paths: paths[:50] + (['... %d more' % (len(paths) - 50)] if len(paths) > 50 else [])
            found[route] = {'mcp': listed([path for path in files if path not in bundled]), 'deps': listed(bundled)}
        found['note'] = (
            'Files that mention the application\'s internal state or operations (internals), driving its window '
            '(window), or editing its documents without the application (documents): the MCP\'s own files (mcp) and, '
            'separately, its bundled dependencies under deps/ (deps). ' + (
                'The brief expects an MCP built on what analysis recovers, with the window and direct document editing '
                'only where that was not possible.' if self.variant == 're' else
                'This build learns the application from its window; its internals and files are closed.'))
        return found

    def archive_workspace(self):
        started = time.monotonic()
        tag = uuid.uuid4().hex
        remote = '/opt/cua-world-runtime/workspace-' + tag + '.tar'
        local = self.folder / ('workspace-' + tag + '.tar')
        try:
            self.desktop.require(self.desktop.command('tar -cf ' + remote + ' -C /output .', user='root'))
            self.desktop.copy_from(remote, local)
        finally: self.desktop.require(self.desktop.command('rm -f -- ' + remote, user='root', ignore_budget=True))
        with local.open('rb') as stream: digest = hashlib.file_digest(stream, 'sha256').hexdigest()
        return {'sha256': digest, 'archive': str(local), 'duration_seconds': time.monotonic() - started}

    def snapshot(self):
        return deploy(self.desktop, self.archive_workspace())

    def schema_request(self, tools):
        if tools == self.checked_tools: return self.schema_observation
        payload = model_payload([{'role': 'user', 'content': 'Describe the available interface briefly. Do not call tools.'}], tools)
        payload['max_tokens'] = MAX_TOKENS
        payload['tool_choice'] = 'none'
        conversation = self.folder / 'schema-conversation.jsonl'
        request_path = self.save('schema-request', {'conversation': str(conversation),
            'messages': [record_message(conversation, message, key=self.key) for message in payload['messages']],
            'fields': {k: v for k, v in payload.items() if k != 'messages'}})
        started_at, started = time.time(), time.monotonic()
        try:
            response = requests.post(PIN['endpoint'] + '/chat/completions', json=payload,
                headers={'Authorization': 'Bearer ' + self.key}, timeout=max(0, self.action_deadline - time.monotonic()))
            result = {'http_status': response.status_code, 'response': response.text, 'request': str(request_path)}
        except Exception as exc:
            result = {'error': str(exc), 'request': str(request_path)}
        result.update(started_at=started_at, latency_seconds=time.monotonic() - started)
        result['report'] = str(self.save('schema-response', result))
        self.checked_tools, self.schema_observation = tools, result
        return result

    def try_build(self, calls=None, *, restart, wait_seconds):
        if isinstance(wait_seconds, bool) or not math.isfinite(wait_seconds) or wait_seconds < 0:
            raise ValueError('wait_seconds must be a finite, nonnegative number')
        trial = {'calls': [], 'errors': [], 'restart_requested': restart, 'wait_seconds': wait_seconds}
        def scan(label):
            try: return self.desktop.scan()
            except Exception as exc: trial['errors'].append(label + ': ' + str(exc))
        before = scan('State before calls')
        server = None
        mcp_folder = self.folder / ('mcp-' + uuid.uuid4().hex)
        try:
            started = time.monotonic()
            try:
                snapshot = self.snapshot(); trial['snapshot'] = snapshot
            finally:
                trial['snapshot_duration_seconds'] = time.monotonic() - started
            trial['routes'] = self.routes()
            server = MCP(self.desktop, snapshot['command'], mcp_folder,
                         lifetime_seconds=min(wait_seconds, self.remaining()))
            self.last_started = snapshot
            write_json(self.folder / 'last_started.json', snapshot)
            trial.update(startup=server.initialized, tool_list=server.tools)
            trial['errors'].extend(startup_errors(server.initialized, server.tools))
            if server.remaining() <= 0: raise TimeoutError('MCP did not answer within wait_seconds')
            for call in calls or []:
                try:
                    result = server.call('tools/call', {'name': call['name'], 'arguments': call.get('arguments', {})})
                except TimeoutError as exc:
                    trial['calls'].append({'call': call, 'result': {'error': str(exc)}})
                    raise
                except Exception as exc: result = {'error': str(exc)}
                trial['calls'].append({'call': call, 'result': result})
            tools = server.tools.get('tools') if isinstance(server.tools, dict) else None
            trial['schema_observation'] = self.schema_request(tools) if isinstance(tools, list) and tools else 'No tool list available.'
        except LeakError:
            raise
        except Exception as exc:
            trial['errors'].append(str(exc))
        finally:
            if server:
                try: server.close()
                except Exception as exc: trial['errors'].append('MCP cleanup: ' + str(exc))
        after_calls = scan('State after calls')
        trial.update(stderr_observation(mcp_folder))
        trial['changes_after_calls'] = changes(before, after_calls)
        trial['changes_after_restart'] = None
        if restart:
            try: trial['restart'] = self.desktop.restart()
            except Exception as exc: trial['errors'].append('Application restart: ' + str(exc))
            trial['changes_after_restart'] = changes(after_calls, scan('State after restart'))
        try: trial['screenshot'] = self.gui.screenshot()
        except Exception as exc: trial['errors'].append('Screenshot: ' + str(exc))
        path = self.save('try', trial)
        self.trials.append(str(path))
        # Full diffs are always retained. Return evidence and its location, never
        # a pass/fail verdict or a repaired workspace.
        result = {'report': str(path), 'startup': trial.get('startup'), 'errors': trial['errors'],
                  'changed_after_calls': None if trial['changes_after_calls'] is None else list(trial['changes_after_calls']),
                  'changed_after_restart': None if trial['changes_after_restart'] is None else list(trial['changes_after_restart']),
                  'schema_observation': trial.get('schema_observation'), 'calls': trial['calls'], 'routes': trial.get('routes'),
                  'snapshot_duration_seconds': trial.get('snapshot_duration_seconds'), 'restart_requested': restart}
        result.update(stderr_observation(mcp_folder))
        if 'screenshot' in trial: result.update(trial['screenshot'])
        return result

    def review(self):
        folder = self.folder / ('review-' + uuid.uuid4().hex)
        review_deadline = min(self.deadline, time.monotonic() + self.budgets['review_seconds'])
        observation_errors = []
        try: before = self.desktop.scan()
        except Exception as exc:
            before = None; observation_errors.append('State before review: ' + str(exc))
        server = None
        facts = {}
        try:
            if self.last_started:
                snapshot = deploy(self.desktop, self.last_started)
                server = MCP(self.desktop, snapshot['command'], folder / 'mcp',
                    lifetime_seconds=max(0, review_deadline - time.monotonic()))
                facts = {'startup': server.initialized, 'tool_list': server.tools}
        except Exception as exc: facts = {'startup_error': str(exc)}
        tools = [t for t in self.tools() if t['name'] not in ('try', 'review', 'write_file')]
        tools.append(tool('call_mcp', 'Call a delivered MCP tool with JSON application data.',
                          {'name': STRING, 'arguments': {'type': 'object'}}, ['name', 'arguments']))
        def invoke(name, args):
            if name == 'finish': return args
            if name == 'call_mcp':
                if server is None: return {'isError': True, 'error': 'No MCP started.'}
                return server.call('tools/call', args)
            return self.invoke(name, args)
        job = {'purpose': 'reviewer', 'seconds': max(0, review_deadline - time.monotonic()),
            'tools': {'tools': tools}, 'prompt':
            'Independently explore this application and MCP. You have no future task. '
            'Return observations and suggested next goals through finish. Look for missing objects, settings, files '
            'and computed outputs, and tools accepting scripts, expressions, command lines or code. '
            'Expose application operations, not generic screen primitives such as click, type or screenshot. '
            'Application-native input such as a cell formula is data. Call the MCP as a fresh agent with an '
            'unfamiliar task would, and report what it achieves. Do not repair the workspace. '
            'Report any desktop changes you make. This is advice, not approval.\n' + REVIEW[self.variant] + '\n' + json.dumps(facts)}
        job['last_started_path'] = str(self.folder / 'last_started.json')
        previous = self.action_deadline
        self.action_deadline = time.monotonic() + job['seconds']
        self.desktop.deadline = self.action_deadline
        try:
            result = self.run_loop(job, invoke, folder)
        except LeakError:
            raise
        except Exception as exc:
            result = {'status': 'fail', 'terminal_reason': type(exc).__name__ + ': ' + str(exc),
                'terminal_result': None, 'transcript': str(folder / 'transcript.jsonl'),
                'conversation': str(folder / 'conversation.jsonl')}
        finally:
            self.action_deadline = previous
            self.desktop.deadline = self.deadline
            if server:
                try: server.close()
                except Exception as exc: observation_errors.append('MCP cleanup: ' + str(exc))
        result.update(stderr_observation(folder / 'mcp'))
        if result['terminal_reason'].startswith('key_leak'): raise LeakError('reviewer observed credential leak')
        try: result['desktop_changes'] = changes(before, self.desktop.scan())
        except Exception as exc:
            result['desktop_changes'] = None; observation_errors.append('State after review: ' + str(exc))
        result['observation_errors'] = observation_errors
        result['incomplete'] = result['terminal_reason'] != 'terminated'
        advice = result['terminal_result']
        if advice is None:
            conversation = Path(result['conversation'])
            observations = [json.loads(line) for line in conversation.read_text().splitlines()] if conversation.exists() else []
            advice = {'observations': [m for m in observations if m['role'] == 'assistant'],
                      'stopped_because': result['terminal_reason']}
        path = self.save('review', result); self.reviews.append(str(path))
        return {'report': str(path), 'incomplete': result['incomplete'],
                'status': result.get('status'), 'terminal_reason': result['terminal_reason'],
                'advice': advice, 'transcript': result['transcript'], 'conversation': result['conversation'],
                'desktop_changes': result['desktop_changes'],
                'observation_errors': observation_errors, **stderr_observation(folder / 'mcp')}

    def tools(self):
        return [
            tool('exec', 'Run a shell command; wait at most wait_seconds. Return output head/tail, byte size, PID, log_path and finished. A running command keeps running; use exec to inspect its log or stop its PID.',
                 {'command': STRING, 'wait_seconds': NUMBER}, ['command', 'wait_seconds']),
            tool('read_file', 'Read a desktop file or driver report. Images return as images; text uses Unicode character offset and length, with next_offset and eof. The default text range fits the output allowance.', {'path': STRING, 'offset': INTEGER, 'length': INTEGER}, ['path']),
            tool('write_file', 'Write a UTF-8 desktop file.', {'path': STRING, 'content': STRING}, ['path', 'content']),
            *GUI_TOOLS,
            tool('try', 'Snapshot the workspace, start restricted MCP and run calls. Wait at most wait_seconds for MCP answers, then stop it. Choose whether to restart the application. Return changed paths after calls and optional restart; full diffs stay in the report.',
                 {'restart': {'type': 'boolean'}, 'wait_seconds': NUMBER, 'calls': {'type': 'array', 'items': {'type': 'object', 'properties': {'name': STRING, 'arguments': {'type': 'object'}},
                            'required': ['name', 'arguments'], 'additionalProperties': False}}}, ['restart', 'wait_seconds']),
            tool('review', 'Ask an independent reviewer for observations and next goals on this desktop. Uses up to ' + str(self.budgets['review_seconds']) + ' seconds from your remaining time.'),
            tool('finish', 'Deliver the current workspace if its MCP starts, otherwise the last snapshot that started. With much of your budget left, the first finish asks you to confirm instead. If neither is deliverable, report why and continue; a second finish ends the run anyway. Reviewer: return observations and next goals.',
                 {'notes': STRING})]

    def finish(self, **notes):
        if not self.finish_confirmed and self.remaining() > EARLY_FINISH * self.budgets['builder_seconds']:
            self.finish_confirmed = True
            minutes = round(self.remaining() / 60)
            return {'accepted': False, 'run_continues': True, 'remaining_minutes': minutes, 'routes': self.routes('/output/mcp/'),
                    'note': str(minutes) + ' minutes of your budget remain. Time left at finish is time not spent improving the MCP: '
                            'try it again as a fresh agent with an unfamiliar task would, and improve its weakest part. '
                            'Call finish again to deliver now.'}
        result = {**notes, 'delivered_from': 'last_started', 'errors': []}
        server = None
        mcp_folder = self.folder / ('delivery-' + uuid.uuid4().hex)
        try:
            if self.remaining() > 0:
                snapshot = self.snapshot()
                if self.remaining() > 0:
                    server = MCP(self.desktop, snapshot['command'], mcp_folder,
                                 lifetime_seconds=self.remaining())
                    self.last_started = snapshot
                    write_json(self.folder / 'last_started.json', snapshot)
                    result.update(delivered_from='current_workspace', startup=server.initialized, tool_list=server.tools,
                                  routes=self.routes())
                    result['errors'].extend(startup_errors(server.initialized, server.tools))
        except LeakError:
            raise
        except Exception as exc:
            result['errors'].append(str(exc))
        finally:
            if server:
                try: server.close()
                except Exception as exc: result['errors'].append('MCP cleanup: ' + str(exc))
        result['last_started'] = self.last_started
        result['accepted'] = self.last_started is not None or self.finish_requested
        self.finish_requested = True
        if self.last_started is None:
            result['delivered_from'] = None
            result['delivery_status'] = 'Nothing deliverable: ' + ('; '.join(result['errors']) or 'No MCP started within the remaining budget.')
            result['run_continues'] = not result['accepted']
        result.update(stderr_observation(mcp_folder))
        return result

    def invoke(self, name, args):
        if name == 'exec': return self.desktop.exec(**args)
        if name == 'read_file': return self.read_file(**args)
        if name == 'write_file': return self.write_file(**args)
        if name == 'try': return self.try_build(**args)
        if name == 'review': return self.review(**args)
        if name == 'finish': return self.finish(**args)
        # Without analysis, input goes through cua-input: one typed-code filter sees all that is entered.
        if self.variant == 'no-re' and name != 'screenshot': return self.desktop.window(name, args)
        return self.gui.tools()[name](**args)

    def run(self):
        self.desktop.deadline = self.deadline
        job = {'purpose': 'builder', 'seconds': self.remaining(), 'tools': {'tools': self.tools()},
               'prompt': brief(self.variant, self.ocr) + '\nApplication: ' + self.desktop.software['name'],
               'last_started_path': str(self.folder / 'last_started.json')}
        result = self.run_loop(job, self.invoke, self.folder)
        self.desktop.deadline = None
        try: result['workspace'] = self.archive_workspace()
        except Exception as exc: result['workspace_archive_error'] = str(exc)
        result.update(variant=self.variant, ocr=self.ocr, last_started=self.last_started, reviews=self.reviews, trials=self.trials,
                      delivery=result['terminal_result'] or {'last_started': self.last_started,
                          'delivered_from': 'last_started' if self.last_started else None})
        write_json(self.folder / 'delivery.json', result)
        return result
