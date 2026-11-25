#!/usr/bin/env python3
"""Acquire and unpack the pinned Windows research target locally (configured in config/game.json)."""
import hashlib
import json
from pathlib import Path
import shutil
import struct
import subprocess
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
LOCAL = ROOT / ".local/windows"
GAME = json.loads((ROOT / "config/game.json").read_text())
TARGET = GAME["target"]
EXE = GAME["executable"]


def main():
    # Do not replace files that an active Wine process has mapped.
    prefix = str(LOCAL / "wine")
    for process in Path("/proc").iterdir():
        if not process.name.isdigit():
            continue
        try:
            command = (process / "cmdline").read_bytes().split(b"\0")[0]
            environment = (process / "environ").read_bytes().split(b"\0")
            if command.endswith(EXE.encode()) and f"WINEPREFIX={prefix}".encode() in environment:
                raise SystemExit("Close this project's Windows game before running setup")
        except (OSError, PermissionError):
            continue
    if not shutil.which("7z"):
        raise SystemExit("7z is required to unpack the target")
    downloads = LOCAL / "downloads"
    downloads.mkdir(parents=True, exist_ok=True)
    archive = downloads / TARGET["archive_name"]
    if not archive.exists():
        temporary = archive.with_suffix(".partial")
        with urllib.request.urlopen(TARGET["url"], timeout=60) as response, temporary.open("wb") as output:
            shutil.copyfileobj(response, output)
        temporary.rename(archive)
    if hashlib.sha1(archive.read_bytes()).hexdigest() != TARGET["archive_sha1"]:
        raise SystemExit("Archive digest mismatch; refusing to unpack")
    subprocess.run(["7z", "x", "-y", str(archive), f"-o{downloads / 'archive'}"], check=True, stdout=subprocess.DEVNULL)
    installer = downloads / "archive" / TARGET["installer_relpath"]
    content = installer.read_bytes()
    # This installer embeds one Microsoft cabinet after its MSI metadata.
    offset = int(str(TARGET["cabinet_offset"]), 0)
    if content[offset:offset + 4] != b"MSCF":
        raise SystemExit("Unexpected installer cabinet layout")
    size = struct.unpack_from("<I", content, offset + 8)[0]
    if size != int(TARGET["cabinet_size"]) or offset + size > len(content):
        raise SystemExit("Unexpected installer cabinet size")
    cabinet = downloads / "payload.cab"
    cabinet.write_bytes(content[offset:offset + size])
    game = LOCAL / "game"
    subprocess.run(["7z", "x", "-y", str(cabinet), f"-o{game}"], check=True, stdout=subprocess.DEVNULL)
    if hashlib.sha256((game / EXE).read_bytes()).hexdigest() != TARGET["game_sha256"]:
        raise SystemExit("Game digest mismatch; refusing to use this target")
    manifest = {
        "title": GAME["version_label"],
        "source": TARGET["url"],
        "source_kind": TARGET["source_kind"],
        "archive_sha1": TARGET["archive_sha1"],
        "installer_sha256": hashlib.sha256(content).hexdigest(),
        "game_sha256": TARGET["game_sha256"],
        "cabinet_offset": hex(offset),
        "files": [
            {"name": p.name, "bytes": p.stat().st_size, "sha256": hashlib.sha256(p.read_bytes()).hexdigest()}
            for p in sorted(game.iterdir()) if p.is_file()
        ],
    }
    renderer = subprocess.run(["bash", str(ROOT / "native/build-dxvk.sh")], check=True, capture_output=True, text=True)
    renderer_path = Path(renderer.stdout.strip().splitlines()[-1])
    shutil.copy2(renderer_path, game / "d3d9.dll")
    manifest["renderer"] = {
        "name": f"DXVK {GAME['dxvk']['tag']} with the Fruit Ninja singular-normal-matrix fix",
        "upstream_fix": GAME["dxvk"]["upstream_fix"],
        "sha256": hashlib.sha256(renderer_path.read_bytes()).hexdigest(),
        "patch_sha256": hashlib.sha256((ROOT / "native/dxvk-2.4-fruit-ninja.patch").read_bytes()).hexdigest(),
    }
    (LOCAL / "target.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(json.dumps({"game": str(game), "sha256": TARGET["game_sha256"]}))


if __name__ == "__main__":
    main()
