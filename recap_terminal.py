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

import re
import subprocess
import sys
import threading
from typing import Callable, Optional

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
except Exception as _te:  # pragma: no cover - textual is a hard dep of recap
    _TEXTUAL_IMPORT_ERROR = repr(_te)
    # Stand-ins so the module still imports for py_compile / pure-function tests
    # on a box without textual.
    Widget = object  # type: ignore
    Segment = Style = Strip = events = None  # type: ignore

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
_MENU_RE = re.compile(r"(?:^\s*\d+\.\s+\S.*\n){2,}", re.MULTILINE)


def classify_pty_status(recent_text: str) -> str:
    """Classify the tail of decoded PTY output into ``"busy"`` / ``"waiting"`` /
    ``"idle"``.

    Priority: Busy > Waiting > Idle. ANSI is stripped first (the footer is
    heavily colored) and only the last ~2 KB is inspected (scrollback holds
    stale prompts). recap debounces flips and cross-checks the file-based
    registry status — this is a refinement signal, not the sole source.
    """
    t = _ANSI_RE.sub("", recent_text or "")[-2000:]
    if not t.strip():
        return "idle"
    last_line = t.splitlines()[-1] if t.splitlines() else ""
    if _BUSY_RE.search(t) or _SPINNER_RE.search(last_line):
        return "busy"
    if _WAITING_RE.search(t) or _MENU_RE.search(t):
        return "waiting"
    return "idle"


# ── pyte cell → rich.Style ───────────────────────────────────────────────────
_HEX6 = re.compile(r"\A[0-9a-fA-F]{6}\Z")


def _pyte_color(color: Optional[str]) -> Optional[str]:
    """Map a pyte color (name string like 'red', 6-hex without '#', or
    'default') to a value rich.Style accepts, or None for the terminal
    default."""
    if not color or color == "default":
        return None
    if _HEX6.match(color):
        return "#" + color
    return color  # named color: 'red', 'brightblue', …


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
    "ctrl+backslash": "\x1c", "ctrl+]": "\x1d",
    "ctrl+^": "\x1e", "ctrl+underscore": "\x1f",
})

#: The key that releases focus back to the session list (the escape hatch).
#: A focused terminal swallows every key, so without this the user is trapped.
#: ctrl+f1 is NOT reliably delivered by Windows ConPTY; ctrl+b (tmux-style
#: prefix) is, and claude rarely needs it. Popped from _KEYMAP so it is never
#: forwarded to the child.
RELEASE_FOCUS_KEY = "ctrl+b"
_KEYMAP.pop(RELEASE_FOCUS_KEY, None)


def encode_key(key: str, character: Optional[str]) -> Optional[str]:
    """Translate a Textual key event into the byte string to write to the PTY,
    or None if the key carries nothing the child should receive.

    Pure + table-driven so it is unit-testable without a TTY.
    """
    mapped = _KEYMAP.get(key)
    if mapped is not None:
        return mapped
    # Printable single char (letters, digits, punctuation, space, IME unicode).
    if character and character.isprintable():
        return character
    return None


