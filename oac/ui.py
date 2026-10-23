"""Modern terminal rendering for oac.

Zero-dependency ANSI engine: theme system with truecolor/256/16 degradation,
markdown rendering, syntax-highlighted code blocks, tables, panels, progress
bars, a live spinner, and ANSI-aware word wrapping.
"""
from __future__ import annotations

import difflib
import os
import re
import shutil
import sys
import threading
from dataclasses import dataclass

RESET = "\x1b[0m"
BOLD = "\x1b[1m"
DIM = "\x1b[2m"
ITALIC = "\x1b[3m"
UNDERLINE = "\x1b[4m"

ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


# --------------------------------------------------------------------------
# Capability detection and color conversion
# --------------------------------------------------------------------------

def detect_level(stream=None) -> int:
    """Return supported color level: 0 none, 1 basic 16, 2 256-color, 3 truecolor."""
    stream = stream or sys.stdout
    if os.environ.get("NO_COLOR"):
        return 0
    force = os.environ.get("FORCE_COLOR")
    if force is not None:
        if force in ("", "0", "false", "no"):
            return 0
        return {"1": 1, "2": 2}.get(force, 3)
    if not hasattr(stream, "isatty") or not stream.isatty():
        return 0
    term = os.environ.get("TERM", "")
    if term == "dumb":
        return 0
    if os.environ.get("COLORTERM") in ("truecolor", "24bit", "yes", "true"):
        return 3
    if "256color" in term:
        return 2
    return 1


def _hex_rgb(value: str) -> tuple[int, int, int]:
    return tuple(int(value[i:i + 2], 16) for i in (1, 3, 5))  # type: ignore[return-value]


def _to_256(r: int, g: int, b: int) -> int:
    if r == g == b:
        if r < 8:
            return 16
        if r > 248:
            return 231
        return 232 + round((r - 8) / 247 * 23)
    def seg(x: int) -> int:
        return 0 if x < 48 else (1 if x < 115 else 2)
    return 16 + 36 * seg(r) + 6 * seg(g) + seg(b)


_ANSI16 = [
    (0, 0, 0), (205, 0, 0), (0, 205, 0), (205, 205, 0), (0, 0, 238), (205, 0, 205),
    (0, 205, 205), (229, 229, 229), (127, 127, 127), (255, 0, 0), (0, 255, 0),
    (255, 255, 0), (92, 92, 255), (255, 0, 255), (0, 255, 255), (255, 255, 255),
]


def _to_16(r: int, g: int, b: int) -> int:
    index, best = 0, 1 << 30
    for i, (cr, cg, cb) in enumerate(_ANSI16):
        distance = (r - cr) ** 2 + (g - cg) ** 2 + (b - cb) ** 2
        if distance < best:
            best, index = distance, i
    return index


def fg_code(color: str, level: int) -> str:
    if not color or level == 0:
        return ""
    r, g, b = _hex_rgb(color)
    if level >= 3:
        return f"\x1b[38;2;{r};{g};{b}m"
    if level == 2:
        return f"\x1b[38;5;{_to_256(r, g, b)}m"
    index = _to_16(r, g, b)
    return f"\x1b[{30 + index}m" if index < 8 else f"\x1b[{90 + index - 8}m"


def visible(text: str) -> str:
    """Strip ANSI escape sequences."""
    return ANSI_RE.sub("", text)


def visible_len(text: str) -> int:
    return len(visible(text))


