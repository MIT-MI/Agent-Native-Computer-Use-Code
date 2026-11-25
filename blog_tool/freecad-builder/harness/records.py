"""Durable evidence."""
import hashlib
import json
from pathlib import Path
import threading

_WRITE_LOCK = threading.RLock()

def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _WRITE_LOCK:
        temporary = path.with_suffix('.tmp')
        temporary.write_text(json.dumps(value, indent=2, sort_keys=True, default=str) + '\n')
        temporary.replace(path)


def sha(data):
    return hashlib.sha256(data).hexdigest()

