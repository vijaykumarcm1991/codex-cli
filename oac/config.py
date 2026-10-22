from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, fields
from pathlib import Path


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

    @classmethod
    def load(cls) -> Config:
        path = config_path()
        data = json.loads(path.read_text()) if path.exists() else {}
        allowed = {field.name for field in fields(cls)}
        result = cls(**{key: value for key, value in data.items() if key in allowed})
        for name, attr in [("OPENAI_BASE_URL", "base_url"), ("OAC_MODEL", "model"), ("OAC_API_MODE", "api_mode")]:
            if os.environ.get(name):
                setattr(result, attr, os.environ[name])
        if os.environ.get("OAC_CONTEXT_WINDOW"):
            result.context_window = int(os.environ["OAC_CONTEXT_WINDOW"])
        result.validate()
        return result

    def validate(self) -> None:
        if self.api_mode not in {"chat", "responses"}:
            raise ValueError("api_mode must be chat or responses")
        if self.max_output_tokens < 1 or self.max_steps < 1 or self.timeout <= 0:
            raise ValueError("Token budget, step limit, and timeout must be positive")
        if self.context_window is not None and self.context_window < 128:
            raise ValueError("context_window must be at least 128 tokens")

    def save(self) -> None:
        self.validate()
        atomic_json(config_path(), asdict(self))
