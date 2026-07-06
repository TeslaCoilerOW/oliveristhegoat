#!/usr/bin/env python3
# ===========================================================================
# Engaging Coder / GOAT Chat -- the terminal front-end for local Ollama
# models on the MIT ORCD Engaging cluster.
#
# One file, two faces:
#   coder (default)  an agentic coding CLI in the style of Claude Code /
#                    Codex: editor-style file cards with syntax highlighting
#                    and line numbers, real red/green diffs, command cards
#                    with exit badges, approvals, and streaming markdown.
#   --chat           the same rendering engine as a pure chat REPL (used by
#                    ollama-chat): streamed markdown, fenced code rendered
#                    like an editor, reasoning ("thinking") shown dimmed,
#                    token-rate footers.
#
# Pure Python standard library -- no pip installs, so it runs on any node.
# It talks to Ollama's native /api/chat endpoint (streaming + tool calling).
#
#   python3 engaging_coder.py --model qwen3-coder:480b --host 127.0.0.1:15513
#   python3 engaging_coder.py --chat --model gpt-oss:20b "explain RAG"
#   python3 engaging_coder.py --selftest     # offline sanity checks
#   python3 engaging_coder.py --demo         # render every UI element, no model
#
# You normally never call this directly -- ollama-code / ollama-chat do.
# ===========================================================================
import argparse
import difflib
import fnmatch
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

try:
    import readline  # noqa: F401  -- line editing + history for input()
except Exception:
    pass

APP_CODER = "ENGAGING CODER"
APP_CHAT = "GOAT CHAT"
MAX_TOOL_OUTPUT = 6000          # chars of tool output fed back to the model
MAX_READ_OUTPUT = 24000        # chars for read_file (bigger: whole files / ranges)
MAX_CARD_LINES = 24             # lines shown inside file/output cards
DEFAULT_READ_LINES = 500        # read_file lines returned when no range is asked
MAX_READ_LINES = 2000           # hard cap on a single read_file range
try:
    RUN_TIMEOUT = max(1, int(os.environ.get("GOAT_RUN_TIMEOUT") or 180))  # shell kill (s)
except (TypeError, ValueError):
    RUN_TIMEOUT = 180

# Directories the code search / file finder never descend into (version control,
# caches, build output, vendored deps). Keeps searches fast and — on a shared
# cluster filesystem — considerate. Dot-directories are skipped wholesale too.
SKIP_DIRS = frozenset((
    ".git", ".hg", ".svn", "__pycache__", "node_modules", ".venv", "venv",
    "env", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox", ".cache",
    "dist", "build", ".idea", ".vscode", "site-packages", ".ipynb_checkpoints",
    ".ollama", ".terraform", "target", ".next", ".gradle",
))
SEARCH_MAX_RESULTS = 100        # matches returned by search_code
SEARCH_MAX_FILES = 20000        # files walked before search_code/find_files stop
FIND_MAX_RESULTS = 200          # paths returned by find_files
MAX_SCAN_FILE_BYTES = 2_000_000  # files bigger than this are skipped when searching


