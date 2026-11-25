"""Driver choices that need no desktop: which images a run uses, and what a solve keeps.

The driver configures the process when it is imported, so each case runs in its own interpreter.
"""
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
PREPARE = '''
import json, sys
from pathlib import Path
sys.path[:0] = [%r, %r]
import driver
made = object.__new__(driver.Driver)
made.run = {'software_image': 'sha256:software', 'toolbox_image': 'sha256:toolbox', 'start_images': {'plate': 'sha256:start'},
            'ocr_images': {'software_image': 'sha256:software-ocr', 'toolbox_image': 'sha256:toolbox-ocr',
                           'start_images': {'plate': 'sha256:start-ocr'}}}
''' % (str(ROOT / 'agent'), str(ROOT / 'harness'))


def run(source, *arguments):
    row = subprocess.run([sys.executable, '-c', PREPARE + source, *map(str, arguments)], capture_output=True, text=True, env={'PATH': '/usr/bin:/bin'})
    assert row.returncode == 0, row.stderr[-3000:]
    return json.loads(row.stdout.splitlines()[-1])


def test_ocr_runs_use_the_ocr_images_and_other_runs_never_do():
    chosen = run('''
task = {'name': 'plate', 'path': 'tasks/plate'}
result = {}
for ocr in (False, True):
    made.ocr = ocr
    result[str(ocr)] = [made.builder_image('re').object_id, made.builder_image('no-re').object_id,
                        made.start_image(task, made.ocr).object_id, made.start_image(task).object_id]
print(json.dumps(result))
''')
    assert chosen['False'] == ['sha256:toolbox', 'sha256:software', 'sha256:start', 'sha256:start']
    # The last is what the GUI arm asks for: never an OCR image.
    assert chosen['True'] == ['sha256:toolbox-ocr', 'sha256:software-ocr', 'sha256:start-ocr', 'sha256:start']


def test_mcp_solve_keeps_the_desktop_before_the_solver_and_after_it_before_grading(tmp_path):
    seen = run('''
seen = []
class Desktop:
    folder, deadline, variant = Path(sys.argv[1]), None, 're'
    def capture_screenshot(self, path):
        seen.append(Path(path).name)
        if Path(path).name == 'after-solve.png': raise RuntimeError('display lost')
    def refused_inputs(self): return []
made.ocr, made.folder = True, Path(sys.argv[1])
made.run.update(tasks=['tasks/plate'], builder_profile='re')
made.task = lambda path: {'name': 'plate', 'path': path, 'deadline_seconds': 60, 'solve_turns': 1}
made.task_software = lambda task: {}
made.desktop = lambda image, name, **options: seen.append([image.object_id, name, options['variant']]) or Desktop()
made.start = lambda desktop: seen.append('start')
made.grade = lambda desktop, task: seen.append('grade') or {}
made.record_solve = lambda mode, task, result, graded: seen.append({k: result.get(k) for k in ('status', 'variant', 'ocr')})
made.close = lambda desktop: seen.append('close')
made.event = lambda event, **facts: seen.append([event, facts.get('name')])
made.solve_mcp()
print(json.dumps(seen))
''', tmp_path)
    assert seen == [['sha256:start-ocr', 'mcp-plate', 're'], 'start', 'before-solve.png', 'after-solve.png',
                    ['screenshot_error', 'after-solve.png'],  # recorded, and the solve is still graded
                    'grade', {'status': 'mcp_failure', 'variant': 're', 'ocr': True}, 'close']
