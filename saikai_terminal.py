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
import copy
import os
import re
import signal
import subprocess
import sys
import threading
import time
import unicodedata
import uuid
from collections import deque
from dataclasses import dataclass
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
_EGC_MAX_CODEPOINTS = 256


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


# IME-anchor: re-anchor the outer cursor to the child caret and, on Windows,
# expose that native cursor for CJK composition. This is host-side anchoring,
# not an extra child-rendered cursor. DECTCEM visibility and DECSCUSR shape are
# followed on either buffer; redraw transients are settled so the candidate
# window does not chase intermediate positions. Set SAIKAI_IME_ANCHOR=0 to turn
# it off completely. (#ime-anchor-optout)
_IME_ANCHOR = str(os.environ.get("SAIKAI_IME_ANCHOR", "1")).strip().lower() not in (
    "0", "false", "no", "off")


def _under_wsl() -> bool:
    """True when this process runs inside WSL.

    WSL_DISTRO_NAME/WSL_INTEROP are the fast path but are absent for non-shell
    entry points and can be disabled; /proc/version naming Microsoft is the
    env-independent proof and cannot arrive through inheritance."""
    if os.environ.get("WSL_DISTRO_NAME") or os.environ.get("WSL_INTEROP"):
        return True
    try:
        with open("/proc/version", "r", encoding="utf-8", errors="replace") as fh:
            return "microsoft" in fh.read().lower()
    except Exception:
        return False


def _wt_posix_host(env=None, wsl=None) -> bool:
    """True when a POSIX saikai is really drawing into a Windows Terminal tab.

    WT_SESSION names Windows Terminal but only says "this process tree started in
    a WT tab": it survives ssh out of that tab and dotfile exports. A WSL proof
    alone says nothing about which emulator is on the other side. Require both.
    A terminal multiplexer owns the caret itself, so defer to it."""
    env = os.environ if env is None else env
    if not env.get("WT_SESSION"):
        return False
    if env.get("TMUX") or env.get("STY"):
        return False
    return _under_wsl() if wsl is None else bool(wsl)


_WT_POSIX_HOST = (not _IS_WIN) and _wt_posix_host()
# SAIKAI_NATIVE_CARET forces caret ownership either way (unset = auto), for a
# host the detection above cannot name.
_NATIVE_CARET_OVERRIDE: Optional[bool] = (
    None if not str(os.environ.get("SAIKAI_NATIVE_CARET", "")).strip()
    else str(os.environ.get("SAIKAI_NATIVE_CARET", "")).strip().lower() not in (
        "0", "false", "no", "off"))


def _native_caret() -> bool:
    """True when saikai owns the one real outer caret (and the IME anchors to it).

    The single ownership predicate: the render path must NOT draw a software
    caret when this is true, and the driver must NOT be told to show/hide the
    real cursor when it is false. Reading the module flags at call time keeps it
    monkeypatchable from the platform tests."""
    if not _IME_ANCHOR:
        return False
    if _NATIVE_CARET_OVERRIDE is not None:
        return _NATIVE_CARET_OVERRIDE
    return bool(_IS_WIN or _WT_POSIX_HOST)

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
    # WT_SESSION is handled by _child_pty_env, not stripped here: it is a
    # deliberate Windows-only caret compatibility signal. (#wt-session)
    "TERM_PROGRAM",
    "TERM_PROGRAM_VERSION",
    "LC_TERMINAL",
    "LC_TERMINAL_VERSION",
    "TMUX",
    "TMUX_PANE",
    "STY",
    "VTE_VERSION",
    "ZED_TERM",
    # Claude sets this for Windows/WT fleet views; saikai's pane is neither.
    # NOTE: CLAUDE_CODE_FORCE_SYNC_OUTPUT is deliberately NOT stripped — an
    # explicit user override stays explicit, and saikai now presents ?2026 frames
    # atomically instead of needing the child to avoid them.
    "CLAUDE_CODE_ALT_SCREEN_FULL_REPAINT",
}
# Whole emulator families include live IPC endpoints (WEZTERM_UNIX_SOCKET,
# KITTY_LISTEN_ON, ALACRITTY_SOCKET, Konsole/GNOME DBus identifiers), so a
# hand-list is both fragile and unsafe. TERMINFO* is also host-specific: an
# inherited override can make the child emit capabilities that saikai's virtual
# terminal does not implement.
_HOST_TERMINAL_ENV_STRIP_PREFIXES = (
    "WEZTERM_",
    "KITTY_",
    "ALACRITTY_",
    "KONSOLE_",
    "GNOME_TERMINAL_",
    "TERMINFO",
    # WT_PROFILE_ID / WT_SETTINGS_DIR name the outer tab's profile and settings
    # store, and Windows Terminal keeps adding to this namespace. WT_SESSION is
    # the one deliberate exception and is re-added below, after the strip.
    "WT_",
)
# Windows Terminal forwards its identity into WSL through WSLENV, so a stripped
# WT_* variable can still be named there. WSLENV also carries the user's OWN
# forwarding, so it is rewritten rather than dropped. (#wt-session #wsl)
_WSLENV_STRIP_PREFIXES = ("WT_",)

# Opt out of presenting the pane as Windows Terminal (env side):
# SAIKAI_WT_IDENTITY=0 leaves WT_SESSION as the host set it on Windows. POSIX/WSL
# always strips it because the pane does not emulate WT there. (#wt-session-optout)
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


def _utf8_locale(value: object) -> str:
    """Replace only a locale's codeset, preserving language and modifier."""
    locale = str(value or "").strip()
    if not locale:
        return locale
    if locale.upper() in ("UTF-8", "UTF8"):
        return locale
    base_and_codeset, marker, modifier = locale.partition("@")
    base = base_and_codeset.split(".", 1)[0]
    if base.upper() == "POSIX":
        base = "C"
    normalized = f"{base or 'C'}.UTF-8"
    return normalized + (marker + modifier if marker else "")


def _child_pty_env(base_env, is_win: Optional[bool] = None) -> dict:
    """Environment advertised by saikai's PTY renderer to the child.

    The child talks to saikai's virtual terminal. Keep the capability contract
    explicit and deterministic instead of inheriting the outer terminal's brand
    probes (TERM_PROGRAM, WEZTERM_*, KITTY_WINDOW_ID, …).

    WT_SESSION is the one host compatibility signal saikai preserves or
    synthesizes on Windows. MEASURED on-device: with it present Claude tracks the
    input caret with
    the terminal cursor — which is what the IME anchor follows; without it Claude
    PARKS the cursor at a fixed base cell and the anchor pins composition there.
    Inheriting it made that a lottery on the outer host (broken under WezTerm and
    conhost), so synthesize one on Windows. On POSIX the pane is not a WT
    endpoint, and WSL may inherit WT_SESSION from its outer host; strip it there
    so the child does not select WT-specific redraw paths behind pyte.
    (#agents-cursor #wt-session #wsl)"""
    platform_is_win = _IS_WIN if is_win is None else is_win
    env = dict(base_env)

    # Capture the host's WT_SESSION BEFORE the strip loop: the WT_ prefix now
    # scrubs the whole Windows Terminal namespace, and the identity exception
    # below has to re-add the outer value rather than a synthesized one.
    outer_wt = None
    for key in list(env):
        if (key.upper() if platform_is_win else key) == "WT_SESSION":
            outer_wt = env.pop(key)

    # os.environ is case-insensitive on Windows, but callers/tests may supply a
    # plain dict. Match the target platform's semantics and remove duplicate
    # spellings before adding canonical saikai values.
    for key in list(env):
        comparable = key.upper() if platform_is_win else key
        if (comparable in _HOST_TERMINAL_ENV_STRIP
                or comparable.startswith(_HOST_TERMINAL_ENV_STRIP_PREFIXES)):
            env.pop(key, None)

    # WSLENV forwards variables across the Win32<->WSL boundary. Remove the
    # entries naming variables saikai just stripped and keep the user's own; drop
    # the directive only when nothing is left.
    for key in list(env):
        if (key.upper() if platform_is_win else key) != "WSLENV":
            continue
        kept = [entry for entry in str(env[key]).split(":")
                if entry and not entry.upper().startswith(_WSLENV_STRIP_PREFIXES)]
        if kept:
            env[key] = ":".join(kept)
        else:
            env.pop(key, None)
    if platform_is_win:
        if _WT_IDENTITY:
            env["WT_SESSION"] = outer_wt or _wt_session_id()
        elif outer_wt is not None:
            env["WT_SESSION"] = outer_wt
    # POSIX/WSL never inherits WT_SESSION, including when the Windows identity
    # compatibility switch is disabled.

    for key in list(env):
        comparable = key.upper() if platform_is_win else key
        if comparable == "LANG" or comparable.startswith("LC_"):
            if env[key]:
                env[key] = _utf8_locale(env[key])
    if not any(env.get(key) for key in ("LC_ALL", "LC_CTYPE", "LANG")):
        env["LC_CTYPE"] = "C.UTF-8"

    for key in list(env):
        if (key.upper() if platform_is_win else key) in {
                "TERM", "COLORTERM", "TERM_PROGRAM",
                "PYTHONUTF8", "PYTHONIOENCODING"}:
            env.pop(key, None)
    env["TERM"] = "xterm-256color"
    env["COLORTERM"] = "truecolor"
    env["TERM_PROGRAM"] = "saikai"
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    return env


# ── global reap-thread registry ───────────────────────────────────────────────
# Every detached PTY is closed/reaped on daemon workers; explicit Windows kill
# also runs `taskkill /F /T` there. If saikai exits first, descendants may orphan
# and blocking backend cleanup may be abandoned. on_unmount-driven teardown and
# exceptions do not all route through the App's join_reaps, so track every reap
# and close helper here and bounded-join them at interpreter exit.
_REAP_THREADS: list = []
_REAP_LOCK = threading.Lock()
_PTY_WRITER_THREADS: list = []
_PTY_WRITER_THREADS_LOCK = threading.Lock()


def _track_reap(t) -> None:
    if t is None:
        return
    with _REAP_LOCK:
        _REAP_THREADS[:] = [x for x in _REAP_THREADS if x.is_alive()]
        _REAP_THREADS.append(t)


def _track_pty_writer(thread) -> None:
    if thread is None:
        return
    with _PTY_WRITER_THREADS_LOCK:
        _PTY_WRITER_THREADS[:] = [
            item for item in _PTY_WRITER_THREADS if item.is_alive()]
        _PTY_WRITER_THREADS.append(thread)


def join_all_pty_writers(timeout: float = 1.0) -> None:
    """Bounded-join stopped pane writers and prune the global registry."""
    deadline = time.monotonic() + timeout
    with _PTY_WRITER_THREADS_LOCK:
        threads = list(_PTY_WRITER_THREADS)
    for thread in threads:
        try:
            thread.join(timeout=max(0.0, deadline - time.monotonic()))
        except Exception:
            pass
    with _PTY_WRITER_THREADS_LOCK:
        _PTY_WRITER_THREADS[:] = [
            item for item in _PTY_WRITER_THREADS if item.is_alive()]


def join_all_reaps(timeout: float = 3.0) -> None:
    """Bounded-join every tracked reap so process exit doesn't orphan node
    workers. Safe to call repeatedly; prunes finished threads."""
    import time
    deadline = time.monotonic() + timeout
    current = threading.current_thread()
    # A POSIX reaper can register its reap-close helper while this function is
    # already joining that parent. Keep taking live snapshots until the
    # registry reaches a fixed point (or the shared deadline expires), rather
    # than abandoning helpers absent from the first snapshot.
    while True:
        with _REAP_LOCK:
            _REAP_THREADS[:] = [x for x in _REAP_THREADS if x.is_alive()]
            threads = [x for x in _REAP_THREADS if x is not current]
        if not threads:
            break
        remaining = deadline - time.monotonic()
        if remaining <= 0.0:
            break
        for thread in threads:
            remaining = deadline - time.monotonic()
            if remaining <= 0.0:
                break
            try:
                # Short rounds let a newly registered helper receive part of
                # the same bounded shutdown budget even if another reap stalls.
                thread.join(timeout=min(0.05, remaining))
            except Exception:
                pass
    with _REAP_LOCK:
        _REAP_THREADS[:] = [x for x in _REAP_THREADS if x.is_alive()]
    join_all_pty_writers(timeout=max(0.0, deadline - time.monotonic()))


atexit.register(join_all_reaps)


def _post_signal(pid, sig_name: str) -> None:
    """POSIX: send `sig_name` to pid's process GROUP (ptyprocess setsid()s the
    child, so pgid == pid and the group covers claude's node workers — the
    `taskkill /T` analog). Fall back to the single process only when the host
    has no process-group API; an ESRCH from killpg must not be retried against
    a numeric PID which may already have been reused. The signal is looked up
    by NAME so this module — and the headless tests that exercise the POSIX kill
    path — stay importable on Windows. Never raises."""
    sig = getattr(signal, sig_name, None)
    if not pid or sig is None:
        return
    try:
        os.killpg(pid, sig)
        return
    except (AttributeError, NotImplementedError):
        # Windows and a few restricted POSIX runtimes have no group primitive.
        pass
    except Exception:
        # The group disappeared or access was denied. Never reinterpret that as
        # authority to signal a possibly recycled bare PID.
        return
    try:
        os.kill(pid, sig)
    except Exception:
        pass


