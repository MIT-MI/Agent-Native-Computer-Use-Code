"""Solver roles: an MCP-native prompt with the server's own instructions, or the same prompt for GUI tools."""
SYSTEM_PROMPT = ('You complete a user task in a desktop application using only the MCP tools listed. '
    'Read the MCP server instructions below, if any. Inspect the current state before changing it, '
    'make the changes, then verify them with the tools before you finish. Tool arguments are JSON data. '
    'Each tool call counts as one turn. '
    'Call terminate with status success when the task is complete, or failure when it is impossible.')
TERMINATE = {'name': 'terminate', 'description': 'End the task: success when complete, failure when impossible.',
    'inputSchema': {'type': 'object', 'properties': {'status': {'type': 'string', 'enum': ['success', 'failure']},
                    'answer': {'type': 'string'}}, 'required': ['status'], 'additionalProperties': False}}


def configure(job):
    job = dict(job)
    terminal = 'terminate'
    names = {t['name'] for t in job['tools']['tools']}
    while terminal in names: terminal += '_'
    prompt = SYSTEM_PROMPT if job.get('purpose') != 'gui-solve' else SYSTEM_PROMPT.replace(
        'using only the MCP tools listed. Read the MCP server instructions below, if any.', 'using only the GUI tools listed.')
    prompt = prompt.replace('Call terminate', 'Call ' + terminal)
    if job.get('instructions'):
        prompt += '\n\nMCP server instructions:\n' + job['instructions']
    job.update(system_prompt=prompt, terminate_tool=terminal, prompt='Task:\n' + job.get('prompt', ''),
               tools={'tools': [*job['tools']['tools'], {**TERMINATE, 'name': terminal}]})
    return job
