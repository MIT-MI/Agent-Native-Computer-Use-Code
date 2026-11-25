"""Describe every entry without following links or excluding caches."""
import hashlib
import json
import os
from pathlib import Path
import stat
import sys

def scan(roots):
    result = {}
    pending = list(map(Path, roots))
    while pending:
        path = pending.pop()
        row = {}
        try:
            info = path.lstat()
            row.update(mode=info.st_mode, uid=info.st_uid, gid=info.st_gid,
                       size=info.st_size, mtime_ns=info.st_mtime_ns)
            if stat.S_ISDIR(info.st_mode):
                pending.extend(path.iterdir())
            elif stat.S_ISREG(info.st_mode):
                with path.open('rb') as stream:
                    digest = hashlib.sha256()
                    for chunk in iter(lambda: stream.read(os.statvfs(path).f_bsize), b''):
                        digest.update(chunk)
                    row['sha256'] = digest.hexdigest()
            elif stat.S_ISLNK(info.st_mode):
                row['target'] = os.readlink(path)
            else:
                row['special'] = True
        except OSError as exc:
            row['error'] = str(exc)
        result[str(path)] = row
    return result

if __name__ == '__main__':
    print(json.dumps(scan(sys.argv[1:])))
