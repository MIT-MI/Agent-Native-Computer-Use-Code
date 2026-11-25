import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'agent'))
import kimi_reference as upstream
from kimi_loop import Loop, PIN, CONTINUE
from kimi_loop import OUTPUT_LIMIT, MAX_TOKENS
from kimi_loop import BudgetError, LeakError
from kimi_loop import EndpointError
from unittest.mock import Mock
import pytest
import requests


def call(name, arguments=None):
    return {'id': name, 'type': 'function', 'function': {'name': name, 'arguments': json.dumps(arguments or {})}}


def reply(*calls):
    return {'model': PIN['model'], 'usage': {'prompt_tokens': 1, 'completion_tokens': 1, 'total_tokens': 2}, 'choices': [{
        'finish_reason': 'tool_calls' if calls else 'stop', 'message': {'role': 'assistant',
        'content': 'working', 'reasoning_content': 'preserved', 'tool_calls': list(calls)}}]}


def archived_messages(path):
    record = json.loads(Path(path).read_text())
    with Path(record['conversation']).open('rb') as stream:
        messages = []
        for ref in record['messages']:
            stream.seek(ref['offset']); messages.append(json.loads(stream.read(ref['length'])))
    return messages


class LoopTests(unittest.TestCase):
    def loop(self, replies, role='builder', invoke=None, seconds=100):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        job = {'purpose': role, 'prompt': 'test', 'seconds': seconds,
            'tools': {'tools': [{'name': n, 'inputSchema': {'type': 'object'}} for n in ('write', 'finish')]},
            'terminate_tool': 'finish', 'context_window': 1048576}
        self.requests = []
        def request(payload):
            self.requests.append(payload)
            value = replies.pop(0)
            return value if isinstance(value, tuple) else (200, json.dumps(value))
        return Loop(job, invoke or (lambda n, a: {'accepted': True}), self.tmp.name,
                    'test-secret', request=request, context_window=job.pop('context_window'))

    def test_builder_and_reviewer_continue_without_calls(self):
        for role in ('builder', 'reviewer'):
            loop = self.loop([reply()] * (upstream.PREDICT_RETRIES + 1) + [reply(call('finish'))], role)
            result = loop.run()
            self.assertEqual(result['terminal_reason'], 'terminated')
            observations = [json.loads(m['content'].removeprefix(CONTINUE + '\n'))
                for m in self.requests[-1]['messages'] if str(m.get('content')).startswith(CONTINUE + '\n')]
            self.assertTrue(observations and all('remaining_seconds' in row for row in observations))
            if role == 'builder':
                self.assertTrue(all(row['delivery_if_time_expired'] == 'No MCP snapshot has started yet.' for row in observations))
            self.assertFalse(any('stop exploring' in str(m) for m in self.requests[-1]['messages']))

    def test_invalid_calls_continue_and_multiple_calls_run_in_order(self):
        seen = []
        loop = self.loop([reply(call('missing'))] * (upstream.PREDICT_RETRIES + 1) +
            [reply(call('write', {'value': 'first'}), call('write', {'value': 'second'}), call('finish'))],
            invoke=lambda n, a: seen.append((n, a)) or {'accepted': True})
        self.assertEqual(loop.run()['terminal_reason'], 'terminated')
        self.assertEqual([a.get('value') for n, a in seen if n == 'write'], ['first', 'second'])
        self.assertIn('unknown tool: missing', str(self.requests[-1]['messages']))

    def test_transport_errors_retry_within_budget_for_every_role(self):
        for role in ('builder', 'reviewer', 'mcp-solve'):
            loop = self.loop([(503, 'unavailable')] * (upstream.HTTP_RETRIES + 1) + [reply(call('finish'))], role)
            with patch('time.sleep') as sleep:
                self.assertEqual(loop.run()['terminal_reason'], 'terminated')
            self.assertTrue(all(args.args == (upstream.RETRY_SLEEP,) for args in sleep.call_args_list))
            self.assertEqual(len(list(Path(self.tmp.name).glob('request-*.json'))), 1)

    def test_malformed_calls_are_returned_as_errors(self):
        loop = self.loop([reply(None, 'broken', {}), reply(call('finish'))])
        self.assertEqual(loop.run()['terminal_reason'], 'terminated')
        self.assertTrue(all(row['isError'] for row in loop.tool_results[:-1]))

    def test_solver_has_no_gui_parity_limits(self):
        seen = []
        loop = self.loop([reply()] * (upstream.PREDICT_RETRIES + 1) + [reply(call('missing'))] * (upstream.PREDICT_RETRIES + 1) +
                         [reply(call('write'), call('finish'))],
                         'mcp-solve', invoke=lambda *a: seen.append(a) or {'accepted': True})
        result = loop.run()
        self.assertEqual(result['terminal_reason'], 'terminated')
        self.assertEqual([name for name, args in seen], ['write', 'finish'])
        self.assertTrue(all(r['tool_choice'] == 'auto' and r['parallel_tool_calls'] for r in self.requests))
        observations = [m for m in self.requests[-1]['messages'] if 'remaining_turns' in str(m.get('content'))]
        self.assertTrue(observations)

    def test_request_timeout_is_the_remaining_budget_for_every_role(self):
        for role in ('builder', 'reviewer', 'mcp-solve'):
            loop = self.loop([], role)
            with patch.object(loop, 'remaining', return_value=12345.0), patch('kimi_loop.requests.post') as post:
                loop.http({})
            self.assertEqual(post.call_args.kwargs['timeout'], 12345.0)

    def test_full_outputs_and_older_screenshots_are_retained_on_disk(self):
        loop = self.loop([reply(call('finish'))])
        text = 'x' * 10000
        self.assertEqual(json.loads(loop.content(text)[0]['text'])['result'], text)
        self.assertEqual(json.loads(next(Path(self.tmp.name).glob('output-*.json')).read_text()), text)
        loop.messages = [{'role': 'user', 'content': [{'type': 'image_url', 'image_url': {'url': 'data:x'}}]} for _ in range(8)]
        loop.trim_images()
        self.assertEqual(len(list(Path(self.tmp.name).glob('screenshot-*.json'))), 8 - upstream.MAX_IMAGE_HISTORY_LENGTH)
        self.assertEqual(sum(c['type'] == 'image_url' for m in loop.messages for c in m['content']), upstream.MAX_IMAGE_HISTORY_LENGTH)

    def test_endpoint_context_report_archives_full_history(self):
        loop = self.loop([(400, 'context_length_exceeded'), reply(), reply(call('finish'))])
        loop.messages.append({'role': 'user', 'content': 'retain this exact evidence'})
        with patch('time.sleep'):
            self.assertEqual(loop.run()['terminal_reason'], 'terminated')
        path = next(Path(self.tmp.name).glob('context-*.json'))
        self.assertIn('retain this exact evidence', str(archived_messages(path)))
        self.assertIn('Summary of previous work', str(self.requests[-1]['messages']))
        self.assertIn(str(loop.conversation), str(self.requests[-1]['messages']))

    def test_budget_and_tool_error_do_not_invent_success(self):
        loop = self.loop([], seconds=0)
        self.assertEqual(loop.run()['terminal_reason'], 'time_budget')
        def invoke(name, args):
            if name == 'write': raise ValueError('actual tool error')
            return {'delivered': True}
        loop = self.loop([reply(call('write')), reply(call('finish'))], invoke=invoke)
        self.assertEqual(loop.run()['terminal_reason'], 'terminated')
        self.assertIn('actual tool error', str(self.requests[-1]['messages']))

