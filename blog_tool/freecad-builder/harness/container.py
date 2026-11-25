"""Docker equivalents of the Modal image and exec calls this harness uses."""
import codecs
import fnmatch
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

CHUNK = 1 << 16


class Input:
    """Writable stdin with the write/drain/write_eof methods the transports call."""
    def __init__(self, pipe): self.pipe = pipe
    def write(self, data): self.pipe.write(data.encode() if isinstance(data, str) else data)
    def drain(self):
        if not self.pipe.closed: self.pipe.flush()
    def write_eof(self): self.pipe.close()


class Output:
    """Readable stream that yields chunks as they arrive and reads to EOF."""
    def __init__(self, pipe, text):
        self.pipe, self.text = pipe, text
        self.decoder = codecs.getincrementaldecoder('utf-8')('replace') if text else None

    def __iter__(self):
        while chunk := os.read(self.pipe.fileno(), CHUNK):
            data = self.decoder.decode(chunk) if self.text else chunk
            if data: yield data
        if self.text and (rest := self.decoder.decode(b'', final=True)): yield rest

    def read(self):
        return ('' if self.text else b'').join(self)


class Process:
    def __init__(self, argv, *, text=True):
        self.argv = argv
        self.child = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                      stderr=subprocess.PIPE, bufsize=0)
        self.stdin = Input(self.child.stdin)
        self.stdout, self.stderr = Output(self.child.stdout, text), Output(self.child.stderr, text)

    def wait(self): return self.child.wait()
    def poll(self): return self.child.poll()


def docker(*args, check=True):
    row = subprocess.run(['docker', *args], capture_output=True, text=True)
    if check and row.returncode: raise RuntimeError('docker ' + args[0] + ': ' + (row.stderr or row.stdout).strip())
    return row.stdout.strip()


class Image:
    """A Dockerfile chain with the subset of Modal's Image API used by images.py."""
    def __init__(self, base, lines=(), files=()):
        self.base, self.lines, self.files = base, list(lines), list(files)
        self.object_id = base if not self.lines and base.startswith('sha256:') else None

    @classmethod
    def from_registry(cls, reference): return cls(reference)

    @classmethod
    def from_id(cls, image_id): return cls(image_id)

    def _with(self, lines=(), files=()):
        return Image(self.base, self.lines + list(lines), self.files + list(files))

    def env(self, values):
        return self._with(['ENV ' + ' '.join(k + '=' + _quote(v) for k, v in values.items())])

    def run_commands(self, *commands):
        return self._with(['RUN ' + command for command in commands])

    def apt_install(self, *packages):
        return self.run_commands('apt-get update && apt-get install -y ' + ' '.join(packages) +
                                 ' && rm -rf /var/lib/apt/lists/*')

    def add_local_dir(self, source, target, copy=True, ignore=()):
        name = 'context-' + str(len(self.files))
        return self._with(['COPY ' + name + ' ' + str(target)], [(name, Path(source), tuple(ignore))])

    def add_local_file(self, source, target, copy=True):
        name = 'context-' + str(len(self.files))
        return self._with(['COPY ' + name + ' ' + str(target)], [(name, Path(source), ())])

    def dockerfile(self):
        base = self.base
        if base.startswith('sha256:'):
            # BuildKit resolves names, not bare IDs; a local tag names the exact ID.
            base = 'freecad-builder-base:' + base.split(':', 1)[1][:24]
            docker('tag', self.base, base)
        return '\n'.join(['FROM ' + base, *self.lines]) + '\n'

    def build(self, *, tag=None):
        if self.object_id: return self
        with tempfile.TemporaryDirectory() as context:
            for name, source, ignore in self.files:
                target = Path(context) / name
                if source.is_dir():
                    patterns = [p.split('/')[-1] for p in ignore]
                    shutil.copytree(source, target, symlinks=True,
                                    ignore=lambda d, names: [n for n in names if any(fnmatch.fnmatch(n, p) for p in patterns)])
                else: shutil.copy2(source, target)
            (Path(context) / 'Dockerfile').write_text(self.dockerfile())
            argv = ['docker', 'build', '-q'] + (['-t', tag] if tag else []) + [context]
            row = subprocess.run(argv, capture_output=True, text=True)
            if row.returncode: raise RuntimeError('docker build failed:\n' + row.stderr[-20000:])
        return Image(row.stdout.strip().splitlines()[-1])


def _quote(value):
    value = str(value)
    return value if value and all(c.isalnum() or c in '._-/:' for c in value) else '"' + value.replace('\\', '\\\\').replace('"', '\\"') + '"'
