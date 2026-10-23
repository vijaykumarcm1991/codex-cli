from __future__ import annotations

import difflib
import json
import os
import re
import signal
import subprocess
import tempfile
from pathlib import Path
from typing import Callable

from . import asttools
from .audit import AuditLog
from .restore import RestorePoints
from .webtools import fetch as web_fetch


def function(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {"type": "function", "function": {"name": name, "description": description, "parameters": {"type": "object", "properties": properties, "required": required, "additionalProperties": False}}}


STRING = {"type": "string"}
SCHEMAS = [
    function("read_file", "Read a UTF-8 project file, with optional line range.", {"path": STRING, "start_line": {"type": "integer"}, "end_line": {"type": "integer"}}, ["path"]),
    function("list_files", "List project files, excluding Git metadata, dependencies, caches, and secrets.", {"path": STRING}, []),
    function("search", "Search project text files for a literal string.", {"query": STRING, "path": STRING}, ["query"]),
    function("find_files", "Find project files matching a glob pattern such as '**/*.py' or 'src/**/*.ts'.", {"pattern": STRING, "path": STRING}, ["pattern"]),
    function("regex_search", "Search project text files with a Python regular expression (case-sensitive).", {"pattern": STRING, "path": STRING, "max_results": {"type": "integer"}}, ["pattern"]),
    function("write_file", "Create or replace a UTF-8 file. Shows a diff and requires approval.", {"path": STRING, "content": STRING}, ["path", "content"]),
    function("edit_file", "Replace an exact, unique substring in a file. Requires approval.", {"path": STRING, "old": STRING, "new": STRING}, ["path", "old", "new"]),
    function("shell", "Execute a shell command in the project. Requires approval; can access the host outside the project.", {"command": STRING, "timeout": {"type": "integer"}}, ["command"]),
    function("run_tests", f"Run the project's configured test command ({{test_cmd}}) and return the tail of its output plus the exit status. Auto-approved for safe extra arguments only.", {"args": {"type": "string", "description": "Extra arguments, e.g. 'tests/test_api.py' or '-k login'"}, "timeout": {"type": "integer"}}, []),
    function("web_fetch", "Fetch an http(s) URL and return its readable text (HTML is stripped to plain text). Read-only; useful for docs, API references, and error lookups.", {"url": STRING, "max_chars": {"type": "integer", "description": "Maximum characters to return (default 24000)"}}, ["url"]),
    function("list_symbols", "List classes and functions in a Python file with their line ranges.", {"path": STRING}, ["path"]),
    function("find_references", "Find all non-attribute occurrences of a Python identifier in one file, with line numbers and whether each is a definition.", {"path": STRING, "name": STRING}, ["path", "name"]),
    function("rename_symbol", "Rename a Python symbol (definitions and plain references, never obj.attr accesses) across one file. Requires approval.", {"path": STRING, "old": STRING, "new": STRING}, ["path", "old", "new"]),
]
# Extra arguments run_tests may append to the configured command (kept strict).
SAFE_TEST_ARGS = re.compile(r"^[A-Za-z0-9_@./:+=,-]+$")
SKIP = {".git", ".venv", "node_modules", "__pycache__", ".oac", ".aws", ".ssh", ".codex"}


class Tools:
    def __init__(self, root: Path, approval: str = "ask", approve: Callable[[str], bool] | None = None,
                 test_cmd: str = "python -m pytest -x -q", allow: list[str] | None = None,
                 audit: AuditLog | None = None, restore: RestorePoints | None = None):
        self.root = root.resolve()
        self.approval = approval
        self.approve = approve or (lambda description: False)
        self.test_cmd = test_cmd or "python -m pytest -x -q"
        self.allow = [item.strip() for item in (allow or []) if item.strip()]
        self.audit = audit
        self.restore = restore or RestorePoints(self.root)
        # Undo journal: every accepted file mutation is recorded as (path, before, after).
        self.journal: list[dict] = []

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
            result = result[:24000] + ("\n[output truncated]" if len(result) > 24000 else "")
        except (ValueError, TypeError, KeyError, OSError, UnicodeError) as exc:
            result = f"Tool error: {exc}"
        if self.audit is not None:
            self.audit.log(name, args if isinstance(args, dict) else {}, result)
        return result

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
        if name == "find_files":
            base = self.path(args.get("path", "."))
            pattern = args["pattern"]
            if pattern.startswith(("/", "\\")):
                raise ValueError("Pattern must be relative to the project root")
            try:
                matches = base.glob(pattern)
            except (ValueError, NotImplementedError) as exc:
                raise ValueError(f"Invalid glob pattern: {exc}") from None
            hits = []
            for path in sorted(matches):
                try:
                    safe = self.path(str(path))
                except ValueError:  # escapes root, protected, or symlinked out
                    continue
                relative = str(safe.relative_to(self.root))
                hits.append(relative + ("/" if safe.is_dir() else ""))
                if len(hits) >= 300:
                    break
            return "\n".join(hits) + "\n[limited to 300 results]" if len(hits) >= 300 else ("\n".join(hits) or "[no matches]")
        if name == "regex_search":
            try:
                regex = re.compile(args["pattern"])
            except re.error as exc:
                raise ValueError(f"Invalid regular expression: {exc}") from None
            base = self.path(args.get("path", "."))
            limit = min(300, max(1, int(args.get("max_results", 100))))
            hits = []
            paths = [base] if base.is_file() else self.files(base)
            for path in paths:
                if path.stat().st_size > 1_000_000:
                    continue
                try:
                    content = path.read_text()
                except (UnicodeError, OSError):
                    continue
                relative = str(path.relative_to(self.root))
                for line, text in enumerate(content.splitlines(), 1):
                    if regex.search(text):
                        hits.append(f"{relative}:{line}: {text[:1000]}")
                        if len(hits) >= limit:
                            return "\n".join(hits) + f"\n[limited to {limit} results]"
            return "\n".join(hits) or "[no matches]"
        if name == "run_tests":
            extra = str(args.get("args", "")).strip()
            if extra and not SAFE_TEST_ARGS.fullmatch(extra):
                raise ValueError("run_tests args may only contain safe test selectors (letters, digits, ./:-_=+,)")
            timeout = min(300, max(1, int(args.get("timeout", 180))))
            if self.approval == "read-only":
                return "Denied by approval policy. Do not retry this action without user instruction."
            return self._run_command(f"{self.test_cmd} {extra}".strip(), timeout)
        if name == "web_fetch":
            return web_fetch(args["url"], int(args.get("max_chars", 24000)))
        if name == "list_symbols":
            path = self.path(args["path"])
            if path.suffix != ".py":
                raise ValueError("list_symbols works on Python files only")
            symbols = asttools.list_symbols(path.read_text())
            return "\n".join(f"{s['line']}-{s['end_line']} {s['kind']} {s['name']}" for s in symbols) or "[no symbols]"
        if name == "find_references":
            path = self.path(args["path"])
            if path.suffix != ".py":
                raise ValueError("find_references works on Python files only")
            references = asttools.find_references(path.read_text(), args["name"], str(path.relative_to(self.root)))
            return "\n".join(f"{r['path']}:{r['line']}{':def' if r['definition'] else ''}: {r['text']}" for r in references) or "[no references]"
        if name == "rename_symbol":
            path = self.path(args["path"])
            before = path.read_text()
            after, count = asttools.rename_symbol(before, args["old"], args["new"])
            diff = "".join(difflib.unified_diff(before.splitlines(True), after.splitlines(True),
                                                fromfile=str(path.relative_to(self.root)), tofile=str(path.relative_to(self.root))))
            if not self.allowed(f"rename_symbol: {args['old']} -> {args['new']} in {path.relative_to(self.root)} ({count} sites)\n{diff}"):
                return "Denied by approval policy. Do not retry this action without user instruction."
            if path.read_text() != before:
                raise ValueError("File changed during approval; read it again")
            self.restore.capture([path], f"rename_symbol {args['old']}->{args['new']}")
            path.write_text(after)
            self.journal.append({"path": path, "before": before, "after": after})
            return f"Renamed {args['old']} to {args['new']} at {count} sites in {path.relative_to(self.root)}"
        if name in {"write_file", "edit_file"}:
            path = self.path(args["path"])
            if path.exists() and path.stat().st_size > 2_000_000:
                raise ValueError("File exceeds 2 MB; use an approved shell command")
            before = path.read_text() if path.exists() else None
            if name == "edit_file":
                if not args["old"] or (before or "").count(args["old"]) != 1:
                    raise ValueError("old text must match exactly once; read the file before editing")
                after = (before or "").replace(args["old"], args["new"], 1)
            else:
                after = args["content"]
            previous = before or ""
            diff = "".join(difflib.unified_diff(previous.splitlines(True), after.splitlines(True), fromfile=str(path.relative_to(self.root)), tofile=str(path.relative_to(self.root))))
            if not self.allowed(f"{name}: {path.relative_to(self.root)}\n{diff}"):
                return "Denied by approval policy. Do not retry this action without user instruction."
            # Reject edits if a user or external process changed the file during review.
            if (path.read_text() if path.exists() else "") != previous:
                raise ValueError("File changed during approval; read it again")
            path.parent.mkdir(parents=True, exist_ok=True)
            self.restore.capture([path], f"{name}: {path.relative_to(self.root)}")
            path.write_text(after)
            self.journal.append({"path": path, "before": before, "after": after})
            return f"Updated {path.relative_to(self.root)}"
        if name == "shell":
            command = args["command"]
            timeout = min(300, max(1, int(args.get("timeout", 60))))
            if self.allowlisted(command):
                pass  # prefix allowlisted via --allow or config; no prompt needed
            elif not self.allowed(f"Run shell command in {self.root}:\n{command}"):
                return "Denied by approval policy. Do not retry this action without user instruction."
            return self._run_command(command, timeout)
        raise ValueError(f"Unknown tool: {name}")

    def allowlisted(self, command: str) -> bool:
        """True when the command's leading words match a configured allow prefix."""
        words = command.split()
        for prefix in self.allow:
            prefix_words = prefix.split()
            if words[:len(prefix_words)] == prefix_words:
                return True
        return False

    def _run_command(self, command: str, timeout: int) -> str:
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

    def undo(self) -> str:
        """Revert the most recent journaled file mutation, if the file is unchanged since."""
        if not self.journal:
            return "Nothing to undo: no agent file changes recorded in this session."
        entry = self.journal.pop()
        path: Path = entry["path"]
        current = path.read_text() if path.exists() else None
        if current != entry["after"]:
            return f"Skipped undo: {path.relative_to(self.root)} changed since the agent wrote it."
        if entry["before"] is None:
            path.unlink()
            return f"Deleted {path.relative_to(self.root)} (the agent created it)."
        path.write_text(entry["before"])
        return f"Reverted {path.relative_to(self.root)} to its previous content."
