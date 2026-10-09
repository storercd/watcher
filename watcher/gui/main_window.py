"""Main Watcher window: a small, draggable, always-on-top status list."""

from __future__ import annotations

import logging
import os
import queue
import time
import tkinter as tk
import webbrowser
from tkinter import messagebox, ttk
from typing import Any, Dict, Optional

from watcher import __full_version__
from watcher.core.base import Status
from watcher.core.engine import Engine
from watcher.core.logging_config import configure_logging
from watcher.gui.add_watcher_dialog import AddWatcherDialog, EditWatcherDialog
from watcher.gui.settings_dialog import SettingsDialog
from watcher.notifiers.router import MODES

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


class _Tooltip:
    """Small hover tooltip; `text` may be a string or a callable returning one."""

    DELAY_MS = 500

    def __init__(self, widget: tk.Widget, text) -> None:
        self.widget = widget
        self.text = text
        self._after_id = None
        self._tip: Optional[tk.Toplevel] = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")

    def _schedule(self, _event=None) -> None:
        self._hide()
        self._after_id = self.widget.after(self.DELAY_MS, self._show)

    def _show(self) -> None:
        self._after_id = None
        text = self.text() if callable(self.text) else self.text
        # A child label placed inside the window (not a separate Toplevel):
        # separate tooltip windows get stacked beneath a floating window on macOS.
        top = self.widget.winfo_toplevel()
        tip = tk.Label(
            top, text=text, bg="#ffffe0", fg="black", relief=tk.SOLID, borderwidth=1,
            padx=6, pady=2, font=(FONT_FAMILY, 11),
        )
        tip.update_idletasks()
        x = self.widget.winfo_rootx() - top.winfo_rootx()
        y = self.widget.winfo_rooty() - top.winfo_rooty() + self.widget.winfo_height() + 4
        x = max(0, min(x, top.winfo_width() - tip.winfo_reqwidth()))
        if y + tip.winfo_reqheight() > top.winfo_height():
            y = self.widget.winfo_rooty() - top.winfo_rooty() - tip.winfo_reqheight() - 4
        tip.place(x=x, y=max(0, y))
        tip.lift()
        self._tip = tip

    def _hide(self, _event=None) -> None:
        if self._after_id is not None:
            self.widget.after_cancel(self._after_id)
            self._after_id = None
        if self._tip is not None:
            self._tip.destroy()
            self._tip = None


