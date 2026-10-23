from __future__ import annotations

import argparse
import base64
import contextlib
import json
import re
import subprocess
import sys
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from . import __version__, exports, stats
from .agent import Agent, Session, estimate
from .api import APIError, Client
from .audit import AuditLog
from .config import PRESETS, Config, config_path, state_path
from .editor import edit_text
from .history import Repl
from .mcp import McpError, McpRegistry
from .notify import notify_done
from .plans import PLAN_INSTRUCTIONS, PlanState, parse_plan
from .restore import RestorePoints
from .tools import SCHEMAS, Tools
from .ui import DEFAULT_THEME, THEMES, UI, Spinner, theme_list_line
from .usage import format_cost
from .webtools import fetch as web_fetch

HELP_ROWS = [
    ("/help", "Show this command reference"),
    ("/new", "Start a fresh session in this project"),
    ("/model ID", "Switch the active model"),
    ("/models", "List models available on the API"),
    ("/context", "Show context budget with a usage bar"),
    ("/usage", "Show token usage, requests, and estimated cost"),
    ("/undo", "Revert the agent's last file change"),
    ("/retry", "Re-run the previous prompt"),
    ("/diff [--side]", "Show Git changes; --side renders two columns"),
    ("/compact", "Keep only the latest turn in context"),
    ("/plan [approve|next|skip|abort]", "Plan-first mode: draft, review, execute stepwise"),
    ("/fork [N]", "Branch this session from user turn N"),
    ("/restore [ID [FILE]]", "Roll files back to a saved restore point"),
    ("/mcp", "Show MCP server and tool status"),
    ("/editor", "Compose the next prompt in $EDITOR (also Ctrl-E)"),
    ("/export [PATH]", "Save the transcript as styled HTML"),
    ("/notify [off|bell|desktop]", "Completion alerts for long tasks"),
    ("/fetch URL", "Fetch a web page as readable text"),
    ("/theme [name]", "Show or switch the color theme"),
    ("/title TEXT", "Set this session's title"),
    ("/sessions", "List sessions for this project"),
    ("/copy", "Copy the last answer to the clipboard (OSC52)"),
    ("/save [PATH]", "Save the transcript as Markdown"),
    ("/exit", "Leave the REPL (also Ctrl-D)"),
]

IMAGE_AT = re.compile(r"(?<!\S)@(\S+)", re.IGNORECASE)
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}


SUBCOMMANDS = {"init", "config", "profiles", "sessions", "theme", "fetch", "search",
               "stats", "audit", "mcp", "models", "doctor", "run", "chat"}
VERSION_FLAGS = {"-h", "--help", "--version"}
GLOBAL_FLAGS = ("--no-color",)
GLOBAL_VALUE_FLAGS = ("--profile",)


