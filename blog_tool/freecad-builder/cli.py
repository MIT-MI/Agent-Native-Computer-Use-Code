"""Start, read, wait for or stop a local driver run in tmux."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import uuid

ROOT = Path(__file__).resolve().parent
CONFIG = json.loads((ROOT / 'config.json').read_text())


def start(args):
    if args.model and args.model not in json.loads((ROOT / 'config.json').read_text())['models']:
        raise ValueError('Unknown model; see config.json "models"')
    session_id = args.id or uuid.uuid4().hex[:12]
    commands = []
    artifacts = args.artifact or []
    runs = len(artifacts) if len(artifacts) > 1 else args.repeat
    for index in range(runs):
        run_id = session_id if runs == 1 else session_id + '-' + str(index + 1)
        evidence = Path(args.evidence).resolve() / run_id
        run = {'id': run_id, 'software': args.software, 'evidence': str(evidence), 'checks': args.checks,
               'source_commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()}
        run.update(builder_profile=args.profile, smoke=args.smoke, ocr=args.ocr)
        if args.model: run['model_name'] = args.model
        if 'solve-gui' in args.checks: run['gui_agent'] = args.gui_agent
        if args.tasks: run['tasks'] = args.tasks
        if artifacts:
            source = artifacts[index if len(artifacts) > 1 else 0]
            previous = json.loads(Path(source).read_text())['run']
            if not previous.get('artifact'): raise ValueError('That run delivered no MCP: ' + source)
            run['artifact'] = previous['artifact']; run['artifact_from'] = str(Path(source).resolve())
            run['artifact_profile'] = previous.get('builder_profile')
            run['ocr'] = bool(previous.get('ocr'))  # an MCP is solved with what its Builder had
        if args.reuse_images:
            previous = json.loads(Path(args.reuse_images).read_text())['run']
            if previous['software'] != args.software: raise ValueError('Image receipt has a different software file')
            run.update({k: previous[k] for k in ('software_image', 'toolbox_image', 'desktop_base_image', 'package_versions', 'start_images', 'ocr_images') if k in previous})
        evidence.mkdir(parents=True, exist_ok=True)
        config = evidence / 'run-config.json'
        config.write_text(json.dumps(run, indent=2) + '\n')
        commands.append(f'FREECAD_BUILDER_MODEL={args.model or ""} PYTHONDONTWRITEBYTECODE=1 {sys.executable} -u {ROOT}/harness/driver.py {config} 2>&1 | tee {evidence}/driver.log')
    session = 'freecad-builder-' + session_id
    # Repeated runs share one session and run one after another.
    command = '; '.join(commands + [f'tmux wait-for -S {session}'])
    subprocess.run(['tmux', 'new-session', '-d', '-s', session, command], check=True)
    subprocess.run(['tmux', 'has-session', '-t', session], check=True)
    print(json.dumps({'session': session, 'runs': runs, 'last_receipt': str(evidence / 'receipt.json')}))


def status(args):
    receipt = json.loads(Path(args.receipt).read_text())
    print(json.dumps({'status': receipt.get('status', 'running'), 'checks': receipt['checks'],
                      'last_event': receipt['events'][-1] if receipt['events'] else None}, indent=2))


def wait(args):
    subprocess.run(['tmux', 'wait-for', 'freecad-builder-' + args.id], check=True)


def stop(args):
    ids = subprocess.check_output(['docker', 'ps', '-aq', '--filter', 'label=cua-world-run=' + args.id], text=True).split()
    if ids: subprocess.run(['docker', 'rm', '-f', *ids], check=True)
    subprocess.run(['tmux', 'kill-session', '-t', 'freecad-builder-' + args.id], check=False)
    print(json.dumps({'stopped_containers': ids}))


def main():
    parser = argparse.ArgumentParser(description=__doc__); sub = parser.add_subparsers(required=True)
    p = sub.add_parser('start'); p.set_defaults(fn=start)
    p.add_argument('--software', default='software/freecad.json'); p.add_argument('--evidence', required=True)
    p.add_argument('--checks', nargs='*', default=[],
                   choices=['toolbox-check', 'compatibility', 'noop', 'allowlist', 'isolation-check', 'analysis-check',
                            'build', 'solve-mcp', 'solve-gui'])
    p.add_argument('--profile', choices=['re', 'no-re'], default='re',
                   help='Builder variant: re (with analysis) or no-re (without, as a separate user)')
    p.add_argument('--tasks', nargs='*', help='task folders; default: all in config.json')
    p.add_argument('--smoke', action='store_true', help='smoke Builder budgets')
    p.add_argument('--ocr', action='store_true',
                   help='offline OCR for the Builder and its MCP, at build and solve time; with --artifact, as that MCP was built')
    p.add_argument('--artifact', nargs='+', help='receipts of runs whose delivered MCPs to solve with, one run each')
    p.add_argument('--model', help='a model from config.json "models"; default: "model"')
    p.add_argument('--gui-agent', choices=['default', 'opencode'], default='default',
                   help='GUI solve: the default GUI tools on our loop, or the earlier '
                        'FreeCAD rounds\' OpenCode agent with its screenshot desktop tools')
    p.add_argument('--repeat', type=int, default=1, help='independent runs, one after another in one tmux session')
    p.add_argument('--reuse-images'); p.add_argument('--id')
    p = sub.add_parser('status'); p.set_defaults(fn=status); p.add_argument('receipt')
    p = sub.add_parser('wait'); p.set_defaults(fn=wait); p.add_argument('id')
    p = sub.add_parser('stop'); p.set_defaults(fn=stop); p.add_argument('id')
    args = parser.parse_args(); args.fn(args)


if __name__ == '__main__': main()