def app_version():
    try:
        vf = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "VERSION")
        with open(vf, "r", encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


# --------------------------------------------------------------------------
# ANSI-aware width helpers
# --------------------------------------------------------------------------
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def vlen(s):
    """Visible length of a string containing SGR escapes."""
    return len(ANSI_RE.sub("", s))


def vpad(s, width):
    """Pad (or leave) a styled string to `width` visible columns."""
    gap = width - vlen(s)
    return s + " " * gap if gap > 0 else s


def vtrunc(plain, width):
    """Truncate a PLAIN string to width, with an ellipsis."""
    if len(plain) <= width:
        return plain
    return plain[: max(0, width - 1)] + "…"


def term_cols():
    return shutil.get_terminal_size((100, 24)).columns


# --------------------------------------------------------------------------
# Palette / style. Brand-matched to lib/goat.sh: gold accents, the banner's
# ice→violet for identity, ember for errors. Editor tones are a muted dark
# theme so highlighted code reads like a real editor pane.
# --------------------------------------------------------------------------
class P:
    gold = (255, 191, 0)
    ember = (244, 115, 55)
    ice = (140, 225, 255)
    violet = (170, 120, 240)
    ok = (126, 199, 148)
    warn = (224, 187, 106)
    err = (233, 110, 110)
    dim = (128, 128, 140)
    faint = (95, 95, 108)
    fg = (226, 226, 231)
    cyan = (130, 200, 235)
    blue = (125, 170, 255)
    # syntax
    kw = (198, 146, 233)        # keywords          (violet)
    fn = (125, 170, 255)        # def/callable      (blue)
    string = (152, 195, 121)    # strings           (green)
    num = (229, 192, 123)       # numbers           (amber)
    com = (108, 112, 128)       # comments          (slate, italic)
    deco = (86, 182, 194)       # decorators/preproc/vars (teal)
    # diff backgrounds (subtle editor tints)
    bg_add = (26, 51, 36)
    bg_del = (58, 32, 36)
    bg_chip = (44, 44, 56)      # inline-code chip


class Style:
    def __init__(self, enabled):
        self.on = enabled

    def sgr(self, *parts):
        return "\x1b[" + ";".join(str(p) for p in parts) + "m" if self.on else ""

    def fgc(self, rgb):
        return self.sgr(38, 2, *rgb)

    def bgc(self, rgb):
        return self.sgr(48, 2, *rgb)

    @property
    def reset(self):
        return "\x1b[0m" if self.on else ""

    @property
    def bold(self):
        return "\x1b[1m" if self.on else ""

    @property
    def italic(self):
        return "\x1b[3m" if self.on else ""

    def paint(self, rgb, text, bold=False, bg=None, italic=False):
        if not self.on:
            return text
        pre = self.fgc(rgb)
        if bold:
            pre += self.bold
        if italic:
            pre += self.italic
        if bg:
            pre += self.bgc(bg)
        return f"{pre}{text}{self.reset}"

    def gradient(self, text, c1, c2, bold=True):
        """Per-character linear gradient — the 'expensive' title treatment."""
        if not self.on:
            return text
        n = max(len(text) - 1, 1)
        out = [self.bold] if bold else []
        for i, ch in enumerate(text):
            t = i / n
            rgb = tuple(round(a + (b - a) * t) for a, b in zip(c1, c2))
            out.append(self.fgc(rgb) + ch)
        return "".join(out) + self.reset

    # shorthands used everywhere
    def gold(self, t, b=False):  return self.paint(P.gold, t, b)
    def ok(self, t, b=False):    return self.paint(P.ok, t, b)
    def warn(self, t, b=False):  return self.paint(P.warn, t, b)
    def err(self, t, b=False):   return self.paint(P.err, t, b)
    def dim(self, t, b=False):   return self.paint(P.dim, t, b)
    def faint(self, t):          return self.paint(P.faint, t)
    def fg(self, t, b=False):    return self.paint(P.fg, t, b)
    def cyan(self, t, b=False):  return self.paint(P.cyan, t, b)
    def blue(self, t, b=False):  return self.paint(P.blue, t, b)
    def ice(self, t, b=False):   return self.paint(P.ice, t, b)


def supports_color():
    if os.environ.get("NO_COLOR") or os.environ.get("GOAT_COLOR") == "0":
        return False
    if os.environ.get("FORCE_COLOR") or os.environ.get("GOAT_COLOR") == "1":
        return True
    return sys.stdout.isatty()


def rl_prompt(styled):
    """Wrap SGR escapes in \\001..\\002 so readline counts columns right."""
    if "readline" not in sys.modules:
        return styled
    return ANSI_RE.sub(lambda m: "\001" + m.group(0) + "\002", styled)


# --------------------------------------------------------------------------
# Syntax highlighting — a compact regex lexer, per line, with just enough
# cross-line state (triple-quoted strings, /* */) to look right in cards.
# --------------------------------------------------------------------------
LANG_ALIASES = {
    "py": "python", "python": "python", "python3": "python",
    "sh": "bash", "bash": "bash", "zsh": "bash", "shell": "bash", "console": "bash",
    "js": "js", "javascript": "js", "jsx": "js", "ts": "js", "tsx": "js", "typescript": "js",
    "json": "json", "jsonc": "json",
    "yaml": "yaml", "yml": "yaml", "toml": "ini", "ini": "ini", "cfg": "ini", "conf": "ini",
    "c": "c", "h": "c", "cpp": "c", "cc": "c", "hpp": "c", "c++": "c", "cuda": "c", "cu": "c",
    "go": "go", "rust": "rust", "rs": "rust", "java": "java", "kotlin": "java",
    "rb": "ruby", "ruby": "ruby", "sql": "sql", "tex": "tex", "latex": "tex",
    "md": "md", "markdown": "md", "diff": "diff", "patch": "diff",
    "make": "bash", "makefile": "bash", "dockerfile": "bash", "sbatch": "bash", "slurm": "bash",
}

EXT_LANGS = {
    ".py": "python", ".sh": "bash", ".bash": "bash", ".bashrc": "bash",
    ".js": "js", ".jsx": "js", ".ts": "js", ".tsx": "js", ".mjs": "js",
    ".json": "json", ".yaml": "yaml", ".yml": "yaml", ".toml": "ini", ".ini": "ini",
    ".c": "c", ".h": "c", ".cpp": "c", ".cc": "c", ".hpp": "c", ".cu": "c",
    ".go": "go", ".rs": "rust", ".java": "java", ".kt": "java", ".rb": "ruby",
    ".sql": "sql", ".tex": "tex", ".md": "md", ".diff": "diff", ".patch": "diff",
    ".sbatch": "bash", ".slurm": "bash",
}

KEYWORDS = {
    "python": ("False None True and as assert async await break class continue def del elif else"
               " except finally for from global if import in is lambda nonlocal not or pass raise"
               " return try while with yield match case self cls"),
    "bash": ("if then else elif fi for while until do done case esac function in select time"
             " local return exit export readonly declare set unset shift trap source alias eval"
             " printf echo read cd true false"),
    "js": ("break case catch class const continue debugger default delete do else export extends"
           " finally for function if import in instanceof let new of return static super switch"
           " this throw try typeof var void while with yield async await null undefined true false"),
    "json": "true false null",
    "yaml": "true false null yes no on off",
    "ini": "true false",
    "c": ("auto break case char const continue default do double else enum extern float for goto"
          " if inline int long register restrict return short signed sizeof static struct switch"
          " typedef union unsigned void volatile while bool class namespace new delete private"
          " public protected template typename using virtual nullptr constexpr"),
    "go": ("break case chan const continue default defer else fallthrough for func go goto if"
           " import interface map package range return select struct switch type var nil true false"),
    "rust": ("as async await break const continue crate dyn else enum extern fn for if impl in let"
             " loop match mod move mut pub ref return self static struct super trait type unsafe"
             " use where while true false"),
    "java": ("abstract assert boolean break byte case catch char class const continue default do"
             " double else enum extends final finally float for if implements import instanceof int"
             " interface long native new package private protected public return short static"
             " strictfp super switch synchronized this throw throws transient try void volatile"
             " while true false null var record"),
    "ruby": ("BEGIN END alias and begin break case class def defined? do else elsif end ensure"
             " false for if in module next nil not or redo rescue retry return self super then"
             " true undef unless until when while yield"),
    "sql": ("select from where and or not insert into values update set delete create table drop"
            " alter index join left right inner outer on as order by group having limit offset"
            " distinct union all exists between like is null primary key foreign references"),
    "tex": "begin end documentclass usepackage section subsection item textbf textit emph",
    "md": "", "diff": "",
}
KEYWORDS = {k: frozenset(v.split()) for k, v in KEYWORDS.items()}

_WORD_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_NUM_RE = re.compile(r"\d[\d_]*(?:\.\d[\d_]*)?(?:[eE][+-]?\d+)?|0[xXbBoO][0-9a-fA-F_]+")


def lang_for_path(path):
    base = os.path.basename(path or "").lower()
    if base in ("makefile", "dockerfile"):
        return "bash"
    _, ext = os.path.splitext(base)
    return EXT_LANGS.get(ext, "")


def norm_lang(tag):
    return LANG_ALIASES.get((tag or "").strip().lower(), "")


class Lexer:
    """highlight(line) -> styled line; keeps triple-quote / block-comment state."""

    def __init__(self, st, lang):
        self.st = st
        self.lang = lang or ""
        self.state = None            # None | ("mlstr", delim) | "mlcom"
        self.kws = KEYWORDS.get(self.lang, frozenset())

    def _p(self, rgb, text, italic=False):
        return self.st.paint(rgb, text, italic=italic) if text else ""

    def highlight(self, line):
        if not self.st.on or not self.lang or self.lang in ("md",):
            return self.st.fg(line) if self.st.on else line
        if self.lang == "diff":
            if line.startswith("+"):
                return self._p(P.ok, line)
            if line.startswith("-"):
                return self._p(P.err, line)
            if line.startswith("@@"):
                return self._p(P.cyan, line)
            return self._p(P.dim, line)
        out, i, n = [], 0, len(line)

        # resume a multi-line string / comment
        if self.state:
            kind = self.state[0] if isinstance(self.state, tuple) else self.state
            if kind == "mlstr":
                delim = self.state[1]
                end = line.find(delim)
                if end < 0:
                    return self._p(P.string, line)
                out.append(self._p(P.string, line[: end + len(delim)]))
                i = end + len(delim)
                self.state = None
            elif kind == "mlcom":
                end = line.find("*/")
                if end < 0:
                    return self._p(P.com, line, italic=True)
                out.append(self._p(P.com, line[: end + 2], italic=True))
                i = end + 2
                self.state = None

        prev_word = ""
        while i < n:
            ch = line[i]
            # comments ------------------------------------------------------
            if self.lang in ("python", "bash", "yaml", "ini", "ruby") and ch == "#":
                out.append(self._p(P.com, line[i:], italic=True)); break
            if self.lang in ("c", "js", "go", "rust", "java") and line.startswith("//", i):
                out.append(self._p(P.com, line[i:], italic=True)); break
            if self.lang == "sql" and line.startswith("--", i):
                out.append(self._p(P.com, line[i:], italic=True)); break
            if self.lang == "tex" and ch == "%":
                out.append(self._p(P.com, line[i:], italic=True)); break
            if self.lang in ("c", "js", "go", "rust", "java") and line.startswith("/*", i):
                end = line.find("*/", i + 2)
                if end < 0:
                    out.append(self._p(P.com, line[i:], italic=True))
                    self.state = "mlcom"; break
                out.append(self._p(P.com, line[i:end + 2], italic=True)); i = end + 2; continue
            # strings -------------------------------------------------------
            if self.lang == "python" and (line.startswith('"""', i) or line.startswith("'''", i)):
                delim = line[i:i + 3]
                end = line.find(delim, i + 3)
                if end < 0:
                    out.append(self._p(P.string, line[i:]))
                    self.state = ("mlstr", delim); break
                out.append(self._p(P.string, line[i:end + 3])); i = end + 3; continue
            if ch in "\"'`":
                j = i + 1
                while j < n:
                    if line[j] == "\\":
                        j += 2; continue
                    if line[j] == ch:
                        j += 1; break
                    j += 1
                out.append(self._p(P.string, line[i:j])); i = j; continue
            # decorators / preprocessor / shell vars -------------------------
            if self.lang == "python" and ch == "@" and (i == 0 or line[:i].isspace()):
                m = _WORD_RE.match(line, i + 1)
                j = m.end() if m else i + 1
                out.append(self._p(P.deco, line[i:j])); i = j; continue
            if self.lang == "c" and ch == "#" and (i == 0 or line[:i].isspace()):
                out.append(self._p(P.deco, line[i:])); break
            if self.lang == "bash" and ch == "$":
                if line.startswith("${", i):
                    j = line.find("}", i)
                    j = (j + 1) if j >= 0 else n
                else:
                    m = _WORD_RE.match(line, i + 1)
                    j = m.end() if m else i + 1
                out.append(self._p(P.deco, line[i:j])); i = j; continue
            if self.lang == "tex" and ch == "\\":
                m = _WORD_RE.match(line, i + 1)
                j = m.end() if m else i + 1
                out.append(self._p(P.kw, line[i:j])); i = j; continue
            # numbers -------------------------------------------------------
            m = _NUM_RE.match(line, i)
            if m and (i == 0 or not (line[i - 1].isalnum() or line[i - 1] == "_")):
                out.append(self._p(P.num, m.group(0))); i = m.end(); continue
            # words ---------------------------------------------------------
            m = _WORD_RE.match(line, i)
            if m:
                word = m.group(0)
                nxt = line[m.end():m.end() + 1]
                low = word if self.lang != "sql" else word.lower()
                if low in self.kws:
                    out.append(self._p(P.kw, word))
                elif prev_word in ("def", "class", "fn", "func", "function") or nxt == "(":
                    out.append(self._p(P.fn, word))
                else:
                    out.append(self._p(P.fg, word))
                prev_word = word
                i = m.end(); continue
            # punctuation / everything else ---------------------------------
            out.append(self._p(P.dim, ch) if ch in "()[]{}<>,;:=+-*/|&!%^~" else self._p(P.fg, ch))
            i += 1
        return "".join(out)


# --------------------------------------------------------------------------
# Cards — the editor-pane look: rounded borders, title chips, gutters.
# Streaming-friendly: begin() ... line() ... end().
# --------------------------------------------------------------------------
class Card:
    def __init__(self, st, indent="  "):
        self.st = st
        self.ind = indent
        self.w = max(44, min(term_cols() - len(indent) - 2, 100))
        self.inner = self.w - 2          # columns between the borders

    def _edge(self, left, right, segments):
        """Border row with styled segments embedded in dim rule characters."""
        st = self.st
        parts, used = [], 0
        for seg in segments:
            parts.append(st.dim("─ ") + seg + st.dim(" "))
            used += vlen(seg) + 3
        fill = max(0, self.inner - used)
        return self.ind + st.faint(left) + "".join(parts) + st.faint("─" * fill + right)

    def begin(self, title_segs):
        print(self._edge("╭", "╮", title_segs))

    def row(self, styled, fill=""):
        st = self.st
        body = vpad(styled, self.inner)
        if vlen(body) > self.inner:      # never break the right border
            body = body[:0] + styled     # fallback: raw (rare; wrapped upstream)
            body = vpad(body, self.inner)
        print(self.ind + st.faint("│") + body + st.faint("│"))

    def end(self, footer_segs=()):
        print(self._edge("╰", "╯", footer_segs))


class CodeCard(Card):
    """A Card that renders numbered, syntax-highlighted code lines."""

    def __init__(self, st, lang="", indent="  ", start=1):
        super().__init__(st, indent)
        self.lex = Lexer(st, lang)
        self.n = start
        self.gut = 4
        self.body_w = self.inner - self.gut - 2

    def code_line(self, plain):
        st = self.st
        plain = plain.replace("\t", "    ")
        segs = [plain[i:i + self.body_w] for i in range(0, max(len(plain), 1), self.body_w)] or [""]
        for k, seg in enumerate(segs):
            gutter = f"{self.n:>{self.gut}} " if k == 0 else " " * self.gut + " "
            self.row(st.faint(gutter) + vpad(self.lex.highlight(seg), self.body_w + 1))
        self.n += 1

    def more(self, count):
        self.row(self.st.dim(f"{'':>{self.gut}} … +{count} more line{'s' if count != 1 else ''}"))


# --------------------------------------------------------------------------
# Streaming markdown renderer. Prose streams token-by-token with live
# inline styling (**bold**, `code`); block elements (fences, headings,
# lists, rules, quotes) are recognised at line starts; fenced code becomes
# a live CodeCard. Reasoning ("thinking") renders dimmed, via native events
# or inline <think> tags.
# --------------------------------------------------------------------------
class MarkdownStream:
    def __init__(self, st, indent="  "):
        self.st = st
        self.ind = indent
        self.linebuf = ""            # undecided start-of-line buffer
        self.decided_prose = False   # current line is flowing prose
        self.bold = False
        self.code = False            # inline-code chip state
        self.card = None             # open CodeCard when inside a fence
        self.fence_buf = None
        self.think = False
        self.think_native = False    # opened by native events, not <think> tags
        self.think_t0 = 0.0
        self.tail = ""               # partial <think> tag carry
        self.produced = False

    # ---- helpers -----------------------------------------------------------
    def _style_char(self, ch):
        st = self.st
        if self.think:
            return st.paint(P.faint, ch, italic=True)
        if self.code:
            return st.paint(P.warn, ch, bg=P.bg_chip)
        if self.bold:
            return st.fg(ch, b=True)
        return st.fg(ch)

    def _emit(self, s):
        sys.stdout.write(s)
        self.produced = True

    def _open_think(self):
        if not self.think:
            self.think = True
            self.think_t0 = time.time()
            self._emit("\n" + self.ind + self.st.paint(P.faint, "∴ thinking", italic=True) + "\n" + self.ind)

    def _close_think(self):
        if self.think:
            dt = time.time() - self.think_t0
            self._emit("\n" + self.ind + self.st.paint(P.faint, f"∴ {dt:.1f}s of thought", italic=True) + "\n\n")
            self.think = False
            self.think_native = False

    # ---- block-line rendering ----------------------------------------------
    def _render_block_line(self, line):
        st = self.st
        stripped = line.strip()
        if stripped.startswith("```"):
            if self.card:
                self.card.end()
                self.card = None
            else:
                lang = norm_lang(stripped[3:].strip()) or (stripped[3:].strip() or "")
                self.card = CodeCard(st, norm_lang(stripped[3:].strip()), indent=self.ind)
                tag = (stripped[3:].strip() or "text")
                self.card.begin([st.cyan(tag)])
            return
        m = re.match(r"^(#{1,4})\s+(.*)$", stripped)
        if m:
            depth, text = len(m.group(1)), m.group(2)
            if depth == 1:
                self._emit(self.ind + st.gold(text, b=True) + "\n" + self.ind
                           + st.faint("─" * min(vlen(text), 48)) + "\n")
            elif depth == 2:
                self._emit(self.ind + st.gold("▸ ", b=True) + st.fg(text, b=True) + "\n")
            else:
                self._emit(self.ind + st.fg(text, b=True) + "\n")
            return
        if re.match(r"^(-{3,}|\*{3,}|_{3,})$", stripped):
            self._emit(self.ind + st.faint("─" * 40) + "\n")
            return
        m = re.match(r"^(\s*)([-*+])\s+(.*)$", line)
        if m:
            self._emit(self.ind + m.group(1) + st.gold("• ") + self._inline(m.group(3)) + "\n")
            return
        m = re.match(r"^(\s*)(\d+)[.)]\s+(.*)$", line)
        if m:
            self._emit(self.ind + m.group(1) + st.gold(m.group(2) + ". ") + self._inline(m.group(3)) + "\n")
            return
        if stripped.startswith(">"):
            self._emit(self.ind + st.faint("▎") + st.dim(stripped[1:].strip()) + "\n")
            return
        self._emit(self.ind + self._inline(line) + "\n")

    def _inline(self, text):
        """One-shot inline styling for complete lines (lists, headings...)."""
        st = self.st
        out, i, n = [], 0, len(text)
        bold = code = False
        while i < n:
            if text.startswith("**", i):
                bold = not bold; i += 2; continue
            if text[i] == "`":
                code = not code; i += 1; continue
            ch = text[i]
            if code:
                out.append(st.paint(P.warn, ch, bg=P.bg_chip))
            elif bold:
                out.append(st.fg(ch, b=True))
            else:
                out.append(st.fg(ch))
            i += 1
        return "".join(out)

    # ---- streaming entry points ---------------------------------------------
    def feed_think(self, text):
        self._open_think()
        self.think_native = True
        self._emit(self.st.paint(P.faint, text.replace("\n", "\n" + self.ind), italic=True))
        sys.stdout.flush()

    def feed(self, text):
        # a native thinking phase ends the moment real content starts
        if self.think and self.think_native:
            self._close_think()
        text = self.tail + text
        self.tail = ""
        # hold back a partial <think> / </think> tag split across tokens
        for tag in ("</think>", "<think>"):
            for k in range(len(tag) - 1, 0, -1):
                if text.endswith(tag[:k]):
                    self.tail = text[-k:]
                    text = text[:-k]
                    break
            if self.tail:
                break
        i = 0
        while i < len(text):
            if text.startswith("<think>", i):
                self._open_think(); i += 7; continue
            if text.startswith("</think>", i):
                self._close_think(); i += 8; continue
            ch = text[i]
            if self.think:
                if ch == "\n":
                    self._emit("\n" + self.ind)
                else:
                    self._emit(self.st.paint(P.faint, ch, italic=True))
                i += 1
                continue
            i += self._feed_char(text, i)
        sys.stdout.flush()

    def _feed_char(self, text, i):
        ch = text[i]
        if self.card is not None:                    # inside a fence: buffer lines
            if ch == "\n":
                line = self.linebuf
                self.linebuf = ""
                if line.strip().startswith("```"):
                    self.card.end()
                    self.card = None
                else:
                    self.card.code_line(line)
            else:
                self.linebuf += ch
            return 1
        if not self.decided_prose:                   # at/near start of line
            if ch == "\n":
                self._render_pending_line()
                return 1
            self.linebuf += ch
            starter = re.match(r"^\s*([#>`*+-]|\d|```)", self.linebuf)
            if starter or len(self.linebuf) < 4:
                return 1                             # keep buffering block-ish lines
            # plain prose: flush the buffer as live stream
            buffered = self.linebuf
            self.linebuf = ""
            self.decided_prose = True
            self._emit(self.ind)
            self._stream_prose(buffered)
            return 1
        if ch == "\n":                               # end of a streamed prose line
            self._emit("\n")
            self.decided_prose = False
            self.bold = self.code = False
            return 1
        self._stream_prose(ch)
        return 1

    def _render_pending_line(self):
        line, self.linebuf = self.linebuf, ""
        if not line.strip():
            self._emit("\n")
            return
        self._render_block_line(line)

    def _stream_prose(self, chunk):
        i, n = 0, len(chunk)
        while i < n:
            if chunk.startswith("**", i):
                self.bold = not self.bold
                i += 2
                continue
            if chunk[i] == "*" and i + 1 == n:       # lone trailing '*': hold
                self.tail += "*"
                i += 1
                continue
            if chunk[i] == "`":
                self.code = not self.code
                i += 1
                continue
            self._emit(self._style_char(chunk[i]))
            i += 1

    def close(self):
        if self.card is not None:
            self.card.end([self.st.dim("interrupted")])
            self.card = None
        if self.linebuf:
            if self.decided_prose:
                self._stream_prose(self.linebuf)
            else:
                self._render_pending_line()
            self.linebuf = ""
        self._close_think()
        if self.produced:
            self._emit("\n")
        sys.stdout.flush()


# --------------------------------------------------------------------------
# Spinner — braille frames + phase + elapsed time, cleans up after itself.
# --------------------------------------------------------------------------
class Spinner:
    FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

    def __init__(self, st):
        self.st = st
        self._stop = threading.Event()
        self._thread = None
        self.label = "thinking"

    def start(self, label):
        if not self.st.on or not sys.stdout.isatty():
            return
        self.label = label
        self._stop.clear()
        t0 = time.time()

        def run():
            i = 0
            while not self._stop.is_set():
                frame = self.FRAMES[i % len(self.FRAMES)]
                dt = time.time() - t0
                sys.stdout.write("\r  " + self.st.gold(frame) + " "
                                 + self.st.dim(f"{self.label} · {dt:.0f}s ")
                                 + self.st.faint("(ctrl-c to interrupt)") + "  ")
                sys.stdout.flush()
                i += 1
                time.sleep(0.08)

        self._thread = threading.Thread(target=run, daemon=True)
        self._thread.start()

    def stop(self):
        if self._thread is None:
            return
        self._stop.set()
        self._thread.join()
        self._thread = None
        if self.st.on and sys.stdout.isatty():
            sys.stdout.write("\r" + " " * (term_cols() - 1) + "\r")
            sys.stdout.flush()


# --------------------------------------------------------------------------
# UI — banner, prompts, tool cards, approvals, footers.
# --------------------------------------------------------------------------
TOOL_DOTS = {
    "read": (P.cyan, "read"), "list": (P.cyan, "list"),
    "search": (P.deco, "search"), "find": (P.blue, "find"),
    "write": (P.ok, "write"), "edit": (P.warn, "edit"), "run": (P.violet, "run"),
}


class UI:
    def __init__(self, style, chat=False):
        self.st = style
        self.chat = chat

    # ---- banner -------------------------------------------------------------
    def banner(self, model, node, gpus, cwd):
        st = self.st
        card = Card(st, indent=" ")
        name = APP_CHAT if self.chat else APP_CODER
        grad = (P.ice, P.violet) if self.chat else (P.gold, P.ember)
        tagline = "local model · private · yours" if self.chat else "local · on-cluster · agentic"
        ver = app_version()

        title = st.gradient("▐█ " + name, *grad)
        right = st.dim(tagline)
        gap = max(1, card.inner - vlen(title) - vlen(right) - 3)
        card.begin([])
        card.row("  " + title + " " * gap + right + " ")
        card.row("")

        def kv(k, v):
            card.row("  " + st.dim(f"{k:<7}") + v)

        kv("model", st.fg(model, b=True))
        loc = st.fg(node) + ((st.dim("  ·  ") + st.gold(gpus)) if gpus else "")
        kv("node", loc)
        if not self.chat:
            home = os.path.expanduser("~")
            shown = "~" + cwd[len(home):] if cwd.startswith(home) else cwd
            kv("dir", st.fg(shown))
        card.row("")
        tips = ("/help commands · /bye frees the GPU" if self.chat
                else "/help commands · /auto approvals · /exit frees the GPU")
        card.row("  " + st.faint(tips))
        card.end([st.dim("v" + ver)] if ver else [])
        print()

    # ---- prompt / stream ------------------------------------------------------
    def user_prompt(self):
        return input(rl_prompt(self.st.gold("❯ ", b=True)))

    def assistant_header(self, model):
        d = "◆" if not self.chat else "◆"
        print(self.st.gold(d, b=True) + " " + self.st.dim(model))

    def turn_footer(self, done_obj):
        try:
            toks = int(done_obj.get("eval_count") or 0)
            ns = int(done_obj.get("eval_duration") or 0)
        except (TypeError, ValueError, AttributeError):
            return
        if not toks or not ns:
            return
        rate = toks / (ns / 1e9)
        print("  " + self.st.faint(f"↳ {toks} tok · {rate:.1f} tok/s"))

    # ---- notes / errors -------------------------------------------------------
    def note(self, text):
        print("  " + self.st.dim("• " + text))

    def ok(self, text):
        print("  " + self.st.ok("✓ ", b=True) + self.st.dim(text))

    def error(self, text):
        print("  " + self.st.err("✗ " + text, b=True))

    # ---- tool rendering ---------------------------------------------------------
    def tool_dot(self, kind, detail, metric=""):
        rgb, label = TOOL_DOTS.get(kind, (P.dim, kind or "?"))
        st = self.st
        line = "  " + st.paint(rgb, "●", bold=True) + " " + st.fg(f"{label:<6}", b=True) + " " + detail
        if metric:
            pad = max(2, min(term_cols(), 102) - vlen(line) - vlen(metric) - 2)
            line += " " * pad + st.faint(metric)
        print(line)

    def file_card(self, path, content, new=True, cap=MAX_CARD_LINES):
        st = self.st
        lang = lang_for_path(path)
        card = CodeCard(st, lang, indent="  ")
        tag = [st.fg(path, b=True)]
        state = st.ok("new file") if new else st.warn("overwrite")
        tag.append(state)
        if lang:
            tag.append(st.cyan(lang))
        card.begin(tag)
        lines = content.splitlines() or [""]
        for ln in lines[:cap]:
            card.code_line(ln)
        if len(lines) > cap:
            card.more(len(lines) - cap)
        card.end()

    def diff_card(self, path, before, after, cap=MAX_CARD_LINES * 2):
        st = self.st
        lang = lang_for_path(path)
        lex = Lexer(st, lang)
        a, b = before.splitlines(), after.splitlines()
        hunks = list(difflib.unified_diff(a, b, n=2, lineterm=""))[2:]  # drop ---/+++
        adds = sum(1 for h in hunks if h.startswith("+"))
        dels = sum(1 for h in hunks if h.startswith("-"))
        card = Card(st, indent="  ")
        card.begin([st.fg(path, b=True),
                    st.err(f"−{dels}") + st.dim(" ") + st.ok(f"+{adds}")])
        gut = 4
        body_w = card.inner - gut - 3
        old_n = new_n = 0
        shown = 0
        first_hunk = True
        for h in hunks:
            if shown >= cap:
                card.row(st.dim(f"{'':>{gut}}  … diff truncated"))
                break
            if h.startswith("@@"):
                m = re.match(r"@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@", h)
                if m:
                    old_n, new_n = int(m.group(1)), int(m.group(2))
                if not first_hunk:
                    card.row(st.faint(f"{'':>{gut}}  ⋯"))
                first_hunk = False
                continue
            body = h[1:].replace("\t", "    ")
            seg = vtrunc(body, body_w)
            code = lex.highlight(seg)
            if h.startswith("+"):
                card.row(st.faint(f"{new_n:>{gut}} ") + st.paint(P.ok, "+", bold=True)
                         + " " + vpad(self._bg(seg, P.bg_add, lang), body_w))
                new_n += 1
            elif h.startswith("-"):
                card.row(st.faint(f"{old_n:>{gut}} ") + st.paint(P.err, "−", bold=True)
                         + " " + vpad(self._bg(seg, P.bg_del, lang), body_w))
                old_n += 1
            else:
                card.row(st.faint(f"{new_n:>{gut}}   ") + vpad(st.dim(seg), body_w))
                old_n += 1
                new_n += 1
            shown += 1
        card.end()
        return adds, dels

    def _bg(self, plain, bg, lang):
        """Syntax-highlight text over a subtle background tint."""
        st = self.st
        if not st.on:
            return plain
        hl = Lexer(st, lang).highlight(plain)
        # re-assert the background after every reset the lexer emitted
        return st.bgc(bg) + hl.replace(st.reset, st.reset + st.bgc(bg)) + st.reset

    def run_card(self, cmd, output, code, dur, cap=MAX_CARD_LINES):
        st = self.st
        card = Card(st, indent="  ")
        card.begin([st.gold("$ ", b=True) + st.fg(vtrunc(cmd, card.inner - 12), b=True)])
        lines = (output.rstrip("\n") or "(no output)").splitlines()
        for ln in lines[:cap]:
            card.row(" " + st.dim(vtrunc(ln.replace("\t", "    "), card.inner - 2)))
        if len(lines) > cap:
            card.row(" " + st.faint(f"… +{len(lines) - cap} more lines"))
        badge = (st.ok(f"✓ exit {code}") if code == 0 else st.err(f"✗ exit {code}", b=True))
        card.end([badge, st.dim(f"{dur:.1f}s")])

    def listing_block(self, entries, cap=14):
        st = self.st
        for e in entries[:cap]:
            print("    " + (st.blue(e) if e.endswith("/") else st.dim(e)))
        if len(entries) > cap:
            print("    " + st.faint(f"… +{len(entries) - cap} more"))

    # ---- approvals ---------------------------------------------------------------
    def confirm(self, question):
        st = self.st
        keys = (st.gold("y", b=True) + st.dim(" yes  ")
                + st.gold("n", b=True) + st.dim(" no  ")
                + st.gold("a", b=True) + st.dim(" always"))
        try:
            ans = input(rl_prompt("  " + st.warn("⏸ ", b=True) + st.fg(question, b=True)
                                  + "  " + keys + st.dim(" › ")))
        except (EOFError, KeyboardInterrupt):
            print()
            return "n"
        return (ans or "n").strip().lower()[:1]

    # ---- help / session -----------------------------------------------------------
    def help(self):
        st = self.st
        if self.chat:
            cmds = [("/help", "this help"),
                    ("/clear", "forget the conversation"),
                    ("/model", "show the model"),
                    ("/bye, /exit", "quit and free the GPU")]
        else:
            cmds = [("/help", "this help"),
                    ("/add <path>...", "read files into the conversation"),
                    ("/run <cmd>", "run a command yourself, share the output"),
                    ("/auto", "toggle auto-approve for edits & commands"),
                    ("/clear", "forget the conversation"),
                    ("/model", "show the model"),
                    ("/exit, /quit", "leave (frees the GPU when the job ends)")]
        print()
        for c, d in cmds:
            print("    " + st.gold(f"{c:<16}") + st.dim(d))
        if not self.chat:
            print("    " + st.faint("the model can search · read · write · edit files · run commands — with your approval"))
        print()

    def session_footer(self, stats):
        st = self.st
        mins, secs = divmod(int(stats.get("dur", 0)), 60)
        bits = [f"{mins} m {secs:02d} s", f"{stats.get('turns', 0)} turn{'s' if stats.get('turns', 0) != 1 else ''}"]
        if not self.chat:
            bits.append(f"{stats.get('edits', 0)} file change{'s' if stats.get('edits', 0) != 1 else ''}")
            bits.append(f"{stats.get('cmds', 0)} command{'s' if stats.get('cmds', 0) != 1 else ''}")
        print("  " + st.faint("── session · " + " · ".join(bits) + " ──"))
        self.ok("done — the GPU frees when this job ends")


# --------------------------------------------------------------------------
# Ollama client — native /api/chat, streaming + tools + thinking.
# --------------------------------------------------------------------------
class Ollama:
    def __init__(self, host, model, temperature=0.2):
        self.base = f"http://{host}"
        self.model = model
        self.temperature = temperature

    def stream_chat(self, messages, tools):
        payload = {
            "model": self.model,
            "messages": messages,
            "stream": True,
            "options": {"temperature": self.temperature},
        }
        if tools:
            payload["tools"] = tools
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            self.base + "/api/chat", data=data,
            headers={"Content-Type": "application/json"}, method="POST",
        )
        try:
            resp = urllib.request.urlopen(req, timeout=None)
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")
            yield ("error", f"HTTP {e.code}: {body.strip()[:400]}")
            return
        except urllib.error.URLError as e:
            yield ("error", f"cannot reach Ollama at {self.base}: {e.reason}")
            return
        try:
            for raw in resp:
                line = raw.strip()
                if not line:
                    continue
                for ev in parse_stream_line(line):
                    yield ev
                    if ev[0] in ("error", "done"):
                        return
        finally:
            resp.close()


