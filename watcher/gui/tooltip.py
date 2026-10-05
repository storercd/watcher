"""A minimal hover tooltip for tkinter widgets, used to show watcher notes."""

from __future__ import annotations

import tkinter as tk
from typing import Optional

SHOW_DELAY_MS = 400


class Tooltip:
    """
    Attaches a small popup to a widget that appears after a short hover delay.

    The displayed text can be changed later via :meth:`set_text`; the tooltip
    stays bound to the widget's hover/leave events but shows nothing (and
    schedules nothing) when the text is empty.
    """

    def __init__(self, widget: tk.Widget, text: str = ""):
        """Bind hover handlers to ``widget`` and set the initial tooltip text."""
        self.widget = widget
        self.text = text
        self._after_id: Optional[str] = None
        self._tip_window: Optional[tk.Toplevel] = None

        widget.bind("<Enter>", self._on_enter, add="+")
        widget.bind("<Leave>", self._on_leave, add="+")
        widget.bind("<ButtonPress>", self._on_leave, add="+")

    def set_text(self, text: str) -> None:
        """Update the text shown next time the tooltip is triggered."""
        self.text = text

    def _on_enter(self, _event=None) -> None:
        if not self.text:
            return
        self._cancel_pending()
        self._after_id = self.widget.after(SHOW_DELAY_MS, self._show)

    def _on_leave(self, _event=None) -> None:
        self._cancel_pending()
        self._hide()

    def _cancel_pending(self) -> None:
        if self._after_id is not None:
            self.widget.after_cancel(self._after_id)
            self._after_id = None

    def _show(self) -> None:
        if self._tip_window is not None or not self.text:
            return
        x = self.widget.winfo_rootx() + 12
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 4

        self._tip_window = tw = tk.Toplevel(self.widget)
        tw.wm_overrideredirect(True)
        tw.wm_geometry(f"+{x}+{y}")
        tw.attributes("-topmost", True)
        label = tk.Label(
            tw,
            text=self.text,
            justify=tk.LEFT,
            bg="#ffffe0",
            fg="black",
            relief=tk.SOLID,
            borderwidth=1,
            font=("Helvetica", 9),
            wraplength=260,
            padx=4,
            pady=2,
        )
        label.pack()

    def _hide(self) -> None:
        if self._tip_window is not None:
            self._tip_window.destroy()
            self._tip_window = None
