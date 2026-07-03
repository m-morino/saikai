#!/usr/bin/env python3
"""
saikai_terminal — a live, interactive PTY terminal as a Textual widget.

This module backs saikai's TRUE SPLIT-LIVE mode: the left pane stays the
session DataTable; the right pane hosts one or more live agent CLI processes,
each in its own tab, each rendered from a real pseudo-console.

Building blocks (real PTY lifecycle is smoke-tested on all CI operating
systems; live visual render + keystroke behavior still needs native interactive
review — see NOTE):

  * pywinpty (ConPTY)  — spawn an interactive child attached to a pseudo
    console; blocking read() returns ``str`` and raises ``EOFError`` at EOF;
    ``setwinsize(rows, cols)`` on resize; ``taskkill /T /F`` by pid for a
    clean tree kill.
  * pyte                — turn the child's ANSI/VT byte stream into a grid of
    styled cells we re-render every frame via Textual's Line API.
  * textual             — ``render_line(y) -> Strip`` for the grid; ``on_key``
    -> PTY bytes; background reader thread + ``call_from_thread`` for repaint.

POSIX note: pywinpty is Windows-only. On POSIX we fall back to ``ptyprocess``,
which exposes the same surface we use (spawn / read / write / setwinsize /
isalive / pid). The widget runs on both; saikai's primary host is Windows.

NOTE — what can and cannot be verified without an interactive TTY
-----------------------------------------------------------------
CANNOT (needs a human at a terminal):
  * the live visual render (Textual paints the alternate screen) and real
    keyboard forwarding into a running agent CLI.
CAN:
  * ``python -m py_compile saikai_terminal.py``
  * PTY spawn + resize + threaded read + EOF + exit detection
  * pyte ctor/resize argument order, cell-attribute extraction, alt-screen
    mode-bit detection
  * the pure functions here: ``encode_key``, ``classify_pty_status``,
    ``_pyte_color``, ``AltScreenTracker``.

Design stance: correctness and graceful failure over features. Every PTY /
import / decode operation is defensive; a failure degrades the pane to an
error line — it never tears down the host app.
"""
from __future__ import annotations

import atexit
import os
import re
import signal
import subprocess
import sys
import threading
import time
from typing import Callable, Optional

# Platform branch as a module flag (not inline sys.platform checks) so the
# headless tests can exercise the POSIX kill path on the Windows dev box.
_IS_WIN = sys.platform == "win32"

# Per-pane pyte scrollback depth. Each retained history line costs memory
# (≈ cols × a pyte Char object); at 200 cols a FULL 5000-line history measured
# ~95 MB PER pane, so a handful of open panes pushed the saikai process into the
# high hundreds of MB. Default trimmed to 2000 (~39 MB worst case); saikai.py
# overrides this at startup from [limits] scrollback_lines / SAIKAI_SCROLLBACK
# (clamped). Lower it (e.g. 1000 ≈ 20 MB/pane) on a memory-tight machine.
SCROLLBACK_LINES = 2000


def _log(msg: str) -> None:
    """Best-effort append to the shared saikai.log (same file saikai.py's _log
    writes; standalone here so this module keeps no saikai import). Size-bounded,
    never raises. `[term]` tags lines from the split-live PTY layer so a
    post-mortem can tell the process lifecycle from the list-side events."""
    try:
        d = os.path.join(os.path.expanduser("~"), ".cache", "saikai")
        os.makedirs(d, exist_ok=True)
        lf = os.path.join(d, "saikai.log")
        try:
            if os.path.getsize(lf) > 1_000_000:
                os.replace(lf, lf + ".1")
        except OSError:
            pass
        with open(lf, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}  [term] {msg}\n")
    except Exception:
        pass


# SAIKAI_IME_DEBUG=1 logs IME-anchor moves + pane focus/blur to ~/.cache/saikai/
# saikai.log, so the cursor/IME behaviour on a given terminal can be inspected
# after the fact. The IME candidate window anchors to wherever app.cursor_position
# parks the hardware cursor (Windows Terminal reads the cursor CELL, not its
# visibility) — these lines show that cell as it changes, plus when the pane gains
# or loses focus, so a post-mortem can tell whether the anchor was correct/current.
_IME_DEBUG = str(os.environ.get("SAIKAI_IME_DEBUG", "")).strip().lower() in (
    "1", "true", "yes", "on")

# Custom IME-anchor machinery (keepalive blink + cursor re-anchor to claude's
# prompt cell + native hardware cursor + the App._display re-assert). It puts the
# WT IME/composition window AT the claude prompt. DEFAULT ON: without it the IME
# window sits at Textual's default cursor (wrong place — unusable for CJK input),
# which is worse than the known alt-tab ×/ON flicker. Set SAIKAI_IME_ANCHOR=0 to
# turn it off (cursor handling then matches a stock Textual app — flicker-free but
# the IME window is mispositioned). (#ime-anchor-optout)
_IME_ANCHOR = str(os.environ.get("SAIKAI_IME_ANCHOR", "1")).strip().lower() not in (
    "0", "false", "no", "off")

# Opt-in raw-PTY capture: when SAIKAI_PTY_CAPTURE names a file, every decoded chunk
# the reader feeds is appended as repr() (escape sequences visible) — for diagnosing
# how a child renders, e.g. whether an agent TUI drives ?1049 alt-screen, ?2026
# synchronized output, or ?1000/?1006 mouse reporting (which terminal scrollback and
# saikai's pyte mirror handle differently). Off by default; debug only.
_PTY_CAPTURE = os.environ.get("SAIKAI_PTY_CAPTURE", "").strip()


def _ime_anchor_xy(cursor_x, cursor_y, rx, ry, rw, rh):
    """Pure geometry for the terminal-cursor / IME anchor: map claude's grid cursor
    (cursor_x, cursor_y) inside a content region at screen origin (rx, ry) sized
    rw x rh to the absolute screen cell (x, y), clamped into the region. Returns
    None for an empty region. Kept module-level (no textual dep) so it is unit-
    testable headless; the widget wraps the result in a textual Offset."""
    if rw <= 0 or rh <= 0:
        return None
    x = rx + max(0, min(int(cursor_x), rw - 1))
    y = ry + max(0, min(int(cursor_y), rh - 1))
    return (x, y)


# ── global reap-thread registry ───────────────────────────────────────────────
# Every kill() spawns a daemon thread running `taskkill /F /T` to reap the
# child's grandchildren (claude's node workers). If saikai exits before that
# taskkill finishes the daemon dies and the workers orphan (the 0fd9fcf hazard).
# on_unmount-driven teardown and exceptions don't route through the App's
# join_reaps, so track EVERY reap here and join them at interpreter exit.
_REAP_THREADS: list = []
_REAP_LOCK = threading.Lock()


def _track_reap(t) -> None:
    if t is None:
        return
    with _REAP_LOCK:
        _REAP_THREADS[:] = [x for x in _REAP_THREADS if x.is_alive()]
        _REAP_THREADS.append(t)


def join_all_reaps(timeout: float = 3.0) -> None:
    """Bounded-join every tracked reap so process exit doesn't orphan node
    workers. Safe to call repeatedly; prunes finished threads."""
    import time
    deadline = time.monotonic() + timeout
    with _REAP_LOCK:
        threads = list(_REAP_THREADS)
    for t in threads:
        try:
            t.join(timeout=max(0.0, deadline - time.monotonic()))
        except Exception:
            pass
    with _REAP_LOCK:
        _REAP_THREADS[:] = [x for x in _REAP_THREADS if x.is_alive()]


atexit.register(join_all_reaps)


def _post_signal(pid, sig_name: str) -> None:
    """POSIX: send `sig_name` to pid's process GROUP (ptyprocess setsid()s the
    child, so pgid == pid and the group covers claude's node workers — the
    `taskkill /T` analog), falling back to the single process. The signal is
    looked up by NAME so this module — and the headless tests that exercise the
    POSIX kill path — stay importable on Windows, where signal.SIGHUP doesn't
    exist. Never raises; no-op for a missing signal or pid."""
    sig = getattr(signal, sig_name, None)
    if not pid or sig is None:
        return
    try:
        os.killpg(pid, sig)     # AttributeError on Windows lands in the except
        return
    except Exception:
        pass
    try:
        os.kill(pid, sig)
    except Exception:
        pass

# ── Soft imports ─────────────────────────────────────────────────────────────
# The widget is only constructed when these are present (saikai probes
# TERMINAL_AVAILABLE before offering split-live). Importing this module never
# raises just because a dep is missing — that keeps the preview fallback intact
# and lets py_compile / unit tests run without textual/pyte/pywinpty.
try:
    import pyte  # type: ignore
    # pyte (via the wcwidth module) counts each Regional-Indicator symbol
    # (U+1F1E6–U+1F1FF) as width 2, so a flag emoji like 🇯🇵 occupies FOUR cells in
    # pyte's grid. But Rich/Textual AND Windows Terminal render a flag pair as
    # width 2 (verified: rich.cell_len('🇯🇵')==2). That 4-vs-2 disagreement shifts
    # every line carrying a flag (e.g. claude's "🇯🇵 JA" status line) two columns
    # and cascades into stale-cell garble in the rows below. Reconcile pyte to the
    # render target: treat each RI as width 1, so a pair renders as 2 like Rich/WT.
    # (#flag-width — confirmed via the Ctrl+F12 pane dump: pyte stored 🇯🇵 as 4 cells)
    try:
        _pyte_wcwidth_orig = pyte.screens.wcwidth

        def _wcwidth_flag_aware(char, _orig=_pyte_wcwidth_orig):
            if char and 0x1F1E6 <= ord(char[0]) <= 0x1F1FF:
                return 1
            return _orig(char)

        pyte.screens.wcwidth = _wcwidth_flag_aware
    except Exception:
        pass

    # pyte's Screen.draw merges only TRUE combining marks (unicodedata.combining
    # != 0) into the previous cell and `break`s on any OTHER width-0 char — which
    # ABORTS the whole draw() call, silently dropping every remaining character in
    # that chunk. ZWJ (U+200D, category Cf, combining==0) and emoji variation
    # selectors (U+FE0F) are width-0 non-combining, so a single ZWJ-emoji family
    # (👨‍👩‍👧‍👦) or a VS16 emoji (❤️) in claude's output truncated the pane from that
    # point on. Subclass to merge ANY width-0 char into the previous cell (keeping
    # the grapheme's codepoints contiguous so the outer terminal can render it) and
    # to SKIP (not break on) width<0 chars. Faithful copy of pyte's draw otherwise;
    # guarded so a pyte-internal change just falls back to the stock screen. (#audit-zwj)
    _HistoryScreenBase = pyte.HistoryScreen
    try:
        import unicodedata as _ud
        from pyte import modes as _mo

        class _SaikaiHistoryScreen(pyte.HistoryScreen):  # type: ignore[misc]
            def draw(self, data: str) -> None:
                data = data.translate(
                    self.g1_charset if self.charset else self.g0_charset)
                _ww = pyte.screens.wcwidth   # the flag-aware wrapper installed above
                for char in data:
                    char_width = _ww(char)
                    if self.cursor.x == self.columns:
                        if _mo.DECAWM in self.mode:
                            self.dirty.add(self.cursor.y)
                            self.carriage_return()
                            self.linefeed()
                        elif char_width > 0:
                            self.cursor.x -= char_width
                    if _mo.IRM in self.mode and char_width > 0:
                        self.insert_characters(char_width)
                    line = self.buffer[self.cursor.y]
                    if char_width == 1:
                        line[self.cursor.x] = self.cursor.attrs._replace(data=char)
                    elif char_width == 2:
                        line[self.cursor.x] = self.cursor.attrs._replace(data=char)
                        if self.cursor.x + 1 < self.columns:
                            line[self.cursor.x + 1] = self.cursor.attrs._replace(data="")
                    elif char_width == 0:
                        # Merge ANY zero-width char (combining mark, ZWJ, VS16, other
                        # Cf) into the preceding cell instead of pyte's break. NFC-fold
                        # only real combining marks, to match pyte's prior behaviour.
                        if self.cursor.x:
                            last = line[self.cursor.x - 1]
                            merged = last.data + char
                            if _ud.combining(char):
                                merged = _ud.normalize("NFC", merged)
                            line[self.cursor.x - 1] = last._replace(data=merged)
                        elif self.cursor.y:
                            prev = self.buffer[self.cursor.y - 1][self.columns - 1]
                            merged = prev.data + char
                            if _ud.combining(char):
                                merged = _ud.normalize("NFC", merged)
                            self.buffer[self.cursor.y - 1][self.columns - 1] = \
                                prev._replace(data=merged)
                        # else: leading zero-width char, nothing on-screen to attach to.
                    else:
                        continue   # width < 0: unprintable; skip it, DON'T abort the chunk
                    if char_width > 0:
                        self.cursor.x = min(self.cursor.x + char_width, self.columns)
                self.dirty.add(self.cursor.y)

        _HistoryScreenBase = _SaikaiHistoryScreen
    except Exception:
        pass
except Exception:  # pragma: no cover - exercised only when dep absent
    pyte = None  # type: ignore
    _HistoryScreenBase = None  # type: ignore

