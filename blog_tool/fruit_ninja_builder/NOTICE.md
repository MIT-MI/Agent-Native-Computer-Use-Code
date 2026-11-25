# Notice and redistribution status

**Origin.** This standalone repository was extracted from `fruit-ninja-mcp` (branch
`feat/windows-builder`, commit `bfd4ca4`). It carries no license grant for third-party code or
game assets.

**Game.** `native/setup.py` downloads a hash-pinned third-party historical archive of Fruit Ninja HD
1.6.1 for Windows (parameters in [config/game.json](config/game.json)). The executable is Halfbrick
game software; archive provenance and hashes do not establish publisher authenticity. Binaries,
assets, Wine state, decompiled output and recordings are not vendored. Keep videos local and do not
publish them without permission.

**DXVK.** The DXVK compatibility backport is an altered upstream source patch under
[the zlib/libpng license](native/DXVK-LICENSE.txt).

**Dependencies.** npm packages are pinned by `package-lock.json` and
`sandbox/toolchain/package-lock.json`; base images and toolbox archives are pinned by digest or hash.
