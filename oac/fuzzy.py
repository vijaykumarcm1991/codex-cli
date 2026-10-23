"""Tiny fuzzy matcher (fzf-style subsequence scoring) for the / command palette."""
from __future__ import annotations

from typing import Iterable


def score(query: str, target: str) -> int | None:
    """Subsequence match score; None when query is not a subsequence of target.

    Bonuses: consecutive matches, word starts (after ``/ - _ . space`` or at 0),
    and exact prefix matches. Earlier matches and shorter targets score higher.
    """
    query, target = query.lower(), target.lower()
    if not query:
        return 0
    if query in target:
        bonus = 12 if target.startswith(query) else 6
        return 100 + bonus - (len(target) - len(query))
    total = 0
    cursor = 0
    streak = 0
    first = -1
    for index, char in enumerate(query):
        found = target.find(char, cursor)
        if found < 0:
            return None
        streak = streak + 1 if found == cursor else 0
        total += 1 + 2 * streak
        if found == 0 or target[found - 1] in "/-_./ ":
            total += 4
        if first < 0:
            first = found
        cursor = found + 1
    return total - first - (len(target) - len(query)) // 2


def rank(query: str, targets: Iterable[str], limit: int = 8) -> list[tuple[int, str]]:
    """Targets ranked by fuzzy score, best first; empty when nothing matches."""
    scored = []
    for target in targets:
        value = score(query, target)
        if value is not None:
            scored.append((value, target))
    scored.sort(key=lambda pair: (-pair[0], pair[1]))
    return scored[:limit]
