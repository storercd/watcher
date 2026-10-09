"""Shared dialog base that stays above a pinned (always-on-top) main window."""

from tkinter import simpledialog


class OnTopDialog(simpledialog.Dialog):
    def buttonbox(self):
        super().buttonbox()
        if self.master.winfo_toplevel().attributes("-topmost"):
            # Dialog.__init__ blocks in a modal wait right after this, and a
            # level set before the window is mapped is dropped on macOS, so
            # apply it once the dialog is actually visible.
            self.after(50, self._raise_above_parent)

    def _raise_above_parent(self):
        self.wm_transient("")
        self.attributes("-topmost", False)
        self.attributes("-topmost", True)
        self.lift()
