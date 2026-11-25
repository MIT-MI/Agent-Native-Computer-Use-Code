"""Local driver: Docker images, desktop exec, model calls and durable receipts."""
import argparse
import copy
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import time
import tomllib
import traceback

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'agent'))
CONFIG = json.loads((ROOT / 'config.json').read_text())
# cli.py --model selects another pinned model from config.json "models".
if os.environ.get('FREECAD_BUILDER_MODEL'): CONFIG['model'] = CONFIG['models'][os.environ['FREECAD_BUILDER_MODEL']]
# Configure the unchanged upstream Kimi agent, whose constants the shared loop imports.
for field, name in [('model', 'KIMI_MODEL'), ('endpoint', 'KIMI_BASE_URL'), ('max_tokens', 'KIMI_MAX_TOKENS'),
                    ('temperature', 'KIMI_TEMPERATURE'), ('top_p', 'KIMI_TOP_P'), ('reasoning_effort', 'KIMI_REASONING_EFFORT')]:
    os.environ[name] = str(CONFIG['model'][field])
os.environ['KIMI_MAX_STEPS'] = str(CONFIG['budgets']['solve_turns'])

from builder import Builder, variant
import access_check
from container import Image, docker
from desktop import Desktop
from gui_allowlist import activate
from images import desktop_base_image, desktop_image, package_manifest, runtime_image, PACKAGE_MANIFEST, PACKAGE_QUERY
from mcp_transport import MCP, MCPProtocolError, deploy, startup_errors
from records import write_json, sha
from scorer import score
from solve import default_gui_solve, mcp_solve
from opencode_gui import opencode_gui_solve
import compatibility
import kimi_loop
kimi_loop.PIN.clear(); kimi_loop.PIN.update(CONFIG['model'])  # the loop reads config.json itself


def model_key():
    spec = CONFIG['keys'][CONFIG['model']['model']]
    path = Path(os.path.expanduser(spec['file']))
    return path.read_text().strip() if path.exists() else os.environ.get(spec['env'], '')


