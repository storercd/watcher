#!/usr/bin/env bash
#
# Builds Watcher.app, a thin macOS app bundle that launches the existing
# Python/tkinter Watcher program. No compilation or freezing happens here:
# the bundle just gives Watcher its own Dock icon, name, and app identity
# (instead of the generic Python "rocket") so it can be pinned to the Dock
# or kept in /Applications, while the app itself keeps running as plain
# Python from this repo checkout.
#
# Usage:
#   ./scripts/build_macos_app.sh [destination-dir]
#
# destination-dir defaults to ./dist (git-ignored). Common choices:
#   ./scripts/build_macos_app.sh ~/Applications
#   ./scripts/build_macos_app.sh /Applications

set -euo pipefail

if [[ "$(uname -s)" != "Darwin" ]]; then
    echo "error: this script only builds a macOS .app bundle (requires Darwin)." >&2
    exit 1
fi

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "${script_dir}/.." && pwd)"
packaging_dir="${repo_root}/packaging/macos"

dest_arg="${1:-"${repo_root}/dist"}"
mkdir -p "${dest_arg}"
dest_dir="$(cd "${dest_arg}" && pwd)"

app_path="${dest_dir}/Watcher.app"
contents_dir="${app_path}/Contents"
macos_dir="${contents_dir}/MacOS"
resources_dir="${contents_dir}/Resources"

# Prefer a project-local virtualenv's interpreter if one exists, since that's
# where pyobjc-framework-Cocoa (needed for the bouncing Dock notification)
# would be installed. Fall back to python3 on PATH otherwise.
python_bin="python3"
for venv_candidate in "${repo_root}/.venv/bin/python3" "${repo_root}/venv/bin/python3"; do
    if [[ -x "${venv_candidate}" ]]; then
        python_bin="${venv_candidate}"
        break
    fi
done

rm -rf "${app_path}"
mkdir -p "${macos_dir}" "${resources_dir}"

# Kill any already-running instance of this checkout's main.py first. The
# bundle gets overwritten at the same path every build, but a running
# process keeps using its already-open (now-unlinked) executable text/
# launcher script -- rebuilding does NOT replace what a still-running
# process is executing. Reopening the Dock icon (or double-clicking the
# app again) while an old instance is alive just reactivates that stale,
# already-running window instead of starting a fresh one with the new
# code, which looks exactly like "the rebuild didn't change anything".
running_pids="$(pgrep -f "${repo_root}/main.py" || true)"
if [[ -n "${running_pids}" ]]; then
    echo "Stopping existing Watcher process(es): ${running_pids//$'\n'/, }"
    while IFS= read -r pid; do
        [[ -n "${pid}" ]] && kill "${pid}" 2>/dev/null || true
    done <<< "${running_pids}"
    sleep 1
fi

cp "${packaging_dir}/Info.plist.template" "${contents_dir}/Info.plist"
cp "${packaging_dir}/Watcher.icns" "${resources_dir}/Watcher.icns"

# If WATCHER_NATIVE_CHROME is set in the environment this script itself is
# run in, bake it into the launcher so double-clicking/opening the built
# .app (which doesn't inherit a Terminal's env) also gets it -- useful both
# to diagnose the overrideredirect repaint bug via the actual app bundle,
# and as a permanent workaround if that's what it takes.
native_chrome_export=""
if [[ -n "${WATCHER_NATIVE_CHROME:-}" ]]; then
    native_chrome_export="export WATCHER_NATIVE_CHROME=${WATCHER_NATIVE_CHROME}"
    echo "Baking WATCHER_NATIVE_CHROME=${WATCHER_NATIVE_CHROME} into the launcher."
fi

cat > "${macos_dir}/Watcher" << LAUNCHER
#!/usr/bin/env bash
# Launches Watcher using the Python checkout at ${repo_root}.
# Regenerate this file by re-running scripts/build_macos_app.sh if the
# repo moves or you switch virtualenvs.
${native_chrome_export}
exec "${python_bin}" "${repo_root}/main.py"
LAUNCHER

chmod +x "${macos_dir}/Watcher"

echo "Built ${app_path}"
echo "Using interpreter: ${python_bin}"
echo
echo "Drag it to /Applications, or into the Dock, to pin it like any other app."
echo "Fully quit any already-running Watcher window before relaunching, so you're"
echo "not looking at a stale instance from before this rebuild."
