"""Token usage and cost tracking for API calls."""
from __future__ import annotations


def format_cost(amount: float) -> str:
    return f"${amount:.4f}" if amount < 1 else f"${amount:.2f}"


def match_pricing(pricing: dict, model: str) -> dict | None:
    """Find a pricing entry whose key is a case-insensitive substring of the model id."""
    for key, rate in (pricing or {}).items():
        if key.lower() in model.lower():
            return rate
    return None


class Usage:
    """Accumulates prompt/completion token counts per model."""

    def __init__(self) -> None:
        self.requests = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.models: dict[str, list[int]] = {}

    def add(self, model: str, prompt: int | None, completion: int | None) -> None:
        try:
            prompt = max(0, int(prompt or 0))
            completion = max(0, int(completion or 0))
        except (TypeError, ValueError):
            return
        if prompt == 0 and completion == 0:
            return
        self.requests += 1
        self.prompt_tokens += prompt
        self.completion_tokens += completion
        entry = self.models.setdefault(model, [0, 0, 0])
        entry[0] += prompt
        entry[1] += completion
        entry[2] += 1

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def cost(self, pricing: dict) -> float | None:
        """Estimated cost in USD; None when no pricing entry matches any model."""
        total, matched = 0.0, False
        for model, (prompt, completion, _count) in self.models.items():
            rate = match_pricing(pricing, model)
            if rate:
                matched = True
                total += prompt / 1e6 * float(rate.get("input", 0))
                total += completion / 1e6 * float(rate.get("output", 0))
        return total if matched else None