if __name__ == '__main__': unittest.main()


def test_solver_tool_errors_consume_turns_without_prediction_retries(tmp_path):
    replies = [reply(call('write')), reply(call('finish'))]
    job = {'purpose': 'mcp-solve', 'prompt': 'task', 'tools': {'tools': [
        {'name': n, 'inputSchema': {'type': 'object'}} for n in ('write', 'finish')]}, 'terminate_tool': 'finish', 'context_window': 1048576}
    loop = Loop(job, lambda name, args: {'isError': True} if name == 'write' else {'accepted': True},
                tmp_path, '', request=lambda payload: (200, replies.pop(0)), context_window=job.pop('context_window'))
    result = loop.run()
    assert result['terminal_reason'] == 'terminated' and result['steps'] == 2
    assert result['calls'][0]['arguments_verbatim'] == '{}'


def test_builder_and_reviewer_compact_from_reported_usage_and_keep_latest_images(tmp_path):
    for role in ('builder', 'reviewer'):
        image = 'A' * 1048576
        summary = reply(); summary['choices'][0]['message']['content'] = 'Read /output/intermediate/notes next.'
        high = reply(call('inspect')); high['usage'] = {'prompt_tokens': PIN['max_tokens'], 'completion_tokens': 1}
        # Summary usage describes the old context, not the resumed request.
        summary['usage'] = high['usage']
        replies = [high, summary, reply(call('finish'))]
        requests = []
        job = {'purpose': role, 'prompt': 'Keep this original brief.', 'context_window': PIN['max_tokens'] * 2,
               'tools': {'tools': [{'name': n, 'inputSchema': {'type': 'object'}} for n in ('inspect', 'finish')]}}
        def request(payload):
            requests.append(payload)
            assert all(payload[k] == v for k, v in PIN.items() if k not in ('endpoint', 'reasoning_effort', 'max_tokens'))
            assert payload['max_tokens'] == MAX_TOKENS
            assert payload['reasoning'] == {'effort': PIN['reasoning_effort'], 'exclude': False}
            assert 'reasoning_effort' not in payload
            return 200, replies.pop(0)
        loop = Loop(job, lambda name, args: {'image': image, 'mimeType': 'image/png', 'fact': 'native state'}
                    if name == 'inspect' else {}, tmp_path / role, '', request=request, context_window=job.pop('context_window'))
        result = loop.run()
        assert result['terminal_reason'] == 'terminated'
        assert len(result['compactions']) == 1
        assert 'tools' not in requests[1]
        assert 'base64,' not in json.dumps(requests[1]) and image not in json.dumps(requests[1])
        assert 'Image saved at' in requests[1]['messages'][-1]['content']
        saved_image = json.loads(next((tmp_path / role).glob('screenshot-*.json')).read_text())
        assert saved_image['image_url']['url'].endswith(image)
        resumed = requests[-1]['messages']
        assert resumed[1]['content'] == job['prompt']
        assert 'read /output/intermediate/notes next' in resumed[2]['content'].lower()
        assert resumed[-3]['tool_calls'][0]['function']['name'] == 'inspect'
        assert resumed[-2]['tool_call_id'] == 'inspect'
        assert resumed[-1]['content'][-1] == saved_image
        assert image in Path(result['conversation']).read_text()


