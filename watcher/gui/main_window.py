"""Main Watcher window: a small, draggable, always-on-top status list."""

from __future__ import annotations

import logging
import queue
import time
import tkinter as tk
import webbrowser
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
from watcher.gui.add_watcher_dialog import AddWatcherDialog, EditWatcherDialog
from watcher.gui.settings_dialog import SettingsDialog
from watcher.notifiers.away import DEFAULT_NTFY_SERVER
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

# How often to re-assert attention (e.g. re-request a Dock bounce) for a
# still-unacknowledged result. Separate from QUEUE_POLL_MS since a bounce
# request can be silently ignored by macOS if the app was frontmost when it
# first fired; this catches the user switching away afterward without
# acknowledging.
NUDGE_INTERVAL_MS = 5000

# Base (scale=1.0) font sizes, bumped up from the original cramped defaults
# for readability. Actual widget fonts are computed by scaling these - see
# MainWindow._scaled_font() - so the whole UI grows/shrinks together both
# automatically (window resize) and manually (the "A-"/"A+" buttons).
FONT_FAMILY = "Helvetica"
BASE_FONT_SIZES = {
    "title": (13, True),
    "button": (14, False),
    "button_bold": (14, True),
    "body": (13, False),
    "detail": (11, False),
    "dot": (14, False),
    "icon": (12, False),
}

# Window width (matching the default geometry below) that corresponds to a
# 1x auto font scale; wider/narrower windows scale fonts up/down from there.
BASE_WINDOW_WIDTH = 420
DEFAULT_WINDOW_SIZE = "420x320"
MIN_WINDOW_WIDTH, MIN_WINDOW_HEIGHT = 300, 200