class Driver:
    def __init__(self, run):
        self.run = run
        self.folder = Path(run['evidence']); self.folder.mkdir(parents=True, exist_ok=True)
        self.software = json.loads((ROOT / run['software']).read_text())
        self.spec = json.loads((ROOT / self.software['environment']).read_text())
        self.desktops = []
        self.run.update(model={k: v for k, v in CONFIG['model'].items() if k not in ('key_env', 'key_file')},
                        budgets=CONFIG['budgets'], smoke_budgets=CONFIG['smoke_budgets'])
        self.run.setdefault('tasks', CONFIG['tasks']['paths'])
        self.receipt = {'run': run, 'events': [], 'checks': {}}
        # Offline OCR for the Builder and its MCP; the GUI arm and its start images never have it.
        self.ocr = bool(self.run.setdefault('ocr', False))
        self.key = model_key()
        self.save()

    def save(self):
        write_json(self.folder / 'receipt.json', self.receipt)
        write_json(self.folder / 'run-config.json', self.run)

    def event(self, event_name, **facts):
        row = {'event': event_name, 'time': time.time(), **facts}
        self.receipt['events'].append(row); self.save()
        print(json.dumps(row), flush=True)

    def desktop(self, image, name, *, software=None, gui=False, variant=None):
        started = []
        try:
            desktop = Desktop(image, software or self.software, self.folder / name, self.run['id'], gui=gui, variant=variant,
                              register=lambda d: (started.append(d), self.desktops.append(d)))
        except RuntimeError as exc:
            # An infrastructure failure is retried once, with the first attempt's evidence kept.
            if not str(exc).startswith(('Desktop readiness failed', 'desktop init did not start')): raise
            self.event('desktop_retry', name=name, error=str(exc)[:500])
            for failed in started: self.close(failed)
            (self.folder / name).rename(self.folder / (name + '-attempt-1'))
            desktop = Desktop(image, software or self.software, self.folder / name, self.run['id'], gui=gui, variant=variant,
                              register=self.desktops.append)
        self.record_image(image.object_id, desktop.require(desktop.command(PACKAGE_QUERY, user='root')).encode())
        return desktop

    def record_image(self, image_id, manifest=None):
        if manifest is None:
            manifest = docker('run', '--rm', '--network=none', '--entrypoint', '/bin/cat', image_id, PACKAGE_MANIFEST).encode() + b'\n'
        path = self.folder / 'images' / (image_id.replace(':', '-') + '.packages.tsv')
        path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(manifest)
        self.run.setdefault('image_manifests', {})[image_id] = {'path': str(path), 'sha256': sha(manifest)}
        self.save()

    def close(self, desktop):
        try: desktop.close()
        except Exception as exc: self.event('cleanup_error', container=desktop.container, error=str(exc))
        finally:
            if desktop in self.desktops: self.desktops.remove(desktop)

    def check_freeze(self):
        frozen = json.loads((ROOT / 'tasks/freeze.json').read_text())['files']
        for name, digest in frozen.items():
            if sha((ROOT / name).read_bytes()) != digest: raise RuntimeError('frozen task file changed: ' + name)
        pin = json.loads((ROOT / 'agent/kimi-source.json').read_text())
        if sha((ROOT / 'agent/kimi_reference.py').read_bytes()) != pin['sha256']: raise RuntimeError('upstream GUI agent bytes changed')
        self.event('freeze_valid', task_files=len(frozen), upstream_sha256=pin['sha256'])

    def snapshot(self, desktop, name):
        desktop.require(desktop.command(PACKAGE_QUERY + ' > ' + PACKAGE_MANIFEST, user='root'))
        started = time.monotonic()
        image_id = desktop.commit()
        self.run[name] = image_id
        self.record_image(image_id, desktop.require(desktop.command('cat ' + PACKAGE_MANIFEST, user='root')).encode())
        self.event('snapshot_observed', name=name, image_id=image_id, duration_seconds=time.monotonic() - started)
        return Image.from_id(image_id)

    def prepare_images(self):
        if self.run.get('software_image') and self.run.get('toolbox_image'):
            self.event('reusing_images', software_image=self.run['software_image'], toolbox_image=self.run['toolbox_image'])
            return
        self.event('provisioning_started')
        base = self.run.get('desktop_base_image')
        if not base:
            base = desktop_base_image().build().object_id
            self.run['desktop_base_image'] = base; self.record_image(base)
        image = desktop_image(Image.from_id(base), self.software).build()
        desktop = self.desktop(image, 'provision')
        try:
            versions = {}
            for package, expected in self.software['packages'].items():
                installed = desktop.require(desktop.command('dpkg-query -W -f=' + shlex.quote('${Version}') +
                                                            ' ' + shlex.quote(package), user='root')).strip()
                versions[package] = installed
                self.run['package_versions'] = versions; self.save()
                if expected is not None and installed != expected:
                    raise RuntimeError('Package version differs: ' + package + ' expected ' + expected + ', installed ' + installed)
            if self.spec['hooks'].get('post_start'):
                desktop.require(desktop.command('bash ' + shlex.quote(self.spec['hooks']['post_start']), user='root'))
            software_image = self.snapshot(desktop, 'software_image')
        finally: self.close(desktop)
        # Only this image receives Builder tools and the offline reference layer.
        builder = runtime_image(software_image, self.software, toolbox=True).build()
        self.run['toolbox_image'] = builder.object_id; self.record_image(builder.object_id)
        self.event('images_ready', software_image=self.run['software_image'], toolbox_image=builder.object_id)

    def image(self, name, ocr=False):
        """A built image; with OCR, its copy that also has the OCR packages."""
        return Image.from_id((self.run['ocr_images'] if ocr else self.run)[name])

    def ocr_images(self):
        """Copies of the software and toolbox images with offline OCR added, built once and then reused."""
        images = self.run.setdefault('ocr_images', {})
        if images.get('software_image') and images.get('toolbox_image'):
            self.event('reusing_ocr_images', software_image=images['software_image'], toolbox_image=images['toolbox_image'])
            return
        packages = lambda image_id: dict(line.split('\t') for line in Path(self.run['image_manifests'][image_id]['path']).read_text().split('\n') if line)
        tools = 'for tool in ' + shlex.join(CONFIG['isolation']['analysis_tools']) + '; do command -v "$tool"; done; true'
        present = lambda image_id: set(docker('run', '--rm', '--network=none', '--entrypoint', '/bin/sh', image_id, '-c', tools).split())
        for name in ('software_image', 'toolbox_image'):
            self.record_image(self.run[name])
            image = package_manifest(Image.from_id(self.run[name]).apt_install(*CONFIG['ocr']['packages'])).build()
            self.record_image(image.object_id)
            before, after = packages(self.run[name]), packages(image.object_id)
            changed = {package: [before[package], version] for package, version in after.items() if package in before and before[package] != version}
            images[name] = image.object_id
            images[name + '_changes'] = {'added': {package: version for package, version in after.items() if package not in before},
                                         'changed': changed, 'removed': sorted(set(before) - set(after)),
                                         'analysis_tools_added': sorted(present(image.object_id) - present(self.run[name]))}
            self.save()
            # The application must be the same build, and no analysis tool may arrive with OCR.
            if {package.split(':')[0] for package in changed} & set(self.software['packages']) or images[name + '_changes']['analysis_tools_added'] or images[name + '_changes']['removed']:
                raise RuntimeError('Adding OCR changed the application or brought an analysis tool: ' + json.dumps(images[name + '_changes'])[:2000])
        self.event('ocr_images_ready', software_image=images['software_image'], toolbox_image=images['toolbox_image'],
                   added=sorted(images['software_image_changes']['added']), changed=images['software_image_changes']['changed'])

    def builder_image(self, profile):
        """re: toolbox and offline references; no-re: the plain software desktop, closed at start."""
        return self.image('toolbox_image' if profile == 're' else 'software_image', self.ocr)

    def start(self, desktop):
        """Start the application; a no-re desktop then gets the GUI arm's executable policy for its user."""
        rows = desktop.restart()
        if desktop.variant == 'no-re': activate(desktop)
        return rows

    def analysis_tools(self, desktop):
        # The re Builder's own helper; like the toolbox, it is absent at solve time.
        desktop.install(Path(__file__).with_name('re_tools') / 'apptool.py', '/opt/re-tools/apptool')

    def task(self, path):
        folder = ROOT / path
        info = json.loads((folder / 'environment/task.json').read_text())
        toml = tomllib.loads((folder / 'task.toml').read_text())
        return {'path': path, 'name': folder.name, 'input': '/app/work/' + info['inputFile'],
                'deadline_seconds': toml['agent']['timeout_sec'], 'solve_turns': CONFIG['budgets']['solve_turns'],
                'description': (folder / 'instruction.md').read_text()}

    def task_software(self, task):
        # The application opens the task's input file, as the earlier GUI runs did.
        software = copy.deepcopy(self.software)
        for app in software['applications']: app['launch'] = app['launch'] + [task['input']]
        return software

    def start_image(self, task, ocr=False):
        images = (self.run['ocr_images'] if ocr else self.run).setdefault('start_images', {})
        if task['name'] not in images:
            user = self.spec['user_accounts'][0]['name']
            image = (self.image('software_image', ocr)
                     .add_local_dir(ROOT / task['path'] / 'environment/work', '/app/work')
                     .run_commands('chown -R ' + user + ':' + user + ' /app/work').build())
            images[task['name']] = image.object_id; self.save()
            self.event('start_image_ready', task=task['name'], image_id=image.object_id, ocr=ocr)
        return Image.from_id(images[task['name']])

    def grade(self, desktop, task):
        return score(desktop, task['path'], desktop.folder / 'score', CONFIG['tasks']['judge_image'])

    def toolbox_check(self):
        desktop = self.desktop(Image.from_id(self.run['toolbox_image']), 'toolbox-check')
        try:
            desktop.restart()
            checks = {}
            for name, command in json.loads((ROOT / 'toolbox.json').read_text())['offline_checks'].items():
                check = {'command': command, 'expected_returncode': 0} if isinstance(command, str) else command
                checks[name] = {**desktop.command(check['command']), 'expected_returncode': check['expected_returncode']}
            binary = self.software['applications'][0]['binary']
            command = 'mkdir -p /tmp/decompile-check && cp ' + shlex.quote(binary) + ' /tmp/decompile-check/program && '
            command += 'strip --strip-all /tmp/decompile-check/program && cd /tmp/decompile-check && kuna decompile-project ./program'
            checks['stripped_decompile'] = desktop.command(command)
            write_json(desktop.folder / 'toolbox-checks.json', checks)
            self.receipt['checks']['toolbox'] = {'path': str(desktop.folder / 'toolbox-checks.json'),
                'exit_codes': {k: v['returncode'] for k, v in checks.items()},
                'unexpected': sorted(k for k, v in checks.items() if v['returncode'] != v.get('expected_returncode', 0))}
            self.save(); self.event('toolbox_check_complete', unexpected=self.receipt['checks']['toolbox']['unexpected'])
        finally: self.close(desktop)

    def compatibility(self):
        profile = variant(self.run.get('builder_profile'))
        desktop = self.desktop(self.builder_image(profile), 'compatibility', variant=profile)
        try:
            self.start(desktop)
            if profile == 're': self.analysis_tools(desktop)
            result = compatibility.run(desktop, desktop.folder / 'builder', CONFIG['smoke_budgets'], profile, self.ocr)
            self.receipt['checks']['compatibility'] = result; self.save()
            self.event('compatibility_complete', summary=result['summary'])
        finally: self.close(desktop)

    def noop(self):
        """Score each untouched start state: the scorer's calibration, with no model."""
        for path in self.run['tasks']:
            task = self.task(path)
            desktop = self.desktop(self.start_image(task, self.ocr), 'noop-' + task['name'], software=self.task_software(task))
            try:
                desktop.restart()
                result = score(desktop, path, desktop.folder / 'score', CONFIG['tasks']['judge_image'])
                self.receipt['checks'].setdefault('noop', {})[task['name']] = {k: result.get(k) for k in ('passed', 'total', 'quality', 'exit_code')}
                self.save(); self.event('noop_scored', task=task['name'], passed=result.get('passed'), total=result.get('total'))
            finally: self.close(desktop)

    def allowlist(self):
        """cua-world's GUI policy check: the app starts, terminals and interpreters do not."""
        task = self.task(self.run['tasks'][0])
        desktop = self.desktop(self.start_image(task), 'allowlist', software=self.task_software(task), gui=True)
        try:
            launched = desktop.restart()
            policy = activate(desktop)
            reopened = desktop.restart()
            targets = [('/bin/sh', '-c', 'true'), ('/usr/bin/python3', '-c', 'pass'),
                       ('/usr/bin/gnome-terminal',), ('/usr/bin/gnome-text-editor',)]
            checks = {}
            for argv in targets:
                row = subprocess.run(['docker', 'exec', desktop.container, 'runuser', '-u', desktop.user, '--', *argv],
                                     capture_output=True, text=True)
                checks[shlex.join(argv)] = {'returncode': row.returncode, 'stderr': row.stderr[-500:]}
            result = {'policy': policy, 'commands': checks, 'allowed_application_launch': launched,
                      'allowed_application_restart': reopened}
            write_json(desktop.folder / 'allowlist.json', result)
            self.receipt['checks']['allowlist'] = {'path': str(desktop.folder / 'allowlist.json'),
                                                  'probe_returncodes': {k: v['returncode'] for k, v in checks.items()}}
            self.save()
            if any(not row['returncode'] for row in checks.values()):
                raise RuntimeError('GUI allowlist allowed a blocked executable probe')
            self.event('allowlist_complete', probes=self.receipt['checks']['allowlist']['probe_returncodes'])
        finally: self.close(desktop)

    def isolation_check(self):
        """As the no-re agent on a task desktop, every path to the application must be closed."""
        task = self.task(self.run['tasks'][0])
        desktop = self.desktop(self.start_image(task, self.ocr), 'isolation-check', software=self.task_software(task), variant='no-re')
        try:
            self.start(desktop)
            report = access_check.isolation(desktop, CONFIG['isolation'], files=[task['input']], ocr=self.ocr)
            report.update(task=task['name'], ocr=self.ocr)
            desktop.capture_screenshot(desktop.folder / 'application.png')
            write_json(desktop.folder / 'isolation-check.json', report)
            self.receipt['checks']['isolation'] = {'path': str(desktop.folder / 'isolation-check.json'), 'passed': report['passed'],
                                                  'unexpected': [row['probe'] for row in report['unexpected']]}
            self.save(); self.event('isolation_check_complete', passed=report['passed'], unexpected=self.receipt['checks']['isolation']['unexpected'])
            if not report['passed']: raise RuntimeError('The no-re desktop left a path to the application open, or a helper failed')
        finally: self.close(desktop)

    def analysis_check(self):
        """As the re identity, memory reads, debugger and instrumentation attach, and apptool must work."""
        task = self.task(self.run['tasks'][0])
        builder = self.desktop(self.image('toolbox_image', self.ocr), 'analysis-check', variant='re')
        solver = None
        try:
            self.start(builder); self.analysis_tools(builder)
            solver = self.desktop(self.start_image(task, self.ocr), 'analysis-check-task', software=self.task_software(task), variant='re')
            self.start(solver)
            report = access_check.analysis(builder, solver, CONFIG['isolation'], ocr=self.ocr)
            report.update(task=task['name'], ocr=self.ocr)
            write_json(solver.folder / 'analysis-check.json', report)
            self.receipt['checks']['analysis'] = {'path': str(solver.folder / 'analysis-check.json'), 'passed': report['passed'],
                                                 'unexpected': [row['probe'] for row in report['unexpected']]}
            self.save(); self.event('analysis_check_complete', passed=report['passed'], unexpected=self.receipt['checks']['analysis']['unexpected'])
            if not report['passed']: raise RuntimeError('The re identity could not analyse the application')
        finally:
            self.close(builder)
            if solver: self.close(solver)

    def shot(self, desktop, name):
        """Keep what the desktop shows at this moment of a solve; a failed capture is recorded, not fatal."""
        try: desktop.capture_screenshot(desktop.folder / name)
        except Exception as exc: self.event('screenshot_error', name=name, error=str(exc)[:500])

    def build(self):
        budgets = CONFIG['smoke_budgets' if self.run.get('smoke') else 'budgets']
        profile = variant(self.run.get('builder_profile'))
        self.event('builder_started', profile=profile, budgets=budgets, ocr=self.ocr)
        desktop = self.desktop(self.builder_image(profile), 'builder', variant=profile)
        try:
            self.start(desktop)
            if profile == 're': self.analysis_tools(desktop)
            builder = Builder(desktop, desktop.folder / 'builder', self.key, budgets, profile=profile, ocr=self.ocr)
            delivery = builder.run()
            if delivery['terminal_reason'].startswith('key_leak'): raise RuntimeError('Confirmed credential leak; run stopped')
            self.run['artifact'] = delivery['last_started']
            self.receipt['checks']['builder'] = {'report': str(builder.folder / 'delivery.json'), 'profile': profile, 'ocr': self.ocr,
                'terminal_reason': delivery['terminal_reason'], 'status': delivery.get('status'), 'last_started': delivery['last_started']}
            self.save(); self.event('builder_complete', terminal_reason=delivery['terminal_reason'], delivered=bool(delivery['last_started']))
        finally: self.close(desktop)

    def solve_mcp(self):
        # The solve desktop follows the delivered MCP's variant: the identity its Builder had.
        profile = variant(self.run.get('artifact_profile') or self.run.get('builder_profile'))
        for path in self.run['tasks']:
            task = self.task(path)
            desktop = self.desktop(self.start_image(task, self.ocr), 'mcp-' + task['name'], software=self.task_software(task), variant=profile)
            server = None
            try:
                self.start(desktop)
                self.shot(desktop, 'before-solve.png')
                desktop.deadline = time.monotonic() + task['deadline_seconds']
                try:
                    if not self.run.get('artifact'): raise RuntimeError('No MCP artifact was delivered')
                    snapshot = deploy(desktop, self.run['artifact'])
                    server = MCP(desktop, snapshot['command'], desktop.folder / 'mcp',
                                 lifetime_seconds=max(0, desktop.deadline - time.monotonic()))
                    errors = startup_errors(server.initialized, server.tools)
                    if errors: raise MCPProtocolError('; '.join(errors))
                except Exception as exc:
                    result = {'status': 'mcp_failure', 'error': str(exc), 'terminal_reason':
                              'mcp_protocol_error' if isinstance(exc, MCPProtocolError) else 'mcp_deployment_failed'}
                    write_json(desktop.folder / 'run.json', result)
                else:
                    result = mcp_solve(desktop, desktop.folder, server, task,
                                       {'solve_turns': task['solve_turns']}, self.key)
                if server:
                    closing, server = server, None
                    try: closing.close()
                    except Exception as exc: self.event('cleanup_error', error=str(exc))
                desktop.deadline = None
                self.shot(desktop, 'after-solve.png')
                result.update(variant=profile, ocr=self.ocr)
                if profile == 'no-re': result['blocked_code_inputs'] = len(desktop.refused_inputs())
                graded = self.grade(desktop, task)
                self.record_solve('mcp', task, result, graded)
            finally:
                if server:
                    try: server.close()
                    except Exception as exc: self.event('cleanup_error', error=str(exc))
                self.close(desktop)

    def solve_gui(self):
        for path in self.run['tasks']:
            task = self.task(path)
            desktop = self.desktop(self.start_image(task), 'gui-' + task['name'], software=self.task_software(task), gui=True)
            try:
                desktop.restart()
                activate(desktop)
                try:
                    budgets = {'solve_seconds': task['deadline_seconds'], 'solve_turns': task['solve_turns']}
                    if self.run.get('gui_agent') == 'opencode':
                        result = opencode_gui_solve(desktop, desktop.folder, task, CONFIG['model'], budgets, self.key)
                    else: result = default_gui_solve(desktop, desktop.folder, task, budgets, self.key)
                except Exception as exc:
                    result = {'status': 'infrastructure_failure', 'terminal_reason': 'runtime_error', 'errors': [str(exc)]}
                    write_json(desktop.folder / 'run.json', result)
                desktop.deadline = None
                graded = self.grade(desktop, task)
                self.record_solve('gui', task, result, graded)
            finally: self.close(desktop)

    def record_solve(self, mode, task, result, graded):
        row = {'status': result.get('status'), 'terminal_reason': result.get('terminal_reason'),
               'elapsed_seconds': result.get('elapsed_seconds'), 'steps': result.get('steps'),
               'tokens': result.get('tokens'), 'passed': graded.get('passed'), 'total': graded.get('total'),
               'quality': graded.get('quality'), 'judge_exit': graded.get('exit_code'),
               'report': str(self.folder / (mode + '-' + task['name']))}
        for field in ('blocked_code_inputs', 'variant', 'ocr'):
            if field in result: row[field] = result[field]
        row['limits'] = {'seconds': task['deadline_seconds'], 'turns': task['solve_turns']}
        self.receipt['checks'].setdefault(mode + '_solve', {})[task['name']] = row
        self.save(); self.event(mode + '_scored', task=task['name'], passed=row['passed'], total=row['total'],
                                terminal_reason=row['terminal_reason'])

    def execute(self):
        try:
            self.check_freeze()
            self.prepare_images()
            if self.ocr: self.ocr_images()
            checks = {'toolbox-check': self.toolbox_check, 'compatibility': self.compatibility, 'noop': self.noop, 'allowlist': self.allowlist,
                      'isolation-check': self.isolation_check, 'analysis-check': self.analysis_check,
                      'build': self.build, 'solve-mcp': self.solve_mcp, 'solve-gui': self.solve_gui}
            for name in self.run['checks']: checks[name]()
            self.receipt['status'] = 'complete'
        except Exception as exc:
            self.receipt.update(status='infrastructure_failure', error=str(exc), traceback=traceback.format_exc())
            self.event('run_stopped', error=str(exc))
        finally:
            for desktop in list(self.desktops): self.close(desktop)
            self.save()
            self.event('run_ended', status=self.receipt['status'])


def main():
    parser = argparse.ArgumentParser(); parser.add_argument('run_config'); args = parser.parse_args()
    Driver(json.loads(Path(args.run_config).read_text())).execute()


if __name__ == '__main__': main()