def parse_stream_line(line):
    """Parse one NDJSON line from /api/chat into UI events. Pure -> testable."""
    if isinstance(line, (bytes, bytearray)):
        line = line.decode("utf-8", "replace")
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return
    if isinstance(obj, dict) and obj.get("error"):
        yield ("error", str(obj["error"]))
        return
    msg = obj.get("message") or {}
    thinking = msg.get("thinking")
    if thinking:
        yield ("think", thinking)
    content = msg.get("content")
    if content:
        yield ("token", content)
    tcs = msg.get("tool_calls")
    if tcs:
        yield ("tool_calls", tcs)
    if obj.get("done"):
        yield ("done", obj)


def parse_tool_call(tc):
    """Extract (name, args_dict) from an Ollama tool_call. Lenient about shapes."""
    fn = tc.get("function", tc) if isinstance(tc, dict) else {}
    name = fn.get("name", "")
    args = fn.get("arguments", {})
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except json.JSONDecodeError:
            args = {"_raw": args}
    if not isinstance(args, dict):
        args = {}
    return name, args


# --------------------------------------------------------------------------
# Tools the model can call
# --------------------------------------------------------------------------
def _fnschema(name, desc, props, required):
    return {"type": "function", "function": {
        "name": name, "description": desc,
        "parameters": {"type": "object", "properties": props, "required": required}}}


