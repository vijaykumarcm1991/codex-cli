"""Read-only research sub-agents that run inside the main agent's tool loop."""
from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from typing import Callable

from .agent import SYSTEM, fit_context
from .tools import SCHEMAS, Tools, function

# Sub-agents see read-only tools only; writes are denied by policy as well.
READ_ONLY_SCHEMAS = [spec for spec in SCHEMAS
                     if spec["function"]["name"] in {"read_file", "list_files", "search", "find_files", "regex_search"}]

SPAWN_SCHEMA = function(
    "spawn_agents",
    "Spawn 1-3 read-only research sub-agents that inspect the project in parallel "
    "(reading files and searching) and report findings. They cannot modify files or "
    "run commands. Use them to explore unfamiliar code or verify assumptions while "
    "you keep working; each prompt should be a self-contained research question.",
    {"prompts": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 3},
     "focus": {"type": "string", "description": "Optional hint: file or directory each agent should start from"}},
    ["prompts"],
)

SUB_SYSTEM = SYSTEM + """
You are a READ-ONLY research sub-agent. Never attempt to create, edit, or delete
files, and never run shell commands; such requests fail by design. Inspect the
project (read/search tools only) and answer the research question concisely with
concrete file paths and line references. Reply with findings, not commentary.
"""


def _run_one(client, model, root: str, prompt: str, max_turns: int) -> str:
    tools = Tools(root, approval="read-only")
    messages = [{"role": "user", "content": prompt}]
    for _ in range(max_turns):
        context = model.context_window or 8192
        tool_cost = 400  # read-only subset; small fixed overhead estimate
        trimmed, _removed = fit_context([{"role": "system", "content": SUB_SYSTEM}, *messages],
                                        context - tool_cost, 2048)
        response = client.complete(model, trimmed, READ_ONLY_SCHEMAS, None)
        response["role"] = "assistant"
        messages.append(response)
        calls = response.get("tool_calls") or []
        if not calls:
            return (response.get("content") or "").strip() or "(no findings)"
        for call in calls:
            fn = call.get("function", {})
            result = tools.execute(fn.get("name", ""), fn.get("arguments", "{}"))
            messages.append({"role": "tool", "tool_call_id": call.get("id", ""), "content": result})
    return "(sub-agent hit its step limit before reporting findings)"


def spawn(client, model, root, prompts: list[str], max_turns: int = 8,
          status: Callable[[str], None] | None = None) -> str:
    """Run research prompts as read-only agents; returns their combined findings."""
    prompts = [re.sub(r"\s+", " ", p).strip() for p in prompts if p and p.strip()][:3]
    if not prompts:
        raise ValueError("spawn_agents needs at least one non-empty prompt")
    if len(prompts) == 1:
        results = [_run_one(client, model, root, prompts[0], max_turns)]
    else:
        with ThreadPoolExecutor(max_workers=len(prompts)) as pool:
            results = list(pool.map(lambda p: _run_one(client, model, root, p, max_turns), prompts))
    sections = []
    for prompt, result in zip(prompts, results):
        heading = prompt if len(prompt) <= 72 else prompt[:69] + "..."
        sections.append(f"## Research: {heading}\n\n{result}")
    return "\n\n".join(sections)