def _process_group_alive(pgid) -> bool:
    """Return whether a POSIX PTY process group still has any member."""
    if _IS_WIN or not pgid:
        return False
    try:
        os.killpg(int(pgid), 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        return False

# ── Soft imports ─────────────────────────────────────────────────────────────
# The widget is only constructed when these are present (saikai probes
# TERMINAL_AVAILABLE before offering split-live). Importing this module never
# raises just because a dep is missing — that keeps the preview fallback intact
# and lets py_compile / unit tests run without textual/pyte/pywinpty.
try:
    import pyte  # type: ignore
    import regex as _regex
    from rich.cells import cell_len as _rich_cell_len

    _GRAPHEME_RE = _regex.compile(r"\X")
    _HistoryScreenBase = pyte.HistoryScreen
    try:
        from pyte import modes as _mo

        class _SaikaiHistoryScreen(pyte.HistoryScreen):  # type: ignore[misc]
            """A pyte history screen whose grid stores complete Unicode EGCs.

            pyte draws codepoints and uses a process-global wcwidth function.
            Rich/Textual render grapheme clusters, so codepoint widths disagree
            for flags, VS16, keycaps, emoji modifiers, and ZWJ sequences. Keep
            that policy local: segment with ``regex \\X``, put one complete EGC
            in its leader cell, and advance by Rich's own cell-width function.

            A base character is visible immediately. Only bounded metadata for
            the most recent EGC is retained; a later adjacent draw may replace
            it retrospectively when the new codepoints extend the cluster.
            """

            def __init__(self, *args, **kwargs) -> None:
                self._egc_candidate = None
                self._history_generation = 0
                # OSC 8 metadata is kept beside pyte's immutable Char tuples.
                # id -> (strong Char ref, (params, uri)); the strong reference
                # prevents Python id reuse until bounded pruning.
                self._saikai_hyperlink_refs = {}
                self._saikai_hyperlinks = {}
                self._saikai_active_hyperlink = None
                # xterm.js stores a grapheme in at most two physical cells.
                # Mark semantics which require a final authoritative reseed.
                self._saikai_mirror_hazard_serial = 0
                self._saikai_mirror_has_wide_cluster = False
                super().__init__(*args, **kwargs)

            def invalidate_grapheme_candidate(self) -> None:
                self._egc_candidate = None

            def before_event(self, event: str) -> None:
                # Any VT operation between two draw events is a hard grapheme
                # boundary, even when it happens to leave the cursor adjacent.
                if event != "draw":
                    self.invalidate_grapheme_candidate()
                super().before_event(event)

            def reset(self) -> None:
                self.invalidate_grapheme_candidate()
                super().reset()
                self._saikai_hyperlink_refs.clear()
                self._saikai_hyperlinks.clear()
                self._saikai_active_hyperlink = None
                self._saikai_mirror_has_wide_cluster = False

            def _mark_mirror_semantics_hazard(self, *, wide=False) -> None:
                self._saikai_mirror_hazard_serial += 1
                if wide:
                    self._saikai_mirror_has_wide_cluster = True

            def _refresh_mirror_wide_state(self) -> bool:
                """Recompute whether visible xterm cell edits need reseeding."""
                self._saikai_mirror_has_wide_cluster = any(
                    bool(char.data)
                    and int(_rich_cell_len(char.data)) > 2
                    for row in range(self.lines)
                    for char in (
                        self.buffer[row][column]
                        for column in range(self.columns)
                    )
                )
                return self._saikai_mirror_has_wide_cluster

            def _stamp_active_hyperlink(self, char) -> None:
                link = self._saikai_active_hyperlink
                if link is None:
                    return
                refs = self._saikai_hyperlink_refs
                refs[id(char)] = (char, link)
                cap = max(1024, self.lines * self.columns * 4)
                if len(refs) > cap:
                    self._refresh_saikai_hyperlinks(prune=True)

            def _refresh_saikai_hyperlinks(self, *, prune=False):
                """Materialize visible OSC 8 cell coordinates for mirror seeds."""
                refs = self._saikai_hyperlink_refs
                coordinates = {}
                live_ids = set()
                for row in range(self.lines):
                    line = self.buffer[row]
                    for column in range(self.columns):
                        char = line[column]
                        entry = refs.get(id(char))
                        if entry is None or entry[0] is not char:
                            continue
                        coordinates[(row, column)] = entry[1]
                        live_ids.add(id(char))
                self._saikai_hyperlinks = coordinates
                if prune:
                    self._saikai_hyperlink_refs = {
                        key: refs[key] for key in live_ids
                    }
                return coordinates

            @staticmethod
            def _fixed_pane_modes(modes, *, private: bool):
                """Drop modes whose pyte side effects violate a fixed pane.

                DECCOLM resizes only pyte's active screen to 80/132 columns,
                leaving the PTY, widget, inactive buffer, and mirror at the real
                pane width. DECSCNM destructively rewrites cell SGR attributes
                instead of applying a renderer-wide reverse-video flag. The
                embedded xterm.js ignores both with its default options, so keep
                them side-effect free locally as well.
                """
                ignored = (
                    {3, 5} if private
                    else {_mo.DECCOLM, _mo.DECSCNM}
                )
                return tuple(mode for mode in modes if mode not in ignored)

            def set_mode(self, *modes: int, **kwargs) -> None:
                filtered = self._fixed_pane_modes(
                    modes, private=bool(kwargs.get("private")))
                if filtered:
                    super().set_mode(*filtered, **kwargs)

            def reset_mode(self, *modes: int, **kwargs) -> None:
                filtered = self._fixed_pane_modes(
                    modes, private=bool(kwargs.get("private")))
                if filtered:
                    super().reset_mode(*filtered, **kwargs)

            def save_cursor(self) -> None:
                """Model xterm's one overwriteable DECSC slot, not a stack."""
                super().save_cursor()
                if len(self.savepoints) > 1:
                    self.savepoints[:] = [self.savepoints[-1]]

            def restore_cursor(self) -> None:
                """DECRC is repeatable; restoring does not consume the slot."""
                if not self.savepoints:
                    super().restore_cursor()
                    return
                saved = self.savepoints[-1]
                # pyte assigns saved.cursor directly to self.cursor. Retain a
                # distinct copy or later cursor movement would mutate the slot.
                persistent = saved._replace(cursor=copy.copy(saved.cursor))
                super().restore_cursor()
                # pyte only ever re-SETS origin/wrap, so a mode saved OFF and
                # restored while ON stayed on. Reconcile both directions through
                # the mode set directly: set_mode/reset_mode home the cursor on any
                # DECOM change, and DECRC restores position and mode together.
                for flag, saved_on in ((_mo.DECOM, saved.origin),
                                       (_mo.DECAWM, saved.wrap)):
                    if saved_on:
                        self.mode.add(flag)
                    else:
                        self.mode.discard(flag)
                # pyte then clamps into the scroll region unconditionally
                # (ensure_vbounds(use_margins=True)). A savepoint taken with origin
                # mode OFF holds an ABSOLUTE position that may legitimately sit
                # outside the margins, so re-apply it under the restored mode.
                self.cursor.x = persistent.cursor.x
                self.cursor.y = persistent.cursor.y
                self.ensure_hbounds()
                self.ensure_vbounds(use_margins=bool(saved.origin))
                self.savepoints[:] = [persistent]

            def resize(self, lines=None, columns=None) -> None:
                self.invalidate_grapheme_candidate()
                old_columns = self.columns
                super().resize(lines, columns)
                # pyte clips visible rows cell-by-cell and does not resize its
                # history queues. A shrink through the stub of a wide EGC can
                # therefore leave a two-cell leader in the new final column;
                # old explicit history cells beyond the new width can also
                # reappear after a later grow. Canonicalize every retained row.
                for row in range(self.lines):
                    self._normalize_row_clusters(row)
                for history_rows in (
                        getattr(self.history, "top", ()),
                        getattr(self.history, "bottom", ())):
                    for line in history_rows:
                        if self.columns >= old_columns:
                            continue
                        for column in range(self.columns, old_columns):
                            line.pop(column, None)
                        # Rich permits a single EGC to occupy more than two
                        # cells (for example repeated Hangul L or some Indic
                        # conjuncts). A shrink can therefore clip a leader far
                        # from the new edge; validate the complete retained row.
                        self._normalize_line_clusters(line)

            def index(self) -> None:
                top, bottom = self.margins or (0, self.lines - 1)
                if self.cursor.y == bottom:
                    self._history_generation += 1
                super().index()

            def _candidate_is_live(self, candidate) -> bool:
                (text, row, col, width, after_row, after_col,
                 _displaced, _rollback) = candidate
                if (self.cursor.y, self.cursor.x) != (after_row, after_col):
                    return False
                if not (0 <= row < self.lines and 0 <= col < self.columns):
                    return False
                line = self.buffer[row]
                if line[col].data != text:
                    return False
                return all(
                    col + offset < self.columns
                    and line[col + offset].data == ""
                    for offset in range(1, width)
                )

            def _intersecting_cell_positions(
                    self, row: int, start: int, width: int) -> tuple[int, ...]:
                """Return every leader/stub cell touched by a replacement.

                A narrow overwrite of a wide glyph must also clear its old stub;
                writing into a stub must clear the old leader. pyte's stock draw
                leaves those half-glyph cells behind.
                """
                line = self.buffer[row]
                touched = set()
                for pos in range(start, min(self.columns, start + max(1, width))):
                    leader = pos
                    while leader > 0 and line[leader].data == "":
                        leader -= 1
                    touched.add(leader)
                positions = set()
                for leader in touched:
                    positions.add(leader)
                    pos = leader + 1
                    while pos < self.columns and line[pos].data == "":
                        positions.add(pos)
                        pos += 1
                return tuple(sorted(positions))

            def _clear_intersecting_cells(self, row: int, start: int, width: int) -> None:
                blank = self.cursor.attrs._replace(data=" ")
                line = self.buffer[row]
                for pos in self._intersecting_cell_positions(row, start, width):
                    line[pos] = blank

            def _snapshot_displaced_cells(
                    self, row: int, start: int, width: int,
                    *, insert_mode: bool) -> tuple:
                """Save the bounded row cells changed by a provisional EGC.

                Most snapshots contain one or two cells. In insert mode the
                affected suffix is included because pyte shifts it; its size is
                bounded by the already-allocated terminal row.
                """
                positions = set(
                    self._intersecting_cell_positions(row, start, width))
                if insert_mode:
                    positions.update(range(start, self.columns))
                line = self.buffer[row]
                return tuple((pos, line[pos]) for pos in sorted(positions))

            def _restore_displaced_cells(self, row: int, displaced: tuple) -> None:
                line = self.buffer[row]
                for pos, char in displaced:
                    if 0 <= pos < self.columns:
                        line[pos] = char
                self.dirty.add(row)

            def _merge_displaced_cells(
                    self, row: int, start: int, width: int,
                    *, insert_mode: bool, displaced: tuple) -> tuple:
                """Extend a provisional EGC's snapshot without losing old cells.

                A cluster can widen more than once as one PTY write is split
                (for example an Indic conjunct whose Rich width progresses
                1 -> 2 -> 3).  Snapshot newly covered cells before replacing
                them, while retaining the values saved before the first draw.
                """
                saved = dict(displaced)
                for position, char in self._snapshot_displaced_cells(
                        row, start, width, insert_mode=insert_mode):
                    saved.setdefault(position, char)
                return tuple(sorted(saved.items()))

            def _capture_scroll_rollback(self):
                """Capture the bounded state needed to undo one provisional wrap."""
                top, bottom = self.margins or (0, self.lines - 1)
                if self.cursor.y != bottom:
                    return None
                history_top = self.history.top
                evicted = (
                    history_top[0]
                    if history_top.maxlen is not None
                    and history_top.maxlen > 0
                    and len(history_top) == history_top.maxlen
                    else None
                )
                return (
                    top,
                    bottom,
                    tuple((row, self.buffer[row])
                          for row in range(top, bottom + 1)),
                    evicted,
                    self._history_generation,
                )

            def _restore_candidate_rollback(self, rollback) -> None:
                origin_y, origin_x, scrolls = rollback
                for top, bottom, rows, evicted, generation in reversed(scrolls):
                    history_top = self.history.top
                    if history_top:
                        history_top.pop()
                    if evicted is not None:
                        history_top.appendleft(evicted)
                    for row, line in rows:
                        self.buffer[row] = line
                    self._history_generation = generation
                    self.dirty.update(range(top, bottom + 1))
                self.cursor.y = max(0, min(origin_y, self.lines - 1))
                self.cursor.x = max(0, min(origin_x, self.columns))

            def _normalize_line_clusters(self, line) -> None:
                """Remove orphan stubs and leaders clipped by a cell operation."""
                for column in tuple(line):
                    if column < 0 or column >= self.columns:
                        line.pop(column, None)
                col = 0
                while col < self.columns:
                    char = line[col]
                    data = char.data
                    if data == "":
                        line[col] = char._replace(data=" ")
                        col += 1
                        continue
                    width = max(0, int(_rich_cell_len(data)))
                    if width > 1:
                        if (col + width > self.columns
                                or any(
                                    line[col + offset].data != ""
                                    for offset in range(1, width)
                                )):
                            line[col] = char._replace(data=" ")
                            col += 1
                            continue
                        col += width
                        continue
                    col += 1

            def _normalize_row_clusters(self, row: int) -> None:
                """Canonicalize one visible row after a cell-level mutation."""
                self._normalize_line_clusters(self.buffer[row])
                self.dirty.add(row)

            def _clear_edit_intersections(
                    self, row: int, start: int, end: int) -> None:
                """Blank complete clusters touched by an erase/delete interval."""
                start = max(0, min(int(start), self.columns))
                end = max(start, min(int(end), self.columns))
                if start >= end:
                    return
                blank = self.cursor.attrs._replace(data=" ")
                line = self.buffer[row]
                # The operation itself overwrites every cell in the interval.
                # Only clusters crossing either boundary need extra clearing;
                # probing the whole interval made EL/ED several times slower
                # during full-screen repaint storms.
                positions = set()
                for probe in {start, end - 1}:
                    positions.update(
                        self._intersecting_cell_positions(row, probe, 1))
                for position in positions:
                    line[position] = blank

            def _clear_wide_cluster_at_cursor(self) -> None:
                """ICH inside a wide EGC's stub first erases that whole EGC."""
                row = self.cursor.y
                column = int(self.cursor.x)
                if not (0 <= row < self.lines and 0 <= column < self.columns):
                    return
                line = self.buffer[row]
                if line[column].data != "":
                    return
                positions = self._intersecting_cell_positions(row, column, 1)
                blank = self.cursor.attrs._replace(data=" ")
                for position in positions:
                    line[position] = blank

            def erase_characters(self, count=None) -> None:
                effective = count or 1
                self._clear_edit_intersections(
                    self.cursor.y, self.cursor.x,
                    self.cursor.x + effective)
                super().erase_characters(count)

            def delete_characters(self, count=None) -> None:
                effective = count or 1
                self._clear_edit_intersections(
                    self.cursor.y, self.cursor.x,
                    self.cursor.x + effective)
                super().delete_characters(count)

            def insert_characters(self, count=None) -> None:
                self._clear_wide_cluster_at_cursor()
                super().insert_characters(count)
                # A right shift may clip several stubs from one EGC, whose
                # leader can be multiple cells away from the edge.
                self._normalize_row_clusters(self.cursor.y)

            def erase_in_line(self, how: int = 0, private: bool = False) -> None:
                if how == 0:
                    start, end = self.cursor.x, self.columns
                elif how == 1:
                    start, end = 0, self.cursor.x + 1
                elif how == 2:
                    start, end = 0, self.columns
                else:
                    start = end = 0
                self._clear_edit_intersections(
                    self.cursor.y, start, end)
                super().erase_in_line(how, private)

            def erase_in_display(self, how: int = 0, *args, **kwargs) -> None:
                # The base implementation calls our erase_in_line override for
                # the partial current row; all other affected rows are replaced
                # completely, so no additional full-grid normalization is needed.
                super().erase_in_display(how, *args, **kwargs)

            def _write_cluster(
                    self, cluster: str, *, apply_insert: bool = True,
                    displaced_override=None, rollback_override=None):
                # One EGC is theoretically unbounded (base + infinite combining
                # marks). Cap the retained/grid value so hostile incremental
                # input cannot grow metadata forever or make re-segmentation
                # quadratic without bound.
                if len(cluster) > _EGC_MAX_CODEPOINTS:
                    cluster = cluster[:_EGC_MAX_CODEPOINTS]
                width = max(0, int(_rich_cell_len(cluster)))
                if width <= 0:
                    self._mark_mirror_semantics_hazard()
                    self.invalidate_grapheme_candidate()
                    return None
                if self.columns <= 0 or self.lines <= 0:
                    self.invalidate_grapheme_candidate()
                    return None
                if width > self.columns:
                    # An EGC is atomic, so there is no valid leader/stub layout
                    # when it is wider than the whole viewport. Dropping that
                    # one cluster preserves the grid and leaves following text
                    # at the correct cursor instead of installing a clipped
                    # leader whose declared width crosses rows.
                    self._mark_mirror_semantics_hazard()
                    self.invalidate_grapheme_candidate()
                    return None

                rollback = (
                    rollback_override
                    if rollback_override is not None
                    else (self.cursor.y, self.cursor.x, ())
                )

                # Pending-wrap and a wide EGC that cannot fit both wrap before
                # presentation. The latter matters when a quiet narrow base at
                # the final column later widens through VS16/keycap/RI input.
                needs_wrap = (
                    self.cursor.x == self.columns
                    or self.cursor.x + width > self.columns
                )
                if needs_wrap and _mo.DECAWM in self.mode:
                    scroll_rollback = self._capture_scroll_rollback()
                    self.dirty.add(self.cursor.y)
                    self.carriage_return()
                    self.linefeed()
                    if scroll_rollback is not None:
                        rollback = (
                            rollback[0],
                            rollback[1],
                            rollback[2] + (scroll_rollback,),
                        )
                elif needs_wrap:
                    # xterm/WT do not teleport a double-width glyph leftward
                    # just to make it fit with DECAWM disabled. A pending-wrap
                    # narrow glyph overwrites the final cell; a wide glyph that
                    # cannot start there is ignored.
                    self.cursor.x = min(self.cursor.x, self.columns - 1)
                    if self.cursor.x + width > self.columns:
                        self.invalidate_grapheme_candidate()
                        return None

                row, col = self.cursor.y, self.cursor.x
                insert_mode = bool(apply_insert and _mo.IRM in self.mode)
                displaced = (
                    self._snapshot_displaced_cells(
                        row, col, width, insert_mode=insert_mode)
                    if displaced_override is None else displaced_override
                )

                if insert_mode:
                    self.insert_characters(width)
                    self._normalize_row_clusters(self.cursor.y)

                self._clear_intersecting_cells(row, col, width)
                line = self.buffer[row]
                leader = self.cursor.attrs._replace(data=cluster)
                line[col] = leader
                self._stamp_active_hyperlink(leader)
                if width > 2:
                    self._mark_mirror_semantics_hazard(wide=True)
                for offset in range(1, width):
                    if col + offset < self.columns:
                        line[col + offset] = self.cursor.attrs._replace(data="")
                self.cursor.x = min(col + width, self.columns)
                self.dirty.add(row)
                candidate = (
                    cluster, row, col, width, self.cursor.y, self.cursor.x,
                    displaced, rollback)
                self._egc_candidate = candidate
                return candidate

            def _replace_candidate(self, candidate, cluster: str) -> None:
                (_text, row, col, old_width, _after_row, _after_col,
                 displaced, rollback) = candidate
                new_width = max(0, int(_rich_cell_len(cluster)))
                insert_mode = _mo.IRM in self.mode
                displaced = self._merge_displaced_cells(
                    row, col, new_width,
                    insert_mode=insert_mode, displaced=displaced)
                self._restore_displaced_cells(row, displaced)

                if new_width <= 0 or new_width > self.columns:
                    # The complete EGC could never have been represented. Undo
                    # every provisional prefix, including a wrap/scroll caused
                    # by an earlier narrower prefix, and leave the cursor where
                    # the atomic cluster began.
                    self._mark_mirror_semantics_hazard()
                    self._restore_candidate_rollback(rollback)
                    self.invalidate_grapheme_candidate()
                    return

                reflow = (
                    new_width > 1 and col + new_width > self.columns
                    and _mo.DECAWM in self.mode
                )
                self.cursor.y = row
                clipped = (
                    new_width > 1 and col + new_width > self.columns
                    and _mo.DECAWM not in self.mode
                )
                if clipped:
                    self.cursor.x = min(col, self.columns - 1)
                    self.invalidate_grapheme_candidate()
                    return
                self.cursor.x = col
                self._write_cluster(
                    cluster,
                    # On a reflow the destination row has its own displaced
                    # cells; otherwise retain the cumulative pre-prefix image.
                    displaced_override=None if reflow else displaced,
                    rollback_override=rollback,
                )

            def draw(self, data: str) -> None:
                data = data.translate(
                    self.g1_charset if self.charset else self.g0_charset)
                if not data:
                    return

                candidate = self._egc_candidate
                if candidate is not None and self._candidate_is_live(candidate):
                    old = candidate[0]
                    joined = list(_GRAPHEME_RE.findall(old + data))
                    if joined and joined[0] != old and joined[0].startswith(old):
                        merged = joined[0]
                        consumed = len(merged) - len(old)
                        self._replace_candidate(candidate, merged)
                        data = data[consumed:]
                else:
                    self.invalidate_grapheme_candidate()

                for cluster in _GRAPHEME_RE.findall(data):
                    self._write_cluster(cluster)

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
    from pyte.modes import (  # type: ignore
        DECAWM as _PYTE_DECAWM,
        DECOM as _PYTE_DECOM,
        DECTCEM as _PYTE_DECTCEM,
        IRM as _PYTE_IRM,
        LNM as _PYTE_LNM,
    )
except Exception:  # pragma: no cover - exercised only when dep absent
    _PYTE_DECAWM = None  # type: ignore
    _PYTE_DECOM = None  # type: ignore
    _PYTE_DECTCEM = None  # type: ignore
    _PYTE_IRM = None  # type: ignore
    _PYTE_LNM = None  # type: ignore

_PYTE_GLOBAL_SCREEN_MODES = tuple(
    mode for mode in (_PYTE_DECAWM, _PYTE_DECOM, _PYTE_IRM, _PYTE_LNM)
    if mode is not None
)

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
_COLOR_CACHE_MAX = 128
_COLOR_CACHE: dict[str, Optional[str]] = {}


def _pyte_color(color: Optional[str]) -> Optional[str]:
    """Map a pyte color (name like 'red'/'brown', 6-hex without '#', or
    'default') to a value rich.Style accepts, or None for the terminal default.
    Validated against rich once per name and cached; an unknown/unparseable
    color degrades to default rather than raising — a single bad color must
    never tear down the pane."""
    if not color or color == "default":
        return None
    # Arbitrary 24-bit values are already valid Rich colors; retaining each
    # unique value would make a hostile/animated truecolor stream a global leak.
    if _HEX6.match(color):
        return "#" + color
    if color in _COLOR_CACHE:
        val = _COLOR_CACHE.pop(color)
        _COLOR_CACHE[color] = val
        return val
    name = _PYTE_TO_RICH.get(color, color)
    try:
        from rich.color import Color as _RichColor
        _RichColor.parse(name)
        val: Optional[str] = name
    except Exception:
        val = None
    if len(_COLOR_CACHE) >= _COLOR_CACHE_MAX:
        _COLOR_CACHE.pop(next(iter(_COLOR_CACHE)))
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


# DECSCUSR shapes: 0/1/2 block, 3/4 underline, 5/6 bar (odd = blinking, which the
# outer terminal would have to animate; saikai does not repaint for it).
_CARET_UNDERLINE_SHAPES = (3, 4)
_CARET_BAR_SHAPES = (5, 6)
_CARET_BAR_GLYPH = "▏"        # LEFT ONE EIGHTH BLOCK — a real one-cell bar


def _caret_segment(ch, shape: int):  # -> rich.Segment; render_line only
    """Draw saikai's own caret in the shape the child asked for with DECSCUSR.

    A text grid cannot put a sub-cell bar over a glyph, so the bar substitutes a
    bar character only on an otherwise empty cell and falls back to the block
    over real text rather than destroying the character. The underline promotes
    to a double underline on an already-underscored cell so the caret is always a
    visible CHANGE — the same reason the selection XORs reverse instead of
    setting it. Every shape stays exactly one cell wide. (#native-cursor)"""
    base = _cell_style(ch)
    text = ch.data or " "
    if shape in _CARET_UNDERLINE_SHAPES:
        underscored = bool(getattr(ch, "underscore", False))
        return Segment(text, base + Style(underline=not underscored,
                                          underline2=underscored))
    if shape in _CARET_BAR_SHAPES and not text.strip():
        return Segment(_CARET_BAR_GLYPH, base)
    return Segment(text, base + Style(reverse=True))


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
# Canonical Kitty keyboard encodings. These follow the protocol's functional
# key table rather than the legacy terminfo spellings (notably F1/F2/F4 use CSI
# and F3 is CSI 13~); capability masking below limits which entries are emitted.
_KITTY_FINAL_KEYS = {
    **_MODIFIED_CSI_FINALS,
    "f1": "P", "f2": "Q", "f4": "S",
}
_KITTY_TILDE_KEYS = {
    **_MODIFIED_TILDE_KEYS,
    "f3": "13",
    "f5": "15", "f6": "17", "f7": "18", "f8": "19",
    "f9": "20", "f10": "21", "f11": "23", "f12": "24",
}
_KITTY_U_KEYS = {
    "escape": 27, "enter": 13, "return": 13, "tab": 9, "backspace": 127,
    "caps_lock": 57358, "scroll_lock": 57359, "num_lock": 57360,
    "print_screen": 57361, "pause": 57362, "menu": 57363,
    **{f"f{number}": 57363 + number for number in range(13, 36)},
    "kp_0": 57399, "kp_1": 57400, "kp_2": 57401, "kp_3": 57402,
    "kp_4": 57403, "kp_5": 57404, "kp_6": 57405, "kp_7": 57406,
    "kp_8": 57407, "kp_9": 57408, "kp_decimal": 57409,
    "kp_divide": 57410, "kp_multiply": 57411, "kp_subtract": 57412,
    "kp_add": 57413, "kp_enter": 57414, "kp_equal": 57415,
    "kp_separator": 57416, "kp_left": 57417, "kp_right": 57418,
    "kp_up": 57419, "kp_down": 57420, "kp_pageup": 57421,
    "kp_pagedown": 57422, "kp_home": 57423, "kp_end": 57424,
    "kp_insert": 57425, "kp_delete": 57426,
    "media_play": 57428, "media_pause": 57429,
    "media_play_pause": 57430, "media_reverse": 57431,
    "media_stop": 57432, "media_fast_forward": 57433,
    "media_rewind": 57434, "media_track_next": 57435,
    "media_track_previous": 57436, "media_record": 57437,
    "lower_volume": 57438, "raise_volume": 57439, "mute_volume": 57440,
    "left_shift": 57441, "left_control": 57442, "left_alt": 57443,
    "left_super": 57444, "left_hyper": 57445, "left_meta": 57446,
    "right_shift": 57447, "right_control": 57448, "right_alt": 57449,
    "right_super": 57450, "right_hyper": 57451, "right_meta": 57452,
    "iso_level3_shift": 57453, "iso_level5_shift": 57454,
}
_KITTY_MODIFIER_KEYS = {
    name for name in _KITTY_U_KEYS
    if name.endswith(("_shift", "_control", "_alt", "_super", "_hyper", "_meta"))
    or name.startswith("iso_level")
}
_TEXTUAL_ASCII_UNICODE_NAMES = {
    # Textual shortens these Unicode names when it constructs Key.key.
    "slash": "SOLIDUS",
    "backslash": "REVERSE SOLIDUS",
    "at": "COMMERCIAL AT",
    "minus": "HYPHEN-MINUS",
    "plus": "PLUS SIGN",
    "underscore": "LOW LINE",
    "less_than_sign": "LESS-THAN SIGN",
    "greater_than_sign": "GREATER-THAN SIGN",
}


def _kitty_parameter(code: int, modifier: int, final: str = "u") -> str:
    """Encode one canonical Kitty key, omitting the default modifier."""
    suffix = str(code) if modifier == 1 else f"{code};{modifier}"
    return f"\x1b[{suffix}{final}"


def _kitty_functional_key(base: str, modifier: int) -> Optional[str]:
    """Return a negotiated Kitty encoding for one non-text key."""
    if base in _KITTY_FINAL_KEYS:
        final = _KITTY_FINAL_KEYS[base]
        return f"\x1b[{final}" if modifier == 1 else f"\x1b[1;{modifier}{final}"
    if base in _KITTY_TILDE_KEYS:
        return _kitty_parameter(int(_KITTY_TILDE_KEYS[base]), modifier, "~")
    code = _KITTY_U_KEYS.get(base)
    if code is None:
        return None
    if base in ("enter", "return", "tab", "backspace"):
        return None
    if base in _KITTY_MODIFIER_KEYS:
        return None
    return _kitty_parameter(code, modifier)


def _textual_named_ascii_codepoint(base: str) -> Optional[int]:
    """Recover printable ASCII from the key names Textual emits for Kitty input."""
    unicode_name = _TEXTUAL_ASCII_UNICODE_NAMES.get(
        base, base.replace("_", " ").upper())
    try:
        character = unicodedata.lookup(unicode_name)
    except KeyError:
        return None
    if len(character) == 1 and 0x20 <= ord(character) <= 0x7e:
        return ord(character)
    return None


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


def encode_key(key: str, character: Optional[str], *,
               application_cursor: bool = False,
               kitty_flags: int = 0) -> Optional[str]:
    """Translate a Textual key event into the byte string to write to the PTY,
    or None if the key carries nothing the child should receive.

    Defaults preserve the legacy API. DECCKM changes only unmodified cursor,
    Home, and End keys; negotiated Kitty flags encode keys this terminal can
    describe truthfully from a Textual press event.
    """
    if key == RELEASE_FOCUS_KEY:
        return None
    parts = key.split("+")
    base, modifiers = parts[-1], set(parts[:-1])
    mod = (
        1
        + ("shift" in modifiers)
        + 2 * ("alt" in modifiers)
        + 4 * ("ctrl" in modifiers)
        + 8 * ("super" in modifiers)
        + 16 * ("hyper" in modifiers)
        + 32 * ("meta" in modifiers)
    )
    negotiated = int(kitty_flags or 0) & _KITTY_KBD_SUPPORTED_FLAGS
    if negotiated:
        functional = _kitty_functional_key(base, mod)
        if functional is not None:
            return functional

    # DECCKM applies only to the legacy protocol.  Negotiated Kitty functional
    # keys use their canonical CSI form and must not regress to SS3.
    if application_cursor and not modifiers and base in _MODIFIED_CSI_FINALS:
        return f"\x1bO{_MODIFIED_CSI_FINALS[base]}"

    codepoint = None
    if base in ("enter", "return"):
        codepoint = 13
    elif len(base) == 1 and base.isprintable():
        codepoint = ord(base)
    elif character and len(character) == 1 and character.isprintable():
        codepoint = ord(character)
    elif negotiated:
        codepoint = _textual_named_ascii_codepoint(base)
    if negotiated and codepoint is not None:
        if modifiers and (
                modifiers.intersection({
                    "alt", "ctrl", "super", "hyper", "meta"})
                or base in ("enter", "return")):
            return f"\x1b[{codepoint};{mod}u"

    mapped = _KEYMAP.get(key)
    if mapped is not None:
        return mapped
    if modifiers and modifiers <= {"shift", "alt", "ctrl"}:
        # Textual normalizes host-terminal input; emit the standard xterm
        # modifier form expected by interactive children, independent of the
        # outer terminal emulator. Modifier parameter: 1 + Shift + 2*Alt + 4*Ctrl.
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
        if character and character.isprintable():
            return "\x1b" + character
        if len(rest) == 1:
            return "\x1b" + rest
        return None   # alt+<named> (arrows etc.) aren't readline word ops
    # Printable single char (letters, digits, punctuation, space, IME unicode).
    if character and character.isprintable():
        return character
    return None


# Runtime alternate-buffer switching is token-driven and uses distinct MAIN/ALT
# screen+stream pairs; 47/1047 are normalized to saikai's exact-main-restore
# 1049 contract.
# Internal noncharacter framing for presentation-only grapheme boundaries.
# Raw occurrences are escaped, so the marker never leaks to pyte or the mirror.
_EGC_BOUNDARY_MARKER = "\ufdd0"
_EGC_BOUNDARY_TOKEN = _EGC_BOUNDARY_MARKER + "B"
_EGC_LITERAL_TOKEN = _EGC_BOUNDARY_MARKER + "T"


def _encode_presentation_data(text: str) -> str:
    return text.replace(_EGC_BOUNDARY_MARKER, _EGC_LITERAL_TOKEN)


def _presentation_fragments(text: str):
    """Yield (decoded_text, is_boundary) from internal presentation framing."""
    out = []
    index = 0
    while index < len(text):
        char = text[index]
        if char != _EGC_BOUNDARY_MARKER:
            out.append(char)
            index += 1
            continue
        if out:
            yield "".join(out), False
            out.clear()
        suffix = text[index + 1:index + 2]
        if suffix == "T":
            out.append(_EGC_BOUNDARY_MARKER)
            index += 2
        elif suffix == "B":
            yield "", True
            index += 2
        else:
            # A fail-open truncation may cut the two-character framing token.
            # Drop the internal marker rather than rendering a noncharacter.
            index += 1
    if out:
        yield "".join(out), False
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
# these to negotiate key reporting. saikai tracks the state and honors the
# supported disambiguation flag while keeping negotiation bytes out of the
# display grid. (Plain CSI u = SCO restore-cursor has no private marker, so it is
# NOT stripped.)
_KITTY_KBD_RE = re.compile(r"\x1b\[[<>=?][0-9;:]*u")
_KITTY_KBD_STACK_MAX = 16
# Textual supplies press events, a delivered character, and modifier names, so
# saikai can implement disambiguation (1). Its public Key event deliberately
# collapses keypad codes to their non-keypad names (for example keypad 0 -> "0"
# and keypad Enter -> "enter"), so advertising report-all-keys (8) would promise
# physical-key identity that encode_key cannot reconstruct. Event types (2),
# alternate keys (4), report-all (8), and associated text (16) stay masked.
_KITTY_KBD_SUPPORTED_FLAGS = 1
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
_DEC_PRIVATE_RE = re.compile(r"(?:\x1b\[|\x9b)\?([0-9;]+)([hl])")
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
_CSI_INTRO_RE = r"(?:\x1b\[|\x9b)"
_OSC_INTRO_RE = r"(?:\x1b\]|\x9d)"
_OSC_TERM_RE = r"(?:\x07|\x1b\\|\x9c)"
_DA_RE = re.compile(_CSI_INTRO_RE + r"0?c")              # Primary Device Attributes
_DA2_RE = re.compile(_CSI_INTRO_RE + r">0?c")            # Secondary DA (vim t_RV, tmux)
_DSR_RE = re.compile(_CSI_INTRO_RE + r"(?:[56]|\?6)n")  # standard DSR + DECXCPR
_DECRQM_RE = re.compile(_CSI_INTRO_RE + r"\?(\d+)\$p")   # DECRQM (mode support query)
_XTVERSION_RE = re.compile(_CSI_INTRO_RE + r">0?q")      # XTVERSION (terminal name/version)
_OSC_COLOR_Q_RE = re.compile(                            # OSC 10/11 fg/bg color query
    _OSC_INTRO_RE + r"(1[01]);\?(" + _OSC_TERM_RE + r")")
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
    + [_CSI_INTRO_RE + r"=0?c",             # tertiary DA
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
# UTF-8-byte ceiling for all accepted but not-yet-written pane input. A child
# that stops reading stdin must not turn the serialized writer into an unbounded
# buffer. The in-flight item counts too, so retained input is bounded even while
# the backend's write call is blocked.
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
    "1004": "_focus_reporting",
    "1006": "_mouse_sgr",
    "2004": "_bracketed_paste",
}
_DECRQM_ALT_SCREEN = ("47", "1047", "1049")


# DCS/APC/PM/SOS strings are opaque terminal-private payloads. pyte does not
# implement them and draws their bodies into the grid, so strip them before pyte
# and the mirror. OSC and DCS retain their historical defensive BEL terminator;
# APC/PM/SOS require ST. (#dcs-scrub)
_DCS_MAX_DROP = 4 * 1024 * 1024
_VT_TOKENIZER_MAX_CARRY = 64 * 1024
_OPAQUE_STRING_KINDS = frozenset(("dcs", "apc", "pm", "sos", "ignored"))
_STRING_C1_INTRODUCERS = frozenset(("\x9b", "\x9d", "\x90", "\x9f", "\x9e", "\x98"))


@dataclass(frozen=True)
class VTToken:
    """One decoded VT protocol unit, retaining the original characters exactly."""

    kind: str
    raw: str
    parameters: str = ""
    intermediates: str = ""
    final: str = ""
    # True when an invalid/over-cap control unit failed open as data. Feeding
    # its raw ESC/C0 bytes back into pyte/xterm would reinterpret the control and
    # defeat fail-open, so the presentation layer renders those bytes visibly.
    literal: bool = False


class VTTokenizer:
    """Provider-neutral incremental tokenizer for decoded terminal text.

    The tokenizer does not interpret a sequence; it only identifies its VT
    grammar and preserves its raw characters. Incomplete units are retained for
    the next PTY read. A malformed or overlong unit is emitted as ordinary data
    rather than being retained forever.
    """

    def __init__(self, max_carry=_VT_TOKENIZER_MAX_CARRY,
                 max_dropped_string=_DCS_MAX_DROP):
        self.max_carry = max(1, int(max_carry))
        self.max_dropped_string = max(0, int(max_dropped_string))
        self.carry = ""
        # Bounded accounting for malformed control strings that have failed open.
        # It is diagnostic state only; the raw data is emitted, never discarded.
        self.dropped_string_chars = 0
        # An over-cap unit has already been exposed as text. Retain only its
        # grammar kind so a following chunk's terminator remains text too.
        self._fail_open_kind: str | None = None
        self._fail_open_pending_esc = False

    @staticmethod
    def _is_parameter(ch: str) -> bool:
        return "\x30" <= ch <= "\x3f"

    @staticmethod
    def _is_intermediate(ch: str) -> bool:
        return "\x20" <= ch <= "\x2f"

    @staticmethod
    def _is_final(ch: str) -> bool:
        return "\x40" <= ch <= "\x7e"

    @staticmethod
    def _is_escape_final(ch: str) -> bool:
        return "\x30" <= ch <= "\x7e"

    @staticmethod
    def _is_control(ch: str) -> bool:
        return ord(ch) < 0x20 or 0x7f <= ord(ch) <= 0x9f

    def _emit_or_fail_open(self, token: VTToken, out: list[VTToken]) -> None:
        """Emit a complete protocol token only when its raw text is bounded."""
        if len(token.raw) > self.max_carry:
            out.append(VTToken("text", token.raw, literal=True))
        else:
            out.append(token)

    def _retain_or_fail_open(self, raw: str, out: list[VTToken], *, string=False,
                             kind: str) -> None:
        """Carry *raw* only while it is bounded; otherwise expose it as text."""
        if len(raw) <= self.max_carry:
            self.carry = raw
            return
        if string:
            self.dropped_string_chars = min(
                self.max_dropped_string, self.dropped_string_chars + len(raw))
        out.append(VTToken("text", raw, literal=True))
        self._fail_open_kind = kind
        self._fail_open_pending_esc = kind in (
            "osc", "dcs", "apc", "pm", "sos") and raw.endswith("\x1b")

    def _drain_fail_open(self, text: str, out: list[VTToken]) -> int:
        """Emit text through the terminator of an already fail-open unit.

        The original prefix was emitted before this call, so this stores only a
        small grammar marker. That prevents a later BEL/ST/final byte from being
        reinterpreted as a fresh control sequence without retaining the prefix.
        """
        kind = self._fail_open_kind
        if not kind:
            return 0
        if kind in ("osc", "dcs", "apc", "pm", "sos") and self._fail_open_pending_esc:
            if text.startswith("\\"):
                out.append(VTToken("text", "\\", literal=True))
                self._fail_open_kind = None
                self._fail_open_pending_esc = False
                return 1
            if text:
                self._fail_open_pending_esc = False
        pos = 0
        end = None
        while pos < len(text):
            ch = text[pos]
            if kind == "csi":
                if self._is_final(ch) or self._is_control(ch):
                    end = pos + 1
                    break
            elif kind == "esc":
                if self._is_escape_final(ch) or self._is_control(ch):
                    end = pos + 1
                    break
            else:  # OSC / opaque strings
                if ch == "\x9c" or (ch == "\x07" and kind in ("osc", "dcs")):
                    end = pos + 1
                    break
                if ch in ("\x18", "\x1a"):
                    end = pos + 1
                    break
                if ch == "\x1b" and pos + 1 < len(text) and text[pos + 1] == "\\":
                    end = pos + 2
                    break
                if ch == "\x1b" or ch in _STRING_C1_INTRODUCERS:
                    end = pos
                    break
            pos += 1
        if end is None:
            if text:
                out.append(VTToken("text", text, literal=True))
                self._fail_open_pending_esc = (
                    kind in ("osc", "dcs", "apc", "pm", "sos")
                    and text.endswith("\x1b"))
            return len(text)
        if end:
            out.append(VTToken("text", text[:end], literal=True))
        self._fail_open_kind = None
        self._fail_open_pending_esc = False
        return end

    def _parse_csi(self, text: str, start: int, body: int,
                   out: list[VTToken], *, introducer: str | None = None
                   ) -> int | None:
        """Emit a CSI at *start*, or retain an incomplete unit and return None."""
        introducer = text[start:body] if introducer is None else introducer
        pos = body
        parameters: list[str] = []
        intermediates: list[str] = []
        in_intermediates = False
        while pos < len(text):
            ch = text[pos]
            # Preserve the existing cancellation contract: expose the incomplete
            # prefix, then let the outer tokenizer execute CAN/SUB or reparse ESC.
            if ch in ("\x18", "\x1a", "\x1b"):
                partial = (
                    introducer + "".join(parameters) + "".join(intermediates))
                out.append(VTToken("text", partial, literal=True))
                return pos
            # ECMA-48 C0 controls execute without leaving CSI entry/parameter/
            # intermediate state. They therefore precede the eventual CSI token.
            if ord(ch) < 0x20:
                out.append(VTToken("control", ch))
                pos += 1
                continue
            if not in_intermediates and self._is_parameter(ch):
                parameters.append(ch)
                pos += 1
                continue
            if self._is_intermediate(ch):
                in_intermediates = True
                intermediates.append(ch)
                pos += 1
                continue
            if self._is_final(ch):
                params = "".join(parameters)
                inters = "".join(intermediates)
                self._emit_or_fail_open(VTToken(
                    "csi", introducer + params + inters + ch,
                    params, inters, ch), out)
                return pos + 1
            # An invalid byte cannot complete this CSI. Keep no poison for a
            # later PTY read: return its normalized raw prefix as ordinary data.
            partial = introducer + "".join(parameters) + "".join(intermediates)
            out.append(VTToken("text", partial, literal=True))
            return pos
        raw = introducer + "".join(parameters) + "".join(intermediates)
        self._retain_or_fail_open(raw, out, kind="csi")
        return None

    def _parse_string(self, text: str, start: int, body: int, kind: str,
                      out: list[VTToken], *, introducer: str | None = None
                      ) -> int | None:
        """Emit one complete control string, or carry its bounded suffix.

        CAN/SUB cancel a string. A non-ST ESC or a C1 control introducer ends
        it immediately and is reparsed by the outer tokenizer, matching xterm.
        """
        introducer = text[start:body] if introducer is None else introducer

        def raw_through(end: int) -> str:
            return introducer + text[body:end]

        pos = body
        while pos < len(text):
            ch = text[pos]
            if ch == "\x9c" or (ch == "\x07" and kind in ("osc", "dcs")):
                pos += 1
                self._emit_or_fail_open(VTToken(kind, raw_through(pos)), out)
                return pos
            if ch in ("\x18", "\x1a"):
                pos += 1
                self._emit_or_fail_open(VTToken("ignored", raw_through(pos)), out)
                return pos
            if ch == "\x1b":
                if pos + 1 == len(text):
                    self._retain_or_fail_open(
                        raw_through(len(text)), out, string=True, kind=kind)
                    return None
                if text[pos + 1] == "\\":
                    pos += 2
                    self._emit_or_fail_open(VTToken(kind, raw_through(pos)), out)
                    return pos
                self._emit_or_fail_open(
                    VTToken("ignored", raw_through(pos)), out)
                return pos
            if ch in _STRING_C1_INTRODUCERS:
                self._emit_or_fail_open(
                    VTToken("ignored", raw_through(pos)), out)
                return pos
            pos += 1
        self._retain_or_fail_open(
            raw_through(len(text)), out, string=True, kind=kind)
        return None

    def _parse_escape(self, text: str, start: int,
                      out: list[VTToken]) -> int | None:
        """Parse ESC plus its optional intermediate bytes and final byte."""
        pos = start + 1
        intermediates: list[str] = []
        while pos < len(text):
            ch = text[pos]
            # CAN/SUB cancel and a fresh ESC restarts parsing at its own position.
            if ch in ("\x18", "\x1a", "\x1b"):
                partial = "\x1b" + "".join(intermediates)
                out.append(VTToken("text", partial, literal=True))
                return pos
            # As in CSI state, ordinary C0 controls execute and ESC parsing
            # continues in the same intermediate state.
            if ord(ch) < 0x20:
                out.append(VTToken("control", ch))
                pos += 1
                continue
            if not intermediates and ch == "[":
                return self._parse_csi(
                    text, start, pos + 1, out, introducer="\x1b[")
            if not intermediates and ch == "]":
                return self._parse_string(
                    text, start, pos + 1, "osc", out, introducer="\x1b]")
            if not intermediates and ch == "P":
                return self._parse_string(
                    text, start, pos + 1, "dcs", out, introducer="\x1bP")
            if not intermediates and ch in ("_", "^", "X"):
                return self._parse_string(
                    text, start, pos + 1,
                    {"_": "apc", "^": "pm", "X": "sos"}[ch], out,
                    introducer="\x1b" + ch)
            if self._is_intermediate(ch):
                intermediates.append(ch)
                pos += 1
                continue
            if self._is_escape_final(ch):
                inters = "".join(intermediates)
                self._emit_or_fail_open(VTToken(
                    "esc", "\x1b" + inters + ch,
                    intermediates=inters, final=ch), out)
                return pos + 1
            partial = "\x1b" + "".join(intermediates)
            out.append(VTToken("text", partial, literal=True))
            return pos
        self._retain_or_fail_open(
            "\x1b" + "".join(intermediates), out, kind="esc")
        return None

    def feed(self, text: str) -> list[VTToken]:
        """Tokenize one decoded PTY chunk, retaining only an incomplete suffix."""
        if not text and not self.carry:
            return []
        text = self.carry + (text or "")
        self.carry = ""
        out: list[VTToken] = []
        pos = self._drain_fail_open(text, out)
        text_start = pos

        def emit_text(end: int) -> None:
            nonlocal text_start
            if end > text_start:
                out.append(VTToken("text", text[text_start:end]))

        while pos < len(text):
            ch = text[pos]
            if ch == "\x1b":
                emit_text(pos)
                next_pos = self._parse_escape(text, pos, out)
                if next_pos is None:
                    break
                pos = text_start = next_pos
                continue
            if ch == "\x9b":
                emit_text(pos)
                next_pos = self._parse_csi(text, pos, pos + 1, out)
                if next_pos is None:
                    break
                pos = text_start = next_pos
                continue
            if ch == "\x9d":
                emit_text(pos)
                next_pos = self._parse_string(text, pos, pos + 1, "osc", out)
                if next_pos is None:
                    break
                pos = text_start = next_pos
                continue
            if ch == "\x90":
                emit_text(pos)
                next_pos = self._parse_string(text, pos, pos + 1, "dcs", out)
                if next_pos is None:
                    break
                pos = text_start = next_pos
                continue
            if ch in ("\x9f", "\x9e", "\x98"):
                emit_text(pos)
                next_pos = self._parse_string(
                    text, pos, pos + 1,
                    {"\x9f": "apc", "\x9e": "pm", "\x98": "sos"}[ch], out)
                if next_pos is None:
                    break
                pos = text_start = next_pos
                continue
            if self._is_control(ch):
                emit_text(pos)
                out.append(VTToken("control", ch))
                pos = text_start = pos + 1
                continue
            pos += 1
        else:
            emit_text(len(text))
        return out

    def flush(self) -> list[VTToken]:
        """Fail an incomplete EOF suffix open once, then retire parser state."""
        raw = self.carry
        self.carry = ""
        self._fail_open_kind = None
        self._fail_open_pending_esc = False
        if not raw:
            return []
        return [VTToken("text", raw, literal=True)]


def _mirror_alt_contract_token(token: VTToken) -> str:
    """Map legacy alternate-buffer switches to saikai's 1049 semantics.

    Local 47/1047 deliberately preserve MAIN's cursor just like 1049. Browser
    xterm implements their historical no-save behavior, so forwarding them
    verbatim would make a connected mirror diverge after the first switch.
    """
    if (token.kind != "csi" or token.intermediates
            or token.final not in ("h", "l")
            or not token.parameters.startswith("?")):
        return token.raw
    modes = token.parameters[1:].split(";")
    if not any(mode in ("47", "1047") for mode in modes):
        return token.raw
    mapped = ["1049" if mode in ("47", "1047") else mode for mode in modes]
    introducer = "\x9b" if token.raw.startswith("\x9b") else "\x1b["
    return introducer + "?" + ";".join(mapped) + token.final


def _literalize_control_data(text: str) -> str:
    """Make fail-open VT bytes visible without executing them a second time."""
    out = []
    for char in text:
        code = ord(char)
        if code < 0x20:
            out.append(chr(0x2400 + code))
        elif code == 0x7f:
            out.append("\u2421")
        elif 0x80 <= code <= 0x9f:
            out.append(f"<{code:02X}>")
        else:
            out.append(char)
    return "".join(out)


def _osc_parts(token: VTToken) -> tuple[str, str, str]:
    """Return (code, payload, terminator) for one complete OSC token."""
    raw = token.raw
    body = raw[2:] if raw.startswith("\x1b]") else raw[1:]
    if body.endswith("\x1b\\"):
        body, terminator = body[:-2], "\x1b\\"
    elif body.endswith("\x9c"):
        body, terminator = body[:-1], "\x9c"
    elif body.endswith("\x07"):
        body, terminator = body[:-1], "\x07"
    else:
        return "", "", ""
    code, sep, payload = body.partition(";")
    return (code, payload if sep else "", terminator)


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

    def _append(self, text):
        if text:
            self._parts.append(text)
            self._chars += len(text)

    def _release(self, reason=None, bypass=False):
        text = "".join(self._parts)
        self._parts = []
        self._chars = 0
        self._opened_at = 0.0
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

        # PTY ownership is one versioned tuple. The reader EOF path and the UI
        # kill path race through _detach_owned_pty; exactly one generation wins
        # cleanup, so a late callback can never signal a replacement/reused PID.
        self._lifecycle_lock = threading.Lock()
        self._lifecycle_generation = 0
        self._lifecycle_retiring_generation = None
        self._lifecycle_eof_events = {}
        self._pty = None
        self._pid: Optional[int] = None
        self._screen = None          # currently active pyte screen
        self._stream = None          # stream paired with the active screen
        self._main_screen = None
        self._main_stream = None
        self._alt_screen = None
        self._alt_stream = None
        self._alt = AltScreenTracker()
        self._scroll = 0             # lines scrolled back (0 = live bottom)
        self._scroll_snapshot = None # immutable combined history/live view while pinned
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
        self._bracketed_paste = False  # ?2004: wrap multi-line paste input
        self._fwd_buttons = set()      # forwarded buttons currently held (a drag in progress)
        self._fwd_captured = False     # captured the mouse for the current forwarded gesture?
        self._fwd_last = (1, 1)        # last forwarded (col,row) — for a synthetic release
        self._autoscroll_dir = 0     # drag at top/bottom edge: +1 up / -1 down / 0
        self._autoscroll_timer = None  # ticks while edge-dragging (#drag-autoscroll)
        self._sel_prev_frozen = False
        self._frozen_buf = None      # snapshot of the displayed rows while frozen
                                     # (the reader keeps mutating screen.buffer, so
                                     # render + copy must read the FROZEN frame)
        self._vt_tokenizer = VTTokenizer()  # the only decoded-stream carry/parser
        # Every PTY write is serialized by one persistent worker. UI and reader
        # threads only append to this bounded deque; the condition is never held
        # across the backend's potentially blocking write().
        self._write_condition = threading.Condition()
        self._write_q = deque()
        self._write_queued_bytes = 0
        self._write_inflight_bytes = 0
        self._write_pending_bytes = 0
        self._write_drop_count = 0
        self._write_drop_bytes = 0
        self._write_drop_reason = ""
        self._write_accepting = False
        self._write_stop = False
        self._write_closed = False
        self._writer: Optional[threading.Thread] = None
        self._writer_generation = None
        self._writer_workers_started = 0
        self._sync_output = _SynchronizedOutputStager()
        # The reader and one persistent deadline worker share only the stager.
        # This lock is never nested with self._lock; units are fed after release.
        self._sync_lock = threading.RLock()
        # Serializes reader batches, deadline expiry, and EOF flush so a timeout
        # cannot overtake presentation already extracted by the reader.
        self._sync_dispatch_lock = threading.RLock()
        self._sync_deadline_condition = threading.Condition()
        self._sync_deadline_generation = 0
        self._sync_deadline_at: Optional[float] = None
        self._sync_deadline_opened_at: Optional[float] = None
        self._sync_deadline_stop = False
        self._sync_deadline_worker: Optional[threading.Thread] = None
        self._sync_deadline_workers_started = 0
        self._app_cursor = False     # ?1 DECCKM — replayed in the mirror seed so a
                                     # pane-view browser encodes arrows correctly (#pane-direct)
        self._cursor_visible = True  # ?25 DECTCEM, explicit so held sync frames query truthfully
        self._alt_screen_mode = False  # ?47/?1047/?1049 DECRQM state at stream position
        self._kitty_keyboard_flags = {False: 0, True: 0}
        self._kitty_keyboard_stacks = {False: [], True: []}
        self._osc8_active = None
        self._mirror_mode_reseed_pending = False
        self._hw_cursor_visible: Optional[bool] = None  # last ?25 visibility we wrote
        self._hw_cursor_shape = 0    # DECSCUSR shape last applied to the outer driver
        self._cursor_style = 0       # child-requested DECSCUSR shape (0..6)
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
        self._input_status_deadline = 0.0
        self._input_status_deadline_seen = True
        self._input_status_generation = 0
        self._input_status_timer = None

    # ── geometry helpers ──────────────────────────────────────────────────────
    def _dims(self) -> tuple[int, int]:
        """Current (rows, cols). When the widget has NO real size yet — an inactive
        TabbedContent pane (ContentSwitcher sets display:none → size 0) or pre-layout
        — fall back to 80x24 instead of the old 2x2 floor: a child spawned into a 2x2
        PTY can't render its UI, so its trust-gate / prompts never appear and the
        status classifier can't see "waiting" until the tab is activated and resized
        (the "restored pane isn't flagged needs-input until I select it" bug). 80x24
        lets the child render + be classified while still backgrounded; on_resize
        corrects to the exact size when the tab is shown. (#inactive-pane-size)

        The fallback applies PER AXIS and only to an axis with no size at all. A
        real but small pane must be reported honestly: telling a child it has 80
        columns inside a 7-column pane makes it wrap for a screen that does not
        exist and place its prompt on rows the widget never paints."""
        w = int(self.size.width or 0)
        h = int(self.size.height or 0)
        if w <= 0:                               # inactive/hidden pane or pre-layout
            w = 80
        if h <= 0:
            h = 24
        return max(h, 1), max(w, 1)              # pyte needs at least one cell

    def _create_screen_pair(self, rows: int, cols: int) -> None:
        """Create independent main/alternate grids and select the main grid."""
        # HistoryScreen keeps scrolled-off MAIN lines in .history.top. The
        # alternate grid deliberately has no terminal scrollback; fullscreen
        # children own their own viewport and every entry starts clean.
        main = _HistoryScreenBase(cols, rows, history=SCROLLBACK_LINES)
        alternate = _HistoryScreenBase(cols, rows, history=0)
        self._main_screen = main
        self._main_stream = pyte.Stream(main)
        self._alt_screen = alternate
        self._alt_stream = pyte.Stream(alternate)
        self._screen = main
        self._stream = self._main_stream
        self._alt.in_alt = False
        self._alt_screen_mode = False

    def _ensure_screen_pair_locked(self) -> None:
        """Install a missing pair for legacy/headless objects (lock held)."""
        screen = getattr(self, "_screen", None)
        stream = getattr(self, "_stream", None)
        if screen is None:
            return
        main = getattr(self, "_main_screen", None)
        alternate = getattr(self, "_alt_screen", None)
        if main is None:
            self._main_screen = screen
            self._main_stream = stream if stream is not None else pyte.Stream(screen)
        if alternate is None:
            alternate = _HistoryScreenBase(
                screen.columns, screen.lines, history=0)
            self._alt_screen = alternate
            self._alt_stream = pyte.Stream(alternate)

    @staticmethod
    def _invalidate_screen_grapheme(screen) -> None:
        invalidator = getattr(screen, "invalidate_grapheme_candidate", None)
        if invalidator is not None:
            invalidator()

    def _switch_alt_screen_locked(self, enabled: bool) -> bool:
        """Switch the active screen at one stream position (lock held)."""
        self._ensure_screen_pair_locked()
        current = bool(getattr(getattr(self, "_alt", None), "in_alt", False))
        if enabled == current:
            return False
        main = self._main_screen
        alternate = self._alt_screen
        if main is None or alternate is None:
            return False
        self._invalidate_screen_grapheme(main)
        self._invalidate_screen_grapheme(alternate)
        title = getattr(self._screen, "title", "") or ""
        icon_name = getattr(self._screen, "icon_name", "") or ""
        if enabled:
            source = self._screen
            # xterm's 1049-like entry uses the active MAIN buffer's one DECSC
            # slot. It therefore overwrites an older explicit ESC 7 savepoint
            # with the entry cursor/rendition. The main grid itself remains
            # untouched, so leaving ALT still restores that exact live state.
            # The idempotence return above ensures a repeated SET cannot
            # overwrite the slot again or clear the already-active ALT buffer.
            source.save_cursor()
            alternate.reset()
            # A 1049-style switch clears the alternate grid, but begins it with
            # the current cursor/rendition/charset. Terminal-global modes remain
            # in force; DECSTBM margins are buffer-local and reset with ALT.
            # The two cursor objects then diverge so
            # leaving ALT restores MAIN exactly.
            for mode in _PYTE_GLOBAL_SCREEN_MODES:
                if mode in getattr(source, "mode", ()):
                    alternate.mode.add(mode)
                else:
                    alternate.mode.discard(mode)
            try:
                alternate.cursor.y = max(
                    0, min(int(source.cursor.y), alternate.lines - 1))
                alternate.cursor.x = max(
                    0, min(int(source.cursor.x), alternate.columns))
                alternate.cursor.attrs = source.cursor.attrs
                alternate.g0_charset = source.g0_charset
                alternate.g1_charset = source.g1_charset
                alternate.charset = source.charset
            except Exception:
                pass
            alternate.title = title
            alternate.icon_name = icon_name
            self._screen = alternate
            self._stream = self._alt_stream
        else:
            # Window/icon titles are terminal-global even though cells/cursors
            # are buffer-local, so retain a title set while alternate was active.
            main.title = title
            main.icon_name = icon_name
            self._screen = main
            self._stream = self._main_stream
        try:
            visible = bool(getattr(self, "_cursor_visible", True))
            if _PYTE_DECTCEM is not None:
                if visible:
                    self._screen.mode.add(_PYTE_DECTCEM)
                else:
                    self._screen.mode.discard(_PYTE_DECTCEM)
            self._screen.cursor.hidden = not visible
        except Exception:
            pass
        self._alt.in_alt = enabled
        self._alt_screen_mode = enabled
        self._scroll = 0
        self._scroll_snapshot = None
        return True

    def _apply_ris_locked(self) -> None:
        """Apply RIS (ESC c) to saikai-owned terminal state (lock held).

        pyte's own reset covers the ACTIVE grid: buffer, margins, its mode set,
        charsets, tabstops, title. Everything saikai tracks on the child's behalf
        is invisible to it, so without this a hard reset left saikai answering
        DECRQM with modes the child had just cleared, kept the alternate buffer
        active, and re-seeded a joining browser with the stale modes — while the
        mirror's xterm.js performed a real full reset on the same byte.

        Runs at the RIS stream position and BEFORE the pyte feed, so the reset
        lands on the primary buffer. (#ris)"""
        # Primary buffer becomes active again, and BOTH grids are cleared. The
        # caller's feed resets the (now primary) active screen; the alternate is
        # never fed, so clear it here.
        self._switch_alt_screen_locked(False)
        alternate = getattr(self, "_alt_screen", None)
        if alternate is not None:
            self._invalidate_screen_grapheme(alternate)
            try:
                alternate.reset()
            except Exception:
                pass
        # _switch_alt_screen_locked is idempotent, so it returns early — and
        # skips this — when the RIS arrived on the primary buffer. The pinned
        # view must be dropped either way: pyte's reset empties the history the
        # offset points into.
        self._scroll = 0
        self._scroll_snapshot = None

        self._app_cursor = False            # ?1 DECCKM
        self._cursor_visible = True         # ?25 DECTCEM defaults to SET
        self._mouse_click = False           # ?1000
        self._mouse_btn_motion = False      # ?1002
        self._mouse_any_motion = False      # ?1003
        self._mouse_sgr = False             # ?1006
        self._mouse_reporting = False
        self._focus_reporting = False       # ?1004
        self._bracketed_paste = False       # ?2004
        self._cursor_style = 0              # DECSCUSR back to the host default
        self._kitty_keyboard_flags = {False: 0, True: 0}
        self._kitty_keyboard_stacks = {False: [], True: []}
        self._osc8_active = None
        for screen in (getattr(self, "_main_screen", None), alternate):
            if screen is None:
                continue
            try:
                screen.savepoints.clear()   # pyte's reset keeps the DECSC slot
            except Exception:
                pass
        # The browser reset itself on the same byte; re-seed so a client that
        # joins afterwards gets the post-reset modes rather than the old ones.
        self._mirror_mode_reseed_pending = True

    def _sync_global_screen_state_locked(self, token: "VTToken") -> None:
        """Copy global mode results without moving the inactive saved cursor."""
        if token.kind != "csi" or token.intermediates:
            return
        sync_modes = (
            token.final in ("h", "l")
            and not token.parameters.startswith((">", "<", "="))
        )
        if not sync_modes:
            return
        self._ensure_screen_pair_locked()
        active = self._screen
        inactive = (
            self._main_screen if active is self._alt_screen
            else self._alt_screen
        )
        if active is None or inactive is None:
            return
        for mode in _PYTE_GLOBAL_SCREEN_MODES:
            if mode in getattr(active, "mode", ()):
                inactive.mode.add(mode)
            else:
                inactive.mode.discard(mode)

    # ── lifecycle ─────────────────────────────────────────────────────────────
    def _ensure_lifecycle_state(self) -> None:
        """Initialize ownership fields for lightweight ``__new__`` test panes."""
        if getattr(self, "_lifecycle_lock", None) is not None:
            return
        self._lifecycle_lock = threading.Lock()
        self._lifecycle_generation = int(
            getattr(self, "_lifecycle_generation", 0))
        self._lifecycle_retiring_generation = getattr(
            self, "_lifecycle_retiring_generation", None)
        self._lifecycle_eof_events = getattr(
            self, "_lifecycle_eof_events", {})
        if not hasattr(self, "_pty"):
            self._pty = None
        if not hasattr(self, "_pid"):
            self._pid = None

    def _attach_pty(self, pty, pid=None) -> int:
        """Attach a newly spawned PTY and return its monotonically increasing id."""
        self._ensure_lifecycle_state()
        with self._lifecycle_lock:
            if self._pty is not None or self._pid is not None:
                raise RuntimeError("cannot replace an attached PTY generation")
            if self._lifecycle_retiring_generation is not None:
                raise RuntimeError(
                    "cannot attach a PTY while the previous generation retires")
            self._lifecycle_generation += 1
            self._pty = pty
            self._pid = pid
            self._lifecycle_eof_events[
                self._lifecycle_generation] = threading.Event()
            return self._lifecycle_generation

    def _lifecycle_snapshot(self):
        """Return the current ``(pty, pid, generation)`` atomically."""
        self._ensure_lifecycle_state()
        with self._lifecycle_lock:
            return (
                self._pty,
                self._pid,
                self._lifecycle_generation,
            )

    def _detach_owned_pty(self, pty, generation):
        """Detach only the exact generation owned by a reader/kill caller."""
        self._ensure_lifecycle_state()
        with self._lifecycle_lock:
            if (self._pty is not pty
                    or self._lifecycle_generation != generation):
                return None
            pid = self._pid
            if pty is None and pid is None:
                return None
            self._pty = None
            self._pid = None
            self._lifecycle_retiring_generation = generation
            return pty, pid, generation

    def _mark_generation_natural_eof(self, generation: int) -> None:
        """Publish EOF even when kill won the detach race for this generation."""
        self._ensure_lifecycle_state()
        with self._lifecycle_lock:
            event = self._lifecycle_eof_events.get(generation)
        if event is not None:
            event.set()

    def _generation_eof_event(self, generation: int):
        self._ensure_lifecycle_state()
        with self._lifecycle_lock:
            return self._lifecycle_eof_events.get(generation)

    def _generation_is_retiring(self, generation: int) -> bool:
        self._ensure_lifecycle_state()
        with self._lifecycle_lock:
            return self._lifecycle_retiring_generation == generation

    def _finish_pty_retirement(self, generation: int) -> None:
        """Release the attach fence after old-reader shared cleanup is complete."""
        self._ensure_lifecycle_state()
        with self._lifecycle_lock:
            if self._lifecycle_retiring_generation == generation:
                self._lifecycle_retiring_generation = None
                self._lifecycle_eof_events.pop(generation, None)

    def on_mount(self) -> None:
        rows, cols = self._dims()
        try:
            self._create_screen_pair(rows, cols)
        except Exception as e:  # pragma: no cover
            self._fail(f"pyte init failed: {e!r}")
            return
        try:
            self._spawn(rows, cols)
        except Exception as e:
            self._fail(f"spawn failed: {e!r}")
            return
        pty, _pid, generation = self._lifecycle_snapshot()
        if pty is None:
            self._fail("spawn failed: backend returned no PTY")
            return
        self._reader = threading.Thread(
            target=self._read_loop, args=(pty, generation),
            name=f"pty-read-{self.sid or 'new'}",
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
        pty = PtyProcess.spawn(self._argv, **kwargs)
        # POSIX ptyprocess.PtyProcessUnicode decodes with codec_errors='strict'
        # by default, so a single invalid UTF-8 byte from the child (a binary blob
        # cat'd into the pane, a legacy-encoded log) raises UnicodeDecodeError out
        # of read() and kills the reader thread — the pane freezes instead of just
        # showing a replacement char. Swap in a lenient decoder right after spawn
        # (nothing has been read yet, so no buffered state is lost). winpty returns
        # str already and has no decoder attr, so this is POSIX-only. (#audit-pty-decode)
        if not _IS_WIN and pty is not None:
            try:
                import codecs
                enc = getattr(pty, "encoding", None) or "utf-8"
                pty.codec_errors = "replace"
                pty.decoder = codecs.getincrementaldecoder(enc)(errors="replace")
            except Exception:
                pass
        pid = getattr(pty, "pid", None)
        self._attach_pty(pty, pid)
        self._start_writer(reopen=True)
        _log(f"spawn: sid={(getattr(self, 'sid', None) or '?')[:8]} pid={pid}")

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
        if self._spawn_error is not None:
            # Graceful failure surface: show the error on row 0, blanks below.
            if y == 0:
                text = f" ⚠ terminal unavailable: {self._spawn_error}"
                return Strip([Segment(text[:width] if width else text)])
            return Strip.blank(width)

        with self._lock:
            # Buffer switches happen on the reader thread under this same lock.
            # Select the active screen only after acquiring it, otherwise one
            # render can combine a stale buffer pointer with the new terminal
            # state.
            screen = self._screen
            if screen is None or y >= screen.lines:
                return Strip.blank(width)
            if self.is_dead and self._scroll == 0 and y == screen.lines - 1:
                # Never overwrite the process's final diagnostic. Use the bottom
                # row for the exit hint only when that row is genuinely blank.
                bottom = screen.buffer[y]
                if not any(
                        (bottom[x].data or "").strip()
                        for x in range(screen.columns)):
                    msg = (
                        " ⏎ agent exited — Enter relaunches · "
                        "F10 closes this tab "
                    )
                    return Strip([Segment(
                        msg[:width] if width else msg, Style(reverse=True))])
            if (self._frozen and not self.is_dead
                    and self._scroll == 0 and y == 0):
                # Frozen for copy/select: keep the hint at the top so recent
                # output at the bottom remains selectable.
                msg = " ❄ frozen — Shift+drag to copy · Shift+F9 / type to resume "
                return Strip([Segment(
                    msg[:width] if width else msg, Style(reverse=True))])
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
            if (cells is not None and s == 0 and y == cursor_y
                    and 0 <= cursor_x < cols):
                # Addressing the right half of a wide cell lands on pyte's empty
                # stub. render_line skips stubs, so draw the software caret over
                # the complete grapheme's leader instead of losing it.
                while cursor_x > 0 and cells[cursor_x].data == "":
                    cursor_x -= 1
            selection_bounds = (
                self._selection_bounds_for_row(y, cells, cols)
                if cells is not None else None
            )

        if cells is None:
            return Strip.blank(width)
        # Cursor only in the live view (it lives at the bottom, not in history).
        show_cursor = (s == 0 and self.has_focus and y == cursor_y
                       and not self.is_dead and not cursor_hidden)
        native_caret = _native_caret()   # hoisted: one predicate, one owner
        caret_shape = int(getattr(self, "_cursor_style", 0) or 0)
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
            if show_cursor and x == cursor_x and not native_caret:
                # Draw saikai's own cursor (cell reversed, keeping the cell's real
                # fg/bg/bold so a themed prompt isn't flattened). SKIP exactly when
                # saikai owns the real outer caret (_native_caret): there
                # _show_hw_cursor shows the terminal's NATIVE cursor instead, and
                # drawing here too would stack a wide reverse-block on it. When it
                # does not — anchor off, or a plain Linux/macOS terminal — we MUST
                # draw here, else the pane has no caret at all. (#native-cursor)
                flush(x)
                run_chars = []
                segments.append(_caret_segment(ch, caret_shape))
                run_style = None
                continue
            st = _cell_style(ch)
            if (selection_bounds is not None
                    and selection_bounds[0] <= x <= selection_bounds[1]):
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
        data = encode_key(
            event.key,
            getattr(event, "character", None),
            application_cursor=getattr(self, "_app_cursor", False),
            kitty_flags=self._kitty_flags(),
        )
        if data is None:
            return
        self._note_input()
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
        try:
            with self._lock:
                # Buffer switches use this lock too; selecting the screen here
                # gives the snapshot one exact presentation order.
                scr = getattr(self, "_screen", None)
                if scr is None:
                    self._frozen_buf = None
                    return
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
            self._note_input()
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
        self._note_input()
        self._snap_to_live()   # injected input returns the view to the live bottom
        try:
            self._write_child(text)
        except Exception:
            pass

    def submit(self) -> None:
        """Send a single Enter (\\r) to submit the current input. UI-thread only."""
        if self._pty is None or self.is_dead:
            return
        self._note_input()
        self._snap_to_live()   # submitting returns the view to the live bottom
        try:
            self._write_child("\r")
        except Exception:
            pass

    def _note_input(self, now=None) -> None:
        """Stamp user input and arm one bounded four-second status invalidation."""
        stamp = time.monotonic() if now is None else float(now)
        self.last_input_ts = stamp
        self._input_status_deadline = stamp + 4.0
        self._input_status_deadline_seen = False
        self._input_status_generation = (
            int(getattr(self, "_input_status_generation", 0)) + 1)
        generation = self._input_status_generation
        old_timer = getattr(self, "_input_status_timer", None)
        if old_timer is not None:
            try:
                old_timer.stop()
            except Exception:
                pass
        self._input_status_timer = None
        try:
            if not self.is_attached:
                return
        except Exception:
            return
        try:
            self._input_status_timer = self.set_timer(
                4.0,
                lambda g=generation: self._expire_input_status(g),
            )
        except Exception:
            # Headless tests and pre-mount input still get deterministic expiry
            # through the host's periodic refresh_status poll.
            self._input_status_timer = None

    def _expire_input_status(self, generation: int) -> None:
        """UI-timer callback; stale timers cannot reclassify newer input."""
        if generation != getattr(self, "_input_status_generation", 0):
            return
        self._input_status_timer = None
        self.refresh_status()

    def _stop_input_status_timer(self) -> None:
        timer = self._retire_input_status_timer()
        if timer is not None:
            try:
                timer.stop()
            except Exception:
                pass

    def _retire_input_status_timer(self):
        """Invalidate and detach the timer without touching its asyncio loop."""
        self._input_status_generation = (
            int(getattr(self, "_input_status_generation", 0)) + 1)
        timer = getattr(self, "_input_status_timer", None)
        self._input_status_timer = None
        return timer

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
            self._note_input()
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
        with self._lock:
            if self._scroll == 0:
                self._capture_scroll_snapshot_locked()
            snapshot = getattr(self, "_scroll_snapshot", None)
            hist_len = (
                max(0, len(snapshot["rows"]) - snapshot["lines"])
                if snapshot is not None
                else len(self._screen.history.top)
            )
            self._scroll = min(self._scroll + 3, hist_len)
            if self._scroll == 0:
                self._scroll_snapshot = None
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
            if back_at_live:
                self._scroll_snapshot = None
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
            self._scroll_snapshot = None
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
    def _capture_scroll_snapshot_locked(self) -> None:
        """Pin one bounded combined history/live image (lock held).

        ``deque(maxlen)`` keeps the same length while evicting, so inferring
        scroll movement from ``len(history)`` cannot hold a displayed line
        stable. A snapshot makes render and selection copy read identical cells
        until the user returns to the live bottom.
        """
        screen = self._screen
        if screen is None:
            self._scroll_snapshot = None
            return
        cols = screen.columns
        rows = [
            [line[x] for x in range(cols)]
            for line in list(screen.history.top)
        ]
        rows.extend(
            [screen.buffer[y][x] for x in range(cols)]
            for y in range(screen.lines)
        )
        self._scroll_snapshot = {
            "screen": screen,
            "rows": rows,
            "lines": screen.lines,
            "columns": cols,
        }

    def _buf_for_row(self, screen, s, y):
        """pyte cell-row backing display row y (lock held). s>0 windows into
        history.top + live buffer; None = past the scrollback top."""
        if s > 0:
            snapshot = getattr(self, "_scroll_snapshot", None)
            if (snapshot is not None
                    and snapshot.get("screen") is screen
                    and snapshot.get("lines") == screen.lines
                    and snapshot.get("columns") == screen.columns):
                rows = snapshot["rows"]
                hist_len = max(0, len(rows) - screen.lines)
                idx = _scroll_row_index(hist_len, s, y)
                return rows[idx] if 0 <= idx < len(rows) else None
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

    def _selection_bounds_for_row(self, y: int, cells, cols: int):
        """Return inclusive bounds expanded to complete wide graphemes."""
        a, h = self._sel_anchor, self._sel_head
        if a is None or h is None or cells is None or cols <= 0:
            return None
        (r0, c0), (r1, c1) = (a, h) if a <= h else (h, a)
        if y < r0 or y > r1:
            return None
        if r0 == r1:
            lo, hi = c0, c1
        elif y == r0:
            lo, hi = c0, cols - 1
        elif y == r1:
            lo, hi = 0, c1
        else:
            lo, hi = 0, cols - 1
        lo = max(0, min(int(lo), cols - 1))
        hi = max(0, min(int(hi), cols - 1))
        while lo > 0 and cells[lo].data == "":
            lo -= 1
        if cells[hi].data == "":
            while hi > 0 and cells[hi].data == "":
                hi -= 1
        while hi + 1 < cols and cells[hi + 1].data == "":
            hi += 1
        return lo, hi

    def _extract_selection(self) -> str:
        a, h = self._sel_anchor, self._sel_head
        if a is None or h is None:
            return ""
        (r0, c0), (r1, c1) = (a, h) if a <= h else (h, a)
        lines = []
        with self._lock:
            screen = self._screen
            if screen is None:
                return ""
            s = self._scroll
            cols = screen.columns
            for y in range(r0, r1 + 1):
                buf = self._buf_for_row(screen, s, y)
                if buf is None:
                    lines.append("")
                    continue
                bounds = self._selection_bounds_for_row(y, buf, cols)
                if bounds is None:
                    lines.append("")
                    continue
                lo, hi = bounds
                row = "".join(buf[x].data for x in range(lo, hi + 1)
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
        X10. kind ∈ {down,up,move}. UI-thread only; the pane writer enqueue is
        non-blocking and the backend write runs on its worker. (#faithful-mouse)"""
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
            self._note_input()
        except Exception:
            pass

    def _child_owns_mouse(self) -> bool:
        """True when the child enabled mouse tracking and can take events now."""
        return bool(self._mouse_reporting) and self._pty is not None and not self.is_dead

    def on_mouse_down(self, event) -> None:   # events.MouseDown
        if self._screen is None:
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
            if self._scroll == 0 and d > 0:
                self._capture_scroll_snapshot_locked()
            snapshot = getattr(self, "_scroll_snapshot", None)
            hist = (
                max(0, len(snapshot["rows"]) - snapshot["lines"])
                if snapshot is not None
                else len(scr.history.top)
            )
            old = self._scroll
            self._scroll = (min(old + 1, hist) if d > 0 else max(old - 1, 0))
            new = self._scroll
            if new == 0:
                self._scroll_snapshot = None
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
            self._ensure_screen_pair_locked()
            frozen_before = (
                getattr(self, "_frozen_buf", None)
                if getattr(self, "_frozen", False) else None
            )
            screens = []
            for screen in (self._main_screen, self._alt_screen):
                if screen is not None and screen not in screens:
                    screens.append(screen)
            for screen in screens:
                self._invalidate_screen_grapheme(screen)
                try:
                    screen.resize(rows, cols)       # pyte: (rows, cols)!
                except Exception:
                    continue
                # pyte may retain an out-of-range cursor after a shrink.
                try:
                    screen.cursor.y = max(0, min(int(screen.cursor.y), rows - 1))
                    screen.cursor.x = max(0, min(int(screen.cursor.x), cols))
                except Exception:
                    pass
            self._scroll = 0
            self._scroll_snapshot = None
            active = self._screen
            if getattr(self, "_frozen", False) and active is not None:
                if frozen_before is None:
                    # Defensive legacy state: frozen without a pinned frame.
                    self._frozen_buf = {
                        y: [active.buffer[y][x] for x in range(active.columns)]
                        for y in range(active.lines)
                    }
                else:
                    # Resize the frame the user actually froze. The live grid
                    # may have advanced far beyond it while repaints were paused;
                    # re-snapshotting live here makes the display jump on resize.
                    blank = getattr(active, "default_char", None)
                    if blank is None:
                        blank = active.cursor.attrs._replace(data=" ")
                    resized = {}
                    for row_index in range(rows):
                        old_row = list(frozen_before.get(row_index, ()))
                        row = old_row[:cols]
                        row.extend([blank] * (cols - len(row)))
                        # A horizontal crop may cut off a wide EGC's stub.
                        column = 0
                        while column < cols:
                            data = row[column].data
                            if data == "":
                                row[column] = blank
                                column += 1
                                continue
                            cell_width = max(
                                0, int(_rich_cell_len(data)))
                            if cell_width > 1:
                                if (column + cell_width > cols
                                        or any(
                                            row[column + offset].data != ""
                                            for offset in range(1, cell_width)
                                        )):
                                    row[column] = blank
                                    column += 1
                                else:
                                    column += cell_width
                            else:
                                column += 1
                        resized[row_index] = row
                    self._frozen_buf = resized
            else:
                self._frozen_buf = None
            if active is not None:
                def clamp_point(point):
                    if point is None:
                        return None
                    y, x = point
                    return (
                        max(0, min(int(y), active.lines - 1)),
                        max(0, min(int(x), active.columns - 1)),
                    )
                self._sel_anchor = clamp_point(self._sel_anchor)
                self._sel_head = clamp_point(self._sel_head)
                self._pending_anchor = clamp_point(self._pending_anchor)
            self._scr_ver += 1
            self._cached_ver = -1
            self._cached_screen = ("", "")
            self._last_poll_ver = -1
        pty, _pid, _generation = self._lifecycle_snapshot()
        if pty is not None and not self.is_dead:
            try:
                pty.setwinsize(rows, cols)          # winpty: (rows, cols)
            except Exception:
                pass
        self.refresh()
        # Host/IME/mirror callbacks stay outside the pyte lock.
        self._sync_terminal_cursor(reason="focus")
        try:
            self.mirror_reseed()
        except Exception:
            pass

    # ── (4) background reader -> feed pyte -> repaint on the UI thread ─────────
    def _ensure_sync_deadline_state(self) -> None:
        """Initialize deadline fields for legacy/minimal `__new__` test objects."""
        if not hasattr(self, "_sync_lock"):
            self._sync_lock = threading.RLock()
        if not hasattr(self, "_sync_dispatch_lock"):
            self._sync_dispatch_lock = threading.RLock()
        if not hasattr(self, "_sync_deadline_condition"):
            self._sync_deadline_condition = threading.Condition()
            self._sync_deadline_generation = 0
            self._sync_deadline_at = None
            self._sync_deadline_opened_at = None
            self._sync_deadline_stop = False
            self._sync_deadline_worker = None
            self._sync_deadline_workers_started = 0

    def _arm_sync_deadline(self, max_age: float, opened_at: float) -> int:
        """Set one generation-checked deadline on the pane's persistent worker."""
        self._ensure_sync_deadline_state()
        condition = self._sync_deadline_condition
        with condition:
            if self._sync_deadline_stop:
                return self._sync_deadline_generation
            self._sync_deadline_generation += 1
            generation = self._sync_deadline_generation
            self._sync_deadline_at = time.monotonic() + max(0.0, float(max_age))
            self._sync_deadline_opened_at = float(opened_at)
            worker = self._sync_deadline_worker
            if worker is None or not worker.is_alive():
                worker = self._sync_deadline_worker = threading.Thread(
                    target=self._sync_deadline_loop,
                    name=f"sync-deadline-{getattr(self, 'sid', None) or 'new'}",
                    daemon=True,
                )
                self._sync_deadline_workers_started += 1
                worker.start()
            condition.notify()
            return generation

    def _cancel_sync_deadline(self) -> None:
        """Invalidate the current deadline without retiring the persistent worker."""
        self._ensure_sync_deadline_state()
        condition = self._sync_deadline_condition
        with condition:
            self._sync_deadline_generation += 1
            self._sync_deadline_at = None
            self._sync_deadline_opened_at = None
            condition.notify()

    def _retire_sync_deadline(self) -> None:
        """Retire the deadline and fence any already-authorized presentation.

        The dispatch lock is re-entrant because EOF flush already owns it.  Taking
        the same lock here gives retirement one linearization point with timeout
        extraction, pyte feed, and repaint scheduling: once this method returns,
        an old deadline cannot present anything later.
        """
        self._ensure_sync_deadline_state()
        condition = self._sync_deadline_condition
        # Publish cancellation first, so an expiry blocked on either ordering
        # lock loses as soon as it reaches its final generation check.
        with condition:
            self._sync_deadline_generation += 1
            self._sync_deadline_at = None
            self._sync_deadline_opened_at = None
            self._sync_deadline_stop = True
            condition.notify_all()

        # A test/exception path may retire while it already owns the stager
        # lock.  Waiting for dispatch there would invert expiry's
        # dispatch->stager order.  It is safe to return: an expiry cannot have
        # passed the final check while this thread owns the stager lock, and the
        # cancellation above will reject it after the lock is released.
        try:
            owns_stager = self._sync_lock._is_owned()
        except Exception:
            owns_stager = False
        if owns_stager:
            return

        # Otherwise fence an expiry which had already been authorized.  It
        # either completes feed/repaint first or observes cancellation; in both
        # cases no old deadline can present after this acquisition returns.
        with self._sync_dispatch_lock:
            pass

    def _sync_deadline_loop(self) -> None:
        condition = self._sync_deadline_condition
        while True:
            with condition:
                while (not self._sync_deadline_stop
                       and self._sync_deadline_at is None):
                    condition.wait()
                if self._sync_deadline_stop:
                    return
                generation = self._sync_deadline_generation
                remaining = self._sync_deadline_at - time.monotonic()
                if remaining > 0:
                    condition.wait(remaining)
                    continue
            self._expire_sync_output(generation)

    def _present_sync_unit(self, text: str, deferred_ui=None) -> None:
        """Call the built-in presenter with deferral, preserving test/subclass APIs."""
        presenter = self._consume_ready
        if getattr(presenter, "__func__", None) is AgentTerminal._consume_ready:
            presenter(text, deferred_ui=deferred_ui)
        else:
            presenter(text)

    def _consume_sync_units(self, units, deferred_ui=None) -> bool:
        """Feed extracted stager units after its lock has been released."""
        changed = False
        for text, fail_reason in units:
            if fail_reason:
                _log(f"sync-output fail-open: reason={fail_reason} chars={len(text)}")
            self._present_sync_unit(text, deferred_ui=deferred_ui)
            changed = True
        return changed

    def _expire_sync_output(self, generation: int) -> bool:
        """Fail open one still-current quiet frame, then repaint through coalescing."""
        self._ensure_sync_deadline_state()
        condition = self._sync_deadline_condition
        deferred_ui = []
        with condition:
            deadline = self._sync_deadline_at
            opened_at = self._sync_deadline_opened_at
            if (self._sync_deadline_stop
                    or generation != self._sync_deadline_generation
                    or deadline is None
                    or opened_at is None
                    or time.monotonic() < deadline):
                return False
            self._sync_deadline_at = None
            self._sync_deadline_opened_at = None
        # Reader batches and expiry feed have one presentation-order owner.
        # Revalidate retirement/generation after waiting for that owner.
        with self._sync_dispatch_lock:
            with condition:
                if (self._sync_deadline_stop
                        or generation != self._sync_deadline_generation):
                    return False
            # Never hold the deadline condition or stager lock while feeding
            # pyte, marshalling callbacks, or scheduling UI work.
            with self._sync_lock:
                # Retirement/new-generation can happen while expiry waits for
                # the stager lock, so this is the final authorization point.
                with condition:
                    if (self._sync_deadline_stop
                            or generation != self._sync_deadline_generation):
                        return False
                now = time.monotonic()
                stager = self._sync_output
                if (not stager.active
                        or stager._opened_at != opened_at
                        or stager._opened_at + stager.max_age > now):
                    units = []
                else:
                    units = stager.flush("timeout", now=now)
            changed = self._consume_sync_units(units, deferred_ui=deferred_ui)
            if (changed and getattr(self, "_scroll", 0) == 0
                    and not getattr(self, "_frozen", False)):
                self._schedule_pane_refresh()
        if not getattr(self, "_stop", threading.Event()).is_set():
            for callback in deferred_ui:
                self._marshal(callback)
        return changed

    def _flush_sync_output(self, reason: str) -> bool:
        """Release one retained frame on the reader thread, at most once."""
        self._ensure_sync_deadline_state()
        sync_output = getattr(self, "_sync_output", None)
        if sync_output is None:
            return False
        deferred_ui = []
        with self._sync_dispatch_lock:
            if reason == "eof":
                self._retire_sync_deadline()
            else:
                self._cancel_sync_deadline()
            with self._sync_lock:
                units = sync_output.flush(reason)
            changed = self._consume_sync_units(units, deferred_ui=deferred_ui)
        if not getattr(self, "_stop", threading.Event()).is_set():
            for callback in deferred_ui:
                self._marshal(callback)
        return changed

    def _read_loop(self, pty=None, generation=None) -> None:
        if pty is None or generation is None:
            current_pty, _pid, current_generation = self._lifecycle_snapshot()
            if pty is None:
                pty = current_pty
            if generation is None:
                generation = current_generation
        assert pty is not None
        natural_eof = False
        try:
            while not self._stop.is_set():
                try:
                    # Ask for a real buffer: ptyprocess defaults to 1024 bytes, so a
                    # multi-megabyte turn woke this loop ~1000 times per MB and paid
                    # the whole per-chunk pipeline each time. (#linux-read-size)
                    chunk = pty.read(_PTY_READ_SIZE)   # blocking; str on winpty
                except EOFError:                     # child closed the pty
                    natural_eof = True
                    self._mark_generation_natural_eof(generation)
                    break
                except Exception:
                    break
                if not chunk:
                    # Defensive: some backends may yield "" transiently. Avoid a
                    # busy-spin; re-check isalive and back off before continuing.
                    if not _safe_isalive(pty):
                        natural_eof = True
                        self._mark_generation_natural_eof(generation)
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
            bundle = self._detach_owned_pty(pty, generation)
            same_generation_ended = (
                bundle is not None
                or self._generation_is_retiring(generation)
            )
            if not same_generation_ended:
                # A stale reader must not stop, flush, or finalize a replacement
                # generation which another owner attached after explicit detach.
                return

            try:
                writer = self._stop_writer()
                if bundle is not None:
                    # Flush a truncated final escape as visible literal data before
                    # the synchronized-output stager. This preserves the last bytes
                    # instead of silently losing an incomplete CSI/OSC at EOF.
                    try:
                        self._flush_vt_tokenizer_eof()
                    except Exception:
                        pass
                    # Guarded: either flush can feed pyte and then the status
                    # classifier (a caller-supplied callable), so a raise must not
                    # cost us reap or the final death notification. (#eof-flush)
                    try:
                        self._flush_sync_output("eof")
                    except Exception:
                        pass
                    try:
                        with self._lock:
                            for screen in (
                                    getattr(self, "_main_screen", None),
                                    getattr(self, "_alt_screen", None)):
                                if screen is not None:
                                    self._invalidate_screen_grapheme(screen)
                    except Exception:
                        pass
                    self._stop.set()
                    if not natural_eof and not _IS_WIN:
                        _post_signal(bundle[1], "SIGHUP")
                        _post_signal(bundle[1], "SIGTERM")
                    self._start_owned_reaper(
                        bundle, writer, natural=natural_eof)

                # kill may have won the detach race; it already stopped the pane.
                # Either way, this exact generation owns the death callback.
                self._finalize()
            finally:
                self._finish_pty_retirement(generation)

    def _honor_osc52(self, b64: str, deferred_ui=None) -> None:
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
            self._queue_or_marshal(
                lambda t=text: self._copy_osc52_if_allowed(t), deferred_ui)

    def _osc52_copy_allowed(self) -> bool:
        """Fail closed unless this pane exclusively owns visible active focus."""
        try:
            app = self.app
            screen = self.screen
            return bool(
                self._pty is not None
                and not self.is_dead
                and self.is_attached
                and self.display
                and app.app_focus
                and screen.is_active
                and self._is_focused_pane()
            )
        except Exception:
            return False

    def _copy_osc52_if_allowed(self, text: str) -> None:
        """UI-thread half of OSC 52: re-check focus before host/mirror writes."""
        if text and self._osc52_copy_allowed():
            self._copy_text(text)

    def _ensure_writer_state(self) -> None:
        """Initialize writer fields for lightweight ``__new__`` test panes."""
        if getattr(self, "_write_condition", None) is not None:
            return
        self._write_condition = threading.Condition()
        self._write_q = deque()
        self._write_queued_bytes = 0
        self._write_inflight_bytes = 0
        self._write_pending_bytes = 0
        self._write_drop_count = 0
        self._write_drop_bytes = 0
        self._write_drop_reason = ""
        self._write_accepting = False
        self._write_stop = False
        self._write_closed = False
        self._writer = None
        self._writer_generation = None
        self._writer_workers_started = 0

    def _start_writer(self, *, reopen: bool = False):
        """Start this pane's sole persistent PTY writer.

        ``reopen`` is reserved for the spawn boundary. Once teardown closes the
        queue, an input race must not resurrect a worker against the dying PTY.
        """
        self._ensure_writer_state()
        pty, _pid, generation = self._lifecycle_snapshot()
        condition = self._write_condition
        worker = None
        with condition:
            if reopen:
                self._write_closed = False
                self._write_stop = False
            current = self._writer
            if (current is not None and current.is_alive()
                    and self._writer_generation == generation):
                return current
            if (self._write_closed or self._write_stop
                    or pty is None or self.is_dead):
                return current
            self._write_accepting = True
            worker = threading.Thread(
                target=self._writer_loop, args=(generation,),
                name=f"saikai-pty-write-{getattr(self, 'sid', None) or 'new'}",
                daemon=True,
            )
            self._writer = worker
            self._writer_generation = generation
            self._writer_workers_started += 1
        try:
            worker.start()
        except Exception:
            with condition:
                if self._writer is worker:
                    self._writer = None
                    self._writer_generation = None
                self._write_accepting = False
                self._write_closed = True
                condition.notify_all()
            return None
        _track_pty_writer(worker)
        return worker

    def _stop_writer(self):
        """Stop acceptance, discard queued input, and wake the worker.

        Never joins: kill/on_unmount may run on Textual's UI thread and the
        worker may currently be blocked in the backend. The process reaper
        closes/signals the PTY; callers that are already off-thread may bounded-
        join the returned worker.
        """
        condition = getattr(self, "_write_condition", None)
        if condition is None:
            return None
        with condition:
            self._write_accepting = False
            self._write_closed = True
            self._write_stop = True
            queued = self._write_queued_bytes
            self._write_q.clear()
            self._write_queued_bytes = 0
            self._write_pending_bytes = max(
                self._write_inflight_bytes,
                self._write_pending_bytes - queued,
            )
            worker = self._writer
            condition.notify_all()
            return worker

    def _record_write_drop(self, reason: str, encoded_bytes: int = 0) -> None:
        """Aggregate a bounded diagnostic without caller-thread filesystem I/O."""
        self._ensure_writer_state()
        condition = self._write_condition
        with condition:
            if self._write_drop_count == 0:
                self._write_drop_reason = reason
            elif self._write_drop_reason != reason:
                self._write_drop_reason = "multiple reasons"
            self._write_drop_count = min(
                0x7fffffff, self._write_drop_count + 1)
            self._write_drop_bytes = min(
                0x7fffffffffffffff,
                self._write_drop_bytes + max(0, int(encoded_bytes)),
            )
            condition.notify_all()

    def write(self, data: str) -> bool:
        """Enqueue raw child input in O(1), bounded by encoded UTF-8 bytes."""
        pty, _pid, _generation = self._lifecycle_snapshot()
        if not isinstance(data, str) or not data or pty is None or self.is_dead:
            return False
        self._ensure_writer_state()
        if self._writer is None:
            self._start_writer()
        try:
            encoded_bytes = len(data.encode("utf-8"))
        except UnicodeEncodeError:
            self._record_write_drop("input is not valid UTF-8")
            return False

        condition = self._write_condition
        rejection = None
        with condition:
            if (not self._write_accepting or self._write_stop
                    or self._writer is None):
                return False
            if encoded_bytes > _PTY_WRITE_QUEUE_MAX:
                rejection = "item too large"
            elif self._write_pending_bytes + encoded_bytes > _PTY_WRITE_QUEUE_MAX:
                rejection = "queue full"
            else:
                # Bind acceptance to one exact PTY generation. A detached
                # generation's queued key must never be delivered to a later
                # process which happens to reuse this pane.
                self._write_q.append(
                    (data, encoded_bytes, pty, _generation))
                self._write_queued_bytes += encoded_bytes
                self._write_pending_bytes += encoded_bytes
                condition.notify()
                return True
            if self._write_drop_count == 0:
                self._write_drop_reason = str(rejection)
            elif self._write_drop_reason != rejection:
                self._write_drop_reason = "multiple reasons"
            self._write_drop_count = min(
                0x7fffffff, self._write_drop_count + 1)
            self._write_drop_bytes = min(
                0x7fffffffffffffff,
                self._write_drop_bytes + encoded_bytes,
            )
            condition.notify_all()
        return False

    def _write_child(self, data: str) -> bool:
        """Compatibility name for local input paths; all work is queued."""
        return self.write(data)

    def _writer_loop(self, generation=None) -> None:
        """Drain all accepted PTY writes in FIFO order off caller threads."""
        if generation is None:
            _pty, _pid, generation = self._lifecycle_snapshot()
        condition = self._write_condition
        me = threading.current_thread()
        while True:
            drop_count = 0
            drop_bytes = 0
            drop_reason = ""
            stop = False
            item = None
            pty = None
            with condition:
                while (not self._write_q and not self._write_stop
                       and self._write_drop_count == 0
                       and self._writer is me
                       and self._writer_generation == generation):
                    condition.wait()
                if self._write_drop_count:
                    drop_count = self._write_drop_count
                    drop_bytes = self._write_drop_bytes
                    drop_reason = self._write_drop_reason
                    self._write_drop_count = 0
                    self._write_drop_bytes = 0
                    self._write_drop_reason = ""
                if (self._writer is not me
                        or self._writer_generation != generation):
                    # A replacement generation installed its own worker. This
                    # worker may finish an in-flight old write, but may not
                    # consume the replacement's queue.
                    stop = True
                elif self._write_stop:
                    if self._writer is me:
                        self._writer = None
                        self._writer_generation = None
                    condition.notify_all()
                    stop = True
                elif self._write_q:
                    item = self._write_q.popleft()
                    self._write_queued_bytes -= item[1]
                    self._write_inflight_bytes += item[1]
            if drop_count:
                suffix = (
                    f", {drop_count} items"
                    if drop_count != 1 else "")
                _log(
                    f"pty write dropped: {drop_reason} "
                    f"({drop_bytes} UTF-8 bytes{suffix})"
                )
            if stop:
                return
            if item is None:
                continue
            data, encoded_bytes, item_pty, item_generation = item
            current_pty, _pid, current_generation = self._lifecycle_snapshot()
            if (current_pty is item_pty
                    and current_generation == item_generation):
                try:
                    item_pty.write(data)
                except Exception:
                    pass
            with condition:
                self._write_inflight_bytes -= encoded_bytes
                self._write_pending_bytes = max(
                    0, self._write_pending_bytes - encoded_bytes)
                condition.notify_all()

    def _send_to_child(self, data: str) -> bool:
        """Guarded compatibility entry point for terminal-generated replies."""
        return self.write(data)

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

    def _mirror_reseed_locked(self) -> bool:
        reset, synth, scr = self._mirror_reset, self._mirror_synth, self._screen
        if reset is None or synth is None or scr is None:
            return False
        for tracked in (
                getattr(self, "_main_screen", None),
                getattr(self, "_alt_screen", None)):
            if tracked is None:
                continue
            try:
                tracked._refresh_saikai_hyperlinks()
                tracked._refresh_mirror_wide_state()
            except Exception:
                pass
        try:
            scr._saikai_active_hyperlink = getattr(
                self, "_osc8_active", None)
        except Exception:
            pass
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
            "cursor_style": int(getattr(self, "_cursor_style", 0) or 0),
            "kitty_keyboard": self._kitty_flags(),
            # Private seed inputs. Keeping the four-argument synth callback
            # contract preserves embedders/tests while the built-in serializer
            # can reconstruct both real buffers.
            "_main_screen": getattr(self, "_main_screen", None),
            "_alt_screen": getattr(self, "_alt_screen", None),
        }
        try:
            reset(synth(scr, scr.columns, scr.lines, modes))
            self._mirror_mode_reseed_pending = False
            return True
        except Exception:
            return False

    def _ring_bell(self) -> None:
        """Ring the host terminal bell (UI thread)."""
        try:
            self.app.bell()
        except Exception:
            pass

    def _queue_or_marshal(self, callback: Callable, deferred_ui=None) -> None:
        """Defer UI work until the presentation-order lock has been released."""
        if deferred_ui is None:
            self._marshal(callback)
        else:
            deferred_ui.append(callback)

    def _notify_host(self, msg: str, deferred_ui=None) -> None:
        """Surface a child desktop-notification (OSC 9/777/99) as a saikai toast.
        Reader thread → marshal the toast onto the UI thread. (#osc-notify)"""
        msg = (msg or "").strip()
        if not msg:
            return
        self._queue_or_marshal(
            lambda m=msg[:200]: self._safe_notify(m), deferred_ui)

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
                lines = int(getattr(scr, "lines", 0) or 0)
                cols = int(getattr(scr, "columns", 0) or 0)
                if lines:
                    row = min(max(1, row), lines)
                if cols:
                    col = min(max(1, col), cols)
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
            if stager is None:
                return 2
            self._ensure_sync_deadline_state()
            with self._sync_lock:
                in_block = stager.in_block
            return 1 if in_block else 2
        if mode in _DECRQM_ALT_SCREEN:
            return 1 if getattr(
                self, "_alt_screen_mode",
                getattr(getattr(self, "_alt", None), "in_alt", False)) else 2
        if mode == "25":
            return 1 if getattr(self, "_cursor_visible", True) else 2
        attr = _DECRQM_TRACKED.get(mode)
        if attr is None:
            return 0
        return 1 if getattr(self, attr, False) else 2

    def _apply_dec_private(self, token: VTToken) -> None:
        """Apply every DECSET/DECRST parameter in stream order."""
        if (token.kind != "csi" or token.intermediates
                or token.final not in ("h", "l")
                or not token.parameters.startswith("?")):
            return
        old_kitty_flags = self._kitty_flags()
        enabled = token.final == "h"
        for mode in token.parameters[1:].split(";"):
            if mode == "1":
                self._app_cursor = enabled
            elif mode == "25":
                self._cursor_visible = enabled
            elif mode in _DECRQM_ALT_SCREEN:
                self._alt_screen_mode = enabled
            elif mode in ("1000", "1002", "1003"):
                # Mouse tracking is one exclusive protocol slot. Enabling one
                # replaces the slot; resetting any family member clears it.
                self._mouse_click = enabled and mode == "1000"
                self._mouse_btn_motion = enabled and mode == "1002"
                self._mouse_any_motion = enabled and mode == "1003"
            elif mode == "1004":
                self._focus_reporting = enabled
            elif mode == "1006":
                self._mouse_sgr = enabled
            elif mode == "2004":
                self._bracketed_paste = enabled
        self._mouse_reporting = (
            getattr(self, "_mouse_click", False)
            or getattr(self, "_mouse_btn_motion", False)
            or getattr(self, "_mouse_any_motion", False)
        )
        if self._kitty_flags() != old_kitty_flags:
            self._mirror_mode_reseed_pending = True

    def _apply_cursor_style(self, token: VTToken) -> bool:
        """Track DECSCUSR (CSI Ps SP q); return whether the token matched."""
        if (token.kind != "csi" or token.final != "q"
                or token.intermediates != " "
                or token.parameters.startswith((">", "?", "<", "="))):
            return False
        try:
            style = int(token.parameters or "0")
        except ValueError:
            style = 0
        self._cursor_style = style if 0 <= style <= 6 else 0
        return True

    def _ensure_kitty_keyboard_state(self) -> None:
        if not hasattr(self, "_kitty_keyboard_flags"):
            self._kitty_keyboard_flags = {False: 0, True: 0}
            self._kitty_keyboard_stacks = {False: [], True: []}

    def _kitty_flags(self) -> int:
        self._ensure_kitty_keyboard_state()
        alternate = bool(getattr(self, "_alt_screen_mode", False))
        return self._kitty_keyboard_flags[alternate]

    def _apply_kitty_keyboard(self, token: VTToken) -> Optional[str]:
        """Apply/query Kitty keyboard state for the current main/alt buffer."""
        if (token.kind != "csi" or token.final != "u"
                or token.intermediates
                or token.parameters[:1] not in "<>=?"):
            return None
        self._ensure_kitty_keyboard_state()
        prefix = token.parameters[0]
        fields = token.parameters[1:].split(";") if token.parameters[1:] else []

        def number(index: int, default: int) -> int:
            if index >= len(fields) or fields[index] == "":
                return default
            try:
                return max(0, int(fields[index]))
            except ValueError:
                return default

        alternate = bool(getattr(self, "_alt_screen_mode", False))
        current = self._kitty_keyboard_flags[alternate]
        old_current = current
        stack = self._kitty_keyboard_stacks[alternate]
        if prefix == "?":
            return f"\x1b[?{current}u"
        if prefix == "=":
            flags = number(0, 0) & _KITTY_KBD_SUPPORTED_FLAGS
            mode = number(1, 1)
            if mode == 1:
                current = flags
            elif mode == 2:
                current |= flags
            elif mode == 3:
                current &= ~flags
            else:
                return None
        elif prefix == ">":
            if len(stack) >= _KITTY_KBD_STACK_MAX:
                del stack[0]
            stack.append(current)
            current = number(0, 0) & _KITTY_KBD_SUPPORTED_FLAGS
        else:  # "<": pop one (default) or N states; over-pop returns to 0.
            for _ in range(max(1, number(0, 1))):
                current = stack.pop() if stack else 0
        self._kitty_keyboard_flags[alternate] = current
        if current != old_current:
            self._mirror_mode_reseed_pending = True
        return None

    def _csi_query_reply(self, token: VTToken) -> Optional[str]:
        """Return one reply for one CSI query token, or None."""
        if token.kind != "csi":
            return None
        params = token.parameters
        if token.final == "c" and not token.intermediates:
            if params in ("", "0"):
                # VT500 class plus ANSI colour only. Do not claim sixel (4),
                # selective erase (6), UDK/DRCS/macros (8), or rectangular
                # editing (28), none of which the pane implements.
                return "\x1b[?62;22c"
            if params in (">", ">0"):
                return "\x1b[>0;10;1c"
        if token.final == "n" and not token.intermediates:
            private = "?" if params.startswith("?") else ""
            kind = params[1:] if private else params
            if kind == "5" and not private:
                return "\x1b[0n"
            if kind == "6":
                row, col = self._cursor_rowcol()
                return f"\x1b[{private}{row};{col}R"
        if (token.final == "p" and token.intermediates == "$"
                and params.startswith("?") and params[1:].isdigit()):
            mode = params[1:]
            return f"\x1b[?{mode};{self._decrqm_report(mode)}$y"
        if (token.final == "q" and not token.intermediates
                and params in (">", ">0")):
            return "\x1bP>|saikai\x1b\\"
        return None

    @staticmethod
    def _is_cursor_query(token: VTToken) -> bool:
        return (token.kind == "csi" and token.final == "n"
                and not token.intermediates
                and token.parameters in ("6", "?6"))

    @staticmethod
    def _is_csi_query(token: VTToken) -> bool:
        if token.kind != "csi":
            return False
        params = token.parameters
        if not token.intermediates and token.final == "c":
            return params in ("", "0", ">", ">0")
        if not token.intermediates and token.final == "n":
            return params in ("5", "6", "?6")
        if token.intermediates == "$" and token.final == "p":
            return params.startswith("?") and params[1:].isdigit()
        return (not token.intermediates and token.final == "q"
                and params in (">", ">0"))

    def _apply_osc8_state(self, token: VTToken) -> bool:
        """Track the ordered OSC 8 link applied to subsequently drawn cells."""
        if token.kind != "osc":
            return False
        code, payload, _terminator = _osc_parts(token)
        if code != "8":
            return False
        params, separator, uri = payload.partition(";")
        if not separator:
            return False
        self._osc8_active = (params, uri) if uri else None
        screen = getattr(self, "_screen", None)
        if screen is not None:
            screen._saikai_active_hyperlink = self._osc8_active
        return True

    def _osc_side_effect(self, token: VTToken, deferred_ui=None) -> Optional[str]:
        """Dispatch a complete OSC and return its query reply, if any."""
        code, payload, terminator = _osc_parts(token)

        def side_effect(method, value):
            # Tests and subclasses may replace these one-argument hooks.  Queue
            # the call itself instead of passing an internal deferral keyword.
            builtin = getattr(method, "__func__", None)
            if builtin in (AgentTerminal._honor_osc52, AgentTerminal._notify_host):
                method(value, deferred_ui=deferred_ui)
                return
            if deferred_ui is None:
                method(value)
            else:
                deferred_ui.append(lambda m=method, v=value: m(v))

        if code in ("10", "11") and payload == "?":
            rgb = "1e1e/1e1e/1e1e" if code == "11" else "c0c0/c0c0/c0c0"
            return f"\x1b]{code};rgb:{rgb}{terminator}"
        if code == "52":
            _selection, sep, b64 = payload.partition(";")
            if sep:
                side_effect(self._honor_osc52, b64)
        elif code == "9" and not payload.startswith("4;"):
            side_effect(self._notify_host, payload)
        elif code == "777" and payload.startswith("notify;"):
            side_effect(
                self._notify_host,
                payload.removeprefix("notify;").replace(";", ": ", 1),
            )
        elif code == "99":
            _metadata, sep, message = payload.partition(";")
            if sep:
                side_effect(self._notify_host, message)
        return None

    def _answer_queries(self, chunk: str) -> None:
        """Answer already-presented queries once each, in token order."""
        for token in VTTokenizer().feed(chunk):
            reply = (self._osc_side_effect(token) if token.kind == "osc"
                     else self._csi_query_reply(token))
            if reply is not None:
                self._send_to_child(reply)

    def _answer_static_queries(self, chunk: str) -> None:
        """Compatibility wrapper; ordered callers use `_consume` directly."""
        self._answer_queries(chunk)

    def _answer_cursor_queries(self, chunk: str) -> None:
        self._answer_queries(chunk)

    def _answer_mode_queries(self, chunk: str) -> None:
        self._answer_queries(chunk)

    def _consume(self, chunk: str) -> bool:
        """Serialize reader dispatch against deadline/EOF presentation."""
        self._ensure_sync_deadline_state()
        deferred_ui = []
        with self._sync_dispatch_lock:
            changed = self._consume_ordered(chunk, deferred_ui=deferred_ui)
        if not getattr(self, "_stop", threading.Event()).is_set():
            for callback in deferred_ui:
                self._marshal(callback)
        return changed

    def _consume_ordered(
            self, chunk: str, deferred_ui=None, *, tokens=None) -> bool:
        """Stage decoded output and feed only complete units to pyte."""
        if _PTY_CAPTURE:
            try:
                with open(_PTY_CAPTURE, "a", encoding="utf-8") as _cf:
                    _cf.write(repr(chunk) + "\n")   # raw chunk, escape seqs visible
            except Exception:
                pass
        tokenizer = getattr(self, "_vt_tokenizer", None)
        if tokenizer is None:                       # minimal __new__ test objects
            tokenizer = self._vt_tokenizer = VTTokenizer()
        sync_output = getattr(self, "_sync_output", None)
        if sync_output is None:
            sync_output = self._sync_output = _SynchronizedOutputStager()
        self._ensure_sync_deadline_state()
        ready = []
        changed = False

        def stage(text):
            if text:
                with self._sync_lock:
                    was_active = sync_output.active
                    opened_at = sync_output._opened_at
                    units = sync_output.push(text)
                    is_active = sync_output.active
                    new_opened_at = sync_output._opened_at
                closed = was_active and not is_active
                for index, (unit, reason) in enumerate(units):
                    ready.append((unit, reason, closed and index == len(units) - 1))
                if is_active and (
                        not was_active or new_opened_at != opened_at):
                    self._arm_sync_deadline(sync_output.max_age, new_opened_at)
                elif was_active and not is_active:
                    self._cancel_sync_deadline()

        def flush_stager(reason):
            with self._sync_lock:
                was_active = sync_output.active
                units = sync_output.flush(reason)
            if was_active:
                self._cancel_sync_deadline()
            for index, (unit, fail_reason) in enumerate(units):
                ready.append((unit, fail_reason, index == len(units) - 1))

        def present_ready():
            nonlocal changed
            if not ready:
                return
            groups = []
            parts = []
            reasons = []
            for unit, reason, boundary in ready:
                parts.append(unit)
                if reason:
                    reasons.append((reason, len(unit)))
                if boundary:
                    groups.append("".join(parts))
                    parts.clear()
            if parts:
                groups.append("".join(parts))
            ready.clear()
            for reason, chars in reasons:
                _log(f"sync-output fail-open: reason={reason} chars={chars}")
            for text in groups:
                if text:
                    self._present_sync_unit(text, deferred_ui=deferred_ui)
                    changed = True

        token_stream = tokenizer.feed(chunk) if tokens is None else tokens
        for token in token_stream:
            # A tokenizer fail-open is DATA, not another chance to interpret ESC.
            raw = _literalize_control_data(token.raw) if token.literal else token.raw
            if token.kind in _OPAQUE_STRING_KINDS:
                stage(_EGC_BOUNDARY_TOKEN)
                continue                         # opaque strings never reach pyte/mirror
            if (token.kind == "csi" and token.final == "m"
                    and token.parameters[:1] in "<>="):
                stage(_EGC_BOUNDARY_TOKEN)
                continue                         # private SGR negotiation, display-inert
            if (token.kind == "csi" and token.final == "u"
                    and token.parameters[:1] in "<>=?"):
                kitty_reply = self._apply_kitty_keyboard(token)
                if kitty_reply is not None:
                    self._send_to_child(kitty_reply)
                stage(_EGC_BOUNDARY_TOKEN)
                continue                         # effect first; never leak trailing "u"

            csi_query = self._is_csi_query(token)
            if token.kind == "csi" and not csi_query:
                self._apply_dec_private(token)
            osc_reply = (
                self._osc_side_effect(token, deferred_ui=deferred_ui)
                if token.kind == "osc" else None
            )
            # Keep the reader-side tee byte-faithful.  Host-owned queries are
            # removed by the mirror hub's drain-side C1-aware strip regex.
            stage(_encode_presentation_data(raw))

            # A cursor report must include all preceding presentation, including a
            # retained synchronized frame, but never trailing presentation.
            if self._is_cursor_query(token):
                flush_stager("pending-query")
                present_ready()
            reply = osc_reply if osc_reply is not None else self._csi_query_reply(token)
            if reply is not None:
                # Enter the shared FIFO at this exact stream position. Delaying
                # replies until the end of a large chunk lets a concurrent UI
                # key overtake an earlier child query.
                self._send_to_child(reply)

        present_ready()
        return changed

    def _flush_vt_tokenizer_eof(self) -> bool:
        """Fail open the tokenizer's bounded EOF tail through normal ordering."""
        tokenizer = getattr(self, "_vt_tokenizer", None)
        if tokenizer is None:
            return False
        tokens = tokenizer.flush()
        if not tokens:
            return False
        self._ensure_sync_deadline_state()
        deferred_ui = []
        with self._sync_dispatch_lock:
            changed = self._consume_ordered(
                "", deferred_ui=deferred_ui, tokens=tokens)
        if not getattr(self, "_stop", threading.Event()).is_set():
            for callback in deferred_ui:
                self._marshal(callback)
        return changed

    def _consume_ready(self, chunk: str, deferred_ui=None) -> None:
        """Feed one complete presentation unit to pyte and its mirror."""
        if not chunk:
            return
        with self._lock:
            mirror_parts = []
            mirror_hazard = False
            try:
                # Tokenize again only at the presentation boundary. This keeps
                # screen switching and grapheme invalidation at their exact
                # stream positions even when one synchronized frame contains
                # several main/alternate transitions.
                for presentation, is_boundary in _presentation_fragments(chunk):
                    if is_boundary:
                        self._invalidate_screen_grapheme(self._screen)
                        continue
                    for token in VTTokenizer().feed(presentation):
                        mirror_parts.append(_mirror_alt_contract_token(token))
                        if token.kind != "text":
                            self._invalidate_screen_grapheme(self._screen)
                        self._apply_cursor_style(token)
                        if (token.kind == "csi"
                                and token.final in ("h", "l")
                                and not token.intermediates
                                and token.parameters.startswith("?")):
                            modes = token.parameters[1:].split(";")
                            if any(mode in _DECRQM_ALT_SCREEN for mode in modes):
                                self._switch_alt_screen_locked(token.final == "h")
                        elif (token.kind == "esc" and token.final == "c"
                              and not token.intermediates):
                            # RIS, at its stream position and BEFORE the pyte feed
                            # below, so the reset lands on the primary buffer.
                            self._apply_ris_locked()
                        screen = self._screen
                        hazard_serial = int(getattr(
                            screen, "_saikai_mirror_hazard_serial", 0))
                        if bool(getattr(
                                screen, "_saikai_mirror_has_wide_cluster", False)):
                            mirror_hazard = True
                        if token.kind == "osc":
                            self._apply_osc8_state(token)
                        try:
                            screen._saikai_active_hyperlink = getattr(
                                self, "_osc8_active", None)
                        except Exception:
                            pass
                        self._stream.feed(token.raw)
                        if int(getattr(
                                screen, "_saikai_mirror_hazard_serial", 0)
                               ) != hazard_serial:
                            mirror_hazard = True
                        self._sync_global_screen_state_locked(token)
            except Exception:
                # A malformed sequence must not kill the reader; drop rather than crash.
                pass
            self._scr_ver += 1   # screen mutated → invalidates the _current_screen cache
            # Mirror pane-direct tee — INSIDE the lock, after the pyte feed, so
            # attach_mirror()'s seed (computed under this same lock) strictly
            # precedes every chunk tee'd after it: a chunk is either in the seed
            # or in the stream, never both. The tee is a put_nowait into the hub
            # ingest queue — no marshal, no blocking, no regex (invariant #1
            # holds; the child-query strip runs on the hub's DRAIN thread via
            # set_pane_strip(_MIRROR_QUERY_STRIP_RE), so a burst never pays a
            # regex scan while holding this lock). All scrubbed presentation
            # goes through in order. Only 47/1047 are rewritten to 1049 so the
            # browser follows saikai's exact-main-restore buffer contract.
            # (#pane-direct)
            _tee = getattr(self, "_mirror_tee", None)   # getattr: minimal test
            if _tee is not None:                        # instances skip __init__
                try:
                    needs_reseed = (
                        mirror_hazard
                        or bool(getattr(
                            self, "_mirror_mode_reseed_pending", False))
                    )
                    can_reseed = (
                        getattr(self, "_mirror_reset", None) is not None
                        and getattr(self, "_mirror_synth", None) is not None
                    )
                    if not (needs_reseed and can_reseed
                            and self._mirror_reseed_locked()):
                        _tee("".join(mirror_parts))
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
            status = self._classify(_txt, _title)
            updater = self._update_status
            if getattr(updater, "__func__", None) is AgentTerminal._update_status:
                updater(status, deferred_ui=deferred_ui)
            else:
                updater(status)
        # A real BEL from the child (pyte distinguishes it from an OSC terminator):
        # ring the host bell — claude's attention signal / notification fallback.
        # Gated by SAIKAI_NO_BELL. (#bell)
        _scr = self._screen
        if _scr is not None and getattr(_scr, "_bell_rang", False):
            _scr._bell_rang = False
            if not os.environ.get("SAIKAI_NO_BELL"):
                self._queue_or_marshal(self._ring_bell, deferred_ui)

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
        now = time.monotonic()
        input_deadline_due = bool(
            not getattr(self, "_input_status_deadline_seen", True)
            and getattr(self, "_input_status_deadline", 0.0) > 0.0
            and now >= self._input_status_deadline
        )
        if (self._scr_ver == self._last_poll_ver and self._status != "busy"
                and getattr(self, "_pending_status", None) is None
                and not input_deadline_due):
            return
        if input_deadline_due:
            self._input_status_deadline_seen = True
        self._last_poll_ver = self._scr_ver
        txt, title = self._current_screen()
        self._update_status(self._classify(txt, title))

    def _update_status(self, new: str, deferred_ui=None) -> None:
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
            self._queue_or_marshal(
                lambda: self._safe_status_cb(fire), deferred_ui)
        # Leaving 'busy' = the agent storm ended and the prompt is stable. The
        # per-repaint anchor sync FROZE while busy (anti-fly), so settle it now onto
        # the resting prompt and flush. Marshalled to the UI thread (this runs on the
        # reader thread or the host poll); the sync no-ops off the focused/live pane.
        # (#agents-cursor)
        if fire is not None and fire not in ("busy", "dead"):
            self._queue_or_marshal(
                lambda: self._sync_terminal_cursor(reason="settle"), deferred_ui)

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
        self._retire_sync_deadline()
        input_timer = self._retire_input_status_timer()
        if input_timer is not None:
            # Timer.stop mutates Textual's asyncio task/event and therefore must
            # run on the UI loop, not this reader thread.
            self._marshal(lambda timer=input_timer: timer.stop())
        if not self.is_dead:
            _log(f"exit: sid={(getattr(self, 'sid', None) or '?')[:8]} (agent ended)")
        self.is_dead = True
        self._stop_writer()
        self._marshal(lambda: self._show_hw_cursor(False, force=True))
        # A pane frozen for copy/select (Shift+F9) that then dies must not stay
        # pinned to its stale snapshot — clear freeze so the final live frame shows
        # (on_key early-returns for a dead pane before its resume-unfreeze line).
        # BUT do not clobber an ACTIVE drag-selection's pinned snapshot: if the
        # child exits mid-drag, on_mouse_up still needs _frozen_buf to extract the
        # selection (else it falls back to the live/dead buffer). is_dead is set
        # ABOVE. A later drag can still select the retained final screen; only an
        # in-progress drag (sel_anchor set) needs its current snapshot preserved,
        # and its own on_mouse_up restores the state. (#audit-finalize-race)
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
        # Timeout expiry must schedule its repaint before relinquishing the
        # presentation-order fence, so retirement can linearize after it.  A
        # blocking call_from_thread here would deadlock if the UI thread were
        # simultaneously in kill()/retire waiting for that fence.  Textual's
        # call_later is backed by thread-safe post_message and does not wait for
        # the UI callback to run.
        dispatch_lock = getattr(self, "_sync_dispatch_lock", None)
        try:
            dispatch_owned = bool(
                dispatch_lock is not None and dispatch_lock._is_owned())
        except Exception:
            dispatch_owned = False
        if dispatch_owned:
            try:
                if self.call_later(self._do_pane_refresh):
                    return
            except Exception:
                pass
            self._refresh_pending = False
            return
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

    def _set_hw_cursor_shape(
            self, shape: int, *, force: bool = False, _driver=None) -> None:
        """Apply one DECSCUSR shape to the shared outer driver."""
        if not _IME_ANCHOR:
            return
        try:
            drv = _driver
            if drv is None:
                drv = getattr(self.app, "_driver", None)
            if drv is None:
                return
            desired = shape if 0 <= int(shape) <= 6 else 0
            owner = getattr(drv, "_saikai_cursor_owner", None)
            fallback_owner = (
                owner is None
                and (getattr(self, "_hw_cursor_visible", None) is True
                     or int(getattr(self, "_hw_cursor_shape", 0) or 0) != 0)
            )
            if desired == 0 and owner is not self and not fallback_owner:
                return
            applied = int(getattr(
                drv, "_saikai_cursor_shape",
                getattr(self, "_hw_cursor_shape", 0)) or 0)
            if desired != applied or (force and desired != 0):
                drv.write(f"\x1b[{desired} q")
            drv._saikai_cursor_shape = desired
            self._hw_cursor_shape = desired
        except Exception:
            pass

    def _release_hw_cursor_owner(self, driver=None) -> None:
        """Relinquish shared driver state without changing Textual's visibility."""
        try:
            drv = driver if driver is not None else getattr(
                self.app, "_driver", None)
            if drv is not None and getattr(
                    drv, "_saikai_cursor_owner", None) is self:
                drv._saikai_cursor_owner = None
        except Exception:
            pass
        self._hw_cursor_visible = None
        self._hw_cursor_shape = 0

    def _show_hw_cursor(self, show: bool, *, force: bool = False) -> None:
        """Apply child cursor shape and show/hide the REAL cursor on Windows.

        DECSCUSR shape is safe on the outer terminals Textual supports and is
        restored to Textual's default whenever the pane no longer owns the
        cursor. Visibility remains Windows-specific: there the hardware cursor
        is the IME anchor; Textual uses a software cursor elsewhere.
        Repeated identical writes are suppressed.
        (#native-cursor #agents-cursor)"""
        if not _IME_ANCHOR:
            return
        try:
            drv = getattr(self.app, "_driver", None)
        except Exception:
            return
        if drv is None:
            return
        owner = getattr(drv, "_saikai_cursor_owner", None)
        fallback_owner = (
            owner is None
            and (getattr(self, "_hw_cursor_visible", None) is True
                 or int(getattr(self, "_hw_cursor_shape", 0) or 0) != 0)
        )
        focused_owner = False
        if owner is None and not fallback_owner:
            try:
                focused_owner = self._is_focused_pane()
            except Exception:
                pass
        if show:
            try:
                drv._saikai_cursor_owner = self
            except Exception:
                pass
        elif owner is not self and not fallback_owner and not focused_owner:
            # Another pane owns the one real outer cursor. A background pane's
            # EOF/hide/unmount must not reset its shape or hide it.
            return
        elif focused_owner:
            try:
                drv._saikai_cursor_owner = self
            except Exception:
                pass
        desired_shape = (
            int(getattr(self, "_cursor_style", 0) or 0) if show else 0)
        self._set_hw_cursor_shape(
            desired_shape, force=force, _driver=drv)
        if not _native_caret():
            if not show:
                self._release_hw_cursor_owner(drv)
            return
        try:
            applied = getattr(
                drv, "_saikai_cursor_visible",
                getattr(self, "_hw_cursor_visible", None))
            if force or applied is not show:
                drv.write("\x1b[?25h" if show else "\x1b[?25l")
            drv._saikai_cursor_visible = show
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
        if not show:
            self._release_hw_cursor_owner(drv)

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
        torn = atomic = False
        if stager is not None:
            self._ensure_sync_deadline_state()
            with self._sync_lock:
                torn = stager.torn_at(now)
                atomic = stager.atomic_at(now)
        if torn:
            return True
        if getattr(self, "_status", None) != "busy":
            return False
        return not (stager is not None and atomic)

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
            # A multi-cell grapheme lives at its leader with empty stubs after it.
            # render_line walks back to the leader before drawing the software
            # caret; the anchor has to agree, or composition opens one — or for a
            # 3+ cell cluster several — columns right of the glyph being edited.
            # A refinement, so never let it abort the sync. (#flag-width #native-cursor)
            try:
                row = screen.buffer[cy]
                while cx > 0 and row[cx].data == "":
                    cx -= 1
            except Exception:
                pass
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
        if _hands_off:
            # Input/TextArea owns visibility and position, but must not inherit
            # the child pane's underline/bar DECSCUSR shape.
            self._set_hw_cursor_shape(0)
            self._release_hw_cursor_owner()
        else:
            self._show_hw_cursor(False)
        if getattr(self, "_focus_reporting", False):                # ?1004: tell the child it lost focus
            self._send_to_child("\x1b[O")
        self._cancel_forwarded_drag()          # a lost MouseUp must not stick capture

    def on_hide(self, event=None) -> None:
        """A hidden tab/screen relinquishes native cursor state completely."""
        self._show_hw_cursor(False, force=True)
        self._cancel_forwarded_drag()

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
        self._show_hw_cursor(False, force=True)
        self.kill()

    def kill(self):
        """Stop the reader and kill the child PROCESS TREE. Returns the daemon
        reap thread (or None) so a caller that must not exit before the reap
        completes (kill_all on quit) can join it. Idempotent.

        Windows: pywinpty close/terminate and taskkill all run on the tracked
        reaper.  ConPTY teardown can block, so none of it belongs on Textual's
        UI thread.

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
        self._retire_sync_deadline()
        self._stop_input_status_timer()
        # Stop acceptance and wake the persistent writer without joining it on
        # this UI-thread path. Closing/signalling below releases an in-flight
        # backend write; the worker then retires.
        writer = self._stop_writer()
        pty, pid, generation = self._lifecycle_snapshot()
        bundle = self._detach_owned_pty(pty, generation)
        if bundle is None:
            return None
        if pid:
            _log(f"kill: sid={(getattr(self, 'sid', None) or '?')[:8]} pid={pid}")
        if not _IS_WIN:
            # POSIX: signals only on this thread (see docstring); blocking close
            # stays on the reaper. SIGHUP mirrors closing a controlling master;
            # SIGTERM covers children which deliberately ignore SIGHUP.
            _post_signal(pid, "SIGHUP")
            _post_signal(pid, "SIGTERM")
        return self._start_owned_reaper(bundle, writer)

    @staticmethod
    def _join_writer_worker(writer, timeout: float = 2.0) -> None:
        """Bounded-join a pane writer from a non-UI teardown worker."""
        if writer is None or writer is threading.current_thread():
            return
        try:
            writer.join(timeout=max(0.0, float(timeout)))
        except Exception:
            pass

    def _start_owned_reaper(self, bundle, writer=None, *, natural=False):
        """Start and globally track cleanup for one detached PTY generation."""
        if bundle is None:
            return None
        pty, pid, generation = bundle
        target = self._reap_windows if _IS_WIN else self._reap_posix
        eof_event = self._generation_eof_event(generation)
        args = ((pty, pid, writer, natural, eof_event)
                if _IS_WIN else (pty, pid, 2.0, writer))
        thread = threading.Thread(
            target=target,
            args=args,
            name=f"reap-{pid or 'pty'}",
            daemon=True,
        )
        thread.start()
        _track_reap(thread)
        return thread

    @staticmethod
    def _reap_windows(
            pty, pid, writer=None, natural=False, eof_event=None) -> None:
        """Close ConPTY and reap its process tree entirely off the UI thread."""
        if eof_event is not None and eof_event.is_set():
            natural = True
        handle_proves_live_child = False
        # pywinpty's process liveness check is handle-backed. It closes the
        # tiny race where EOF returned but the reader has not yet published its
        # generation marker; unlike a bare PID lookup it cannot bless reuse.
        if not natural and pty is not None:
            try:
                handle_proves_live_child = bool(pty.isalive())
                if not handle_proves_live_child:
                    natural = True
            except Exception:
                # Fail closed: without the handle-backed identity check, a bare
                # numeric PID may already name an unrelated process.
                handle_proves_live_child = False
        # taskkill must enumerate the tree while the root PID still exists.
        # Closing ConPTY first can terminate that root and make /T miss already
        # detached descendants.
        if pid and not natural and handle_proves_live_child:
            AgentTerminal._reap_tree(pid)
        if pty is not None:
            try:
                pty.close(force=True)
            except Exception:
                try:
                    pty.terminate(force=True)
                except Exception:
                    pass
        # Natural EOF, a dead handle, or an unreadable handle skips taskkill:
        # none authorizes acting on a possibly recycled bare numeric PID.
        AgentTerminal._join_writer_worker(writer)

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
    def _reap_posix(
            pty, pid, deadline_s: float = 2.0, writer=None) -> None:
        # POSIX analog of _reap_tree: bounded wait for the (already signalled)
        # child to die, escalate to SIGKILL, then close the pty fd. The close
        # MUST stay off the UI thread — BufferedRWPair.close() blocks on the
        # reader lock until the reader unblocks at EOF; harmless on this daemon
        # (joined bounded at quit/atexit), fatal on the UI thread. deadline_s is
        # injectable for the headless tests.
        deadline = time.monotonic() + max(0.0, float(deadline_s))
        direct_alive = pty is not None and _safe_isalive(pty)
        group_alive = _process_group_alive(pid)
        while ((direct_alive or group_alive)
               and time.monotonic() < deadline):
            time.sleep(0.05)
            direct_alive = pty is not None and _safe_isalive(pty)
            group_alive = _process_group_alive(pid)
        if direct_alive or group_alive:
            _post_signal(pid, "SIGKILL")
        if pty is not None:
            # close() takes the BufferedRWPair reader lock that the reader holds in
            # read1() until the master EOFs. The child's death normally EOFs it and
            # close() returns at once — but a grandchild that survived and kept the
            # slave fd open means no EOF, so close() would block THIS reap thread
            # forever (and join_reaps at quit would only time out, leaking it). We
            # The owned process group was checked/escalated above, but a
            # process-group-escaping grandchild can still retain the slave.
            # Run close() on a tracked helper and stop waiting after a bound —
            # normally it returns instantly; in the stuck case this reap still
            # completes and the fd is reclaimed at process exit. (#9)
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
        AgentTerminal._join_writer_worker(writer)

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
        join_all_pty_writers(timeout=max(0.0, deadline - time.monotonic()))

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
