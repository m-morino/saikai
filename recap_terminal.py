#!/usr/bin/env python3
"""
recap_terminal — a live, interactive PTY terminal as a Textual widget.

This module backs recap's TRUE SPLIT-LIVE mode: the left pane stays the
session DataTable; the right pane hosts one or more live `claude` processes,
each in its own tab, each rendered from a real pseudo-console.

Building blocks (spot-checked non-interactively on this Windows box via uv-run;
the live visual render + keystroke path still need an interactive TTY — see NOTE.
NOTE: an earlier draft over-claimed "verified on CPython 3.13.5"; the live
render was NOT executed against a running claude — treat render fidelity as
unproven until interactively tested):

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
isalive / pid). The widget runs on both; recap's primary host is Windows.

NOTE — what can and cannot be verified without an interactive TTY
-----------------------------------------------------------------
CANNOT (needs a human at a terminal):
  * the live visual render (Textual paints the alternate screen) and real
    keyboard forwarding into a running ``claude``.
CAN (and was, on this machine):
  * ``python -m py_compile recap_terminal.py``
  * PTY spawn + threaded read + EOF + exit detection
    (``cmd /c echo … & exit`` round-trip)
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
import subprocess
import sys
import threading
import time
from typing import Callable, Optional


def _log(msg: str) -> None:
    """Best-effort append to the shared recap.log (same file recap.py's _log
    writes; standalone here so this module keeps no recap import). Size-bounded,
    never raises. `[term]` tags lines from the split-live PTY layer so a
    post-mortem can tell the process lifecycle from the list-side events."""
    try:
        d = os.path.join(os.path.expanduser("~"), ".cache", "recap")
        os.makedirs(d, exist_ok=True)
        lf = os.path.join(d, "recap.log")
        try:
            if os.path.getsize(lf) > 1_000_000:
                os.replace(lf, lf + ".1")
        except OSError:
            pass
        with open(lf, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}  [term] {msg}\n")
    except Exception:
        pass


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
# child's grandchildren (claude's node workers). If recap exits before that
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

# ── Soft imports ─────────────────────────────────────────────────────────────
# The widget is only constructed when these are present (recap probes
# TERMINAL_AVAILABLE before offering split-live). Importing this module never
# raises just because a dep is missing — that keeps the preview fallback intact
# and lets py_compile / unit tests run without textual/pyte/pywinpty.
try:
    import pyte  # type: ignore
except Exception:  # pragma: no cover - exercised only when dep absent
    pyte = None  # type: ignore

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
except Exception as _te:  # pragma: no cover - textual is a hard dep of recap
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
    recap surfaces this in a toast so the user knows why it fell back to the
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
    "ctrl+@": "\x00", "ctrl+space": "\x00",
    "ctrl+backslash": "\x1c", "ctrl+right_square_bracket": "\x1d",
    "ctrl+circumflex_accent": "\x1e", "ctrl+underscore": "\x1f",
})


def _normalize_key(spec: str) -> str:
    """Map a human key spec (e.g. 'ctrl+]') to Textual's key name
    ('ctrl+right_square_bracket') so RECAP_RELEASE_KEY accepts either form."""
    s = (spec or "").strip().lower()
    repl = {"]": "right_square_bracket", "[": "left_square_bracket",
            "\\": "backslash", "_": "underscore", "^": "circumflex_accent"}
    if "+" in s:
        head, _, tail = s.rpartition("+")
        return f"{head}+{repl.get(tail, tail)}"
    return repl.get(s, s)

#: The key that releases focus back to the session list (the escape hatch). A
#: focused terminal swallows every key, so without this the user is trapped. Esc
#: goes to claude (interrupt) and the readline editing keys (Ctrl+A/B/E/W/K/…) are
#: forwarded, so the default is Ctrl+] — a control char ConPTY delivers reliably,
#: rarely needed in claude (readline char-search). Override with RECAP_RELEASE_KEY
#: (human form like 'ctrl+]' or a Textual name). Popped from _KEYMAP so it is
#: never forwarded to the child. NOTE: Textual names ']' as right_square_bracket,
#: so the literal 'ctrl+]' string would never match — _normalize_key fixes that.
RELEASE_FOCUS_KEY = _normalize_key(os.environ.get("RECAP_RELEASE_KEY") or "ctrl+]")
_KEYMAP.pop(RELEASE_FOCUS_KEY, None)
# F2/F3 are reserved by recap for prev/next tab (priority bindings); never
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
# these to negotiate key reporting, but recap encodes keys in the legacy format
# regardless, so dropping the negotiation is display-only and harmless. (Plain
# CSI u = SCO restore-cursor has no private marker, so it is NOT stripped.)
_KITTY_KBD_RE = re.compile(r"\x1b\[[<>=?][0-9;:]*u")
# Bracketed-paste mode (CSI ?2004 h/l): claude enables it so it can distinguish a
# PASTE from typed input. pyte doesn't expose the mode, so we track it from the
# output stream and re-wrap pastes (\x1b[200~ … \x1b[201~) in on_paste — otherwise
# claude treats a multi-line paste as typed lines and submits on each newline.
_BRACKETED_RE = re.compile(r"\x1b\[\?2004([hl])")


def _scroll_row_index(hist_len: int, scroll: int, y: int) -> int:
    """Absolute index into (history.top + live buffer) for visible row y at a
    given scroll offset (0 = live bottom). idx < hist_len -> a history line;
    idx >= hist_len -> live buffer row (idx - hist_len)."""
    return hist_len - scroll + y


def set_clipboard_windows(text: str) -> bool:
    """Put `text` on the Windows clipboard as CF_UNICODETEXT via Win32 directly.

    Codepage-INDEPENDENT, which is the whole point: piping to `clip.exe` makes it
    decode stdin using the console's code page, so multibyte text (CJK / emoji)
    garbles whenever the launch codepage differs from what we encoded for — e.g.
    UTF-16LE bytes read back as UTF-8 turned 裏がとれております into 'ψL0h0…'.
    Setting the UTF-16 clipboard format the OS actually stores makes it
    round-trip no matter how recap was started. Returns False on any failure so
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
class ClaudeTerminal(Widget):  # type: ignore[misc]  # Widget is object w/o textual
    """A live PTY terminal rendered from a pyte screen buffer via the Line API.

    One instance owns exactly one child process (an interactive ``claude``,
    or any argv). It spawns on mount, reads in a background thread, feeds the
    bytes to pyte, and marshals a repaint onto the UI thread. Keys are encoded
    to PTY bytes in ``on_key``; resize is propagated to both pyte and the PTY.
    On unmount / app exit it kills the whole child tree.

    Reactivity is kept simple on purpose: a full ``refresh()`` per read chunk
    (Textual then calls ``render_line`` per visible row). That is plenty for a
    chat-style child; dirty-line optimisation can come later.
    """

    can_focus = True
    DEFAULT_CSS = "ClaudeTerminal { width: 1fr; height: 1fr; }"

    def __init__(
        self,
        argv: list[str],
        cwd: Optional[str] = None,
        env: Optional[dict] = None,
        *,
        sid: Optional[str] = None,
        title: str = "claude",
        on_status: Optional[Callable[[str, str], None]] = None,
        on_exit: Optional[Callable[[str], None]] = None,
        **kw,
    ) -> None:
        """
        argv      : list — ALWAYS a list (string argv is over-quoted by the
                    ConPTY shell layer; see pywinpty spike gotcha #3).
        cwd, env  : child working dir / environment (recap builds these via
                    its shared _build_resume_invocation helper).
        sid       : the recap session id this pane is attached to (or None for
                    a brand-new session). Passed back to on_status/on_exit.
        title     : tab label seed.
        on_status : called (sid, status) when Busy/Waiting/Idle changes, so
                    recap can mirror it onto the DataTable marker + tab label.
        on_exit   : called (sid) when the child exits, so recap can re-title
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

        self._pty = None
        self._pid: Optional[int] = None
        self._screen = None          # pyte.Screen
        self._stream = None          # pyte.Stream (feeds str)
        self._alt = AltScreenTracker()
        self._scroll = 0             # lines scrolled back (0 = live bottom)
        self._frozen = False         # paused repaint: hold the view still so a
                                     # streaming pane can be drag-selected
        self._sel_anchor = None      # (row,col) drag start — recap-OWNED selection
        self._sel_head = None        # (row,col) drag head; None ⇒ no selection
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
            self._screen = pyte.HistoryScreen(cols, rows, history=5000)   # (cols, rows)!
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

    def _spawn(self, rows: int, cols: int) -> None:
        kwargs: dict = {"dimensions": (rows, cols)}
        if self._cwd:
            kwargs["cwd"] = self._cwd
        if self._env is not None:
            kwargs["env"] = self._env
        # argv MUST be a list (pywinpty spike gotcha #3).
        self._pty = PtyProcess.spawn(self._argv, **kwargs)
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
            msg = " ⏎ claude exited — Enter relaunches · F10 closes this tab "
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
            s = self._scroll
            buf = self._buf_for_row(screen, s, y)
            cells = [buf[x] for x in range(cols)] if buf is not None else None

        if cells is None:
            return Strip.blank(width)
        # Cursor only in the live view (it lives at the bottom, not in history).
        show_cursor = (s == 0 and self.has_focus and y == cursor_y
                       and not self.is_dead)
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
            if show_cursor and x == cursor_x:
                # break the run, emit the cursor cell reversed, restart. Keep the
                # cell's real fg/bg/bold and just invert it, so the cursor on a
                # themed prompt isn't flattened to a default-colour block.
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
            # Re-wrap in bracketed-paste markers when claude enabled the mode
            # (?2004h, tracked in _consume) so it knows this is a PASTE — else each
            # embedded newline submits the line and a multi-line paste runs early.
            if getattr(self, "_bracketed_paste", False):
                text = "\x1b[200~" + text + "\x1b[201~"
            try:
                self._pty.write(text)
            except Exception:
                pass
            event.stop()

    # ── mouse wheel -> scroll back through history.top ─────────────────────────
    def on_mouse_scroll_up(self, event) -> None:    # events.MouseScrollUp
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

    # ── recap-owned text selection (drag) ─────────────────────────────────────
    # The host terminal's native Shift+drag can't anchor to a TUI widget — recap
    # repaints a fixed region, so a streaming pane wipes the native selection (see
    # recap/CLAUDE.md). recap therefore captures a plain LEFT-drag itself: freeze
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
        """Cross-platform clipboard: Windows Win32 CF_UNICODETEXT first
        (codepage-safe — clip.exe mangles multibyte text under a mismatched
        console codepage), then clip.exe as a fallback, then OSC-52 via the app
        (Linux/macOS/WezTerm/SSH)."""
        if not text:
            return
        if sys.platform == "win32":
            if set_clipboard_windows(text):
                return
            try:
                # Fallback if the Win32 path failed (e.g. clipboard locked). UTF-8
                # because recap.cmd sets chcp 65001; best-effort only.
                subprocess.run(["clip"], input=text.encode("utf-8"), check=True,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return
            except Exception:
                pass
        try:
            self.app.copy_to_clipboard(text)
        except Exception:
            pass

    def on_mouse_down(self, event) -> None:   # events.MouseDown
        # Left-drag selects. Start a selection + FREEZE so a streaming claude can't
        # repaint over it; capture the mouse so a drag outside the pane still
        # tracks. Do NOT stop the event — the click should still focus the pane.
        if self._screen is None or self.is_dead or getattr(event, "button", 1) != 1:
            return
        self._sel_prev_frozen = self._frozen
        self._frozen = True
        if self._frozen_buf is None:     # entering freeze for this drag → pin frame
            self._snapshot_frozen()      # (already Shift+F9-frozen → keep its frame)
        self._sel_anchor = (event.y, event.x)
        self._sel_head = (event.y, event.x)
        try:
            self.capture_mouse()
        except Exception:
            pass
        self.refresh()

    def on_mouse_move(self, event) -> None:   # events.MouseMove
        if self._sel_anchor is None:
            return
        self._sel_head = (event.y, event.x)
        self.refresh()
        try:
            event.stop()
        except Exception:
            pass

    def on_mouse_up(self, event) -> None:     # events.MouseUp
        if self._sel_anchor is None:
            return
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
                if self._scroll == 0 and not self._frozen:
                    self._schedule_pane_refresh()
        finally:
            self._finalize()

    def _consume(self, chunk: str) -> None:
        """Feed a decoded chunk to pyte (handling alt-screen resets) and update
        the rolling tail + status. Runs on the reader thread."""
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
        chunk = chunk.replace("0011Ignore", "")
        chunk = _PRIVATE_SGR_RE.sub("", chunk)   # drop XTMODKEYS \x1b[>4;2m etc. (pyte misreads as SGR-4 underline)
        chunk = _KITTY_KBD_RE.sub("", chunk)     # drop Kitty-keyboard CSI-u (pyte leaks the trailing 'u' into the grid)
        _bp = _BRACKETED_RE.findall(chunk)       # track claude's bracketed-paste mode for on_paste (last h/l wins)
        if _bp:
            self._bracketed_paste = (_bp[-1] == "h")
        if not chunk:
            return
        with self._lock:
            top_before = len(self._screen.history.top)
            try:
                pos = 0
                for m in _ALT_ANY_RE.finditer(chunk):
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
        self._update_status(classify_pty_status(_txt, _title))

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
                txt = "\n".join(self._screen.display)
            except Exception:
                txt = ""
            title = getattr(self._screen, "title", "") or ""
            self._cached_ver = self._scr_ver
            self._cached_screen = (txt, title)
            return txt, title

    def refresh_status(self) -> None:
        """Re-classify from the current screen + title. The host calls this
        periodically so a pane that went idle WITHOUT new output (no reader tick
        to re-run _consume) still flips out of 'busy', and the debounce gets its
        second tick on the timer cadence."""
        if self._screen is None or self.is_dead:
            return
        # Skip the screen-join + classify for a STABLE pane that produced no
        # output since the last poll — UNLESS it is still 'busy', which must keep
        # being re-checked so it can flip to idle (the debounce's 2nd tick) when
        # claude stops without emitting anything further.
        if self._scr_ver == self._last_poll_ver and self._status != "busy":
            return
        self._last_poll_ver = self._scr_ver
        txt, title = self._current_screen()
        self._update_status(classify_pty_status(txt, title))

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
            _log(f"exit: sid={(getattr(self, 'sid', None) or '?')[:8]} (claude ended)")
        self.is_dead = True
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

    def on_focus(self, event=None) -> None:
        # Anchor the IME the moment the pane is focused (don't wait for a repaint).
        self._sync_terminal_cursor()

    def _sync_terminal_cursor(self) -> None:
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
        if Offset is None or self.is_dead or not self.has_focus or self._scroll != 0:
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
            except Exception:
                return
        try:
            region = self.content_region
            xy = _ime_anchor_xy(cx, cy, region.x, region.y, region.width, region.height)
            if xy is not None:
                app.cursor_position = Offset(*xy)
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
        thread reaping the grandchildren (or None) so a caller that must not exit
        before the reap completes (kill_all on quit) can join it.

        The FAST part (close() → cancel_io() → reader unblocks) runs inline; the
        SLOW part (taskkill /T, ~hundreds ms–seconds) runs OFF the UI thread so
        closing one pane — or many in parallel — never freezes recap. Idempotent."""
        self._stop.set()
        pty, pid = self._pty, self._pid
        self._pty = None
        self._pid = None        # idempotent: a 2nd kill() must not re-taskkill a (recycled) PID
        if pid:
            _log(f"kill: sid={(getattr(self, 'sid', None) or '?')[:8]} pid={pid}")
        if pty is not None:
            try:
                pty.close(force=True)   # → terminate() → cancel_io(): unblock reader fast
            except Exception:
                try:
                    pty.terminate(force=True)
                except Exception:
                    pass
        if sys.platform == "win32" and pid:
            t = threading.Thread(target=self._reap_tree, args=(pid,),
                                 name=f"reap-{pid}", daemon=True)
            t.start()
            _track_reap(t)   # joined at interpreter exit (atexit) on every exit path
            return t
        return None

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


