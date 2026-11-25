"""MCP and default GUI solvers on the shared loop."""
from pathlib import Path
import time
from gui import GUI
from records import write_json
from code_guard import REJECTED, looks_like_code


def default_gui_solve(desktop, folder, task, budgets, key):
    """The shared loop with the Builder's GUI tools and nothing model-specific."""
    from kimi_loop import Loop
    from kimi_roles import configure
    from builder import GUI_TOOLS
    folder = Path(folder); gui = GUI(desktop, folder); blocked = []
    desktop.deadline = time.monotonic() + budgets['solve_seconds']
    job = configure({'purpose': 'gui-solve', 'prompt': task['description'], 'tools': {'tools': GUI_TOOLS},
                     'seconds': budgets['solve_seconds'], 'max_tools': budgets['solve_turns']})
    def invoke(name, arguments):
        if name == job['terminate_tool']: return {'accepted': True, **arguments}
        if name == 'type_text' and looks_like_code(arguments.get('text')):
            blocked.append(arguments); write_json(folder / 'blocked-code-inputs.json', blocked)
            return {'executed': False, 'error': REJECTED}
        return gui.tools()[name](**arguments)
    result = Loop(job, invoke, folder, key).run()
    result['blocked_code_inputs'] = len(blocked)
    return result


def mcp_solve(desktop, folder, server, task, budgets, key):
    from kimi_loop import Loop
    from kimi_roles import configure
    folder = Path(folder)
    write_json(folder / 'delivered-tool-list.json', server.tools)
    job = configure({'purpose': 'mcp-solve', 'prompt': task['description'], 'tools': server.tools,
                     'instructions': server.initialized.get('instructions', ''),
                     'seconds': max(0, desktop.deadline - time.monotonic()), 'max_tools': budgets['solve_turns']})
    def invoke(name, arguments):
        if name == job['terminate_tool']: return {'accepted': True, **arguments}
        return server.call('tools/call', {'name': name, 'arguments': arguments})
    return Loop(job, invoke, folder, key).run()
