# Watcher

Watcher is a small desktop window that watches
things — Jenkins jobs, GitHub pull request status checks, and GitHub
Actions workflow runs — and notifies
you when they become actionable (e.g. a build finishes or all checks pass).

## What it does

- Shows a compact window with native macOS title bar (close/minimize/zoom) listing everything you're
  watching, with a live status dot (idle / building / success / failure /
  error) and the last-checked time for each.
- Polls each watched item on a background timer (default every 15s) without
  blocking the UI.
- Detects state transitions — e.g. a Jenkins job going from *building* to
  *success*/*failure*, a GitHub PR's checks going from running to
  passed/failed, or a GitHub Actions run completing — and fires a
  notification once per newly completed run, not on every poll.
- When a watched item finishes (success, failure, or aborted), its whole row
  turns bright green/red and keeps notifying on every app restart until you
  click **Got it** to acknowledge it — so a finished build can't be missed.
- Fires a native macOS notification (via `osascript`/`display notification`,
  no extra dependency) when something becomes actionable.
- Has a "Mode" dropdown (**At Desk** / **Away**) that controls which
  notifier(s) fire. "At Desk" uses native macOS notifications; "Away"
  publishes to a configurable [ntfy](https://ntfy.sh/) topic so you can get
  a push notification on your phone via the ntfy app.
- Has a Settings window (⌘,) for the ntfy server and topic used by "Away"
  mode, polling interval, text size and keep-on-top.
- Saves everything you add to `~/.watcher/config.json` so your watchers
  survive an app restart.

## Installing a pre-built release (recommended)

Every merge to `main` is automatically built and published as a new
[GitHub Release](../../releases) — the most recent one there is always the
current "latest good build", no manual release step needed.

1. Download `Watcher-<version>-macos.zip` from the
   [latest release](../../releases/latest) and unzip it.
2. Drag `Watcher.app` to `/Applications` (or the Dock).
3. **First launch only:** these builds aren't Apple-notarized (no paid Apple
   Developer account), so downloading via a browser leaves a quarantine flag
   that makes Gatekeeper refuse to open it, reporting it as **"damaged"**
   rather than the more familiar "unidentified developer" warning —
   right-click → Open does *not* help with this particular dialog. Instead,
   clear the quarantine flag once from Terminal:

   ```bash
   xattr -cr /Applications/Watcher.app   # or wherever you dragged it
   ```

   After that one-time step, it opens normally (including from the Dock).

**Getting notified of updates:** Watcher checks GitHub for a newer release
shortly after it starts (and periodically while it keeps running) and shows
a small banner at the top of the window if one's available — click it to
open the release page, or the **×** to dismiss that version. There's no
silent/automatic install step: download and re-drag the new `.app` into
`/Applications` the same way as above.

**Versioning:** released builds are versioned `<base>.<build number>` (e.g.
`0.1.47`), where `<build number>` is the commit count on `main` at build
time — it increments on every merge automatically, so the update checker
can tell builds apart even between intentional version bumps. `<base>` is
bumped by hand in `watcher/__init__.py` only for notable milestones.

## Running it

If you'd rather run from source (e.g. to make changes). This needs Python
3.9+ and Swift 5.9+ (Xcode or the Command Line Tools):

```bash
pip install -r requirements.txt
cd macos && WATCHER_REPO=.. swift run WatcherApp   # run from source
./scripts/build_native_app.sh                      # build dist/Watcher.app
./scripts/build_and_run.sh                         # build, then launch it
```

`macos/` is a SwiftUI app that launches the Python engine as a sidecar and
talks to it over the local API (see below). When run from source it finds the
repo (or `WATCHER_REPO`) and uses its `.venv`/`WATCHER_PYTHON`/`python3`; the
built app embeds a frozen copy of the engine instead. Notifications
(including Away-mode ntfy) are delivered by the engine.

The build script uses a project-local `.venv`/`venv` interpreter if one
exists, otherwise `python3` on `PATH`. Drag the resulting `Watcher.app` into
the Dock or `/Applications`. See `packaging/macos/Watcher.icns` for the icon.

From the window:

- Click **+** and paste a Jenkins job, GitHub PR or GitHub Actions run URL;
  the type is detected from the URL.
  - Jenkins: enter the job URL (e.g. `https://jenkins.example.com/job/my-job`)
    and an optional label.
  - GitHub PR: enter the PR URL (e.g.
    `https://github.com/owner/repo/pull/123`) and an optional label. This
    uses your existing `gh` CLI login — see below.
  - GitHub Actions Run: enter the run URL (e.g.
    `https://github.com/owner/repo/actions/runs/123456`) and an optional
    label. Also uses your existing `gh` CLI login — see below.
- Right-click a row to open it in the browser, edit it, or remove it;
  double-click opens it in the browser.
- When a row turns green/red, click **Got it** to acknowledge the finished
  build and clear the highlight.
- **Settings** (⌘,) has text size, keep-window-on-top, polling and ntfy options.

The window is unauthenticated/anonymous-access only for Jenkins right now —
no credentials are sent, so the job must allow anonymous read access to its
`api/json` endpoint.

### GitHub PR / GitHub Actions run watcher setup

GitHub PR watchers poll `gh pr view <pr_url> --json ...` to read the PR's
status-check rollup (GitHub Actions check runs and/or legacy commit
statuses), and report `building` while any check is still running,
`failure` if any finished check failed, or `success` once all have passed.

This requires the [GitHub CLI](https://cli.github.com/) (`gh`) to be
installed and already authenticated on your machine:

```bash
gh auth login     # one-time, if you haven't already
gh auth status     # sanity check
```

No separate configuration is needed in Watcher itself — it shells out to
`gh`, which reuses your existing CLI session/token, so private repos you
already have `gh` access to work out of the box. If `gh` isn't installed or
isn't authenticated, the watcher row shows an `error` status with the
underlying `gh` error message as the detail text.

GitHub Actions Run watchers work the same way, but poll `gh run view
<run_id> --repo owner/repo --json ...` for a single workflow run (the
whole run, not an individual job within it), reporting `building` while
the run is queued/in progress, `failure` if it concluded as a failure,
cancellation, timeout, or similar, and `success` once it completes
cleanly. Re-running the workflow is detected (via the run's attempt
number) and notifies again once the new attempt finishes.

## Project layout

```
watcher/
  core/
    base.py        # Status enum, CheckResult, abstract Watcher interface
    registry.py     # watcher_type -> class registry (for config loading)
    config.py       # JSON load/save for ~/.watcher/config.json
    scheduler.py     # background polling thread + thread-safe results queue
    engine.py        # headless Engine: watchers, polling, notifications, config, update checks
  api/
    server.py       # localhost HTTP/JSON + SSE API over the Engine (contract: docs/api.yaml)
    __main__.py     # `python -m watcher.api`: run the engine headless
  watchers/
    jenkins.py      # JenkinsWatcher plugin (polls <job_url>/api/json)
    github_pr.py    # GitHubPRWatcher plugin (polls `gh pr view` status checks)
    github_actions_run.py # GitHubActionsRunWatcher plugin (polls `gh run view`)
  notifiers/
    base.py         # abstract Notifier interface
    macos.py        # MacOSNotifier (osascript "display notification")
    away.py         # AwayNotifier (publishes to a configurable ntfy topic)
    router.py       # NotificationRouter: mode -> notifier(s) dispatch
tests/
  test_jenkins_watcher.py    # unit tests for Jenkins status + transition logic
  test_github_pr_watcher.py  # unit tests for GitHub PR status + transition logic
  test_github_actions_run_watcher.py # unit tests for GitHub Actions run status + transition logic
  test_engine.py             # unit tests for the headless Engine
  test_api.py                # tests for the local HTTP API
macos/              # SwiftUI app (Package.swift, Sources/WatcherKit, Sources/WatcherApp, Tests)
docs/
  api.yaml          # OpenAPI contract for the local API
```

## Headless engine and local API

Everything except drawing the window lives in `watcher.core.engine.Engine`;
the SwiftUI app is a thin client of it. The engine runs on its own and is
driven over a localhost-only HTTP/JSON API (with a Server-Sent Events stream),
so another native UI (e.g. a future Windows app) can reuse it:

```bash
python -m watcher.api --exit-on-stdin-close
# prints {"port": 51234, "token": "..."} then serves http://127.0.0.1:<port>/v1/...
```

Every request needs `Authorization: Bearer <token>`. See
[`docs/api.yaml`](docs/api.yaml) for the full contract.

## Plugin architecture

Watcher is built so that new "things to watch" (AWS Step Functions, a
filesystem path, a CI pipeline on another platform, ...) can be added without
touching the UI or scheduler.

To add a new watcher type:

1. Subclass `watcher.core.base.Watcher` and implement:
   - `check() -> CheckResult` — poll the target, return a normalized
     `Status` (`idle`/`building`/`success`/`failure`/`error`) plus whether
     this check represents something *newly* actionable.
   - `to_config()` / `from_config()` — serialize/restore the watcher's
     settings (e.g. a URL) to/from the JSON config.
2. Set a unique `watcher_type` class attribute and decorate the class with
   `@register` from `watcher.core.registry` so the config loader can
   reconstruct it on startup.
3. Import the new module somewhere that's loaded at startup (see how
   `core/engine.py` imports `watcher.watchers.jenkins` purely for its
   registration side effect).

The scheduler, engine, and config layer only ever interact with the `Watcher`
base class — they never need to know about Jenkins (or any other) specifics.

Notifiers follow the same pattern: subclass `watcher.notifiers.base.Notifier`
and implement `notify(title, message)`, then wire it into
`watcher.notifiers.router.NotificationRouter` under the mode(s) that should
trigger it.

## Tests and linting

Unit tests cover the Jenkins, GitHub PR, and GitHub Actions run
polling/state-transition logic (HTTP calls and `gh` CLI invocations are
mocked, so no network, real Jenkins
server, or `gh` auth is required to run the tests). Linting is done
with [ruff](https://docs.astral.sh/ruff/) (style, pyflakes, isort, McCabe
complexity, and Google-style docstrings — see `pyproject.toml`):

```bash
pip install -r requirements.txt   # installs pytest + ruff (dev dependencies)
ruff check .
pytest
```

Both run automatically on every pull request via the `CI` GitHub Actions
workflow (`.github/workflows/ci.yml`), across Python 3.9–3.12.

## Known limitations / next steps

- Jenkins access is anonymous-only; no credentials/API token support yet.
- GitHub PR and GitHub Actions run watching both require the `gh` CLI to be
  installed and authenticated separately (`gh auth login`); Watcher does not
  manage GitHub credentials itself.
- GitHub Actions Run watchers track the whole run's status, not an
  individual job within a multi-job run.
- "Away" mode notifications require a reachable ntfy server (defaults to
  the public `ntfy.sh`) and the user to subscribe to the configured topic in
  the ntfy app on their phone; there's no in-app verification that the topic
  is actually being received.
- Poll interval is a single global value for all watchers (set in Settings),
  not per watcher.
- The UI is macOS-only for now; the engine and its API (`docs/api.yaml`) are
  platform-neutral, so a Windows UI could be added as another client.