TOOL_SCHEMA = [
    _fnschema("read_file",
              "Read a UTF-8 text file. Lines come back with 1-based line-number prefixes "
              "(like `cat -n`) for reference ONLY — never copy those numbers into edit_file. "
              "Page through large files with 'offset' (first line) and 'limit' (line count).",
              {"path": {"type": "string", "description": "File path (relative to the working dir)."},
               "offset": {"type": "integer", "description": "1-based line to start reading at (default 1)."},
               "limit": {"type": "integer", "description": "Maximum lines to return (default %d)." % DEFAULT_READ_LINES}},
              ["path"]),
    _fnschema("search_code",
              "Search file CONTENTS across the project for a regular expression (or a literal "
              "string when regex=false) and return matching 'path:line: text' hits. The fast "
              "way to find a symbol, string, or definition. Skips .git, caches, build output "
              "and binary files.",
              {"pattern": {"type": "string", "description": "Regex (or literal, if regex=false) to search for."},
               "path": {"type": "string", "description": "Directory or single file to search under; defaults to '.'."},
               "glob": {"type": "string", "description": "Only search files whose name matches this glob, e.g. '*.py'."},
               "regex": {"type": "boolean", "description": "Treat 'pattern' as a regex (default true)."},
               "ignore_case": {"type": "boolean", "description": "Case-insensitive match (default false)."}},
              ["pattern"]),
    _fnschema("find_files",
              "Find files by NAME across the project — a glob like '*.py' / 'test_*.py', or a "
              "plain substring. Returns matching paths. Use it to locate where something lives "
              "before reading it.",
              {"pattern": {"type": "string", "description": "Filename glob or substring."},
               "path": {"type": "string", "description": "Directory to search under; defaults to '.'."}},
              ["pattern"]),
    _fnschema("list_dir", "List the entries of a directory.",
              {"path": {"type": "string", "description": "Directory path; defaults to '.'."}},
              []),
    _fnschema("write_file", "Create or overwrite a file with the given contents.",
              {"path": {"type": "string"}, "content": {"type": "string"}},
              ["path", "content"]),
    _fnschema("edit_file",
              "Replace text in a file. By default 'find' must occur EXACTLY ONCE — include "
              "enough surrounding context to make it unique. Pass replace_all=true to change "
              "every occurrence. Give 'find' verbatim; do not include read_file line numbers.",
              {"path": {"type": "string"},
               "find": {"type": "string", "description": "Exact text to replace (verbatim, no line numbers)."},
               "replace": {"type": "string", "description": "Replacement text."},
               "replace_all": {"type": "boolean", "description": "Replace all occurrences instead of requiring a unique match (default false)."}},
              ["path", "find", "replace"]),
    _fnschema("run_shell", "Run a shell command in the working directory and return its output.",
              {"command": {"type": "string"}},
              ["command"]),
]


