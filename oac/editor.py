"""Compose long prompts in $VISUAL/$EDITOR and pipe them back into oac."""
from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path


def _editor_command() -> str:
    for variable in ("OAC_EDITOR", "VISUAL", "EDITOR"):
        value = os.environ.get(variable)
        if value:
            return value
    return "notepad" if os.name == "nt" else "vi"


def edit_text(initial: str = "", suffix: str = ".md") -> str | None:
    """Open the editor pre-filled with ``initial``; return the saved text.

    Returns None when the editor cannot run or the user saves nothing new.
    """
    editor = _editor_command()
    handle, name = tempfile.mkstemp(prefix="oac-prompt-", suffix=suffix, text=True)
    path = Path(name)
    try:
        with os.fdopen(handle, "w") as stream:
            stream.write(initial)
        try:
            result = subprocess.run(f"{editor} {path}", shell=True, check=False, timeout=1800)
        except (OSError, subprocess.SubprocessError):
            return None
        if result.returncode != 0:
            return None
        text = path.read_text(errors="replace")
    finally:
        try:
            path.unlink()
        except OSError:
            pass
    if text.strip() and text.strip() != initial.strip():
        return text.rstrip("\n")
    return None
