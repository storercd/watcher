"""Main Watcher window: a small, draggable, always-on-top status list."""

from __future__ import annotations

import logging
import queue
import time
import tkinter as tk
from tkinter import messagebox, ttk
from typing import Dict, Optional

from watcher.core.base import CheckResult, Status, Watcher
from watcher.core.config import (
    load_config,
    save_config,
    watchers_to_config_list,
)
from watcher.core.logging_config import configure_logging
from watcher.core.registry import detect_watcher_class, get_watcher_class
from watcher.core.scheduler import Scheduler
from watcher.gui.add_watcher_dialog import AddWatcherDialog, EditNotesDialog
from watcher.notifiers.router import AT_DESK, MODES, NotificationRouter
from watcher.watchers.github_actions_run import GitHubActionsRunWatcher  # noqa: F401 - registers plugin
from watcher.watchers.github_pr import GitHubPRWatcher  # noqa: F401 - registers plugin
from watcher.watchers.jenkins import JenkinsWatcher  # noqa: F401 - registers plugin

logger = logging.getLogger("watcher.gui")

# How long a single _drain_results() tick (which runs on the Tk main thread)
# is allowed to take before it's suspicious enough to log a warning - this is
# scheduled every QUEUE_POLL_MS, so any one call taking much longer than that
# is itself evidence the GUI is about to appear to "hang".
SLOW_DRAIN_THRESHOLD_S = 0.2

STATUS_COLORS = {
    Status.UNKNOWN: "#888888",
    Status.IDLE: "#888888",
    Status.BUILDING: "#d9a400",
    Status.SUCCESS: "#2e8b57",
    Status.FAILURE: "#c0392b",
    Status.ERROR: "#c0392b",
}

STATUS_LABELS = {
    Status.UNKNOWN: "unknown",
    Status.IDLE: "idle",
    Status.BUILDING: "building",
    Status.SUCCESS: "success",
    Status.FAILURE: "failure",
    Status.ERROR: "error",
}

# Full-row background used while an actionable result is unacknowledged, so
# a completed (or failed/aborted) job is unmissable until the user clicks it.
ROW_HIGHLIGHT_BG = {
    Status.SUCCESS: STATUS_COLORS[Status.SUCCESS],
    Status.FAILURE: STATUS_COLORS[Status.FAILURE],
}
DEFAULT_ROW_BG = "#2b2b2b"

QUEUE_POLL_MS = 500


def _make_label_button(
    parent: tk.Widget,
    text: str,
    command,
    bg: str,
    fg: str = "white",
    hover_bg: Optional[str] = None,
    font=("Helvetica", 12),
) -> tk.Label:
    """
    A Label styled/clicked like a button.

    Plain tk.Button on macOS ignores custom bg/fg once the window is active
    (native Aqua rendering takes over and the button flashes white), so these
    icon buttons are built from Labels with click/hover bindings instead.

    Returns:
        The configured Label widget acting as a button.
    """
    btn = tk.Label(parent, text=text, bg=bg, fg=fg, font=font, cursor="pointinghand", padx=4)

    def on_click(_event):
        command()

    def on_enter(_event):
        btn.configure(bg=hover_bg or bg)

    def on_leave(_event):
        btn.configure(bg=bg)

    btn.bind("<Button-1>", on_click)
    btn.bind("<Enter>", on_enter)
    btn.bind("<Leave>", on_leave)
    return btn


