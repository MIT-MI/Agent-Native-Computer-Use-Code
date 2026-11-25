#!/usr/bin/env bash
# Altered DXVK 2.4, zlib/libpng license: see DXVK-LICENSE.txt.
# Backports the singular-normal-matrix fix from upstream PR #5765 only.
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
BUILD_ROOT="$ROOT_DIR/.local/windows/toolchain/dxvk-build"
SOURCE_DIR="$BUILD_ROOT/source"
PATCH_FILE="$ROOT_DIR/native/dxvk-2.4-fruit-ninja.patch"

read -r DXVK_TAG BASE_COMMIT DXVK_REPO GLSLANG_URL GLSLANG_NAME GLSLANG_SHA256 < <(
  python3 -c 'import json, sys; d = json.load(open(sys.argv[1]))["dxvk"]; print(d["tag"], d["base_commit"], d["repo"], d["glslang_deb_url"], d["glslang_deb_name"], d["glslang_deb_sha256"])' \
    "$ROOT_DIR/config/game.json"
)

mkdir -p "$BUILD_ROOT"
for executable in git meson ninja i686-w64-mingw32-g++ curl dpkg-deb; do
  command -v "$executable" >/dev/null || { echo "Required tool missing: $executable" >&2; exit 1; }
done
if [ ! -d "$SOURCE_DIR/.git" ]; then
  git clone --depth 1 --branch "$DXVK_TAG" "$DXVK_REPO" "$SOURCE_DIR" >&2
fi
test "$(git -C "$SOURCE_DIR" rev-parse HEAD)" = "$BASE_COMMIT"
git -C "$SOURCE_DIR" submodule update --init --depth 1 >&2
if git -C "$SOURCE_DIR" apply --check "$PATCH_FILE" 2>/dev/null; then
  git -C "$SOURCE_DIR" apply "$PATCH_FILE"
else
  git -C "$SOURCE_DIR" apply --reverse --check "$PATCH_FILE"
fi
# Pin shader tools locally instead of changing system packages.
GLSLANG_DEB="$BUILD_ROOT/$GLSLANG_NAME"
if [ ! -f "$GLSLANG_DEB" ]; then
  curl --fail --location --max-time 120 --silent --show-error \
    "$GLSLANG_URL" -o "$GLSLANG_DEB"
fi
echo "$GLSLANG_SHA256  $GLSLANG_DEB" | sha256sum --check >&2
dpkg-deb -x "$GLSLANG_DEB" "$BUILD_ROOT/tools"
export PATH="$BUILD_ROOT/tools/usr/bin:$PATH"
if [ ! -f "$BUILD_ROOT/build32/build.ninja" ]; then
  meson setup "$BUILD_ROOT/build32" "$SOURCE_DIR" \
    --cross-file "$SOURCE_DIR/build-win32.txt" --buildtype release \
    -Denable_dxgi=false -Denable_d3d8=false -Denable_d3d10=false -Denable_d3d11=false >&2
fi
ninja -C "$BUILD_ROOT/build32" -j4 src/d3d9/d3d9.dll >&2
printf '%s\n' "$BUILD_ROOT/build32/src/d3d9/d3d9.dll"
