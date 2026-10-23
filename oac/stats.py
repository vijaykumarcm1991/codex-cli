"""Persistent usage ledger plus the ``oac stats`` dashboard."""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import state_path
from .usage import format_cost, match_pricing

LEDGER = "usage.jsonl"


def record(model: str, prompt: int, completion: int, session: str = "-", project: str = "-",
           pricing: dict | None = None) -> None:
    """Append one usage entry to the global ledger (never raises)."""
    entry = {"date": datetime.now(timezone.utc).date().isoformat(),
             "model": model, "prompt": int(prompt), "completion": int(completion),
             "session": session, "project": str(project)}
    rate = match_pricing(pricing or {}, model)
    if rate:
        entry["cost"] = round(prompt / 1e6 * float(rate.get("input", 0))
                              + completion / 1e6 * float(rate.get("output", 0)), 6)
    try:
        path = state_path() / LEDGER
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as stream:
            stream.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass


def load(days: int | None = None) -> list[dict]:
    path = state_path() / LEDGER
    if not path.is_file():
        return []
    try:
        lines = path.read_text(errors="replace").splitlines()
    except OSError:
        return []
    entries = []
    for line in lines:
        try:
            entries.append(json.loads(line))
        except ValueError:
            continue
    if days is not None and days <= 0:
        return []
    if days:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days - 1)).date().isoformat()
        entries = [e for e in entries if str(e.get("date", "")) >= cutoff]
    return entries


def dashboard(entries: list[dict], bar_fn=lambda f: "█" * int(f * 10 + 0.5)) -> str:
    """Render totals, per-day bars, and per-model/project tables as plain lines."""
    if not entries:
        return "No recorded usage yet. Totals appear after API calls report tokens."
    prompt = sum(int(e.get("prompt", 0)) for e in entries)
    completion = sum(int(e.get("completion", 0)) for e in entries)
    requests = len(entries)
    cost = sum(float(e.get("cost", 0)) for e in entries if e.get("cost") is not None)
    lines = [f"requests   {requests:,}",
             f"prompt     {prompt:,} tokens",
             f"output     {completion:,} tokens",
             f"total      {prompt + completion:,} tokens",
             f"cost       {format_cost(cost)} (only models with configured pricing)"]

    by_day: dict[str, int] = defaultdict(int)
    for entry in entries:
        by_day[str(entry.get("date", "?"))] += int(entry.get("prompt", 0)) + int(entry.get("completion", 0))
    if len(by_day) > 1 or next(iter(by_day), "") != datetime.now(timezone.utc).date().isoformat() or len(entries) > 1:
        lines.append("")
        peak = max(by_day.values()) or 1
        for day in sorted(by_day)[-14:]:
            lines.append(f"{day}  {bar_fn(by_day[day] / peak):<12} {by_day[day]:,}")

    models: dict[str, list[int]] = defaultdict(lambda: [0, 0, 0])
    for entry in entries:
        bucket = models[str(entry.get("model", "?"))]
        bucket[0] += int(entry.get("prompt", 0))
        bucket[1] += int(entry.get("completion", 0))
        bucket[2] += 1
    lines.append("")
    lines.append("model                                      prompt      output    calls")
    for model, (p, c, n) in sorted(models.items(), key=lambda item: -item[1][0])[:12]:
        lines.append(f"{model[:40]:<40} {p:>10,} {c:>10,} {n:>7,}")

    projects: dict[str, int] = defaultdict(int)
    for entry in entries:
        projects[str(entry.get("project", "?"))] += int(entry.get("prompt", 0)) + int(entry.get("completion", 0))
    if len(projects) > 1:
        lines.append("")
        lines.append("project tokens (top 8)")
        for project, tokens in sorted(projects.items(), key=lambda item: -item[1])[:8]:
            lines.append(f"  {tokens:>12,}  {project}")
    return "\n".join(lines)
