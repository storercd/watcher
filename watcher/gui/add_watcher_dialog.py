"""
Simple dialog for adding a new Jenkins watcher.

Kept Jenkins-specific for now (matching the task's initial scope), but the
return shape (``job_url``/``label``) is intentionally generic so it's easy to
add a watcher-type dropdown later that swaps the fields shown.
"""

from __future__ import annotations

import tkinter as tk
from tkinter import simpledialog
from typing import Optional, Tuple


class AddJenkinsWatcherDialog(simpledialog.Dialog):
    """Modal dialog that asks for a Jenkins job URL and optional label."""

    def __init__(self, parent):
        """Initialize dialog state before building the modal body."""
        self.job_url_var = tk.StringVar()
        self.label_var = tk.StringVar()
        self.result: Optional[Tuple[str, str]] = None
        super().__init__(parent, title="Add Jenkins Watcher")

    def body(self, master):
        """
        Build the dialog's form fields and return the widget to focus initially.

        Returns:
            The Entry widget that should receive initial keyboard focus.
        """
        tk.Label(master, text="Jenkins job URL:").grid(row=0, column=0, sticky="w", padx=4, pady=4)
        url_entry = tk.Entry(master, textvariable=self.job_url_var, width=40)
        url_entry.grid(row=0, column=1, padx=4, pady=4)

        tk.Label(master, text="Label (optional):").grid(row=1, column=0, sticky="w", padx=4, pady=4)
        tk.Entry(master, textvariable=self.label_var, width=40).grid(row=1, column=1, padx=4, pady=4)

        tk.Label(
            master,
            text="e.g. https://jenkins.example.com/job/my-job",
            fg="gray",
        ).grid(row=2, column=0, columnspan=2, sticky="w", padx=4)

        return url_entry  # initial focus

    def validate(self) -> bool:
        """
        Require a non-empty job URL before the dialog can be accepted.

        Returns:
            True if the job URL field is non-empty, False otherwise.
        """
        if not self.job_url_var.get().strip():
            return False
        return True

    def apply(self) -> None:
        """Store the entered URL/label as the dialog's result on accept."""
        self.result = (self.job_url_var.get().strip(), self.label_var.get().strip())
