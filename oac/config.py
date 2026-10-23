from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

from .ui import THEMES


def config_path() -> Path:
    return Path(os.environ.get("OAC_CONFIG", str(Path.home() / ".config/oac/config.json")))


def state_path() -> Path:
    return Path(os.environ.get("OAC_STATE_DIR", str(Path.home() / ".local/state/oac")))


def atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, indent=2, ensure_ascii=False)
            stream.write("\n")
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


# Local-server presets: OpenAI-compatible endpoints shipped by common tools.
PRESETS: dict[str, dict[str, str]] = {
    "ollama": {"base_url": "http://localhost:11434/v1", "api_key_env": "OLLAMA_API_KEY",
               "hint": "try: ollama pull qwen2.5-coder:7b"},
    "lmstudio": {"base_url": "http://localhost:1234/v1", "api_key_env": "LMSTUDIO_API_KEY",
                 "hint": "start the local server in LM Studio, then pick a loaded model"},
    "vllm": {"base_url": "http://localhost:8000/v1", "api_key_env": "VLLM_API_KEY",
             "hint": "vLLM serves an OpenAI-compatible API on port 8000"},
    "jan": {"base_url": "http://localhost:1337/v1", "api_key_env": "JAN_API_KEY",
            "hint": "Jan's local OpenAI-compatible server"},
    "llamacpp": {"base_url": "http://localhost:8080/v1", "api_key_env": "LLAMACPP_API_KEY",
                 "hint": "llama-server exposes /v1 for chat completions"},
}

# Keys a profile may override on top of the base configuration.
PROFILE_KEYS = {"base_url", "api_key_env", "model", "api_mode", "context_window"}


@dataclass
class Config:
    base_url: str = "https://api.openai.com/v1"
    api_key_env: str = "OPENAI_API_KEY"
    model: str = "auto"
    api_mode: str = "chat"
    context_window: int | None = None
    max_output_tokens: int = 4096
    timeout: float = 120
    max_steps: int = 30
    theme: str = "tokyo-night"
    retries: int = 3
    retry_backoff: float = 0.8
    pricing: dict[str, dict[str, float]] = field(default_factory=dict)
    # v0.3.0 additions
    profiles: dict[str, dict] = field(default_factory=dict)  # name -> PROFILE_KEYS overrides
    allow: list[str] = field(default_factory=list)  # auto-approved shell command prefixes
    audit: bool = True
    notify: str = "bell"  # off | bell | desktop | all
    notify_min_seconds: float = 8.0
    auto_compact: float = 0.75  # fraction of the context window that triggers a summary; 0 disables
    test_cmd: str = "python -m pytest -x -q"
    mcp_servers: dict[str, dict] = field(default_factory=dict)  # name -> {command, args, env}
    agents_md: bool = True  # auto-load AGENTS.md project instructions

    @classmethod
    def load(cls, profile: str | None = None) -> Config:
        path = config_path()
        data = json.loads(path.read_text()) if path.exists() else {}
        allowed = {field.name for field in fields(cls)}
        result = cls(**{key: value for key, value in data.items() if key in allowed})
        # Profile selection: explicit argument wins, then OAC_PROFILE.
        chosen = profile or os.environ.get("OAC_PROFILE") or ""
        if chosen:
            result.apply_profile(chosen)
        for name, attr in [("OPENAI_BASE_URL", "base_url"), ("OAC_MODEL", "model"), ("OAC_API_MODE", "api_mode"), ("OAC_THEME", "theme")]:
            if os.environ.get(name):
                setattr(result, attr, os.environ[name])
        if os.environ.get("OAC_CONTEXT_WINDOW"):
            result.context_window = int(os.environ["OAC_CONTEXT_WINDOW"])
        result.validate()
        return result

    def apply_profile(self, name: str) -> None:
        entry = self.profiles.get(name)
        if not isinstance(entry, dict):
            known = ", ".join(sorted(self.profiles)) or "none configured yet"
            raise ValueError(f"Unknown profile {name!r}. Available: {known}")
        for key, value in entry.items():
            if key in PROFILE_KEYS:
                setattr(self, key, value)

    def save_profile(self, name: str) -> None:
        """Store the current connection settings as a named profile."""
        if not name or not all(ch.isalnum() or ch in "-_" for ch in name) or len(name) > 40:
            raise ValueError("Profile names may use letters, digits, '-' and '_' (max 40 chars)")
        self.profiles[name] = {key: getattr(self, key) for key in sorted(PROFILE_KEYS)}
        self.save()

    def validate(self) -> None:
        if self.api_mode not in {"chat", "responses"}:
            raise ValueError("api_mode must be chat or responses")
        if self.max_output_tokens < 1 or self.max_steps < 1 or self.timeout <= 0:
            raise ValueError("Token budget, step limit, and timeout must be positive")
        if self.context_window is not None and self.context_window < 128:
            raise ValueError("context_window must be at least 128 tokens")
        if self.theme not in THEMES:
            raise ValueError(f"theme must be one of: {', '.join(sorted(THEMES))}")
        if not 0 <= self.retries <= 8:
            raise ValueError("retries must be between 0 and 8")
        if self.retry_backoff < 0:
            raise ValueError("retry_backoff must not be negative")
        if self.notify not in {"off", "bell", "desktop", "all"}:
            raise ValueError("notify must be off, bell, desktop, or all")
        if self.notify_min_seconds < 0 or self.auto_compact < 0 or self.auto_compact > 1:
            raise ValueError("notify_min_seconds and auto_compact must be sane positive values")
        if not str(self.test_cmd).strip():
            raise ValueError("test_cmd must not be empty")
        if not isinstance(self.allow, list) or not all(isinstance(item, str) for item in self.allow):
            raise ValueError("allow must be a list of command prefixes")
        for name, server in self.mcp_servers.items():
            if not isinstance(server, dict) or not str(server.get("command", "")).strip():
                raise ValueError(f"mcp server {name!r} needs a 'command' string")

    def save(self) -> None:
        self.validate()
        atomic_json(config_path(), asdict(self))
