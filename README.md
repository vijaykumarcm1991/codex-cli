# Open Agent CLI (`oac`) — v0.3.0

A **modern terminal coding agent** for OpenAI-compatible APIs. Built with Python 3.11+ and **zero runtime dependencies** — every panel, theme, spinner, syntax highlight, and even the MCP client is hand-rolled on raw ANSI, `urllib`, and the standard library.

```
   ▄███▄     ██     ▄███▄
  ██   ██   ████   ██   ██      oac v0.3.0 ● tokyo-night
  ██   ██  ██  ██  ██   ██      model     gpt-4o-mini
  ██   ██  ██████  ██   ██      context   128000
   ▀███▀   ██  ██   ▀███▀       project   ~/my-app
```

## What's new in 0.3.0

| Area | Upgrades |
| --- | --- |
| **Plan mode** | `/plan` arms the agent to draft a numbered plan first; review the checklist, send feedback to revise, then `/plan approve` executes step-by-step with a live ☐/☑ list (plus `/plan next`, `/plan skip N`, `/plan abort`). `oac run --plan` prints a draft plan without executing. |
| **Sub-agents** | New `spawn_agents` tool fans out 1–3 **read-only research agents** in parallel — they can read/search your project but never write or execute — and report findings back to the main agent. |
| **MCP client** | Connect [Model Context Protocol](https://modelcontextprotocol.io) servers over stdio (`npx @modelcontextprotocol/server-memory`, filesystem servers, custom tools…). Their tools appear in the agent loop as `mcp__<server>__<tool>` with annotations-aware approvals. `oac mcp` inspects, `/mcp` shows live status. |
| **Vision input** | Attach images with `@file.png` in any prompt (or `--image PATH`) — base64 data-URL parts for vision models, in Chat and Responses modes. |
| **New tools** | `web_fetch` (URL → readable text), `run_tests` (runs your configured test command, auto-approved for safe selectors), and AST-powered `list_symbols` / `find_references` / `rename_symbol` for Python refactoring that skips `obj.attr` accesses. |
| **Restore points** | Every agent file mutation snapshots the prior state into `<project>/.oac/restore/`. `/restore [ID [FILE]]` rolls back any checkpoint, even across sessions — a persistent, granular superset of `/undo`. |
| **Shell allowlist** | `--allow "git status,pytest"` (or an `allow` list in the config) auto-approves matching command prefixes while everything else still asks. |
| **Audit log** | Every tool call lands in `<project>/.oac/audit.jsonl` with arguments, result, and status. `oac audit` reviews it. |
| **Auto-compaction** | When a session nears its context window, earlier turns are summarized by the model and replaced — no more silent truncation. Threshold configurable (`auto_compact`), 0 disables. |
| **Session forking & search** | `/fork [N]` branches the conversation from user turn N (parent recorded). `oac search REGEX` greps across all saved sessions with match highlighting. |
| **Streaming markdown** | Answers render **live as they stream** — completed paragraphs and code blocks paint progressively, no flicker, no repaint. |
| **Nicer diffs** | `/diff --side` renders an aligned two-column view (red removals / green additions) with line numbers for every changed file. |
| **Editor integration** | `Ctrl-E` (or `/editor`) opens `$OAC_EDITOR`/`$VISUAL`/`$EDITOR` to compose long prompts and pipes them back in. |
| **Fuzzy palette** | Tab-completion now fuzzy-matches slash commands (`/pln` → `/plan`) and completes `@image` attachments. |
| **Notifications** | Terminal bell or desktop notification (`notify-send`/`osascript`) when a task longer than `notify_min_seconds` finishes. `/notify off|bell|desktop|all`. |
| **Profiles & presets** | `oac init --save-as work` stores connection profiles; switch with `--profile work` or `OAC_PROFILE`. One-command local presets: `oac init --preset ollama|lmstudio|vllm|jan|llamacpp`. |
| **Usage ledger** | Token usage persists across sessions in a global ledger. `oac stats` shows totals, per-day bars, and per-model/per-project breakdowns. `oac sessions --export ID` writes a standalone styled HTML transcript; `/export` does it from the REPL. |

## Install and configure

`oac` is a single pure-Python package with **zero runtime dependencies**, so a global install is safe — no version conflicts, nothing else in the venv to break.

**Recommended: install once, use `oac` from anywhere in any directory**

```sh
# pick one of these three:
pipx install .                  # pipx isolates the install
git clone https://github.com/vijaykumarcm1991/codex-cli && pipx install ./codex-cli
uv tool install .               # if you use uv
# ...or the classic way into your user site:
pip install --user .
```

Every option above puts a single `oac` executable on your `PATH` — run it from any directory, no `.venv/bin/` prefix ever again.

**Or keep it project-local**

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e .
```

Configure and start (works the same for global and local installs):

```sh
oac init --base-url https://api.openai.com/v1 --model auto
export OPENAI_API_KEY='your-key'
oac models
oac doctor
oac                               # that's it — chat opens with no subcommand
```

Set credentials locally in your shell or secret manager; never commit them. `init` stores the **name** of the credential variable, never its value. Config lives in `~/.config/oac/config.json`; override with `OAC_CONFIG`. Sessions live in `~/.local/state/oac/sessions`; override with `OAC_STATE_DIR`. Session files contain prompts, code, and tool output, and are written with owner-only permissions.

Local models with one command:

```sh
oac init --preset ollama        # or lmstudio, vllm, jan, llamacpp
ollama pull qwen2.5-coder:7b    # then:
oac
```

Profiles for multiple providers:

```sh
oac init --base-url https://api.openai.com/v1 --api-key-env OPENAI_API_KEY --save-as openai
oac init --preset ollama --save-as local
oac --profile openai            # or: OAC_PROFILE=openai oac
oac profiles                    # list saved profiles
```

Gateways with the Responses API:

```sh
oac init --base-url https://your-gateway.example/v1 --api-mode responses
```

The configured URL is the API prefix, usually ending in `/v1`. Remote URLs require HTTPS; loopback servers can use HTTP. TLS validation remains enabled, redirects are rejected, and API keys are sent only to the configured endpoint. A server must support function calling for agent tools; listing models alone does not establish that support. `doctor` makes a real text completion, which may incur provider charges.

## Coding workflow

`oac` with no subcommand opens the chat REPL — `oac chat` still works, and `oac TEXT...` is shorthand for `oac chat TEXT...`:

```sh
oac                                 # interactive REPL in the current directory
oac --cwd /path/to/project          # interactive REPL for another project
oac --continue                      # resume the latest session here
oac 'why does this test fail?'      # same as: oac chat 'why does this test fail?'
oac run 'Inspect the project and explain its architecture' --approval read-only
oac run 'Fix the failing tests and run them' --max-steps 40
oac run --plan 'Add dark mode to the settings page'   # draft a plan, don't execute
oac run 'Compare these screenshots @new-ui.png @old-ui.png'   # vision input
printf 'Summarize the README' | oac run --approval read-only --json
oac sessions                        # table: id, title, model, updated
oac sessions --delete ID            # remove one session
oac sessions --export ID            # standalone HTML transcript
oac search 'TODO|FIXME'             # grep across all saved sessions
oac stats --days 14                 # token/cost dashboard
oac audit --tail 30                 # recent tool calls in this project
```

### Tools available to the agent

| Tool | Purpose |
| --- | --- |
| `read_file` | Read a UTF-8 file with optional line range |
| `list_files` | List project files (skips VCS/dependency/cache/secret paths) |
| `search` | Literal text search |
| `find_files` | Glob-pattern file finder (`**/*.py`) |
| `regex_search` | Regular-expression search (case-sensitive) |
| `write_file` | Create/replace a file (diff preview + approval) |
| `edit_file` | Exact unique-substring edit (diff preview + approval) |
| `rename_symbol` | **new** AST-aware Python rename (definitions + references, never `obj.attr`) |
| `list_symbols` / `find_references` | **new** Python symbol map and identifier lookups |
| `run_tests` | **new** Runs the configured `test_cmd` (default `python -m pytest -x -q`) |
| `web_fetch` | **new** HTTP GET → readable text (HTML stripped to plain text) |
| `spawn_agents` | **new** 1–3 parallel read-only research sub-agents |
| `mcp__server__tool` | **new** Any tool exposed by your configured MCP servers |
| `shell` | Execute a command with timeout and exit-code capture |

Root and ancestor `AGENTS.md` files are loaded each turn (disable with `--no-instructions`). Read nested `AGENTS.md` files before modifying their directories. Git changes can be inspected with `/diff` (or the side-by-side `/diff --side`), the agent's last file change reverts with `/undo`, and every mutation is recoverable through `/restore` checkpoints.

### Approval policies

| Policy | Behavior |
| --- | --- |
| `ask` (default) | Automatically reads project files; asks before each write or shell command. Noninteractive input cannot grant approval. |
| `read-only` | Reads, lists, and searches; denies writes and all shell commands. |
| `auto` | Grants file writes and arbitrary shell commands without asking. Use only with trusted work in an isolated environment. |

Shell allowlists soften `ask` mode: `oac chat --allow "git status,git diff,pytest"` auto-approves those command prefixes (word-prefix matching — `evil` does not match `e`) while everything else still prompts. `run_tests` is auto-approved for safe selectors (paths, `-k expr`); arbitrary arguments still follow the approval policy.

**Approval controls are not an OS sandbox.** Shell commands run with your user privileges, inherit your environment, and can access the filesystem and network outside the project. File tools restrict paths to the project, reject symlink escapes, and block common credential and metadata paths. MCP tools run in server processes you configure with the same privileges. These safeguards do not comprehensively identify secrets; any allowed file content may be sent to the configured model provider. Use an isolated checkout/container and scoped credentials for untrusted tasks. Restore points and `/undo` cover agent file writes, not arbitrary shell side effects; inspect Git diffs before accepting changes.

### Plan mode

```
oac ❯ /plan                      · plan mode armed
oac ❯ add CSV export to the report page
     Proposed plan:
       ☐  1. Survey the report module and tests
       ☐  2. Add a csv() renderer
       ☐  3. Wire the export route
       ☐  4. Extend and run tests
     · /plan approve executes · /plan abort discards · or send feedback to revise
oac ❯ /plan approve
     ━━ plan step 1/4 ──── Survey the report module and tests
     ...
```

Drafting is read-only. Steps execute one at a time against the same session, so the agent keeps full context; failures pause the run (`/plan next` retries, `/plan skip N` moves on).

### MCP servers

Add servers to `~/.config/oac/config.json` (or your `OAC_CONFIG` path):

```json
{
  "mcp_servers": {
    "memory": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-memory"]},
    "fs": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp/data"]}
  }
}
```

oac performs the stdio JSON-RPC handshake (`initialize` → `tools/list`), bridges each remote tool into the agent loop, restarts dead servers once, and reports status via `oac mcp`, `oac mcp --test NAME`, or `/mcp`. Tools whose annotations declare `readOnlyHint` run without prompts; everything else follows the approval policy. MCP requires an installed `command` (e.g. Node for `npx`) — oac itself stays dependency-free.

### REPL commands

| Command | Description |
| --- | --- |
| `/help` | Command reference |
| `/new` | Start a fresh session in this project |
| `/model ID` | Switch the active model |
| `/models` | List models on the API |
| `/context` | Context budget with a usage bar |
| `/usage` | Token usage, requests, and estimated cost |
| `/undo` | Revert the agent's last file change |
| `/retry` | Re-run the previous prompt |
| `/diff [--side]` | Show Git changes; `--side` renders two columns |
| `/compact` | Keep only the latest turn in context |
| `/plan [approve\|next\|skip\|abort]` | Plan-first workflow (see above) |
| `/fork [N]` | Branch this session from user turn N |
| `/restore [ID [FILE]]` | Roll files back to a restore point |
| `/mcp` | MCP server and tool status |
| `/editor` | Compose the next prompt in `$EDITOR` (Ctrl-E) |
| `/export [PATH]` | Save the transcript as styled HTML |
| `/notify [off\|bell\|desktop]` | Completion alerts for long tasks |
| `/fetch URL` | Fetch a web page as readable text |
| `/theme [name]` | Show or switch the color theme |
| `/title TEXT` | Set this session's title |
| `/sessions` | List sessions for this project |
| `/copy` | Copy the last answer to the clipboard (OSC52) |
| `/save [PATH]` | Save the transcript as Markdown |
| `/exit` | Leave the REPL (Ctrl-D also works; Ctrl-C aborts the current turn) |

Input history persists between runs; Tab completes fuzzy-matched `/commands`, project paths, and `@image` attachments. Images (`@shot.png`) attach to the next prompt for vision models; up to 4 per message, 5 MB each.

## Themes

```sh
oac theme            # show the list
oac theme dracula    # set and save
```

Also switchable per-run (`oac chat --theme nord`) or live (`/theme nord`), or via the `OAC_THEME` environment variable. `--no-color`/`NO_COLOR` disables all styling; `FORCE_COLOR={1,2,3}` forces a capability level. Piping output automatically drops colors.

## Token usage and cost

`/usage` shows this run's requests and per-model breakdown; `oac stats` aggregates the persistent ledger across sessions (per-day bars, per-model and per-project tables, cost where pricing is configured). Add prices to `~/.config/oac/config.json`:

```json
{
  "pricing": {
    "gpt-4o": { "input": 2.5, "output": 10.0 },
    "claude": { "input": 3.0, "output": 15.0 }
  }
}
```

Rates are USD per million tokens; the key matches any model id containing it. Usage counts only responses whose server actually reported usage.

## Automatic model and context discovery

On startup the CLI queries `GET /models`. With `model=auto`, it filters obvious embedding, image, speech, and moderation models, then deterministically prefers coding/chat/instruct model names, advertised context size, and model ID. This is a heuristic, **not** a capability or quality benchmark. Pin `--model ID` when a gateway lists incompatible or unwanted models. There is no silent model failover that could send code to a different provider.

Context limits are read from model-list metadata, then from `GET /models/{id}` when missing. Recognized fields include `context_window`, `context_length`, `max_context_length`, `max_model_len`, and `max_position_embeddings`, including common nested metadata objects. Advertised output limits are also respected.

Many OpenAI-compatible APIs, including the standard OpenAI model listing, **do not expose context limits**. The CLI reports `unknown` and uses an 8192-token fallback budget; it never invents a discovered limit based on the model name. Configure the actual limit with `--context-window`, `OAC_CONTEXT_WINDOW`, or `init --context-window`. If even the fallback is too large for your model, lower it explicitly. Explicit `--model` also works when model discovery is unavailable.

Context management estimates tokens conservatively from UTF-8 bytes, accounts for tool schemas and output headroom, and drops whole earlier turns from requests while preserving saved session history and tool call/result pairs. When `auto_compact` (default 0.75) is exceeded after a turn, earlier messages are summarized by the model and replaced in the session — the latest turn is never cut. This is an estimate, not exact provider tokenization. No probing requests deliberately consume a model's full context window.

Transient API failures (429, 500, 502, 503, 504, and connection errors) are retried automatically with exponential backoff and jitter, up to `retries` times (default 3). Servers that send `Retry-After` are honored up to 30 seconds. Configure via `retries` and `retry_backoff` in the config file.

Configuration precedence: command flags > `OPENAI_BASE_URL`, `OAC_MODEL`, `OAC_API_MODE`, `OAC_CONTEXT_WINDOW`, `OAC_THEME`, `OAC_PROFILE` > config file > defaults. To change credential variable names, use `init --api-key-env YOUR_KEY_VARIABLE`. Other config fields include `timeout`, `max_steps`, `max_output_tokens`, `theme`, `retries`, `retry_backoff`, `pricing`, `profiles`, `allow`, `audit`, `notify`, `notify_min_seconds`, `auto_compact`, `test_cmd`, `agents_md`, and `mcp_servers`.

Chat Completions streams text and assembles incremental tool calls. Responses mode currently buffers each response; image parts are converted to `input_image`. `--no-stream` works around servers without SSE support. `--no-render` prints the raw token stream; by default markdown renders progressively as it streams. `--json` outputs the final answer, session ID, and selected model; operational messages go to stderr. `--max-output-tokens` controls the response budget. Sessions save after each completed tool round and final response, and resume only in the original project directory.

## Platform support

| Platform | Status |
| --- | --- |
| Linux | Verified with Python 3.12: installation, all 112 tests, and end-to-end CLI smoke tests against a local API (including an MCP handshake against a mock stdio server). Hosted-provider access has not been verified. |
| Windows (native) | Partial. Shell timeout and interruption cleanup use Unix-specific process controls (`os.killpg`, `signal.SIGKILL`), the standard-library `readline` is unavailable (no history/completion, use `/editor` instead of Ctrl-E), and MCP stdio reads fall back to blocking mode. |

The commands above use Linux shell syntax and virtual-environment paths (`.venv/bin/`). Native Windows uses `.venv\Scripts\python.exe` and `.venv\Scripts\oac.exe`, with different activation and environment-variable syntax.

## Development and verification

```sh
python3 -m unittest discover -s tests -v
python3 -m oac --help
```

Tests (112) run local HTTP servers and exercise actual API requests, model/context discovery, streaming tool calls, file changes, retry/backoff, stream usage capture, Responses conversion, JSON CLI output, session restore/titles/forking, approval denial and allowlists, path protection, context trimming and auto-compaction, shell timeouts, undo journaling, restore points, the audit log, HTML export, session search, the usage ledger, plan mode end-to-end, read-only sub-agents, image input, streaming markdown, side-by-side diffs, fuzzy matching, profiles/presets, and a full MCP stdio handshake against a mock server. They do not require a paid API or credentials.

Not yet implemented: browser/computer-use tools, audio input, IDE integration, provider OAuth login, native OS sandboxing, exact tokenizer accounting, or full provider-specific reasoning-item replay. Some reasoning models require that replay in Responses mode; use Chat Completions or a compatible non-reasoning model until supported. No hosted endpoint has been validated without user-provided API configuration.

## Changelog

See [CHANGELOG.md](CHANGELOG.md).