def _make_label_button(
    parent: tk.Widget,
    text: str,
    command,
    bg: str,
    fg: str = "white",
    hover_bg: Optional[str] = None,
    font=(FONT_FAMILY, BASE_FONT_SIZES["button"][0]),
    tooltip=None,
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
    btn.rest_bg = bg

    def on_click(_event):
        command()

    def on_enter(_event):
        btn.configure(bg=hover_bg or btn.rest_bg)

    def on_leave(_event):
        btn.configure(bg=btn.rest_bg)

    btn.bind("<Button-1>", on_click)
    btn.bind("<Enter>", on_enter)
    btn.bind("<Leave>", on_leave)
    if tooltip:
        btn.tooltip = _Tooltip(btn, tooltip)
    return btn


class MainWindow:
    """Top-level window showing all watchers and controls to add/remove them."""

    def __init__(self, root: tk.Tk):
        """Build the window, load saved config/watchers, and start polling."""
        self.root = root
        # Deliver notifications on this (Tk main) thread rather than from an
        # engine thread: the Dock-bounce uses AppKit, which wants the main thread.
        self.engine = Engine(version=__full_version__, deliver_notifications=False)
        settings = self.engine.get_settings()
        self.row_widgets: Dict[str, Dict[str, tk.Widget]] = {}
        self._last_drain_at: Optional[float] = None
        self._events: "queue.Queue[Dict[str, Any]]" = queue.Queue()

        # Font scaling state: "manual" is adjusted via the A-/A+ buttons and
        # persisted across restarts; "auto" tracks the window width and is
        # recomputed on every resize. The two combine into one effective
        # scale applied to every registered widget's font.
        self.manual_font_scale = _clamp(
            float(settings.get("font_scale", 1.0)), MANUAL_SCALE_MIN, MANUAL_SCALE_MAX
        )
        self._auto_font_scale = 1.0
        self._scalable_widgets: list = []  # [(widget, font_key), ...]
        self._last_scaled_width: Optional[int] = None

        self._restore_pending = False
        self._build_ui(settings)

        self.root.bind("<Configure>", self._on_root_configure)
        # Tk re-applies its own collection behavior on window events. Do NOT
        # bind <Map> here: when un-minimizing from the Dock, Cocoa delivers
        # the map event from inside a native callback where Python's thread
        # state isn't restorable, and any Python handler aborts the process
        # (PyEval_RestoreThread fatal error).
        self.root.bind("<FocusIn>", lambda e: self._disable_native_fullscreen(), add="+")
        self.root.after(300, self._disable_native_fullscreen)

        # On macOS, clicking the Dock icon of an already-running app sends a
        # "reopen" Apple Event, which Tk/Aqua surfaces as the virtual event
        # <<ReopenApplication>>. Because our window is frameless
        # (overrideredirect), macOS sometimes activates the app without
        # actually restacking this window above whatever else is frontmost.
        # Explicitly re-raise/focus it so the Dock icon reliably brings the
        # window to the top instead of appearing to do nothing.
        # Tk's Dock-click hook is the Tcl command ::tk::mac::ReopenApplication
        # (not a virtual event), and its default does nothing for a minimized
        # window. Override it in pure Tcl and defer a virtual event to the
        # normal event loop, since running Python inside the native callback
        # aborts the process.
        self.root.tk.eval(
            "proc ::tk::mac::ReopenApplication {} "
            "{after idle {event generate . <<WatcherReopen>>}}"
        )
        self.root.bind("<<WatcherReopen>>", self._on_reopen_application)
        self.root.bind("<<ReopenApplication>>", self._on_reopen_application)
        # Cmd+Tab (and Dock clicks that don't send a reopen event) activate
        # the app without <<ReopenApplication>>, so also watch for activation
        # via Cocoa when PyObjC is available.
        self._install_activation_observer()

        self.engine.subscribe(self._events.put)
        self.engine.start()

        self.root.after(QUEUE_POLL_MS, self._drain_events)
        self.root.after(NUDGE_INTERVAL_MS, self._nudge_tick)

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
        self._disable_native_fullscreen()
        width = self.root.winfo_width()
        if width == self._last_scaled_width:
            return
        self._last_scaled_width = width
        auto_scale = _clamp(width / BASE_WINDOW_WIDTH, AUTO_SCALE_MIN, AUTO_SCALE_MAX)
        if abs(auto_scale - self._auto_font_scale) < 0.01:
            return
        self._auto_font_scale = auto_scale
        self._apply_font_scale()

    def _on_reopen_application(self, event: Optional[tk.Event] = None) -> None:
        """
        Bring the window to front when the Dock icon is clicked.

        Toggling ``-topmost`` off then back on (rather than just calling
        ``lift()``) is what actually forces macOS to restack an
        already-topmost, frameless window above whatever else currently has
        focus; without it the app activates but the window can stay hidden
        behind other windows.
        """
        # Cocoa is already un-minimizing the window when the Dock icon is
        # clicked; calling deiconify() mid-animation crashes Tk. Let it
        # finish and only touch the window once it's no longer iconic.
        if self._restore_pending:
            return
        self._restore_pending = True
        self.root.after(150, self._finish_reopen)

    def _finish_reopen(self) -> None:
        self._restore_pending = False
        try:
            if self.root.state() == "iconic":
                # Dock icon click (not the minimized-window thumbnail):
                # Cocoa won't restore it, so do it ourselves, outside the
                # native callback.
                self.root.deiconify()
        except tk.TclError:
            return
        self._disable_native_fullscreen()
        if self.pinned:
            self.root.attributes("-topmost", False)
            self.root.attributes("-topmost", True)
        self.root.lift()
        self.root.focus_force()

    def _disable_native_fullscreen(self) -> None:
        """
        Disable macOS native full screen so the green button zooms instead.

        Native full screen crashes Tk 8.6 (fatal error inside setStyleMask
        during the transition).
        """
        try:
            from AppKit import NSApplication  # noqa: PLC0415 - optional, lazy dep
        except ImportError:
            return
        fullscreen_primary, fullscreen_none = 1 << 7, 1 << 9
        for window in NSApplication.sharedApplication().windows():
            if window.isMiniaturized():
                continue
            behavior = window.collectionBehavior()
            window.setCollectionBehavior_((behavior & ~fullscreen_primary) | fullscreen_none)

    def _install_activation_observer(self) -> None:
        try:
            from Foundation import NSNotificationCenter  # noqa: PLC0415 - optional, lazy dep
        except ImportError:
            return

        def _on_active(_notification) -> None:
            self.root.after(0, self._on_reopen_application)

        # Keep a reference so the observer isn't garbage-collected.
        self._activation_observer = (
            NSNotificationCenter.defaultCenter().addObserverForName_object_queue_usingBlock_(
                "NSApplicationDidBecomeActiveNotification", None, None, _on_active
            )
        )

    def _on_zoom(self, delta: float) -> None:
        self.manual_font_scale = _clamp(
            self.manual_font_scale + delta, MANUAL_SCALE_MIN, MANUAL_SCALE_MAX
        )
        self._apply_font_scale()
        self.engine.update_settings({"font_scale": self.manual_font_scale})

    def _build_ui(self, settings: Dict[str, Any]) -> None:
        self.root.title("Watcher")
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.pinned = bool(settings.get("always_on_top", False))
        self.root.attributes("-topmost", self.pinned)
        self.root.geometry(DEFAULT_WINDOW_SIZE)
        self.root.configure(bg="#1e1e1e")
        self.root.minsize(MIN_WINDOW_WIDTH, MIN_WINDOW_HEIGHT)

        # Toolbar below the native macOS title bar.
        titlebar = tk.Frame(self.root, bg="#2b2b2b", height=34)
        titlebar.pack(side=tk.TOP, fill=tk.X)
        titlebar.pack_propagate(False)

        title_label = tk.Label(titlebar, text="👀 Watcher", bg="#2b2b2b", fg="white")
        title_label.pack(side=tk.LEFT, padx=8)
        self._register_font(title_label, "title")

        self.pin_btn = _make_label_button(
            titlebar, "📌", self._on_toggle_pin, bg="#2b2b2b", hover_bg="#3a3a3a",
            tooltip=lambda: (
                "Keep on top: ON (click to let other windows cover Watcher)"
                if self.pinned
                else "Keep on top: OFF (click to float above other windows)"
            ),
        )
        self.pin_btn.pack(side=tk.RIGHT, padx=4)
        self._register_font(self.pin_btn, "button")
        self._update_pin_button()

        add_btn = _make_label_button(
            titlebar, "+", self._on_add_watcher, bg="#2b2b2b", hover_bg="#2e8b57",
            tooltip="Add a watcher",
        )
        add_btn.pack(side=tk.RIGHT, padx=2)
        self._register_font(add_btn, "button_bold")

        settings_btn = _make_label_button(
            titlebar, "⚙", self._on_open_settings, bg="#2b2b2b", hover_bg="#3a3a3a",
            tooltip="Settings (notifications, poll interval)",
        )
        settings_btn.pack(side=tk.RIGHT, padx=2)
        self._register_font(settings_btn, "button")

        # Text size (zoom) controls, independent of window-resize auto-scale.
        zoom_in_btn = _make_label_button(
            titlebar, "A+", lambda: self._on_zoom(MANUAL_SCALE_STEP), bg="#2b2b2b", hover_bg="#3a3a3a",
            tooltip="Increase text size",
        )
        zoom_in_btn.pack(side=tk.RIGHT, padx=2)
        self._register_font(zoom_in_btn, "button")

        zoom_out_btn = _make_label_button(
            titlebar, "A-", lambda: self._on_zoom(-MANUAL_SCALE_STEP), bg="#2b2b2b", hover_bg="#3a3a3a",
            tooltip="Decrease text size",
        )
        zoom_out_btn.pack(side=tk.RIGHT, padx=2)
        self._register_font(zoom_out_btn, "button")

        # Update-available banner: hidden by default, shown (packed) above
        # the mode selector by _show_update_banner() when a newer release is
        # found. Click the text to open the release page; click x to dismiss
        # it for that specific version (see _on_dismiss_update).
        self.update_banner = tk.Frame(self.root, bg="#4a4420")
        self._update_banner_url: Optional[str] = None
        update_label = tk.Label(
            self.update_banner, text="", bg="#4a4420", fg="#ffd479", anchor="w", cursor="pointinghand",
        )
        update_label.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=8, pady=3)
        update_label.bind("<Button-1>", lambda _event: self._on_open_update())
        self._register_font(update_label, "detail")
        self.update_banner_label = update_label
        update_dismiss_btn = _make_label_button(
            self.update_banner, "×", self._on_dismiss_update, bg="#4a4420", hover_bg="#5f5728",
            tooltip="Dismiss this update notice",
        )
        update_dismiss_btn.pack(side=tk.RIGHT, padx=4)
        self._register_font(update_dismiss_btn, "button")

        # Mode selector.
        self.mode_frame = tk.Frame(self.root, bg="#1e1e1e")
        mode_frame = self.mode_frame
        mode_frame.pack(side=tk.TOP, fill=tk.X, padx=6, pady=(6, 0))
        mode_text_label = tk.Label(mode_frame, text="Mode:", bg="#1e1e1e", fg="white")
        mode_text_label.pack(side=tk.LEFT)
        self._register_font(mode_text_label, "body")
        self.mode_var = tk.StringVar(value=settings["mode"])
        self._combobox_style = ttk.Style()
        self._combobox_style.configure("Watcher.TCombobox", font=self._scaled_font("body"))
        mode_menu = ttk.Combobox(
            mode_frame, textvariable=self.mode_var, values=list(MODES), state="readonly",
            width=10, style="Watcher.TCombobox",
        )
        mode_menu.bind("<<ComboboxSelected>>", self._on_mode_change)
        mode_menu.pack(side=tk.LEFT, padx=4)

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

        self.empty_label = None
        watchers = self.engine.list_watchers()
        for snapshot in watchers:
            self._add_row(snapshot)
            if snapshot["last_checked"] is not None:
                self._update_row(snapshot)

        if not watchers:
            self._show_empty_label()

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

    def _on_toggle_pin(self) -> None:
        self.pinned = not self.pinned
        self.root.attributes("-topmost", self.pinned)
        self._update_pin_button()
        self.engine.update_settings({"always_on_top": self.pinned})

    def _update_pin_button(self) -> None:
        rest_bg = "#2e6fba" if self.pinned else "#2b2b2b"
        self.pin_btn.rest_bg = rest_bg
        self.pin_btn.configure(bg=rest_bg, fg="white" if self.pinned else "#888888")

    # ------------------------------------------------------------------
    # Watcher row management
    # ------------------------------------------------------------------
    def _add_row(self, watcher: Dict[str, Any]) -> None:
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

        label = tk.Label(info_frame, text=watcher["label"], bg="#2b2b2b", fg="white", anchor="w")
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
        followup_label.bind("<Button-1>", lambda _e, wid=watcher["id"]: self._on_edit_watcher(wid))

        notes_icon = tk.Label(row, text="📝", bg="#2b2b2b", cursor="hand2")
        notes_icon.pack(side=tk.RIGHT, padx=(0, 4))
        self._register_font(notes_icon, "icon")
        notes_icon.bind("<Button-1>", lambda _e, wid=watcher["id"]: self._on_edit_watcher(wid))
        self._update_notes_icon(notes_icon, watcher["notes"])
        self._update_followup_label(followup_label, detail, watcher["notes"])

        remove_btn = _make_label_button(
            row, "−", lambda wid=watcher["id"]: self._on_remove_watcher(wid),
            bg="#2b2b2b", hover_bg="#c0392b", tooltip="Stop watching this item",
        )
        remove_btn.pack(side=tk.RIGHT)
        self._register_font(remove_btn, "button")

        self.row_widgets[watcher["id"]] = {
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
            widget.bind("<Button-1>", lambda _event, wid=watcher["id"]: self._on_acknowledge(wid))
            widget.bind("<Double-Button-1>", lambda _event, wid=watcher["id"]: self._on_open_url(wid))

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
        if not self.row_widgets and getattr(self, "empty_label", None) is None:
            self._show_empty_label()

    def _update_row(self, snapshot: Dict[str, Any]) -> None:
        """Render a full engine snapshot (label, notes and status) into its row."""
        widgets = self.row_widgets.get(snapshot["id"])
        if not widgets:
            return

        status = Status(snapshot["status"])
        unacknowledged = snapshot["unacknowledged"]
        bg = ROW_HIGHLIGHT_BG.get(status, DEFAULT_ROW_BG) if unacknowledged else DEFAULT_ROW_BG
        fg = "white" if unacknowledged else "gray"
        cursor = "pointinghand" if unacknowledged else "arrow"

        widgets["row"].configure(bg=bg, cursor=cursor)
        widgets["info_frame"].configure(bg=bg, cursor=cursor)
        widgets["dot"].configure(fg=STATUS_COLORS.get(status, "#888888"), bg=bg)
        widgets["label"].configure(text=snapshot["label"], bg=bg)
        self._update_notes_icon(widgets["notes_icon"], snapshot["notes"])
        self._update_followup_label(widgets["followup_label"], widgets["detail"], snapshot["notes"])

        if snapshot["last_checked"] is None:
            text = "unknown · never checked"
        else:
            checked_at = time.strftime("%H:%M:%S", time.localtime(snapshot["last_checked"]))
            text = f"{STATUS_LABELS.get(status, status.value)} · last checked {checked_at}"
            if snapshot["detail"]:
                text += f" · {snapshot['detail']}"
            if unacknowledged:
                text += " · click to acknowledge"
        widgets["detail"].configure(text=text, bg=bg, fg=fg)

    # ------------------------------------------------------------------
    # Event handlers
    # ------------------------------------------------------------------
    def _on_open_settings(self) -> None:
        settings = self.engine.get_settings()
        dialog = SettingsDialog(
            self.root,
            ntfy_server=settings["ntfy_server"],
            ntfy_topic=settings["ntfy_topic"],
            renotify_on_restart=settings["renotify_on_restart"],
        )
        if not dialog.result:
            return
        server, topic, renotify_on_restart = dialog.result
        self.engine.update_settings(
            {"ntfy_server": server, "ntfy_topic": topic, "renotify_on_restart": renotify_on_restart}
        )

    def _on_add_watcher(self) -> None:
        dialog = AddWatcherDialog(self.root)
        if not dialog.result:
            return
        url, label, notes = dialog.result
        try:
            self.engine.add_watcher(url, label, notes)
        except ValueError as exc:
            messagebox.showerror("Add Watcher", str(exc))

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
        snapshot = self.engine.get_watcher(watcher_id)
        if snapshot is None:
            return
        dialog = EditWatcherDialog(
            self.root, label=snapshot["label"], initial_url=snapshot["url"] or "", initial_notes=snapshot["notes"]
        )
        if dialog.result is None:
            return
        url, label, notes = dialog.result

        try:
            self.engine.update_watcher(watcher_id, url=url, label=label, notes=notes)
        except ValueError as exc:
            messagebox.showerror("Edit Watcher", str(exc))
        except KeyError:
            logger.debug("watcher %s was removed while its edit dialog was open", watcher_id)

    def _on_remove_watcher(self, watcher_id: str) -> None:
        self.engine.remove_watcher(watcher_id)

    def _on_acknowledge(self, watcher_id: str) -> None:
        self.engine.acknowledge(watcher_id)

    def _on_open_url(self, watcher_id: str) -> None:
        """Open the watched item's URL in the default browser (double-click)."""
        snapshot = self.engine.get_watcher(watcher_id)
        if snapshot is None or not snapshot["url"]:
            return
        webbrowser.open(snapshot["url"])

    def _on_mode_change(self, _event=None) -> None:
        self.engine.update_settings({"mode": self.mode_var.get()})

    def _on_close(self) -> None:
        self.engine.stop()
        self.root.destroy()
        logger.info("Watcher shut down")
        # Signals, rather than waits for, the engine's daemon threads above -
        # one of them may still be blocked inside a
        # network call (urllib/ssl) when the user closes the window. Falling
        # through to normal interpreter shutdown in that case can segfault
        # (Python finalizing state out from under a thread stuck in a C
        # extension's blocking I/O). os._exit() tears the process down
        # immediately at the OS level instead, skipping that finalization -
        # safe here since config is already saved above.
        os._exit(0)

    # ------------------------------------------------------------------
    # Engine event draining (runs on the Tk main loop thread)
    # ------------------------------------------------------------------
    def _drain_events(self) -> None:
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
                self._apply_event(self._events.get_nowait())
        except queue.Empty:
            pass
        finally:
            drain_elapsed = time.monotonic() - tick_start
            if drain_elapsed > SLOW_DRAIN_THRESHOLD_S:
                logger.warning("_drain_events() itself took %.3fs", drain_elapsed)
            self.root.after(QUEUE_POLL_MS, self._drain_events)

    def _apply_event(self, event: Dict[str, Any]) -> None:
        """Reflect one engine event in the UI (and deliver notifications here, on the Tk thread)."""
        kind = event["type"]
        if kind == "watcher_added":
            if getattr(self, "empty_label", None) is not None:
                self.empty_label.destroy()
                self.empty_label = None
            self._add_row(event["watcher"])
            self._update_row(event["watcher"])
        elif kind == "watcher_updated":
            self._update_row(event["watcher"])
        elif kind == "watcher_removed":
            self._remove_row(event["id"])
        elif kind == "notification":
            self._deliver_notification(event["title"], event["message"])
        elif kind == "update_available":
            self._show_update_banner(event["version"], event["url"])

    def _deliver_notification(self, title: str, message: str) -> None:
        notify_start = time.monotonic()
        self.engine.router.notify(title=title, message=message)
        notify_elapsed = time.monotonic() - notify_start
        logger.debug("router.notify() for %r took %.3fs", title, notify_elapsed)
        if notify_elapsed > 0.1:
            logger.warning("router.notify() for %r blocked the GUI thread for %.3fs", title, notify_elapsed)

    def _nudge_tick(self) -> None:
        """Periodically re-assert attention if any watcher is still unacknowledged."""
        try:
            self.engine.nudge()
        finally:
            self.root.after(NUDGE_INTERVAL_MS, self._nudge_tick)

    def _show_update_banner(self, version: str, url: str) -> None:
        logger.info("update available: %s", version)
        self._update_banner_url = url
        self._update_banner_version = version
        self.update_banner_label.configure(text=f"⬆ Watcher {version} available — click to download")
        self.update_banner.pack(side=tk.TOP, fill=tk.X, before=self.mode_frame)

    def _on_open_update(self) -> None:
        if self._update_banner_url:
            webbrowser.open(self._update_banner_url)

    def _on_dismiss_update(self) -> None:
        self.update_banner.pack_forget()
        version = getattr(self, "_update_banner_version", None)
        if version:
            self.engine.dismiss_update(version)


def run() -> None:
    """Create and run the Watcher main window's Tk event loop."""
    configure_logging()
    logger.info("Watcher starting up")
    root = tk.Tk()
    MainWindow(root)
    # _on_close() logs shutdown and calls os._exit() itself (see its
    # docstring/comment) rather than letting mainloop() return normally, so
    # there's deliberately no post-mainloop code here.
    root.mainloop()
