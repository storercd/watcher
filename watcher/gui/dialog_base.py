"""Shared dialog base that stays above a pinned (always-on-top) main window."""

from tkinter import simpledialog


class OnTopDialog(simpledialog.Dialog):
    def buttonbox(self):
        # Runs inside Dialog.__init__ before the modal wait; a transient
        # dialog otherwise stacks beneath a topmost parent on macOS.
        super().buttonbox()
        if self.master.winfo_toplevel().attributes("-topmost"):
            self.attributes("-topmost", True)
