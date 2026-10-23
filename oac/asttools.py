"""AST-powered Python navigation and editing for oac.

Symbol listing, reference finding, and token-accurate renaming using only the
standard library (``ast`` + ``tokenize``). Renames skip attribute accesses so
``obj.name`` is never touched when renaming a module-level ``name``.
"""
from __future__ import annotations

import ast
import io
import tokenize
from pathlib import Path


class SymbolError(ValueError):
    pass


def _load(source: str) -> ast.Module:
    try:
        return ast.parse(source)
    except SyntaxError as exc:
        raise SymbolError(f"Cannot parse Python source: {exc}") from None


def list_symbols(source: str) -> list[dict]:
    """Return top-level and class-level symbols: name, kind, start/end lines."""
    tree = _load(source)
    result: list[dict] = []

    def visit(node: ast.AST, prefix: str, depth: int) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                result.append({"name": prefix + child.name, "kind": "async def" if isinstance(child, ast.AsyncFunctionDef) else "def",
                               "line": child.lineno, "end_line": child.end_lineno or child.lineno})
            elif isinstance(child, ast.ClassDef):
                result.append({"name": prefix + child.name, "kind": "class",
                               "line": child.lineno, "end_line": child.end_lineno or child.lineno})
                if depth < 1:  # one nesting level: methods, no deep recursion
                    visit(child, prefix + child.name + ".", depth + 1)

    visit(tree, "", 0)
    return result


def _name_tokens(source: str, target: str) -> list[tuple[int, int]]:
    """Positions (row, col) of NAME tokens equal to target that are plain identifiers."""
    positions: list[tuple[int, int]] = []
    previous_significant = ""
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
    except tokenize.TokenError as exc:
        raise SymbolError(f"Cannot tokenize source: {exc}") from None
    for token in tokens:
        if token.type == tokenize.NAME:
            if token.string == target and previous_significant != ".":
                positions.append(token.start)
            previous_significant = token.string
        elif token.type not in {tokenize.NL, tokenize.NEWLINE, tokenize.COMMENT, tokenize.INDENT, tokenize.DEDENT}:
            previous_significant = token.string
    return positions


def find_references(source: str, name: str, path: str = "") -> list[dict]:
    """All non-attribute occurrences of ``name`` with line content."""
    if not name.isidentifier():
        raise SymbolError(f"{name!r} is not a valid Python identifier")
    lines = source.splitlines()
    references = []
    for row, col in _name_tokens(source, name):
        line = lines[row - 1] if row - 1 < len(lines) else ""
        references.append({"line": row, "col": col + 1, "text": line.strip()[:200],
                           "definition": line.lstrip().startswith(("def ", "async def ", "class "))})
    if path:
        for reference in references:
            reference["path"] = path
    return references


def rename_symbol(source: str, old: str, new: str) -> tuple[str, int]:
    """Rename every non-attribute occurrence of ``old``; returns (new_source, count)."""
    if not old.isidentifier() or not new.isidentifier():
        raise SymbolError("Both old and new names must be Python identifiers")
    if old in __import__("keyword").kwlist:
        raise SymbolError("Cannot rename a Python keyword")
    positions = _name_tokens(source, old)
    if not positions:
        raise SymbolError(f"No occurrences of {old!r} found in this file")
    lines = source.splitlines(keepends=True)
    for row, col in sorted(positions, reverse=True):
        line = lines[row - 1]
        lines[row - 1] = line[:col] + new + line[col + len(old):]
    return "".join(lines), len(positions)
