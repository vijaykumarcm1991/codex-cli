# Open Agent CLI (`oac`)

A terminal coding agent for OpenAI-compatible APIs, built with Python 3.11+ and no runtime dependencies. Works with hosted gateways and local servers that implement Chat Completions with function calling, or the Responses API.

This is an initial implementation of the core coding-agent workflow. It does not claim full Codex or Claude Code feature parity.

## Platform support

| Platform | Status |
| --- | --- |
| Linux | Verified with Python 3.12: installation, all 19 tests, and an end-to-end CLI smoke test against a local API passed. Hosted-provider access has not been verified. |
| Windows (native) | Not fully supported or tested. Shell timeout and interruption cleanup uses Unix-specific process controls (`os.killpg` and `signal.SIGKILL`). These need a Windows-compatible implementation and testing before native Windows support can be claimed. |

The commands below use Linux shell syntax and virtual-environment paths (`.venv/bin/`). Native Windows uses `.venv\Scripts\python.exe` and `.venv\Scripts\oac.exe`, with different activation and environment-variable syntax. Changing those paths alone does not resolve the shell process-control limitations.

## Install and configure

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/oac init --base-url https://api.openai.com/v1 --model auto
export OPENAI_API_KEY='your-key'
.venv/bin/oac models
.venv/bin/oac doctor
.venv/bin/oac chat
```

Set credentials locally in your shell or secret manager; never commit them. `init` stores the **name** of the credential variable, never its value. Config lives in `~/.config/oac/config.json`; override with `OAC_CONFIG`. Sessions live in `~/.local/state/oac/sessions`; override with `OAC_STATE_DIR`. Session files contain prompts, code, and tool output, and are written with owner-only permissions.

Local server example:

```sh
oac init --base-url http://localhost:8000/v1 --model auto
oac models --json
oac chat --context-window 32768
```

Gateways with the Responses API:

```sh
oac init --base-url https://your-gateway.example/v1 --api-mode responses
```

The configured URL is the API prefix, usually ending in `/v1`. Remote URLs require HTTPS; loopback servers can use HTTP. TLS validation remains enabled, redirects are rejected, and API keys are sent only to the configured endpoint. A server must support function calling for agent tools; listing models alone does not establish that support. `doctor` makes a real text completion, which may incur provider charges.

## Coding workflow

```sh
oac chat --cwd /path/to/project
oac run 'Inspect the project and explain its architecture' --approval read-only
oac run 'Fix the failing tests and run them' --max-steps 40
printf 'Summarize the README' | oac run --approval read-only --json
oac sessions
oac chat --resume SESSION_ID --cwd /path/to/original/project
```

Available tools: file reading, file listing, literal text search, file creation, exact substring editing with diff previews, and shell execution with timeouts and exit codes. Root and ancestor `AGENTS.md` files are loaded each turn. Read nested `AGENTS.md` files before modifying their directories. Git changes can be inspected with `/diff`. The agent can run builds, tests, Git commands, and other installed tools through approved shell commands.

Approval policies:

| Policy | Behavior |
| --- | --- |
| `ask` (default) | Automatically reads project files; asks before each write or shell command. Noninteractive input cannot grant approval. |
| `read-only` | Reads, lists, and searches; denies writes and all shell commands. |
| `auto` | Grants file writes and arbitrary shell commands without asking. Use only with trusted work in an isolated environment. |

**Approval controls are not an OS sandbox.** Shell commands run with your user privileges, inherit your environment, and can access the filesystem and network outside the project. File tools restrict paths to the project, reject symlink escapes, and block common credential and metadata paths. These safeguards do not comprehensively identify secrets; any allowed file content may be sent to the configured model provider. Use an isolated checkout/container and scoped credentials for untrusted tasks. There is no automatic rollback; inspect Git diffs before accepting changes.

Interactive commands: `/help`, `/new`, `/model ID`, `/models`, `/context`, `/diff`, `/compact`, `/exit`. `/compact` discards earlier turns and keeps the latest user turn and its tool results; it does not generate a summary. Ctrl-C exits with status 130; shell execution is bounded to 300 seconds and its process group is terminated on timeout or interruption.

Chat Completions streams text and assembles incremental tool calls. Responses mode currently buffers each response. `--no-stream` works around servers without SSE support. `--json` outputs the final answer, session ID, and selected model; operational messages go to stderr. `--max-output-tokens` controls the response budget. Sessions save after each completed tool round and final response, and resume only in the original project directory.

## Automatic model and context discovery

On startup the CLI queries `GET /models`. With `model=auto`, it filters obvious embedding, image, speech, and moderation models, then deterministically prefers coding/chat/instruct model names, advertised context size, and model ID. This is a heuristic, **not** a capability or quality benchmark. Pin `--model ID` when a gateway lists incompatible or unwanted models. There is no silent model failover that could send code to a different provider.

Context limits are read from model-list metadata, then from `GET /models/{id}` when missing. Recognized fields include `context_window`, `context_length`, `max_context_length`, `max_model_len`, and `max_position_embeddings`, including common nested metadata objects. Advertised output limits are also respected.

Many OpenAI-compatible APIs, including the standard OpenAI model listing, **do not expose context limits**. The CLI reports `unknown` and uses an 8192-token fallback budget; it never invents a discovered limit based on the model name. Configure the actual limit with `--context-window`, `OAC_CONTEXT_WINDOW`, or `init --context-window`. If even the fallback is too large for your model, lower it explicitly. Explicit `--model` also works when model discovery is unavailable.

Context management estimates tokens conservatively from UTF-8 bytes, accounts for tool schemas and output headroom, and drops whole earlier turns from requests while preserving saved session history and tool call/result pairs. The latest turn is never silently cut; an oversized turn produces an actionable error. This is an estimate, not exact provider tokenization. No probing requests deliberately consume a model's full context window.

Configuration precedence: command flags > `OPENAI_BASE_URL`, `OAC_MODEL`, `OAC_API_MODE`, `OAC_CONTEXT_WINDOW` > config file > defaults. To change credential variable names, use `init --api-key-env YOUR_KEY_VARIABLE`. Other config fields include `timeout`, `max_steps`, and `max_output_tokens`.

## Development and verification

```sh
python3 -m unittest discover -s tests -v
python3 -m oac --help
```

Tests run a local HTTP server and exercise actual API requests, model/context discovery, streaming tool calls, file changes, Responses conversion, JSON CLI output, session restore, approval denial, path protection, context trimming, and shell timeouts. They do not require a paid API or credentials.

Not yet implemented: MCP servers, browser/computer tools, image/audio input, subagents, IDE integration, provider OAuth login, native OS sandboxing, exact tokenizer accounting, or full provider-specific reasoning-item replay. Some reasoning models require that replay in Responses mode; use Chat Completions or a compatible non-reasoning model until supported. No hosted endpoint has been validated without user-provided API configuration.