def _clip(text, limit=MAX_TOOL_OUTPUT):
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... [truncated, {len(text) - limit} more chars]"


def _get(args, *keys, default=""):
    for k in keys:
        if k in args and args[k] is not None:
            return args[k]
    return default


def _as_bool(val, default=False):
    """Coerce a tool argument to bool — models send true/false, 1/0, or strings."""
    if isinstance(val, bool):
        return val
    if isinstance(val, (int, float)):
        return bool(val)
    if isinstance(val, str):
        return val.strip().lower() in ("1", "true", "yes", "y", "on")
    return default


def human_bytes(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n / 1.0:.1f} {unit}"
        n /= 1024.0


def _syntax_check(path, content):
    """Mechanical post-write verification for file types we can check with the
    stdlib alone. Returns (ok, message), or None when there is no checker for
    this file type. Never executes the code being checked."""
    ext = os.path.splitext(path.lower())[1]
    if ext == ".py":
        try:
            compile(content, path, "exec")
            return True, "python syntax OK"
        except SyntaxError as e:
            return False, f"python syntax error at line {e.lineno}: {e.msg}"
        except (ValueError, RecursionError) as e:  # NUL bytes / pathological nesting
            return False, f"python compile failed: {e}"
    if ext in (".sh", ".bash", ".sbatch", ".slurm"):
        if not shutil.which("bash"):
            return None
        try:
            proc = subprocess.run(["bash", "-n", path], capture_output=True,
                                  text=True, timeout=10)
        except (subprocess.SubprocessError, OSError):
            return None
        if proc.returncode == 0:
            return True, "bash syntax OK"
        tail = (proc.stderr or "").strip().splitlines()
        return False, "bash syntax error: " + (tail[-1].strip() if tail else "bash -n failed")
    if ext == ".json":
        try:
            json.loads(content)
            return True, "JSON valid"
        except json.JSONDecodeError as e:
            return False, f"invalid JSON at line {e.lineno}: {e.msg}"
    return None


def _iter_files(root, max_files=SEARCH_MAX_FILES):
    """Yield file paths under `root`, pruning heavy/generated/dot directories.
    Bounded by `max_files` so a huge tree can never run away on a shared FS.
    Directory and file order is sorted, so callers get stable results."""
    if os.path.isfile(root):
        yield root
        return
    seen = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames
                             if d not in SKIP_DIRS and not d.startswith("."))
        for name in sorted(filenames):
            yield os.path.join(dirpath, name)
            seen += 1
            if seen >= max_files:
                return


def _read_text_lines(path):
    """Lines of a texty file, or None if it is binary / too large / unreadable."""
    try:
        if os.path.getsize(path) > MAX_SCAN_FILE_BYTES:
            return None
        with open(path, "rb") as f:
            chunk = f.read(MAX_SCAN_FILE_BYTES + 1)
    except OSError:
        return None
    if b"\x00" in chunk:                      # NUL byte ⇒ almost certainly binary
        return None
    for enc in ("utf-8", "latin-1"):
        try:
            return chunk.decode(enc).splitlines()
        except UnicodeDecodeError:
            continue
    return None


class ToolBox:
    def __init__(self, ui, cwd, auto=False):
        self.ui = ui
        self.cwd = cwd
        self.auto = auto
        self.edits = 0
        self.cmds = 0

    def _resolve(self, path):
        path = os.path.expanduser(path or ".")
        if not os.path.isabs(path):
            path = os.path.join(self.cwd, path)
        return os.path.normpath(path)

    def _rel(self, path):
        try:
            return os.path.relpath(path, self.cwd)
        except ValueError:
            return path

    def dispatch(self, name, args):
        handler = {
            "read_file": self.read_file,
            "search_code": self.search_code,
            "find_files": self.find_files,
            "list_dir": self.list_dir,
            "write_file": self.write_file,
            "edit_file": self.edit_file,
            "run_shell": self.run_shell,
        }.get(name)
        if handler is None:
            self.ui.tool_dot(name or "?", self.ui.st.dim("unknown tool"))
            return f"error: unknown tool '{name}'"
        try:
            return handler(args)
        except Exception as e:  # never let a tool crash the session
            self.ui.error(f"{type(e).__name__}: {e}")
            return f"error: {type(e).__name__}: {e}"

    # ---- safe (no confirmation) --------------------------------------
    def read_file(self, args):
        p = self._resolve(_get(args, "path"))
        if not os.path.isfile(p):
            self.ui.tool_dot("read", self.ui.st.fg(self._rel(p)), "no such file")
            return f"error: no such file: {self._rel(p)}"
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
        total = len(lines)
        try:
            offset = max(1, int(_get(args, "offset", "start", default=1) or 1))
        except (TypeError, ValueError):
            offset = 1
        limit_raw = _get(args, "limit", "count", default=None)
        try:
            limit = int(limit_raw) if limit_raw not in (None, "") else None
        except (TypeError, ValueError):
            limit = None
        ranged = offset > 1 or limit is not None
        limit = DEFAULT_READ_LINES if limit is None else limit
        limit = max(1, min(limit, MAX_READ_LINES))
        start, end = offset - 1, min(total, offset - 1 + limit)
        shown = lines[start:end] if start < total else []
        width = max(1, len(str(end)))
        numbered = "\n".join(f"{start + k + 1:>{width}}\t{ln}" for k, ln in enumerate(shown))

        detail = (f"lines {offset}–{end} of {total}" if (ranged or end < total)
                  else f"{total} lines · {human_bytes(os.path.getsize(p))}")
        self.ui.tool_dot("read", self.ui.st.fg(self._rel(p)), detail)
        if total == 0:
            return "(empty file)"
        if start >= total:
            return f"[offset {offset} is past the end of the file ({total} lines)]"
        note = (f"\n... [showing lines {offset}-{end} of {total}; "
                f"read further with offset={end + 1}]" if end < total else "")
        return _clip(numbered + note, MAX_READ_OUTPUT)

    def list_dir(self, args):
        p = self._resolve(_get(args, "path", default="."))
        if not os.path.isdir(p):
            self.ui.tool_dot("list", self.ui.st.fg(self._rel(p)), "not a directory")
            return f"error: not a directory: {self._rel(p)}"
        entries = sorted(os.listdir(p))
        pretty = [(e + "/") if os.path.isdir(os.path.join(p, e)) else e for e in entries]
        self.ui.tool_dot("list", self.ui.st.fg(self._rel(p) or "."), f"{len(entries)} entries")
        self.ui.listing_block(pretty)
        return _clip("\n".join(pretty) or "(empty)")

    def search_code(self, args):
        pattern = _get(args, "pattern", "query", "q")
        pattern = str(pattern) if pattern not in (None, "") else ""
        if not pattern:
            self.ui.tool_dot("search", self.ui.st.dim("(no pattern)"))
            return "error: empty search pattern"
        root = self._resolve(_get(args, "path", default="."))
        regex_val = _get(args, "regex", default=None)
        as_regex = True if regex_val is None else _as_bool(regex_val, default=True)
        ignore_case = _as_bool(_get(args, "ignore_case", "i", default=False))
        globpat = _get(args, "glob", "include", default="") or ""
        try:
            rx = re.compile(pattern if as_regex else re.escape(pattern),
                            re.IGNORECASE if ignore_case else 0)
        except re.error as e:
            self.ui.tool_dot("search", self.ui.st.fg(str(pattern), b=True), "bad regex")
            return f"error: invalid regex {pattern!r}: {e}"

        hits, files_with_hits, truncated = [], 0, False
        for fp in _iter_files(root):
            if globpat and not fnmatch.fnmatch(os.path.basename(fp), globpat):
                continue
            lines = _read_text_lines(fp)
            if lines is None:
                continue
            rel, file_hit = self._rel(fp), False
            for lineno, line in enumerate(lines, 1):
                if rx.search(line):
                    hits.append(f"{rel}:{lineno}: {line.strip()[:200]}")
                    file_hit = True
                    if len(hits) >= SEARCH_MAX_RESULTS:
                        truncated = True
                        break
            files_with_hits += file_hit
            if truncated:
                break

        metric = (f"{len(hits)} hit{'s' if len(hits) != 1 else ''} · "
                  f"{files_with_hits} file{'s' if files_with_hits != 1 else ''}")
        self.ui.tool_dot("search", self.ui.st.fg(str(pattern), b=True), metric)
        if not hits:
            return (f"no matches for {pattern!r}"
                    + (f" in files matching {globpat!r}" if globpat else ""))
        self.ui.listing_block(hits, cap=12)
        out = "\n".join(hits)
        if truncated:
            out += f"\n... [stopped at {SEARCH_MAX_RESULTS} matches; narrow the pattern or pass a glob]"
        return _clip(out)

    def find_files(self, args):
        pattern = _get(args, "pattern", "name", "glob", "query")
        pattern = str(pattern) if pattern not in (None, "") else ""
        if not pattern:
            self.ui.tool_dot("find", self.ui.st.dim("(no pattern)"))
            return "error: empty filename pattern"
        root = self._resolve(_get(args, "path", default="."))
        has_glob = any(ch in pattern for ch in "*?[")
        matches, truncated = [], False
        for fp in _iter_files(root):
            base = os.path.basename(fp)
            hit = fnmatch.fnmatch(base, pattern) if has_glob else pattern.lower() in base.lower()
            if hit:
                matches.append(self._rel(fp))
                if len(matches) >= FIND_MAX_RESULTS:
                    truncated = True
                    break
        matches.sort()
        self.ui.tool_dot("find", self.ui.st.fg(str(pattern), b=True),
                         f"{len(matches)} file{'s' if len(matches) != 1 else ''}"
                         + (" +" if truncated else ""))
        if not matches:
            return f"no files matching {pattern!r} under {self._rel(root) or '.'}"
        self.ui.listing_block(matches, cap=20)
        out = "\n".join(matches)
        if truncated:
            out += f"\n... [stopped at {FIND_MAX_RESULTS} files]"
        return _clip(out)

    # ---- mutating (confirmation unless --auto) -----------------------
    def _post_write_check(self, p, content):
        """Syntax-gate a file that was just saved: show the result in the UI and
        append it to the tool reply so the model reacts on its very next turn."""
        chk = _syntax_check(p, content)
        if chk is None:
            return ""
        ok, msg = chk
        if ok:
            self.ui.note(msg)
            return f" ({msg})"
        self.ui.error(msg)
        return (f"\nWARNING: the file was saved but is broken — {msg}. "
                "Fix it now, before doing anything else.")

    def _approved(self, question):
        if self.auto:
            return True
        ans = self.ui.confirm(question)
        if ans == "a":
            self.auto = True
            return True
        return ans == "y"

    def write_file(self, args):
        p = self._resolve(_get(args, "path"))
        content = _get(args, "content")
        exists = os.path.isfile(p)
        verb = "overwrite" if exists else "create"
        self.ui.tool_dot("write", self.ui.st.fg(self._rel(p)),
                         f"{verb} · {content.count(chr(10)) + 1} lines")
        self.ui.file_card(self._rel(p), content, new=not exists)
        if not self._approved(f"{verb} {self._rel(p)}?"):
            self.ui.note("declined")
            return "error: user declined the write"
        os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(content)
        self.edits += 1
        self.ui.ok(f"wrote {self._rel(p)}")
        return f"ok: wrote {self._rel(p)} ({len(content)} bytes)" + self._post_write_check(p, content)

    @staticmethod
    def _strip_line_numbers(text):
        """If EVERY non-blank line of `text` is prefixed 'N\\t' (a read_file paste),
        return it with those prefixes removed; otherwise None. Lets an edit whose
        'find' was copied straight out of read_file still land."""
        lines = text.split("\n")
        pat = re.compile(r"^\s*\d+\t")
        real = [ln for ln in lines if ln.strip()]
        if real and all(pat.match(ln) for ln in real):
            return "\n".join(pat.sub("", ln) for ln in lines)
        return None

    @staticmethod
    def _near_miss(body, find):
        """A short hint at the closest existing line to a 'find' that wasn't present."""
        head = next((ln for ln in (find or "").splitlines() if ln.strip()), "")[:80]
        if not head:
            return ""
        best = difflib.get_close_matches(head, body.splitlines(), n=1, cutoff=0.5)
        return f" Closest line in the file: {best[0].strip()[:120]!r}" if best else ""

    def edit_file(self, args):
        p = self._resolve(_get(args, "path"))
        find = _get(args, "find", "old", "old_string")
        repl = _get(args, "replace", "new", "new_string")
        replace_all = _as_bool(_get(args, "replace_all", "all", "global", default=False))
        if not os.path.isfile(p):
            self.ui.tool_dot("edit", self.ui.st.fg(self._rel(p)), "no such file")
            return f"error: no such file: {self._rel(p)}"
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            body = f.read()
        n = body.count(find) if find else 0
        if find and n == 0:                       # tolerate a line-numbered paste
            stripped = self._strip_line_numbers(find)
            if stripped and body.count(stripped) > 0:
                find, n = stripped, body.count(stripped)
        if not find or n == 0:
            self.ui.tool_dot("edit", self.ui.st.fg(self._rel(p)), "text not found")
            return ("error: 'find' text not present in file; read it first, then retry with "
                    "an exact match." + self._near_miss(body, find))
        if n > 1 and not replace_all:
            self.ui.tool_dot("edit", self.ui.st.fg(self._rel(p)), f"{n} matches — ambiguous")
            return (f"error: 'find' matches {n} times in {self._rel(p)} — it must be unique. "
                    "Add surrounding context to target one occurrence, or pass replace_all=true.")
        after = body.replace(find, repl)
        self.ui.tool_dot("edit", self.ui.st.fg(self._rel(p)),
                         f"{n} replacement{'s' if n != 1 else ''}")
        self.ui.diff_card(self._rel(p), body, after)
        if not self._approved(f"apply this edit to {self._rel(p)}?"):
            self.ui.note("declined")
            return "error: user declined the edit"
        with open(p, "w", encoding="utf-8") as f:
            f.write(after)
        self.edits += 1
        self.ui.ok(f"edited {self._rel(p)}")
        return f"ok: replaced {n} occurrence(s) in {self._rel(p)}" + self._post_write_check(p, after)

    def run_shell(self, args):
        cmd = _get(args, "command", "cmd")
        if not cmd:
            return "error: empty command"
        self.ui.tool_dot("run", self.ui.st.fg(cmd, b=True))
        if not self._approved("run this command?"):
            self.ui.note("declined")
            return "error: user declined to run the command"
        t0 = time.time()
        try:
            proc = subprocess.run(cmd, shell=True, cwd=self.cwd, text=True,
                                  capture_output=True, timeout=RUN_TIMEOUT)
            dur = time.time() - t0
            out = (proc.stdout or "") + (proc.stderr or "")
            self.ui.run_card(cmd, out, proc.returncode, dur)
            self.cmds += 1
            return _clip(f"exit code: {proc.returncode}\n{out}")
        except subprocess.TimeoutExpired:
            self.ui.run_card(cmd, f"timed out after {RUN_TIMEOUT}s", 124, time.time() - t0)
            return f"error: command timed out after {RUN_TIMEOUT}s"


