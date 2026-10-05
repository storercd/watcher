"""
Dialogs for adding a new watcher.

Includes a single ``AddWatcherDialog`` that takes one pasted URL and lets
``main_window.py`` figure out which ``Watcher`` subclass it belongs to (via
``watcher.core.registry.detect_watcher_class``), instead of asking the user
to pick a watcher type up front. Also includes a dialog for editing a
watcher's follow-up notes.
"""

from __future__ import annotations

import tkinter as tk
from tkinter import simpledialog
from typing import Optional, Tuple


class AddWatcherDialog(simpledialog.Dialog):
    """Modal dialog that asks for a URL to watch; the type is auto-detected."""

    def __init__(self, parent):
        """Initialize dialog state before building the modal body."""
        self.url_var = tk.StringVar()
        self.label_var = tk.StringVar()
        self.notes_text: Optional[tk.Text] = None
        self.result: Optional[Tuple[str, str, str]] = None
        super().__init__(parent, title="Add Watcher")

    def body(self, master):
        """
        Build the dialog's form fields and return the widget to focus initially.

        Returns:
            The Entry widget that should receive initial keyboard focus.
        """
        tk.Label(master, text="URL to watch:").grid(row=0, column=0, sticky="w", padx=4, pady=4)
        url_entry = tk.Entry(master, textvariable=self.url_var, width=44)
        url_entry.grid(row=0, column=1, padx=4, pady=4)

        tk.Label(master, text="Label (optional):").grid(row=1, column=0, sticky="w", padx=4, pady=4)
        tk.Entry(master, textvariable=self.label_var, width=44).grid(row=1, column=1, padx=4, pady=4)

        tk.Label(master, text="Notes (optional):").grid(row=2, column=0, sticky="nw", padx=4, pady=4)
        self.notes_text = tk.Text(master, width=34, height=3, wrap="word")
        self.notes_text.grid(row=2, column=1, padx=4, pady=4)

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


class EditNotesDialog(simpledialog.Dialog):
    """Modal dialog for editing the follow-up notes on an existing watcher."""

    def __init__(self, parent, label: str, initial_notes: str = ""):
        """Initialize dialog state, pre-filling the text box with existing notes."""
        self.label = label
        self.initial_notes = initial_notes
        self.notes_text: Optional[tk.Text] = None
        self.result: Optional[str] = None
        super().__init__(parent, title=f"Notes — {label}")

    def body(self, master):
        """
        Build the dialog's form fields and return the widget to focus initially.

        Returns:
            The Text widget that should receive initial keyboard focus.
        """
        tk.Label(master, text=f"Why does {self.label!r} matter? What's the follow-up?").grid(
            row=0, column=0, sticky="w", padx=4, pady=(4, 2)
        )
        self.notes_text = tk.Text(master, width=40, height=5, wrap="word")
        self.notes_text.grid(row=1, column=0, padx=4, pady=(0, 4))
        self.notes_text.insert("1.0", self.initial_notes)
        return self.notes_text  # initial focus

    def apply(self) -> None:
        """Store the edited notes as the dialog's result on accept."""
        self.result = self.notes_text.get("1.0", "end").strip() if self.notes_text else ""
