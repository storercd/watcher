# Watcher

Watcher is a small, draggable, always-on-top desktop window that watches
things — Jenkins jobs and GitHub pull request status checks — and notifies
you when they become actionable (e.g. a build finishes or all checks pass).

## What it does

- Shows a compact, frameless, always-on-top window listing everything you're
  watching, with a live status dot (idle / building / success / failure /
  error) and the last-checked time for each.
- Polls each watched item on a background timer (default every 15s) without
  blocking the GUI.
- Detects state transitions — e.g. a Jenkins job going from *building* to
  *success*/*failure*, or a GitHub PR's checks going from running to
  passed/failed — and fires a notification once per newly completed run,
  not on every poll.
- When a watched item finishes (success, failure, or aborted), its whole row
  turns bright green/red and keeps notifying on every app restart until you
  click the row to acknowledge it — so a finished build can't be missed.
- Fires a native macOS notification (via `osascript`/`display notification`,
  no extra dependency) when something becomes actionable.
- Has a "Mode" dropdown (**At Desk** / **Away**) that controls which
  notifier(s) fire. "Away" currently just logs a TODO — SMS/Slack
  integration is a planned follow-up.
- Saves everything you add to `~/.watcher/config.json` so your watchers
  survive an app restart.

## Running it

Requires Python 3.9+ with Tkinter (ships with the standard python.org macOS
installer and most Linux distro Python packages; on some minimal Linux
installs you may need `apt install python3-tk` or similar).

```bash
python3 main.py
```

From the window:

- Click **+** to add a watcher, then choose **Jenkins Job** or **GitHub PR**.
  - Jenkins: enter the job URL (e.g. `https://jenkins.example.com/job/my-job`)
    and an optional label.
  - GitHub PR: enter the PR URL (e.g.
    `https://github.com/owner/repo/pull/123`) and an optional label. This
    uses your existing `gh` CLI login — see below.
- Click **−** next to a watcher to remove it.
- When a row turns green/red, click anywhere on it to acknowledge the
  finished build and clear the highlight.
- Drag the title bar to move the window; it stays on top of other windows.
- Click **×** to close (this also saves your current config).

The window is unauthenticated/anonymous-access only for Jenkins right now —
no credentials are sent, so the job must allow anonymous read access to its
`api/json` endpoint.

### GitHub PR watcher setup

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

## Project layout

```
watcher/
  core/
    base.py        # Status enum, CheckResult, abstract Watcher interface
    registry.py     # watcher_type -> class registry (for config loading)
    config.py       # JSON load/save for ~/.watcher/config.json
    scheduler.py     # background polling thread + thread-safe results queue
  watchers/
    jenkins.py      # JenkinsWatcher plugin (polls <job_url>/api/json)
    github_pr.py    # GitHubPRWatcher plugin (polls `gh pr view` status checks)
  notifiers/
    base.py         # abstract Notifier interface
    macos.py        # MacOSNotifier (osascript "display notification")
    away.py         # AwayNotifier stub (TODO: SMS/Slack)
    router.py       # NotificationRouter: mode -> notifier(s) dispatch
  gui/
    main_window.py        # the always-on-top window, drag handling, polling loop
    add_watcher_dialog.py # watcher-type picker + "Add Jenkins/GitHub PR Watcher" dialogs
main.py             # entrypoint: python main.py
tests/
  test_jenkins_watcher.py    # unit tests for Jenkins status + transition logic
  test_github_pr_watcher.py  # unit tests for GitHub PR status + transition logic
```

## Plugin architecture

Watcher is built so that new "things to watch" (AWS Step Functions, a
filesystem path, a CI pipeline on another platform, ...) can be added without
touching the GUI or scheduler.

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
   `gui/main_window.py` imports `watcher.watchers.jenkins` purely for its
   registration side effect).

The scheduler, GUI, and config layer only ever interact with the `Watcher`
base class — they never need to know about Jenkins (or any other) specifics.

Notifiers follow the same pattern: subclass `watcher.notifiers.base.Notifier`
and implement `notify(title, message)`, then wire it into
`watcher.notifiers.router.NotificationRouter` under the mode(s) that should
trigger it.

## Tests and linting

Unit tests cover the Jenkins and GitHub PR polling/state-transition logic
(HTTP calls and `gh` CLI invocations are mocked, so no network, real Jenkins
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
- GitHub PR watching requires the `gh` CLI to be installed and authenticated
  separately (`gh auth login`); Watcher does not manage GitHub credentials
  itself.
- "Away" mode notifications are a stub (prints a TODO) pending SMS/Slack
  integration.
- Poll interval is currently a single global value for all watchers
  (`~/.watcher/config.json` -> `poll_interval`), not configurable in the GUI
  yet.
- Frameless window dragging/always-on-top behavior has been developed and
  tested on macOS; other platforms may need minor Tkinter attribute tweaks
  (e.g. `-topmost` is cross-platform, but window-manager chrome removal via
  `overrideredirect` can behave differently on Linux).