# ── alt-screen tracking (pyte gap, see pyte spike) ────────────────────────────
# pyte records the ?1049h/?1049l mode bit but has only ONE buffer: it does NOT
# swap/save/restore, and ignores ?47/?1047 entirely. claude is a full-screen
# TUI that lives on the alt screen. We can't give pyte a second buffer, but a
# single pyte buffer rendering the alt-screen content is exactly what an
# embedded terminal wants (claude owns the pane for its whole lifetime). We
# still detect the boundary so the host can RESET the pyte screen on each
# transition — that mirrors a real terminal's buffer swap and prevents the
# pre-alt prompt from bleeding under claude's UI, and prevents claude's last
# frame from lingering after it exits back to a shell.
_ALT_ENTER_RE = re.compile(r"\x1b\[\?(?:1049|1047|47)h")
_ALT_LEAVE_RE = re.compile(r"\x1b\[\?(?:1049|1047|47)l")
_ALT_ANY_RE = re.compile(r"\x1b\[\?(?:1049|1047|47)[hl]")


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
        self._reader: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._lock = threading.Lock()   # guards pyte feed vs render_line read

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
            self._screen = pyte.Screen(cols, rows)          # (cols, rows)!
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

    def _fail(self, msg: str) -> None:
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

        with self._lock:
            buf = screen.buffer[y]              # defaultdict[x] -> Char (safe)
            cursor_x = screen.cursor.x
            cursor_y = screen.cursor.y
            cols = screen.columns
            cells = [buf[x] for x in range(cols)]

        show_cursor = self.has_focus and y == cursor_y and not self.is_dead
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
                # break the run, emit the cursor cell reversed, restart
                flush(x)
                run_chars = []
                segments.append(Segment(ch.data or " ", Style(reverse=True)))
                run_style = None
                continue
            st = _cell_style(ch)
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

    def on_paste(self, event) -> None:  # events.Paste (bracketed paste)
        text = getattr(event, "text", "")
        if self._pty is not None and not self.is_dead and text:
            try:
                self._pty.write(text)
            except Exception:
                pass
            event.stop()

    # ── (3) widget resize -> pyte + PTY ────────────────────────────────────────
    def on_resize(self, event) -> None:  # events.Resize
        if self._screen is None:
            return
        rows, cols = self._dims()
        with self._lock:
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
                    # busy-spin; re-check isalive and continue.
                    if not _safe_isalive(pty):
                        break
                    continue
                self._consume(chunk)
                # NEVER touch the UI from this thread — marshal the repaint.
                self._marshal(self.refresh)
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
        chunk = chunk.replace("0011Ignore", "")
        if not chunk:
            return
        with self._lock:
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
                    self._stream.feed(m.group())
                    pos = m.end()
                rest = chunk[pos:]
                if rest:
                    self._stream.feed(rest)
            except Exception:
                # A malformed sequence must not kill the reader; drop rather than crash.
                pass
        # status tail (ANSI kept; classify strips it). Bound the buffer.
        self._tail = (self._tail + chunk)[-4000:]
        self._update_status(classify_pty_status(self._tail))

    def _update_status(self, new: str) -> None:
        """Debounce: a new status must persist >=2 reader ticks before it
        flips (spinners momentarily clear the line and would otherwise flicker
        Idle<->Busy). Busy is reported immediately (responsiveness), the flip
        OUT of Busy is what we debounce."""
        if new == self._status:
            self._pending_status = None
            self._pending_ticks = 0
            return
        if new == "busy":
            self._set_status("busy")
            return
        # leaving busy / changing among waiting/idle: require persistence
        if new == self._pending_status:
            self._pending_ticks += 1
        else:
            self._pending_status = new
            self._pending_ticks = 1
        if self._pending_ticks >= 2:
            self._set_status(new)
            self._pending_status = None
            self._pending_ticks = 0

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

    def kill(self) -> None:
        """Stop the reader and kill the child PROCESS TREE.

        Order matters: close()/terminate() calls cancel_io(), which UNBLOCKS the
        blocked reader read() fast (taskkill alone takes ~2s to EOF it). THEN
        taskkill /T reaps grandchildren (claude's node workers) that terminate()
        alone would orphan — the SIGHUP-emulation concern from commit 0fd9fcf.
        Idempotent."""
        self._stop.set()
        pty, pid = self._pty, self._pid
        self._pty = None
        if pty is not None:
            try:
                pty.close(force=True)   # → terminate() → cancel_io(): unblock reader fast
            except Exception:
                try:
                    pty.terminate(force=True)
                except Exception:
                    pass
        if sys.platform == "win32" and pid:
            try:
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(pid)],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                    timeout=5,
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
        self._status[sid] = status

    def status(self, sid: str) -> str:
        return self._status.get(sid, "")

    def statuses(self) -> dict[str, str]:
        return dict(self._status)

    def all_terms(self) -> list["ClaudeTerminal"]:
        return list(self._terms.values())

    def kill_all(self) -> None:
        for term in list(self._terms.values()):
            try:
                term.kill()
            except Exception:
                pass
        self._terms.clear()
        self._status.clear()


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
    """Build a TabPane label like '◐ recap' / '⏳ claude-md' / '✓ edge-auth'."""
    glyph = STATUS_GLYPH.get(status, "")
    name = (title or "claude")[:18]
    return f"{glyph} {name}".strip()