def wrap_ansi(text: str, width: int) -> str:
    """Word-wrap styled text, counting only visible characters."""
    if width < 8:
        return text
    words, current, in_escape = [], "", False
    for ch in text:
        if in_escape:
            current += ch
            in_escape = ch != "m"
        elif ch == "\x1b":
            current += ch
            in_escape = True
        elif ch == " ":
            if current:
                words.append(current)
                current = ""
        else:
            current += ch
    if current:
        words.append(current)
    lines: list[str] = []
    line = ""
    for word in words:
        if not line:
            line = word
        elif visible_len(line) + 1 + visible_len(word) <= width:
            line += " " + word
        else:
            lines.append(line)
            line = word
    if line:
        lines.append(line)
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Themes
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Theme:
    name: str
    dark: bool
    primary: str
    accent: str
    success: str
    warning: str
    error: str
    muted: str
    bright: str
    link: str
    keyword: str
    string: str
    comment: str
    number: str
    func: str
    operator: str


THEMES: dict[str, Theme] = {
    "tokyo-night": Theme(
        "tokyo-night", True, "#7aa2f7", "#7dcfff", "#9ece6a", "#e0af68", "#f7768e",
        "#565f89", "#c0caf5", "#bb9af7", "#bb9af7", "#9ece6a", "#565f89", "#ff9e64",
        "#7aa2f7", "#89ddff",
    ),
    "dracula": Theme(
        "dracula", True, "#bd93f9", "#8be9fd", "#50fa7b", "#f1fa8c", "#ff5555",
        "#6272a4", "#f8f8f2", "#ff79c6", "#ff79c6", "#f1fa8c", "#6272a4", "#bd93f9",
        "#50fa7b", "#ff79c6",
    ),
    "nord": Theme(
        "nord", True, "#88c0d0", "#8fbcbb", "#a3be8c", "#ebcb8b", "#bf616a",
        "#616e88", "#eceff4", "#b48ead", "#81a1c1", "#a3be8c", "#616e88", "#b48ead",
        "#88c0d0", "#81a1c1",
    ),
    "light": Theme(
        "light", False, "#0969da", "#8250df", "#1a7f37", "#9a6700", "#cf222e",
        "#6e7781", "#24292f", "#0969da", "#cf222e", "#0a3069", "#6e7781", "#0550ae",
        "#8250df", "#0550ae",
    ),
    "mono": Theme(
        "mono", True, "", "", "", "", "", "", "", "", "", "", "", "", "", "",
    ),
}

DEFAULT_THEME = "tokyo-night"


# --------------------------------------------------------------------------
# Syntax highlighting
# --------------------------------------------------------------------------

KEYWORDS = {
    "python": set("""def class import from return if elif else for while try except
        finally with as pass break continue raise lambda yield global nonlocal assert
        del in is not and or None True False async await match case self cls print
        len range str int float dict list set tuple type isinstance super open""".split()),
    "javascript": set("""function const let var class return if else for while do try
        catch finally new typeof instanceof import export from async await this null
        undefined true false yield throw switch case break continue default delete
        void extends super static get set of in""".split()),
    "bash": set("""if then else elif fi for while until do done case esac in function
        return export local source echo cd sudo apt pip npm git python python3 node
        set shift read printf ls mkdir rm cp mv cat grep sed awk curl wget exit""".split()),
}

LANG_MAP = {
    "py": "python", "python": "python", "python3": "python",
    "js": "javascript", "javascript": "javascript", "jsx": "javascript",
    "ts": "javascript", "typescript": "javascript", "tsx": "javascript",
    "mjs": "javascript", "cjs": "javascript",
    "sh": "bash", "bash": "bash", "shell": "bash", "zsh": "bash",
    "console": "bash", "sh-session": "bash",
    "json": "json", "jsonc": "json",
}

TOKEN = re.compile(
    r"(?P<comment>\#[^\n]*|//[^\n]*)"
    r"|(?P<string>\"(?:\\.|[^\"\\\n])*\"|'(?:\\.|[^'\\\n])*'|`(?:\\.|[^`\\])*`)"
    r"|(?P<number>\b\d[\d_]*(?:\.\d+)?j?\b)"
    r"|(?P<name>[A-Za-z_$][A-Za-z0-9_$]*)"
    r"|(?P<punct>[{}()\[\],;:.])"
    r"|(?P<op>[+\-*/%=<>!&|^~?@]+)"
)