# --------------------------------------------------------------------------
# System prompts
# --------------------------------------------------------------------------
def _slurm_context():
    """Short description of the Slurm allocation we are inside ('' outside one)."""
    job = os.environ.get("SLURM_JOB_ID")
    if not job:
        return ""
    bits = [f"Slurm job {job}"]
    part = os.environ.get("SLURM_JOB_PARTITION")
    if part:
        bits.append(f"partition {part}")
    cpus = os.environ.get("SLURM_CPUS_PER_TASK")
    if cpus:
        bits.append(f"{cpus} CPU cores")
    gpus = pretty_gpus(os.environ.get("GOAT_GPUS", ""))
    if gpus:
        bits.append(gpus)
    return ", ".join(bits)


def system_prompt(model, node, cwd):
    alloc = _slurm_context()
    where = (f"inside a Slurm allocation ({alloc}) on compute node '{node}'"
             if alloc else f"on node '{node}'")
    return (
        "You are Engaging Coder, an expert software engineer working directly inside a "
        "user's project on the MIT Engaging HPC cluster. "
        f"You are the model '{model}', running locally {where}. "
        f"The working directory is '{cwd}'.\n\n"
        "Your tools: search_code (regex search across the project), find_files (locate files "
        "by name), read_file (with offset/limit paging for big files), list_dir, write_file, "
        "edit_file, run_shell (executes on this node inside this job's CPU/RAM limits — "
        "build and test here, but submit separate heavy or long-running work with sbatch).\n\n"
        "Work like a careful engineer: SEARCH and READ to understand the code before you "
        "change it — never fabricate file contents, APIs, or command output. Prefer edit_file "
        "for small, targeted changes and write_file for new files. read_file shows line "
        "numbers for reference only — never copy them into edit_file, and remember edit_file "
        "needs its 'find' text to be unique (quote enough surrounding context) unless you pass "
        "replace_all=true.\n\n"
        "VERIFY, DON'T ASSERT: after changing code, prove it works with run_shell — run the "
        "test suite, the script itself, or at least a compile/import — and read the exit code "
        "and output before drawing conclusions. Files you write are syntax-checked "
        "automatically; if a WARNING comes back, fix it before anything else. Never claim "
        "code works, compiles, or passes tests unless you ran it here and saw it succeed; if "
        "something was not verified, say so explicitly. File edits and shell commands are "
        "shown to the user for approval, so act decisively and explain what you are doing.\n\n"
        "Be concise. Format replies as markdown; put code in fenced blocks with a language "
        "tag. When a task is done, summarize what changed and how it was verified."
    )


def chat_prompt(model, node):
    return (
        f"You are a helpful, sharp assistant (the model '{model}') running locally and "
        f"privately on MIT Engaging compute node '{node}' — no cloud, no data leaves the "
        "cluster. Format replies as markdown; put code in fenced blocks with a language "
        "tag. Be direct and concise; use structure (headings, lists) only when it helps."
    )


# --------------------------------------------------------------------------
# Agent loop
# --------------------------------------------------------------------------
class Agent:
    def __init__(self, client, ui, toolbox, use_tools=True):
        self.client = client
        self.ui = ui
        self.toolbox = toolbox
        self.tools = TOOL_SCHEMA if use_tools else None
        self.spinner = Spinner(ui.st)
        self.turns = 0

    def run_turn(self, messages):
        self.turns += 1
        while True:
            content = []
            tool_calls = []
            error = None
            done_obj = None
            md = None

            def ensure_stream():
                nonlocal md
                if md is None:
                    self.spinner.stop()
                    self.ui.assistant_header(self.client.model)
                    md = MarkdownStream(self.ui.st)
                return md

            self.spinner.start("thinking")
            try:
                for kind, data in self.client.stream_chat(messages, self.tools):
                    if kind == "think":
                        ensure_stream().feed_think(data)
                    elif kind == "token":
                        content.append(data)
                        ensure_stream().feed(data)
                    elif kind == "tool_calls":
                        tool_calls.extend(data)
                    elif kind == "error":
                        error = data
                    elif kind == "done":
                        done_obj = data
            except KeyboardInterrupt:
                self.spinner.stop()
                if md:
                    md.close()
                self.ui.note("interrupted")
                return messages
            finally:
                self.spinner.stop()

            if md:
                md.close()

            if error:
                low = error.lower()
                if self.tools and ("does not support tools" in low or ("tool" in low and "support" in low)):
                    self.ui.note("this model has no tool support — continuing as plain chat")
                    self.tools = None
                    continue
                self.ui.error(error)
                return messages

            assistant = {"role": "assistant", "content": "".join(content)}
            if tool_calls:
                assistant["tool_calls"] = tool_calls
            messages.append(assistant)

            if not tool_calls:
                if done_obj:
                    self.ui.turn_footer(done_obj)
                return messages

            for tc in tool_calls:
                name, targs = parse_tool_call(tc)
                result = self.toolbox.dispatch(name, targs)
                messages.append({"role": "tool", "tool_name": name, "content": result})
            # loop again so the model can react to tool results


