#!/usr/bin/env bash
#
# Builds Watcher.app (via build_native_app.sh) and launches it, for quick
# manual verification. Any running Watcher instance is quit first so the
# fresh build is the one that opens.
#
# Usage:
#   ./scripts/build_and_run.sh [destination-dir]   # defaults to ./dist
#
# Accepts the same environment variables as build_native_app.sh.

set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "${script_dir}/.." && pwd)"
dest_arg="${1:-"${repo_root}/dist"}"

"${script_dir}/build_native_app.sh" "${dest_arg}"

app="$(cd "${dest_arg}" && pwd)/Watcher.app"

if pgrep -x Watcher >/dev/null 2>&1; then
    echo "==> Quitting running Watcher"
    osascript -e 'tell application id "com.storercd.watcher" to quit' >/dev/null 2>&1 || true
    for _ in {1..20}; do
        pgrep -x Watcher >/dev/null 2>&1 || break
        sleep 0.25
    done
fi

echo "==> Launching ${app}"
open "${app}"
