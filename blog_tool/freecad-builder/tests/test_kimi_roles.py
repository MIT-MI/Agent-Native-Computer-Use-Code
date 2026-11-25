import sys,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'agent'))
from kimi_roles import configure

class RoleTests(unittest.TestCase):
    def test_frozen_tools_unchanged(self):
        tools=[{'name':'save','inputSchema':{'type':'object'}}]
        job={'tools':{'tools':tools}}
        configured=configure(job)
        self.assertEqual(configured['tools']['tools'][:-1],tools)
        self.assertEqual(configured['tools']['tools'][-1]['name'],'terminate')
    def test_lifecycle_collision(self):
        job=configure({'tools':{'tools':[{'name':'terminate'}]}})
        self.assertEqual(job['tools']['tools'][0]['name'],'terminate')
        self.assertEqual(job['terminate_tool'],'terminate_')
    def test_server_instructions_reach_system_prompt_verbatim(self):
        instructions='Save using the native application format.\nPaths are absolute.'
        configured=configure({'tools':{'tools':[]},'instructions':instructions})
        self.assertTrue(configured['system_prompt'].endswith(instructions))
        self.assertIn('Each tool call counts as one turn.',configured['system_prompt'])