# --------------------------------------------------------------------------
# REPL
# --------------------------------------------------------------------------
def pretty_gpus(spec):
    """'h200:3' -> '3× H200'; '1' -> ''; passthrough otherwise."""
    if not spec or spec == "1":
        return ""
    if ":" in spec:
        kind, _, n = spec.partition(":")
        if n.isdigit():
            return f"{n}× {kind.upper()}"
    return spec


def detect_gpus():
    spec = os.environ.get("GOAT_GPUS", "")
    if spec:
        return pretty_gpus(spec)
    cuda = os.environ.get("CUDA_VISIBLE_DEVICES", "")
    if cuda:
        return f"{len([c for c in cuda.split(',') if c.strip()])}× GPU"
    cpus = os.environ.get("SLURM_CPUS_PER_TASK", "")
    if cpus:
        return f"CPU · {cpus} cores"
    return ""


def handle_slash(cmd, agent, ui, messages, base_system):
    """Return True if the REPL should continue, False to exit."""
    parts = cmd.split()
    head = parts[0].lower()
    rest = cmd[len(parts[0]):].strip()
    if head in ("/exit", "/quit", "/q", "/bye"):
        return False
    if head == "/help":
        ui.help()
    elif head == "/clear":
        del messages[1:]
        ui.note("conversation cleared")
    elif head == "/model":
        ui.note(f"model: {agent.client.model}   ·   tools: {'on' if agent.tools else 'off'}")
    elif head == "/auto" and not ui.chat:
        agent.toolbox.auto = not agent.toolbox.auto
        ui.note(f"auto-approve is now {'ON — edits & commands run without asking' if agent.toolbox.auto else 'OFF'}")
    elif head == "/add" and not ui.chat:
        if not rest:
            ui.error("usage: /add <path> [more paths]")
        else:
            blob = []
            for path in rest.split():
                rp = agent.toolbox._resolve(path)
                if os.path.isfile(rp):
                    with open(rp, "r", encoding="utf-8", errors="replace") as f:
                        blob.append(f"--- {path} ---\n{f.read()}")
                    ui.note(f"added {path}")
                else:
                    ui.error(f"not a file: {path}")
            if blob:
                messages.append({"role": "user", "content": "Here are files for context:\n\n" + "\n\n".join(blob)})
    elif head == "/run" and not ui.chat:
        if not rest:
            ui.error("usage: /run <command>")
        else:
            out = agent.toolbox.run_shell({"command": rest})
            messages.append({"role": "user", "content": f"I ran `{rest}` and got:\n{out}"})
    else:
        ui.error(f"unknown command: {head}  (try /help)")
    return True


def repl(agent, ui, base_system, initial=None, oneshot=False, hold_note=False):
    messages = [{"role": "system", "content": base_system}]
    t0 = time.time()
    if initial:
        messages.append({"role": "user", "content": initial})
        agent.run_turn(messages)
        if oneshot:
            return
        if hold_note:
            print()
            ui.ok("holding this allocation — next prompts are instant, no new queue")
    while True:
        try:
            print()
            line = ui.user_prompt()
        except EOFError:
            print()
            break
        except KeyboardInterrupt:
            print()
            ui.note("(/bye or Ctrl-D to quit)" if ui.chat else "(Ctrl-D or /exit to quit)")
            continue
        line = line.strip()
        if not line:
            continue
        if line.startswith("/"):
            if not handle_slash(line, agent, ui, messages, base_system):
                break
            continue
        messages.append({"role": "user", "content": line})
        agent.run_turn(messages)
    ui.session_footer({"dur": time.time() - t0, "turns": agent.turns,
                       "edits": agent.toolbox.edits, "cmds": agent.toolbox.cmds})


# --------------------------------------------------------------------------
# Offline self-test (no network) so the tool can be verified on any node
# --------------------------------------------------------------------------
def selftest():
    st = Style(False)
    stc = Style(True)
    ok = True

    def check(name, cond):
        nonlocal ok
        ok = ok and cond
        print(f"  {'PASS' if cond else 'FAIL'}  {name}")

    # stream line parsing
    evs = list(parse_stream_line('{"message":{"content":"hel"},"done":false}'))
    check("token parse", evs == [("token", "hel")])
    evs = list(parse_stream_line(
        '{"message":{"content":"","tool_calls":[{"function":{"name":"run_shell","arguments":{"command":"ls"}}}]},"done":true}'))
    kinds = [e[0] for e in evs]
    check("tool_calls + done parse", kinds == ["tool_calls", "done"])
    check("error parse", list(parse_stream_line('{"error":"boom"}')) == [("error", "boom")])
    check("garbage line ignored", list(parse_stream_line("not json")) == [])
    evs = list(parse_stream_line('{"message":{"thinking":"hmm"},"done":false}'))
    check("thinking parse", evs == [("think", "hmm")])

    # tool-call extraction, incl. stringified arguments
    n, a = parse_tool_call({"function": {"name": "edit_file", "arguments": '{"path":"x"}'}})
    check("tool_call str-args", n == "edit_file" and a == {"path": "x"})

    # ANSI width helpers
    styled = stc.gold("abc", b=True)
    check("vlen strips ansi", vlen(styled) == 3)
    check("vpad pads visible", vlen(vpad(styled, 10)) == 10)
    check("vtrunc", vtrunc("abcdefgh", 5) == "abcd…" and vtrunc("ab", 5) == "ab")

    # lexer: keywords, strings, comments, cross-line state
    lx = Lexer(stc, "python")
    line = lx.highlight("def foo(x):  # hi")
    check("lexer emits ansi", "\x1b[" in line and "def" in ANSI_RE.sub("", line))
    lx2 = Lexer(stc, "python")
    lx2.highlight('s = """start')
    check("lexer enters mlstring", lx2.state == ("mlstr", '"""'))
    lx2.highlight('still inside"""')
    check("lexer exits mlstring", lx2.state is None)
    check("lexer plain passthrough", Lexer(st, "python").highlight("def x():") == "def x():")
    check("lang from path", lang_for_path("a/b.py") == "python" and lang_for_path("Makefile") == "bash")
    check("lang alias", norm_lang("PY") == "python" and norm_lang("c++") == "c")

    # markdown stream: fences open/close a card, think tags filtered
    import io
    buf = io.StringIO()
    old = sys.stdout
    sys.stdout = buf
    try:
        md = MarkdownStream(st)
        md.feed("hello **wor")
        md.feed("ld**\n```python\nx = 1\n```\nplain\n")
        md.close()
    finally:
        sys.stdout = old
    out = buf.getvalue()
    check("md prose streamed", "hello world" in out)
    check("md fence card drawn", "╭" in out and "x = 1" in out and "╰" in out)
    buf = io.StringIO()
    sys.stdout = buf
    try:
        md = MarkdownStream(st)
        md.feed("<think>secret plan</think>answer\n")
        md.close()
    finally:
        sys.stdout = old
    out = buf.getvalue()
    check("think block rendered dim + kept", "thinking" in out and "answer" in out)
    buf = io.StringIO()
    sys.stdout = buf
    try:
        md = MarkdownStream(st)
        md.feed("<thi")            # tag split across tokens
        md.feed("nk>x</think>ok\n")
        md.close()
    finally:
        sys.stdout = old
    check("split think tag handled", "ok" in buf.getvalue())
    buf = io.StringIO()
    sys.stdout = buf
    try:
        md = MarkdownStream(stc)   # colored: native thinking must NOT dim the answer
        md.feed_think("plan it out")
        md.feed("## Done\n")
        md.close()
    finally:
        sys.stdout = old
    out = buf.getvalue()
    plain = ANSI_RE.sub("", out)
    check("native think closes on content", "thought" in plain and "Done" in plain)
    check("content after think not dimmed", "38;2;95;95;108" not in out.rsplit("thought", 1)[-1])

    # diff card renders +/- with line numbers
    buf = io.StringIO()
    sys.stdout = buf
    try:
        UI(st).diff_card("t.py", "a\nb\nc\n", "a\nB\nc\n")
    finally:
        sys.stdout = old
    out = buf.getvalue()
    check("diff card + and -", "+" in out and "−" in out and "B" in out)

    # tools against a temp dir
    import tempfile
    d = tempfile.mkdtemp(prefix="engaging-coder-selftest-")
    buf = io.StringIO()
    sys.stdout = buf
    try:
        ui = UI(st)
        tb = ToolBox(ui, d, auto=True)
        r1 = "ok:" in tb.write_file({"path": "a.txt", "content": "hello\nworld\n"})
        r2 = "hello" in tb.read_file({"path": "a.txt"})
        r3 = "ok:" in tb.edit_file({"path": "a.txt", "find": "world", "replace": "there"})
        r4 = "there" in open(os.path.join(d, "a.txt")).read()
        r5 = "error:" in tb.edit_file({"path": "a.txt", "find": "nope", "replace": "x"})
        r6 = "a.txt" in tb.list_dir({"path": "."})
        r7 = "exit code: 0" in tb.run_shell({"command": "echo hi"})
        r8 = "error:" in tb.read_file({"path": "ghost.txt"})
    finally:
        sys.stdout = old
    for nm, r in [("write_file", r1), ("read_file", r2), ("edit_file", r3),
                  ("edit content applied", r4), ("edit_file missing text", r5),
                  ("list_dir", r6), ("run_shell", r7), ("read missing", r8)]:
        check(nm, r)

    # search_code / find_files / ranged read / smart edit
    buf = io.StringIO()
    sys.stdout = buf
    try:
        ui = UI(st)
        tb = ToolBox(ui, d, auto=True)
        tb.write_file({"path": "pkg/mod.py",
                       "content": "def alpha():\n    return 1\n\ndef beta():\n    return alpha()\n"})
        tb.write_file({"path": "pkg/util.py", "content": "X = 1\nY = 2\nX = 3\nZ = X\n"})
        tb.write_file({"path": "node_modules/lib.py", "content": "import x\n"})  # heavy dir
        s_hits = tb.search_code({"pattern": r"def \w+"})
        s_glob = tb.search_code({"pattern": "alpha", "glob": "*.py"})
        s_none = tb.search_code({"pattern": "zzzznope"})
        s_lit = tb.search_code({"pattern": "alpha()", "regex": False})
        f_glob = tb.find_files({"pattern": "*.py"})
        f_sub = tb.find_files({"pattern": "util"})
        rng = tb.read_file({"path": "pkg/mod.py", "offset": 4, "limit": 1})
        numbered = tb.read_file({"path": "pkg/util.py"})
        amb = tb.edit_file({"path": "pkg/util.py", "find": "X = ", "replace": "Z = "})
        uniq = tb.edit_file({"path": "pkg/mod.py", "find": "return 1", "replace": "return 2"})
        allrep = tb.edit_file({"path": "pkg/util.py", "find": "X", "replace": "W", "replace_all": True})
        miss = tb.edit_file({"path": "pkg/mod.py", "find": "def alfa():", "replace": "x"})
        lnpaste = tb.edit_file({"path": "pkg/mod.py", "find": "1\tdef alpha():", "replace": "def gamma():"})
        mod_after = open(os.path.join(d, "pkg", "mod.py")).read()
    finally:
        sys.stdout = old
    check("search_code regex", "pkg/mod.py:1:" in s_hits and "pkg/mod.py:4:" in s_hits)
    check("search_code glob filter", "alpha" in s_glob and "util.py" not in s_glob)
    check("search_code no match", "no matches" in s_none)
    check("search_code literal", "pkg/mod.py:5:" in s_lit)
    check("find_files glob", "pkg/mod.py" in f_glob and "pkg/util.py" in f_glob)
    check("find_files substring", "pkg/util.py" in f_sub and "mod.py" not in f_sub)
    check("search/find skip heavy dirs", "node_modules" not in f_glob)
    check("read_file range", "4\tdef beta():" in rng and "alpha" not in rng)
    check("read_file numbered", "1\tX = 1" in numbered)
    check("edit ambiguous refused", "must be unique" in amb and "2 times" in amb)
    check("edit unique applied", "ok:" in uniq)
    check("edit replace_all", "ok:" in allrep and "3 occurrence" in allrep)
    check("edit near-miss hint", "Closest line" in miss)
    check("edit tolerates pasted line numbers", "ok:" in lnpaste and "def gamma():" in mod_after)
    shutil.rmtree(d, ignore_errors=True)

    # post-write syntax verification (the mechanical anti-BS gate)
    d2 = tempfile.mkdtemp(prefix="engaging-coder-verify-")
    buf = io.StringIO()
    sys.stdout = buf
    try:
        ui = UI(st)
        tb = ToolBox(ui, d2, auto=True)
        good_py = tb.write_file({"path": "ok.py", "content": "def f():\n    return 1\n"})
        bad_py = tb.write_file({"path": "bad.py", "content": "def f(:\n    pass\n"})
        bad_json = tb.write_file({"path": "cfg.json", "content": "{not json}"})
        broke = tb.edit_file({"path": "ok.py", "find": "return 1", "replace": "return ("})
        bad_kept = os.path.isfile(os.path.join(d2, "bad.py"))
        if shutil.which("bash"):
            good_sh = tb.write_file({"path": "run.sh", "content": "echo hi\n"})
            bad_sh = tb.write_file({"path": "boom.sh", "content": "if true; then\n"})
        else:  # no bash on this node: checker opts out, nothing to assert
            good_sh, bad_sh = "bash syntax OK", "WARNING bash syntax error"
    finally:
        sys.stdout = old
    check("write .py syntax ok noted", "python syntax OK" in good_py)
    check("write .py syntax error flagged", "WARNING" in bad_py and "syntax error" in bad_py)
    check("flagged file still saved", bad_kept)
    check("write .json invalid flagged", "invalid JSON" in bad_json)
    check("edit that breaks syntax flagged", "WARNING" in broke)
    check("write .sh syntax ok noted", "bash syntax OK" in good_sh)
    check("write .sh syntax error flagged", "bash syntax error" in bad_sh)
    shutil.rmtree(d2, ignore_errors=True)

    # system prompt: names the real allocation + mandates verification
    saved_env = {k: os.environ.get(k) for k in
                 ("SLURM_JOB_ID", "SLURM_JOB_PARTITION", "SLURM_CPUS_PER_TASK", "GOAT_GPUS")}
    try:
        os.environ.update({"SLURM_JOB_ID": "42", "SLURM_JOB_PARTITION": "mit_normal",
                           "SLURM_CPUS_PER_TASK": "16", "GOAT_GPUS": "h200:2"})
        sp = system_prompt("m", "node1", "/tmp")
    finally:
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    check("system prompt names the allocation",
          "Slurm job 42" in sp and "mit_normal" in sp and "2× H200" in sp)
    check("system prompt mandates verification", "VERIFY, DON'T ASSERT" in sp)

    check("pretty_gpus", pretty_gpus("h200:3") == "3× H200" and pretty_gpus("1") == "")
    print("\n  " + ("ALL PASSED" if ok else "FAILURES ABOVE"))
    return 0 if ok else 1