def test_several_screenshots_and_try_stay_as_images_despite_serialized_size(tmp_path):
    image = 'A' * 1048576
    actions = ['screenshot', 'screenshot', 'screenshot', 'try', 'finish']
    replies = [reply(call(name)) for name in actions]
    for response in replies:
        response['usage'] = {'prompt_tokens': 10800, 'completion_tokens': 100}
    requests = []
    job = {'purpose': 'builder', 'prompt': 'Observe the desktop and try the MCP.', 'context_window': 1048576,
           'tools': {'tools': [{'name': n, 'inputSchema': {'type': 'object'}} for n in set(actions)]}}
    def request(payload):
        requests.append(payload)
        return 200, replies.pop(0)
    def invoke(name, args):
        return {} if name == 'finish' else {'image': image, 'mimeType': 'image/png',
            'report': '/reports/try.json' if name == 'try' else '/reports/screenshot.json',
            'changed_after_calls': ['/home/ga/.config/app']}
    window = job.pop('context_window')
    result = Loop(job, invoke, tmp_path, '', request=request, context_window=window).run()
    assert result['terminal_reason'] == 'terminated' and result['compactions'] == []
    assert len(requests) == len(actions)
    assert len(json.dumps(requests[-1])) > window
    images = [block for m in requests[-1]['messages'] if isinstance(m.get('content'), list)
              for block in m['content'] if block['type'] == 'image_url']
    assert len(images) == upstream.MAX_IMAGE_HISTORY_LENGTH
    assert all(block['image_url']['url'].endswith(image) for block in images)
    assert '/reports/try.json' in json.dumps(requests[-1])


def test_missing_endpoint_window_is_reported_without_a_model_request(tmp_path):
    from unittest.mock import Mock
    job = {'purpose': 'builder', 'prompt': 'test', 'tools': {'tools': []}}
    loop = Loop(job, lambda *args: None, tmp_path, '', request=Mock())
    response = Mock(); response.json.return_value = {'data': [{'id': PIN['model']}]}
    with patch('kimi_loop.requests.get', return_value=response): result = loop.run()
    assert 'Ask the owner' in result['terminal_reason']
    loop.request.assert_not_called()


def test_summary_length_rejection_replaces_more_history_without_losing_disk_evidence(tmp_path):
    replies = [(400, 'context_length_exceeded'), (200, reply()), (200, reply(call('finish')))]
    requests = []
    def request(payload):
        requests.append(payload)
        return replies.pop(0)
    loop = Loop({'purpose': 'builder', 'prompt': 'goal', 'context_window': 1048576,
                 'tools': {'tools': [{'name': 'finish', 'inputSchema': {}}]}},
                lambda *args: {}, tmp_path, '', request=request, context_window=1048576)
    loop.append({'role': 'user', 'content': 'native observation at /output/intermediate/notes'})
    with patch('time.sleep'): loop.compact('test preflight')
    assert 'native observation' in requests[0]['messages'][-1]['content']
    assert 'Full previous message saved at' in requests[1]['messages'][-1]['content']
    assert 'native observation' in loop.conversation.read_text()
    assert loop.run()['terminal_reason'] == 'terminated'


def test_summary_length_rejection_can_replace_old_tool_arguments_with_a_saved_path(tmp_path):
    job = {'purpose': 'builder', 'prompt': 'brief', 'context_window': PIN['max_tokens'] * 2,
           'tools': {'tools': []}}
    code = 'native implementation\n' * PIN['max_tokens']
    requests = []
    def request(payload):
        requests.append(payload)
        source = json.loads(payload['messages'][-1]['content'])['conversation']
        if any(m.get('tool_calls') for m in source): return 400, 'context_length_exceeded'
        return 200, reply()
    loop = Loop(job, lambda *args: {}, tmp_path, '', request=request, context_window=job.pop('context_window'))
    loop.append({'role': 'assistant', 'content': '', 'tool_calls': [call('write_file', {'content': code})]})
    loop.append({'role': 'tool', 'tool_call_id': 'write_file', 'content': 'saved'})
    loop.turn_start = len(loop.messages)
    loop.append({'role': 'user', 'content': 'Continue from notes.'})
    with patch('time.sleep'): loop.compact('reported_usage')
    saved = archived_messages(next(tmp_path.glob('context-*.json')))
    assert json.loads(saved[2]['tool_calls'][0]['function']['arguments'])['content'] == code
    assert 'Full previous message saved at' in requests[-1]['messages'][-1]['content']
    assert len(requests) == 2  # The huge assistant message goes before the tiny tool result.