def _with_default_command(argv: list[str]) -> list[str]:
    """Make chat the default command.

    Bare `oac` opens the REPL, `oac TEXT...` is shorthand for `oac chat TEXT...`,
    and `oac --no-color` (no subcommand) still opens chat — while real subcommands
    (`oac models`, `oac run ...`, ...) are left untouched.
    """
    if not argv:
        return ["chat"]
    if not argv[0].startswith("-"):
        return argv if argv[0] in SUBCOMMANDS else ["chat", *argv]
    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok in VERSION_FLAGS:
            return argv  # argparse prints help/version and exits before dispatch.
        name = tok.split("=", 1)[0]
        if any(name == f or (len(name) > 2 and f.startswith(name)) for f in GLOBAL_FLAGS):
            i += 1
        elif any(name == f or (len(name) > 2 and f.startswith(name)) for f in GLOBAL_VALUE_FLAGS):
            i += 1 if "=" in tok else 2
        else:
            break
    if i == len(argv):
        return [*argv, "chat"]
    if argv[i] in SUBCOMMANDS:
        return argv
    return argv[:i] + ["chat"] + argv[i:]


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="oac",
        description="A modern terminal coding agent for OpenAI-compatible APIs",
        epilog="Running `oac` with no arguments starts an interactive chat; `oac TEXT...` is "
               "shorthand for `oac chat TEXT...`.")
    p.add_argument("--version", action="version", version=__version__)
    p.add_argument("--no-color", action="store_true", help="Disable colors, markdown, and animations")
    p.add_argument("--profile", metavar="NAME", help="Use a saved connection profile (or set OAC_PROFILE)")
    sub = p.add_subparsers(dest="command", required=False)
    init = sub.add_parser("init", help="Configure an API; keys are read from an environment variable")
    init.add_argument("--base-url", default="https://api.openai.com/v1")
    init.add_argument("--api-key-env", default="OPENAI_API_KEY")
    init.add_argument("--model", default="auto")
    init.add_argument("--api-mode", choices=["chat", "responses"], default="chat")
    init.add_argument("--context-window", type=int)
    init.add_argument("--preset", choices=sorted(PRESETS), help="Local-server endpoint presets (Ollama, LM Studio, ...)")
    init.add_argument("--save-as", metavar="NAME", help="Also store these settings as a named profile")
    sub.add_parser("config", help="Show configuration (no secret values)")
    sub.add_parser("profiles", help="List saved connection profiles")
    sessions = sub.add_parser("sessions", help="List, inspect, delete, or export saved sessions")
    sessions.add_argument("--json", action="store_true")
    sessions.add_argument("--delete", metavar="ID", help="Delete one session by ID")
    sessions.add_argument("--delete-all", action="store_true", help="Delete every saved session")
    sessions.add_argument("--export", metavar="ID", help="Export one session as a standalone HTML file")
    sessions.add_argument("--out", metavar="PATH", help="Output path for --export (default oac-<ID>.html)")
    theme_cmd = sub.add_parser("theme", help="Show or set the color theme")
    theme_cmd.add_argument("name", nargs="?")
    theme_cmd.add_argument("--list", action="store_true")
    fetch_cmd = sub.add_parser("fetch", help="Fetch a URL and print readable text")
    fetch_cmd.add_argument("url")
    fetch_cmd.add_argument("--max-chars", type=int, default=8000)
    search_cmd = sub.add_parser("search", help="Search across saved sessions with a regular expression")
    search_cmd.add_argument("pattern")
    search_cmd.add_argument("--limit", type=int, default=40)
    search_cmd.add_argument("--project", type=Path, help="Restrict the search to one project directory")
    stats_cmd = sub.add_parser("stats", help="Show token and cost usage over time")
    stats_cmd.add_argument("--days", type=int)
    stats_cmd.add_argument("--json", action="store_true")
    audit_cmd = sub.add_parser("audit", help="Show the project tool-call audit log")
    audit_cmd.add_argument("--cwd", type=Path, default=Path.cwd())
    audit_cmd.add_argument("--tail", type=int, default=20)
    audit_cmd.add_argument("--json", action="store_true")
    mcp_cmd = sub.add_parser("mcp", help="List configured MCP servers and their tools")
    mcp_cmd.add_argument("--test", metavar="NAME", help="Start one server and list its tools")
    for name in ("models", "doctor", "run", "chat"):
        cmd = sub.add_parser(name)
        cmd.add_argument("--base-url")
        cmd.add_argument("--model")
        cmd.add_argument("--api-mode", choices=["chat", "responses"])
        cmd.add_argument("--context-window", type=int)
        if name == "models":
            cmd.add_argument("--json", action="store_true")
        if name in {"run", "chat"}:
            cmd.add_argument("prompt", nargs="*", help="Prompt; run also accepts stdin; @file.png attaches images")
            cmd.add_argument("--cwd", type=Path, default=Path.cwd())
            cmd.add_argument("--approval", choices=["ask", "read-only", "auto"], default="ask", help="auto grants unrestricted shell execution; this is not an OS sandbox")
            cmd.add_argument("--allow", metavar="CMDS", help="Comma-separated shell command prefixes to auto-approve (e.g. 'git status,pytest')")
            cmd.add_argument("--image", action="append", default=[], metavar="PATH", help="Attach an image (repeatable; vision models)")
            cmd.add_argument("--resume", metavar="SESSION_ID")
            cmd.add_argument("--continue", dest="continue_session", action="store_true", help="Resume the latest session in this project")
            cmd.add_argument("--max-steps", type=int)
            cmd.add_argument("--max-output-tokens", type=int)
            cmd.add_argument("--no-stream", action="store_true")
            cmd.add_argument("--no-instructions", action="store_true", help="Do not auto-load AGENTS.md instructions")
            cmd.add_argument("--json", action="store_true", help="Emit final answer and session ID as JSON (no token streaming)")
            cmd.add_argument("--theme", choices=sorted(THEMES), help="Color theme for this run")
            cmd.add_argument("--no-render", action="store_true", help="Print raw token stream instead of rendered Markdown")
            if name == "run":
                cmd.add_argument("--plan", action="store_true", help="Draft a numbered plan instead of executing; prints it and exits")
    return p


def status(text: str) -> None:
    print(text, file=sys.stderr, flush=True)


def approve(description: str) -> bool:
    # Never consume piped prompts as approval, or accept EOF as consent.
    if not sys.stdin.isatty():
        status("Denied: interactive approval requires a terminal. Use --approval auto only for trusted work.")
        return False
    status("\n" + description)
    try:
        print("Allow? [y/N] ", end="", file=sys.stderr, flush=True)
        return input().strip().lower() in {"y", "yes"}
    except EOFError:
        return False


def describe_args(name: str, args: dict) -> str:
    for key in ("command", "pattern", "query", "path", "url", "old", "prompts"):
        value = args.get(key)
        if value:
            return str(value)[:64]
    return ""


def extract_images(prompt: str, root: Path) -> tuple[str, list[Path]]:
    """Pull @file.png tokens out of a prompt; returns (clean prompt, image paths)."""
    images: list[Path] = []

    def replace(match: re.Match) -> str:
        candidate = Path(match.group(1))
        if candidate.suffix.lower() not in IMAGE_EXTENSIONS:
            return match.group(0)
        resolved = candidate if candidate.is_absolute() else (root / candidate)
        if not resolved.is_file():
            raise ValueError(f"@{match.group(1)}: image file not found")
        images.append(resolved)
        return ""

    cleaned = re.sub(r"[ \t]+", " ", IMAGE_AT.sub(replace, prompt)).strip()
    return cleaned, images[:4]