# Bounds for the automatic (window-width-driven) and manual (user button)
# scale factors, and the combined effective scale applied to fonts.
AUTO_SCALE_MIN, AUTO_SCALE_MAX = 0.7, 2.0
MANUAL_SCALE_MIN, MANUAL_SCALE_MAX = 0.5, 2.5
MANUAL_SCALE_STEP = 0.1
EFFECTIVE_SCALE_MIN, EFFECTIVE_SCALE_MAX = 0.6, 3.0


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def _make_label_button(
    parent: tk.Widget,
    text: str,
    command,
    bg: str,
    fg: str = "white",
    hover_bg: Optional[str] = None,
    font=(FONT_FAMILY, BASE_FONT_SIZES["button"][0]),
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

        # Font scaling state: "manual" is adjusted via the A-/A+ buttons and
        # persisted across restarts; "auto" tracks the window width and is
        # recomputed on every resize. The two combine into one effective
        # scale applied to every registered widget's font.
        self.manual_font_scale = _clamp(
            float(self.config.get("font_scale", 1.0)), MANUAL_SCALE_MIN, MANUAL_SCALE_MAX
        )
        self._auto_font_scale = 1.0
        self._scalable_widgets: list = []  # [(widget, font_key), ...]
        self._last_scaled_width: Optional[int] = None

        self.scheduler = Scheduler(poll_interval=self.config.get("poll_interval", 15))
        self.router = NotificationRouter(
            mode=self.config.get("mode", AT_DESK),
            ntfy_server=self.config.get("ntfy_server", DEFAULT_NTFY_SERVER),
            ntfy_topic=self.config.get("ntfy_topic", ""),
        )
        self.renotify_on_restart_var = tk.BooleanVar(
            value=bool(self.config.get("renotify_on_restart", True))
        )

        self._load_watchers_from_config()
        if self.renotify_on_restart_var.get():
            for watcher in self.watchers.values():
                watcher.forget_acknowledgment()

        self._build_ui()
        self._make_draggable(self.root)

        self.root.bind("<Configure>", self._on_root_configure)

        self.scheduler.set_watchers(list(self.watchers.values()))
        self.scheduler.start()
        self.scheduler.poll_once_async()

        self.root.after(QUEUE_POLL_MS, self._drain_results)
        self.root.after(NUDGE_INTERVAL_MS, self._nudge_tick)

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

    # ------------------------------------------------------------------
    # Font scaling: manual (A-/A+ buttons) + automatic (window width)
    # ------------------------------------------------------------------
    def _effective_font_scale(self) -> float:
        return _clamp(
            self._auto_font_scale * self.manual_font_scale, EFFECTIVE_SCALE_MIN, EFFECTIVE_SCALE_MAX
        )

    def _scaled_font(self, key: str) -> tuple:
        base_size, bold = BASE_FONT_SIZES[key]
        size = max(6, round(base_size * self._effective_font_scale()))
        return (FONT_FAMILY, size, "bold") if bold else (FONT_FAMILY, size)

    def _register_font(self, widget: tk.Widget, key: str) -> None:
        """Track a widget so its font is rescaled by _apply_font_scale()."""
        self._scalable_widgets.append((widget, key))
        widget.configure(font=self._scaled_font(key))

    def _apply_font_scale(self) -> None:
        for widget, key in self._scalable_widgets:
            try:
                widget.configure(font=self._scaled_font(key))
            except tk.TclError:
                continue
        if hasattr(self, "_combobox_style"):
            self._combobox_style.configure("Watcher.TCombobox", font=self._scaled_font("body"))

    def _on_root_configure(self, event: tk.Event) -> None:
        if event.widget is not self.root:
            return
        width = self.root.winfo_width()
        if width == self._last_scaled_width:
            return
        self._last_scaled_width = width
        auto_scale = _clamp(width / BASE_WINDOW_WIDTH, AUTO_SCALE_MIN, AUTO_SCALE_MAX)
        if abs(auto_scale - self._auto_font_scale) < 0.01:
            return
        self._auto_font_scale = auto_scale
        self._apply_font_scale()

    def _on_zoom(self, delta: float) -> None:
        self.manual_font_scale = _clamp(
            self.manual_font_scale + delta, MANUAL_SCALE_MIN, MANUAL_SCALE_MAX
        )
        self._apply_font_scale()
        self._save()

    def _build_ui(self) -> None:
        self.root.title("Watcher")
        self.root.attributes("-topmost", True)
        self.root.geometry(DEFAULT_WINDOW_SIZE)
        self.root.configure(bg="#1e1e1e")
        self.root.overrideredirect(True)  # frameless-ish window
        self.root.minsize(MIN_WINDOW_WIDTH, MIN_WINDOW_HEIGHT)

        # Title bar (also the drag handle) with close button and mode selector.
        titlebar = tk.Frame(self.root, bg="#2b2b2b", height=34)
        titlebar.pack(side=tk.TOP, fill=tk.X)
        titlebar.pack_propagate(False)
        self._drag_handles = [titlebar]

        title_label = tk.Label(titlebar, text="👀 Watcher", bg="#2b2b2b", fg="white")
        title_label.pack(side=tk.LEFT, padx=8)
        self._register_font(title_label, "title")
        self._drag_handles.append(title_label)

        close_btn = _make_label_button(titlebar, "×", self._on_close, bg="#2b2b2b", hover_bg="#c0392b")
        close_btn.pack(side=tk.RIGHT, padx=4)
        self._register_font(close_btn, "button")

        add_btn = _make_label_button(titlebar, "+", self._on_add_watcher, bg="#2b2b2b", hover_bg="#2e8b57")
        add_btn.pack(side=tk.RIGHT, padx=2)
        self._register_font(add_btn, "button_bold")

        settings_btn = _make_label_button(
            titlebar, "⚙", self._on_open_settings, bg="#2b2b2b", hover_bg="#3a3a3a",
        )
        settings_btn.pack(side=tk.RIGHT, padx=2)
        self._register_font(settings_btn, "button")

        # Text size (zoom) controls, independent of window-resize auto-scale.
        zoom_in_btn = _make_label_button(
            titlebar, "A+", lambda: self._on_zoom(MANUAL_SCALE_STEP), bg="#2b2b2b", hover_bg="#3a3a3a",
        )
        zoom_in_btn.pack(side=tk.RIGHT, padx=2)
        self._register_font(zoom_in_btn, "button")

        zoom_out_btn = _make_label_button(
            titlebar, "A-", lambda: self._on_zoom(-MANUAL_SCALE_STEP), bg="#2b2b2b", hover_bg="#3a3a3a",
        )
        zoom_out_btn.pack(side=tk.RIGHT, padx=2)
        self._register_font(zoom_out_btn, "button")

        # Mode selector.
        mode_frame = tk.Frame(self.root, bg="#1e1e1e")
        mode_frame.pack(side=tk.TOP, fill=tk.X, padx=6, pady=(6, 0))
        mode_text_label = tk.Label(mode_frame, text="Mode:", bg="#1e1e1e", fg="white")
        mode_text_label.pack(side=tk.LEFT)
        self._register_font(mode_text_label, "body")
        self.mode_var = tk.StringVar(value=self.router.mode)
        self._combobox_style = ttk.Style()
        self._combobox_style.configure("Watcher.TCombobox", font=self._scaled_font("body"))
        mode_menu = ttk.Combobox(
            mode_frame, textvariable=self.mode_var, values=list(MODES), state="readonly",
            width=10, style="Watcher.TCombobox",
        )
        mode_menu.pack(side=tk.LEFT, padx=4)
        mode_menu.bind("<<ComboboxSelected>>", self._on_mode_change)

        # Scrollable list of watchers.
        list_container = tk.Frame(self.root, bg="#1e1e1e")
        list_container.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=4, pady=4)

        self.canvas = tk.Canvas(list_container, bg="#1e1e1e", highlightthickness=0)
        scrollbar = tk.Scrollbar(list_container, orient="vertical", command=self.canvas.yview)
        self.list_frame = tk.Frame(self.canvas, bg="#1e1e1e")

        self._list_frame_window = self.canvas.create_window((0, 0), window=self.list_frame, anchor="nw")
        self.list_frame.bind(
            "<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        )
        # Keep the inner frame as wide as the canvas so row content reflows
        # (instead of clipping/truncating) as the window is resized.
        self.canvas.bind(
            "<Configure>",
            lambda e: self.canvas.itemconfigure(self._list_frame_window, width=e.width),
        )
        self.canvas.configure(yscrollcommand=scrollbar.set)

        self.canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        for watcher in self.watchers.values():
            self._add_row(watcher)

        self.empty_label = None
        if not self.watchers:
            self._show_empty_label()

        self._add_resize_grip()

    def _show_empty_label(self) -> None:
        self.empty_label = tk.Label(
            self.list_frame,
            text="No watchers yet. Click + to add one.",
            bg="#1e1e1e",
            fg="gray",
            wraplength=360,
            justify=tk.LEFT,
        )
        self._register_font(self.empty_label, "body")
        self.empty_label.pack(anchor="w", pady=10)

    def _add_resize_grip(self) -> None:
        """
        Add a bottom-right drag handle so this frameless window can be resized.

        `overrideredirect(True)` removes native OS window chrome (including
        the native resize border/corner), so without this the window would
        be stuck at its initial size.
        """
        grip = tk.Label(
            self.root, text="⋰", bg="#2b2b2b", fg="#888888", font=("Helvetica", 14, "bold"),
            cursor="bottom_right_corner",
        )
        grip.place(relx=1.0, rely=1.0, anchor="se", width=16, height=16)

        def start_resize(event):
            self._resize_origin = (
                event.x_root, event.y_root, self.root.winfo_width(), self.root.winfo_height(),
            )

        def do_resize(event):
            start_x, start_y, start_w, start_h = self._resize_origin
            new_w = max(self.root.winfo_reqwidth(), start_w + (event.x_root - start_x))
            new_h = max(self.root.winfo_reqheight(), start_h + (event.y_root - start_y))
            min_w, min_h = MIN_WINDOW_WIDTH, MIN_WINDOW_HEIGHT
            new_w = max(new_w, min_w)
            new_h = max(new_h, min_h)
            self.root.geometry(f"{new_w}x{new_h}")

        grip.bind("<ButtonPress-1>", start_resize)
        grip.bind("<B1-Motion>", do_resize)

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

        dot = tk.Label(row, text="●", fg=STATUS_COLORS[Status.UNKNOWN], bg="#2b2b2b")
        dot.pack(side=tk.LEFT)
        self._register_font(dot, "dot")

        info_frame = tk.Frame(row, bg="#2b2b2b")
        info_frame.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=6)

        label = tk.Label(info_frame, text=watcher.label, bg="#2b2b2b", fg="white", anchor="w")
        label.pack(side=tk.TOP, fill=tk.X)
        self._register_font(label, "body")

        detail = tk.Label(
            info_frame, text="unknown · never checked", bg="#2b2b2b", fg="gray",
            anchor="w", wraplength=320, justify=tk.LEFT,
        )
        detail.pack(side=tk.TOP, fill=tk.X)
        self._register_font(detail, "detail")

        followup_label = tk.Label(
            info_frame, text="", bg="#2b2b2b", fg="#d9a400",
            anchor="w", wraplength=320, justify=tk.LEFT, cursor="hand2",
        )
        self._register_font(followup_label, "detail")
        followup_label.bind("<Button-1>", lambda _e, wid=watcher.id: self._on_edit_watcher(wid))

        notes_icon = tk.Label(row, text="📝", bg="#2b2b2b", cursor="hand2")
        notes_icon.pack(side=tk.RIGHT, padx=(0, 4))
        self._register_font(notes_icon, "icon")
        notes_icon.bind("<Button-1>", lambda _e, wid=watcher.id: self._on_edit_watcher(wid))
        self._update_notes_icon(notes_icon, watcher.notes)
        self._update_followup_label(followup_label, detail, watcher.notes)

        remove_btn = _make_label_button(
            row, "−", lambda wid=watcher.id: self._on_remove_watcher(wid),
            bg="#2b2b2b", hover_bg="#c0392b",
        )
        remove_btn.pack(side=tk.RIGHT)
        self._register_font(remove_btn, "button")

        self.row_widgets[watcher.id] = {
            "dot": dot,
            "label": label,
            "detail": detail,
            "row": row,
            "info_frame": info_frame,
            "notes_icon": notes_icon,
            "followup_label": followup_label,
        }

        # Clicking anywhere on the row (besides the remove/notes buttons)
        # acknowledges a completed/failed build and clears its highlight.
        # Double-clicking instead opens the watched item's URL in the
        # browser, so the user can jump straight to the Jenkins job/PR/run.
        for widget in (row, info_frame, label, detail):
            widget.bind("<Button-1>", lambda _event, wid=watcher.id: self._on_acknowledge(wid))
            widget.bind("<Double-Button-1>", lambda _event, wid=watcher.id: self._on_open_url(wid))

    def _remove_row(self, watcher_id: str) -> None:
        widgets = self.row_widgets.pop(watcher_id, None)
        if widgets:
            # Drop these from the scalable-font registry before destroying
            # them, otherwise _apply_font_scale() keeps trying (harmlessly,
            # but pointlessly) to configure dead widgets forever.
            dead = set(widgets.values())
            self._scalable_widgets = [
                (widget, key) for widget, key in self._scalable_widgets if widget not in dead
            ]
            widgets["row"].destroy()
        if not self.watchers and getattr(self, "empty_label", None) is None:
            self._show_empty_label()

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
    def _on_open_settings(self) -> None:
        dialog = SettingsDialog(
            self.root,
            ntfy_server=self.router.away_notifier.server,
            ntfy_topic=self.router.away_notifier.topic,
            renotify_on_restart=self.renotify_on_restart_var.get(),
        )
        if not dialog.result:
            return
        server, topic, renotify_on_restart = dialog.result
        self.router.configure_away(server=server, topic=topic)
        self.renotify_on_restart_var.set(renotify_on_restart)
        self._save()

    def _on_add_watcher(self) -> None:
        dialog = AddWatcherDialog(self.root)
        if not dialog.result:
            return
        url, label, notes = dialog.result
        watcher = self._construct_watcher(url, label, notes, error_title="Add Watcher")
        if watcher is None:
            return

        self.watchers[watcher.id] = watcher
        self._add_row(watcher)
        self.scheduler.add_watcher(watcher)
        self._save()
        self.scheduler.poll_once_async()

    def _construct_watcher(
        self, url: str, label: str, notes: str, error_title: str, watcher_id: Optional[str] = None
    ) -> Optional[Watcher]:
        """
        Detect the watcher type for ``url`` and construct a watcher instance.

        Shows an error dialog and returns None if the URL isn't recognized or
        the watcher type rejects it.

        Returns:
            The constructed watcher, or None on failure (after showing an
            error dialog).
        """
        watcher_cls = detect_watcher_class(url)
        if watcher_cls is None:
            messagebox.showerror(
                error_title,
                "Could not determine what kind of URL this is.\n\n"
                "Expected a Jenkins job URL (contains /job/), a GitHub PR URL "
                "(github.com/owner/repo/pull/123), or a GitHub Actions run URL "
                "(github.com/owner/repo/actions/runs/123456).",
            )
            return None

        try:
            return watcher_cls(url, watcher_id=watcher_id, label=label, notes=notes)
        except ValueError as exc:
            messagebox.showerror(error_title, str(exc))
            return None

    def _update_notes_icon(self, icon: tk.Label, notes: str) -> None:
        icon.configure(fg="#d9a400" if notes else "#555555")

    def _update_followup_label(self, label: tk.Label, detail: tk.Label, notes: str) -> None:
        if notes:
            label.configure(text=f"↳ {notes}")
            if not label.winfo_ismapped():
                label.pack(side=tk.TOP, fill=tk.X, after=detail)
        else:
            label.configure(text="")
            if label.winfo_ismapped():
                label.pack_forget()

    def _on_edit_watcher(self, watcher_id: str) -> None:
        """
        Open the general "edit watcher" dialog.

        Lets the user change the watched URL, label, and follow-up note,
        replacing the watcher in-place if the URL (and therefore what's
        being watched) changed.
        """
        watcher = self.watchers.get(watcher_id)
        if watcher is None:
            return
        dialog = EditWatcherDialog(
            self.root, label=watcher.label, initial_url=watcher.display_url or "", initial_notes=watcher.notes
        )
        if dialog.result is None:
            return
        url, label, notes = dialog.result

        url_changed = url != (watcher.display_url or "")
        if url_changed:
            new_watcher = self._construct_watcher(url, label, notes, error_title="Edit Watcher", watcher_id=watcher_id)
            if new_watcher is None:
                return
            self.watchers[watcher_id] = new_watcher
            self.scheduler.add_watcher(new_watcher)
            watcher = new_watcher
        else:
            watcher.label = label
            watcher.notes = notes

        widgets = self.row_widgets.get(watcher_id)
        if widgets:
            widgets["label"].configure(text=watcher.label)
            self._update_notes_icon(widgets["notes_icon"], watcher.notes)
            self._update_followup_label(widgets["followup_label"], widgets["detail"], watcher.notes)
            if url_changed:
                # The replaced watcher hasn't been checked yet; reset the row
                # to its initial "never checked" appearance instead of
                # leaving stale status from whatever was watched before.
                widgets["row"].configure(bg=DEFAULT_ROW_BG, cursor="arrow")
                widgets["info_frame"].configure(bg=DEFAULT_ROW_BG, cursor="arrow")
                widgets["dot"].configure(fg=STATUS_COLORS[Status.UNKNOWN], bg=DEFAULT_ROW_BG)
                widgets["label"].configure(bg=DEFAULT_ROW_BG)
                widgets["detail"].configure(text="unknown · never checked", bg=DEFAULT_ROW_BG, fg="gray")
        self._save()
        if url_changed:
            self.scheduler.poll_once_async()

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

    def _on_open_url(self, watcher_id: str) -> None:
        """Open the watched item's URL in the default browser (double-click)."""
        watcher = self.watchers.get(watcher_id)
        if watcher is None or not watcher.display_url:
            return
        webbrowser.open(watcher.display_url)

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

    def _nudge_tick(self) -> None:
        """Periodically re-assert attention if any watcher is still unacknowledged."""
        has_unacknowledged = any(watcher.unacknowledged for watcher in self.watchers.values())
        try:
            self.router.nudge(has_unacknowledged)
        except Exception:  # noqa: BLE001 - a nudge failure must never stop the GUI's tick loop
            logger.exception("router.nudge() failed")
        finally:
            self.root.after(NUDGE_INTERVAL_MS, self._nudge_tick)

    def _save(self) -> None:
        self.config["watchers"] = watchers_to_config_list(list(self.watchers.values()))
        self.config["mode"] = self.router.mode
        self.config["ntfy_server"] = self.router.away_notifier.server
        self.config["ntfy_topic"] = self.router.away_notifier.topic
        self.config["poll_interval"] = self.scheduler.poll_interval
        self.config["font_scale"] = self.manual_font_scale
        self.config["renotify_on_restart"] = self.renotify_on_restart_var.get()
        save_config(self.config)


def run() -> None:
    """Create and run the Watcher main window's Tk event loop."""
    configure_logging()
    logger.info("Watcher starting up")
    root = tk.Tk()
    MainWindow(root)
    root.mainloop()
    logger.info("Watcher shut down")
