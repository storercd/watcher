"""Shared dialog base that stays above a pinned (always-on-top) main window."""

from tkinter import simpledialog


class OnTopDialog(simpledialog.Dialog):
    """A modal dialog that floats above its parent when the parent is pinned."""

    def buttonbox(self):
        """Build the buttons, then schedule raising the dialog if the parent is topmost."""
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
