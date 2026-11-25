"""Run a command with durable output; waiting never terminates the command."""
import json
import math
from pathlib import Path
import subprocess
import sys


def execute(command, wait_seconds, log_path, output_limit):
    if isinstance(wait_seconds, bool) or not math.isfinite(wait_seconds) or wait_seconds < 0:
        raise ValueError('wait_seconds must be a finite, nonnegative number')
    path = Path(log_path)
    with path.open('wb') as log:
        child = subprocess.Popen(['/bin/bash', '--noprofile', '--norc', '-c', command],
            stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        try: child.wait(timeout=wait_seconds)
        except subprocess.TimeoutExpired: pass
        size = path.stat().st_size
        with path.open('rb') as stream:
            head = stream.read(min(size, output_limit if size <= output_limit else output_limit // 2))
            tail = b''
            if size > output_limit:
                stream.seek(size - (output_limit - len(head)))
                tail = stream.read(output_limit - len(head))
    return {'pid': child.pid, 'log_path': str(path), 'finished': child.returncode is not None,
            'returncode': child.returncode, 'head': head.decode(errors='replace'),
            'tail': tail.decode(errors='replace'), 'size': size}


if __name__ == '__main__':
    print(json.dumps(execute(**json.loads(sys.argv[1]))))
