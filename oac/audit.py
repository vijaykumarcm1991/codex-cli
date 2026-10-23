"""Project-local JSONL audit log: one line per agent tool call."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path


class AuditLog:
    """Appends ``.oac/audit.jsonl`` entries; safe to disable or missing directories."""

    def __init__(self, root: Path, session_id: str = "-", enabled: bool = True):
        self.path = Path(root).resolve() / ".oac" / "audit.jsonl"
        self.session_id = session_id
        self.enabled = enabled

    def log(self, tool: str, args: dict, result: str, approved: bool = True) -> None:
        if not self.enabled:
            return
        try:
            payload = {"time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                       "session": self.session_id, "tool": tool, "ok": approved and not str(result).startswith(("Tool error", "Denied by approval")),
                       "args": {key: (value if len(str(value)) < 400 else str(value)[:400] + "…") for key, value in (args or {}).items()},
                       "result": str(result)[:600]}
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a") as stream:
                stream.write(json.dumps(payload, ensure_ascii=False) + "\n")
        except OSError:
            pass  # auditing must never break the agent loop

    @staticmethod
    def tail(path: Path, count: int = 20) -> list[dict]:
        if not path.is_file():
            return []
        try:
            lines = path.read_text(errors="replace").splitlines()
        except OSError:
            return []
        entries = []
        for line in lines[-max(1, count):]:
            try:
                entries.append(json.loads(line))
            except ValueError:
                continue
        return entries