# --------------------------------------------------------------------------
# Markdown parsing
# --------------------------------------------------------------------------

FENCE = re.compile(r"^\s*```([\w+-]*)\s*$")
HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
HR = re.compile(r"^ {0,3}(?:-{3,}|\*{3,}|_{3,})\s*$")
UL = re.compile(r"^(\s*)[-*+]\s+(.*)$")
OL = re.compile(r"^(\s*)\d+[.)]\s+(.*)$")
QUOTE = re.compile(r"^>\s?(.*)$")
TABLE_ROW = re.compile(r"^\s*\|(.+)\|\s*$")
TABLE_SEP = re.compile(r"^\s*\|?[\s:|-]*-[\s:|-]*\|?\s*$")

INLINE = re.compile(r"(`[^`]+`)|(\*\*[^*`]+\*\*)|(\*[^*`]+\*)|(\[[^\]]+\]\([^)\s]+\))")

_LOGO = {
    "o": ["  ▄███▄ ", " ██   ██", " ██   ██", " ██   ██", "  ▀███▀ "],
    "a": ["   ██   ", "  ████  ", " ██  ██ ", " ██████ ", " ██  ██ "],
    "c": ["  ▄███▄ ", " ██  ▀█ ", " ██     ", " ██  ▄█ ", "  ▀███▀ "],
}


class Spinner:
    """Animated braille spinner on stderr; degrades to disabled without a tty."""

    FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

    def __init__(self, ui: "UI", label: str = "thinking", stream=None):
        self.ui = ui
        self.label = label
        self.stream = stream or sys.stderr
        self.enabled = (
            ui.level > 0
            and hasattr(self.stream, "isatty")
            and self.stream.isatty()
            and os.environ.get("CI") is None
            and os.environ.get("TERM") != "dumb"
        )
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._dirty = False

    def start(self, label: str | None = None) -> None:
        if not self.enabled or self._thread:
            return
        if label:
            self.label = label
        self._stop.clear()
        self._thread = threading.Thread(target=self._spin, daemon=True)
        self._thread.start()

    def _spin(self) -> None:
        index = 0
        while not self._stop.wait(0.09):
            frame = self.FRAMES[index % len(self.FRAMES)]
            index += 1
            with self._lock:
                self.stream.write(
                    "\r" + self.ui.s(frame, color=self.ui.theme.accent, bold=True)
                    + " " + self.ui.s(self.label, dim=True) + " ")
                self.stream.flush()
                self._dirty = True

    def tick(self, label: str) -> None:
        with self._lock:
            self.label = label

    def stop(self) -> None:
        if not self._thread:
            return
        self._stop.set()
        self._thread.join()
        self._thread = None
        if self._dirty:
            self.stream.write("\r\x1b[2K")
            self.stream.flush()
            self._dirty = False


