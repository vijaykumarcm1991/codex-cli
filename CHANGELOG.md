# Changelog

## 0.3.0 — 2026-10-24

The "everything" release: agent intelligence, safety infrastructure, and a
full MCP client — still zero runtime dependencies.

### Added
- Chat is now the default command: bare `oac` opens the interactive REPL,
  `oac TEXT...` is shorthand for `oac chat TEXT...`, and `oac --no-color` /
  `oac --profile NAME` without a subcommand also open chat. Every real
  subcommand (`oac run`, `oac models`, ...) is untouched, and `oac --version`
  / `oac --help` still work exactly as before. README now documents the
  global install paths (`pipx install .`, `uv tool install .`,
  `pip install --user .`) that put a single `oac` on `PATH` — zero runtime
  dependencies makes a global install conflict-free.
- Plan mode: `/plan` arms read-only planning, the agent drafts a numbered
  plan, and `/plan approve` executes steps one at a time with a live ☐/☑
  checklist; feedback revises the plan, `/plan next|skip N|abort` steer
  execution, and `oac run --plan` prints a draft without executing.
- Sub-agents: the `spawn_agents` tool fans out 1–3 parallel read-only
  research agents (read/search tools only, writes denied) and merges their
  findings into the main conversation.
- MCP client (`oac/mcp.py`): stdio JSON-RPC 2.0 handshake, tool discovery,
  and bridged `mcp__<server>__<tool>` functions with one automatic server
  restart; annotation-driven approvals (`readOnlyHint` skips prompts);
  `oac mcp`, `oac mcp --test NAME`, and `/mcp` for status.
- Vision input: `@file.png` tokens in prompts (and `--image PATH`) become
  base64 data-URL image parts for vision models in Chat and Responses modes;
  caps of 4 images and 5 MB each with clear errors.
- New tools: `web_fetch` (HTTP GET → readable text, HTML stripped, stdlib
  `html.parser`), `run_tests` (configured `test_cmd`, auto-approved only for
  safe selector arguments), and AST-powered `list_symbols`,
  `find_references`, and `rename_symbol` for Python (token-accurate, never
  renames `obj.attr` accesses, diff preview, journaled for `/undo`).
- Restore points: every accepted file mutation snapshots prior state into
  `<project>/.oac/restore/` (100-checkpoint rolling window); `/restore`,
  `/restore ID`, and `/restore ID FILE` roll back across sessions.
- Shell allowlists: `--allow "git status,pytest"` and a config `allow` list
  auto-approve word-prefix matches in `ask` mode.
- Audit log: JSONL record of every tool call (args, result, status) at
  `<project>/.oac/audit.jsonl`; `oac audit [--tail N] [--json]` reviews it.
- Auto-compaction: when session usage crosses `auto_compact` (default 0.75
  of the context window), earlier turns are model-summarized and replaced,
  preserving the latest turn; best-effort and configurable, 0 disables.
- Session forking: `/fork [N]` branches from user turn N into a new session
  with the parent recorded.
- Cross-session search: `oac search REGEX [--project DIR] [--limit N]`
  greps saved sessions with highlighted matches.
- HTML export: `oac sessions --export ID [--out PATH]` and `/export` write
  a standalone dark-theme transcript page with rendered markdown, code
  blocks, and collapsible tool calls.
- Usage ledger + `oac stats`: per-response token records persist globally
  (`usage.jsonl`) with per-day bars and per-model/per-project breakdowns;
  cost is computed where pricing matches.
- Streaming markdown: rendered blocks print progressively as the stream
  completes paragraphs and code fences — no flicker, no repaint; the
  spinner ticks through it.
- Side-by-side diffs: `/diff --side` aligns HEAD vs working tree in two
  columns (red/green, line numbers, 20-file cap, narrow-terminal fallback).
- Editor prompts: `Ctrl-E` and `/editor` open `$OAC_EDITOR`/`$VISUAL`/
  `$EDITOR` pre-filled with the last prompt and pipe the result back.
