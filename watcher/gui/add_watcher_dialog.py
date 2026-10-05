"""
Dialogs for adding a new watcher.

Includes a watcher-type picker plus one dialog per watcher type (Jenkins
Includes a watcher-type picker plus one dialog per watcher type (Jenkins
job, GitHub PR, GitHub Actions run), plus a dialog for editing a watcher's
follow-up notes. The per-type "add" dialogs each return a generic
``(target, label, notes)`` tuple so ``main_window.py`` can build the right
``Watcher`` subclass without needing to know the field names used in each
dialog's body.
"""

from __future__ import annotations

import tkinter as tk
from tkinter import simpledialog
from typing import Optional, Tuple


class ChooseWatcherTypeDialog(simpledialog.Dialog):
    """Modal dialog that asks which kind of watcher to add."""

    def __init__(self, parent):
        """Initialize dialog state before building the modal body."""
        self.result: Optional[str] = None
        super().__init__(parent, title="Add Watcher")

    def body(self, master):
        """
        Build the dialog's type-choice buttons.

        Returns:
            None — this dialog has no field to focus initially.
        """
        tk.Label(master, text="What do you want to watch?").grid(
            row=0, column=0, columnspan=2, sticky="w", padx=4, pady=(4, 8)
        )

        jenkins_btn = tk.Button(master, text="Jenkins Job", width=16, command=self._choose_jenkins)
        jenkins_btn.grid(row=1, column=0, padx=4, pady=4)

        github_pr_btn = tk.Button(master, text="GitHub PR", width=16, command=self._choose_github_pr)
        github_pr_btn.grid(row=1, column=1, padx=4, pady=4)

        github_actions_run_btn = tk.Button(
            master, text="GitHub Actions Run", width=16, command=self._choose_github_actions_run
        )
        github_actions_run_btn.grid(row=2, column=0, columnspan=2, padx=4, pady=4)

        self.buttonbox = lambda: None  # suppress the default OK/Cancel row
        return None

    def _choose_jenkins(self) -> None:
        self.result = "jenkins"
        self.destroy()

    def _choose_github_pr(self) -> None:
        self.result = "github_pr"
        self.destroy()

    def _choose_github_actions_run(self) -> None:
        self.result = "github_actions_run"
        self.destroy()


class AddJenkinsWatcherDialog(simpledialog.Dialog):
    """Modal dialog that asks for a Jenkins job URL and optional label."""

    def __init__(self, parent):
        """Initialize dialog state before building the modal body."""
        self.job_url_var = tk.StringVar()
        self.label_var = tk.StringVar()
        self.notes_text: Optional[tk.Text] = None
        self.result: Optional[Tuple[str, str, str]] = None
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

        tk.Label(master, text="Notes (optional):").grid(row=2, column=0, sticky="nw", padx=4, pady=4)
        self.notes_text = tk.Text(master, width=30, height=3, wrap="word")
        self.notes_text.grid(row=2, column=1, padx=4, pady=4)

        tk.Label(
            master,
            text="e.g. https://jenkins.example.com/job/my-job",
            fg="gray",
        ).grid(row=3, column=0, columnspan=2, sticky="w", padx=4)

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
        """Store the entered URL/label/notes as the dialog's result on accept."""
        notes = self.notes_text.get("1.0", "end").strip() if self.notes_text else ""
        self.result = (self.job_url_var.get().strip(), self.label_var.get().strip(), notes)