class Runner:
    """Glue between the agent loop and the UI: spinner, streaming, rendering.

    In rendered mode, markdown is streamed paragraph-by-paragraph: completed
    blocks (split on blank lines outside code fences) print as they finish,
    so long answers render live without flicker or repainting.
    """

    def __init__(self, ui: UI, render: bool, quiet: bool):
        self.ui = ui
        self.render = render
        self.quiet = quiet
        self.spinner = Spinner(ui, "thinking")
        if quiet:
            self.spinner.enabled = False
        self.chars = 0
        self.buffer = ""
        self.flushed = 0

    def start(self) -> None:
        self.buffer, self.flushed = "", 0
        self.spinner.start("thinking")

    def stop(self) -> None:
        self.spinner.stop()

    @staticmethod
    def _safe_split(text: str) -> int:
        """Length of a prefix safe to render now (ends at a block boundary)."""
        lines = text.split("\n")
        fences, position, best, last_newline = 0, 0, 0, 0
        for index, line in enumerate(lines):
            if line.strip().startswith("```"):
                fences += 1
            position += len(line) + (1 if index < len(lines) - 1 else 0)
            even = fences % 2 == 0
            if even:
                last_newline = position
                if index < len(lines) - 1 and (line.strip() == "" or line.strip().startswith("```")):
                    best = position
        if not best and len(text) > 4000 and last_newline:
            return last_newline  # very long paragraph: flush at the last safe line
        return best

    def _print_block(self, text: str) -> None:
        rendered = self.ui.md(text)
        if rendered:
            self.spinner.stop()
            print(rendered)
            self.spinner.start("thinking")

    def emit(self, text: str) -> None:
        if self.quiet:
            return
        if self.render:
            self.buffer += text
            self.chars += len(text)
            if self.spinner._thread:
                self.spinner.tick(f"{self.chars:,} characters in")
            split = self._safe_split(self.buffer[self.flushed:])
            if split > 0:
                chunk = self.buffer[self.flushed:self.flushed + split]
                self.flushed += split
                self._print_block(chunk)
        else:
            self.spinner.stop()
            print(text, end="", flush=True)

    def status(self, text: str) -> None:
        self.spinner.stop()
        print(self.ui.s(f"· {text}", dim=True), file=sys.stderr, flush=True)
        self.spinner.start("thinking")

    def on_tool(self, name: str, args: dict, elapsed: float, error: bool) -> None:
        self.spinner.stop()
        ui, theme = self.ui, self.ui.theme
        if error:
            color = theme.error
        elif name in {"write_file", "edit_file", "rename_symbol"}:
            color = theme.warning
        elif name.startswith("mcp__") or name in {"shell", "run_tests", "spawn_agents"}:
            color = theme.primary
        else:
            color = theme.accent
        glyph = "✗" if error else "▸"
        parts = [ui.s(glyph, color=color, bold=True), " ", ui.s(name, bold=error)]
        detail = describe_args(name, args)
        if detail:
            parts += ["  ", ui.s(detail, dim=True)]
        if elapsed >= 0.05:
            parts += [" ", ui.s(f"({elapsed:.1f}s)", dim=True)]
        print("".join(parts), file=sys.stderr, flush=True)
        self.spinner.start("thinking")

    def finish(self, answer: str) -> None:
        self.spinner.stop()
        if self.quiet:
            return
        if self.render:
            remaining = answer[self.flushed:] if self.flushed else answer
            if remaining.strip():
                self._print_block(remaining)
            elif not answer.strip():
                print(self.ui.s("(no content returned)", dim=True))
        else:
            print()


def osc52(text: str) -> str:
    payload = base64.b64encode(text.encode()).decode()
    return f"\x1b]52;c;{payload}\x07"


def copy_to_clipboard(text: str) -> bool:
    try:
        with open("/dev/tty", "w") as tty:
            tty.write(osc52(text))
            tty.flush()
        return True
    except OSError:
        return False


def transcript(session: Session, model_id: str) -> str:
    lines = [
        "# oac transcript", "",
        f"- Session: `{session.id}`",
        f"- Title: {session.title or '(untitled)'}",
        f"- Model: `{model_id}`",
        f"- Project: `{session.root}`",
        f"- Saved: {datetime.now(timezone.utc).isoformat()}", "",
    ]
    for message in session.messages:
        role = message.get("role")
        if role == "user":
            lines += ["## You", "", str(message.get("content", "")), ""]
        elif role == "assistant":
            if message.get("content"):
                lines += ["## oac", "", str(message["content"]), ""]
            for call in message.get("tool_calls", []):
                fn = call.get("function", {})
                arguments = str(fn.get("arguments", ""))[:120]
                lines.append(f"- tool `{fn.get('name', '?')}` `{arguments}`")
            if message.get("tool_calls"):
                lines.append("")
        elif role == "tool":
            content = str(message.get("content", ""))[:2000]
            lines += ["```", content, "```", ""]
    return "\n".join(lines)


