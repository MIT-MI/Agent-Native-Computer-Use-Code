import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'harness'), str(ROOT / 'agent')]
from opencode_gui import BUILTIN, TOOLS, config, inputs, pixel, shortcut


def test_the_earlier_rounds_desktop_tools_and_grid():
    assert [t['name'] for t in TOOLS] == ['screenshot', 'click', 'type_text', 'key_press', 'scroll', 'drag', 'wait_for_screen']
    assert [pixel(v, 1280) for v in (0, 500, 1000)] == [0, 640, 1279]
    assert [pixel(v, 800) for v in (0, 500, 1000)] == [0, 400, 799]


def test_tool_calls_become_structured_actions():
    assert inputs('click', {'x': 500, 'y': 500}, (1280, 800)) == ([{'mouse': {'left_click': [640, 400]}}], [])
    assert inputs('click', {'x': 0, 'y': 0, 'count': 2}, (1280, 800))[0] == [{'mouse': {'double_click': [0, 0]}}]
    actions, held = inputs('click', {'x': 0, 'y': 0, 'button': 'right', 'modifiers': ['ctrl']}, (1280, 800))
    assert actions == [{'keyboard': {'keys_down': ['ctrl']}}, {'mouse': {'right_click': [0, 0]}}] and held == ['ctrl']
    assert inputs('key_press', {'keys': 'ctrl+S'}, (1280, 800))[0] == [{'keyboard': {'keys': ['ctrl', 's']}}]
    assert shortcut('shift+End') == ['shift', 'End'] and shortcut('F12') == ['F12'] and shortcut('Enter') == ['Return']
    assert inputs('drag', {'from_x': 0, 'from_y': 0, 'to_x': 1000, 'to_y': 1000}, (1280, 800))[0] == [
        {'mouse': {'left_click_drag': [[0, 0], [1279, 799]]}}]
    assert inputs('screenshot', {}, (1280, 800)) == ([], [])


def test_opencode_keeps_only_the_desktop_tools():
    settings = config({'model': 'example/model', 'temperature': 1.0, 'top_p': 0.95, 'reasoning_effort': 'high'},
                      'http://127.0.0.1:1')
    assert settings['permission']['*'] == 'deny' and settings['permission']['desktop_*'] == 'allow'
    assert all(settings['tools'][name] is False for name in BUILTIN)
    assert settings['provider']['pinned']['options']['apiKey'] == 'held-by-the-driver'
    assert settings['mcp']['desktop']['command'][-2:] == ['mcp', 'http://127.0.0.1:1']
