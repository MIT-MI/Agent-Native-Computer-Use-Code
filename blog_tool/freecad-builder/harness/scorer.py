"""Frozen task judge on the persisted work folder; the score is kept before cleanup."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tomllib
from records import write_json

ROOT = Path(__file__).resolve().parents[1]
WORK = '/app/work'


def persist(desktop, folder):
    remote = '/opt/cua-world-runtime/work.tar'
    desktop.require(desktop.command('tar -cf ' + remote + ' -C ' + WORK + ' .', user='root', ignore_budget=True))
    desktop.copy_from(remote, folder / 'work.tar')
    work = folder / 'work'; work.mkdir(parents=True, exist_ok=True)
    with tarfile.open(folder / 'work.tar') as archive: archive.extractall(work, filter='data')
    return work


def judge(task, work, folder, image):
    """Run the task's unchanged judge offline, as the earlier FreeCAD benchmark did."""
    task = ROOT / task
    tests = folder / 'judge-tests'; shutil.copytree(task / 'tests', tests)
    logs = folder / 'judge-logs'; logs.mkdir(parents=True, exist_ok=True)
    timeout = tomllib.loads((task / 'task.toml').read_text())['verifier']['timeout_sec']
    argv = ['docker', 'run', '--rm', '--network=none', '--read-only', '--cap-drop=ALL', '--security-opt=no-new-privileges',
            '--user', f'{os.getuid()}:{os.getgid()}',
            '--cpus=2', '--memory=4g', '--memory-swap=4g', '--pids-limit=512',
            '--tmpfs=/tmp:rw,nosuid,nodev,size=512m,mode=1777',
            '--mount', f'type=bind,src={work},dst={WORK},readonly',
            '--mount', f'type=bind,src={task / "environment/context.json"},dst=/app/context.json,readonly',
            '--mount', f'type=bind,src={tests},dst=/tests', '--mount', f'type=bind,src={logs},dst=/logs/verifier',
            '-e', 'REEF_WORKSPACE=' + WORK, '-e', 'HOME=/tmp', '--entrypoint=/bin/bash', image, '/tests/test.sh']
    try:
        row = subprocess.run(argv, capture_output=True, text=True, timeout=timeout + 10)
        code, output = row.returncode, (row.stdout + row.stderr)[-20000:]
    except subprocess.TimeoutExpired:
        code, output = 'timeout', ''
    path = logs / 'criteria.json'
    result = json.loads(path.read_text()) if path.exists() else {'infrastructure_error': 'Judge produced no criteria.json'}
    result.update(exit_code=code, judge_output=output)
    write_json(folder / 'score.json', result)
    return result


def score(desktop, task, folder, image):
    folder.mkdir(parents=True, exist_ok=True)
    return judge(task, persist(desktop, folder), folder, image)