def load_session_records(root: Path | None = None) -> list[dict]:
    records = []
    directory = state_path() / "sessions"
    if not directory.is_dir():
        return []
    for path in sorted(directory.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            data = json.loads(path.read_text())
        except (ValueError, OSError):
            continue
        if root is not None and Path(data.get("root", "?")).resolve() != root:
            continue
        data["_path"] = str(path)
        records.append(data)
    return records


def usage_panel(ui: UI, client: Client, config: Config) -> str:
    usage = client.usage
    lines = [
        ui.kv("requests", f"{usage.requests:,}"),
        ui.kv("prompt", f"{usage.prompt_tokens:,} tokens"),
        ui.kv("output", f"{usage.completion_tokens:,} tokens"),
        ui.kv("total", f"{usage.total_tokens:,} tokens"),
    ]
    cost = usage.cost(config.pricing)
    if cost is not None:
        lines.append(ui.kv("cost", format_cost(cost)))
    elif config.pricing:
        lines.append(ui.kv("cost", "no pricing match for used models"))
    if usage.models:
        rows = [[model, f"{entry[0]:,}", f"{entry[1]:,}", str(entry[2])]
                for model, entry in usage.models.items()]
        lines += ["", ui.table(["model", "prompt", "output", "requests"], rows)]
    else:
        lines += ["", ui.s("No usage reported yet. Servers that stream without a final usage",
                           dim=True),
                  ui.s("chunk report nothing here; totals update as reports arrive.", dim=True),
                  "", ui.s("Tip: oac stats shows usage history across all sessions.", dim=True)]
    return ui.panel("\n".join(lines), title="usage")


def show_plan(ui: UI, plan: PlanState) -> None:
    for index, (step, done) in enumerate(zip(plan.steps, plan.completed), 1):
        mark = ui.s("☑" if done else "☐", color=ui.theme.success if done else ui.theme.muted)
        number = ui.s(f"{index:>2}.", dim=True)
        print(f"  {mark} {number} {ui.s(step, dim=done)}")


def git_side_by_side(ui: UI, root: Path) -> None:
    def git(*arguments: str) -> subprocess.CompletedProcess:
        return subprocess.run(["git", "--no-pager", *arguments], cwd=root, capture_output=True, text=True, check=False)
    if git("rev-parse", "--is-inside-work-tree").returncode != 0:
        status(ui.s("· not a Git work tree; /diff --side needs Git", dim=True))
        return
    modified = [f for f in git("diff", "--name-only").stdout.splitlines() if f.strip()]
    untracked = [f for f in git("ls-files", "--others", "--exclude-standard").stdout.splitlines() if f.strip()]
    rendered = 0
    for name in (modified + untracked)[:20]:
        path = root / name
        if path.suffix.lower() in IMAGE_EXTENSIONS or not path.is_file():
            continue
        old_text = git("show", f"HEAD:{name}").stdout if name in modified else ""
        try:
            new_text = path.read_text()
        except (UnicodeError, OSError):
            continue
        if "\x00" in old_text + new_text:
            continue
        print(ui.s(f"◆ {name}", bold=True, color=ui.theme.accent))
        print(ui.side_by_side(old_text.splitlines(), new_text.splitlines(), "HEAD", "working"))
        rendered += 1
    if not rendered:
        status(ui.s("· no text changes to show", dim=True))
    elif len(modified + untracked) > 20:
        status(ui.s(f"· showing 20 of {len(modified + untracked)} changed files", dim=True))


def session_search(pattern: str, limit: int, project: Path | None, ui: UI) -> list[str]:
    try:
        regex = re.compile(pattern)
    except re.error as exc:
        raise ValueError(f"Invalid regular expression: {exc}") from None
    records = load_session_records(project.resolve() if project else None)
    lines: list[str] = []
    for record in records:
        if len(lines) >= limit:
            break
        for message in record.get("messages", []):
            if len(lines) >= limit:
                break
            content = message.get("content")
            if isinstance(content, list):
                content = " ".join(str(part.get("text", "")) for part in content if isinstance(part, dict))
            for line in str(content or "").splitlines():
                if regex.search(line):
                    session_id = record.get("id", "?")
                    title = (record.get("title") or "(untitled)")[:40]
                    lines.append(ui.s(f"{session_id} ", color=ui.theme.accent, bold=True)
                                 + ui.s(f"[{message.get('role', '?')}] ", dim=True)
                                 + ui.s(f"{title} · ", dim=True)
                                 + line.strip()[:200])
                    break
    return lines


def main(argv: list[str] | None = None) -> int:
    argv = _with_default_command(list(sys.argv[1:] if argv is None else argv))
    args = parser().parse_args(argv)
    if getattr(args, "command", None) is None:  # a value flag swallowed the subcommand
        args = parser().parse_args([*argv, "chat"])
    if args.no_color:
        import os
        os.environ["NO_COLOR"] = "1"
    ui: UI | None = None
    try:
        config = Config.load(getattr(args, "profile", None))
        if args.command == "init":
            if args.preset:
                preset = PRESETS[args.preset]
                args.base_url = preset["base_url"]
                args.api_key_env = preset["api_key_env"]
            config = Config(base_url=args.base_url, api_key_env=args.api_key_env, model=args.model,
                            api_mode=args.api_mode, context_window=args.context_window, theme=config.theme,
                            pricing=config.pricing, profiles=config.profiles)
            Client(config)  # Validate the URL before persisting it.
            if args.save_as:
                config.save_profile(args.save_as)
            else:
                config.save()
            ui = UI(config.theme)
            body = [ui.kv("base url", config.base_url),
                    ui.kv("api mode", config.api_mode),
                    ui.kv("model", config.model),
                    ui.kv("theme", config.theme)]
            if args.preset:
                body += ["", ui.s(PRESETS[args.preset]["hint"], dim=True)]
            if args.save_as:
                body += ["", f"Saved profile {args.save_as!r}; use:  oac --profile {args.save_as} chat"]
            body += ["", f"Export {config.api_key_env} if your API requires authentication,",
                     "then try:  oac models   oac doctor   oac chat"]
            print(ui.panel("\n".join(body), title=f"saved {config_path()}"))
            return 0
        if args.command == "config":
            ui = UI(config.theme)
            text = json.dumps(asdict(config), indent=2)
            print(ui.highlight(text, "json") if ui.level > 0 else text)
            return 0
        if args.command == "profiles":
            ui = UI(config.theme)
            if config.profiles:
                rows = []
                for name in sorted(config.profiles):
                    entry = config.profiles[name]
                    rows.append([name, str(entry.get("base_url", "?")), str(entry.get("model", "auto"))])
                print(ui.table(["profile", "base url", "model"], rows, accent_col=0))
                print(ui.s("use: oac --profile NAME chat", dim=True))
            else:
                print("No profiles saved yet. Create one with:  oac init --base-url ... --save-as NAME")
            return 0
        if args.command == "theme":
            ui = UI(config.theme)
            if args.name:
                if args.name not in THEMES:
                    print(f"Unknown theme {args.name!r}. Available: {', '.join(sorted(THEMES))}", file=sys.stderr)
                    return 1
                config.theme = args.name
                config.save()
                ui2 = UI(args.name)
                print(ui2.s("✓", color=ui2.theme.success) + f" Theme set to {args.name} and saved.")
            else:
                print(theme_list_line(ui))
                print(ui.s(f"  current: {config.theme}", dim=True))
            return 0
        if args.command == "fetch":
            print(web_fetch(args.url, max_chars=args.max_chars))
            return 0
        if args.command == "search":
            ui = UI(config.theme)
            lines = session_search(args.pattern, max(1, args.limit), args.project, ui)
            if lines:
                print("\n".join(lines))
                print(ui.s(f"{len(lines)} matching session(s) (limit {args.limit})", dim=True))
            else:
                print("No sessions match that pattern.")
            return 0
        if args.command == "stats":
            entries = stats.load(args.days)
            if args.json:
                print(json.dumps(entries, indent=2))
            else:
                ui = UI(config.theme)
                print(ui.panel(stats.dashboard(entries, ui.bar), title="usage stats"
                               + (f" · {args.days} days" if args.days else " · all time")))
            return 0
        if args.command == "audit":
            log_path = args.cwd.resolve() / ".oac" / "audit.jsonl"
            entries = AuditLog.tail(log_path, args.tail)
            if not entries:
                print(f"No audit entries at {log_path}")
                return 0
            if args.json:
                print(json.dumps(entries, indent=2))
            else:
                ui = UI(config.theme)
                rows = [[str(e.get("time", "?"))[:19], str(e.get("tool", "?")),
                         "ok" if e.get("ok") else "FAIL", str(e.get("result", ""))[:60]] for e in entries]
                print(ui.table(["time", "tool", "status", "result"], rows, accent_col=1))
            return 0
        if args.command == "mcp":
            ui = UI(config.theme)
            if args.test:
                registry = McpRegistry({args.test: config.mcp_servers.get(args.test, {})}, Path.cwd())
                if args.test not in config.mcp_servers:
                    print(f"No MCP server named {args.test!r} in the config", file=sys.stderr)
                    return 1
                registry.connect(status)
                for line in registry.status_lines():
                    print(line)
                registry.stop_all()
            else:
                if not config.mcp_servers:
                    print("No MCP servers configured. Add one under \"mcp_servers\" in the config:")
                    print('  {"memory": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-memory"]}}')
                else:
                    for name in sorted(config.mcp_servers):
                        print(ui.s(name, color=ui.theme.accent, bold=True) + f"  {config.mcp_servers[name].get('command', '?')}")
                    print(ui.s("probe one with:  oac mcp --test NAME", dim=True))
            return 0
        if args.command == "sessions" and args.export:
            target = state_path() / "sessions" / f"{args.export}.json"
            if not target.exists():
                print(f"No session {args.export!r}", file=sys.stderr)
                return 1
            data = json.loads(target.read_text())
            out = Path(args.out) if args.out else Path.cwd() / f"oac-{args.export}.html"
            exports.export_session_file(data, out, data.get("model", ""))
            print(f"Exported {args.export} to {out}")
            return 0
        if args.command == "sessions":
            ui = UI(config.theme)
            if args.delete:
                target = state_path() / "sessions" / f"{args.delete}.json"
                if not target.exists():
                    print(f"No session {args.delete!r}", file=sys.stderr)
                    return 1
                target.unlink()
                print(f"Deleted session {args.delete}")
                return 0
            if args.delete_all:
                directory = state_path() / "sessions"
                removed = 0
                if directory.is_dir():
                    for path in directory.glob("*.json"):
                        path.unlink()
                        removed += 1
                print(f"Deleted {removed} session{'s' if removed != 1 else ''}.")
                return 0
            records = load_session_records()
            if args.json:
                print(json.dumps([{k: v for k, v in r.items() if not k.startswith("_")} for r in records], indent=2))
            elif records:
                rows = [[r.get("id", "?"), (r.get("title") or "(untitled)")[:38], r.get("model", "?"),
                         str(r.get("updated", "?"))[:16].replace("T", " "), r.get("root", "?")] for r in records]
                print(ui.table(["id", "title", "model", "updated", "project"], rows, accent_col=0))
            else:
                print("No saved sessions yet. Run oac chat to start one.")
            return 0
        for field in ("base_url", "model", "api_mode", "context_window", "max_steps", "max_output_tokens"):
            value = getattr(args, field, None)
            if value is not None:
                setattr(config, field, value)
        config.validate()
        ui = UI(getattr(args, "theme", None) or config.theme)
        client = Client(config)
        if args.command == "models":
            models = client.models()
            if args.json:
                print(json.dumps([asdict(m) for m in models], indent=2))
            else:
                rows = [[m.id, str(m.context_window or "unknown"), str(m.max_output_tokens or "unknown"), m.source] for m in models]
                print(ui.table(["model", "context", "max output", "limits"], rows, accent_col=0) if rows else "No models reported by the API.")
            return 0
        model = client.discover()
        status(ui.s(f"· model {model.id}", dim=True) + "  " + ui.s(f"context {model.context_window or 'unknown (8192 fallback)'} · {model.source}", dim=True))
        if args.command == "doctor":
            result = client.complete(model, [{"role": "user", "content": "Reply with OK."}], [])
            if not result.get("content"):
                raise APIError("Doctor expected a text completion but the API returned no text")
            body = "\n".join([
                ui.kv("model", model.id),
                ui.kv("context", str(model.context_window or "unknown")),
                ui.kv("api mode", config.api_mode),
                ui.kv("completion", result["content"]),
            ])
            print(ui.panel(body, title="doctor"))
            return 0
        root = args.cwd.resolve()
        if not root.is_dir():
            raise ValueError("--cwd must be an existing directory")
        resume_id = args.resume
        if args.continue_session and not resume_id:
            resume_id = Session.latest(root)
            if not resume_id:
                raise ValueError("No previous session in this project to continue")
        session = Session(root, resume_id)
        render = (not args.json) and (not args.no_render) and ui.level > 0
        runner = Runner(ui, render, quiet=bool(args.json))
        allow = [*config.allow, *[item.strip() for item in (args.allow or "").split(",") if item.strip()]]
        tools = Tools(root, args.approval, approve, test_cmd=config.test_cmd, allow=allow,
                      audit=AuditLog(root, session.id, enabled=config.audit))
        registry = McpRegistry(config.mcp_servers, root)

        def mcp_dispatch(name: str, call_args: dict) -> str:
            found = registry.lookup(name)
            if not found:
                raise ValueError(f"Unknown MCP tool {name}")
            server, tool = found
            if tool.read_only or tools.allowed(f"MCP {server.name}.{tool.name}:\n{json.dumps(call_args, ensure_ascii=False)[:600]}"):
                try:
                    return registry.dispatch(name, call_args)
                except McpError as exc:
                    return f"Tool error: {exc}"
            return "Denied by approval policy. Do not retry this action without user instruction."

        agent = Agent(client, model, tools, session,
                      runner.emit, runner.status, stream=not args.no_stream and not args.json,
                      on_tool=runner.on_tool,
                      extra_schemas=registry.specs(), extra_tools=mcp_dispatch,
                      auto_compact=config.auto_compact,
                      agents_md=config.agents_md and not args.no_instructions)
        if registry.servers and args.command in {"chat", "run"}:
            registry.connect(runner.status if not args.json else None)
        if args.json:
            status(f"Session: {session.id}; project: {root}; approval: {args.approval}")
        else:
            if sys.stdin.isatty():
                print(ui.banner(__version__, model.id,
                                f"{model.context_window or 'unknown (8192 fallback)'}",
                                str(root), session.id))
            status(ui.s(f"session {session.id}", dim=True) + ui.s(f" · approval {args.approval}", dim=True)
                   + ui.s(" · /help for commands", dim=True))

        def do_run(prompt: str, images: list[Path] | None = None, system_suffix: str = "", silence_stream: bool = False) -> str:
            started = time.monotonic()
            saved_quiet = runner.quiet
            if silence_stream:
                runner.quiet = True  # draft silently; the caller renders the result
            runner.start()
            try:
                answer = agent.run(prompt, images, system_suffix)
            finally:
                runner.quiet = saved_quiet
                runner.stop()
                if not args.json and time.monotonic() - started >= config.notify_min_seconds:
                    notify_done(config.notify, f"oac finished: {session.title or 'task'}")
            return answer

        initial = " ".join(args.prompt)
        prompt_images = [Path(p) for p in args.image]
        if args.command == "run":
            prompt = initial or (sys.stdin.read() if not sys.stdin.isatty() else "")
            if not prompt.strip() and not prompt_images:
                raise ValueError("Supply a prompt or pipe one to oac run")
            prompt, at_images = extract_images(prompt, root)
            images = prompt_images + at_images
            if args.plan:
                answer = do_run(prompt, images, PLAN_INSTRUCTIONS, silence_stream=True)
                steps = parse_plan(answer)
                runner.stop()
                registry.stop_all()
                if steps:
                    print(ui.md("\n".join(f"{i}. {step}" for i, step in enumerate(steps, 1))))
                    status(ui.s("· plan drafted; review it in oac chat with /plan to execute", dim=True))
                else:
                    print(answer)
                    status(ui.s("· no numbered plan detected in the reply", dim=True))
                return 0
            answer = do_run(prompt, images)
            runner.finish(answer)
            if args.json:
                print(json.dumps({"session_id": session.id, "model": model.id, "answer": answer}))
            registry.stop_all()
            return 0

        repl = Repl(root, state_path() / "history") if sys.stdin.isatty() else None
        last_prompt, last_answer = "", ""
        plan = PlanState()
        plan_armed = False

        def run_plan_step() -> bool:
            pending = plan.pending()
            if not pending:
                plan.abort()
                status(ui.s("· plan complete — all steps handled", dim=True))
                return False
            index = pending[0]
            print(ui.s(f"━━ plan step {index + 1}/{len(plan.steps)} ", bold=True, color=ui.theme.warning)
                  + ui.s("─" * 8, dim=True) + " " + plan.steps[index])
            try:
                do_run(f"Execute step {index + 1} of the approved plan: {plan.steps[index]}\n"
                       "Do only this step; report what you did and any blockers.")
                plan.mark(index, True)
                show_plan(ui, plan)
                return True
            except (APIError, KeyboardInterrupt):
                runner.stop()
                status(ui.s(f"· paused at step {index + 1}; /plan next retries it, /plan skip {index + 1} skips", dim=True))
                return False

        def dispatch(prompt: str) -> bool:
            """Handle one REPL input; returns True when the REPL should exit."""
            nonlocal last_prompt, last_answer, session, model, plan_armed
            if prompt in {"/exit", "/quit"}:
                return True
            stripped = prompt.strip()
            if not stripped:
                return False
            if stripped == "/help":
                print(ui.table(["command", "description"], HELP_ROWS, accent_col=0))
            elif stripped == "/new":
                session = Session(root)
                agent.session = session
                plan.abort()
                plan_armed = False
                status(ui.s(f"· new session {session.id}", dim=True))
            elif stripped.startswith("/model"):
                target = stripped.split(maxsplit=1)[1].strip() if len(stripped.split(maxsplit=1)) > 1 else ""
                if not target:
                    status(ui.s(f"· current model {model.id}", dim=True))
                else:
                    config.model = target
                    model = client.discover()
                    agent.model = model
                    status(ui.s(f"· model {model.id} · context {model.context_window or 'unknown'}", dim=True))
            elif stripped == "/models":
                rows = [[m.id, str(m.context_window or "unknown")] for m in client.models()]
                print(ui.table(["model", "context"], rows, accent_col=0))
            elif stripped == "/context":
                tool_cost = estimate([{"role": "system", "content": json.dumps(agent.schemas())}])
                used = estimate(session.messages) + tool_cost
                context = model.context_window or 8192
                fraction = min(1.0, used / context)
                print("  " + ui.bar(fraction) + ui.s(f" {used:,} / {context:,} tokens (estimated)", dim=True))
                if config.auto_compact > 0:
                    status(ui.s(f"· auto-compaction triggers at {config.auto_compact:.0%}", dim=True))
            elif stripped == "/usage":
                print(usage_panel(ui, client, config))
            elif stripped == "/undo":
                result = agent.tools.undo()
                ok = not result.startswith(("Nothing", "Skipped"))
                print(ui.s("✓ " if ok else "· ", color=ui.theme.success if ok else None) + result)
            elif stripped == "/retry":
                if not last_prompt:
                    status(ui.s("· nothing to retry yet", dim=True))
                else:
                    last_answer = do_run(last_prompt)
                    runner.finish(last_answer)
            elif stripped.startswith("/diff"):
                if "--side" in stripped:
                    git_side_by_side(ui, root)
                else:
                    subprocess.run(["git", "--no-pager", "diff", "--no-ext-diff"], cwd=root, check=False)
            elif stripped == "/compact":
                indexes = [i for i, m in enumerate(session.messages) if m["role"] == "user"]
                if indexes:
                    session.messages = session.messages[indexes[-1]:]
                    session.save(model.id)
                status(ui.s("· kept the latest turn; discarded earlier turns", dim=True))
            elif stripped.startswith("/plan"):
                parts = stripped.split()
                sub = parts[1].lower() if len(parts) > 1 else ""
                if not sub:
                    if plan.status == "off" and not plan_armed:
                        plan_armed = True
                        status(ui.s("· plan mode armed — describe the goal; the agent drafts a plan for review", dim=True))
                    elif plan.status == "review":
                        show_plan(ui, plan)
                        status(ui.s(f"· {plan.summary()}", dim=True))
                    else:
                        show_plan(ui, plan)
                        status(ui.s(f"· {plan.summary()}", dim=True))
                elif sub in {"approve", "go"} and plan.status == "review":
                    plan.start()
                    while run_plan_step():
                        pass
                elif sub == "next" and plan.status == "executing":
                    run_plan_step()
                elif sub == "skip":
                    pending = plan.pending()
                    if not pending:
                        status(ui.s("· no pending steps", dim=True))
                    else:
                        index = pending[0]
                        if len(parts) > 2 and parts[2].isdigit():
                            index = int(parts[2]) - 1
                        plan.mark(index, True)
                        status(ui.s(f"· skipped step {index + 1}", dim=True))
                elif sub in {"abort", "off", "cancel"}:
                    plan.abort()
                    plan_armed = False
                    status(ui.s("· plan mode off", dim=True))
                else:
                    status(ui.s(f"· {plan.summary()}", dim=True))
            elif stripped.startswith("/fork"):
                parts = stripped.split()
                turns = [i for i, m in enumerate(session.messages) if m.get("role") == "user"]
                if not turns:
                    status(ui.s("· nothing to fork yet", dim=True))
                    return False
                try:
                    keep = int(parts[1]) if len(parts) > 1 else max(1, len(turns) - 1)
                except ValueError:
                    status(ui.s("· usage: /fork [N] — branch from user turn N", dim=True))
                    return False
                if not 1 <= keep < len(turns):
                    status(ui.s(f"· turn must be between 1 and {len(turns) - 1} (there are {len(turns)} turns)", dim=True))
                    return False
                end = turns[keep]
                forked = Session(root)
                forked.messages = [dict(m) for m in session.messages[:end]]
                forked.title = ("fork: " + session.title)[:72]
                parent = session.id
                forked.save(model.id, parent=parent)
                session, agent.session = forked, forked
                status(ui.s(f"· forked {forked.id} from turn {keep} (parent {parent}); {len(forked.messages)} messages kept", dim=True))
            elif stripped.startswith("/restore"):
                parts = stripped.split()
                points = RestorePoints(root)
                if len(parts) == 1:
                    records = points.list()
                    if records:
                        rows = [[p["id"], str(p["time"])[:19], p["label"][:44], str(len(p["files"]))] for p in records]
                        print(ui.table(["id", "time", "label", "files"], rows, accent_col=0))
                        status(ui.s("· /restore ID [FILE] rolls back", dim=True))
                    else:
                        status(ui.s("· no restore points yet (they appear when the agent edits files)", dim=True))
                else:
                    try:
                        actions = points.restore(parts[1], parts[2] if len(parts) > 2 else None)
                    except ValueError as exc:
                        status(ui.s(f"✗ {exc}", color=ui.theme.error))
                    else:
                        for action in actions:
                            print(ui.s("✓ ", color=ui.theme.success) + action)
            elif stripped == "/mcp":
                for line in registry.status_lines():
                    print(ui.s(line, dim=True))
            elif stripped.startswith("/export"):
                target = stripped.split(maxsplit=1)[1].strip() if len(stripped.split(maxsplit=1)) > 1 else ""
                path = Path(target) if target else Path.cwd() / f"oac-{session.id}.html"
                exports.export_session_file({"id": session.id, "title": session.title, "messages": session.messages},
                                            path, model.id)
                status(ui.s(f"· exported HTML to {path}", dim=True))
            elif stripped == "/editor":
                text = edit_text(last_prompt)
                if text:
                    return dispatch(text)
                status(ui.s("· editor returned no changes", dim=True))
            elif stripped.startswith("/notify"):
                parts = stripped.split()
                if len(parts) > 1 and parts[1].lower() in {"off", "bell", "desktop", "all"}:
                    config.notify = parts[1].lower()
                    config.save()
                    status(ui.s(f"· notifications set to {config.notify}", dim=True))
                else:
                    status(ui.s(f"· notify {config.notify} (after {config.notify_min_seconds:.0f}s tasks); /notify off|bell|desktop|all", dim=True))
            elif stripped.startswith("/fetch"):
                target = stripped.split(maxsplit=1)[1].strip() if len(stripped.split(maxsplit=1)) > 1 else ""
                if not target:
                    status(ui.s("· usage: /fetch URL", dim=True))
                else:
                    try:
                        print(ui.md(web_fetch(target)))
                    except ValueError as exc:
                        status(ui.s(f"✗ {exc}", color=ui.theme.error))
            elif stripped.startswith("/theme"):
                target = stripped.split(maxsplit=1)[1].strip() if len(stripped.split(maxsplit=1)) > 1 else ""
                if not target:
                    print(theme_list_line(ui))
                    status(ui.s(f"· current {ui.theme_name}", dim=True))
                elif ui.set_theme(target):
                    config.theme = target
                    config.save()
                    print(ui.s("✓", color=ui.theme.success) + f" Theme switched to {target} and saved.")
                else:
                    status(ui.s(f"· unknown theme {target!r}", color=ui.theme.error))
            elif stripped.startswith("/title"):
                title = stripped.split(maxsplit=1)[1].strip() if len(stripped.split(maxsplit=1)) > 1 else ""
                session.title = title or session.title
                session.save(model.id)
                status(ui.s(f"· session title set to {session.title!r}", dim=True))
            elif stripped == "/sessions":
                records = load_session_records(root)
                rows = [[r.get("id", "?"), (r.get("title") or "(untitled)")[:38],
                         str(r.get("updated", "?"))[:16].replace("T", " ")] for r in records]
                print(ui.table(["id", "title", "updated"], rows, accent_col=0) if rows
                      else "No sessions in this project yet.")
            elif stripped == "/copy":
                if not last_answer:
                    status(ui.s("· no answer to copy yet", dim=True))
                elif copy_to_clipboard(last_answer):
                    status(ui.s("· copied the last answer to the clipboard", dim=True))
                else:
                    status(ui.s("· clipboard copy needs a terminal (OSC52)", dim=True))
            elif stripped.startswith("/save"):
                target = stripped.split(maxsplit=1)[1].strip() if len(stripped.split(maxsplit=1)) > 1 else ""
                path = Path(target) if target else Path.cwd() / f"oac-{session.id}.md"
                path.write_text(transcript(session, model.id))
                status(ui.s(f"· saved {path}", dim=True))
            elif stripped.startswith("/"):
                status(ui.s(f"· unknown command {stripped.split()[0]!r} — try /help", dim=True))
            else:
                try:
                    if plan_armed:
                        plan_armed = False
                        answer = do_run(prompt, None, PLAN_INSTRUCTIONS, silence_stream=True)
                        steps = parse_plan(answer)
                        if steps:
                            plan.set_plan(steps)
                            print(ui.s("Proposed plan:", bold=True, color=ui.theme.warning))
                            show_plan(ui, plan)
                            status(ui.s("· /plan approve executes · /plan abort discards · or send feedback to revise", dim=True))
                        else:
                            print(ui.md(answer) if answer.strip() else "")
                            status(ui.s("· no numbered plan detected; plan mode disarmed", dim=True))
                    elif plan.status == "review":
                        answer = do_run(prompt + "\n\nRevise the previous plan given this feedback. Output the full revised plan in the ```plan block again.", None, PLAN_INSTRUCTIONS, silence_stream=True)
                        steps = parse_plan(answer)
                        if steps:
                            plan.set_plan(steps)
                            show_plan(ui, plan)
                            status(ui.s("· revised; /plan approve executes", dim=True))
                        else:
                            print(ui.md(answer) if answer.strip() else "")
                            status(ui.s("· still no plan detected; /plan abort to give up", dim=True))
                    else:
                        last_prompt = prompt
                        clean, at_images = extract_images(prompt, root)
                        last_answer = do_run(clean or prompt, at_images or None)
                        runner.finish(last_answer)
                        if args.json:
                            print(json.dumps({"session_id": session.id, "model": model.id, "answer": last_answer}))
                except APIError as exc:
                    runner.stop()
                    status(ui.s(f"✗ {exc}", color=ui.theme.error))
                except KeyboardInterrupt:
                    runner.stop()
                    status(ui.s("· interrupted — session saved; the REPL is still active", dim=True))
            return False

        try:
            while True:
                try:
                    prompt_text = (ui.s("oac", bold=True, color=ui.theme.accent) + " "
                                   + ui.s(model.id, dim=True) + " "
                                   + (ui.s("plan ", color=ui.theme.warning) if (plan_armed or plan.status != "off") else "")
                                   + ui.s("❯", color=ui.theme.accent) + " ")
                    prompt = initial or (repl.input(prompt_text) if repl else input(prompt_text))
                    initial = ""
                except EOFError:
                    break
                if dispatch(prompt):
                    break
        finally:
            if repl:
                repl.save()
            registry.stop_all()
        return 0
    except (APIError, ValueError, OSError, KeyError) as exc:
        if ui is not None:
            status(ui.s(f"✗ {exc}", color=ui.theme.error))
        else:
            status(f"Error: {exc}")
        return 1
    except KeyboardInterrupt:
        status("Interrupted.")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