class UI:
    """Render text, markdown, code, tables and panels in the active theme."""

    def __init__(self, theme: str | None = None, level: int | None = None, width: int | None = None):
        self.theme_name = theme if theme in THEMES else DEFAULT_THEME
        self.theme = THEMES[self.theme_name]
        self.level = detect_level() if level is None else level
        if self.level == 0:
            self.theme = THEMES["mono"]
            self.theme_name = "mono"
        columns = width or shutil.get_terminal_size((100, 24)).columns
        self.width = max(40, columns - 1)

    # -- primitives --------------------------------------------------------

    def set_theme(self, name: str) -> bool:
        if name not in THEMES or self.level == 0:
            return False
        self.theme_name = name
        self.theme = THEMES[name]
        return True

    def s(self, text: str, color: str | None = None, bold: bool = False,
          dim: bool = False, italic: bool = False, underline: bool = False) -> str:
        if self.level == 0:
            return text
        parts: list[str] = []
        if bold:
            parts.append(BOLD)
        if dim:
            parts.append(DIM)
        if italic:
            parts.append(ITALIC)
        if underline:
            parts.append(UNDERLINE)
        if color:
            code = fg_code(color, self.level)
            if code:
                parts.append(code)
        if not parts:
            return text
        return "".join(parts) + text + RESET

    def kv(self, key: str, value: str) -> str:
        return self.s(f"{key:<10}", dim=True) + self.s(str(value), color=self.theme.bright or None)

    def rule(self, title: str = "") -> str:
        if title:
            fill = max(1, self.width - len(title) - 6)
            return (self.s("──", dim=True) + " " + self.s(title, color=self.theme.accent)
                    + " " + self.s("─" * fill, dim=True))
        return self.s("─" * self.width, dim=True)

    def bar(self, fraction: float, width: int = 24) -> str:
        fraction = max(0.0, min(1.0, fraction))
        filled = round(fraction * width)
        color = self.theme.success if fraction < 0.8 else (self.theme.warning if fraction < 0.95 else self.theme.error)
        return (self.s("█" * filled, color=color) + self.s("░" * (width - filled), dim=True))

    def side_by_side(self, old: list[str], new: list[str],
                     old_title: str = "before", new_title: str = "after",
                     width: int | None = None, max_rows: int = 400) -> str:
        """Aligned two-column diff: red left removals, green right additions."""
        width = width or self.width
        if width < 70:
            return self.s("Terminal too narrow for side-by-side; use /diff (unified)", dim=True)
        column = max(16, (width - 12) // 2)
        rows = [self.s(f"{old_title[:column]:<{column + 6}}{new_title[:column]}", bold=True, color=self.theme.muted)]
        matcher = difflib.SequenceMatcher(a=old, b=new, autojunk=False)
        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            if tag == "equal":
                for k in range(i2 - i1):
                    rows.append(self._diff_row(i1 + k + 1, old[i1 + k], j1 + k + 1, new[j1 + k], " ", False, column))
            else:
                for k in range(max(i2 - i1, j2 - j1)):
                    lno = i1 + k + 1 if k < i2 - i1 else None
                    rno = j1 + k + 1 if k < j2 - j1 else None
                    mark = "|" if (lno and rno) else ("<" if lno else ">")
                    rows.append(self._diff_row(lno, old[i1 + k] if lno else "", rno,
                                               new[j1 + k] if rno else "", mark, True, column))
            if len(rows) > max_rows:
                rows.append(self.s(f"… truncated at {max_rows} rows", dim=True))
                break
        return "\n".join(rows)

    def _diff_row(self, lno: int | None, left: str, rno: int | None, right: str,
                  mark: str, changed: bool, column: int) -> str:
        left, right = left.rstrip()[:column], right.rstrip()[:column]
        if changed:
            left = self.s(left, color=self.theme.error) if lno else ""
            right = self.s(right, color=self.theme.success) if rno else ""
            mark = self.s(mark, color=self.theme.warning, bold=True)
        else:
            left, right = self.s(left, dim=True), self.s(right, dim=True)
            mark = self.s(mark, dim=True)
        pad = max(column - visible_len(left), 1)
        lnum = self.s(f"{lno:>4}", dim=True) if lno else " " * 4
        rnum = self.s(f"{rno:>4}", dim=True) if rno else " " * 4
        return f"{lnum} {left}{' ' * pad}{mark} {rnum} {right}"

    def panel(self, text: str, title: str = "", color: str | None = None) -> str:
        color = color or self.theme.muted
        body = text.split("\n")
        inner = max([visible_len(line) for line in body] + [len(title) + 2, 20])
        inner = min(inner, max(20, self.width - 4))
        title_seg = f" {title} " if title else ""
        top = (self.s("╭─", color=color) + (self.s(title_seg, color=self.theme.accent, bold=True) if title else "")
               + self.s("─" * (inner + 2 - len(title_seg)) + "╮", color=color))
        lines = [top]
        for line in body:
            pad = " " * max(0, inner - visible_len(line))
            lines.append(self.s("│ ", color=color) + line + pad + self.s(" │", color=color))
        lines.append(self.s("╰" + "─" * (inner + 2) + "╯", color=color))
        return "\n".join(lines)

    def table(self, headers: list[str], rows: list[list[str]],
              aligns: list[str] | None = None, accent_col: int | None = None) -> str:
        aligns = aligns or ["<"] * len(headers)

        def clip(value: str, width: int) -> str:
            value = str(value)
            return value if len(value) <= width else value[:width - 1] + "…"

        def pad(value: str, width: int, align: str) -> str:
            gap = max(0, width - len(value))
            if align.startswith(">"):
                return " " * gap + value
            if align.startswith("^"):
                left = gap // 2
                return " " * left + value + " " * (gap - left)
            return value + " " * gap

        widths = [len(h) for h in headers]
        capped: list[list[str]] = []
        for row in rows:
            items = [clip(row[i] if i < len(row) else "", 38) for i in range(len(headers))]
            capped.append(items)
            for i, item in enumerate(items):
                widths[i] = max(widths[i], len(item))
        header_line = "  ".join(
            self.s(pad(headers[i], widths[i], aligns[i]), bold=True, color=self.theme.primary)
            for i in range(len(headers)))
        separator = self.s("  ".join("─" * width for width in widths), dim=True)
        lines = [header_line, separator]
        for items in capped:
            cells = []
            for i, item in enumerate(items):
                padded = pad(item, widths[i], aligns[i])
                if i == accent_col:
                    cells.append(self.s(padded, color=self.theme.accent))
                else:
                    cells.append(padded)
            lines.append("  ".join(cells))
        return "\n".join(lines)

    def banner(self, version: str, model: str, context: str, project: str, session: str) -> str:
        colors = [self.theme.accent, self.theme.primary, self.theme.success]
        letters = [_LOGO["o"], _LOGO["a"], _LOGO["c"]]
        rows = []
        for i in range(5):
            row = "  ".join(self.s(letter[i], color=colors[j]) for j, letter in enumerate(letters))
            rows.append("  " + row)
        dot = self.s("●", color=self.theme.accent)
        rows.append("")
        rows.append("  " + self.s("oac", bold=True) + " " + self.s(f"v{version}", dim=True)
                    + " " + dot + " " + self.s(self.theme_name, dim=True))
        rows.append("  " + self.kv("model", model))
        rows.append("  " + self.kv("context", context))
        rows.append("  " + self.kv("project", project))
        rows.append("  " + self.kv("session", session))
        return "\n".join(rows)

    # -- syntax highlighting ------------------------------------------------

    def highlight(self, code: str, lang: str) -> str:
        if self.level == 0:
            return code
        lang = LANG_MAP.get(lang.strip().lower(), "")
        if lang == "json":
            return self._highlight_json(code)
        if lang in KEYWORDS:
            return self._highlight_generic(code, lang)
        return code

    def _highlight_generic(self, code: str, lang: str) -> str:
        keywords = KEYWORDS[lang]
        out: list[str] = []
        pos = 0
        for match in TOKEN.finditer(code):
            out.append(code[pos:match.start()])
            text = match.group(0)
            kind = match.lastgroup
            if kind == "comment":
                out.append(self.s(text, color=self.theme.comment, italic=True))
            elif kind == "string":
                out.append(self.s(text, color=self.theme.string))
            elif kind == "number":
                out.append(self.s(text, color=self.theme.number))
            elif kind == "name":
                if text in keywords:
                    out.append(self.s(text, color=self.theme.keyword))
                elif code[match.end():match.end() + 1] == "(":
                    out.append(self.s(text, color=self.theme.func))
                elif text[:1].isupper():
                    out.append(self.s(text, color=self.theme.bright))
                else:
                    out.append(text)
            elif kind == "punct":
                out.append(self.s(text, color=self.theme.muted))
            else:
                out.append(self.s(text, color=self.theme.operator))
            pos = match.end()
        out.append(code[pos:])
        return "".join(out)

    def _highlight_json(self, code: str) -> str:
        token = re.compile(
            r"(?P<key>\"(?:\\.|[^\"\\])*\")(\s*:)"
            r"|(?P<string>\"(?:\\.|[^\"\\])*\")"
            r"|(?P<number>-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)"
            r"|(?P<literal>\b(?:true|false|null)\b)")
        out: list[str] = []
        pos = 0
        for match in token.finditer(code):
            out.append(code[pos:match.start()])
            text = match.group(0)
            kind = match.lastgroup
            if kind == "key":
                out.append(self.s(match.group("key"), color=self.theme.func) + match.group(2))
            elif kind == "string":
                out.append(self.s(text, color=self.theme.string))
            elif kind == "number":
                out.append(self.s(text, color=self.theme.number))
            else:
                out.append(self.s(text, color=self.theme.keyword))
            pos = match.end()
        out.append(code[pos:])
        return "".join(out)

    def code_block(self, code: str, lang: str = "") -> str:
        code = code.replace("\t", "    ").rstrip("\n")
        if not code.strip():
            return ""
        body = self.highlight(code, lang) if lang else code
        lines = body.split("\n")
        widest = max(visible_len(line) for line in lines)
        if widest + 4 <= self.width:
            return self.panel("\n".join(lines), title=lang or "code")
        bar = self.s("▎ ", color=self.theme.accent)
        return "\n".join(bar + line for line in lines)

    # -- markdown ------------------------------------------------------------

    def _inline_segments(self, text: str) -> list[tuple[str, str]]:
        segments: list[tuple[str, str]] = []
        pos = 0
        for match in INLINE.finditer(text):
            if match.start() > pos:
                segments.append((text[pos:match.start()], ""))
            chunk = match.group(0)
            if chunk.startswith("`"):
                segments.append((chunk[1:-1], "code"))
            elif chunk.startswith("**"):
                segments.append((chunk[2:-2], "bold"))
            elif chunk.startswith("*"):
                segments.append((chunk[1:-1], "italic"))
            else:
                inner = re.fullmatch(r"\[([^\]]+)\]\(([^)\s]+)\)", chunk)
                if inner:
                    label, url = inner.group(1), inner.group(2)
                    segments.append((label, "link"))
                    if url != label:
                        segments.append((f" ({url})", "dim"))
            pos = match.end()
        if pos < len(text):
            segments.append((text[pos:], ""))
        return segments

    def _style_chunk(self, text: str, style: str) -> str:
        if style == "code":
            return self.s(text, color=self.theme.string)
        if style == "bold":
            return self.s(text, bold=True)
        if style == "italic":
            return self.s(text, italic=True)
        if style == "link":
            return self.s(text, color=self.theme.link, underline=True)
        if style == "dim":
            return self.s(text, dim=True)
        return text

    def render_inline(self, text: str) -> str:
        return "".join(self._style_chunk(chunk, style) for chunk, style in self._inline_segments(text))

    def _paragraph(self, text: str) -> str:
        return wrap_ansi(self.render_inline(text), self.width)

    def _list(self, items: list[tuple[int, str, str]]) -> str:
        bullets = ["•", "◦", "▪"]
        counters: dict[int, int] = {}
        out: list[str] = []
        for level, kind, content in items:
            level = min(level, 2)
            if kind == "ol":
                counters[level] = counters.get(level, 0) + 1
                marker = self.s(f"{counters[level]}.", color=self.theme.accent)
            else:
                marker = self.s(bullets[level], color=self.theme.accent)
            indent = "  " * level
            wrapped = wrap_ansi(self.render_inline(content), max(20, self.width - len(indent) - 2))
            lines = wrapped.split("\n")
            out.append(f"{indent}{marker} {lines[0]}")
            for extra in lines[1:]:
                out.append(f"{indent}  {extra}")
        return "\n".join(out)

    def _quote(self, lines: list[str]) -> str:
        bar = self.s("▎", color=self.theme.accent)
        wrapped = wrap_ansi(self.render_inline(" ".join(lines)), max(20, self.width - 2))
        return "\n".join(f"{bar} {self.s(line, italic=True)}" for line in wrapped.split("\n"))

    def md(self, text: str) -> str:
        """Render a markdown string (headings, lists, tables, code, quotes) to ANSI."""
        lines = text.replace("\r\n", "\n").split("\n")
        blocks: list[str] = []
        i, count = 0, len(lines)
        while i < count:
            line = lines[i]
            if not line.strip():
                i += 1
                continue
            fence = FENCE.match(line)
            if fence:
                body: list[str] = []
                i += 1
                while i < count and not FENCE.match(lines[i]):
                    body.append(lines[i])
                    i += 1
                i += 1
                blocks.append(self.code_block("\n".join(body), fence.group(1)))
                continue
            heading = HEADING.match(line)
            if heading:
                level = len(heading.group(1))
                color = {1: self.theme.primary, 2: self.theme.accent, 3: self.theme.bright}.get(level, "")
                blocks.append(self.s(heading.group(2).strip(), bold=True, color=color or None))
                i += 1
                continue
            if HR.match(line):
                blocks.append(self.rule())
                i += 1
                continue
            if QUOTE.match(line):
                quote: list[str] = []
                while i < count and QUOTE.match(lines[i]):
                    quote.append(QUOTE.match(lines[i]).group(1))  # type: ignore[union-attr]
                    i += 1
                blocks.append(self._quote(quote))
                continue
            if TABLE_ROW.match(line) and i + 1 < count and TABLE_SEP.match(lines[i + 1]):
                def cells(row: str) -> list[str]:
                    return [cell.strip() for cell in row.strip().strip("|").split("|")]
                headers = cells(line)
                aligns = []
                for cell in cells(lines[i + 1]):
                    left, right = cell.startswith(":"), cell.endswith(":")
                    aligns.append("^" if left and right else (">" if right else "<"))
                i += 2
                rows: list[list[str]] = []
                while i < count and TABLE_ROW.match(lines[i]):
                    rows.append(cells(lines[i]))
                    i += 1
                blocks.append(self.table(headers, rows, aligns=aligns))
                continue
            if UL.match(line) or OL.match(line):
                items: list[tuple[int, str, str]] = []
                while i < count:
                    item = UL.match(lines[i]) or OL.match(lines[i])
                    if not item:
                        break
                    kind = "ul" if UL.match(lines[i]) else "ol"
                    indent = len(item.group(1).expandtabs(4))
                    items.append((indent // 2, kind, item.group(2).strip()))
                    i += 1
                blocks.append(self._list(items))
                continue
            paragraph = [line]
            i += 1
            while i < count and lines[i].strip() and not (
                    FENCE.match(lines[i]) or HEADING.match(lines[i]) or HR.match(lines[i])
                    or UL.match(lines[i]) or OL.match(lines[i]) or QUOTE.match(lines[i])
                    or TABLE_ROW.match(lines[i])):
                paragraph.append(lines[i])
                i += 1
            blocks.append(self._paragraph(" ".join(paragraph)))
        return "\n\n".join(block for block in blocks if block)


def theme_list_line(ui: UI) -> str:
    """One line of colored dots, one per registered theme."""
    parts = []
    for name, theme in THEMES.items():
        if ui.level > 0 and theme.accent:
            parts.append(ui.s("●", color=theme.accent) + " " + name)
        else:
            parts.append("● " + name)
    return "  ".join(parts)