class MainWindow:
    """Top-level window showing all watchers and controls to add/remove them."""

    def __init__(self, root: tk.Tk):
        """Build the window, load saved config/watchers, and start polling."""
        self.root = root
        self.config = load_config()
        self.watchers: Dict[str, Watcher] = {}
        self.row_widgets: Dict[str, Dict[str, tk.Widget]] = {}
        self._last_drain_at: Optional[float] = None

        self.scheduler = Scheduler(poll_interval=self.config.get("poll_interval", 15))
        self.router = NotificationRouter(mode=self.config.get("mode", AT_DESK))

        self._load_watchers_from_config()

        self._build_ui()
        self._make_draggable(self.root)

        self.scheduler.set_watchers(list(self.watchers.values()))
        self.scheduler.start()
        self.scheduler.poll_once_async()

        self.root.after(QUEUE_POLL_MS, self._drain_results)

    # ------------------------------------------------------------------
    # Setup
    # ------------------------------------------------------------------
    def _load_watchers_from_config(self) -> None:
        for entry in self.config.get("watchers", []):
            try:
                watcher_cls = get_watcher_class(entry["watcher_type"])
                watcher = watcher_cls.from_config(entry)
                self.watchers[watcher.id] = watcher
            except (KeyError, ValueError) as exc:
                print(f"[watcher] skipping invalid config entry {entry!r}: {exc}")

    def _build_ui(self) -> None:
        self.root.title("Watcher")
        self.root.attributes("-topmost", True)
        self.root.geometry("340x240")
        self.root.configure(bg="#1e1e1e")
        self.root.overrideredirect(True)  # frameless-ish window
        self.root.minsize(260, 160)

        # Title bar (also the drag handle) with close button and mode selector.
        titlebar = tk.Frame(self.root, bg="#2b2b2b", height=28)
        titlebar.pack(side=tk.TOP, fill=tk.X)
        titlebar.pack_propagate(False)
        self._drag_handles = [titlebar]

        title_label = tk.Label(
            titlebar, text="👀 Watcher", bg="#2b2b2b", fg="white", font=("Helvetica", 11, "bold")
        )
        title_label.pack(side=tk.LEFT, padx=6)
        self._drag_handles.append(title_label)

        close_btn = _make_label_button(
            titlebar, "×", self._on_close, bg="#2b2b2b", hover_bg="#c0392b", font=("Helvetica", 12)
        )
        close_btn.pack(side=tk.RIGHT, padx=4)

        add_btn = _make_label_button(
            titlebar, "+", self._on_add_watcher, bg="#2b2b2b", hover_bg="#2e8b57",
            font=("Helvetica", 12, "bold"),
        )
        add_btn.pack(side=tk.RIGHT, padx=2)

        # Mode selector.
        mode_frame = tk.Frame(self.root, bg="#1e1e1e")
        mode_frame.pack(side=tk.TOP, fill=tk.X, padx=6, pady=(4, 0))
        tk.Label(mode_frame, text="Mode:", bg="#1e1e1e", fg="white").pack(side=tk.LEFT)
        self.mode_var = tk.StringVar(value=self.router.mode)
        mode_menu = ttk.Combobox(
            mode_frame, textvariable=self.mode_var, values=list(MODES), state="readonly", width=10
        )
        mode_menu.pack(side=tk.LEFT, padx=4)
        mode_menu.bind("<<ComboboxSelected>>", self._on_mode_change)

        # Scrollable list of watchers.
        list_container = tk.Frame(self.root, bg="#1e1e1e")
        list_container.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=4, pady=4)

        self.canvas = tk.Canvas(list_container, bg="#1e1e1e", highlightthickness=0)
        scrollbar = tk.Scrollbar(list_container, orient="vertical", command=self.canvas.yview)
        self.list_frame = tk.Frame(self.canvas, bg="#1e1e1e")

        self.list_frame.bind(
            "<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        )
        self.canvas.create_window((0, 0), window=self.list_frame, anchor="nw")
        self.canvas.configure(yscrollcommand=scrollbar.set)

        self.canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        for watcher in self.watchers.values():
            self._add_row(watcher)

        if not self.watchers:
            self.empty_label = tk.Label(
                self.list_frame,
                text="No watchers yet. Click + to add one.",
                bg="#1e1e1e",
                fg="gray",
                wraplength=280,
                justify=tk.LEFT,
            )
            self.empty_label.pack(anchor="w", pady=10)
        else:
            self.empty_label = None

    def _make_draggable(self, root: tk.Tk) -> None:
        self._drag_offset = (0, 0)

        def start_drag(event):
            self._drag_offset = (event.x, event.y)

        def do_drag(event):
            x = root.winfo_pointerx() - self._drag_offset[0]
            y = root.winfo_pointery() - self._drag_offset[1]
            root.geometry(f"+{x}+{y}")

        for widget in self._drag_handles:
            widget.bind("<ButtonPress-1>", start_drag)
            widget.bind("<B1-Motion>", do_drag)

    # ------------------------------------------------------------------
    # Watcher row management
    # ------------------------------------------------------------------
    def _add_row(self, watcher: Watcher) -> None:
        if getattr(self, "empty_label", None) is not None:
            self.empty_label.destroy()
            self.empty_label = None

        row = tk.Frame(self.list_frame, bg="#2b2b2b", pady=4, padx=4)
        row.pack(side=tk.TOP, fill=tk.X, pady=2)

        dot = tk.Label(row, text="●", fg=STATUS_COLORS[Status.UNKNOWN], bg="#2b2b2b", font=("Helvetica", 12))
        dot.pack(side=tk.LEFT)

        info_frame = tk.Frame(row, bg="#2b2b2b")
        info_frame.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=6)

        label = tk.Label(info_frame, text=watcher.label, bg="#2b2b2b", fg="white", anchor="w")
        label.pack(side=tk.TOP, fill=tk.X)

        detail = tk.Label(
            info_frame, text="unknown · never checked", bg="#2b2b2b", fg="gray",
            anchor="w", font=("Helvetica", 9),
        )
        detail.pack(side=tk.TOP, fill=tk.X)

        notes_icon = tk.Label(
            row, text="📝", bg="#2b2b2b", font=("Helvetica", 10), cursor="hand2",
        )
        notes_icon.pack(side=tk.RIGHT, padx=(0, 4))
        notes_icon.bind("<Button-1>", lambda _e, wid=watcher.id: self._on_edit_notes(wid))
        self._update_notes_icon(notes_icon, watcher.notes)

        remove_btn = _make_label_button(
            row, "−", lambda wid=watcher.id: self._on_remove_watcher(wid),
            bg="#2b2b2b", hover_bg="#c0392b",
        )
        remove_btn.pack(side=tk.RIGHT)

        self.row_widgets[watcher.id] = {
            "dot": dot,
            "label": label,
            "detail": detail,
            "row": row,
            "info_frame": info_frame,
            "notes_icon": notes_icon,
        }

        # Clicking anywhere on the row (besides the remove/notes buttons)
        # acknowledges a completed/failed build and clears its highlight.
        for widget in (row, info_frame, label, detail):
            widget.bind("<Button-1>", lambda _event, wid=watcher.id: self._on_acknowledge(wid))

    def _remove_row(self, watcher_id: str) -> None:
        widgets = self.row_widgets.pop(watcher_id, None)
        if widgets:
            widgets["row"].destroy()
        if not self.watchers and getattr(self, "empty_label", None) is None:
            self.empty_label = tk.Label(
                self.list_frame,
                text="No watchers yet. Click + to add one.",
                bg="#1e1e1e",
                fg="gray",
                wraplength=280,
                justify=tk.LEFT,
            )
            self.empty_label.pack(anchor="w", pady=10)

    def _update_row(self, watcher_id: str, result: CheckResult) -> None:
        widgets = self.row_widgets.get(watcher_id)
        if not widgets:
            return

        bg = ROW_HIGHLIGHT_BG.get(result.status, DEFAULT_ROW_BG) if result.unacknowledged else DEFAULT_ROW_BG
        fg = "white" if result.unacknowledged else "gray"
        cursor = "pointinghand" if result.unacknowledged else "arrow"

        widgets["row"].configure(bg=bg, cursor=cursor)
        widgets["info_frame"].configure(bg=bg, cursor=cursor)
        widgets["dot"].configure(fg=STATUS_COLORS.get(result.status, "#888888"), bg=bg)
        widgets["label"].configure(bg=bg)

        checked_at = time.strftime("%H:%M:%S", time.localtime(result.checked_at))
        text = f"{STATUS_LABELS.get(result.status, result.status.value)} · last checked {checked_at}"
        if result.detail:
            text += f" · {result.detail}"
        if result.unacknowledged:
            text += " · click to acknowledge"
        widgets["detail"].configure(text=text, bg=bg, fg=fg)

    # ------------------------------------------------------------------
    # Event handlers
    # ------------------------------------------------------------------
    def _on_add_watcher(self) -> None:
        dialog = AddWatcherDialog(self.root)
        if not dialog.result:
            return
        url, label, notes = dialog.result

        watcher_cls = detect_watcher_class(url)
        if watcher_cls is None:
            messagebox.showerror(
                "Add Watcher",
                "Could not determine what kind of URL this is.\n\n"
                "Expected a Jenkins job URL (contains /job/), a GitHub PR URL "
                "(github.com/owner/repo/pull/123), or a GitHub Actions run URL "
                "(github.com/owner/repo/actions/runs/123456).",
            )
            return

        try:
            watcher = watcher_cls(url, label=label, notes=notes)
        except ValueError as exc:
            messagebox.showerror("Add Watcher", str(exc))
            return

        self.watchers[watcher.id] = watcher
        self._add_row(watcher)
        self.scheduler.add_watcher(watcher)
        self._save()
        self.scheduler.poll_once_async()

    def _update_notes_icon(self, icon: tk.Label, notes: str) -> None:
        icon.configure(fg="#d9a400" if notes else "#555555")

    def _on_edit_notes(self, watcher_id: str) -> None:
        watcher = self.watchers.get(watcher_id)
        if watcher is None:
            return
        dialog = EditNotesDialog(self.root, label=watcher.label, initial_notes=watcher.notes)
        if dialog.result is None:
            return
        watcher.notes = dialog.result
        widgets = self.row_widgets.get(watcher_id)
        if widgets:
            self._update_notes_icon(widgets["notes_icon"], watcher.notes)
        self._save()

    def _on_remove_watcher(self, watcher_id: str) -> None:
        self.watchers.pop(watcher_id, None)
        self.scheduler.remove_watcher(watcher_id)
        self._remove_row(watcher_id)
        self._save()

    def _on_acknowledge(self, watcher_id: str) -> None:
        watcher = self.watchers.get(watcher_id)
        if watcher is None or not watcher.unacknowledged:
            return
        watcher.acknowledge()
        self._update_row(
            watcher_id,
            CheckResult(status=watcher.last_status, detail=watcher.last_detail, unacknowledged=False),
        )
        self._save()

    def _on_mode_change(self, _event=None) -> None:
        self.router.set_mode(self.mode_var.get())
        self._save()

    def _on_close(self) -> None:
        self.scheduler.stop()
        self._save()
        self.root.destroy()

    # ------------------------------------------------------------------
    # Scheduler queue draining (runs on the Tk main loop thread)
    # ------------------------------------------------------------------
    def _drain_results(self) -> None:
        tick_start = time.monotonic()
        if self._last_drain_at is not None:
            gap = tick_start - self._last_drain_at
            # This tick is scheduled QUEUE_POLL_MS apart; a much bigger gap
            # means the Tk main loop itself was blocked doing something else
            # (e.g. a synchronous subprocess call) in between ticks.
            if gap > (QUEUE_POLL_MS / 1000.0) * 3:
                logger.warning("GUI main loop stalled for %.3fs between drain ticks", gap)
        self._last_drain_at = tick_start

        try:
            while True:
                watcher_id, result = self.scheduler.results.get_nowait()
                watcher = self.watchers.get(watcher_id)
                if watcher is None:
                    continue
                self._update_row(watcher_id, result)
                if result.newly_actionable:
                    notify_start = time.monotonic()
                    self.router.notify(
                        title=f"Watcher: {watcher.label}",
                        message=f"{STATUS_LABELS.get(result.status, result.status.value)} — {result.detail}",
                    )
                    notify_elapsed = time.monotonic() - notify_start
                    logger.debug("router.notify() for %r took %.3fs", watcher.label, notify_elapsed)
                    if notify_elapsed > 0.1:
                        logger.warning(
                            "router.notify() for %r blocked the GUI thread for %.3fs",
                            watcher.label, notify_elapsed,
                        )
                    self._save()
        except queue.Empty:
            pass
        finally:
            drain_elapsed = time.monotonic() - tick_start
            if drain_elapsed > SLOW_DRAIN_THRESHOLD_S:
                logger.warning("_drain_results() itself took %.3fs", drain_elapsed)
            self.root.after(QUEUE_POLL_MS, self._drain_results)

    def _save(self) -> None:
        self.config["watchers"] = watchers_to_config_list(list(self.watchers.values()))
        self.config["mode"] = self.router.mode
        self.config["poll_interval"] = self.scheduler.poll_interval
        save_config(self.config)


def run() -> None:
    """Create and run the Watcher main window's Tk event loop."""
    configure_logging()
    logger.info("Watcher starting up")
    root = tk.Tk()
    MainWindow(root)
    root.mainloop()
    logger.info("Watcher shut down")
