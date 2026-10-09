"""Modal dialog for editing app-level settings (Away notifications, re-notify on restart)."""

from __future__ import annotations

import tkinter as tk
from watcher.gui.dialog_base import OnTopDialog
from typing import Optional, Tuple


class SettingsDialog(OnTopDialog):
    """Modal dialog that edits the ntfy server/topic and other app-level settings."""

    def __init__(self, parent, ntfy_server: str, ntfy_topic: str, renotify_on_restart: bool):
        """Initialize dialog state, pre-filling the current settings."""
        self.server_var = tk.StringVar(value=ntfy_server)
        self.topic_var = tk.StringVar(value=ntfy_topic)
        self.renotify_var = tk.BooleanVar(value=renotify_on_restart)
        self.result: Optional[Tuple[str, str, bool]] = None
        super().__init__(parent, title="Settings")

    def body(self, master):
        """
        Build the dialog's form fields and return the widget to focus initially.

        Returns:
            The Entry widget that should receive initial keyboard focus.
        """
        tk.Label(master, text="Away notifications (ntfy)", font=(None, 10, "bold")).grid(
            row=0, column=0, columnspan=2, sticky="w", padx=4, pady=(4, 8)
        )

        tk.Label(master, text="ntfy server:").grid(row=1, column=0, sticky="w", padx=4, pady=4)
        server_entry = tk.Entry(master, textvariable=self.server_var, width=36)
        server_entry.grid(row=1, column=1, padx=4, pady=4)

        tk.Label(master, text="ntfy topic:").grid(row=2, column=0, sticky="w", padx=4, pady=4)
        topic_entry = tk.Entry(master, textvariable=self.topic_var, width=36)
        topic_entry.grid(row=2, column=1, padx=4, pady=4)

        tk.Label(
            master,
            text=(
                "When in 'Away' mode, notifications are published to this\n"
                "ntfy topic. Subscribe to the same topic in the ntfy app\n"
                "(https://ntfy.sh) on your phone to receive them."
            ),
            fg="gray",
            justify=tk.LEFT,
        ).grid(row=3, column=0, columnspan=2, sticky="w", padx=4, pady=(8, 0))

        tk.Label(master, text="Notifications", font=(None, 10, "bold")).grid(
            row=4, column=0, columnspan=2, sticky="w", padx=4, pady=(16, 8)
        )

        tk.Checkbutton(
            master,
            text="Re-notify unacknowledged on restart",
            variable=self.renotify_var,
        ).grid(row=5, column=0, columnspan=2, sticky="w", padx=4, pady=4)

        tk.Label(
            master,
            text=(
                "When enabled, any result you already acknowledged in a\n"
                "prior session is treated as unacknowledged again on the\n"
                "next startup - re-highlighting its row and re-firing\n"
                "notifications - instead of staying silently acknowledged."
            ),
            fg="gray",
            justify=tk.LEFT,
        ).grid(row=6, column=0, columnspan=2, sticky="w", padx=4, pady=(0, 4))

        return topic_entry  # initial focus

    def validate(self) -> bool:
        """
        Require a non-empty ntfy server before the dialog can be accepted.

        Returns:
            True if the server field is non-empty, False otherwise.
        """
        if not self.server_var.get().strip():
            return False
        return True

    def apply(self) -> None:
        """Store the entered settings as the dialog's result on accept."""
        self.result = (
            self.server_var.get().strip(),
            self.topic_var.get().strip(),
            self.renotify_var.get(),
        )