# --------------------------------------------------------------------------
# Demo — render every UI element with canned data (no model, no network).
# --------------------------------------------------------------------------
def demo():
    st = Style(supports_color())
    ui = UI(st)
    ui.banner("qwen3-coder:480b", "node2433", "3× H200", os.getcwd())

    print(st.gold("❯ ", b=True) + st.fg("tighten up the config loader and run the tests"))
    print()
    ui.assistant_header("qwen3-coder:480b")
    md = MarkdownStream(st)
    md.feed("I'll look at the loader first, then make a **surgical fix**.\n\n")
    md.close()

    ui.tool_dot("find", st.fg("config.py", b=True), "1 file")
    ui.listing_block(["src/config.py"])
    ui.tool_dot("search", st.fg(r"def (load|save)", b=True), "2 hits · 1 file")
    ui.listing_block(["src/config.py:3: def load(path):",
                      "src/config.py:6: def save(path, data):"], cap=12)
    ui.tool_dot("read", st.fg("src/config.py"), "58 lines · 1.8 KB")
    ui.tool_dot("list", st.fg("src"), "4 entries")
    ui.listing_block(["__init__.py", "config.py", "parser.py", "tests/"])

    before = ('import json\n\ndef load(path):\n    return json.load(open(path))\n\n'
              'def save(path, data):\n    json.dump(data, open(path, "w"))\n')
    after = ('import json\n\ndef load(path):\n    with open(path) as f:\n        return json.load(f)\n\n'
             'def save(path, data):\n    with open(path, "w") as f:\n        json.dump(data, f, indent=2)\n')
    ui.tool_dot("edit", st.fg("src/config.py"), "2 replacements")
    ui.diff_card("src/config.py", before, after)
    ui.ok("edited src/config.py")

    ui.tool_dot("write", st.fg("src/tests/test_config.py"), "create · 9 lines")
    ui.file_card("src/tests/test_config.py",
                 'import json\nfrom src.config import load, save\n\n\n'
                 'def test_roundtrip(tmp_path):\n    p = tmp_path / "c.json"\n'
                 '    save(p, {"gpu": "h200"})   # write\n'
                 '    assert load(p)["gpu"] == "h200"\n', new=True)
    ui.ok("wrote src/tests/test_config.py")

    ui.tool_dot("run", st.fg("pytest -q", b=True))
    ui.run_card("pytest -q", ".....\n5 passed in 0.42s", 0, 0.9)

    md = MarkdownStream(st)
    md.feed("## What changed\n")
    md.feed("- `load`/`save` now close their file handles (context managers)\n")
    md.feed("- `save` pretty-prints with `indent=2`\n")
    md.feed("- added a **round-trip test** — all 5 tests pass\n\n")
    md.feed("```bash\npytest -q          # 5 passed in 0.42s\n```\n")
    md.close()
    ui.turn_footer({"eval_count": 214, "eval_duration": 8_140_000_000})

    print()
    ui.session_footer({"dur": 724, "turns": 3, "edits": 2, "cmds": 1})
    return 0


# --------------------------------------------------------------------------
def main(argv=None):
    ap = argparse.ArgumentParser(description="Engaging Coder / GOAT Chat — terminal UI for local Ollama models.")
    ap.add_argument("--model", default=os.environ.get("MODEL", "qwen3-coder:480b"))
    ap.add_argument("--host", default=os.environ.get("OLLAMA_HOST", "127.0.0.1:11434"))
    ap.add_argument("--gpus", default=os.environ.get("OLLAMA_CODE_GPUS", ""))
    ap.add_argument("--temperature", type=float, default=None)
    ap.add_argument("--chat", action="store_true", help="chat mode: no tools, chat persona")
    ap.add_argument("--oneshot", action="store_true", help="answer the initial prompt and exit")
    ap.add_argument("--auto", action="store_true", help="auto-approve edits and commands")
    ap.add_argument("--no-color", action="store_true")
    ap.add_argument("--selftest", action="store_true", help="run offline checks and exit")
    ap.add_argument("--demo", action="store_true", help="render the UI with canned data and exit")
    ap.add_argument("prompt", nargs="*", help="optional initial task; omit for interactive mode")
    args = ap.parse_args(argv)

    if args.selftest:
        return selftest()
    if args.demo:
        return demo()

    style = Style(supports_color() and not args.no_color)
    ui = UI(style, chat=args.chat)
    cwd = os.getcwd()
    node = socket.gethostname().split(".")[0]
    temp = args.temperature if args.temperature is not None else (0.7 if args.chat else 0.2)
    client = Ollama(args.host, args.model, temperature=temp)
    auto = args.auto or not sys.stdin.isatty()
    toolbox = ToolBox(ui, cwd, auto=auto)
    agent = Agent(client, ui, toolbox, use_tools=not args.chat)

    initial = " ".join(args.prompt).strip() or None
    oneshot = args.oneshot or (initial is not None and not sys.stdin.isatty())

    if not oneshot or initial is None:
        ui.banner(args.model, node, detect_gpus() or pretty_gpus(args.gpus), cwd)
        if auto and not args.auto and not args.chat:
            ui.note("non-interactive input detected — auto-approving tools")

    base = chat_prompt(args.model, node) if args.chat else system_prompt(args.model, node, cwd)
    try:
        repl(agent, ui, base, initial=initial, oneshot=oneshot,
             hold_note=(initial is not None and not oneshot))
    except KeyboardInterrupt:
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
