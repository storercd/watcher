"""
Dialogs for adding and editing a watcher.

Includes ``AddWatcherDialog`` that takes one pasted URL and lets
``main_window.py`` figure out which ``Watcher`` subclass it belongs to (via
``watcher.core.registry.detect_watcher_class``), instead of asking the user
to pick a watcher type up front. ``EditWatcherDialog`` reuses the same
URL/label/follow-up form, pre-filled, to let the user change anything about
an existing watcher - not just its follow-up note.
"""

from __future__ import annotations

import tkinter as tk
from tkinter import simpledialog
from typing import Optional, Tuple


def _build_watcher_form(master, dialog, initial_url: str = "", initial_label: str = "", initial_notes: str = ""):
    """
    Build the shared URL/label/follow-up form fields used by both the add
    and edit dialogs, pre-filled with the given initial values.

    Attaches ``url_var``, ``label_var``, and ``notes_text`` to ``dialog`` and
    returns the widget that should receive initial keyboard focus.

    Returns:
        The Entry widget that should receive initial keyboard focus.
    """
    dialog.url_var = tk.StringVar(value=initial_url)
    dialog.label_var = tk.StringVar(value=initial_label)

    tk.Label(master, text="URL to watch:").grid(row=0, column=0, sticky="w", padx=4, pady=4)
    url_entry = tk.Entry(master, textvariable=dialog.url_var, width=44)
    url_entry.grid(row=0, column=1, padx=4, pady=4)

    tk.Label(master, text="Label (optional):").grid(row=1, column=0, sticky="w", padx=4, pady=4)
    tk.Entry(master, textvariable=dialog.label_var, width=44).grid(row=1, column=1, padx=4, pady=4)

    tk.Label(master, text="Follow Up (optional):").grid(row=2, column=0, sticky="nw", padx=4, pady=4)
    dialog.notes_text = tk.Text(master, width=34, height=3, wrap="word")
    dialog.notes_text.grid(row=2, column=1, padx=4, pady=4)
    if initial_notes:
        dialog.notes_text.insert("1.0", initial_notes)

    tk.Label(
        master,
        text=(
            "Paste a Jenkins job, GitHub PR, or GitHub Actions run URL —\n"
            "the watcher type is detected automatically.\n"
            "GitHub URLs use your `gh` CLI login — run `gh auth login` first."
        ),
        fg="gray",
        justify=tk.LEFT,
    ).grid(row=3, column=0, columnspan=2, sticky="w", padx=4)

    return url_entry  # initial focus


class AddWatcherDialog(simpledialog.Dialog):
    """Modal dialog that asks for a URL to watch; the type is auto-detected."""

    def __init__(self, parent):
        """Initialize dialog state before building the modal body."""
        self.url_var: Optional[tk.StringVar] = None
        self.label_var: Optional[tk.StringVar] = None
        self.notes_text: Optional[tk.Text] = None
        self.result: Optional[Tuple[str, str, str]] = None
        super().__init__(parent, title="Add Watcher")

    def body(self, master):
        """
        Build the dialog's form fields and return the widget to focus initially.

        Returns:
            The Entry widget that should receive initial keyboard focus.
        """
        return _build_watcher_form(master, self)

    def validate(self) -> bool:
        """
        Require a non-empty URL before the dialog can be accepted.

        Returns:
            True if the URL field is non-empty, False otherwise.
        """
        if not self.url_var.get().strip():
            return False
        return True

    def apply(self) -> None:
        """Store the entered URL/label/notes as the dialog's result on accept."""
        notes = self.notes_text.get("1.0", "end").strip() if self.notes_text else ""
        self.result = (self.url_var.get().strip(), self.label_var.get().strip(), notes)


class EditWatcherDialog(simpledialog.Dialog):
    """Modal dialog for editing everything about an existing watcher: its
    watched URL, label, and follow-up note."""

    def __init__(self, parent, label: str, initial_url: str = "", initial_notes: str = ""):
        """Initialize dialog state, pre-filling the form with the watcher's current values."""
        self.label = label
        self.initial_url = initial_url
        self.initial_notes = initial_notes
        self.url_var: Optional[tk.StringVar] = None
        self.label_var: Optional[tk.StringVar] = None
        self.notes_text: Optional[tk.Text] = None
        self.result: Optional[Tuple[str, str, str]] = None
        super().__init__(parent, title=f"Edit Watcher — {label}")

    def body(self, master):
        """
        Build the dialog's form fields and return the widget to focus initially.

        Returns:
            The Entry widget that should receive initial keyboard focus.
        """
        return _build_watcher_form(
            master, self, initial_url=self.initial_url, initial_label=self.label, initial_notes=self.initial_notes
        )

    def validate(self) -> bool:
        """
        Require a non-empty URL before the dialog can be accepted.

        Returns:
            True if the URL field is non-empty, False otherwise.
        """
        if not self.url_var.get().strip():
            return False
        return True

    def apply(self) -> None:
        """Store the edited URL/label/notes as the dialog's result on accept."""
        notes = self.notes_text.get("1.0", "end").strip() if self.notes_text else ""
        self.result = (self.url_var.get().strip(), self.label_var.get().strip(), notes)
