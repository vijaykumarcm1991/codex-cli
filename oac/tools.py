from __future__ import annotations

import difflib
import json
import os
import signal
import subprocess
import tempfile
from pathlib import Path
from typing import Callable


def function(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {"type": "function", "function": {"name": name, "description": description, "parameters": {"type": "object", "properties": properties, "required": required, "additionalProperties": False}}}


STRING = {"type": "string"}
SCHEMAS = [
    function("read_file", "Read a UTF-8 project file, with optional line range.", {"path": STRING, "start_line": {"type": "integer"}, "end_line": {"type": "integer"}}, ["path"]),
    function("list_files", "List project files, excluding Git metadata, dependencies, caches, and secrets.", {"path": STRING}, []),
    function("search", "Search project text files for a literal string.", {"query": STRING, "path": STRING}, ["query"]),
    function("write_file", "Create or replace a UTF-8 file. Shows a diff and requires approval.", {"path": STRING, "content": STRING}, ["path", "content"]),
    function("edit_file", "Replace an exact, unique substring in a file. Requires approval.", {"path": STRING, "old": STRING, "new": STRING}, ["path", "old", "new"]),
    function("shell", "Execute a shell command in the project. Requires approval; can access the host outside the project.", {"command": STRING, "timeout": {"type": "integer"}}, ["command"]),
]
SKIP = {".git", ".venv", "node_modules", "__pycache__", ".oac", ".aws", ".ssh", ".codex"}


class Tools:
    def __init__(self, root: Path, approval: str = "ask", approve: Callable[[str], bool] | None = None):
        self.root = root.resolve()
        self.approval = approval
        self.approve = approve or (lambda description: False)

    def path(self, value: str = ".") -> Path:
        path = (self.root / value).resolve()
        if not path.is_relative_to(self.root):
            raise ValueError("Path escapes project root")
        parts = path.relative_to(self.root).parts
        if any(part in SKIP or part == ".env" or part.startswith(".env.") or part.endswith((".pem", ".key")) for part in parts):
            raise ValueError("Path is protected (metadata, dependencies, or potential secrets)")
        return path

    def allowed(self, description: str) -> bool:
        return self.approval == "auto" or (self.approval == "ask" and self.approve(description))

    def files(self, base: Path):
        for directory, dirs, names in os.walk(base, followlinks=False):
            dirs[:] = sorted(d for d in dirs if d not in SKIP and not d.startswith(".env") and not (Path(directory) / d).is_symlink())
            for name in sorted(names):
                candidate = Path(directory) / name
                if candidate.is_symlink():
                    continue
                try:
                    yield self.path(str(candidate))
                except ValueError:
                    continue

    def execute(self, name: str, arguments: str) -> str:
        try:
            args = json.loads(arguments)
            if not isinstance(args, dict):
                raise ValueError("Tool arguments must be an object")
            result = self._execute(name, args)
            return result[:24000] + ("\n[output truncated]" if len(result) > 24000 else "")
        except (ValueError, TypeError, KeyError, OSError, UnicodeError) as exc:
            return f"Tool error: {exc}"

    def _execute(self, name: str, args: dict) -> str:
        if name == "read_file":
            path = self.path(args["path"])
            if path.stat().st_size > 2_000_000:
                raise ValueError("File exceeds 2 MB; use shell with approval for larger files")
            lines = path.read_text().splitlines()
            start = max(1, int(args.get("start_line", 1)))
            end = int(args.get("end_line", start + 399))
            text = "\n".join(f"{i + 1}: {line}" for i, line in enumerate(lines) if start - 1 <= i < end)
            return text or "[empty file or line range]"
        if name in {"list_files", "search"}:
            base = self.path(args.get("path", "."))
            paths = [base] if base.is_file() else self.files(base)
            hits = []
            for path in paths:
                relative = str(path.relative_to(self.root))
                if name == "list_files":
                    hits.append(relative)
                elif path.stat().st_size <= 1_000_000:
                    try:
                        content = path.read_text()
                    except (UnicodeError, OSError):
                        continue
                    for line, text in enumerate(content.splitlines(), 1):
                        if args["query"] in text:
                            hits.append(f"{relative}:{line}: {text[:1000]}")
                        if len(hits) >= 300:
                            break
                if len(hits) >= 300:
                    return "\n".join(hits) + "\n[limited to 300 results]"
            return "\n".join(hits) or "[no matches]"
        if name in {"write_file", "edit_file"}:
            path = self.path(args["path"])
            if path.exists() and path.stat().st_size > 2_000_000:
                raise ValueError("File exceeds 2 MB; use an approved shell command")
            before = path.read_text() if path.exists() else ""
            if name == "edit_file":
                if not args["old"] or before.count(args["old"]) != 1:
                    raise ValueError("old text must match exactly once; read the file before editing")
                after = before.replace(args["old"], args["new"], 1)
            else:
                after = args["content"]
            diff = "".join(difflib.unified_diff(before.splitlines(True), after.splitlines(True), fromfile=str(path.relative_to(self.root)), tofile=str(path.relative_to(self.root))))
            if not self.allowed(f"{name}: {path.relative_to(self.root)}\n{diff}"):
                return "Denied by approval policy. Do not retry this action without user instruction."
            # Reject edits if a user or external process changed the file during review.
            if (path.read_text() if path.exists() else "") != before:
                raise ValueError("File changed during approval; read it again")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(after)
            return f"Updated {path.relative_to(self.root)}"
        if name == "shell":
            command = args["command"]
            timeout = min(300, max(1, int(args.get("timeout", 60))))
            if not self.allowed(f"Run shell command in {self.root}:\n{command}"):
                return "Denied by approval policy. Do not retry this action without user instruction."
            # Spool output to disk to avoid unbounded memory; terminate the process group on timeout.
            with tempfile.TemporaryFile() as output:
                proc = subprocess.Popen(command, shell=True, cwd=self.root, stdout=output, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True)
                try:
                    status = proc.wait(timeout=timeout)
                    suffix = f"\n[exit {status}]"
                except subprocess.TimeoutExpired:
                    os.killpg(proc.pid, signal.SIGKILL)
                    proc.wait()
                    suffix = "\n[command terminated: timeout]"
                except KeyboardInterrupt:
                    os.killpg(proc.pid, signal.SIGKILL)
                    proc.wait()
                    raise
                output.seek(0)
                captured = output.read(20001)
                return captured[:20000].decode(errors="replace") + ("\n[output truncated]" if len(captured) > 20000 else "") + suffix
        raise ValueError(f"Unknown tool: {name}")
