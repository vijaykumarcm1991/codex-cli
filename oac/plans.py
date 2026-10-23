"""Plan mode: the agent drafts a numbered plan, the user approves, steps execute."""
from __future__ import annotations

import re

PLAN_FENCE = re.compile(r"```plan[ \t]*\n(.*?)```", re.DOTALL | re.IGNORECASE)
NUMBERED = re.compile(r"^\s*(\d+)[.)]\s+(.{3,})$")
MAX_STEPS = 12

PLAN_INSTRUCTIONS = """
PLAN MODE: You are drafting an implementation plan, not making changes yet.
Do not call any tools that modify files. Inspect the project only if needed.
Reply with a numbered plan of 2-8 concrete steps, wrapped in a fenced code block
that starts with ```plan and ends with ```. Each step is one line starting with
its number, describing a single verifiable action. No prose outside the block.
"""


def parse_plan(answer: str) -> list[str]:
    """Extract plan steps from a ```plan fence, falling back to numbered lines."""
    match = PLAN_FENCE.search(answer)
    body = match.group(1) if match else answer
    steps = []
    for line in body.splitlines():
        found = NUMBERED.match(line)
        if found:
            step = found.group(2).strip()
            if step and step not in steps:
                steps.append(step)
        elif steps and line.strip() and not match:
            # tolerate a plan written as a plain numbered list with wrapped lines
            steps[-1] = (steps[-1] + " " + line.strip()).strip()
    return steps[:MAX_STEPS]


class PlanState:
    """Tracks one plan lifecycle: draft -> review -> executing -> done."""

    def __init__(self) -> None:
        self.steps: list[str] = []
        self.completed: list[bool] = []
        self.status = "off"  # off | review | executing
        self.feedback = ""

    def set_plan(self, steps: list[str]) -> None:
        self.steps = steps
        self.completed = [False] * len(steps)
        self.status = "review" if steps else "off"

    def pending(self) -> list[int]:
        return [i for i, done in enumerate(self.completed) if not done]

    def start(self) -> bool:
        if self.status == "review" and self.steps:
            self.status = "executing"
            return True
        return False

    def mark(self, index: int, done: bool = True) -> None:
        if 0 <= index < len(self.completed):
            self.completed[index] = done

    def abort(self) -> None:
        self.steps, self.completed, self.status = [], [], "off"

    def summary(self) -> str:
        if self.status == "off":
            return "off — /plan toggles planning; your next request drafts a plan for review"
        if self.status == "review":
            if not self.steps:
                return "drafting — no plan captured yet"
            return f"review — {len(self.steps)} steps ready; /plan approve to execute, /plan off to discard"
        done = sum(self.completed)
        return f"executing — {done}/{len(self.steps)} steps complete; /plan skip N or /plan abort"
