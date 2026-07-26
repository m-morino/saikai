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
import uuid
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


# IME-anchor: re-anchor the hardware cursor to claude's prompt cell + show the
# native cursor there on Windows. This is a host-side IME anchor, not a child
# render cursor: main-screen Claude may hide/draw its own cursor while it still
# needs WT's native cursor at the prompt for CJK composition. Alt-screen UIs own
# cursor presentation, and repaint-driven cursor moves are debounced so agents
# redraws don't make WT chase transient positions. Set SAIKAI_IME_ANCHOR=0 to turn
# it off completely. (#ime-anchor-optout)
_IME_ANCHOR = str(os.environ.get("SAIKAI_IME_ANCHOR", "1")).strip().lower() not in (
    "0", "false", "no", "off")

# Opt-in raw-PTY capture: when SAIKAI_PTY_CAPTURE names a file, every decoded chunk
# the reader feeds is appended as repr() (escape sequences visible) — for diagnosing
# how a child renders, e.g. whether an agent TUI drives ?1049 alt-screen, ?2026
# synchronized output, or ?1000/?1006 mouse reporting (which terminal scrollback and
# saikai's pyte mirror handle differently). Off by default; debug only.
_PTY_CAPTURE = os.environ.get("SAIKAI_PTY_CAPTURE", "").strip()

# Opt-in IME-anchor tracing: when SAIKAI_IME_DEBUG names a file (or is "1"), every
# _sync_terminal_cursor writes one line with the pyte cursor cell, the pyte screen
# size, the widget content_region, the computed anchor xy, the sync reason, and
# whether the anchor actually moved since the last flush. For diagnosing candidate-
# window misplacement (geometry mismatch vs a stale, never-flushed anchor) on a real
# WT + IME without guessing. Off by default; debug only.
_IME_DEBUG = os.environ.get("SAIKAI_IME_DEBUG", "").strip()
if _IME_DEBUG == "1":
    _IME_DEBUG = os.path.join(
        os.environ.get("TEMP") or os.environ.get("TMP") or ".", "saikai_ime_debug.txt")