def test_growth_within_turn_uses_endpoint_rejection_and_retains_image(tmp_path):
    image = {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,AAAA'}}
    requests = []
    responses = [reply(call('screenshot')), (400, 'context length exceeded'), reply(), reply(call('finish'))]
    def request(payload):
        requests.append(payload)
        response = responses.pop(0)
        return response if isinstance(response, tuple) else (200, response)
    loop = Loop({'purpose': 'builder', 'prompt': 'brief', 'context_window': 1048576,
                 'tools': {'tools': [{'name': n, 'inputSchema': {}} for n in ('screenshot', 'finish')]}},
                lambda name, args: {'image': 'AAAA', 'mimeType': 'image/png'} if name == 'screenshot' else {},
                tmp_path, '', request=request, context_window=1048576)
    with patch('time.sleep'): result = loop.run()
    assert result['terminal_reason'] == 'terminated' and len(result['compactions']) == 1
    assert 'base64,' not in json.dumps(requests[2])
    assert requests[-1]['messages'][-1]['content'][-1] == image


@pytest.mark.parametrize('source', ['model', 'tool'])
@pytest.mark.parametrize('error', [BudgetError('time_budget'), LeakError('withheld')])
def test_solver_budget_and_leak_errors_end_the_run(tmp_path, source, error):
    loop = Loop({'purpose': 'mcp-solve', 'prompt': 'task', 'tools': {'tools': [{'name': 'write', 'inputSchema': {}}]}},
                Mock(side_effect=error), tmp_path, '', request=Mock(return_value=(200, reply(call('write')))),
                context_window=1048576)
    if source == 'model': loop.request.side_effect = error
    result = loop.run()
    assert result['terminal_reason'].startswith('key_leak' if isinstance(error, LeakError) else 'time_budget')
    assert loop.request.call_count == 1


def test_metadata_retries_and_empty_summary_retries_preserve_evidence(tmp_path):
    import requests
    empty = reply(); empty['choices'][0]['message']['content'] = ' '
    loop = Loop({'purpose': 'builder', 'prompt': 'brief', 'tools': {'tools': []}}, lambda *args: {}, tmp_path, '',
                request=Mock(side_effect=[(200, empty), (200, reply())]))
    metadata = Mock(); metadata.json.return_value = {'data': [{'id': PIN['model'], 'context_length': 1048576}]}
    with patch('kimi_loop.requests.get', side_effect=[requests.ConnectionError('offline'), metadata]) as get, patch('time.sleep') as sleep:
        assert loop.read_context_window() == 1048576
        loop.compact('test')
    assert get.call_count == 2 and loop.request.call_count == 2
    assert all(c.args == (upstream.RETRY_SLEEP,) for c in sleep.call_args_list)
    assert len(list(tmp_path.glob('request-*.json'))) == 1
    assert 'empty summary' in (tmp_path / 'transcript.jsonl').read_text()
    assert 'offline' in next(tmp_path.glob('models-error-*.json')).read_text()


@pytest.mark.parametrize('role', ['builder', 'reviewer', 'mcp-solve'])
def test_observations_show_remaining_time_to_every_role(tmp_path, role):
    loop = Loop({'purpose': role, 'prompt': 'brief', 'tools': {'tools': [{'name': 'inspect', 'inputSchema': {}}]}},
                lambda *args: {'image': 'AAAA', 'mimeType': 'image/png'}, tmp_path, '')
    with patch.object(loop, 'remaining', return_value=123): loop.call(call('inspect'))
    observation = json.loads(loop.messages[-1]['content'][0]['text'])
    assert observation['remaining_seconds'] == 123
    assert ('remaining_turns' in observation) == (role == 'mcp-solve')
    if role != 'builder': assert 'delivery_if_time_expired' not in observation
    assert loop.messages[-1]['content'][-1]['type'] == 'image_url'


def test_builder_observations_track_started_snapshot_not_unsaved_workspace(tmp_path):
    state = tmp_path / 'last_started.json'
    first = {'sha256': 'started', 'archive': '/evidence/first.tar'}
    latest = {'sha256': 'finished', 'archive': '/evidence/final.tar'}
    def invoke(name, args):
        if name in ('try', 'finish'): state.write_text(json.dumps(first if name == 'try' else latest))
        if name == 'failed_try': raise RuntimeError('new workspace did not start')
        return {'image': 'AAAA', 'mimeType': 'image/png'}
    loop = Loop({'purpose': 'builder', 'prompt': 'brief', 'last_started_path': str(state),
                 'tools': {'tools': [{'name': n, 'inputSchema': {}} for n in ('inspect', 'try', 'failed_try', 'finish')]}},
                invoke, tmp_path, '')
    for name, expected in [('inspect', 'No MCP snapshot has started yet.'), ('try', first),
                           ('inspect', first), ('failed_try', first), ('finish', latest)]:
        loop.call(call(name))
        observation = json.loads(loop.messages[-1]['content'][0]['text'])
        assert 'remaining_seconds' in observation
        assert observation['delivery_if_time_expired'] == expected


def test_builder_prose_continuation_identifies_deliverable(tmp_path):
    state = tmp_path / 'last_started.json'
    snapshot = {'sha256': 'started', 'archive': '/evidence/started.tar'}
    state.write_text(json.dumps(snapshot))
    request = Mock(side_effect=[(200, reply()), (200, reply(call('finish')))])
    loop = Loop({'purpose': 'builder', 'prompt': 'brief', 'last_started_path': str(state),
                 'tools': {'tools': [{'name': 'finish', 'inputSchema': {}}]}}, Mock(return_value={}),
                tmp_path, '', request=request, context_window=1048576)
    assert loop.run()['terminal_reason'] == 'terminated'
    continued = request.call_args.args[0]['messages'][-1]['content']
    facts = json.loads(continued.removeprefix(CONTINUE + '\n'))
    assert facts['delivery_if_time_expired'] == snapshot and 'remaining_seconds' in facts


def test_all_payloads_use_upstream_reasoning_form():
    from kimi_loop import model_payload
    messages = [{'role': 'user', 'content': 'test'}]
    with patch.object(upstream, 'REASONING_EFFORT', PIN['reasoning_effort']):
        assert model_payload(messages)['reasoning'] == upstream.build_payload(messages)['reasoning']
    assert 'reasoning_effort' not in model_payload(messages)


def test_rejected_solver_request_is_the_mcp_outcome_not_infrastructure(tmp_path):
    loop = Loop({'purpose': 'mcp-solve', 'prompt': 'task', 'tools': {'tools': []}}, lambda *args: {}, tmp_path, '',
                request=Mock(side_effect=[(503, 'unavailable'), (400, 'invalid tool schema')]), context_window=1048576)
    with patch('time.sleep'): result = loop.run()
    assert result['terminal_reason'].startswith('request_rejected:') and result['status'] == 'fail'
    assert loop.request.call_count == 2


@pytest.mark.parametrize('role', ['builder', 'reviewer'])
@pytest.mark.parametrize('status', [400, 401, 403, 404, 422])
def test_permanent_exploratory_request_failure_stops_immediately(tmp_path, role, status):
    loop = Loop({'purpose': role, 'prompt': 'brief', 'tools': {'tools': []}}, Mock(), tmp_path, '',
                request=Mock(return_value=(status, 'request cannot be accepted')), context_window=1048576)
    with patch('time.sleep') as sleep: result = loop.run()
    assert result['status'] == 'infrastructure_failure' and str(status) in result['terminal_reason']
    assert 'request cannot be accepted' in result['terminal_reason']
    loop.request.assert_called_once(); loop.invoke.assert_not_called(); sleep.assert_not_called()
    assert len((tmp_path / 'transcript.jsonl').read_text().splitlines()) == 1


@pytest.mark.parametrize('status', [408, 429, 503])
def test_transient_exploratory_request_retries_with_upstream_sleep(tmp_path, status):
    loop = Loop({'purpose': 'builder', 'prompt': 'brief', 'tools': {'tools': [{'name': 'finish', 'inputSchema': {}}]}},
                lambda *args: {}, tmp_path, '', request=Mock(side_effect=[(status, 'try later'), (200, reply(call('finish')))]),
                context_window=1048576)
    with patch('time.sleep') as sleep: result = loop.run()
    assert result['terminal_reason'] == 'terminated'
    sleep.assert_called_once_with(upstream.RETRY_SLEEP)
    assert len(list(tmp_path.glob('request-*.json'))) == 1


def test_permanent_metadata_and_summary_failures_do_not_retry(tmp_path):
    import requests
    loop = Loop({'purpose': 'reviewer', 'prompt': 'brief', 'tools': {'tools': []}}, Mock(), tmp_path, '',
                request=Mock(return_value=(400, 'summary rejected')))
    response = requests.Response(); response.status_code = 403; response.url = 'https://endpoint/models'
    with patch('kimi_loop.requests.get', return_value=response) as get, patch('time.sleep') as sleep:
        result = loop.run()
        with pytest.raises(EndpointError, match='summary rejected'): loop.compact('diagnostic')
    assert result['status'] == 'infrastructure_failure' and '403' in result['terminal_reason']
    get.assert_called_once(); loop.request.assert_called_once(); sleep.assert_not_called()


def test_reviewer_endpoint_failure_is_not_returned_as_an_ordinary_tool_error(tmp_path):
    loop = Loop({'purpose': 'builder', 'prompt': 'brief', 'tools': {'tools': [{'name': 'review', 'inputSchema': {}}]}},
                Mock(side_effect=EndpointError('review HTTP 400; transcript saved')), tmp_path, '',
                request=Mock(return_value=(200, reply(call('review')))), context_window=1048576)
    result = loop.run()
    assert result['status'] == 'infrastructure_failure' and 'review HTTP 400' in result['terminal_reason']
    loop.request.assert_called_once()


@pytest.mark.parametrize('role', ['builder', 'reviewer', 'mcp-solve'])
@pytest.mark.parametrize('previous_error', [None, 'HTTP 503: earlier failure'])
def test_remaining_budget_timeout_is_time_budget(tmp_path, role, previous_error):
    clock = [0]
    def timeout(*args, **kwargs):
        clock[0] += kwargs['timeout']
        raise requests.ReadTimeout('request reached its deadline')
    with patch('kimi_loop.time.monotonic', side_effect=lambda: clock[0]):
        loop = Loop({'purpose': role, 'prompt': 'brief', 'seconds': upstream.HTTP_TIMEOUT,
                     'tools': {'tools': []}}, Mock(), tmp_path, '', context_window=1048576)
        loop.endpoint_error = previous_error
        with patch('kimi_loop.requests.post', side_effect=timeout) as post, patch('time.sleep') as sleep:
            result = loop.run()
    assert result['status'] == 'pass' and result['terminal_reason'] == 'time_budget'
    post.assert_called_once(); sleep.assert_not_called(); loop.invoke.assert_not_called()
    row = json.loads((tmp_path / 'transcript.jsonl').read_text())
    assert row['error'] == 'BudgetError: time_budget' and row['cause'] == 'request reached its deadline'


@pytest.mark.parametrize('purpose', ['metadata', 'summary'])
def test_metadata_and_summary_deadline_timeouts_preserve_errors(tmp_path, purpose):
    clock = [0]
    def timeout(*args, **kwargs):
        clock[0] += kwargs['timeout']
        raise requests.ReadTimeout(purpose + ' deadline')
    with patch('kimi_loop.time.monotonic', side_effect=lambda: clock[0]):
        loop = Loop({'purpose': 'reviewer', 'prompt': 'brief', 'seconds': upstream.HTTP_TIMEOUT,
                     'tools': {'tools': []}}, Mock(), tmp_path, '')
        with patch('kimi_loop.requests.get', side_effect=timeout) as get, \
             patch('kimi_loop.requests.post', side_effect=timeout) as post, patch('time.sleep') as sleep:
            if purpose == 'summary':
                with pytest.raises(BudgetError, match='time_budget'): loop.compact('test')
            result = loop.run()
    assert result['status'] == 'pass' and result['terminal_reason'] == 'time_budget'
    assert get.call_count + post.call_count == 1
    sleep.assert_not_called()
    path = next(tmp_path.glob('models-error-*.json')) if purpose == 'metadata' else tmp_path / 'transcript.jsonl'
    assert purpose + ' deadline' in path.read_text()


@pytest.mark.parametrize('failure', ['connection', 'http'])
def test_endpoint_failure_before_deadline_is_an_outage(tmp_path, failure):
    clock = [0]
    def sleep(seconds): clock[0] += seconds
    with patch('kimi_loop.time.monotonic', side_effect=lambda: clock[0]):
        loop = Loop({'purpose': 'builder', 'prompt': 'brief', 'seconds': upstream.RETRY_SLEEP,
                     'tools': {'tools': []}}, Mock(), tmp_path, '', context_window=1048576)
        response = Mock(status_code=503, text='unavailable')
        with patch('kimi_loop.requests.post', side_effect=requests.ConnectionError('offline')
                   if failure == 'connection' else None, return_value=response) as post, \
             patch('kimi_loop.time.sleep', side_effect=sleep):
            result = loop.run()
    assert result['status'] == 'infrastructure_failure'
    assert result['terminal_reason'].startswith('endpoint_outage: ')
    assert ('offline' if failure == 'connection' else '503') in result['terminal_reason']
    post.assert_called_once()


def test_solver_endpoint_errors_until_the_deadline_are_an_outage(tmp_path):
    loop = Loop({'purpose': 'mcp-solve', 'prompt': 'task', 'tools': {'tools': []}}, Mock(), tmp_path, '',
                context_window=1048576)
    clock = iter([100.0, 100.0, 100.0, 50.0, 50.0, 50.0] + [0.0] * 10)
    with patch.object(loop, 'remaining', side_effect=lambda: next(clock)), \
         patch('kimi_loop.requests.post', side_effect=requests.ConnectionError('endpoint down')), \
         patch('time.sleep'):
        result = loop.run()
    assert result['status'] == 'infrastructure_failure'
    assert result['terminal_reason'].startswith('endpoint_outage:')


def test_model_and_test_dependency_are_pinned_to_unchanged_upstream():
    import hashlib
    import importlib.metadata
    ROOT = Path(__file__).resolve().parents[1]
    pin = json.loads((ROOT / 'agent/kimi-source.json').read_text())
    assert hashlib.sha256((ROOT / 'agent/kimi_reference.py').read_bytes()).hexdigest() == pin['sha256']
    dependency = json.loads(importlib.metadata.distribution('cua-speedrun').read_text('direct_url.json'))
    assert dependency['vcs_info']['commit_id'] == pin['commit']


@pytest.mark.parametrize('role', ['builder', 'reviewer', 'mcp-solve'])
def test_large_tool_output_shares_one_head_tail_cap_and_preserves_images_and_full_output(tmp_path, role):
    text = 'HEAD-' + 'middle' * OUTPUT_LIMIT + '-TAIL'
    result = {'content': [{'type': 'text', 'text': text[:OUTPUT_LIMIT]},
                         {'type': 'image', 'mimeType': 'image/png', 'data': 'AAAA'},
                         {'type': 'text', 'text': text[OUTPUT_LIMIT:]}]}
    loop = Loop({'purpose': role, 'prompt': 'brief', 'tools': {'tools': [{'name': 'inspect', 'inputSchema': {}}]}},
                lambda *args: result, tmp_path, '')
    loop.call(call('inspect'))
    context = loop.messages[-2]['content']
    assert len(context) == OUTPUT_LIMIT and context.startswith('HEAD-') and '-TAIL' in context
    saved = next(tmp_path.glob('output-*.json'))
    assert (str(saved) not in context) if role == 'mcp-solve' else (str(saved) in context)
    assert json.loads(saved.read_text())['content'] == result['content']
    assert loop.messages[-1]['content'][-1]['image_url']['url'].endswith('AAAA')


def test_requests_reference_append_only_conversation_and_reconstruct_exact_payload(tmp_path):
    import hashlib
    requests = []
    loop = Loop({'purpose': 'builder', 'prompt': 'brief', 'tools': {'tools': []}}, lambda *args: {}, tmp_path, '',
                request=lambda payload: requests.append(payload) or (200, reply()), context_window=1048576)
    huge = {'role': 'user', 'content': 'large evidence ' * OUTPUT_LIMIT}
    loop.append(huge); original = loop.conversation.read_bytes()
    loop.request_model(loop.payload(loop.messages))
    loop.append({'role': 'user', 'content': 'next turn'})
    loop.request_model(loop.payload(loop.messages))
    assert loop.conversation.read_bytes().startswith(original)
    paths = sorted(tmp_path.glob('request-*.json'))
    reconstructed = []
    for path in paths:
        data = json.loads(path.read_text())
        payload = {**data['fields'], 'messages': archived_messages(path)}
        assert hashlib.sha256(loop.safe(payload, sort_keys=True).encode()).hexdigest() == path.stem.removeprefix('request-')
        assert 'large evidence' not in path.read_text()
        reconstructed.append(payload)
    assert all(request in reconstructed for request in requests)
    assert loop.conversation.read_text().count('large evidence') == OUTPUT_LIMIT


def test_unsuccessful_first_finish_returns_to_builder_before_second_finish(tmp_path):
    responses = [reply(call('finish')), reply(call('inspect')), reply(call('finish'))]
    finishes = []
    def invoke(name, args):
        if name != 'finish': return {'observed': True}
        finishes.append(name)
        return {'accepted': len(finishes) > 1, 'delivery_status': 'Nothing deliverable: startup failed'}
    request = Mock(side_effect=lambda payload: (200, responses.pop(0)))
    loop = Loop({'purpose': 'builder', 'prompt': 'brief', 'tools': {'tools': [
        {'name': name, 'inputSchema': {}} for name in ('finish', 'inspect')]}}, invoke, tmp_path, '',
        request=request, context_window=1048576)
    result = loop.run()
    assert result['terminal_reason'] == 'terminated' and result['steps'] == 3
    assert 'Nothing deliverable' in request.call_args_list[1].args[0]['messages'][-2]['content']


@pytest.mark.parametrize('result', [
    {'content': [{'type': 'text', 'text': 'native result'}]},
    {'content': [{'type': 'text', 'text': 'native error'}], 'isError': True},
])
def test_uncut_solver_result_contains_only_mcp_content_and_metadata(tmp_path, result):
    loop = Loop({'purpose': 'mcp-solve', 'prompt': 'task', 'tools': {'tools': []}}, Mock(), tmp_path, '')
    text = loop.content(result)[0]['text']
    expected = 'native result' if 'isError' not in result else 'native error\n' + json.dumps({'isError': True})
    assert text == expected
    assert 'full_output' not in text and 'remaining_seconds' not in text
    long = loop.content({'content': [{'type': 'text', 'text': 'x' * (OUTPUT_LIMIT + 1)}]})[0]['text']
    assert '[Output abbreviated; full output retained in evidence.]' in long
    assert 'read_file' not in long and str(tmp_path) not in long and len(long) == OUTPUT_LIMIT


@pytest.mark.parametrize('role', ['builder', 'reviewer', 'mcp-solve'])
@pytest.mark.parametrize('failure', ['connection', 'http'])
def test_any_endpoint_error_at_deadline_is_time_budget_with_error_evidence(tmp_path, role, failure):
    clock = [0]
    def request(payload):
        clock[0] = 1
        if failure == 'connection': raise requests.ConnectionError('connection closed at deadline')
        return 503, 'unavailable at deadline'
    with patch('kimi_loop.time.monotonic', side_effect=lambda: clock[0]):
        loop = Loop({'purpose': role, 'prompt': 'brief', 'seconds': 1, 'tools': {'tools': []}}, Mock(),
                    tmp_path, '', request=request, context_window=1048576)
        result = loop.run()
    assert result['terminal_reason'] == 'time_budget' and result['status'] == 'pass'
    assert 'at deadline' in (tmp_path / 'transcript.jsonl').read_text()


def test_summary_retry_records_context_references_without_embedded_history(tmp_path):
    import hashlib
    payloads = []
    def request(payload):
        payloads.append(payload)
        return (400, 'context_length_exceeded') if len(payloads) == 1 else (200, reply())
    loop = Loop({'purpose': 'builder', 'prompt': 'brief', 'tools': {'tools': []}}, Mock(), tmp_path, '',
                request=request, context_window=1048576)
    huge = 'unique-huge-message' * OUTPUT_LIMIT
    loop.append({'role': 'user', 'content': huge})
    loop.compact('test')
    assert loop.conversation.read_text().count('unique-huge-message') == OUTPUT_LIMIT
    for path in tmp_path.glob('request-*.json'):
        record = json.loads(path.read_text()); refs = record['summary_context']
        with Path(refs['conversation']).open('rb') as stream:
            source = []
            for ref in refs['messages']:
                stream.seek(ref['offset']); source.append(json.loads(stream.read(ref['length'])))
        messages = [{'role': 'system', 'content': record['instruction']}, {'role': 'user', 'content': loop.safe({
            'conversation': source, 'last_started': record['last_started'], 'full_context': record['full_context'],
            'full_conversation': record['full_conversation']})}]
        payload = {**record['fields'], 'messages': messages}
        assert payload in payloads
        assert hashlib.sha256(loop.safe(payload, sort_keys=True).encode()).hexdigest() == path.stem.removeprefix('request-')
        assert 'unique-huge-message' not in path.read_text()


@pytest.mark.parametrize('role', ['builder', 'reviewer'])
def test_many_screenshots_keep_every_request_below_observed_ingress_limit(tmp_path, role):
    import hashlib
    # Recorded endpoint failure: 38 inline screenshots crossed 64 MiB. These
    # fixture values are not runtime limits; retention comes only from upstream.
    observed_screenshots = 38
    body_limit = 64 * 1024 * 1024
    image_size = body_limit // observed_screenshots + 1
    hashes, request_bytes = [], []
    def invoke(name, args):
        if name == 'finish': return {}
        image = 'A' * image_size + str(len(hashes))
        url = 'data:image/png;base64,' + image
        hashes.append(hashlib.sha256(url.encode()).hexdigest())
        return {'image': image, 'mimeType': 'image/png'}
    def request(payload):
        size = len(json.dumps(payload, allow_nan=False).encode())
        request_bytes.append(size)
        assert size < body_limit
        inline = [b['image_url']['url'] for m in payload['messages'] if isinstance(m.get('content'),list)
                  for b in m['content'] if b['type'] == 'image_url']
        assert [hashlib.sha256(url.encode()).hexdigest() for url in inline] == hashes[-upstream.MAX_IMAGE_HISTORY_LENGTH:]
        return 200, reply(call('screenshot' if len(hashes) < observed_screenshots else 'finish'))
    loop = Loop({'purpose':role, 'prompt':'Explore and preserve evidence.', 'tools':{'tools':[
        {'name':name,'inputSchema':{}} for name in ('screenshot','finish')]}}, invoke, tmp_path, '',
        request=request, context_window=1048576)
    result = loop.run()
    assert result['terminal_reason'] == 'terminated' and result['compactions'] == []
    assert len(request_bytes) == observed_screenshots + 1
    assert image_size * observed_screenshots > body_limit
    saved = list(tmp_path.glob('screenshot-*.json'))
    assert len(saved) == observed_screenshots - upstream.MAX_IMAGE_HISTORY_LENGTH
    assert all(json.loads(path.read_text())['type'] == 'image_url' for path in saved)
    assert loop.conversation.read_bytes().count(b'data:image/png;base64,') == observed_screenshots


@pytest.mark.parametrize('role', ['builder', 'reviewer'])
def test_413_compacts_and_summary_413_shrinks_saved_context_then_continues(tmp_path, role):
    from http import HTTPStatus
    from kimi_loop import permanent_request_error
    rejected = HTTPStatus.REQUEST_ENTITY_TOO_LARGE
    replies = [(rejected, 'Payload Too Large'), (rejected, 'Payload Too Large'),
               (200, reply()), (200, reply(call('finish')))]
    payloads = []
    def request(payload):
        payloads.append(payload)
        return replies.pop(0)
    loop = Loop({'purpose':role, 'prompt':'brief', 'tools':{'tools':[{'name':'finish','inputSchema':{}}]}},
        lambda *args:{}, tmp_path, '', request=request, context_window=1048576)
    huge = 'old observation ' * OUTPUT_LIMIT
    loop.append({'role':'user','content':huge})
    loop.turn_start = len(loop.messages)
    image = {'type':'image_url','image_url':{'url':'data:image/png;base64,AAAA'}}
    loop.append({'role':'user','content':[image]})
    with patch('time.sleep') as sleep: result = loop.run()
    assert not permanent_request_error(rejected, 'Payload Too Large')
    assert result['status'] == 'pass' and result['terminal_reason'] == 'terminated'
    assert result['model_calls'] == 4 and len(result['compactions']) == 1
    assert len(json.dumps(payloads[2])) < len(json.dumps(payloads[1]))
    assert 'base64,' not in json.dumps(payloads[1:3])
    assert payloads[-1]['messages'][-1]['content'] == [image]
    assert huge in loop.conversation.read_text()
    rows = [json.loads(line) for line in (tmp_path/'transcript.jsonl').read_text().splitlines()]
    assert [row['http_status'] for row in rows] == [rejected,rejected,200,200]
    assert rows[0]['request'] != rows[1]['request'] != rows[2]['request']
    sleep.assert_not_called()


@pytest.mark.parametrize('role', ['builder', 'reviewer', 'mcp-solve'])
def test_every_role_shares_output_allowance_and_compaction(tmp_path, role):
    loop = Loop({'purpose': role, 'prompt': 'brief', 'tools': {'tools': []}},
                lambda *args: {}, tmp_path, '', context_window=MAX_TOKENS * 2)
    assert loop.payload(loop.messages)['max_tokens'] == MAX_TOKENS
    assert loop.payload(loop.messages, summary=True)['max_tokens'] == MAX_TOKENS
    loop.last_usage = {'prompt_tokens': MAX_TOKENS, 'completion_tokens': 1}
    with patch.object(loop, 'compact') as compact, patch.object(loop, 'request_model', return_value={}):
        loop.model()
    compact.assert_called_once_with('reported_usage')


def test_parallel_tool_results_precede_one_observation_with_all_images(tmp_path):
    requests = []
    replies = [reply(call('inspect'), {**call('inspect'), 'id': 'second'}), reply(call('finish'))]
    def request(payload):
        requests.append(payload)
        return 200, json.dumps(replies.pop(0))
    loop = Loop({'purpose': 'mcp-solve', 'prompt': 'task', 'terminate_tool': 'finish',
                 'tools': {'tools': [{'name': n, 'inputSchema': {}} for n in ('inspect', 'finish')]}},
                lambda name, args: {'image': 'AAAA', 'mimeType': 'image/png'} if name == 'inspect' else {'accepted': True},
                tmp_path, '', request=request, context_window=1048576)
    assert loop.run()['terminal_reason'] == 'terminated'
    turn = requests[1]['messages'][-4:]
    assert [m['role'] for m in turn] == ['assistant', 'tool', 'tool', 'user']
    assert [m['tool_call_id'] for m in turn[1:3]] == ['inspect', 'second']
    observation = turn[-1]['content']
    assert json.loads(observation[0]['text'])['remaining_turns'] is not None
    assert [block['type'] for block in observation] == ['text', 'image_url', 'image_url']


def test_gui_solver_has_the_same_turn_budget_as_the_mcp_solver(tmp_path):
    replies = [reply(call('screenshot')) for _ in range(5)]
    job = {'purpose': 'gui-solve', 'prompt': 'task', 'max_tools': 2, 'tools': {'tools': [
        {'name': n, 'inputSchema': {'type': 'object'}} for n in ('screenshot', 'terminate')]}, 'terminate_tool': 'terminate'}
    result = Loop(job, lambda name, args: {'executed': True}, tmp_path, '',
                  request=lambda payload: (200, replies.pop(0)), context_window=1048576).run()
    assert result['terminal_reason'] == 'step_budget' and result['steps'] == 2
