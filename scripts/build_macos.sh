#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
if [[ "$(uname -s)" != "Darwin" || "$(uname -m)" != "arm64" ]]; then
    printf '%s\n' '此脚本需要 Apple Silicon Mac。' >&2
    exit 1
fi

python=".venv/bin/python"
if [[ ! -x "$python" ]]; then
    printf '%s\n' '请先用 uv 创建 CPython 3.14 arm64 虚拟环境 .venv。' >&2
    exit 1
fi
"$python" -c 'import platform, sys, tkinter; assert sys.version_info[:2] == (3, 14) and platform.machine() == "arm64"; print("Python", sys.version.split()[0], "Tcl", tkinter.Tcl().eval("info patchlevel"))'
uv pip install --python "$python" --require-hashes -r requirements-build.txt

dist="dist/macos-arm64"
app="$dist/ToppsAutoBot.app"
executable="$app/Contents/MacOS/ToppsAutoBot"
env -u TOPPS_BUILD_PUBLISHER_RESOURCE "$python" -m PyInstaller --clean --noconfirm \
    --distpath "$dist" --workpath build/macos-arm64 packaging/topps_auto_bot.spec
codesign --verify --deep --strict "$app"
"$executable" --check-runtime
version=$("$python" -c 'import tomllib; print(tomllib.load(open("pyproject.toml", "rb"))["project"]["version"])')
if [[ "$("$executable" --version)" != "$version" ]]; then
    printf '%s\n' '应用版本与 pyproject.toml 不一致。' >&2
    exit 1
fi
"$python" scripts/check_release_hygiene.py

archive="ToppsAutoBot-$version-macos-arm64.zip"
staging=$(mktemp -d "$dist/.archive.XXXXXX")
trap 'rm -rf -- "$staging"' EXIT
ditto -c -k --keepParent "$app" "$staging/$archive"
(
    cd "$staging"
    LC_ALL=C shasum -a 256 "$archive" > "$archive.sha256"
)
mv "$staging/$archive" "$staging/$archive.sha256" "$dist/"
printf '%s\n' "$app" "$dist/$archive" "$dist/$archive.sha256"
