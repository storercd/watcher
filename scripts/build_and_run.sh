#!/usr/bin/env bash
#
# Builds Watcher.app (via build_native_app.sh) and launches it, for quick
# manual verification. Any running Watcher instance is quit first so the
# fresh build is the one that opens.
#
# Usage:
#   ./scripts/build_and_run.sh [destination-dir]   # defaults to ./dist
#
# Accepts the same environment variables as build_native_app.sh. Set
# WATCHER_DEV=1 to build a separate "Watcher Dev" app (bundle ID
# com.storercd.watcher.dev) so its permissions don't collide with the release
# app. Pair it with a stable WATCHER_SIGN_IDENTITY (e.g. an Apple Development
# cert from `security find-identity -v -p codesigning`) so grants persist.

set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "${script_dir}/.." && pwd)"
if [[ "${WATCHER_DEV:-0}" == "1" ]]; then
    export WATCHER_BUNDLE_ID="${WATCHER_BUNDLE_ID:-com.storercd.watcher.dev}"
    export WATCHER_APP_NAME="${WATCHER_APP_NAME:-Watcher Dev}"
fi
bundle_id="${WATCHER_BUNDLE_ID:-com.storercd.watcher}"
app_name="${WATCHER_APP_NAME:-Watcher}"
dest_arg="${1:-"${repo_root}/dist"}"

"${script_dir}/build_native_app.sh" "${dest_arg}"

app="$(cd "${dest_arg}" && pwd)/${app_name}.app"

if osascript -e "application id \"${bundle_id}\" is running" 2>/dev/null | grep -q true; then
    echo "==> Quitting running Watcher"
    osascript -e "tell application id \"${bundle_id}\" to quit" >/dev/null 2>&1 || true
    for _ in {1..20}; do
        osascript -e "application id \"${bundle_id}\" is running" 2>/dev/null | grep -q true || break
        sleep 0.25
    done
fi

echo "==> Launching ${app}"
open "${app}"
