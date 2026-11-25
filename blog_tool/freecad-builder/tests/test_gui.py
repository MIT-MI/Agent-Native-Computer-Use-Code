import sys,unittest
from pathlib import Path
BASE=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(BASE/'agent'),str(BASE/'harness')]
from kimi_reference import translate_pyautogui,project_coordinate_to_absolute_scale
from gui import action_commands, GUI
from unittest.mock import Mock
import pytest

class GUITests(unittest.TestCase):
    def test_full_actions(self):
        code="""pyautogui.moveTo(.5,.5)
pyautogui.click(100,100)
pyautogui.doubleClick(100,100)
pyautogui.rightClick(100,100)
pyautogui.middleClick(100,100)
pyautogui.dragTo(200,200)
pyautogui.mouseDown()
pyautogui.mouseUp()
pyautogui.scroll(2)
pyautogui.hscroll(2)
pyautogui.write('ordinary text')
pyautogui.hotkey('ctrl','s')
pyautogui.keyDown('shift')
pyautogui.keyUp('shift')
time.sleep(1)"""
        code=project_coordinate_to_absolute_scale(code,1920,1080)
        actions=translate_pyautogui(code,1920,1080,[])
        self.assertEqual(actions[0],{'mouse':{'move':[960,540]}})
        for action in actions:self.assertTrue(action_commands(action,(1920,1080)))
    def test_no_model_execution(self):
        with self.assertRaises(ValueError):translate_pyautogui("__import__('os').system('bad')",1920,1080,[])
        with self.assertRaises(ValueError):action_commands({'mouse':{'left_click':['1;bad',0]}},(1920,1080))

if __name__=='__main__':unittest.main()

def test_gui_translates_without_text_shortcut_or_order_guards(tmp_path):
    runner = Mock(resolution=(1920, 1080))
    gui = GUI(runner, tmp_path)
    gui.type_text('import os; arbitrary text')
    gui.key(['CTRL', 'ALT', 't'])
    assert runner.inject_action.call_count == 2
    assert runner.inject_action.call_args.args[0] == {'keyboard': {'keys': ['ctrl', 'alt', 't']}}


@pytest.mark.parametrize('amount', [0, .5, -.5])
def test_upstream_zero_scroll_is_a_noop_without_dropping_adjacent_actions(amount):
    from desktop import Desktop
    actions = translate_pyautogui(f'pyautogui.scroll({amount})\npyautogui.click(100,100)', 1920, 1080, [])
    desktop = object.__new__(Desktop); desktop.resolution = (1920, 1080); desktop.deadline = None
    desktop.ensure_display = Mock(); desktop.command = Mock(); desktop.require = Mock()
    desktop.inject_actions(actions)
    desktop.command.assert_called_once_with('xdotool mousemove --sync 100 100 click 1', user='root', timeout=None)
    desktop.ensure_display.assert_called_once()
    assert action_commands({'mouse': {'scroll': ['down', 0]}}, desktop.resolution) == []