# ══════════════════════════════════════════════════════════════════════════════
# Session / tab manager
# ══════════════════════════════════════════════════════════════════════════════
class LiveSessionManager:
    """Bookkeeping for the live terminal tabs hosted in recap's right pane.

    Pure data structure (no Textual coupling) so it is unit-testable: recap's
    PickerApp owns the TabbedContent and asks this object what to do.

      * ``pane_id(sid)``    — deterministic TabPane id for a session.
      * ``register/forget`` — track sid -> ClaudeTerminal.
      * ``at_capacity``     — enforce a concurrent-claude cap.
      * ``statuses``        — last-known status per sid for the DataTable.
    """

    def __init__(self, max_live: int = 4) -> None:
        self.max_live = max_live
        self._terms: dict[str, "ClaudeTerminal"] = {}     # sid -> widget
        self._status: dict[str, str] = {}                 # sid -> status
        self._reaps: list = []                            # in-flight taskkill threads

    @staticmethod
    def pane_id(sid: str) -> str:
        # 8-char prefix keeps the DOM id short but collision-safe enough for a
        # handful of concurrent panes; full sid lives on the widget.
        return f"tab-live-{sid[:8]}"

    @property
    def count(self) -> int:
        return len(self._terms)

    def at_capacity(self) -> bool:
        return len(self._terms) >= self.max_live

    def has(self, sid: str) -> bool:
        return sid in self._terms

    def get(self, sid: str) -> Optional["ClaudeTerminal"]:
        return self._terms.get(sid)

    def register(self, sid: str, term: "ClaudeTerminal") -> None:
        self._terms[sid] = term
        self._status[sid] = "idle"

    def forget(self, sid: str) -> None:
        self._terms.pop(sid, None)
        self._status.pop(sid, None)

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

    def all_terms(self) -> list["ClaudeTerminal"]:
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
        if wait:
            self.join_reaps()


# Status → a compact glyph for the DataTable marker / tab label. Loud on
# "waiting" so a session needing input is visible even when its tab isn't
# focused; calm on idle.
STATUS_GLYPH = {
    "busy": "◐",      # ◐ working
    "waiting": "⏳",   # ⏳ needs input
    "idle": "○",      # ○ ready
    "dead": "✓",      # ✓ exited
}


def tab_label(title: str, status: str) -> str:
    """Build a TabPane label like '◐ recap' / '⏳ docs' / '✓ myproj'."""
    glyph = STATUS_GLYPH.get(status, "")
    name = (title or "claude")[:18]
    return f"{glyph} {name}".strip()