_PTY_IMPORT_ERROR: Optional[str] = None
PtyProcess = None  # type: ignore
try:
    if sys.platform == "win32":
        from winpty import PtyProcess as _WinPty  # type: ignore
        PtyProcess = _WinPty  # type: ignore
    else:  # pragma: no cover - POSIX path not exercised on the Windows host
        from ptyprocess import PtyProcessUnicode as _PosixPty  # type: ignore
        PtyProcess = _PosixPty  # type: ignore
except Exception as _e:  # pragma: no cover
    _PTY_IMPORT_ERROR = repr(_e)

_TEXTUAL_IMPORT_ERROR: Optional[str] = None
try:
    from rich.segment import Segment
    from rich.style import Style
    from textual import events
    from textual.strip import Strip
    from textual.widget import Widget
    from textual.geometry import Offset
except Exception as _te:  # pragma: no cover - textual is a hard dep of saikai
    _TEXTUAL_IMPORT_ERROR = repr(_te)
    # Stand-ins so the module still imports for py_compile / pure-function tests
    # on a box without textual.
    Widget = object  # type: ignore
    Segment = Style = Strip = events = Offset = None  # type: ignore

#: True when every dependency needed for a live pane is importable.
TERMINAL_AVAILABLE = (
    pyte is not None
    and PtyProcess is not None
    and _TEXTUAL_IMPORT_ERROR is None
)


def unavailable_reason() -> Optional[str]:
    """Human-readable reason the live terminal can't run, or None if it can.
    saikai surfaces this in a toast so the user knows why it fell back to the
    static preview."""
    if pyte is None:
        return "pyte not installed (add 'pyte>=0.8' to the script deps)"
    if PtyProcess is None:
        plat = "pywinpty>=2.0" if sys.platform == "win32" else "ptyprocess>=0.7"
        return f"PTY backend unavailable ({_PTY_IMPORT_ERROR or plat})"
    if _TEXTUAL_IMPORT_ERROR is not None:
        return f"textual import failed ({_TEXTUAL_IMPORT_ERROR})"
    return None


# ── ANSI / status detection ───────────────────────────────────────────────────
# Local copy so this module stands alone. Matches CSI (SGR/cursor/private mode)
# and OSC; used only to strip noise before the status regexes run.
_ANSI_RE = re.compile(
    r"\x1b\[[0-9;?]*[ -/]*[@-~]"        # CSI
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"  # OSC … BEL / ST
    r"|\x1b[()][AB0-2]"                 # charset designators
)

# BUSY — claude is actively working a turn. "esc to interrupt" is the single
# most reliable marker (claude prints it in the working footer only while
# streaming); the rest corroborate.
_BUSY_RE = re.compile(
    r"esc to interrupt"
    r"|\besc\b[^\n]*\binterrupt\b"
    r"|Thinking[.…]"
    r"|Working[.…]",
    re.IGNORECASE,
)
# Braille + classic spinner frames ink/claude cycle while busy.
_SPINNER_CHARS = (
    "⠇⠋⠙⠹⠸⠼⠴⠦⠧⠏"
    "⠁⠂⠄⡀▖▗▘▙▚▛"
    "▜▝▞▟"
)
_SPINNER_RE = re.compile("[" + re.escape(_SPINNER_CHARS) + "]")

# WAITING — claude is blocked on the human (permission prompt / forced choice).
_WAITING_RE = re.compile(
    r"Do you want"
    r"|Would you like"
    r"|\(y/n\)|\[y/N\]|\[Y/n\]"
    r"|Press\s+(?:enter|return)\s+to"
    r"|press\s+esc\s+to\s+(?:cancel|skip)"
    r"|❯\s*\d",                       # ❯ pointing at a numbered choice
    re.IGNORECASE,
)
# A multi-line numbered menu (>=2 "N. text" lines) is also a forced choice.
_MENU_RE = re.compile(r"(?:^\s*\d+\.\s+\S.*$\n?){2,}", re.MULTILINE)

# The startup "trust this folder?" gate. It blocks the session on the human
# ("❯ 1. Yes, I trust this folder / 2. No, exit"), but it renders at the TOP of
# the screen with the rest blank — so it falls OUTSIDE the tail window the prompt
# checks use, AND its footer ("Enter to confirm · Esc to cancel") lacks the
# "Press"/"press" the _WAITING_RE patterns want. Detect it across the whole
# screen by its characteristic wording instead.
_TRUST_RE = re.compile(r"trust (?:this folder|the files in this folder)", re.IGNORECASE)


def classify_pty_status(screen_text: str, title: str = "") -> str:
    """Classify into ``"busy"`` / ``"waiting"`` / ``"idle"``.

    The most reliable, real-time signal is claude's OWN OSC-0 title (the same
    thing WezTerm surfaces): a leading braille-spinner glyph (U+2800–U+28FF)
    means it's working; "✳" means ready/idle. We use the title for busy/idle and
    the on-screen text for a permission/forced-choice prompt (waiting).

    The title spinner is checked FIRST and WINS: a numbered list or "Would you
    like…" that claude is STREAMING is not a settled prompt, so the screen-scrape
    must not flip an actively-working pane to "waiting" (the false "needs input"
    bug — it fired on essentially every multi-step session). Only when NOT
    generating does a visible permission/forced-choice prompt mean "waiting".
    Priority: Busy (title spinner) > Waiting (visible prompt) > Busy (body
    markers) > Idle. `screen_text` should be the CURRENT screen (pyte .display).
    """
    # claude's title spinner = actively working: the definitive real-time signal
    # (reliable, survives scrollback). Check it FIRST — and skip the screen
    # ANSI-strip entirely on the common busy tick (the .display can be huge).
    g = (title or "")[:1]
    if g and 0x2800 <= ord(g) <= 0x28FF:
        return "busy"
    # Startup "trust this folder?" gate: a hard human block rendered at the TOP of
    # the screen (rest blank), so it sits OUTSIDE the tail window below. It's a
    # cheap substring scan over the full screen and only runs when NOT busy.
    if _TRUST_RE.search(screen_text or ""):
        return "waiting"
    # Slice to the tail BEFORE the ANSI-strip (pyte's .display is escape-free and
    # we only need the last ~2000 chars). Not generating → a visible permission /
    # forced-choice prompt is the strongest "needs you".
    t = _ANSI_RE.sub("", (screen_text or "")[-2000:])
    if _WAITING_RE.search(t) or _MENU_RE.search(t):
        return "waiting"
    # Corroborating body markers in case the title was missed this tick.
    _lines = t.splitlines()
    last_line = _lines[-1] if _lines else ""
    if _BUSY_RE.search(t) or _SPINNER_RE.search(last_line):
        return "busy"
    return "idle"


def classify_generic_status(screen_text: str, title: str = "") -> str:
    """Conservative status classifier for agents without a trusted OSC title."""
    t = _ANSI_RE.sub("", (screen_text or "")[-2000:])
    if _WAITING_RE.search(t) or _MENU_RE.search(t):
        return "waiting"
    lines = t.splitlines()
    last_line = lines[-1] if lines else ""
    if _BUSY_RE.search(t) or _SPINNER_RE.search(last_line):
        return "busy"
    return "idle"


def classifier_for_profile(profile: str) -> Callable[[str, str], str]:
    """Resolve a provider's declared status profile to a terminal classifier."""
    profiles = {"claude": classify_pty_status, "generic": classify_generic_status}
    try:
        return profiles[profile]
    except KeyError as exc:
        raise ValueError(f"unknown status classifier profile: {profile!r}") from exc


# ── pyte cell → rich.Style ───────────────────────────────────────────────────
_HEX6 = re.compile(r"\A[0-9a-fA-F]{6}\Z")


# pyte uses a few color NAMES that rich does not accept verbatim: ANSI-3 is
# "brown" (rich wants "yellow") and the bright set is "bright<name>" (rich wants
# "bright_<name>"). Map those; anything still unparseable degrades to the
# default color instead of crashing the whole UI (the original 'brown' crash).
_PYTE_TO_RICH = {
    "brown": "yellow", "brightbrown": "bright_yellow",
    "brightblack": "bright_black", "brightred": "bright_red",
    "brightgreen": "bright_green", "brightblue": "bright_blue",
    "brightmagenta": "bright_magenta", "brightcyan": "bright_cyan",
    "brightwhite": "bright_white",
}
_COLOR_CACHE: dict = {}


def _pyte_color(color: Optional[str]) -> Optional[str]:
    """Map a pyte color (name like 'red'/'brown', 6-hex without '#', or
    'default') to a value rich.Style accepts, or None for the terminal default.
    Validated against rich once per name and cached; an unknown/unparseable
    color degrades to default rather than raising — a single bad color must
    never tear down the pane."""
    if not color or color == "default":
        return None
    if color in _COLOR_CACHE:
        return _COLOR_CACHE[color]
    if _HEX6.match(color):
        val: Optional[str] = "#" + color
    else:
        name = _PYTE_TO_RICH.get(color, color)
        try:
            from rich.color import Color as _RichColor
            _RichColor.parse(name)
            val = name
        except Exception:
            val = None
    _COLOR_CACHE[color] = val
    return val


def _cell_style(ch):  # -> rich.Style; only reached from render_line (textual present)
    """Map a pyte Char's attributes (fg/bg + bold/italic/underline/reverse/…)
    to a rich.Style for a single cell."""
    return Style(
        color=_pyte_color(getattr(ch, "fg", None)),
        bgcolor=_pyte_color(getattr(ch, "bg", None)),
        bold=bool(getattr(ch, "bold", False)),
        italic=bool(getattr(ch, "italics", False)),
        underline=bool(getattr(ch, "underscore", False)),
        strike=bool(getattr(ch, "strikethrough", False)),
        reverse=bool(getattr(ch, "reverse", False)),
        blink=bool(getattr(ch, "blink", False)),
    )


# ── Key encoding ──────────────────────────────────────────────────────────────
# event.key -> exact bytes/escape the PTY child expects. We start from the
# textual-terminal reference table, then add the control bytes it leaves to
# event.character — deterministic is safer for a TUI child like claude:
# event.key == "ctrl+c" is guaranteed by Textual; the derived character is not
# portable across terminals.
_KEYMAP: dict[str, str] = {
    "up": "\x1b[A", "down": "\x1b[B", "right": "\x1b[C", "left": "\x1b[D",
    "home": "\x1b[H", "end": "\x1b[F",
    "pageup": "\x1b[5~", "pagedown": "\x1b[6~",
    "delete": "\x1b[3~", "insert": "\x1b[2~",
    "enter": "\r", "tab": "\t", "shift+tab": "\x1b[Z",
    "backspace": "\x7f", "escape": "\x1b",
    "f1": "\x1bOP", "f2": "\x1bOQ", "f3": "\x1bOR", "f4": "\x1bOS",
    "f5": "\x1b[15~", "f6": "\x1b[17~", "f7": "\x1b[18~", "f8": "\x1b[19~",
    "f9": "\x1b[20~", "f10": "\x1b[21~", "f11": "\x1b[23~", "f12": "\x1b[24~",
}
# ctrl+a .. ctrl+z -> 0x01 .. 0x1a  (ctrl+c == 0x03, ctrl+d == 0x04, …)
for _i, _ch in enumerate("abcdefghijklmnopqrstuvwxyz", 1):
    _KEYMAP[f"ctrl+{_ch}"] = chr(_i)
# A few extra control combos readline / claude use.
_KEYMAP.update({
    # Textual names the '@' key "at" (KEY_NAME_REPLACEMENTS), so the event.key for
    # Ctrl+@ (natural NUL on a JIS layout) is "ctrl+at" — the literal "ctrl+@" here
    # never matched and the key was silently swallowed. Keep both forms.
    "ctrl+at": "\x00", "ctrl+@": "\x00", "ctrl+space": "\x00",
    "ctrl+backslash": "\x1c", "ctrl+right_square_bracket": "\x1d",
    "ctrl+circumflex_accent": "\x1e", "ctrl+underscore": "\x1f",
})
_BASE_KEYMAP = dict(_KEYMAP)
_MODIFIED_CSI_FINALS = {
    "up": "A", "down": "B", "right": "C", "left": "D",
    "home": "H", "end": "F",
}
_MODIFIED_TILDE_KEYS = {
    "insert": "2", "delete": "3", "pageup": "5", "pagedown": "6",
}


def _normalize_key(spec: str) -> str:
    """Map a human key spec (e.g. 'ctrl+]') to Textual's key name
    ('ctrl+right_square_bracket') so SAIKAI_RELEASE_KEY accepts either form."""
    s = (spec or "").strip().lower()
    repl = {"]": "right_square_bracket", "[": "left_square_bracket",
            "\\": "backslash", "_": "underscore", "^": "circumflex_accent",
            "@": "at"}   # Textual names Ctrl+@ as 'ctrl+at' (JIS layout NUL)
    if "+" in s:
        head, _, tail = s.rpartition("+")
        return f"{head}+{repl.get(tail, tail)}"
    return repl.get(s, s)

#: The key that releases focus back to the session list (the escape hatch). A
#: focused terminal swallows every key, so without this the user is trapped. Esc
#: goes to claude (interrupt) and the readline editing keys (Ctrl+A/B/E/W/K/…) are
#: forwarded, so the default is Ctrl+] — a control char ConPTY delivers reliably,
#: rarely needed in claude (readline char-search). Override with SAIKAI_RELEASE_KEY
#: (human form like 'ctrl+]' or a Textual name). Popped from _KEYMAP so it is
#: never forwarded to the child. NOTE: Textual names ']' as right_square_bracket,
#: so the literal 'ctrl+]' string would never match — _normalize_key fixes that.
RELEASE_FOCUS_KEY = ""


