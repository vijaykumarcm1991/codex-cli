"""Interactive REPL helpers: persistent history, fuzzy tab completion, editor key.

Uses the standard-library ``readline`` when available (Linux/macOS). Without
it the REPL still works; history and completion are simply disabled.
"""
from __future__ import annotations

import os
from pathlib import Path

try:
    import readline
except ImportError:  # pragma: no cover - Windows without readline
    readline = None  # type: ignore[assignment]

from .fuzzy import rank

COMMANDS = [
    "/help", "/new", "/model", "/models", "/context", "/usage", "/diff", "/undo",
    "/retry", "/compact", "/theme", "/title", "/sessions", "/copy", "/save",
    "/plan", "/fork", "/restore", "/mcp", "/export", "/editor", "/notify",
    "/fetch", "/exit", "/quit",
]


class Repl:
    """Input wrapper with persistent history, fuzzy slash-command, path, and @file completion."""

    def __init__(self, root: Path, history_path: Path, commands: list[str] | None = None):
        self.root = root
        self.commands = commands or COMMANDS
        self.history_path = history_path
        if readline is not None:
            try:
                history_path.parent.mkdir(parents=True, exist_ok=True)
                if history_path.exists():
                    readline.read_history_file(str(history_path))
                readline.set_history_length(5000)
                readline.parse_and_bind("tab: complete")
                readline.set_completer_delims(" \t\n")
                readline.set_completer(self._complete)
                # Ctrl-E opens $EDITOR with the "/editor" command (macro types it + Enter).
                readline.parse_and_bind('"\\C-e": "/editor\\n"')
            except OSError:
                pass

    def _complete(self, text: str, state: int):
        try:
            line = readline.get_line_buffer() if readline else ""
        except ValueError:
            return None
        if text.startswith("/") and " " not in line:
            matches = self._complete_commands(text)
        elif text.startswith("@"):
            matches = ["@" + match for match in self._complete_paths(text[1:], images_only=True)]
        else:
            matches = self._complete_paths(text)
        return matches[state] if state < len(matches) else None

    def _complete_commands(self, text: str) -> list[str]:
        """Exact prefix matches first; otherwise fuzzy subsequence matches."""
        matches = sorted(command for command in self.commands if command.startswith(text))
        if matches or len(text) < 2:
            return matches
        scored = rank(text, self.commands, limit=8)
        return [command for _score, command in scored]

    def _complete_paths(self, text: str, images_only: bool = False) -> list[str]:
        head, _, prefix = text.rpartition("/")
        directory = self.root / head if head else self.root
        try:
            entries = sorted(os.listdir(directory))
        except OSError:
            return []
        extensions = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"} if images_only else None
        matches = []
        for entry in entries:
            if not entry.startswith(prefix):
                continue
            if extensions is not None and Path(entry).suffix.lower() not in extensions:
                continue
            candidate = (head + "/" if head else "") + entry.replace(" ", "\\ ")
            if (directory / entry).is_dir():
                candidate += "/"
            matches.append(candidate)
            if len(matches) >= 64:
                break
        return matches

    def input(self, prompt: str) -> str:
        return input(prompt)

    def save(self) -> None:
        if readline is None or self.history_path is None:
            return
        try:
            self.history_path.parent.mkdir(parents=True, exist_ok=True)
            readline.write_history_file(str(self.history_path))
        except OSError:
            pass