- Fuzzy completion: slash commands fuzzy-match on Tab (`/pln` → `/plan`)
  via a small fzf-style subsequence scorer; `@` completes image paths.
- Completion notifications: `notify` config (`off|bell|desktop|all`) and
  `notify_min_seconds` fire a bell or desktop notification when long tasks
  finish; `/notify` toggles live.
- Connection profiles: `oac init --save-as NAME`, `--profile NAME`, and
  `OAC_PROFILE` switch base URL/key variable/model/api mode/context; listed
  by `oac profiles`.
- Local-server presets: `oac init --preset ollama|lmstudio|vllm|jan|
  llamacpp` configures the loopback endpoint in one command.
- REPL prompt indicator shows active plan state; `/context` reports the
  auto-compaction threshold; `--no-instructions` disables AGENTS.md loading.

### Changed
- Shell execution refactored into a shared runner used by `shell` and the
  new `run_tests` tool (spooled output, process-group kill on timeout).
- `Session.save` accepts a `parent` id (forks) and extra metadata.
- Agent tool dispatch now routes `spawn_agents` and `mcp__*` tools through
  a single hook; schemas include sub-agent and MCP tool definitions in
  context estimates.
- `/usage` panel links to `oac stats`; `--json` REPL output unchanged.
- README rewritten around the new workflows; tests grew from 57 to 112.

## 0.2.0 — 2026-10-24

The modernization release. Same zero-dependency core, a completely rebuilt
terminal experience, and a broader toolset.

### Added
- Modern terminal UI (`oac/ui.py`): markdown rendering (headings, lists,
  tables, blockquotes, rules), syntax-highlighted code blocks for
  Python/JS/TS/JSON/Bash, rounded panels, aligned tables, progress bars, an
  animated spinner, and an ASCII startup banner.
- Theme system with five palettes: `tokyo-night` (default), `dracula`,
  `nord`, `light`, `mono`. Automatic truecolor → 256-color → 16-color →
  plain degradation; `NO_COLOR`, `FORCE_COLOR`, and `--no-color` respected.
  Switch with `oac theme NAME`, `/theme`, `--theme`, or `OAC_THEME`.
- New agent tools: `find_files` (glob patterns) and `regex_search`
  (regular expressions, case-sensitive, capped results).
- Undo journal: `write_file`/`edit_file` mutations are recorded and `/undo`
  reverts the most recent one; refuses when the file changed afterwards.
- Token usage tracking from streaming (`stream_options.include_usage`, with
  automatic fallback when unsupported) and non-streaming responses;
  `/usage` panel with per-model breakdown and optional cost estimates via
  the `pricing` config map.
- Automatic retry with exponential backoff, jitter, and `Retry-After`
  honoring for 429/5xx responses and connection failures (`retries`,
  `retry_backoff` config fields).
- Session management: auto-generated titles, `oac sessions` table with
  `--json`/`--delete ID`/`--delete-all`, `chat --continue`, and `/sessions`.
- REPL upgrades: persistent history, Tab completion for commands and
  project paths, `/retry`, `/copy` (OSC52 clipboard), `/save` Markdown
  transcripts, `/title`, `/context` progress bar, and a table-formatted
  `/help`.
- Tool activity lines with argument previews, timing, and error markers.

### Changed
- Model answers render as styled markdown when stdout is a terminal;
  `--no-render` restores the raw token stream, and piping stays plain text.
- Ctrl-C aborts the current agent turn while keeping the REPL alive;
  Ctrl-C at the prompt still exits.
- `oac models`, `oac sessions`, `oac config`, and `oac init` output
  reformatted as tables/panels (JSON modes unchanged).
- README rewritten; test suite grew from 19 to 57 tests.

### Fixed
- Streaming responses no longer lose token accounting: usage chunks are
  captured when servers provide them.

## 0.1.0 — initial release
- Terminal coding agent for OpenAI-compatible APIs: chat/run commands,
  six tools, approval policies, streaming, session persistence, automatic
  model and context discovery, context trimming, and 19 tests.