def configure_release_focus_key(spec: str) -> str:
    """Apply the configured pane-release key and keep it out of PTY forwarding."""
    global RELEASE_FOCUS_KEY
    old = RELEASE_FOCUS_KEY
    if old in _BASE_KEYMAP and old not in ("f2", "f3", "f4"):
        _KEYMAP[old] = _BASE_KEYMAP[old]
    RELEASE_FOCUS_KEY = _normalize_key(spec or "ctrl+]")
    _KEYMAP.pop(RELEASE_FOCUS_KEY, None)
    return RELEASE_FOCUS_KEY


configure_release_focus_key(os.environ.get("SAIKAI_RELEASE_KEY") or "ctrl+]")
# F2/F3 are reserved by saikai for prev/next tab (priority bindings); never
# forward them to the child, so tab-switching works even while a pane is focused.
for _rk in ("f2", "f3", "f4"):
    _KEYMAP.pop(_rk, None)


def encode_key(key: str, character: Optional[str]) -> Optional[str]:
    """Translate a Textual key event into the byte string to write to the PTY,
    or None if the key carries nothing the child should receive.

    Pure + table-driven so it is unit-testable without a TTY.
    """
    mapped = _KEYMAP.get(key)
    if mapped is not None:
        return mapped
    parts = key.split("+")
    base, modifiers = parts[-1], set(parts[:-1])
    if modifiers and modifiers <= {"shift", "alt", "ctrl"}:
        # Textual normalizes host-terminal input; emit the standard xterm
        # modifier form expected by interactive children, independent of the
        # outer terminal emulator. Modifier parameter: 1 + Shift + 2*Alt + 4*Ctrl.
        mod = 1 + ("shift" in modifiers) + 2 * ("alt" in modifiers) + 4 * ("ctrl" in modifiers)
        if base in _MODIFIED_CSI_FINALS:
            return f"\x1b[1;{mod}{_MODIFIED_CSI_FINALS[base]}"
        if base in _MODIFIED_TILDE_KEYS:
            return f"\x1b[{_MODIFIED_TILDE_KEYS[base]};{mod}~"
        if base in ("enter", "return"):
            # Modified Enter (shift/alt/ctrl+enter) — the "newline in the prompt
            # without submitting" gesture. The legacy encoding can't represent it,
            # so it was returning None and being SILENTLY swallowed. Emit the CSI-u
            # (kitty keyboard) form claude negotiates; 13 = Enter's codepoint. A
            # terminal only delivers a DISTINCT modified-enter under a modern
            # protocol, so the child is kitty-aware here.
            return f"\x1b[13;{mod}u"
    # Meta / Alt = ESC prefix — readline word ops (alt+b/f/d backward/forward/
    # kill-word, alt+. , alt+backspace = backward-kill-word) must reach claude too.
    if key.startswith("alt+"):
        rest = key[4:]
        if rest == "backspace":
            return "\x1b\x7f"
        if len(rest) == 1:
            return "\x1b" + rest
        return None   # alt+<named> (arrows etc.) aren't readline word ops
    # Printable single char (letters, digits, punctuation, space, IME unicode).
    if character and character.isprintable():
        return character
    return None


# ── alt-screen tracking (pyte gap, see pyte spike) ────────────────────────────
# pyte records the ?1049h/?1049l mode bit but has only ONE buffer: it does NOT
# swap/save/restore, and ignores ?47/?1047 entirely. NOTE: current claude
# renders to the NORMAL buffer (probe 2026-06: no ?1049h alt-screen, no mouse
# reporting) — which is why the HistoryScreen scrollback above works. The
# alt-reset below is now a dormant SAFETY NET: if some tool DOES swap buffers,
# resetting pyte's single buffer at the boundary keeps a pre-alt prompt from
# bleeding under its UI and stops the last frame lingering after it exits.
_ALT_ENTER_RE = re.compile(r"\x1b\[\?(?:1049|1047|47)h")
_ALT_LEAVE_RE = re.compile(r"\x1b\[\?(?:1049|1047|47)l")
_ALT_ANY_RE = re.compile(r"\x1b\[\?(?:1049|1047|47)[hl]")
# Private-intro CSI sequences that END in 'm' but are NOT SGR: XTMODKEYS
# (\x1b[>4;2m = modifyOtherKeys) and friends. pyte ignores the >/</= private
# marker and misapplies the params as SGR — '>4;2m' becomes underline(4)+faint(2),
# and since claude never sends a matching reset, EVERY following cell renders
# underlined. Strip them before feeding pyte (keyboard-protocol negotiation,
# irrelevant to the display grid).
_PRIVATE_SGR_RE = re.compile(r"\x1b\[[<>=][0-9;:]*m")
# Kitty keyboard protocol push/pop/set/query (CSI >/</=/? … u). pyte doesn't
# model it and LEAKS the trailing 'u' into the grid — so a kanji being edited
# appears to gain a stray 'u' (the leaked byte lands at the cursor). claude emits
# these to negotiate key reporting, but saikai encodes keys in the legacy format
# regardless, so dropping the negotiation is display-only and harmless. (Plain
# CSI u = SCO restore-cursor has no private marker, so it is NOT stripped.)
_KITTY_KBD_RE = re.compile(r"\x1b\[[<>=?][0-9;:]*u")
# Bracketed-paste mode (CSI ?2004 h/l): claude enables it so it can distinguish a
# PASTE from typed input. pyte doesn't expose the mode, so we track it from the
# output stream and re-wrap pastes (\x1b[200~ … \x1b[201~) in on_paste — otherwise
# claude treats a multi-line paste as typed lines and submits on each newline.
_BRACKETED_RE = re.compile(r"\x1b\[\?2004([hl])")
# Mouse reporting (?1000 click / ?1002 button-drag / ?1003 any-motion) + the SGR
# extended-coordinate encoding (?1006). A full-screen child TUI (e.g. an agent
# picker) enables these to receive mouse events ITSELF — including the WHEEL, which
# it uses to scroll its OWN view. saikai tracks the mode so on_mouse_scroll can
# FORWARD the wheel to the child instead of consuming it for saikai's own scrollback
# (which is empty in the alt-screen such a TUI runs in → the wheel "did nothing").
_MOUSE_REPORT_RE = re.compile(r"\x1b\[\?(?:1000|1002|1003)([hl])")
_MOUSE_SGR_RE = re.compile(r"\x1b\[\?1006([hl])")
# Synchronized output (DEC mode 2026, BSU/ESU): a TUI brackets a full frame's writes
# in ?2026h … ?2026l so the terminal presents the COMPLETE frame, not the half-drawn
# intermediate. saikai feeds pyte continuously but DEFERS the pane repaint until the
# block closes (or a safety timeout), so an agent UI's redraw doesn't tear ("layout
# looks broken"). pyte ignores ?2026, so it's tracked here purely for repaint timing.
_SYNC_RE = re.compile(r"\x1b\[\?2026([hl])")
# Embedded paste markers in text we are about to wrap in bracketed paste: an
# embedded ESC[201~ would END paste mode early so the bytes after it run as
# typed-and-submitted input (the classic bracketed-paste breakout). Strip both
# markers from the content first, exactly as real terminals sanitize a paste.
_PASTE_MARKER_RE = re.compile(r"\x1b\[20[01]~")


def _normalize_paste_newlines(text: str) -> str:
    """Collapse CRLF to LF in pasted text. A Windows clipboard (Notepad, a CRLF
    file, browser text) delivers '\\r\\n' per line; forwarded verbatim into the
    PTY the child's readline sees CR *and* LF for every line and submits/blanks
    twice ('double-enter'). Real terminals strip the CR before delivering a paste.
    Lone '\\r' is left alone (rare, and may be intentional)."""
    return text.replace("\r\n", "\n")


def _wrap_bracketed_paste(text: str) -> str:
    """Wrap text in bracketed-paste markers after stripping any embedded ones."""
    return "\x1b[200~" + _PASTE_MARKER_RE.sub("", text) + "\x1b[201~"


def _scroll_row_index(hist_len: int, scroll: int, y: int) -> int:
    """Absolute index into (history.top + live buffer) for visible row y at a
    given scroll offset (0 = live bottom). idx < hist_len -> a history line;
    idx >= hist_len -> live buffer row (idx - hist_len)."""
    return hist_len - scroll + y


def _pyte_grid_lines(screen) -> list:
    """Visible grid as list[str], one string per row — a robust stand-in for
    pyte's ``Screen.display``.

    ``Screen.display`` carries ``assert sum(map(wcwidth, char[1:])) == 0``, which
    raises ``AssertionError`` on any cell whose combining TAIL has a non-zero
    width — reachable from real terminal output (malformed/edge sequences claude's
    TUI can emit). Our snapshot_text / _current_screen callers wrapped display in
    ``except Exception`` and so silently produced an EMPTY grid (the reported blank
    pane dump, and a blanked status classifier) with no clue why. Walk the buffer
    the way render_line does instead — skip the empty-string wide-char STUB pyte
    stores at x+1, never call wcwidth — so this can't assert. Call under the pane
    lock (buffer access). (#pane-dump)"""
    rows = getattr(screen, "lines", 0) or 0
    cols = getattr(screen, "columns", 0) or 0
    buf = screen.buffer
    out = []
    for y in range(rows):
        row = buf[y]
        out.append("".join(row[x].data for x in range(cols) if row[x].data != ""))
    return out


def set_clipboard_windows(text: str) -> bool:
    """Put `text` on the Windows clipboard as CF_UNICODETEXT via Win32 directly.

    Codepage-INDEPENDENT, which is the whole point: piping to `clip.exe` makes it
    decode stdin using the console's code page, so multibyte text (CJK / emoji)
    garbles whenever the launch codepage differs from what we encoded for — e.g.
    UTF-16LE bytes read back as UTF-8 turned 裏がとれております into 'ψL0h0…'.
    Setting the UTF-16 clipboard format the OS actually stores makes it
    round-trip no matter how saikai was started. Returns False on any failure so
    the caller can fall back to clip / OSC-52. Windows-only (guard before call)."""
    import ctypes
    from ctypes import wintypes
    CF_UNICODETEXT = 13
    GMEM_MOVEABLE = 0x0002
    u32 = ctypes.windll.user32
    k32 = ctypes.windll.kernel32
    k32.GlobalAlloc.restype = wintypes.HGLOBAL
    k32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    k32.GlobalLock.restype = wintypes.LPVOID
    k32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    k32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    k32.GlobalFree.argtypes = [wintypes.HGLOBAL]
    u32.SetClipboardData.restype = wintypes.HANDLE
    u32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
    buf = text.encode("utf-16-le") + b"\x00\x00"      # NUL-terminated wide string
    if not u32.OpenClipboard(None):
        return False
    h = None
    try:
        u32.EmptyClipboard()
        h = k32.GlobalAlloc(GMEM_MOVEABLE, len(buf))
        if not h:
            return False
        ptr = k32.GlobalLock(h)
        if not ptr:
            return False
        ctypes.memmove(ptr, buf, len(buf))
        k32.GlobalUnlock(h)
        if not u32.SetClipboardData(CF_UNICODETEXT, h):
            return False
        h = None        # ownership transferred to the OS — must NOT free it
        return True
    except Exception:
        return False
    finally:
        if h:
            k32.GlobalFree(h)   # SetClipboardData never took ownership → free our block
        u32.CloseClipboard()


