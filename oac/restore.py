"""Persistent restore points: snapshots taken before every agent file mutation.

Checkpoints live in ``<project>/.oac/restore/`` (excluded from agent file
access), so ``/restore`` can roll files back even across sessions.
"""
from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

MAX_CHECKPOINTS = 100


class RestorePoints:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.base = self.root / ".oac" / "restore"

    def _next_id(self) -> int:
        if not self.base.is_dir():
            return 1
        highest = 0
        for entry in self.base.iterdir():
            if entry.is_dir() and entry.name.isdigit():
                highest = max(highest, int(entry.name))
        return highest + 1

    def capture(self, paths: list[Path], label: str) -> str:
        """Snapshot the current state of ``paths``; returns the checkpoint id."""
        checkpoint = str(self._next_id())
        directory = self.base / checkpoint
        directory.mkdir(parents=True, exist_ok=True)
        files: dict[str, str] = {}
        for path in paths:
            path = Path(path).resolve()
            try:
                relative = str(path.relative_to(self.root))
            except ValueError:
                continue  # outside the project; nothing to snapshot
            if path.exists() and path.is_file():
                target = directory / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, target)
                files[relative] = "saved"
            else:
                files[relative] = "created"  # the mutation will create this file
        manifest = {"id": checkpoint, "label": label[:160],
                    "time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "files": files}
        (directory / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
        self._prune()
        return checkpoint

    def _prune(self) -> None:
        if not self.base.is_dir():
            return
        entries = sorted((e for e in self.base.iterdir() if e.is_dir() and e.name.isdigit()),
                         key=lambda e: int(e.name))
        while len(entries) > MAX_CHECKPOINTS:
            shutil.rmtree(entries.pop(0), ignore_errors=True)

    def list(self) -> list[dict]:
        if not self.base.is_dir():
            return []
        result = []
        for entry in sorted((e for e in self.base.iterdir() if e.is_dir() and e.name.isdigit()),
                            key=lambda e: int(e.name), reverse=True):
            manifest = entry / "manifest.json"
            try:
                data = json.loads(manifest.read_text())
            except (ValueError, OSError):
                continue
            result.append({"id": data.get("id", entry.name), "time": data.get("time", "?"),
                           "label": data.get("label", ""), "files": data.get("files", {})})
        return result

    def restore(self, checkpoint: str, only: str | None = None) -> list[str]:
        """Restore one checkpoint (or a single file of it); returns action descriptions."""
        directory = self.base / checkpoint
        if not checkpoint.isdigit() or not directory.is_dir():
            raise ValueError(f"No restore point {checkpoint!r}")
        manifest_path = directory / "manifest.json"
        try:
            manifest = json.loads(manifest_path.read_text())
        except (ValueError, OSError) as exc:
            raise ValueError(f"Restore point {checkpoint} is unreadable: {exc}") from None
        actions: list[str] = []
        for relative, state in sorted(manifest.get("files", {}).items()):
            if only and only not in {relative, Path(relative).name}:
                continue
            source = directory / relative
            target = self.root / relative
            if state == "created":
                if target.exists():
                    target.unlink()
                    actions.append(f"deleted {relative} (the agent created it)")
                else:
                    actions.append(f"{relative} already absent")
            elif source.is_file():
                if target.exists() and target.read_text(errors="replace") == source.read_text(errors="replace"):
                    actions.append(f"{relative} already matches the snapshot")
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
                actions.append(f"restored {relative}")
            else:
                actions.append(f"snapshot for {relative} is missing; skipped")
        if not actions:
            raise ValueError(f"Nothing to restore for {only or checkpoint!r}")
        return actions