class AddGitHubPRWatcherDialog(simpledialog.Dialog):
    """Modal dialog that asks for a GitHub PR URL and optional label."""

    def __init__(self, parent):
        """Initialize dialog state before building the modal body."""
        self.pr_url_var = tk.StringVar()
        self.label_var = tk.StringVar()
        self.notes_text: Optional[tk.Text] = None
        self.result: Optional[Tuple[str, str, str]] = None
        super().__init__(parent, title="Add GitHub PR Watcher")

    def body(self, master):
        """
        Build the dialog's form fields and return the widget to focus initially.

        Returns:
            The Entry widget that should receive initial keyboard focus.
        """
        tk.Label(master, text="GitHub PR URL:").grid(row=0, column=0, sticky="w", padx=4, pady=4)
        url_entry = tk.Entry(master, textvariable=self.pr_url_var, width=44)
        url_entry.grid(row=0, column=1, padx=4, pady=4)

        tk.Label(master, text="Label (optional):").grid(row=1, column=0, sticky="w", padx=4, pady=4)
        tk.Entry(master, textvariable=self.label_var, width=44).grid(row=1, column=1, padx=4, pady=4)

        tk.Label(master, text="Notes (optional):").grid(row=2, column=0, sticky="nw", padx=4, pady=4)
        self.notes_text = tk.Text(master, width=34, height=3, wrap="word")
        self.notes_text.grid(row=2, column=1, padx=4, pady=4)

        tk.Label(
            master,
            text="e.g. https://github.com/owner/repo/pull/123\nUses your `gh` CLI login — run `gh auth login` first.",
            fg="gray",
            justify=tk.LEFT,
        ).grid(row=3, column=0, columnspan=2, sticky="w", padx=4)

        return url_entry  # initial focus

    def validate(self) -> bool:
        """
        Require a non-empty PR URL before the dialog can be accepted.

        Returns:
            True if the PR URL field is non-empty, False otherwise.
        """
        if not self.pr_url_var.get().strip():
            return False
        return True

    def apply(self) -> None:
        """Store the entered URL/label/notes as the dialog's result on accept."""
        notes = self.notes_text.get("1.0", "end").strip() if self.notes_text else ""
        self.result = (self.pr_url_var.get().strip(), self.label_var.get().strip(), notes)


class AddGitHubActionsRunWatcherDialog(simpledialog.Dialog):
    """Modal dialog that asks for a GitHub Actions run URL and optional label."""

    def __init__(self, parent):
        """Initialize dialog state before building the modal body."""
        self.run_url_var = tk.StringVar()
        self.label_var = tk.StringVar()
        self.notes_text: Optional[tk.Text] = None
        self.result: Optional[Tuple[str, str, str]] = None
        super().__init__(parent, title="Add GitHub Actions Run Watcher")

    def body(self, master):
        """
        Build the dialog's form fields and return the widget to focus initially.

        Returns:
            The Entry widget that should receive initial keyboard focus.
        """
        tk.Label(master, text="GitHub Actions run URL:").grid(row=0, column=0, sticky="w", padx=4, pady=4)
        url_entry = tk.Entry(master, textvariable=self.run_url_var, width=44)
        url_entry.grid(row=0, column=1, padx=4, pady=4)

        tk.Label(master, text="Label (optional):").grid(row=1, column=0, sticky="w", padx=4, pady=4)
        tk.Entry(master, textvariable=self.label_var, width=44).grid(row=1, column=1, padx=4, pady=4)

        tk.Label(master, text="Notes (optional):").grid(row=2, column=0, sticky="nw", padx=4, pady=4)
        self.notes_text = tk.Text(master, width=34, height=3, wrap="word")
        self.notes_text.grid(row=2, column=1, padx=4, pady=4)

        tk.Label(
            master,
            text=(
                "e.g. https://github.com/owner/repo/actions/runs/123456\n"
                "Uses your `gh` CLI login — run `gh auth login` first."
            ),
            fg="gray",
            justify=tk.LEFT,
        ).grid(row=3, column=0, columnspan=2, sticky="w", padx=4)

        return url_entry  # initial focus

    def validate(self) -> bool:
        """
        Require a non-empty run URL before the dialog can be accepted.

        Returns:
            True if the run URL field is non-empty, False otherwise.
        """
        if not self.run_url_var.get().strip():
            return False
        return True

    def apply(self) -> None:
        """Store the entered URL/label/notes as the dialog's result on accept."""
        notes = self.notes_text.get("1.0", "end").strip() if self.notes_text else ""
        self.result = (self.run_url_var.get().strip(), self.label_var.get().strip(), notes)


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