def set_clipboard_macos(text: str) -> bool:
    """Use the local macOS clipboard, but leave remote sessions to OSC-52."""
    if os.environ.get("SSH_CONNECTION") or os.environ.get("SSH_TTY"):
        return False
    try:
        subprocess.run(["pbcopy"], input=text.encode("utf-8"), check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except Exception:
        return False


class AltScreenTracker:
    """Track alt-screen enter/leave transitions in a raw VT byte stream."""

    def __init__(self) -> None:
        self.in_alt = False

    def transitions(self, text: str) -> int:
        """Feed a chunk; return how many enter/leave boundaries it contained
        (so the caller resets pyte once per boundary). Updates ``in_alt``."""
        count = 0
        for m in re.finditer(r"\x1b\[\?(?:1049|1047|47)[hl]", text):
            entering = m.group().endswith("h")
            if entering != self.in_alt:
                self.in_alt = entering
                count += 1
        return count


# ══════════════════════════════════════════════════════════════════════════════
# The widget
# ══════════════════════════════════════════════════════════════════════════════
class AgentTerminal(Widget):  # type: ignore[misc]  # Widget is object w/o textual
    """A live PTY terminal rendered from a pyte screen buffer via the Line API.

    One instance owns exactly one child process (an interactive agent CLI,
    or any argv). It spawns on mount, reads in a background thread, feeds the
    bytes to pyte, and marshals a repaint onto the UI thread. Keys are encoded
    to PTY bytes in ``on_key``; resize is propagated to both pyte and the PTY.
    On unmount / app exit it kills the whole child tree.

    Reactivity is kept simple on purpose: a full ``refresh()`` per read chunk
    (Textual then calls ``render_line`` per visible row). That is plenty for a
    chat-style child; dirty-line optimisation can come later.
    """

    can_focus = True
    DEFAULT_CSS = "AgentTerminal { width: 1fr; height: 1fr; }"

    def __init__(
        self,
        argv: list[str],
        cwd: Optional[str] = None,
        env: Optional[dict] = None,
        *,
        sid: Optional[str] = None,
        title: str = "agent",
        on_status: Optional[Callable[[str, str], None]] = None,
        on_exit: Optional[Callable[[str], None]] = None,
        status_classifier: Optional[Callable[[str, str], str]] = None,
        **kw,
    ) -> None:
        """
        argv      : list — ALWAYS a list (string argv is over-quoted by the
                    ConPTY shell layer; see pywinpty spike gotcha #3).
        cwd, env  : child working dir / environment (saikai builds these via
                    its shared _build_resume_invocation helper).
        sid       : the saikai session id this pane is attached to (or None for
                    a brand-new session). Passed back to on_status/on_exit.
        title     : tab label seed.
        on_status : called (sid, status) when Busy/Waiting/Idle changes, so
                    saikai can mirror it onto the DataTable marker + tab label.
        on_exit   : called (sid) when the child exits, so saikai can re-title
                    the tab and stop polling.
        """
        super().__init__(**kw)
        self._argv = list(argv)
        self._cwd = cwd
        self._env = env
        self.sid = sid
        self.title = title
        self._on_status = on_status
        self._on_exit = on_exit
        self._status_classifier = status_classifier or classify_pty_status

        self._pty = None
        self._pid: Optional[int] = None
        self._screen = None          # pyte.Screen
        self._stream = None          # pyte.Stream (feeds str)
        self._alt = AltScreenTracker()
        self._blink_on = True        # cursor-blink flag (see _blink_tick); stays
                                     # True when the timer never runs (non-Windows)
        self._blink_timer = None     # 0.5s blink keepalive — keeps WT's IME alive
        self._scroll = 0             # lines scrolled back (0 = live bottom)
        self._frozen = False         # paused repaint: hold the view still so a
                                     # streaming pane can be drag-selected
        self._sel_anchor = None      # (row,col) drag start — saikai-OWNED selection
        self._sel_head = None        # (row,col) drag head; None ⇒ no selection
        self._pending_anchor = None  # (row,col) of a press awaiting a drag; a click that never drags stays pending → no freeze/capture (#click-no-freeze)
        self._autoscroll_dir = 0     # drag at top/bottom edge: +1 up / -1 down / 0
        self._autoscroll_timer = None  # ticks while edge-dragging (#drag-autoscroll)
        self._sel_prev_frozen = False
        self._frozen_buf = None      # snapshot of the displayed rows while frozen
                                     # (the reader keeps mutating screen.buffer, so
                                     # render + copy must read the FROZEN frame)
        self._esc_carry = ""         # trailing partial escape held across read()s
        self._reader: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._lock = threading.Lock()   # guards pyte feed vs render_line read
        # _scr_ver bumps on every pyte mutation (feed/reset) under _lock so
        # _current_screen can skip re-joining an unchanged screen, and the host
        # poll can skip re-classifying a stable (non-busy) pane with no new output.
        self._scr_ver = 0
        self._cached_ver = -1
        self._cached_screen: tuple = ("", "")
        self._last_poll_ver = -1

        # status detection
        self._tail = ""                  # rolling decoded tail for classify
        self._status = "idle"
        self._pending_status: Optional[str] = None
        self._pending_ticks = 0
        self.is_dead = False
        self._spawn_error: Optional[str] = None

    # ── geometry helpers ──────────────────────────────────────────────────────
    def _dims(self) -> tuple[int, int]:
        """Current (rows, cols), floored at a sane minimum so pyte / ConPTY
        never get a zero dimension during early layout."""
        cols = max(int(self.size.width or 0), 2)
        rows = max(int(self.size.height or 0), 2)
        return rows, cols

    # ── lifecycle ─────────────────────────────────────────────────────────────
    def on_mount(self) -> None:
        rows, cols = self._dims()
        try:
            # HistoryScreen keeps scrolled-off lines in .history.top so the pane
            # can scroll back (claude renders to the NORMAL buffer — verified by
            # probe: no ?1049h alt-screen — so terminal-side scrollback applies).
            self._screen = _HistoryScreenBase(cols, rows, history=SCROLLBACK_LINES)  # (cols, rows)!
            self._stream = pyte.Stream(self._screen)        # feed str; pywinpty already decodes
        except Exception as e:  # pragma: no cover
            self._fail(f"pyte init failed: {e!r}")
            return
        try:
            self._spawn(rows, cols)
        except Exception as e:
            self._fail(f"spawn failed: {e!r}")
            return
        self._reader = threading.Thread(
            target=self._read_loop, name=f"pty-read-{self.sid or 'new'}",
            daemon=True,
        )
        self._reader.start()
        # Cursor-blink keepalive (Windows only — the WT IME fix, see _blink_tick).
        # Created paused; on_focus resumes it, on_blur pauses it. Mirrors how
        # Textual's own Input keeps its cursor (and the IME) alive. (#wt-ime-blink)
        if _IS_WIN:
            try:
                self._blink_timer = self.set_interval(
                    0.5, self._blink_tick, pause=True)
            except Exception:
                self._blink_timer = None

    def _spawn(self, rows: int, cols: int) -> None:
        kwargs: dict = {"dimensions": (rows, cols)}
        if self._cwd:
            kwargs["cwd"] = self._cwd
        # The child renders into saikai's pyte grid (full 24-bit SGR), NOT the host
        # terminal — so advertise a truecolor xterm to it regardless of the host's
        # own TERM. Without this a host with TERM unset (legacy conhost) or without
        # truecolor (Apple Terminal) made the child under-/over-estimate colour
        # support, so pane colours varied by host OS/shell rather than being
        # deterministic. pyte stores whatever the child emits; Rich/Textual then
        # downsamples to the OUTER terminal as needed. (#audit-term)
        base_env = self._env if self._env is not None else os.environ
        env = dict(base_env)
        env["TERM"] = "xterm-256color"
        env["COLORTERM"] = "truecolor"
        kwargs["env"] = env
        # argv MUST be a list (pywinpty spike gotcha #3).
        self._pty = PtyProcess.spawn(self._argv, **kwargs)
        # POSIX ptyprocess.PtyProcessUnicode decodes with codec_errors='strict'
        # by default, so a single invalid UTF-8 byte from the child (a binary blob
        # cat'd into the pane, a legacy-encoded log) raises UnicodeDecodeError out
        # of read() and kills the reader thread — the pane freezes instead of just
        # showing a replacement char. Swap in a lenient decoder right after spawn
        # (nothing has been read yet, so no buffered state is lost). winpty returns
        # str already and has no decoder attr, so this is POSIX-only. (#audit-pty-decode)
        if not _IS_WIN and self._pty is not None:
            try:
                import codecs
                enc = getattr(self._pty, "encoding", None) or "utf-8"
                self._pty.codec_errors = "replace"
                self._pty.decoder = codecs.getincrementaldecoder(enc)(errors="replace")
            except Exception:
                pass
        self._pid = getattr(self._pty, "pid", None)
        _log(f"spawn: sid={(getattr(self, 'sid', None) or '?')[:8]} pid={self._pid}")

    def _fail(self, msg: str) -> None:
        _log(f"spawn FAIL: sid={(getattr(self, 'sid', None) or '?')[:8]} — {msg}")
        self._spawn_error = msg
        self.is_dead = True
        try:
            self.refresh()
        except Exception:
            pass
        if self._on_exit and self.sid:
            try:
                self._on_exit(self.sid)
            except Exception:
                pass

    # ── (1) render a grid of styled cells, one Strip per row ───────────────────
    def render_line(self, y: int):  # -> Strip
        width = self.size.width
        screen = self._screen
        if self._spawn_error is not None:
            # Graceful failure surface: show the error on row 0, blanks below.
            if y == 0:
                text = f" ⚠ terminal unavailable: {self._spawn_error}"
                return Strip([Segment(text[:width] if width else text)])
            return Strip.blank(width)
        if screen is None or y >= screen.lines:
            return Strip.blank(width)
        if self.is_dead and self._scroll == 0 and y == screen.lines - 1:
            # claude exited: overlay a one-line hint on the bottom row so a dead
            # pane isn't just a frozen frame with no cue on how to act. Reads only
            # atomic int/bool (no lock); live view only (s==0) so scrolled-back
            # history stays clean for copy/read. _finalize already repaints once.
            msg = " ⏎ agent exited — Enter relaunches · F10 closes this tab "
            return Strip([Segment(msg[:width] if width else msg, Style(reverse=True))])
        if self._frozen and not self.is_dead and self._scroll == 0 and y == 0:
            # Frozen for copy/select: the view holds still while claude streams in
            # the background, so a WezTerm Shift+drag selection survives. One-row
            # hint at the TOP (recent output is at the bottom, where you select);
            # Shift+F9 or any keypress resumes.
            msg = " ❄ frozen — Shift+drag to copy · Shift+F9 / type to resume "
            return Strip([Segment(msg[:width] if width else msg, Style(reverse=True))])

        with self._lock:
            cols = screen.columns
            # Clamp into the (possibly just-resized) grid — pyte does NOT clamp the
            # cursor on shrink, so a stale cursor_y >= lines would make the cursor
            # vanish for a frame (no display row matches y == cursor_y).
            cursor_x = max(0, min(screen.cursor.x, cols - 1))
            cursor_y = max(0, min(screen.cursor.y, screen.lines - 1))
            # Honour DECTCEM (?25l/?25h): a full-screen TUI (e.g. an agent picker)
            # HIDES the cursor while it repaints, then shows it. pyte tracks this as
            # cursor.hidden; without checking it we drew saikai's reversed cursor cell
            # throughout the repaint — a stray cursor flickering over the half-drawn
            # layout ("the screen-update cursor is visible / layout looks broken").
            cursor_hidden = bool(getattr(screen.cursor, "hidden", False))
            s = self._scroll
            buf = self._buf_for_row(screen, s, y)
            cells = [buf[x] for x in range(cols)] if buf is not None else None

        if cells is None:
            return Strip.blank(width)
        # Cursor only in the live view (it lives at the bottom, not in history).
        # _blink_on toggles every 0.5s while focused (Windows) so the cursor blinks
        # and the keepalive re-emits its move; it stays True elsewhere. (#wt-ime-blink)
        show_cursor = (s == 0 and self.has_focus and y == cursor_y
                       and not self.is_dead and not cursor_hidden
                       and getattr(self, "_blink_on", True))
        _has_sel = self._sel_anchor is not None and self._sel_head is not None
        segments = []
        run_chars: list[str] = []
        run_style = None
        run_start = 0

        def flush(end: int) -> None:
            if run_chars:
                segments.append(Segment("".join(run_chars), run_style))

        for x, ch in enumerate(cells):
            # pyte stores a full-width glyph at x and an empty-string STUB at
            # x+1. Emitting anything for the stub injects an extra column and
            # shifts every line containing CJK / emoji / box-drawing. Skip it —
            # the glyph already carries width 2 (real blank cells hold " ").
            if ch.data == "":
                continue
            if show_cursor and x == cursor_x and not _IS_WIN:
                # Non-Windows: draw saikai's own cursor (cell reversed, keeping the
                # cell's real fg/bg/bold so a themed prompt isn't flattened). On
                # Windows we instead show the terminal's NATIVE cursor via
                # _show_hw_cursor — its configured thin bar — and skip this block so
                # there's no wide reverse-block on top of it. (#native-cursor)
                flush(x)
                run_chars = []
                segments.append(Segment(ch.data or " ",
                                        _cell_style(ch) + Style(reverse=True)))
                run_style = None
                continue
            st = _cell_style(ch)
            if _has_sel and self._in_sel(y, x):
                # XOR reverse so the selection stays visible even over claude's OWN
                # reverse-video cells (highlighted menu row / footer); a plain
                # +reverse=True would no-op on an already-reversed cell.
                st = st + Style(reverse=not bool(getattr(st, "reverse", False)))
            if st != run_style and run_chars:
                segments.append(Segment("".join(run_chars), run_style))
                run_chars = []
            run_style = st
            run_chars.append(ch.data)
        if run_chars:
            segments.append(Segment("".join(run_chars), run_style))
        # Let Textual compute the cell length (handles CJK/emoji double-width).
        return Strip(segments)

    # ── (2) raw keys -> PTY bytes ──────────────────────────────────────────────
    def on_key(self, event) -> None:  # events.Key
        # Escape hatch: hand focus back to the host (the session list) so the
        # terminal doesn't swallow every key forever.
        if event.key == RELEASE_FOCUS_KEY:
            self.post_message(self.FocusReleased())
            event.stop()
            return
        if self._pty is None or self.is_dead:
            # Dead pane: let keys bubble so the host's bindings (close tab,
            # switch tab) still work.
            return
        if self._frozen:
            self.toggle_freeze()   # any key = done selecting → resume live updates
        data = encode_key(event.key, getattr(event, "character", None))
        if data is None:
            return
        self._snap_to_live()   # typing returns the view to the live bottom
        try:
            self._pty.write(data)
        except Exception:
            # Child went away between isalive() checks — mark dead, let the
            # reader's EOF path finalize.
            pass
        event.stop()   # don't leak the key to the host app's bindings

    def _snapshot_frozen(self) -> None:
        """Pin the currently-DISPLAYED live rows (scroll==0) as fixed lists of
        immutable pyte Chars, so a frozen view's render AND selection-copy reflect
        the frame the user sees. The reader keeps feeding pyte into screen.buffer
        while frozen, so reading it live would render/copy text that scrolled in
        AFTER the freeze (the wrong-copy bug). Takes the lock (UI-thread caller)."""
        scr = getattr(self, "_screen", None)   # getattr: __new__-built test instances
        if scr is None:
            self._frozen_buf = None
            return
        try:
            with self._lock:
                cols = scr.columns
                self._frozen_buf = {y: [scr.buffer[y][x] for x in range(cols)]
                                    for y in range(scr.lines)}
        except Exception:
            self._frozen_buf = None

    def toggle_freeze(self) -> bool:
        """Pause/resume per-chunk repaints WITHOUT scrolling, so a streaming pane
        holds still and a drag selection survives (the reader keeps feeding pyte in
        the background). Freeze PINS the displayed frame (snapshot) so render + copy
        stay consistent; resume drops it and repaints once to catch up. UI thread."""
        self._frozen = not self._frozen
        if self._frozen:
            self._snapshot_frozen()
        else:
            self._frozen_buf = None
            try:
                self.refresh()
            except Exception:
                pass
        return self._frozen

    def on_paste(self, event) -> None:  # events.Paste (bracketed paste)
        text = getattr(event, "text", "")
        if self._pty is not None and not self.is_dead and text:
            text = _normalize_paste_newlines(text)   # CRLF → LF (Windows double-enter)
            # Re-wrap in bracketed-paste markers when claude enabled the mode
            # (?2004h, tracked in _consume) so it knows this is a PASTE — else each
            # embedded newline submits the line and a multi-line paste runs early.
            # _wrap_bracketed_paste strips any embedded markers to block breakout.
            if getattr(self, "_bracketed_paste", False):
                text = _wrap_bracketed_paste(text)
            self._snap_to_live()   # pasting returns the view to the live bottom
            try:
                self._pty.write(text)
            except Exception:
                pass
            event.stop()

    def paste_text(self, text: str) -> None:
        """Inject text into the pane as a PASTE (bracketed when claude enabled
        ?2004h) so embedded newlines don't submit line-by-line. UI-thread only."""
        if self._pty is None or self.is_dead or not text:
            return
        text = _normalize_paste_newlines(text)   # CRLF → LF (Windows double-enter)
        if getattr(self, "_bracketed_paste", False):
            text = _wrap_bracketed_paste(text)   # strips embedded markers (breakout)
        self._snap_to_live()   # injected input returns the view to the live bottom
        try:
            self._pty.write(text)
        except Exception:
            pass

    def submit(self) -> None:
        """Send a single Enter (\\r) to submit the current input. UI-thread only."""
        if self._pty is None or self.is_dead:
            return
        self._snap_to_live()   # submitting returns the view to the live bottom
        try:
            self._pty.write("\r")
        except Exception:
            pass

    # ── mouse wheel -> scroll back through history.top ─────────────────────────
    def _forward_wheel(self, event, up: bool) -> bool:
        """When the child enabled mouse reporting, send it a WHEEL event so a
        full-screen TUI scrolls its OWN view — instead of saikai's scrollback, which
        is empty in the alt-screen such a TUI runs in (so the wheel did nothing).
        SGR (?1006) encoding when negotiated, else legacy X10. Returns True if sent."""
        if not getattr(self, "_mouse_reporting", False) or self._pty is None or self.is_dead:
            return False
        try:
            col = max(1, int(getattr(event, "x", 0)) + 1)   # event coords → 1-based cell
            row = max(1, int(getattr(event, "y", 0)) + 1)
            btn = 64 if up else 65                           # SGR wheel: 64 = up, 65 = down
            if getattr(self, "_mouse_sgr", False):
                seq = f"\x1b[<{btn};{col};{row}M"
            else:                                            # legacy X10 (cells capped at 223)
                seq = "\x1b[M" + chr(32 + btn) + chr(32 + min(col, 223)) + chr(32 + min(row, 223))
            self._pty.write(seq)
            return True
        except Exception:
            return False

    def on_mouse_scroll_up(self, event) -> None:    # events.MouseScrollUp
        if self._forward_wheel(event, up=True):     # child owns the wheel (mouse mode on)
            try:
                event.stop()
            except Exception:
                pass
            return
        if self._screen is None:
            return
        with self._lock:   # same lock the reader uses to bump _scroll in _consume
            self._scroll = min(self._scroll + 3, len(self._screen.history.top))
        try:
            event.stop()
        except Exception:
            pass
        self.refresh()

    def on_mouse_scroll_down(self, event) -> None:  # events.MouseScrollDown
        if self._forward_wheel(event, up=False):    # child owns the wheel (mouse mode on)
            try:
                event.stop()
            except Exception:
                pass
            return
        with self._lock:
            moved = self._scroll > 0
            if moved:
                self._scroll = max(0, self._scroll - 3)
        if moved:
            self.refresh()
        try:
            event.stop()
        except Exception:
            pass

    def _snap_to_live(self) -> None:
        """Return the view to the live bottom (_scroll = 0) so new output shows at
        once. Called from the INPUT paths (on_key / on_paste / paste_text / submit):
        typing into a scrolled-back pane must jump to the live view like every
        terminal — the reader repaints ONLY at _scroll == 0 (and bumps _scroll to
        keep a scrolled-back view pinned as output streams in), so without this the
        agent's reply stayed invisible until the user wheeled all the way back down.
        _scroll is guarded by _lock (the reader bumps it in _consume); refresh() runs
        OUTSIDE the lock (render_line takes it). UI-thread caller."""
        with self._lock:
            changed = self._scroll != 0
            self._scroll = 0
        if changed:
            try:
                self.refresh()
            except Exception:
                pass

    # ── saikai-owned text selection (drag) ─────────────────────────────────────
    # The host terminal's native Shift+drag can't anchor to a TUI widget — saikai
    # repaints a fixed region, so a streaming pane wipes the native selection (see
    # saikai/CLAUDE.md). saikai therefore captures a plain LEFT-drag itself: freeze
    # on press (stream can't repaint over it), highlight while dragging, copy on
    # release. Coords are widget-relative display rows/cols, matching render_line.
    def _buf_for_row(self, screen, s, y):
        """pyte cell-row backing display row y (lock held). s>0 windows into
        history.top + live buffer; None = past the scrollback top."""
        if s > 0:
            hist = screen.history.top
            idx = _scroll_row_index(len(hist), s, y)
            if idx < 0:
                return None
            return hist[idx] if idx < len(hist) else screen.buffer[idx - len(hist)]
        # Live view: while frozen, read the pinned snapshot so render AND copy
        # reflect the displayed frame, not the still-mutating live buffer (the
        # reader keeps feeding pyte while frozen). Guard the row length so a
        # resize-while-frozen falls back to live instead of IndexError. getattr for
        # the __new__-built test instances that don't run __init__.
        if getattr(self, "_frozen", False) and getattr(self, "_frozen_buf", None) is not None:
            row = self._frozen_buf.get(y)
            if row is not None and len(row) >= screen.columns:
                return row
        return screen.buffer[y]

    def _in_sel(self, y: int, x: int) -> bool:
        a, h = self._sel_anchor, self._sel_head
        if a is None or h is None:
            return False
        (r0, c0), (r1, c1) = (a, h) if a <= h else (h, a)
        if y < r0 or y > r1:
            return False
        if r0 == r1:
            return c0 <= x <= c1
        if y == r0:
            return x >= c0
        if y == r1:
            return x <= c1
        return True

    def _extract_selection(self) -> str:
        a, h = self._sel_anchor, self._sel_head
        screen = self._screen
        if a is None or h is None or screen is None:
            return ""
        (r0, c0), (r1, c1) = (a, h) if a <= h else (h, a)
        lines = []
        with self._lock:
            s = self._scroll
            cols = screen.columns
            for y in range(r0, r1 + 1):
                buf = self._buf_for_row(screen, s, y)
                if buf is None:
                    lines.append("")
                    continue
                if r0 == r1:
                    lo, hi = c0, c1
                elif y == r0:
                    lo, hi = c0, cols - 1
                elif y == r1:
                    lo, hi = 0, c1
                else:
                    lo, hi = 0, cols - 1
                hi = min(hi, cols - 1)
                row = "".join(buf[x].data for x in range(max(lo, 0), hi + 1)
                              if buf[x].data != "")
                lines.append(row.rstrip())
        return "\n".join(lines).strip("\n")

    def _copy_text(self, text: str) -> None:
        """Cross-platform clipboard: native OS clipboard first
        (codepage-safe — clip.exe mangles multibyte text under a mismatched
        console codepage), then OSC-52 via the app (Linux/remote terminals)."""
        if not text:
            return
        if sys.platform == "win32":
            if set_clipboard_windows(text):
                return
            try:
                # Fallback if the Win32 path failed (e.g. clipboard locked). UTF-8
                # because saikai.cmd sets chcp 65001; best-effort only.
                subprocess.run(["clip"], input=text.encode("utf-8"), check=True,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return
            except Exception:
                pass
        elif sys.platform == "darwin":
            # Textual's OSC-52 path does not work in Terminal.app. Over SSH the
            # helper deliberately declines so OSC-52 can target the client.
            if set_clipboard_macos(text):
                return
        try:
            self.app.copy_to_clipboard(text)
        except Exception:
            pass

    def on_mouse_down(self, event) -> None:   # events.MouseDown
        # Record a PENDING anchor only — do NOT freeze / capture_mouse / refresh on
        # the bare press. A click (down+up with no drag) must simply focus the pane
        # and keep WT's IME alive: freezing + capturing on EVERY press churned focus
        # (blur→focus) and dropped the IME on each click (the ×/OK flicker traced to
        # real button-1 events landing on the pane). The selection engages on the
        # first real drag move (on_mouse_move). Don't stop the event — the click
        # still focuses the pane. (#click-no-freeze)
        if self._screen is None or self.is_dead or getattr(event, "button", 1) != 1:
            return
        self._pending_anchor = (event.y, event.x)
        self._sel_anchor = None
        if _IME_DEBUG:
            _log(f"mouse-down at=({getattr(event,'x','?')},{getattr(event,'y','?')}) "
                 f"button={getattr(event,'button','?')} "
                 f"screen=({getattr(event,'screen_x','?')},{getattr(event,'screen_y','?')}) "
                 f"(pending, no freeze) mouse_report={getattr(self, '_mouse_reporting', False)}")

    def _begin_drag_selection(self) -> None:
        """Engage the selection state (freeze + snapshot + capture + autoscroll)
        once a real drag is detected — deferred from on_mouse_down so a bare click
        never freezes the pane or churns focus (WT IME). (#click-no-freeze)"""
        self._sel_prev_frozen = self._frozen
        self._frozen = True
        if self._frozen_buf is None:     # entering freeze for this drag → pin frame
            self._snapshot_frozen()      # (already Shift+F9-frozen → keep its frame)
        self._autoscroll_dir = 0
        if self._autoscroll_timer is None:      # ticks while a drag sits at an edge
            try:
                self._autoscroll_timer = self.set_interval(0.06, self._autoscroll_tick)
            except Exception:
                self._autoscroll_timer = None
        try:
            self.capture_mouse()
        except Exception:
            pass

    def on_mouse_move(self, event) -> None:   # events.MouseMove
        # Engage the drag-selection on the FIRST real movement after a press. Until
        # then the press is only a focus click (no freeze/capture), so the IME lives.
        if self._sel_anchor is None:
            pend = getattr(self, "_pending_anchor", None)
            if pend is None or (event.y, event.x) == pend:
                return                        # no press, or no movement yet
            self._sel_anchor = pend           # real drag → start selecting now
            self._sel_head = pend
            self._begin_drag_selection()
        scr = self._screen
        rows = scr.lines if scr is not None else 0
        cols = scr.columns if scr is not None else 0
        # A captured drag reports coords outside the pane; clamp the head into the
        # visible grid so the highlight/extract stay in-bounds.
        y = max(0, min(event.y, rows - 1)) if rows else event.y
        x = max(0, min(event.x, cols - 1)) if cols else event.x
        self._sel_head = (y, x)
        # Edge auto-scroll: while the pointer sits at (or past) the top/bottom edge,
        # keep scrolling so the selection can extend beyond the visible region. The
        # tick does the actual scroll + anchor pinning. (#drag-autoscroll)
        if rows:
            self._autoscroll_dir = (1 if event.y <= 0
                                    else -1 if event.y >= rows - 1 else 0)
        self.refresh()
        try:
            event.stop()
        except Exception:
            pass

    def _autoscroll_tick(self) -> None:
        """While drag-selecting with the pointer held at the top/bottom edge,
        scroll one line in that direction and keep the anchor pinned to its content
        so the selection extends past the visible region. Since the visible row for
        a fixed line is `hist - scroll + y` (_scroll_row_index), bumping scroll by Δ
        means the anchor's row must shift by Δ to stay on the same text. UI-thread
        only; _scroll mutates under the lock, refresh runs outside it. (#drag-autoscroll)"""
        if self._sel_anchor is None or self._autoscroll_dir == 0:
            return
        scr = self._screen
        if scr is None:
            return
        d = self._autoscroll_dir
        with self._lock:
            hist = len(scr.history.top)
            old = self._scroll
            self._scroll = (min(old + 1, hist) if d > 0 else max(old - 1, 0))
            new = self._scroll
        delta = new - old
        if _IME_DEBUG:
            _log(f"autoscroll d={d} scroll {old}->{new} hist={hist} delta={delta}")
        if delta == 0:
            return                              # hit the scrollback top / live bottom
        ay, ax = self._sel_anchor
        self._sel_anchor = (ay + delta, ax)     # pin anchor to its line
        hx = self._sel_head[1] if self._sel_head else ax
        self._sel_head = (0 if d > 0 else scr.lines - 1, hx)   # head rides the edge
        self.refresh()

    def _stop_autoscroll(self) -> None:
        self._autoscroll_dir = 0
        if self._autoscroll_timer is not None:
            try:
                self._autoscroll_timer.stop()
            except Exception:
                pass
            self._autoscroll_timer = None

    def on_mouse_up(self, event) -> None:     # events.MouseUp
        if _IME_DEBUG:
            _log(f"mouse-up at=({getattr(event,'x','?')},{getattr(event,'y','?')}) "
                 f"button={getattr(event,'button','?')} anchor={self._sel_anchor} "
                 f"pending={getattr(self, '_pending_anchor', None)}")
        self._pending_anchor = None            # click/drag ended; drop the pending press
        if self._sel_anchor is None:
            return                             # bare click (no drag) → nothing to finalize
        self._stop_autoscroll()
        try:
            self.release_mouse()
        except Exception:
            pass
        dragged = self._sel_head is not None and self._sel_head != self._sel_anchor
        text = self._extract_selection() if dragged else ""
        self._sel_anchor = self._sel_head = None
        self._frozen = self._sel_prev_frozen     # resume (unless Shift+F9-frozen)
        if not self._frozen:
            self._frozen_buf = None              # back to live → drop the snapshot
        if text:
            self._copy_text(text)
        self.refresh()
        try:
            event.stop()
        except Exception:
            pass

    # ── (3) widget resize -> pyte + PTY ────────────────────────────────────────
    def on_resize(self, event) -> None:  # events.Resize
        if self._screen is None:
            return
        rows, cols = self._dims()
        with self._lock:
            self._scroll = 0     # geometry changed; drop any scrollback offset
            try:
                self._screen.resize(rows, cols)     # pyte: (rows, cols)!
            except Exception:
                pass
        if self._pty is not None and not self.is_dead:
            try:
                self._pty.setwinsize(rows, cols)    # winpty: (rows, cols)
            except Exception:
                pass
        self.refresh()

    # ── (4) background reader -> feed pyte -> repaint on the UI thread ─────────
    def _sync_deferring(self) -> bool:
        """True while honouring a synchronized-update block (?2026h…?2026l): the
        pane repaint is held so a half-drawn frame isn't shown (the tearing the user
        saw as "layout looks broken"). A safety timeout forces the repaint if the
        block never closes, so a buggy/stuck child can't freeze the pane."""
        if not getattr(self, "_in_sync_update", False):
            return False
        return (time.monotonic() - getattr(self, "_sync_started", 0.0)) <= 0.2

    def _read_loop(self) -> None:
        pty = self._pty
        assert pty is not None
        try:
            while not self._stop.is_set():
                try:
                    chunk = pty.read()              # blocking; str on winpty
                except EOFError:                     # child closed the pty
                    break
                except Exception:
                    break
                if not chunk:
                    # Defensive: some backends may yield "" transiently. Avoid a
                    # busy-spin; re-check isalive and back off before continuing.
                    if not _safe_isalive(pty):
                        break
                    time.sleep(0.01)
                    continue
                self._consume(chunk)
                # NEVER touch the UI from this thread — marshal a COALESCED
                # repaint so a fast stream of small chunks can't flood the UI.
                # While scrolled back (copy mode) the pinned view shows the SAME
                # history lines regardless of new output (_consume keeps the pin
                # by bumping _scroll), so the repaint would rewrite identical cells
                # for nothing AND clear a WezTerm Shift+drag selection. Skip it —
                # scrolling up thus "freezes" the pane so the user can select/copy;
                # scrolling back to the bottom (_scroll == 0) resumes live repaint.
                # Defer the repaint inside a synchronized-update block (?2026) so a
                # half-drawn agent-UI frame isn't shown; the closing ?2026l (or the
                # safety timeout) lets the next chunk paint the COMPLETE frame.
                if self._scroll == 0 and not self._frozen and not self._sync_deferring():
                    self._schedule_pane_refresh()
        finally:
            self._finalize()

    def _consume(self, chunk: str) -> None:
        """Feed a decoded chunk to pyte (handling alt-screen resets) and update
        the rolling tail + status. Runs on the reader thread."""
        if _PTY_CAPTURE:
            try:
                with open(_PTY_CAPTURE, "a", encoding="utf-8") as _cf:
                    _cf.write(repr(chunk) + "\n")   # raw chunk, escape seqs visible
            except Exception:
                pass
        # pywinpty already decoded to str → feed pyte.Stream directly (no
        # re-encode round-trip). Scrub pywinpty 3.x's "0011Ignore" keepalive
        # sentinel, then split the feed at each alt-screen enter/leave boundary,
        # reset()-ing pyte's single buffer there (it has no second buffer) so a
        # pre-alt shell prompt and claude's frames never share one buffer.
        # Reassemble an escape sequence cut at the read() boundary: the pre-pyte
        # scrubs below are stateless, so a split \x1b[>4;2m / \x1b[?1049h would
        # slip through. Hold a SHORT trailing partial-escape for the next chunk.
        chunk = self._esc_carry + chunk
        self._esc_carry = ""
        _m = re.search(r"\x1b(?:[\[\]][0-9;:<>=?]*)?$", chunk)
        if _m is not None and (len(chunk) - _m.start()) < 32:
            self._esc_carry = chunk[_m.start():]
            chunk = chunk[:_m.start()]
        if _IS_WIN:
            # pywinpty 3.x keepalive sentinel — Windows-only noise. On the POSIX
            # (ptyprocess) byte stream "0011Ignore" is ordinary output, so scrubbing
            # it on every backend would silently corrupt a child that legitimately
            # prints that string (a log line, a hex dump, a test fixture). (#12)
            chunk = chunk.replace("0011Ignore", "")
        chunk = _PRIVATE_SGR_RE.sub("", chunk)   # drop XTMODKEYS \x1b[>4;2m etc. (pyte misreads as SGR-4 underline)
        chunk = _KITTY_KBD_RE.sub("", chunk)     # drop Kitty-keyboard CSI-u (pyte leaks the trailing 'u' into the grid)
        _bp = _BRACKETED_RE.findall(chunk)       # track claude's bracketed-paste mode for on_paste (last h/l wins)
        if _bp:
            self._bracketed_paste = (_bp[-1] == "h")
        _mr = _MOUSE_REPORT_RE.findall(chunk)    # child wants mouse events (incl. wheel)?
        if _mr:
            self._mouse_reporting = (_mr[-1] == "h")
        _msgr = _MOUSE_SGR_RE.findall(chunk)     # …in SGR extended encoding?
        if _msgr:
            self._mouse_sgr = (_msgr[-1] == "h")
        _su = _SYNC_RE.findall(chunk)            # synchronized-update block open/close
        if _su:
            self._in_sync_update = (_su[-1] == "h")
            if self._in_sync_update:
                self._sync_started = time.monotonic()
        if not chunk:
            return
        with self._lock:
            top_before = len(self._screen.history.top)
            try:
                marks = list(_ALT_ANY_RE.finditer(chunk))
                if len(marks) <= 1:
                    # 0 or 1 transition — the normal case; feed exactly as before.
                    pos = 0
                    for m in marks:
                        seg = chunk[pos:m.start()]
                        if seg:
                            self._stream.feed(seg)
                        entering = m.group().endswith("h")
                        if entering != self._alt.in_alt:
                            self._alt.in_alt = entering
                            self._screen.reset()
                            self._scroll = 0
                        self._stream.feed(m.group())
                        pos = m.end()
                    rest = chunk[pos:]
                    if rest:
                        self._stream.feed(rest)
                else:
                    # >1 transition in one chunk: collapse the reset amplification.
                    # Nothing renders mid-_consume and each reset() (a full pyte
                    # buffer reallocation, here under self._lock) discards the prior
                    # buffer, so only the content AFTER the LAST state-changing toggle
                    # is ever visible. Simulate to find that toggle, reset ONCE, and
                    # feed from there — behaviourally identical, but O(1) resets. (#audit-altscreen-reset)
                    sim = self._alt.in_alt
                    last_reset = None
                    for m in marks:
                        entering = m.group().endswith("h")
                        if entering != sim:
                            sim = entering
                            last_reset = m.start()
                    if last_reset is None:
                        self._stream.feed(chunk)      # every marker was a no-op
                    else:
                        self._screen.reset()
                        self._alt.in_alt = sim
                        self._scroll = 0
                        self._stream.feed(chunk[last_reset:])
            except Exception:
                # A malformed sequence must not kill the reader; drop rather than crash.
                pass
            # If the user is scrolled back, advance the offset by however many
            # lines just scrolled into history so their view stays pinned to the
            # same content instead of being dragged by new output.
            if self._scroll > 0:
                added = len(self._screen.history.top) - top_before
                if added > 0:
                    self._scroll = min(self._scroll + added,
                                       len(self._screen.history.top))
            self._scr_ver += 1   # screen mutated → invalidates the _current_screen cache
        # Classify from the CURRENT screen + claude's OSC-0 title (its own state
        # glyph), not a rolling byte tail: a tail keeps stale "esc to interrupt"
        # / answered prompts that scrolled up and would misclassify an idle pane.
        _txt, _title = self._current_screen()
        self._update_status(self._classify(_txt, _title))

    def _current_screen(self) -> tuple:
        """(visible text, title) under the lock. `title` is claude's OSC-0 title
        — its leading glyph (braille spinner = working, ✳ = ready) is the
        reliable state signal; pyte tracks it via set_title."""
        with self._lock:
            if self._screen is None:
                return "", ""
            # Reuse the last join when the screen hasn't changed since (the host
            # poll and render path both call this between feeds).
            if self._scr_ver == self._cached_ver:
                return self._cached_screen
            try:
                # _pyte_grid_lines, not screen.display: display's wcwidth assert
                # can raise on real output and would blank the classifier. (#pane-dump)
                txt = "\n".join(_pyte_grid_lines(self._screen))
            except Exception:
                txt = ""
            title = getattr(self._screen, "title", "") or ""
            self._cached_ver = self._scr_ver
            self._cached_screen = (txt, title)
            return txt, title

    def _classify(self, txt: str, title: str) -> str:
        """Run the status classifier, then suppress a body-text 'waiting' while on
        the ALT screen. claude's full-screen TUIs (the agent switcher, /help, …)
        render numbered-menu / choose-one text that _WAITING_RE / _MENU_RE misfire
        on, flipping the pane to a false 'needs input' — and back, as the TUI
        redraws on scroll/selection (the reported bug). claude's REAL task prompts
        (permission / forced choice) render in the NORMAL buffer, so a 'waiting'
        reached only through the alt-screen body isn't real → treat it as idle. The
        title-spinner 'busy' path is unaffected (it returns before the waiting
        check), so a genuinely working alt-screen UI still reads busy. (#alt-waiting)"""
        classifier = getattr(self, "_status_classifier", classify_pty_status)
        st = classifier(txt, title)
        alt = getattr(self, "_alt", None)
        if st == "waiting" and alt is not None and alt.in_alt:
            return "idle"
        return st

    def refresh_status(self) -> None:
        """Re-classify from the current screen + title. The host calls this
        periodically so a pane that went idle WITHOUT new output (no reader tick
        to re-run _consume) still flips out of 'busy', and the debounce gets its
        second tick on the timer cadence."""
        if self._screen is None or self.is_dead:
            return
        # Skip the screen-join + classify for a STABLE pane that produced no
        # output since the last poll — UNLESS it is still 'busy' (must keep being
        # re-checked so it can flip to idle on the debounce's 2nd tick when claude
        # stops without emitting anything further) OR a non-busy flip is mid-
        # debounce (_pending_status set): the trust-folder gate classifies
        # 'waiting' once, then claude goes silent, so without the pending check the
        # 'waiting' never gets its 2nd tick and the pane never shows "Needs input".
        if (self._scr_ver == self._last_poll_ver and self._status != "busy"
                and getattr(self, "_pending_status", None) is None):
            return
        self._last_poll_ver = self._scr_ver
        txt, title = self._current_screen()
        self._update_status(self._classify(txt, title))

    def _update_status(self, new: str) -> None:
        """Debounce: a new status must persist >=2 ticks (reader OR host poll)
        before it flips (spinners momentarily clear the line and would otherwise
        flicker Idle<->Busy). Busy is reported immediately (responsiveness); the
        flip OUT of Busy is what we debounce. The pending/status RMW is guarded
        by self._lock (reader thread + UI poll both call this); the status
        callback is marshalled OUTSIDE the lock. Calling call_from_thread (it
        BLOCKS until the UI thread runs it) while holding the lock that
        render_line / _current_screen also take DEADLOCKS reader vs UI."""
        fire = None
        with self._lock:
            if new == self._status:
                self._pending_status = None
                self._pending_ticks = 0
            elif new == "busy":
                self._status = "busy"          # report busy immediately
                self._pending_status = None
                self._pending_ticks = 0
                fire = "busy"
            else:
                # leaving busy / changing among waiting/idle: require persistence
                if new == self._pending_status:
                    self._pending_ticks += 1
                else:
                    self._pending_status = new
                    self._pending_ticks = 1
                if self._pending_ticks >= 2:
                    self._status = new
                    self._pending_status = None
                    self._pending_ticks = 0
                    fire = new
        if fire is not None and self._on_status and self.sid:
            self._marshal(lambda: self._safe_status_cb(fire))   # marshal OUTSIDE the lock

    def _set_status(self, status: str) -> None:
        self._status = status
        if self._on_status and self.sid:
            self._marshal(lambda: self._safe_status_cb(status))

    def _safe_status_cb(self, status: str) -> None:
        try:
            self._on_status(self.sid, status)  # type: ignore[arg-type]
        except Exception:
            pass

    def _finalize(self) -> None:
        """Reader-thread teardown: mark dead, notify the host (on the UI
        thread), repaint once more so the final frame is shown."""
        if not self.is_dead:
            _log(f"exit: sid={(getattr(self, 'sid', None) or '?')[:8]} (agent ended)")
        self.is_dead = True
        # A pane frozen for copy/select (Shift+F9) that then dies must not stay
        # pinned to its stale snapshot — clear freeze so the final live frame shows
        # (on_key early-returns for a dead pane before its resume-unfreeze line).
        # BUT do not clobber an ACTIVE drag-selection's pinned snapshot: if the
        # child exits mid-drag, on_mouse_up still needs _frozen_buf to extract the
        # selection (else it falls back to the live/dead buffer). is_dead is set
        # ABOVE, so no NEW drag can start (on_mouse_down bails on is_dead); only an
        # in-progress drag (sel_anchor set) is preserved, and its own on_mouse_up
        # restores the state. (#audit-finalize-race)
        if self._sel_anchor is None:
            self._frozen = False
            self._frozen_buf = None
        if self._status != "dead":
            self._status = "dead"
            if self._on_status and self.sid:
                self._marshal(lambda: self._safe_status_cb("dead"))
        if self._on_exit and self.sid:
            self._marshal(self._safe_exit_cb)
        self._marshal(self.refresh)

    def _safe_exit_cb(self) -> None:
        try:
            self._on_exit(self.sid)  # type: ignore[arg-type]
        except Exception:
            pass

    def _schedule_pane_refresh(self) -> None:
        """Coalesce per-chunk repaints: queue at most ONE refresh on the UI
        thread at a time. claude streams many small chunks/sec and one
        call_from_thread per chunk floods the UI; the next chunk re-queues only
        after the UI painted (flag cleared in _do_pane_refresh)."""
        if getattr(self, "_refresh_pending", False):
            return
        self._refresh_pending = True
        self._marshal(self._do_pane_refresh)

    def _do_pane_refresh(self) -> None:   # runs on the UI thread
        self._refresh_pending = False
        self.refresh()
        self._sync_terminal_cursor()

    def snapshot_text(self) -> str:
        """Plain-text dump of the pane's CURRENT visible pyte screen + geometry,
        for the pane-dump debug key (so a garbled bottom can be inspected off the
        live UI). Render the visible grid with ``_pyte_grid_lines`` (NOT pyte's
        ``screen.display``, whose wcwidth assert can raise on real output and once
        left this body empty) under the lock — the reader feeds the stream under
        the same lock — and format outside. (#pane-dump)"""
        lines: list = []
        meta = {}
        with self._lock:
            scr = self._screen
            if scr is not None:
                try:
                    lines = _pyte_grid_lines(scr)  # visible grid as list[str]
                except Exception as exc:
                    # Never swallow into an empty body again — surface the reason
                    # right in the dump so a future failure is self-diagnosing.
                    lines = [f"<snapshot render failed: {exc!r}>"]
                try:
                    meta = {"cols": scr.columns, "rows": scr.lines,
                            "cx": scr.cursor.x, "cy": scr.cursor.y,
                            "chid": bool(getattr(scr.cursor, "hidden", False)),
                            "hist": len(getattr(scr, "history").top)
                                    if hasattr(scr, "history") else "-"}
                except Exception:
                    meta = {}
        try:
            wsz = f"{self.size.width}x{self.size.height}"
        except Exception:
            wsz = "?"
        alt = getattr(self, "_alt", None)
        hdr = (f"sid={getattr(self, 'sid', None)} "
               f"pyte={meta.get('rows','?')}x{meta.get('cols','?')} widget={wsz} "
               f"cursor=({meta.get('cx','?')},{meta.get('cy','?')}) "
               f"cursor_hidden={meta.get('chid','?')} "
               f"alt_screen={getattr(alt, 'in_alt', '?') if alt else '?'} "
               f"hist={meta.get('hist','?')} scroll={self._scroll} "
               f"mouse_report={getattr(self, '_mouse_reporting', False)}")
        ruler = "    " + "".join(str(i % 10) for i in range(meta.get("cols", 0) or 0))
        body = "\n".join(f"{i:3}|{ln}" for i, ln in enumerate(lines))
        return hdr + "\n" + ruler + "\n" + body + "\n"

    def _is_focused_pane(self) -> bool:
        """True if THIS pane is the screen's LOGICAL focus — the correct gate for
        IME anchoring. Uses ``screen.focused is self`` rather than ``self.has_focus``:
        has_focus is app_focus-gated and LAGS a WT window-refocus (on_focus fires
        while has_focus is still False → the anchor bailed → the ×/ON IME flicker on
        alt-tab). screen.focused is set synchronously by set_focus, so it's already
        this pane when on_focus / app-refocus runs. Falls back to has_focus if the
        screen isn't reachable. (#ime-appfocus)"""
        try:
            return self.screen.focused is self
        except Exception:
            return bool(self.has_focus)

    def _blink_tick(self) -> None:
        """Cursor-blink keepalive — THE Windows-Terminal IME fix.

        ROOT CAUSE: Textual anchors the IME by *moving* the (hidden) hardware
        cursor to `app.cursor_position` on each render — saikai's pane does this
        exactly like Textual's own Input. The difference is that Input BLINKS its
        cursor every 0.5s, which re-renders and so keeps re-emitting the cursor
        move to the terminal; WT keeps the IME enabled only while the cursor keeps
        moving. saikai's pane emitted a move only when claude produced output, so
        an IDLE focused pane stopped moving the cursor and WT disabled the IME (×)
        — exactly the observed busy-OK / idle-× flicker. (A stock Textual Input in
        WT does NOT flicker, which proved this is not a Textual/WT limitation but a
        missing keepalive.) Toggling the cursor every 0.5s and re-anchoring forces
        the move to keep flowing, like Input. Windows-only. (#wt-ime-blink)"""
        if not _IME_ANCHOR:
            return
        if self.is_dead or not self._is_focused_pane() or self._scroll != 0:
            return
        self._blink_on = not self._blink_on
        self._sync_terminal_cursor(reason="focus")   # re-assert + refresh → re-emit move

    def _show_hw_cursor(self, show: bool) -> None:
        """Show/hide the REAL terminal cursor (Windows). This makes the user see the
        terminal's NATIVE cursor — its own configured thin bar/underline — at the
        prompt, instead of saikai's full-cell reverse block (which reads as too
        wide). Textual's Windows driver hides the hardware cursor at startup and
        only *moves* it; we re-show it while a pane is focused — the blink keepalive
        keeps moving it so WT renders it and keeps the IME enabled — and hide it on
        blur so the list / unfocused panes don't carry a stray cursor. Windows-only;
        other platforms keep saikai's drawn cursor cell. (#native-cursor)"""
        if not _IS_WIN or not _IME_ANCHOR:
            return
        try:
            drv = getattr(self.app, "_driver", None)
            if drv is not None:
                drv.write("\x1b[?25h" if show else "\x1b[?25l")
        except Exception:
            pass

    def on_focus(self, event=None) -> None:
        # Anchor the IME the moment the pane is focused (don't wait for a repaint).
        if _IME_DEBUG:
            # Full state dump so the × return and the ON return can be DIFFed: the
            # value that flips between them IS the deterministic ×/ON toggle. (#ime-toggle-diag)
            try:
                _sf = self.screen.focused
                _sfn = f"{type(_sf).__name__}#{getattr(_sf,'id',None)}" if _sf else "None"
            except Exception:
                _sfn = "?"
            try:
                _cp = getattr(self.app, "cursor_position", "?")
            except Exception:
                _cp = "?"
            _tim = getattr(self, "_blink_timer", None)
            _log(f"on_focus sid={getattr(self, 'sid', None)} "
                 f"WT={bool(os.environ.get('WT_SESSION'))} "
                 f"has_focus={self.has_focus} focused_pane={self._is_focused_pane()} "
                 f"screen.focused={_sfn} blink_on={getattr(self,'_blink_on','?')} "
                 f"frozen={getattr(self,'_frozen','?')} scroll={self._scroll} "
                 f"cursor_pos={_cp} timer_paused={getattr(_tim,'_active', '?') if _tim else None}")
        # Show the terminal's native cursor (Windows) — see _show_hw_cursor.
        self._show_hw_cursor(True)
        # Start the cursor-blink keepalive so WT keeps the IME enabled while this
        # pane is focused (the real fix for the ×/OK flicker). (#wt-ime-blink)
        self._blink_on = True
        if self._blink_timer is not None:
            try:
                self._blink_timer.resume()
            except Exception:
                pass
        self._sync_terminal_cursor(reason="focus")
        # The immediate sync above can fire before layout settles — inside the
        # focus event `content_region`/`has_focus` may not be valid yet, so the
        # anchor silently skips and WT shows the IME disabled (×) on focus
        # return, intermittently, depending on the layout/focus race. Re-anchor
        # once the next refresh has settled geometry; idempotent when the
        # immediate sync already landed. (#ime-race)
        try:
            self.call_after_refresh(
                lambda: self._sync_terminal_cursor(reason="focus"))
        except Exception:
            pass

    def _sync_terminal_cursor(self, reason: str = "repaint") -> None:
        """Anchor the real (hidden) terminal cursor at claude's cursor cell so the
        host terminal's IME / composition popup appears at the claude prompt — not
        wherever Textual last parked the cursor (e.g. the search box, which owns the
        cursor until something else sets app.cursor_position). Textual keeps the
        hardware cursor hidden but still `move_to`s it every repaint, and WezTerm
        (and other IMEs) anchor the candidate window to that position.

        UI-thread only (called from _do_pane_refresh / on_focus). No-op unless THIS
        pane is focused and live (scroll at the bottom). Reads the pyte cursor under
        the lock, then sets app.cursor_position OUTSIDE the lock (per the concurrency
        invariant — never marshal/block while holding self._lock)."""
        if not _IME_ANCHOR:
            return
        if Offset is None or self.is_dead or not self._is_focused_pane() or self._scroll != 0:
            # Diagnostic: a focus-triggered sync that bails here is the suspected
            # race — the pane regained focus but the cursor never got anchored,
            # so WT may show the IME as disabled (×). (#ime-race)
            if _IME_DEBUG and reason == "focus":
                _log(f"ime SKIP sid={getattr(self, 'sid', None)} reason=focus "
                     f"OffsetNone={Offset is None} dead={self.is_dead} "
                     f"has_focus={self.has_focus} focused_pane={self._is_focused_pane()} "
                     f"scroll={self._scroll}")
            return
        try:
            app = self.app
        except Exception:
            return
        if app is None:
            return
        with self._lock:
            screen = self._screen
            if screen is None:
                return
            try:
                cx = int(screen.cursor.x)
                cy = int(screen.cursor.y)
                chid = bool(getattr(screen.cursor, "hidden", False))
            except Exception:
                return
        try:
            region = self.content_region
            xy = _ime_anchor_xy(cx, cy, region.x, region.y, region.width, region.height)
            if xy is not None:
                app.cursor_position = Offset(*xy)
                # Textual only EMITS the cursor move/visibility to the real
                # terminal during a CompositorUpdate (App._display). Setting
                # cursor_position alone on an *idle* focus return never reaches
                # the terminal, so WT keeps the IME disabled (×) until claude's
                # next redraw pushes it — exactly the busy-OK / idle-× symptom.
                # On a focus-triggered sync, force one repaint so the cursor is
                # pushed to WT now. (Repaint-triggered syncs already ride a
                # render, so we skip the refresh there to avoid a paint loop.)
                # (#ime-race)
                if reason == "focus":
                    try:
                        self.refresh(repaint=True)
                    except Exception:
                        pass
            # Log every focus-triggered sync (and any repaint that moves the anchor)
            # with the cursor-hidden state: if the × correlates with cursor_hidden
            # or xy=None, that pins the cause (blink-off vs region-not-ready). (#ime-race)
            if _IME_DEBUG and (reason == "focus" or xy != getattr(self, "_ime_log_xy", None)):
                self._ime_log_xy = xy
                _log(f"ime {'FOCUS' if reason == 'focus' else 'move'} "
                     f"sid={getattr(self, 'sid', None)} cell={xy} "
                     f"claude=({cx},{cy}) cursor_hidden={chid} "
                     f"region=({region.x},{region.y},{region.width}x{region.height})")
        except Exception:
            pass

    def on_blur(self, event=None) -> None:
        if _IME_DEBUG:
            _who = "?"
            try:
                _f = self.app.focused
                _who = f"{type(_f).__name__}#{getattr(_f, 'id', None)}" if _f is not None else "None"
            except Exception:
                pass
            _log(f"on_blur sid={getattr(self, 'sid', None)} focus_now={_who}")
        # Hide the native cursor (Windows) so an unfocused pane / the list doesn't
        # carry a stray cursor, and stop the keepalive (no moves needed unfocused).
        self._show_hw_cursor(False)
        self._blink_on = True
        if self._blink_timer is not None:
            try:
                self._blink_timer.pause()
            except Exception:
                pass

    # ── thread → UI marshaling (defensive) ─────────────────────────────────────
    def _marshal(self, fn: Callable) -> None:
        """call_from_thread that never raises on the reader thread (the app may
        be shutting down / the widget already unmounted)."""
        app = None
        try:
            app = self.app
        except Exception:
            return
        if app is None:
            return
        try:
            app.call_from_thread(fn)
        except Exception:
            pass

    # ── teardown ───────────────────────────────────────────────────────────────
    def on_unmount(self) -> None:
        self.kill()

    def kill(self):
        """Stop the reader and kill the child PROCESS TREE. Returns the daemon
        reap thread (or None) so a caller that must not exit before the reap
        completes (kill_all on quit) can join it. Idempotent.

        Windows: pywinpty's close() cancels console I/O natively, so it both
        unblocks the blocked reader AND returns fast — safe inline; only the
        slow `taskkill /T` runs on a reap thread.

        POSIX: ptyprocess's close()/terminate() must NEVER run on this (UI)
        thread. Both block (multiple 0.1 s sleeps) — and close() DEADLOCKS:
        ptyprocess wraps the master fd in io.BufferedRWPair, the reader thread
        sits in fileobj.read1() HOLDING the buffer's reader lock, and
        fileobj.close() takes that same lock. close() only signals the child
        AFTER closing the fileobj, so the read never returns and the lock is
        never released → hard freeze of the UI (the 2026-06 Linux Esc-quit
        freeze; Windows never hit it because pywinpty has no such shared lock).
        So here the UI thread only POSTS SIGNALS (non-blocking): SIGHUP+SIGTERM
        to the child's process group (≈ taskkill /T). The child's death EOFs
        the master, the reader unblocks and releases the lock, and the reap
        thread below escalates to SIGKILL if needed and closes the pty safely
        off-thread."""
        self._stop.set()
        pty, pid = self._pty, self._pid
        self._pty = None
        self._pid = None        # idempotent: a 2nd kill() must not re-kill a (recycled) PID
        if pty is None and pid is None:
            return None
        if pid:
            _log(f"kill: sid={(getattr(self, 'sid', None) or '?')[:8]} pid={pid}")
        if _IS_WIN:
            if pty is not None:
                try:
                    pty.close(force=True)   # → terminate() → cancel_io(): unblock reader fast
                except Exception:
                    try:
                        pty.terminate(force=True)
                    except Exception:
                        pass
            if pid:
                t = threading.Thread(target=self._reap_tree, args=(pid,),
                                     name=f"reap-{pid}", daemon=True)
                t.start()
                _track_reap(t)   # joined at interpreter exit (atexit) on every exit path
                return t
            return None
        # POSIX: signals only on this thread (see docstring); blocking close on
        # the reap thread. SIGHUP = what the kernel would send on master close;
        # SIGTERM = belt-and-braces for a SIGHUP-ignoring child.
        _post_signal(pid, "SIGHUP")
        _post_signal(pid, "SIGTERM")
        t = threading.Thread(target=self._reap_posix, args=(pty, pid),
                             name=f"reap-{pid or 'pty'}", daemon=True)
        t.start()
        _track_reap(t)   # joined at quit (kill_all wait=True) and atexit
        return t

    @staticmethod
    def _reap_tree(pid) -> None:
        # taskkill /T reaps grandchildren (claude's node workers) that a plain
        # terminate() would orphan — the SIGHUP-emulation concern, commit 0fd9fcf.
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                timeout=10,
            )
        except Exception:
            pass

    @staticmethod
    def _reap_posix(pty, pid, deadline_s: float = 2.0) -> None:
        # POSIX analog of _reap_tree: bounded wait for the (already signalled)
        # child to die, escalate to SIGKILL, then close the pty fd. The close
        # MUST stay off the UI thread — BufferedRWPair.close() blocks on the
        # reader lock until the reader unblocks at EOF; harmless on this daemon
        # (joined bounded at quit/atexit), fatal on the UI thread. deadline_s is
        # injectable for the headless tests.
        deadline = time.monotonic() + deadline_s
        while pty is not None and _safe_isalive(pty) and time.monotonic() < deadline:
            time.sleep(0.05)
        if pty is None or _safe_isalive(pty):
            _post_signal(pid, "SIGKILL")
        if pty is not None:
            # close() takes the BufferedRWPair reader lock that the reader holds in
            # read1() until the master EOFs. The child's death normally EOFs it and
            # close() returns at once — but a grandchild that survived and kept the
            # slave fd open means no EOF, so close() would block THIS reap thread
            # forever (and join_reaps at quit would only time out, leaking it). We
            # can't SIGKILL to force the EOF: the child PID may have been recycled
            # (test_posix_kill_signals_only). So run close() on a throwaway daemon
            # and stop waiting after a bound — normally it returns instantly; in the
            # stuck case this reap still completes and the fd leaks only until
            # process exit (reclaimed by the OS), instead of hanging forever. (#9)
            _closed = threading.Event()

            def _do_close(_p=pty):
                try:
                    _p.close(force=True)
                except Exception:
                    pass
                finally:
                    _closed.set()

            _ct = threading.Thread(target=_do_close, name=f"reap-close-{pid or 'pty'}",
                                   daemon=True)
            _ct.start()
            # TRACK it: join_reaps awaits every tracked reap at quit/atexit, so a
            # close() wedged by a process-group-escaping grandchild that holds the
            # slave fd is an ACCOUNTED, bounded-at-exit thread — not an untracked
            # one that escapes the join-everything invariant and leaks silently.
            _track_reap(_ct)
            _closed.wait(timeout=2.0)

    # ── messages ────────────────────────────────────────────────────────────────
    if events is not None:  # only define when textual present
        from textual.message import Message as _Message  # type: ignore

        class FocusReleased(_Message):  # type: ignore[misc]
            """Posted when the user presses RELEASE_FOCUS_KEY. The host moves
            focus back to the session list."""


def _safe_isalive(pty) -> bool:
    try:
        return bool(pty.isalive())
    except Exception:
        return False


# Backward-compatible import name while callers migrate to the agent-neutral API.
ClaudeTerminal = AgentTerminal


# ══════════════════════════════════════════════════════════════════════════════
# Session / tab manager
# ══════════════════════════════════════════════════════════════════════════════
class LiveSessionManager:
    """Bookkeeping for the live terminal tabs hosted in saikai's right pane.

    Pure data structure (no Textual coupling) so it is unit-testable: saikai's
    PickerApp owns the TabbedContent and asks this object what to do.

      * ``pane_id(sid)``    — deterministic TabPane id for a session.
      * ``register/forget`` — track sid -> AgentTerminal.
      * ``at_capacity``     — enforce a concurrent-agent cap.
      * ``statuses``        — last-known status per sid for the DataTable.
    """

    def __init__(self, max_live: int = 4) -> None:
        self.max_live = max_live
        self._terms: dict[str, "AgentTerminal"] = {}     # sid -> widget
        self._status: dict[str, str] = {}                 # sid -> status
        self._pane_ids: dict[str, str] = {}               # sid -> TabPane DOM id
        self._reaps: list = []                            # in-flight taskkill threads

    def pane_id(self, sid: str) -> str:
        # The TabPane's DOM id, set at mount to f"tab-live-{sid}" and IMMUTABLE in
        # Textual. Stored per sid so a re-key (parent->child after /clear) can move
        # the SAME pane's id under the new sid — the TabPane keeps its existing
        # tab-live-{parent} id but is now found via the child sid. An unregistered
        # sid falls back to the deterministic default (callers compare by re-
        # deriving via pane_id(), so the fallback is a safe drop-in).
        #
        # Use the FULL sid (Textual DOM ids have no length limit): an 8-char prefix
        # can collide between two sessions sharing their first 8 UUID hex chars, and
        # the mount path would then remove the wrong pane's tab without killing its
        # process. Nothing parses this back to a sid, so the full form is safe.
        return self._pane_ids.get(sid) or f"tab-live-{sid}"

    @property
    def count(self) -> int:
        return len(self._terms)

    def at_capacity(self) -> bool:
        return len(self._terms) >= self.max_live

    def has(self, sid: str) -> bool:
        return sid in self._terms

    def get(self, sid: str) -> Optional["AgentTerminal"]:
        return self._terms.get(sid)

    def register(self, sid: str, term: "AgentTerminal") -> None:
        self._terms[sid] = term
        self._status[sid] = "idle"
        self._pane_ids[sid] = f"tab-live-{sid}"

    def forget(self, sid: str) -> None:
        self._terms.pop(sid, None)
        self._status.pop(sid, None)
        self._pane_ids.pop(sid, None)

    def rekey(self, old_sid: str, new_sid: str) -> None:
        """Move the live pane's identity old_sid -> new_sid: term + status + the
        TabPane DOM id string. After a b2 /clear checkpoint the SAME PTY pane IS
        the child session, so its bookkeeping must follow the new sid (else restore
        resumes the wrong session, Shift+F6 can't find the parent, and re-opening
        the child spawns a duplicate). The pane_id moves verbatim so the child
        REUSES the parent's existing tab-live-{old} DOM id (Textual TabPane ids are
        immutable at runtime — the pane keeps its id, just looked up under the
        child now). Pure dict manipulation, UI-thread only. No-op if old == new or
        old is absent."""
        if old_sid == new_sid or old_sid not in self._terms:
            return
        self._terms[new_sid] = self._terms.pop(old_sid)
        if old_sid in self._status:
            self._status[new_sid] = self._status.pop(old_sid)
        if old_sid in self._pane_ids:
            self._pane_ids[new_sid] = self._pane_ids.pop(old_sid)

    def set_status(self, sid: str, status: str) -> None:
        # Only track status for a REGISTERED pane. A status callback marshalled by
        # the reader just before the pane was closed (forget() popped _terms AND
        # _status) must not re-insert a ghost entry that statuses() then reports
        # (stale marker / false "needs input" toast / phantom Esc-close target).
        if sid in self._terms:
            self._status[sid] = status

    def status(self, sid: str) -> str:
        return self._status.get(sid, "")

    def statuses(self) -> dict[str, str]:
        return dict(self._status)

    def all_terms(self) -> list["AgentTerminal"]:
        return list(self._terms.values())

    def note_reap(self, thread) -> None:
        """Track an in-flight reap thread (from a single-pane close) so a later
        quit can join it and not orphan the grandchildren. Prune already-finished
        reaps first so the list can't grow unbounded over open/close churn — dead
        reaps need no join, and the module-level _REAP_THREADS (atexit join) still
        guarantees every reap is awaited at process exit."""
        if thread is not None:
            self._reaps[:] = [t for t in self._reaps if t.is_alive()]
            self._reaps.append(thread)

    def join_reaps(self, total_timeout: float = 3.0) -> None:
        """Wait (bounded) for all in-flight reaps so process exit doesn't orphan
        node workers — bounded so quit stays snappy even if a taskkill hangs."""
        import time
        deadline = time.monotonic() + total_timeout
        for t in self._reaps:
            try:
                t.join(timeout=max(0.0, deadline - time.monotonic()))
            except Exception:
                pass
        self._reaps = [t for t in self._reaps if t.is_alive()]

    def kill_all(self, wait: bool = False) -> None:
        # Start every kill FIRST so the taskkills run IN PARALLEL, then
        # (optionally) join — closing N panes costs ~one taskkill, not N.
        for term in list(self._terms.values()):
            try:
                self.note_reap(term.kill())
            except Exception:
                pass
        self._terms.clear()
        self._status.clear()
        self._pane_ids.clear()
        if wait:
            self.join_reaps()


# Status → a compact glyph for the tab label. Loud on "waiting" so a session
# needing input is visible even when its tab isn't focused; calm on idle. The
# session LIST in saikai.py renders these SAME live states in its own ASCII marker
# vocabulary (_LIVE_MARKER: waiting "?", busy "~", idle "="/"!"); the two are
# deliberately distinct glyph sets, so keep both in step when adding/renaming a
# status. "dead" → ✓ here; the list drops a dead pane to its dormant file markers.
STATUS_GLYPH = {
    "busy": "◐",      # ◐ working
    "waiting": "⏳",   # ⏳ needs input
    "idle": "○",      # ○ ready
    "dead": "✓",      # ✓ exited
}


def tab_label(title: str, status: str) -> str:
    """Build a TabPane label like '◐ saikai' / '⏳ docs' / '✓ myproj'."""
    glyph = STATUS_GLYPH.get(status, "")
    name = (title or "agent")[:18]
    return f"{glyph} {name}".strip()