def _ime_dbg(line: str) -> None:
    """Append one IME-anchor trace line (no-op unless SAIKAI_IME_DEBUG is set)."""
    if not _IME_DEBUG:
        return
    try:
        with open(_IME_DEBUG, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:
        pass

# Reader-side re-classify throttle (#agent-storm-throttle): while a pane is stably
# 'busy', an agent-mode spinner emits ~170k synchronized frames/session and
# re-classifying each (a full pyte-grid render ~0.7ms + the classifier regex
# ~0.2ms) burned ~150s of CPU only to re-confirm 'busy'. Throttle the busy-storm
# re-classify to this cadence. A flip INTO busy is never throttled (status != busy
# skips the gate), and the flip OUT of busy is caught by the host refresh_status
# poll (which fires even with no reader tick), so no state transition is lost.
_CLASSIFY_MIN_INTERVAL = 0.1


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


def _native_cursor_should_show(cursor_hidden: bool, in_alt_screen: bool) -> bool:
    """Native-cursor / IME-anchor policy: follow the child's DECTCEM state faithfully.

    The hardware cursor is the IME anchor — the host terminal parks its composition
    window wherever this cursor sits. Anchor it at the child's cursor cell whenever the
    child SHOWS its cursor (?25h), and refuse it only when the child HIDES it (?25l),
    on EITHER screen. A visible cursor is the text insertion point (that is exactly
    where composition belongs); a hidden cursor means the child owns presentation and
    has no insertion point (a pager / spinner / no-cursor TUI mode).

    This is screen-agnostic on purpose. claude's agent / fullscreen renderer runs on
    the ALT screen while KEEPING its prompt cursor VISIBLE — it still needs the IME
    there — so gating on alt-screen (the old policy) wrongly refused to anchor and the
    composition fell back to the pane top-left. Conversely a main-screen program that
    hides its cursor for a progress spinner must NOT have saikai force a cursor back
    on. cursor_hidden is the correct signal for both. in_alt_screen is retained in the
    signature for callers/tests but is not needed for the decision. (#agents-cursor)
    """
    del in_alt_screen
    return not cursor_hidden


_HOST_TERMINAL_ENV_STRIP = {
    # A pane child renders into saikai's pyte/Textual virtual terminal, not
    # directly into the outer emulator. If these leak through, Claude Code can
    # take host-specific paths such as WT full repaint / terminal private
    # protocols that are correct for direct stdout but wrong behind saikai.
    #
    # WT_SESSION is handled by _child_pty_env, not stripped here: on Windows the
    # pane PRESENTS itself as Windows Terminal (see the byte-identical Primary DA
    # reply), so the env must agree. (#wt-session)
    "TERM_PROGRAM",
    "TERM_PROGRAM_VERSION",
    "LC_TERMINAL",
    "LC_TERMINAL_VERSION",
    "KITTY_WINDOW_ID",
    "ALACRITTY_LOG",
    "KONSOLE_VERSION",
    "VTE_VERSION",
    "ZED_TERM",
    # Claude sets this for Windows/WT fleet views; saikai's pane is neither.
    # NOTE: CLAUDE_CODE_FORCE_SYNC_OUTPUT is deliberately NOT stripped — an
    # explicit user override stays explicit, and saikai now presents ?2026 frames
    # atomically instead of needing the child to avoid them.
    "CLAUDE_CODE_ALT_SCREEN_FULL_REPAINT",
}
# The whole WEZTERM_* family, not a hand-listed pair: WEZTERM_UNIX_SOCKET is a live
# mux socket, so leaking it lets a pane child drive the REAL outer window through
# `wezterm cli`, and WEZTERM_CONFIG_* re-identifies the host we just masked.
_HOST_TERMINAL_ENV_STRIP_PREFIXES = ("WEZTERM_",)

# Opt out of presenting the pane as Windows Terminal (env side): SAIKAI_WT_IDENTITY=0
# leaves WT_SESSION exactly as the host set it. (#wt-session-optout)
_WT_IDENTITY = str(os.environ.get("SAIKAI_WT_IDENTITY", "1")).strip().lower() not in (
    "0", "false", "no", "off")
# One synthesized session id per saikai PROCESS: real WT gives every pane in a
# window the same WT_SESSION, and a per-pane id would look like N terminals.
_WT_SESSION_SYNTH = ""


def _wt_session_id() -> str:
    """Stable synthetic WT session id for this saikai process."""
    global _WT_SESSION_SYNTH
    if not _WT_SESSION_SYNTH:
        _WT_SESSION_SYNTH = str(uuid.uuid4())
    return _WT_SESSION_SYNTH


def _child_pty_env(base_env, is_win: Optional[bool] = None) -> dict:
    """Environment advertised by saikai's PTY renderer to the child.

    The child talks to saikai's virtual terminal. Keep the capability contract
    explicit and deterministic instead of inheriting the outer terminal's brand
    probes (TERM_PROGRAM, WEZTERM_*, KITTY_WINDOW_ID, …).

    WT_SESSION is the one identity saikai ASSERTS rather than inherits, because
    the pane emulates Windows Terminal (the Primary DA reply is byte-identical).
    MEASURED on-device: with WT_SESSION present Claude tracks the input caret with
    the terminal cursor — which is what the IME anchor follows; without it Claude
    PARKS the cursor at a fixed base cell and the anchor pins composition there.
    Inheriting it made that a lottery on the outer host (broken under WezTerm and
    conhost), so synthesize one on Windows. On POSIX the anchor is _IS_WIN-gated,
    so a WT identity buys nothing and WSL — where Windows Terminal exports
    WT_SESSION into Linux — would take WT redraw paths behind pyte for free.
    (#agents-cursor #wt-session #wsl)"""
    env = dict(base_env)
    for key in _HOST_TERMINAL_ENV_STRIP:
        env.pop(key, None)
    for key in [k for k in env if k.startswith(_HOST_TERMINAL_ENV_STRIP_PREFIXES)]:
        env.pop(key, None)
    if _WT_IDENTITY:
        if _IS_WIN if is_win is None else is_win:
            env.setdefault("WT_SESSION", _wt_session_id())
        else:
            env.pop("WT_SESSION", None)
    env["TERM"] = "xterm-256color"
    env["COLORTERM"] = "truecolor"
    return env


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

            _bell_rang = False

            def bell(self, *a) -> None:
                # pyte calls this only for a REAL BEL (it consumes an OSC's
                # terminator BEL as part of the OSC, so this can't false-fire on a
                # title/clipboard write). AgentTerminal._consume drains the flag and
                # rings the host bell — claude's notification-fallback is a BEL, since
                # saikai isn't a recognised rich-notification terminal. (#bell)
                self._bell_rang = True

        _HistoryScreenBase = _SaikaiHistoryScreen
    except Exception:
        pass
except Exception:  # pragma: no cover - exercised only when dep absent
    pyte = None  # type: ignore
    _HistoryScreenBase = None  # type: ignore

# Origin mode (DECOM) marker as pyte stores it in Screen.mode — a cursor report is
# margin-relative while it is set. None when pyte is absent. (#term-queries)
try:
    from pyte.modes import DECOM as _PYTE_DECOM  # type: ignore
except Exception:  # pragma: no cover - exercised only when dep absent
    _PYTE_DECOM = None  # type: ignore

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
# DEC private-mode set/reset. ONE regex over the whole param list so a child that
# COMBINES params (e.g. \x1b[?1002;1006h) is parsed — a per-mode regex misses that
# form. We act on the mouse-tracking + SGR-encoding params; others are ignored here
# (bracketed paste / sync-update keep their own trackers below). (#faithful-mouse)
_DEC_PRIVATE_RE = re.compile(r"\x1b\[\?([0-9;]+)([hl])")
# OSC 52 clipboard WRITE from the child (\x1b]52;<sel>;<base64>\x07 or …ST). claude's
# fullscreen renderer copies a mouse selection this way; saikai consumes the child's
# output (the real terminal never sees it) and pyte ignores OSC 52, so without this
# the copy never reaches the host clipboard. base64 group is empty for a "?" (read)
# query, which we ignore. (#osc52-clipboard)
_OSC52_RE = re.compile(r"\x1b\]52;[^;]*;([A-Za-z0-9+/=]*)(?:\x07|\x1b\\)")
# Terminal QUERIES the child sends that expect a written reply (it queries saikai —
# which sits between it and the real terminal — not WT; pyte computes some replies
# but routes them to a no-op). Unanswered, claude's startup capability handshake
# (Primary-DA sentinel, no local timeout) silently disables rich features (OSC 8 /
# 133 / notifications / theme / synchronized output) and its alt-screen redraw probe
# (private ?6n) can block. See _answer_queries. (#term-queries)
_DA_RE = re.compile(r"\x1b\[0?c")                        # Primary Device Attributes
_DA2_RE = re.compile(r"\x1b\[>0?c")                      # Secondary DA (vim t_RV, tmux)
_DSR_RE = re.compile(r"\x1b\[(\??)([56])n")             # DSR: 5=status, 6=cursor position
_DECRQM_RE = re.compile(r"\x1b\[\?(\d+)\$p")            # DECRQM (mode support query)
_XTVERSION_RE = re.compile(r"\x1b\[>0?q")               # XTVERSION (terminal name/version)
_OSC_COLOR_Q_RE = re.compile(r"\x1b\](1[01]);\?(\x07|\x1b\\)")  # OSC 10/11 fg/bg color query
# Queries stripped from the mirror pane stream (#pane-direct): saikai (the PTY
# owner) answers them in _answer_queries; the browser xterm fed the raw stream
# would ALSO auto-answer via onData, and with pane-view input wired the child
# would receive every reply twice (a duplicated cursor-position report confuses
# claude's redraw probe). Built as the UNION of the named request regexes above
# — never hand-transcribed, so extending one of them extends the strip too —
# plus the query shapes xterm.js answers that saikai deliberately ignores:
# secondary/tertiary DA (vim's t_RV, tmux) and the DCS queries DECRQSS/XTGETTCAP
# (browser replies would be foreign dialect the child never negotiated with
# saikai). Applied on the mirror hub's DRAIN thread via set_pane_strip — not on
# the reader thread under the terminal lock.
_MIRROR_QUERY_STRIP_RE = re.compile("|".join(
    [p.pattern for p in (_DA_RE, _DA2_RE, _DSR_RE, _DECRQM_RE, _XTVERSION_RE,
                         _OSC_COLOR_Q_RE)]
    + [r"\x1b\[=0?c",                       # tertiary DA
       r"\x1bP\$q[^\x07\x1b]*(?:\x07|\x1b\\)",   # DECRQSS
       r"\x1bP\+q[0-9a-fA-F;]*(?:\x07|\x1b\\)"]  # XTGETTCAP
))
# Desktop notifications the child may emit. claude usually falls back to a BEL in
# saikai (it isn't a recognised rich-notification terminal), but honour these too.
_OSC9_NOTIFY_RE = re.compile(r"\x1b\]9;(?!4;)([^\x07\x1b]*)\x07")       # iTerm2 (not 9;4 progress)
_OSC777_RE = re.compile(r"\x1b\]777;notify;([^\x07]*)\x07")            # ghostty: title;body
_OSC99_RE = re.compile(r"\x1b\]99;[^;]*;([^\x1b\x07]*)(?:\x07|\x1b\\)") # kitty: metadata;payload
# Synchronized output (DEC mode 2026, BSU/ESU): retain a bracketed frame before
# pyte so neither Textual nor the IME anchor can observe its cursor-hidden/Home
# intermediate state. pyte ignores ?2026; the private stager below supplies the
# presentation boundary and fails open on bounded exceptional paths.
_SYNC_BUFFER_MAX_CHARS = 4 * 1024 * 1024
_SYNC_BUFFER_MAX_AGE = 0.2
# How long a ?25l may look like a mid-redraw blink before the native cursor is
# actually hidden. Above the stager's max age so a fail-open frame's transient
# hidden state can't trip it. (#ime-midframe)
_NATIVE_CURSOR_HIDE_SETTLE = 0.5
# How long a pane counts as "delivering atomic frames" after its last clean frame
# close, and how long a fail-open counts as "mid-tear". Both feed the IME anchor's
# freeze decision, and neither may latch for the pane's lifetime. (#ime-midframe)
_SYNC_ATOMIC_TTL = 2.0
_SYNC_BYPASS_TTL = 2.0
# Above this a PTY write is handed to the pane's writer thread instead of running on
# the UI thread, where a full pty input queue would freeze the event loop. Keystrokes,
# mouse reports and query replies are far below it and stay inline. (#paste-block)
_PTY_INLINE_WRITE_MAX = 4096
# Ceiling on writes waiting for that thread. A child that stops reading stdin for
# good would otherwise turn the queue into an unbounded buffer that also swallows
# every later keystroke. Far above any real typing or paste. (#paste-block)
_PTY_WRITE_QUEUE_MAX = 4 * 1024 * 1024
# Reader buffer. ptyprocess defaults read() to 1024 bytes, which turns a big turn
# into ~1000 wakeups per MB, each paying the whole per-chunk pipeline. winpty
# accepts the same argument. (#linux-read-size)
_PTY_READ_SIZE = 65536


# Private modes saikai tracks, mapped to the attribute holding their current state,
# for DECRQM. ?25 / ?2026 / the alt-screen family are derived instead. (#term-queries)
_DECRQM_TRACKED = {
    "1": "_app_cursor",              # DECCKM (application cursor keys)
    "1000": "_mouse_click",
    "1002": "_mouse_btn_motion",
    "1003": "_mouse_any_motion",
    "1006": "_mouse_sgr",
    "2004": "_bracketed_paste",
}
_DECRQM_ALT_SCREEN = ("47", "1047", "1049")


# DCS strings (ESC P … ST): sixel images, DECRQSS/XTGETTCAP replies. pyte has no
# DCS handler and DRAWS the payload body into the grid, and the pane advertises
# sixel in its Windows-Terminal DA reply, so an auto-detecting image tool would
# fill the pane with garbage. Strip them before pyte and the mirror. (#dcs-scrub)
_DCS_END_RE = re.compile(r"\x07|\x1b\\")
_DCS_MAX_DROP = 4 * 1024 * 1024


def _strip_dcs(text: str, inside: bool = False, dropped: int = 0):
    """Remove DCS strings from *text*, carrying the 'inside a DCS' state.

    Returns (clean_text, inside, dropped). A payload split across PTY reads keeps
    being dropped until its terminator arrives; past _DCS_MAX_DROP the strip gives
    up and passes text through, so a malformed stream degrades to garbage rather
    than blackholing the pane forever."""
    out = []
    pos = 0
    while pos < len(text):
        if inside:
            match = _DCS_END_RE.search(text, pos)
            if match is None:
                dropped += len(text) - pos
                if dropped > _DCS_MAX_DROP:
                    out.append(text[pos:])
                    inside, dropped = False, 0
                return "".join(out), inside, dropped
            inside, dropped = False, 0
            pos = match.end()
            continue
        start = text.find("\x1bP", pos)
        if start < 0:
            out.append(text[pos:])
            break
        out.append(text[pos:start])
        inside = True
        pos = start + 2
    return "".join(out), inside, dropped


# Queries whose answer depends on state the SAME chunk may still change, so they are
# answered at their own stream position rather than up front: cursor position (DSR 6)
# and mode state (DECRQM). Groups: 1-2 = DSR privacy/kind, 3 = DECRQM mode.
_POSITIONAL_QUERY_RE = re.compile("|".join((_DSR_RE.pattern, _DECRQM_RE.pattern)))


def _has_positional_query(text: str) -> bool:
    """True if *text* holds a query that must be answered at its stream position.

    A child can block on one of these before it emits ?2026l, so a retained frame
    holding one has to fail open rather than deadlock."""
    for match in _POSITIONAL_QUERY_RE.finditer(text):
        if match.group(3) is not None or match.group(2) == "6":
            return True
    return False


class _SynchronizedOutputStager:
    """Hold DEC 2026 output until a complete frame is available."""

    def __init__(self, max_chars=_SYNC_BUFFER_MAX_CHARS,
                 max_age=_SYNC_BUFFER_MAX_AGE):
        self.max_chars = int(max_chars)
        self.max_age = float(max_age)
        self._state = "outside"
        self._parts = []
        self._chars = 0
        self._opened_at = 0.0
        self._complete_frames = 0
        self._last_frame_at = 0.0    # monotonic ts of the last CLEAN frame close
        self._bypass_at = 0.0        # monotonic ts the current fail-open started
        self._now = 0.0              # clock of the push/flush in progress
        self._cursor_query = False

    @property
    def active(self):
        return self._state == "staging"

    def torn_at(self, now):
        """True while a fail-open is in flight: a partial frame already reached pyte
        and the rest streams through until the block closes, so pyte's cursor can be
        a mid-frame intermediate. Ages out — the closing ?2026l may never arrive, and
        a permanently 'torn' pane would freeze the IME anchor forever."""
        if self._state != "bypass":
            return False
        return (now - self._bypass_at) <= _SYNC_BYPASS_TTL

    def atomic_at(self, now):
        """True when pyte only ever holds frame-FINAL state: the child is bracketing
        its frames right now and no fail-open is in flight. The IME anchor uses this
        to keep tracking the caret through an output storm.

        Recency matters: 'this pane closed a frame once' would keep the anti-fly
        freeze disabled for the pane's whole life, including for later unbracketed
        output that really does leave pyte mid-frame."""
        if self._state == "bypass":
            return False
        if self._state == "staging":
            return True                 # pyte still holds the last complete frame
        return bool(self._last_frame_at) and (now - self._last_frame_at) <= _SYNC_ATOMIC_TTL

    @property
    def pending_query(self):
        """True when the RETAINED text holds a position-dependent query (DSR-6 or
        DECRQM). The child may be waiting for that reply before it emits ?2026l, so
        the frame has to fail open — but only for a query we are actually holding: one
        that arrived before the block opened is answerable from the plain prefix and
        must not tear the frame."""
        return self._cursor_query

    @property
    def in_block(self):
        """True while the child is inside a BSU/ESU pair, whether we are still
        holding the frame or streaming it through after a fail-open. This is what
        DECRQM ?2026 must report — the child set the mode either way."""
        return self._state in ("staging", "bypass")

    @staticmethod
    def _is_sync(match):
        return "2026" in match.group(1).split(";")

    def _start(self, marker, now):
        self._state = "staging"
        self._parts = [marker]
        self._chars = len(marker)
        self._opened_at = now
        self._cursor_query = False

    def _append(self, text):
        if text:
            self._parts.append(text)
            self._chars += len(text)
            if not self._cursor_query and _has_positional_query(text):
                self._cursor_query = True

    def _release(self, reason=None, bypass=False):
        text = "".join(self._parts)
        self._parts = []
        self._chars = 0
        self._opened_at = 0.0
        self._cursor_query = False
        self._state = "bypass" if bypass else "outside"
        if bypass:
            self._bypass_at = self._now
        if reason is None and text:
            self._complete_frames += 1
            self._last_frame_at = self._now
        return (text, reason) if text else None

    def flush(self, reason, now=None):
        if not self.active:
            return []
        self._now = time.monotonic() if now is None else float(now)
        unit = self._release(reason, bypass=True)
        return [unit] if unit else []

    def push(self, chunk, now=None):
        now = self._now = time.monotonic() if now is None else float(now)
        out = []
        if self.active and now - self._opened_at > self.max_age:
            out.extend(self.flush("timeout", now=now))

        pos = 0
        plain = []

        def emit_plain():
            if plain:
                text = "".join(plain)
                if text:
                    out.append((text, None))
                plain.clear()

        for match in _DEC_PRIVATE_RE.finditer(chunk):
            if not self._is_sync(match):
                continue
            before = chunk[pos:match.start()]
            marker = match.group(0)
            mode = match.group(2)
            if self._state == "staging":
                self._append(before + marker)
                if mode == "l":
                    unit = self._release()
                    if unit:
                        out.append(unit)
            else:
                plain.append(before)
                if mode == "h":
                    # Start staging — including from a fail-open bypass. If the child
                    # is merely re-setting a still-open block the remainder is still
                    # released at the real close; if it ABANDONED that block (which is
                    # why the fail-open happened) the new frame gets staged properly
                    # instead of streaming through torn until some later ?2026l.
                    emit_plain()
                    self._start(marker, now)
                else:
                    plain.append(marker)     # closing/stray ESU: pass it through
                    if self._state == "bypass":
                        self._state = "outside"
            pos = match.end()

        tail = chunk[pos:]
        if self._state == "staging":
            self._append(tail)
            if self._chars > self.max_chars:
                out.extend(self.flush("overflow", now=now))
        else:
            plain.append(tail)
        emit_plain()
        return out
# Embedded paste markers in text we are about to wrap in bracketed paste: an
# embedded ESC[201~ would END paste mode early so the bytes after it run as
# typed-and-submitted input (the classic bracketed-paste breakout). Strip both
# markers from the content first, exactly as real terminals sanitize a paste.
_PASTE_MARKER_RE = re.compile(r"\x1b\[20[01]~")
_PASTE_MARKERS = ("\x1b[200~", "\x1b[201~")


def _normalize_paste_newlines(text: str) -> str:
    """Collapse CRLF to LF in pasted text. A Windows clipboard (Notepad, a CRLF
    file, browser text) delivers '\\r\\n' per line; forwarded verbatim into the
    PTY the child's readline sees CR *and* LF for every line and submits/blanks
    twice ('double-enter'). Real terminals strip the CR before delivering a paste.
    Lone '\\r' is left alone (rare, and may be intentional)."""
    return text.replace("\r\n", "\n")


def _strip_paste_markers(text: str) -> str:
    """Strip embedded paste markers so none survives, in ONE scan.

    Overlapping fragments ("\\x1b[20" + "\\x1b[201~" + "1~") lose the inner marker to
    a plain sub() and let the surviving halves concatenate into a fresh marker at the
    deletion seam — the breakout the strip exists to block. Re-running sub() until
    the text stops changing closes that seam but removes only one marker per pass for
    a nested chain, which is quadratic: a 240KB crafted clipboard took ~7s on the UI
    thread inside on_paste. Emit character by character instead and drop a marker the
    moment the tail completes one, so a seam is caught by the same scan. Output can
    never contain a marker, so no second pass is needed. (#H3)"""
    if "\x1b[20" not in text:                # overwhelmingly the common case
        return text
    out: list = []
    for ch in text:
        out.append(ch)
        if ch == "~" and len(out) >= 6 and "".join(out[-6:]) in _PASTE_MARKERS:
            del out[-6:]
    return "".join(out)


def _wrap_bracketed_paste(text: str) -> str:
    """Wrap text in bracketed-paste markers after stripping any embedded ones."""
    return "\x1b[200~" + _strip_paste_markers(text) + "\x1b[201~"


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


# Mirror clipboard relay (#app-native-select): the app sets this to the mirror
# hub's send_clip at mount, so a child's OSC 52 copy (claude's copy-selection)
# reaches the browsers too. Module-level (one app per process); None = no mirror.
MIRROR_CLIP = None


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
    # Opt OUT of Textual's app-level (drag) text selection: this pane forwards mouse
    # events to the child (which runs its OWN selection/scroll when it enables mouse
    # tracking), so Textual must not also try to select over it. saikai's own
    # Shift+drag copy still works — it's a custom handler, not Textual's selection.
    ALLOW_SELECT = False
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
        self._scroll = 0             # lines scrolled back (0 = live bottom)
        self._frozen = False         # paused repaint: hold the view still so a
                                     # streaming pane can be drag-selected
        self._sel_anchor = None      # (row,col) drag start — saikai-OWNED selection
        self._sel_head = None        # (row,col) drag head; None ⇒ no selection
        self._pending_anchor = None  # (row,col) of a press awaiting a drag; a click that never drags stays pending → no freeze/capture (#click-no-freeze)
        # Child mouse-tracking state (parsed from the child's DEC private-mode sets).
        # A faithful terminal forwards mouse events to the child per these; see
        # _forward_mouse. _mouse_reporting = any tracking on; the three flags below
        # distinguish click-only (?1000) vs button-drag motion (?1002) vs any motion
        # (?1003) so we forward motion only when the child asked for it.
        self._mouse_reporting = False
        self._mouse_sgr = False        # ?1006 SGR extended encoding negotiated
        self._mouse_click = False      # ?1000 press/release
        self._mouse_btn_motion = False # ?1002 motion while a button is held (drag)
        self._mouse_any_motion = False # ?1003 motion always (hover)
        self._focus_reporting = False  # ?1004: child wants \x1b[I / \x1b[O on focus change
        self._fwd_buttons = set()      # forwarded buttons currently held (a drag in progress)
        self._fwd_captured = False     # captured the mouse for the current forwarded gesture?
        self._fwd_last = (1, 1)        # last forwarded (col,row) — for a synthetic release
        self._autoscroll_dir = 0     # drag at top/bottom edge: +1 up / -1 down / 0
        self._autoscroll_timer = None  # ticks while edge-dragging (#drag-autoscroll)
        self._sel_prev_frozen = False
        self._frozen_buf = None      # snapshot of the displayed rows while frozen
                                     # (the reader keeps mutating screen.buffer, so
                                     # render + copy must read the FROZEN frame)
        self._esc_carry = ""         # trailing partial escape held across read()s
        self._dcs_inside = False     # mid-DCS across read()s (sixel payload scrub)
        self._dcs_dropped = 0        # chars swallowed by the current DCS (runaway cap)
        self._write_lock = threading.Lock()  # guards the PTY write queue below
        self._write_q: list = []     # oversized writes waiting for the writer thread
        self._writer = None          # daemon draining _write_q in order (#paste-block)
        self._sync_output = _SynchronizedOutputStager()
        self._osc52_carry = ""       # partial OSC 52 clipboard write held across read()s (base64 can span chunks)
        self._app_cursor = False     # ?1 DECCKM — replayed in the mirror seed so a
                                     # pane-view browser encodes arrows correctly (#pane-direct)
        self._hw_cursor_visible: Optional[bool] = None  # last ?25 visibility we wrote
        self._anchored_xy = None  # last IME anchor cell we set (freeze/flush bookkeeping)
        self._cursor_hidden_since = 0.0  # monotonic ts the child hid its cursor (?25l settle)
        # Mirror pane-direct tee (#pane-direct): tee(str) forwards a scrubbed
        # chunk to the mirror hub's pane channel; reset(str) enqueues a full-
        # state seed; synth(screen, cols, rows, modes) serializes one. All three
        # are set/cleared together under _lock so seed and stream stay ordered.
        self._mirror_tee = None
        self._mirror_reset = None
        self._mirror_synth = None
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
        # monotonic ts of the last USER input written to this pane (keys, paste,
        # mirror-injected bytes). The host's list-rebuild deferral keys off
        # "typing recently", not "pane focused": parking focus in a pane while
        # WATCHING the list froze the State groups on quiet POSIX ptys, where
        # the final busy→idle tick comes from the UI-thread poll and never the
        # reader. (#linux-state-regroup)
        self.last_input_ts = 0.0

    # ── geometry helpers ──────────────────────────────────────────────────────
    def _dims(self) -> tuple[int, int]:
        """Current (rows, cols). When the widget has NO real size yet — an inactive
        TabbedContent pane (ContentSwitcher sets display:none → size 0) or pre-layout
        — fall back to 80x24 instead of the old 2x2 floor: a child spawned into a 2x2
        PTY can't render its UI, so its trust-gate / prompts never appear and the
        status classifier can't see "waiting" until the tab is activated and resized
        (the "restored pane isn't flagged needs-input until I select it" bug). 80x24
        lets the child render + be classified while still backgrounded; on_resize
        corrects to the exact size when the tab is shown. (#inactive-pane-size)"""
        w = int(self.size.width or 0)
        h = int(self.size.height or 0)
        if w < 8 or h < 4:                       # inactive/hidden pane or pre-layout
            w = w if w >= 8 else 80
            h = h if h >= 4 else 24
        return max(h, 2), max(w, 2)

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
        env = _child_pty_env(base_env)
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
        show_cursor = (s == 0 and self.has_focus and y == cursor_y
                       and not self.is_dead and not cursor_hidden)
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
            if show_cursor and x == cursor_x and not (_IS_WIN and _IME_ANCHOR):
                # Draw saikai's own cursor (cell reversed, keeping the cell's real
                # fg/bg/bold so a themed prompt isn't flattened). SKIP only on Windows
                # WHEN the IME anchor is on: there _show_hw_cursor shows the terminal's
                # NATIVE cursor (thin bar) instead, and drawing here too would stack a
                # wide reverse-block on it. With the anchor OFF the native cursor is
                # never shown, so we MUST draw here — else a Windows classic-renderer
                # pane has NO caret at all (the default-OFF regression). (#native-cursor)
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
        self.last_input_ts = time.monotonic()   # (#linux-state-regroup)
        self._snap_to_live()   # typing returns the view to the live bottom
        try:
            self._write_child(data)
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
                self._write_child(text)
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
        self.last_input_ts = time.monotonic()    # (#linux-state-regroup)
        self._snap_to_live()   # injected input returns the view to the live bottom
        try:
            self._write_child(text)
        except Exception:
            pass

    def submit(self) -> None:
        """Send a single Enter (\\r) to submit the current input. UI-thread only."""
        if self._pty is None or self.is_dead:
            return
        self._snap_to_live()   # submitting returns the view to the live bottom
        try:
            self._write_child("\r")
        except Exception:
            pass

    def kill_input_line(self) -> None:
        """Send Ctrl+U to clear the child's input line before an injection.
        A leftover draft the user typed while idle would otherwise CONCATENATE
        with an injected prompt — and a "draft/clear" no longer starts with '/'
        so it submits as a garbage MESSAGE instead of running the command.
        UI-thread only. (#audit-b2-draft)"""
        if self._pty is None or self.is_dead:
            return
        try:
            self._write_child("\x15")
        except Exception:
            pass

    # ── mouse -> child PTY (faithful terminal) ─────────────────────────────────
    def _mouse_seq(self, cb: int, col: int, row: int, final: str) -> str:
        """One mouse report in the negotiated encoding. SGR (?1006) has no coord
        limit. Legacy X10 caps col/row at 95: a cell byte is chr(32+n), and n >= 96
        yields >= U+0080, which pty.write expands to multi-byte UTF-8 and corrupts the
        fixed 6-byte X10 packet (the child then misreads the cell). X10 beyond 95 cells
        is unrepresentable through a str writer; modern children negotiate SGR. For X10
        the caller pre-encodes the button byte in ``cb`` (SGR uses ``final`` to tell
        press 'M' from release 'm'; X10 encodes a release as button 3)."""
        if getattr(self, "_mouse_sgr", False):
            return f"\x1b[<{cb};{col};{row}{final}"
        return ("\x1b[M" + chr(32 + cb)
                + chr(32 + min(col, 95)) + chr(32 + min(row, 95)))

    def _event_cell(self, event) -> tuple:
        """Widget-relative event coords → 1-based terminal (col, row), clamped to the
        grid so a drag past the edge still reports the edge cell (lets the child run
        its own autoscroll). Shared by _forward_wheel and _forward_mouse."""
        col = max(1, int(getattr(event, "x", 0)) + 1)
        row = max(1, int(getattr(event, "y", 0)) + 1)
        scr = getattr(self, "_screen", None)
        if scr is not None:
            try:
                col = min(col, int(scr.columns))
                row = min(row, int(scr.lines))
            except Exception:
                pass
        return col, row

    def _forward_wheel(self, event, up: bool) -> bool:
        """When the child enabled mouse reporting, send it a WHEEL event so a
        full-screen TUI scrolls its OWN view — instead of saikai's scrollback, which
        is empty in the alt-screen such a TUI runs in (so the wheel did nothing).
        Returns True if sent."""
        if not getattr(self, "_mouse_reporting", False) or self._pty is None or self.is_dead:
            return False
        try:
            col, row = self._event_cell(event)
            btn = 64 if up else 65                           # wheel: 64 = up, 65 = down
            self._write_child(self._mouse_seq(btn, col, row, "M"))
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
            back_at_live = moved and self._scroll == 0
        if moved:
            self.refresh()
        if back_at_live:
            # Wheeling back to live re-anchors even with no input and no output —
            # the scrolled-back sync hid the native cursor. (#ime-scrollback)
            self._sync_terminal_cursor(reason="focus")
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
            # Leaving scrollback must RE-ANCHOR: the sync hid the native cursor on the
            # way up, and a repaint-driven sync can't undo that on a quiet pane (the
            # reader only schedules repaints for new output). (#ime-scrollback)
            self._sync_terminal_cursor(reason="focus")

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
        console codepage), then OSC-52 via the app (Linux/remote terminals).

        Also relays to the MIRROR browsers (#app-native-select): claude itself
        does NOT track the mouse in its normal prompt, so the terminal owns
        selection AND copy — this pane's own drag-select copy is the ONLY copy a
        mirror viewer gets, so it must reach the device they're holding, not just
        the host. UI-thread only (both call sites already are)."""
        if not text:
            return
        hook = MIRROR_CLIP
        if hook is not None:
            try:
                hook(text)
            except Exception:
                pass
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

    def _forward_mouse(self, kind: str, event) -> None:
        """Encode a mouse event and write it to the child PTY, so a child that
        enabled mouse tracking (e.g. claude's fullscreen renderer) runs its OWN
        selection / drag-autoscroll — exactly what it gets under a native terminal.
        Inverts Textual's SGR decode (button = (cb+1)&3): cb = (button-1)&3, motion
        adds 32, shift/meta/ctrl add 4/8/16. SGR (?1006) when negotiated, else legacy
        X10. kind ∈ {down,up,move}. UI-thread only (like _forward_wheel; pty.write is
        non-blocking). (#faithful-mouse)"""
        if self._pty is None or self.is_dead:
            return
        try:
            # This event's OWN button — Textual sets it on down / up / drag-motion
            # (its parser decodes button=(cb+1)&3). Using the event (not a stored
            # drag button) keeps multi-button presses/releases correctly attributed.
            button = getattr(event, "button", 0) or 0
            base = ((button - 1) & 3) if button else 3   # 0/1/2 = L/M/R; no-button = 3
            motion = 32 if kind == "move" else 0
            mods = ((4 if getattr(event, "shift", False) else 0)
                    + (8 if getattr(event, "meta", False) else 0)
                    + (16 if getattr(event, "ctrl", False) else 0))
            col, row = self._event_cell(event)
            self._fwd_last = (col, row)                     # for a synthetic release on cancel
            if self._mouse_sgr:                             # SGR: real button + 'm' on release
                cb = base + motion + mods
                self._write_child(self._mouse_seq(cb, col, row, "m" if kind == "up" else "M"))
            else:                                           # X10: a release is button code 3
                lb = (3 if kind == "up" else base) + motion + mods
                self._write_child(self._mouse_seq(lb, col, row, "M"))
        except Exception:
            pass

    def _child_owns_mouse(self) -> bool:
        """True when the child enabled mouse tracking and can take events now."""
        return bool(self._mouse_reporting) and self._pty is not None and not self.is_dead

    def on_mouse_down(self, event) -> None:   # events.MouseDown
        if self._screen is None or self.is_dead:
            return
        # FAITHFUL TERMINAL: when the child tracks the mouse (its fullscreen renderer),
        # forward EVERY press + drag — incl. Shift — so the child runs its OWN
        # selection / drag-autoscroll (smarter: indent/word/line aware, OSC-52 copy).
        # saikai does NOT keep an in-pane selection here; the terminal-native escape
        # hatch is WT's own Shift+drag, which WT intercepts before Textual anyway.
        # saikai's freeze-select below only runs for a child that does NOT track the
        # mouse (the classic renderer / a plain shell). (#faithful-mouse)
        if self._child_owns_mouse():
            try:
                if not self.has_focus:        # own the mouse → own the keys too, but
                    self.focus()              # guard so a click on an already-focused
            except Exception:                 # pane can't churn focus (WT IME)
                pass
            self._fwd_buttons.add(getattr(event, "button", 1) or 1)
            self._forward_mouse("down", event)
            # Capture is DEFERRED to the first drag-move (on_mouse_move) so a bare
            # click never captures — avoids the per-click capture churn the
            # #click-no-freeze fix removed. (#faithful-mouse)
            try:
                event.stop()
            except Exception:
                pass
            return
        # saikai's own selection path (Shift+drag, or a child with no mouse tracking):
        # record a PENDING anchor only — a bare click just focuses the pane (no freeze
        # / capture), the drag engages on the first real move. (#click-no-freeze)
        if getattr(event, "button", 1) != 1:
            return
        self._pending_anchor = (event.y, event.x)
        self._sel_anchor = None

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
        # Forwarding a drag to the child? Relay motion so its selection/autoscroll
        # tracks — but only if the child asked for motion (?1002 button-drag or ?1003
        # any-motion). A ?1000-only child gets press/release only. (#faithful-mouse)
        if self._fwd_buttons:                  # a forwarded drag is active
            if self._mouse_any_motion or self._mouse_btn_motion:
                if not self._fwd_captured:     # capture on the FIRST real drag-move only
                    try:
                        self.capture_mouse()   # → moves keep coming after we leave the pane
                        self._fwd_captured = True
                    except Exception:
                        pass
                self._forward_mouse("move", event)
            try:
                event.stop()
            except Exception:
                pass
            return
        # Hover motion (no button held): forward if the child asked for ANY-motion
        # tracking (?1003) — e.g. hover menus / mouseover highlight. (#faithful-mouse)
        if self._mouse_any_motion and self._child_owns_mouse():
            self._forward_mouse("move", event)
            try:
                event.stop()
            except Exception:
                pass
            return
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
        # End a forwarded drag: relay the release + free the mouse capture. Skip the
        # release write if the child turned tracking OFF mid-drag (else it gets a
        # stray escape it no longer expects), but ALWAYS drop the capture/state.
        # (#faithful-mouse)
        if self._fwd_buttons:
            if self._child_owns_mouse():
                self._forward_mouse("up", event)   # event.button = the released button
            btn = getattr(event, "button", 0) or 0
            if btn:
                self._fwd_buttons.discard(btn)
            else:
                self._fwd_buttons.clear()          # unknown button → end the whole gesture
            if not self._fwd_buttons:              # all buttons up → drop the capture
                self._fwd_captured = False
                try:
                    self.release_mouse()
                except Exception:
                    pass
            try:
                event.stop()
            except Exception:
                pass
            return
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
    def _flush_sync_output(self, reason: str) -> bool:
        """Release one retained frame on the reader thread, at most once."""
        changed = False
        sync_output = getattr(self, "_sync_output", None)
        if sync_output is None:
            return False
        for text, fail_reason in sync_output.flush(reason):
            if fail_reason:
                _log(f"sync-output fail-open: reason={fail_reason} chars={len(text)}")
            self._consume_ready(text)
            changed = True
        return changed

    def _read_loop(self) -> None:
        pty = self._pty
        assert pty is not None
        try:
            while not self._stop.is_set():
                try:
                    # Ask for a real buffer: ptyprocess defaults to 1024 bytes, so a
                    # multi-megabyte turn woke this loop ~1000 times per MB and paid
                    # the whole per-chunk pipeline each time. (#linux-read-size)
                    chunk = pty.read(_PTY_READ_SIZE)   # blocking; str on winpty
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
                changed = self._consume(chunk)
                # NEVER touch the UI from this thread — marshal a COALESCED
                # repaint so a fast stream of small chunks can't flood the UI.
                # While scrolled back (copy mode) the pinned view shows the SAME
                # history lines regardless of new output (_consume keeps the pin
                # by bumping _scroll), so the repaint would rewrite identical cells
                # for nothing AND clear a WezTerm Shift+drag selection. Skip it —
                # scrolling up thus "freezes" the pane so the user can select/copy;
                # scrolling back to the bottom (_scroll == 0) resumes live repaint.
                # A retained synchronized-output block returns False and has not
                # mutated pyte. Its close releases one complete presentation unit.
                if changed and self._scroll == 0 and not self._frozen:
                    self._schedule_pane_refresh()
        finally:
            # Guarded: the flush feeds pyte and then the status classifier — a
            # caller-supplied callable — so a raise here must not cost us
            # _finalize, which is what marks the pane dead. (#eof-flush)
            try:
                self._flush_sync_output("eof")
            except Exception:
                pass
            self._finalize()

    def _honor_osc52(self, b64: str) -> None:
        """Put an OSC 52 clipboard-write payload from the child onto the HOST
        clipboard (a fullscreen child that DOES track the mouse copies via
        OSC 52). Ignores a "?"/empty payload (a read query). Runs on the reader
        thread → marshals onto the UI thread. _copy_text relays to the mirror
        browsers too. (#osc52-clipboard)"""
        if not b64 or b64 == "?":
            return
        try:
            import base64
            text = base64.b64decode(b64, validate=False).decode("utf-8", "replace")
        except Exception:
            return
        if text:
            self._marshal(lambda t=text: self._copy_text(t))

    def _write_child(self, data: str) -> None:
        """Write to the PTY without ever blocking the UI thread on a full pty buffer.

        A POSIX pty input queue is a few KiB, so a large paste into a child that is
        momentarily not reading stdin blocks write() — and with it the whole Textual
        event loop. Oversized writes go to a daemon writer instead; while that queue
        drains every later write joins it, so the child still receives the bytes in
        order. Small writes (keystrokes, mouse reports, query replies) stay inline,
        which is the overwhelmingly common case. (#paste-block)"""
        if not data or self._pty is None or self.is_dead:
            return
        lock = getattr(self, "_write_lock", None)
        queue = getattr(self, "_write_q", None)
        if lock is not None and queue is not None:
            with lock:
                # Queue while a writer is ALIVE, not merely while the queue is
                # non-empty: it holds the popped item outside the queue for the whole
                # blocking write, and an inline write during that window would reach
                # the child first.
                if (queue or getattr(self, "_writer", None) is not None
                        or len(data) > _PTY_INLINE_WRITE_MAX):
                    if sum(len(part) for part in queue) + len(data) > _PTY_WRITE_QUEUE_MAX:
                        # The child has stopped reading stdin for good. Refuse rather
                        # than buffer without limit; the pane is already unusable and
                        # the queue would just grow until memory ran out.
                        _log(f"pty write dropped: queue full ({len(data)} chars)")
                        return
                    queue.append(data)
                    if getattr(self, "_writer", None) is None:
                        self._writer = threading.Thread(
                            target=self._writer_loop,
                            name=f"saikai-pty-write-{getattr(self, 'sid', '?')}",
                            daemon=True)
                        self._writer.start()
                    return
        try:
            self._pty.write(data)
        except Exception:
            pass

    def _writer_loop(self) -> None:
        """Drain queued PTY writes off the UI thread, in order. (#paste-block)"""
        while True:
            with self._write_lock:
                if not self._write_q:
                    self._writer = None
                    return
                data = self._write_q.pop(0)
            pty = self._pty
            if pty is None:
                continue
            try:
                pty.write(data)
            except Exception:
                pass

    def _send_to_child(self, data: str) -> None:
        """Write bytes to the child PTY (guarded). Called on the UI thread (via
        _marshal) so a query reply can't interleave a concurrent keystroke —
        _write_child keeps that ordering even when a paste is still draining."""
        if self._pty is None or self.is_dead:
            return
        try:
            self._write_child(data)
        except Exception:
            pass

    # ── Mirror pane-direct view (#pane-direct) ────────────────────────────────
    def attach_mirror(self, tee, reset, synth) -> None:
        """Start teeing this pane's scrubbed PTY stream to the mirror's pane
        channel. UI thread. The seed (current grid + cursor + terminal modes)
        is computed AND enqueued under _lock, and _consume tees under the same
        lock — so every chunk is either inside the seed or ordered after it,
        never both. tee/reset are hub enqueues (put_nowait, no marshal)."""
        with self._lock:
            self._mirror_tee = tee
            self._mirror_reset = reset
            self._mirror_synth = synth
            self._mirror_reseed_locked()

    def detach_mirror(self) -> None:
        """Stop teeing (pane lost focus / closed / app shutdown). UI thread."""
        with self._lock:
            self._mirror_tee = None
            self._mirror_reset = None
            self._mirror_synth = None

    def mirror_reseed(self) -> None:
        """Re-serialize full state into the pane channel — the hub asks for this
        when a browser joins mid-session, falls behind, or the ingest queue
        overflowed. UI thread (the hub's callback marshals here)."""
        with self._lock:
            self._mirror_reseed_locked()

    def _mirror_reseed_locked(self) -> None:
        reset, synth, scr = self._mirror_reset, self._mirror_synth, self._screen
        if reset is None or synth is None or scr is None:
            return
        modes = {
            "alt": self._alt.in_alt,
            "app_cursor": self._app_cursor,
            "mouse_click": self._mouse_click,
            "mouse_btn_motion": self._mouse_btn_motion,
            "mouse_any_motion": self._mouse_any_motion,
            "mouse_sgr": self._mouse_sgr,
            "focus_reporting": self._focus_reporting,
            "bracketed_paste": getattr(self, "_bracketed_paste", False),
            "cursor_hidden": bool(getattr(scr.cursor, "hidden", False)),
        }
        try:
            reset(synth(scr, scr.columns, scr.lines, modes))
        except Exception:
            pass

    def _ring_bell(self) -> None:
        """Ring the host terminal bell (UI thread)."""
        try:
            self.app.bell()
        except Exception:
            pass

    def _notify_host(self, msg: str) -> None:
        """Surface a child desktop-notification (OSC 9/777/99) as a saikai toast.
        Reader thread → marshal the toast onto the UI thread. (#osc-notify)"""
        msg = (msg or "").strip()
        if not msg:
            return
        self._marshal(lambda m=msg[:200]: self._safe_notify(m))

    def _safe_notify(self, msg: str) -> None:
        try:
            self.notify(msg, title="claude", timeout=6)
        except Exception:
            pass

    def _cursor_rowcol(self) -> tuple:
        """Current pyte cursor as 1-based (row, col), for a Cursor-Position reply.

        Two adjustments a raw +1 gets wrong. Text that exactly fills a row leaves
        pyte in the PENDING-WRAP state with cursor.x == columns, which would report
        a column that does not exist — real terminals report the last column until
        the wrap happens. And with origin mode (DECOM) set the report is relative to
        the scroll region, so an app with a region would otherwise be told it sits
        outside its own margins. (#term-queries)"""
        with self._lock:
            scr = self._screen
            if scr is None:
                return 1, 1
            try:
                row = int(scr.cursor.y) + 1
                col = int(scr.cursor.x) + 1
                cols = int(getattr(scr, "columns", 0) or 0)
                if cols:
                    col = min(col, cols)
                if _PYTE_DECOM is not None and _PYTE_DECOM in getattr(scr, "mode", ()):
                    margins = getattr(scr, "margins", None)
                    if margins is not None:
                        row = max(1, row - int(margins.top))
                return row, col
            except Exception:
                return 1, 1

    def _decrqm_report(self, mode: str) -> int:
        """DECRQM answer for a private *mode*: 1 = set, 2 = reset, 0 = not recognised.

        A pane that answers Primary DA as Windows Terminal must not report "not
        recognised" for modes it honours: a child using the set-then-verify pattern
        then refuses to enable bracketed paste (so a multi-line paste submits line by
        line) or SGR mouse encoding, even though saikai tracks both. (#term-queries)"""
        if mode == "2026":
            stager = getattr(self, "_sync_output", None)
            return 1 if (stager is not None and stager.in_block) else 2
        if mode in _DECRQM_ALT_SCREEN:
            return 1 if getattr(getattr(self, "_alt", None), "in_alt", False) else 2
        if mode == "25":                                    # DECTCEM
            try:
                with self._lock:
                    scr = self._screen
                    hidden = bool(getattr(scr.cursor, "hidden", False)) if scr is not None else False
            except Exception:
                return 0
            return 2 if hidden else 1
        attr = _DECRQM_TRACKED.get(mode)
        if attr is None:
            return 0
        return 1 if getattr(self, attr, False) else 2

    def _answer_static_queries(self, chunk: str) -> None:
        """Answer terminal queries whose reply does not depend on live state.

        Replies go out in the order the child ASKED, not in the order this method
        happens to check: a probe batch that ends with CSI c — "answer what you know,
        then this sentinel" — must not get the sentinel first. (#term-queries)"""
        out = []                      # (position in chunk, reply) — sorted before send
        _m = _DA_RE.search(chunk)
        if _m is not None:
            # Primary DA — reply BYTE-IDENTICAL to the outer Windows Terminal (probed
            # on-device: ESC[?61;...c, a VT500-class terminal with feature extensions).
            # The old minimal "?6c" (VT102) made Claude Code treat the pane as a basic
            # terminal and fall back to a renderer that parks the cursor at a base cell
            # instead of tracking the input caret. Looking exactly like WT (which Claude
            # tracks correctly when run directly) is the fix. (#agents-cursor #wt-da)
            out.append((_m.start(), "\x1b[?61;4;6;7;14;21;22;23;24;28;32;42;52c"))
        _m = _DA2_RE.search(chunk)
        if _m is not None:
            # Secondary DA. A terminal that answers DA1 as WT must answer this too:
            # vim's t_RV and tmux's handshake serialize DA1 then DA2, and silence
            # stalls them for a probe timeout and then mis-detects version-gated
            # features. WT's reply is "VT100, firmware 10, no cartridge". (#wt-da2)
            out.append((_m.start(), "\x1b[>0;10;1c"))
        for _m in _DSR_RE.finditer(chunk):
            if _m.group(2) == "5":
                out.append((_m.start(), "\x1b[0n"))      # device status: OK
        # DECRQM is NOT answered here: it reports live mode state, so it has to be
        # answered at its own stream position once the bytes before it reached pyte
        # (see _feed_presentation_unit). Everything above is state-free.
        # XTVERSION (ESC[>q): the outer Windows Terminal sends NO reply (probed on-
        # device). Answering with a name ("saikai") made Claude Code see an unknown
        # terminal and skip its WT cursor-tracking path. Stay silent, exactly like WT.
        # (#agents-cursor #wt-da)
        for _m in _OSC_COLOR_Q_RE.finditer(chunk):
            # Report a dark background (11) / light foreground (10) so the child picks
            # a dark theme, matching a typical terminal. Answer with the terminator the
            # child ASKED with: an ST-terminated query answered with BEL leaves a strict
            # parser waiting for its ST, and the stray BEL rings the bell.
            _code = _m.group(1)
            _rgb = "1e1e/1e1e/1e1e" if _code == "11" else "c0c0/c0c0/c0c0"
            out.append((_m.start(), f"\x1b]{_code};rgb:{_rgb}{_m.group(2)}"))
        if out:
            out.sort(key=lambda pair: pair[0])
            resp = "".join(reply for _pos, reply in out)
            self._marshal(lambda r=resp: self._send_to_child(r))

    def _answer_cursor_queries(self, chunk: str) -> None:
        """Answer cursor-position DSR after the relevant output reached pyte."""
        out = []
        for private, kind in _DSR_RE.findall(chunk):
            if kind == "6":
                row, col = self._cursor_rowcol()
                out.append(f"\x1b[{private}{row};{col}R")
        if out:
            response = "".join(out)
            self._marshal(lambda r=response: self._send_to_child(r))

    def _answer_mode_queries(self, chunk: str) -> None:
        """Answer DECRQM for a chunk that already reached pyte."""
        out = [f"\x1b[?{mode};{self._decrqm_report(mode)}$y"
               for mode in _DECRQM_RE.findall(chunk)]
        if out:
            response = "".join(out)
            self._marshal(lambda r=response: self._send_to_child(r))

    def _answer_queries(self, chunk: str) -> None:
        """Answer every query in *chunk* for a caller that already fed it to pyte.

        _consume does not use this: it answers the state-free queries up front (a
        child can block on them mid-frame) and the state-dependent ones at their own
        stream position."""
        self._answer_static_queries(chunk)
        self._answer_mode_queries(chunk)
        self._answer_cursor_queries(chunk)

    def _consume(self, chunk: str) -> bool:
        """Stage decoded output and feed only complete units to pyte."""
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
        # Drop DCS strings (sixel payloads, DECRQSS/XTGETTCAP replies) BEFORE any
        # other scan: pyte would print the body into the grid, and a payload must not
        # be able to spoof an OSC 52 clipboard write or a notification either. The
        # 'inside' state carries across reads. (#dcs-scrub)
        if "\x1bP" in chunk or getattr(self, "_dcs_inside", False):
            chunk, self._dcs_inside, self._dcs_dropped = _strip_dcs(
                chunk, getattr(self, "_dcs_inside", False),
                getattr(self, "_dcs_dropped", 0))
        chunk = _PRIVATE_SGR_RE.sub("", chunk)   # drop XTMODKEYS \x1b[>4;2m etc. (pyte misreads as SGR-4 underline)
        chunk = _KITTY_KBD_RE.sub("", chunk)     # drop Kitty-keyboard CSI-u (pyte leaks the trailing 'u' into the grid)
        _bp = _BRACKETED_RE.findall(chunk)       # track claude's bracketed-paste mode for on_paste (last h/l wins)
        if _bp:
            self._bracketed_paste = (_bp[-1] == "h")
        _dec = _DEC_PRIVATE_RE.findall(chunk)    # DEC private-mode sets (mouse / SGR / …)
        if _dec:
            for _params, _hl in _dec:
                _on = (_hl == "h")
                for _p in _params.split(";"):    # handle COMBINED params (?1002;1006h)
                    if _p == "1":
                        self._app_cursor = _on         # DECCKM (#pane-direct seed replay)
                    elif _p in ("1000", "1002", "1003"):
                        # Mouse tracking is ONE exclusive protocol slot in both
                        # real xterm and xterm.js: a DECSET replaces the active
                        # protocol, and a DECRST of ANY family member turns
                        # tracking off entirely. Three independent booleans left
                        # a stale flag behind ("1000h…1003h…1003l" kept click
                        # tracking True) and the mirror seed then re-armed mouse
                        # reporting on a child that had turned it off — browser
                        # SGR reports typed into its stdin. (#review-mouse-slot)
                        self._mouse_click = _on and _p == "1000"
                        self._mouse_btn_motion = _on and _p == "1002"   # drag motion
                        self._mouse_any_motion = _on and _p == "1003"   # hover motion
                    elif _p == "1004":
                        self._focus_reporting = _on    # child wants focus in/out events
                    elif _p == "1006":
                        self._mouse_sgr = _on          # SGR extended encoding
            # any tracking on ⇒ the child owns the mouse (incl. wheel + drag-select)
            self._mouse_reporting = (getattr(self, "_mouse_click", False)
                                     or getattr(self, "_mouse_btn_motion", False)
                                     or getattr(self, "_mouse_any_motion", False))
        # Honor the child's OSC 52 clipboard writes (e.g. claude's fullscreen 'copy
        # selection'): saikai consumes the child's output and pyte ignores OSC 52, so
        # decode + set the HOST clipboard ourselves. Reassemble across reads — a large
        # selection's base64 can span chunks. (#osc52-clipboard)
        if "\x1b]52;" in chunk or getattr(self, "_osc52_carry", ""):
            _clip = getattr(self, "_osc52_carry", "") + chunk
            self._osc52_carry = ""
            _last = 0
            for _mo in _OSC52_RE.finditer(_clip):
                self._honor_osc52(_mo.group(1))
                _last = _mo.end()
            _open = _clip.rfind("\x1b]52;")
            if _open >= _last and _OSC52_RE.search(_clip[_open:]) is None:
                self._osc52_carry = _clip[_open:][-131072:]   # unterminated tail (capped)
        # Surface the child's desktop notifications (OSC 9 / 777 / 99) as a saikai
        # toast. (#osc-notify)
        if "\x1b]9;" in chunk or "\x1b]777;" in chunk or "\x1b]99;" in chunk:
            for _msg in _OSC9_NOTIFY_RE.findall(chunk):
                self._notify_host(_msg)
            for _msg in _OSC777_RE.findall(chunk):
                self._notify_host(_msg.replace(";", ": ", 1))
            for _msg in _OSC99_RE.findall(chunk):
                self._notify_host(_msg)
        # Query side channels must stay live while a synchronized-output frame is
        # retained. In particular, a child can wait for a capability response
        # before it emits ?2026l.
        self._answer_static_queries(chunk)
        sync_output = getattr(self, "_sync_output", None)
        if sync_output is None:                 # compatibility with minimal test objects
            sync_output = self._sync_output = _SynchronizedOutputStager()
        units = sync_output.push(chunk)
        if sync_output.active and sync_output.pending_query:
            units.extend(sync_output.flush("pending-query"))

        changed = False
        for text, fail_reason in units:
            if fail_reason:
                _log(f"sync-output fail-open: reason={fail_reason} chars={len(text)}")
            self._feed_presentation_unit(text)
            changed = True
        return changed

    def _feed_presentation_unit(self, chunk: str) -> None:
        """Feed one complete unit to pyte, answering each DSR-6 AT ITS POSITION.

        A real terminal replies with the cursor as of the query. Answering once the
        whole unit is drawn reports wherever the trailing output left the cursor, so
        a shell-integration wrap probe followed by buffered output in the same PTY
        write computes the wrong prompt row. Splitting the feed is invisible to the
        UI: the pane repaint and the IME anchor observe pyte only after _consume
        returns, so the unit still lands atomically as far as they can tell.
        (#term-queries)"""
        replies = []
        pos = 0
        for _m in _POSITIONAL_QUERY_RE.finditer(chunk):
            _mode = _m.group(3)
            if _mode is None and _m.group(2) != "6":
                continue                       # DSR-5 is state-free, answered early
            self._consume_ready(chunk[pos:_m.end()])
            if _mode is None:
                row, col = self._cursor_rowcol()
                replies.append(f"\x1b[{_m.group(1)}{row};{col}R")
            else:
                replies.append(f"\x1b[?{_mode};{self._decrqm_report(_mode)}$y")
            pos = _m.end()
        self._consume_ready(chunk[pos:])
        if replies:
            resp = "".join(replies)
            self._marshal(lambda r=resp: self._send_to_child(r))

    def _consume_ready(self, chunk: str) -> None:
        """Feed one complete presentation unit to pyte and its mirror."""
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
            # Mirror pane-direct tee — INSIDE the lock, after the pyte feed, so
            # attach_mirror()'s seed (computed under this same lock) strictly
            # precedes every chunk tee'd after it: a chunk is either in the seed
            # or in the stream, never both. The tee is a put_nowait into the hub
            # ingest queue — no marshal, no blocking, no regex (invariant #1
            # holds; the child-query strip runs on the hub's DRAIN thread via
            # set_pane_strip(_MIRROR_QUERY_STRIP_RE), so a burst never pays a
            # regex scan while holding this lock). The FULL scrubbed chunk goes
            # through: the alt-collapse above may feed pyte only a suffix, but
            # the browser xterm has both buffers natively and must see every
            # byte. (#pane-direct)
            _tee = getattr(self, "_mirror_tee", None)   # getattr: minimal test
            if _tee is not None:                        # instances skip __init__
                try:
                    _tee(chunk)
                except Exception:
                    pass
        # Classify from the CURRENT screen + claude's OSC-0 title (its own state
        # glyph), not a rolling byte tail: a tail keeps stale "esc to interrupt"
        # / answered prompts that scrolled up and would misclassify an idle pane.
        # Throttle while stably busy (#agent-storm-throttle): re-classifying every
        # spinner frame renders the whole pyte grid + runs the regex for nothing
        # (status stays 'busy'). A non-busy status is never throttled, so a flip
        # INTO busy and a prompt (waiting) are still caught promptly; the flip OUT
        # of busy rides the host refresh_status poll when output stops.
        _now = time.monotonic()
        if not (getattr(self, "_status", None) == "busy"
                and (_now - getattr(self, "_last_classify_ts", 0.0)) < _CLASSIFY_MIN_INTERVAL):
            self._last_classify_ts = _now
            _txt, _title = self._current_screen()
            self._update_status(self._classify(_txt, _title))
        # A real BEL from the child (pyte distinguishes it from an OSC terminator):
        # ring the host bell — claude's attention signal / notification fallback.
        # Gated by SAIKAI_NO_BELL. (#bell)
        _scr = self._screen
        if _scr is not None and getattr(_scr, "_bell_rang", False):
            _scr._bell_rang = False
            if not os.environ.get("SAIKAI_NO_BELL"):
                self._marshal(lambda: self._ring_bell())

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
        """Run the status classifier, then tame a body-text 'waiting' on the ALT
        screen. The blanket "alt ⇒ never waiting" rule assumed claude's REAL task
        prompts render in the NORMAL buffer — current claude (≥2.1) enters the
        alt screen at boot and never leaves, so that rule silenced every genuine
        gate: probe-verified 2026-07-16 on the resume-from-summary forced choice
        (classify said waiting, the demotion said idle), and by construction the
        same held for mid-turn permission prompts. What the demotion actually
        protects against is (#alt-waiting):
          (a) the user DRIVING a full-screen TUI (agent switcher, /help) whose
              menus redraw under their keys → discriminate by exactly that:
              recent input INTO this pane (keys/paste/mirror all stamp
              last_input_ts), not by which buffer painted;
          (b) a finished ANSWER that merely ends in a numbered list (_MENU_RE
              alone) → still demoted: a real gate carries a ❯ choice pointer or
              an explicit question/y-n (_WAITING_RE), a list does not.
        The title-spinner 'busy' path is unaffected (it returns before the
        waiting check). (#resume-gate-waiting)"""
        classifier = getattr(self, "_status_classifier", classify_pty_status)
        st = classifier(txt, title)
        alt = getattr(self, "_alt", None)
        if st == "waiting" and alt is not None and alt.in_alt:
            if (time.monotonic() - getattr(self, "last_input_ts", 0.0)) < 4.0:
                return "idle"                      # (a) user navigating a TUI
            tail = _ANSI_RE.sub("", (txt or "")[-2000:])
            if not (_WAITING_RE.search(tail) or _TRUST_RE.search(txt or "")):
                return "idle"                      # (b) bare numbered list
        return st

    def refresh_status(self) -> None:
        """Re-classify from the current screen + title. The host calls this
        periodically so a pane that went idle WITHOUT new output (no reader tick
        to re-run _consume) still flips out of 'busy', and the debounce gets its
        second tick on the timer cadence."""
        if self._screen is None or self.is_dead:
            return
        # This poll is also the only tick a QUIET pane gets. The IME anchor's
        # hidden-cursor settle and its mid-frame freeze are both re-evaluated by a
        # sync, and syncs otherwise ride repaints — which the reader schedules only
        # for new output. A child that hid its cursor and then stopped emitting
        # would keep a stale native cursor on screen forever. (#ime-midframe)
        self._sync_terminal_cursor(reason="repaint")
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
        # Leaving 'busy' = the agent storm ended and the prompt is stable. The
        # per-repaint anchor sync FROZE while busy (anti-fly), so settle it now onto
        # the resting prompt and flush. Marshalled to the UI thread (this runs on the
        # reader thread or the host poll); the sync no-ops off the focused/live pane.
        # (#agents-cursor)
        if fire is not None and fire not in ("busy", "dead"):
            self._marshal(lambda: self._sync_terminal_cursor(reason="settle"))

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
        # Sync the IME anchor INLINE on the repaint: it rides this CompositorUpdate
        # (so app.cursor_position actually reaches WT — no separate flush needed),
        # updates cross-platform, and can't be starved by a timer. The anti-fly is a
        # POSITION freeze inside _sync_terminal_cursor (frozen while status=='busy'),
        # not a deferral of the whole sync. (#agents-cursor)
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
        screen isn't reachable. (#ime-appfocus)

        A pushed ModalScreen is only SUSPENDED by Textual — the base screen keeps
        its ``focused`` — so screen.focused alone still names this pane while a
        modal owns the keyboard, and the anchor would steal app.cursor_position
        (and force ?25h) from the modal's Input mid-composition. Require this
        pane's screen to BE the app's active screen. (#ime-modal)"""
        try:
            screen = self.screen
            app = self.app
            if app is not None and app.screen is not screen:
                return False
            return screen.focused is self
        except Exception:
            # The fallback must not raise either: this is a guard on the input path
            # (_snap_to_live re-anchors through it), and has_focus is a reactive that
            # throws on a not-fully-constructed widget.
            try:
                return bool(self.has_focus)
            except Exception:
                return False

    def _show_hw_cursor(self, show: bool, *, force: bool = False) -> None:
        """Show/hide the REAL terminal cursor (Windows).

        This cursor is an IME anchor for the classic prompt. Repaint-driven moves
        are debounced separately so it does not chase child full-screen redraw.
        Repeated identical DEC visibility writes are suppressed; focus/app-focus
        can force one re-assertion after WT/Textual regained the window.
        (#native-cursor #agents-cursor)"""
        if not _IS_WIN or not _IME_ANCHOR:
            return
        if not force and getattr(self, "_hw_cursor_visible", None) is show:
            return
        try:
            drv = getattr(self.app, "_driver", None)
            if drv is not None:
                drv.write("\x1b[?25h" if show else "\x1b[?25l")
                self._hw_cursor_visible = show
                if _IME_DEBUG:
                    try:
                        foc = type(self.screen.focused).__name__
                    except Exception:
                        foc = "?"
                    _ime_dbg(f"hwcursor show={show} force={force} "
                             f"focused_pane={self._is_focused_pane()} screen.focused={foc}")
        except Exception:
            pass

    def on_focus(self, event=None) -> None:
        # Anchor the IME the moment the pane is focused (don't wait for a repaint).
        # _sync_terminal_cursor decides whether the native cursor is actually
        # visible: alt-screen full-screen UIs keep it hidden.
        self._sync_terminal_cursor(reason="focus")
        if getattr(self, "_focus_reporting", False):                # ?1004: tell the child it's focused
            self._send_to_child("\x1b[I")
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

    def _cursor_may_be_midframe(self, now: float) -> bool:
        """True when pyte's cursor is NOT trustworthy as the input caret.

        A torn ?2026 block (fail-open on timeout/overflow/cursor-query, then bypass
        until the block closes) feeds pyte the cursor-hidden/Home intermediate the
        stager exists to hide. Otherwise only an unbracketed 'busy' storm sweeps the
        cursor across the screen every frame — once the child brackets its frames the
        pane only ever holds frame-final state, so tracking stays correct through a
        storm and CJK typed into a generating pane keeps its anchor. (#ime-midframe)"""
        stager = getattr(self, "_sync_output", None)
        if stager is not None and stager.torn_at(now):
            return True
        if getattr(self, "_status", None) != "busy":
            return False
        return not (stager is not None and stager.atomic_at(now))

    def _sync_terminal_cursor(self, reason: str = "repaint", now=None) -> None:
        """Anchor the real (hidden) terminal cursor at claude's cursor cell so the
        host terminal's IME / composition popup appears at the claude prompt — not
        wherever Textual last parked the cursor (e.g. the search box, which owns the
        cursor until something else sets app.cursor_position). Textual keeps the
        hardware cursor hidden but still `move_to`s it every repaint, and WezTerm
        (and other IMEs) anchor the candidate window to that position.

        UI-thread only. Callers pass a `reason`:
          - "repaint" (default, from _do_pane_refresh): rides the paint. FROZEN while
            status=='busy' — an agent spinner moves the pyte cursor Home->…->prompt on
            every one of ~170k frames and a coalesced repaint catches it mid-frame, so
            moving the anchor then makes the IME/candidate window fly. Freezing keeps
            it at the last settled cell.
          - "settle" (from _update_status when the pane leaves 'busy'): the storm ended
            and the prompt is now stable, so re-anchor at it and force one repaint to
            flush (a settle fires outside the paint path).
          - "focus" (from on_focus / OS-window regain): always re-anchor + flush so the
            IME isn't left at Textual's default/search cursor.

        No-op unless THIS pane is focused and live (scroll at the bottom). Reads the
        pyte cursor under the lock, then touches app.cursor_position / writes the driver
        OUTSIDE the lock (per the concurrency invariant — never marshal/block while
        holding self._lock)."""
        if not _IME_ANCHOR:
            return
        if Offset is None or self.is_dead or not self._is_focused_pane():
            return
        if self._scroll != 0:
            # Scrolled back: the live prompt is off-view and the anchored cell now
            # sits on unrelated history, so a bare return would leave the native
            # cursor blinking there (and the IME opening there). Typing snaps the
            # pane back to live and re-anchors. (#ime-scrollback)
            self._show_hw_cursor(False)
            self._anchored_xy = None
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
                cursor_hidden = bool(getattr(screen.cursor, "hidden", False))
            except Exception:
                return
            scols = int(getattr(screen, "columns", 0) or 0)
            slines = int(getattr(screen, "lines", 0) or 0)
            in_alt = bool(getattr(getattr(self, "_alt", None), "in_alt", False))
        # A per-repaint sync FOLLOWS the cursor: claude moves the terminal cursor to
        # the real input caret (e.g. +2 columns per CJK char), and following it every
        # repaint is exactly what makes the IME anchor TRACK typing. It freezes only
        # while the cursor we'd read is NOT a caret:
        #   - mid-frame (an unbracketed agent-mode storm, or a torn ?2026 frame) —
        #     see _cursor_may_be_midframe; a stager delivering ATOMIC frames is safe
        #     to follow even during a storm, because pyte holds frame-final state.
        #   - freshly hidden (?25l): hold briefly so a redraw's ?25l/?25h can't
        #     flicker the anchor, but SETTLE — a child that stays in a no-cursor view
        #     must actually get the native cursor hidden, not left blinking at the
        #     stale cell. "focus"/"settle" are definitive and skip the gate.
        # (#agents-cursor #ime-midframe)
        now = time.monotonic() if now is None else float(now)
        if cursor_hidden:
            if not getattr(self, "_cursor_hidden_since", 0.0):
                self._cursor_hidden_since = now
        else:
            self._cursor_hidden_since = 0.0
        if reason == "repaint":
            hidden_since = getattr(self, "_cursor_hidden_since", 0.0)
            hiding = bool(hidden_since) and (now - hidden_since) < _NATIVE_CURSOR_HIDE_SETTLE
            midframe = self._cursor_may_be_midframe(now)
            if hiding or midframe:
                if _IME_DEBUG:
                    _ime_dbg(f"sync reason=repaint FREEZE cur=({cx},{cy}) "
                             f"midframe={midframe} hiding={hiding}")
                return
        if not _native_cursor_should_show(cursor_hidden, in_alt):
            self._show_hw_cursor(False)
            self._anchored_xy = None
            if _IME_DEBUG:
                _ime_dbg(f"sync reason={reason} HIDE (alt={in_alt} hidden={cursor_hidden})")
            return
        try:
            region = self.content_region
            xy = _ime_anchor_xy(cx, cy, region.x, region.y, region.width, region.height)
            if _IME_DEBUG:
                _ime_dbg(
                    f"sync reason={reason} pyte_cur=({cx},{cy}) pyte_size=({scols}x{slines}) "
                    f"region=(x={region.x},y={region.y},w={region.width},h={region.height}) "
                    f"anchor_xy={xy} moved={xy != getattr(self, '_anchored_xy', None)}")
            # Keep the native cursor SHOWN whenever the child shows its own — even if
            # geometry isn't settled yet (xy is None on an early focus event). Gating
            # the show behind a successful anchor left the IME disabled (×) on focus
            # into a scrolled/unsettled pane. force= on the non-repaint syncs so a
            # blur→refocus re-asserts ?25h even if visibility looked unchanged.
            self._show_hw_cursor(True, force=(reason != "repaint"))
            if xy is None:
                return
            moved = xy != getattr(self, "_anchored_xy", None)
            app.cursor_position = Offset(*xy)   # cross-platform IME anchor
            self._anchored_xy = xy
            # app.cursor_position only reaches the terminal during a CompositorUpdate.
            # A "repaint" sync already rides one. A "settle"/"focus" sync fires outside
            # the paint path, so force ONE repaint to flush the moved anchor — but only
            # when it actually MOVED, so an idle re-assert can't spin a repaint loop.
            if moved and reason != "repaint":
                try:
                    self.refresh(repaint=True)
                except Exception:
                    pass
        except Exception:
            pass

    def _cancel_forwarded_drag(self) -> None:
        """Drop a stuck forwarded-drag capture (e.g. the MouseUp was lost because the
        pane blurred / the OS window switched mid-drag). Send the child a release for
        each still-held button FIRST — else a fullscreen child thinks the button is
        still down and leaves its drag-selection armed — then drop the capture.
        (#faithful-mouse)"""
        if not self._fwd_buttons:
            return
        if self._child_owns_mouse() and self._pty is not None:
            col, row = getattr(self, "_fwd_last", (1, 1))
            for btn in sorted(self._fwd_buttons):
                base = ((btn - 1) & 3) if btn else 3
                try:
                    if self._mouse_sgr:
                        self._write_child(self._mouse_seq(base, col, row, "m"))
                    else:                                 # X10 release = button 3
                        self._write_child(self._mouse_seq(3, col, row, "M"))
                except Exception:
                    pass
        self._fwd_buttons.clear()
        self._fwd_captured = False
        try:
            self.release_mouse()
        except Exception:
            pass

    def on_blur(self, event=None) -> None:
        # Hide the native cursor so an unfocused pane / the list doesn't carry a
        # stray cursor — but NOT when focus moved to a widget that OWNS the cursor
        # (the search Input / a TextArea copy-mode): it needs the cursor visible at
        # its OWN caret. Forcing ?25l here made WT anchor the IME composition window
        # at the last-VISIBLE cell (this pane's prompt) instead of the search box.
        # (#ime-search-cursor)
        try:
            from textual.widgets import Input, TextArea
            _hands_off = isinstance(self.screen.focused, (Input, TextArea))
        except Exception:
            _hands_off = False
        if not _hands_off:
            self._show_hw_cursor(False)
        if getattr(self, "_focus_reporting", False):                # ?1004: tell the child it lost focus
            self._send_to_child("\x1b[O")
        self._cancel_forwarded_drag()          # a lost MouseUp must not stick capture

    # ── thread → UI marshaling (defensive) ─────────────────────────────────────
    def _marshal(self, fn: Callable) -> None:
        """call_from_thread that never raises on the reader thread (the app may
        be shutting down / the widget already unmounted).

        Textual REJECTS call_from_thread from the app's own thread, so a marshal
        issued on the UI thread (the 1.5s status poll flipping busy->idle, an
        input handler) used to be swallowed here and silently lost — the 'settle'
        anchor sync never ran on a quiet turn end. On that thread the callback is
        already in the right context, so run it inline. (#ime-settle)"""
        app = None
        try:
            app = self.app
        except Exception:
            return
        if app is None:
            return
        try:
            if getattr(app, "_thread_id", None) == threading.get_ident():
                try:
                    fn()
                except Exception:
                    pass
                return
        except Exception:
            pass
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
        # Drop anything still queued for the writer: the pane is going away, and a
        # writer blocked on a dying pty should retire instead of draining. (#paste-block)
        lock = getattr(self, "_write_lock", None)
        if lock is not None:
            with lock:
                if getattr(self, "_write_q", None):
                    self._write_q.clear()
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
        if new_sid in self._terms:
            # The target sid already has its OWN registered pane (a user opened
            # the child row in the seconds before the checkpoint re-key landed).
            # Overwriting would silently orphan that live pane's bookkeeping —
            # keep both intact instead; the old pane just stays keyed as-is
            # (same degraded-but-safe behaviour as a failed child detect).
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


# Status → a compact glyph for the tab label. Uses the SAME vocabulary as the
# session LIST (saikai.py _LIVE_MARKER: waiting "?", busy "~", idle "="), so a
# glyph means the same thing whether you read it in the list or on a tab — one
# vocabulary to learn, not two. Keep both in step when adding/renaming a status.
# "dead" → "x" (exited) is tab-only; the list drops a dead pane to its file markers.
STATUS_GLYPH = {
    "busy": "~",      # working
    "waiting": "?",   # needs input
    "idle": "=",      # ready / idle
    "dead": "x",      # exited
}


def tab_label(title: str, status: str) -> str:
    """Build a TabPane label like '~ saikai' / '? docs' / 'x myproj' — the same
    status glyphs the session list uses.

    Titles derive from USER content (the first message, an AI title), so strip
    ANSI escapes and collapse control chars/newlines BEFORE truncating — a
    "\\n" or ESC sequence in a tab label corrupts the whole tab bar, and
    slicing first could cut an escape sequence in half. (#audit-hostile-title)"""
    glyph = STATUS_GLYPH.get(status, "")
    name = _ANSI_RE.sub("", str(title or "agent"))
    name = re.sub(r"[\x00-\x1f\x7f]+", " ", name).strip()[:18] or "agent"
    return f"{glyph} {name}".strip()
