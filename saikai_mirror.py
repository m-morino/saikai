"""Opt-in, loopback/LAN, read-only web mirror of a running saikai session.

Lives in the application layer. It tees the bytes Textual's driver is already
about to write, so the local console is byte-identical and untouched. No second
App, no second PTY, no daemon outliving the App, no transcript writes. Provider-
neutral terminal code (saikai_terminal.py) gains ZERO network code.
"""
from __future__ import annotations

import base64
import collections
import hmac
import http.server
import ipaddress
import json
import os
import queue
import re
import socketserver
import sys
import threading
import pyte
from typing import Optional


# A control frame travels over the SAME per-client queue as output frames, but
# wrapped so _stream can send it as a named SSE event instead of base64 output.
_Control = collections.namedtuple("_Control", ["json"])
# Host-layout frame: cell-space rectangles of the SCROLLABLE content areas
# (session list, visible live pane). The select-mode edge auto-scroll needs
# them — the pane's top/bottom edges live mid-canvas, so a canvas-edge zone
# never fires for a selection INSIDE the pane. (#mirror-regions)
_Regions = collections.namedtuple("_Regions", ["json"])
# Size frame: the host terminal was resized. The browser xterm was sized at
# page load and stays fixed otherwise, so absolute-positioned host ANSI would
# garble against a stale grid — it must re-size live. (#mirror-resize)
_Size = collections.namedtuple("_Size", ["json"])
# ── Pane direct view (#pane-direct) ──────────────────────────────────────────
# The app view re-renders the child through pyte + Textual — a double emulation
# that loses cursor shape, mouse reporting and frame timing. The pane channel
# instead tees the child PTY's (scrubbed) byte stream straight to the browser
# xterm, which then IS the child's terminal. Three typed frames ride the same
# ingest queue as app output so pane data, reseeds and meta stay ordered:
_PaneData = collections.namedtuple("_PaneData", ["data"])    # raw child bytes
# A reseed CARRIES the current meta: geometry must be applied before the seed
# paints (the browser resizes, then resets+writes), and a _PaneMeta lost to any
# backlog flush is re-delivered by the very next reseed — set_pane_meta dedups
# at source, so nothing else would ever resend it. (#review-pane-meta-loss)
_PaneReset = collections.namedtuple("_PaneReset", ["seed", "meta"])
_PaneMeta = collections.namedtuple("_PaneMeta", ["json"])    # geometry/liveness
# Clipboard frame: the child's OSC 52 copy, relayed so "copy in claude" lands
# on the DEVICE THE VIEWER IS HOLDING. App-view clients need it (their stream
# is Textual frames — OSC 52 never reaches them); pane-view pages decode OSC 52
# themselves and ignore this event. (#app-native-select)
_Clip = collections.namedtuple("_Clip", ["json"])
# The pane tee may not starve app frames out of the SHARED ingest queue: raw
# child output is unthrottled (a flood workload writes at PTY throughput while
# Textual frames are render-paced), so pane data is capped to half the queue —
# beyond it chunks are dropped and a reseed heals the gap. (#review-pane-flood)
_PANE_INFLIGHT_CAP = 128
# A DCS that saikai answers itself and must strip before the browser xterm can
# auto-answer it: DECRQSS (ESC P $ q …) and XTGETTCAP (ESC P + q …). Used to
# give a split target query a larger reassembly hold than a passed-through
# sixel. (#review-dcs-bound)
_DCS_TARGET_RE = re.compile(r"\x1bP[0-9;]*[$+]q")

# Brute-force throttle for the SSE write-key: after this many bad attempts,
# refuse input for a cooldown so a lost/guessed key can't be hammered. (#audit-mirror-ratecap)
_BAD_KEY_LOCKOUT_THRESHOLD = 20
_BAD_KEY_LOCKOUT_SECS = 30.0
_BAD_KEY_TTL_SECS = 600.0   # idle sweep age for sub-threshold per-source entries
# Read-token guessing gets its OWN per-source lockout (separate budget from the
# write-key, so guessing one can't consume the other's) — the read token gates the
# SSE stream that hands out the write-key, so hammering it must also be bounded.
_BAD_TOKEN_LOCKOUT_THRESHOLD = 50
# A source that has presented a VALID credential (read token or write-key) within
# this window is EXEMPT from both lockouts — so an attacker sharing the operator's
# IPv6 /64 (or the operator's own stale-token browser tab) can't lock out the real
# operator's device, while a peer that never authenticated stays fully throttled.
_PROVEN_TTL_SECS = 3600.0
# Availability caps (a single-user LAN mirror never needs many of any of these).
# The connection + timeout caps bound an UNAUTHENTICATED Slowloris/socket flood
# that would otherwise park a blocked thread per socket and freeze the host's own
# UI thread; the client cap bounds a token-holder opening many SSE streams. Sized
# for a single user (a browser opens ~6 parallel + one SSE), not a server. (#audit-mirror-dos)
_MAX_SSE_CLIENTS = 8        # concurrent /stream viewers
_MAX_CONNECTIONS = 48       # concurrent accepted sockets, all sources
_MAX_CONN_PER_IP = 12       # concurrent accepted sockets from one source IP
_CONN_TIMEOUT = 20          # seconds; a socket idle on header/body reads is dropped


def _paste_framing_ok(data: str) -> bool:
    """Reject a browser /input batch that opens a bracketed paste (ESC[200~) and
    embeds a raw ESC before the matching ESC[201~. A well-behaved browser client
    never produces this (its JS flushes on every control byte); only a direct API
    caller can, and it's the smuggling primitive that would reconstruct arbitrary
    control bytes into the child PTY keystroke-by-keystroke via the parser's
    escape-resolution fallback. Treat inside-paste as an unconditional raw region:
    no interior ESC allowed (this also rejects a nested ESC[200~ — it starts with
    ESC). (#audit-mirror-paste-smuggle)

    NOTE (#review-paste-earlyclose): an *early-close* shape (ESC[201~ then live
    keystrokes) is deliberately NOT rejected here. /input requires the write key,
    so the sender is a control-holder who can already send those same keystrokes
    directly (or verbatim via /raw) — it is not a privilege boundary. The real
    victim of an accidental early-close is the user's OWN composer paste, and
    that is prevented at the source by stripping embedded markers before framing
    (see the composer's marker-removal loop). Rejecting a lone trailing close
    here would instead break a legitimately split large paste."""
    i = data.find("\x1b[200~")
    while i != -1:
        end = data.find("\x1b[201~", i + 6)
        region = data[i + 6:] if end == -1 else data[i + 6:end]
        if "\x1b" in region:                 # any ESC inside the paste body → smuggling
            return False
        if end == -1:
            break
        i = data.find("\x1b[200~", end + 6)
    return True


def _norm_src(ip: str) -> str:
    """Collapse a peer address to a stable lockout identity so one attacker can't
    rotate identities to dodge the per-source cooldown / connection cap: a
    v4-mapped-v6 address folds to its bare v4 (so '::ffff:1.2.3.4' and '1.2.3.4'
    are ONE source), and a real IPv6 address folds to its /64 network (a single
    LAN host owns every address in its prefix and SLAAC privacy addresses rotate
    within it). Canonicalised via `ipaddress` so the SAME address in different
    textual forms (compressed '2001:db8::1' vs expanded) maps to ONE key — a naive
    string split would give a compressed and an expanded form two buckets and hand
    the attacker back the identity-rotation it's meant to prevent."""
    ip = (ip or "?").strip()
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return ip                            # hostname / "?" / malformed → as-is
    if addr.version == 6:
        mapped = addr.ipv4_mapped
        if mapped is not None:
            return str(mapped)               # ::ffff:v4 → the bare v4
        net = ipaddress.ip_network((int(addr) >> 64 << 64, 64))
        return f"{net.network_address}/64"   # canonical /64 network address
    return str(addr)                         # canonical v4


# pyte stores a colour as: a basic name, a "bright"+name, a 6-hex string
# (256-colour and truecolor both collapse to hex), or "default".
_BASIC = {"black": 0, "red": 1, "green": 2, "brown": 3, "blue": 4,
          "magenta": 5, "cyan": 6, "white": 7}


def _color_sgr(value: str, fg: bool) -> list[int]:
    """SGR params for a pyte colour string, for foreground (fg=True) or bg.

    Handles basic names (30-37/40-47), bright names (90-97/100-107), and
    6-hex 256/truecolor (38;2;r;g;b / 48;2;r;g;b). Earlier this only knew the 8
    basic names, so Textual's bright/accent colours fell back to default and
    coloured borders rendered wrong."""
    if not value or value == "default":
        return [39 if fg else 49]
    if value in _BASIC:
        return [(30 if fg else 40) + _BASIC[value]]
    if value.startswith("bright") and value[6:] in _BASIC:
        return [(90 if fg else 100) + _BASIC[value[6:]]]
    if len(value) == 6:
        try:
            r, g, b = (int(value[i:i + 2], 16) for i in (0, 2, 4))
            return [(38 if fg else 48), 2, r, g, b]
        except ValueError:
            pass
    return [39 if fg else 49]


def _cell_attrs(ch: "pyte.screens.Char") -> tuple:
    """Return a hashable key of all visual attributes for a pyte Char."""
    return (ch.bold, ch.italics, ch.underscore, ch.reverse, ch.fg, ch.bg)


def _attrs_to_sgr(attrs: tuple) -> str:
    """Convert a cell-attrs tuple to an ANSI SGR escape string.

    Emits ESC[0m to reset, then each non-default attribute as its own minimal
    escape so that, for example, a red foreground appears as the literal
    substring ESC[31m rather than being merged into a multi-param sequence."""
    bold, italics, underscore, reverse, fg, bg = attrs
    parts = ["\x1b[0m"]   # always reset first
    if bold:
        parts.append("\x1b[1m")
    if italics:
        parts.append("\x1b[3m")
    if underscore:
        parts.append("\x1b[4m")
    if reverse:
        parts.append("\x1b[7m")
    fg_codes = _color_sgr(fg, True)
    if fg_codes != [39]:               # 39 already covered by the reset
        parts.append("\x1b[" + ";".join(str(c) for c in fg_codes) + "m")
    bg_codes = _color_sgr(bg, False)
    if bg_codes != [49]:
        parts.append("\x1b[" + ";".join(str(c) for c in bg_codes) + "m")
    return "".join(parts)


def _synth_full_frame(screen: "pyte.Screen", cols: int, rows: int) -> str:
    """Render a pyte screen to a self-contained full-repaint ANSI string so a
    late-joining browser gets complete state before the live diff stream.

    Characters with identical attributes are grouped into runs so that plain
    text appears as contiguous substrings and color SGRs are emitted once per
    run rather than once per cell."""
    out = ["\x1b[2J\x1b[H"]   # clear + home
    for y in range(rows):
        line = screen.buffer[y]
        out.append(f"\x1b[{y + 1};1H")   # absolute row, col 1
        run_attrs: tuple | None = None
        run_text: list[str] = []
        for x in range(cols):
            ch = line[x]
            if ch.data == "":
                continue            # wide-char (CJK) continuation cell: the
                                    # preceding 2-wide glyph already covers this
                                    # column. Emitting a space here would shift
                                    # the rest of the line right (garbled JP rows).
            attrs = _cell_attrs(ch)
            glyph = ch.data or " "
            if attrs != run_attrs:
                # Flush previous run.
                if run_text:
                    out.append("".join(run_text))
                # Emit SGR for new run.
                out.append(_attrs_to_sgr(attrs))
                run_attrs = attrs
                run_text = [glyph]
            else:
                run_text.append(glyph)
        if run_text:
            out.append("".join(run_text))
    out.append("\x1b[0m")
    cy, cx = screen.cursor.y, screen.cursor.x
    out.append(f"\x1b[{cy + 1};{cx + 1}H")
    return "".join(out)


def _synth_pane_seed(screen: "pyte.Screen", cols: int, rows: int,
                     modes: dict) -> str:
    """Serialize a live pane's CURRENT state — grid, cursor AND terminal modes —
    into one self-contained ANSI string, so a browser xterm joining the raw
    pane stream mid-session starts from exactly where the child's terminal is.

    The mode replay matters as much as the grid: the child enabled alt-screen /
    mouse tracking / bracketed paste BEFORE this client connected, and without
    replaying them the browser xterm would neither generate mouse reports nor
    frame pastes until the child happens to re-assert them. Every tracked mode
    is emitted explicitly (set OR reset) so the seed is state-idempotent — it
    lands the terminal in the same state whether applied after a term.reset()
    or over a stale previous stream. (#pane-direct)"""
    def _m(flag: str, seq: str) -> str:
        return f"\x1b[?{seq}{'h' if modes.get(flag) else 'l'}"
    # Mouse tracking (1000/1002/1003) is ONE exclusive protocol slot, and
    # xterm.js resets it to NONE on ANY of the three 'l's regardless of which
    # protocol is active (verified against the vendored 5.5 source — a naive
    # h-then-l replay left tracking OFF). Reset the slot once, then enable only
    # the STRONGEST tracked mode (any > drag > click), matching what a real
    # terminal ends up with when the child stacked several enables.
    mouse = ""
    for flag, seq in (("mouse_any_motion", "1003"),
                      ("mouse_btn_motion", "1002"),
                      ("mouse_click", "1000")):
        if modes.get(flag):
            mouse = f"\x1b[?{seq}h"
            break
    parts = [
        # Alt-screen FIRST: the paint below must land in the buffer the child
        # is actually drawing into.
        _m("alt", "1049"),
        _synth_full_frame(screen, cols, rows),
        _m("app_cursor", "1"),             # DECCKM — arrow keys SS3 vs CSI
        "\x1b[?1000l\x1b[?1002l\x1b[?1003l",   # clear the protocol slot…
        mouse,                                  # …then the strongest enable
        _m("focus_reporting", "1004"),
        _m("mouse_sgr", "1006"),
        _m("bracketed_paste", "2004"),
        # Cursor visibility LAST (the paint above ends on a cursor move).
        "\x1b[?25l" if modes.get("cursor_hidden") else "\x1b[?25h",
    ]
    return "".join(parts)


class MirrorHub:
    def __init__(self, token: str, host: str = "127.0.0.1", port: int = 0,
                 cols: int = 80, rows: int = 24, ingest_cap: int = 256,
                 idle_secs: float = 600.0, tls: "tuple[str, str] | None" = None) -> None:
        self._token = token
        self._host = host
        self._port = port
        # TLS: (certfile, keyfile) when the transport is encrypted, else None.
        # When set, serve() wraps the listening socket and url() advertises https.
        self._tls = tls
        self._scheme = "https" if tls else "http"
        self._cols = cols
        self._rows = rows
        self._ingest: queue.Queue[str] = queue.Queue(ingest_cap)
        self._mirror_lock = threading.Lock()
        self._screen = pyte.Screen(cols, rows)
        self._stream = pyte.Stream(self._screen)
        self._clients: "set[queue.Queue]" = set()
        self._clients_lock = threading.Lock()
        self._httpd: Optional["_Server"] = None
        self._drain: Optional[threading.Thread] = None
        self._stopped = threading.Event()
        self._repaint_request = None
        # ── Phase B: interactive control (default OFF; app is the authority) ──
        import secrets as _secrets
        self._control_enabled = False          # advisory cache of the app's gate
        self._input_handler = None             # _marshal-shaped, set at app mount
        self._mouse_handler = None             # _marshal-shaped, set at app mount
        self._key_handler = None               # _marshal-shaped, set at app mount
        self._raw_handler = None               # _marshal-shaped, set at app mount (#pane-direct)
        self._client_change_handler = None     # notified (count) on connect/disconnect
        self._control_change_handler = None    # notified (bool) on HUB-initiated control changes
        self._control_target = None            # focused-pane title (advisory)
        # Write-key: NEVER placed in any URL/file/QR/log; delivered only over the
        # authenticated SSE stream and required as the X-Mirror-Write-Key header.
        self._write_key = _secrets.token_urlsafe(32)
        self._inject_q: queue.Queue[str] = queue.Queue(1024)
        self._inject_drain = None
        # Idle auto-disable + abuse counters.
        self._idle_secs = idle_secs
        self._idle_timer = None
        self._idle_lock = threading.Lock()
        # Per-source (peer IP) bad-key bookkeeping. Global counters let one
        # abusive peer's guesses trip a hub-wide lockout that also refuses the
        # legitimate operator's correct key — a same-LAN DoS knob. Keying by the
        # TCP peer IP (which can't be spoofed on an established connection) bounds
        # a lockout to the offending source. One dict so count/deadline can't
        # drift apart; swept on every note so sub-threshold strays that never
        # return can't grow it for the life of the process. (#audit-mirror-ratecap)
        self._bad_key_lock = threading.Lock()
        # src IP -> (consecutive bad keys, lockout deadline, last seen) — monotonic.
        # Two independent budgets: write-key guesses and read-token guesses.
        self._bad_key: dict[str, tuple[int, float, float]] = {}
        self._bad_token: dict[str, tuple[int, float, float]] = {}
        self._proven: dict[str, float] = {}   # normalised src -> monotonic exempt-until
        self._last_accept_t = 0.0
        # Accepted-input rate cap (seconds between accepted injects). Now actually
        # REACHABLE at runtime via env (was hardcoded 0.0 → the documented flood
        # control never engaged); the LAN opt-in also sets a default floor. (#audit-mirror-ratecap)
        try:
            self._min_accept_gap = max(0.0, float(os.environ.get("SAIKAI_MIRROR_MIN_ACCEPT_GAP", "0") or 0))
        except (TypeError, ValueError):
            self._min_accept_gap = 0.0
        self._ingest_overflow = False  # set by broadcast() on overflow → drain requests a resync
        self.allow_lan_input = False  # set True only via the launch opt-in
        # ── Pane direct view (#pane-direct) ───────────────────────────────────
        # Pane-view SSE clients get the child's raw byte stream, not app frames.
        # The hub keeps NO pane pyte model: the AgentTerminal's pyte screen is
        # the single authority, and any recovery (fresh client, fallen-behind
        # client, ingest overflow) asks the APP for a reseed via the marshal-
        # shaped callback below — a full-state _PaneReset then flows through the
        # ordered ingest queue to every pane client.
        self._pane_clients: "set[queue.Queue]" = set()   # guarded by _clients_lock
        self._pane_meta_json = json.dumps({"open": False}, sort_keys=True)
        self._pane_reseed_request = None    # app-provided, marshal-shaped
        self._pane_reseed_pending = 0.0     # monotonic deadline; 0 = none pending
        self._pane_lost = False             # ingest overflow flushed pane frames
        self._pane_strip = None             # child-query strip regex (drain-side)
        self._pane_strip_carry = ""         # trailing split DCS held across chunks
        self._pane_inflight = 0             # _PaneData currently in the ingest queue
        self._pane_count_lock = threading.Lock()

    def _feed(self, data: str) -> None:
        with self._mirror_lock:
            self._stream.feed(data)

    def _snapshot(self) -> str:
        with self._mirror_lock:
            return _synth_full_frame(self._screen, self._cols, self._rows)

    @staticmethod
    def _resync_client(cq, snapshot, control=None) -> None:
        """Replace a fallen-behind client's backlog with a single full repaint
        (+ the current control frame), so a dropped incremental diff becomes ONE
        clean resync instead of permanent visual corruption / a stale banner. The
        browser has no gap detection, so drop-oldest there is unrecoverable.
        (#audit-mirror-sse-drop / #audit-mirror-control-loss)"""
        try:
            while True:
                cq.get_nowait()
        except queue.Empty:
            pass
        try:
            cq.put_nowait(snapshot)
            if control is not None:
                cq.put_nowait(control)
        except queue.Full:
            pass

    def _note_bad(self, store: dict, src: str, threshold: int) -> None:
        """Count a bad-credential attempt from `src` into `store`; arm a per-source
        cooldown at `threshold` so one peer's guesses can't lock out anyone else.
        `src` is normalised (v4-mapped-v6 / IPv6-/64) so identity-rotation can't
        dodge it. Sweeps entries idle past _BAD_KEY_TTL_SECS (and not locked) so
        many one-off strangers can't leak an entry each forever."""
        import time as _t
        src = _norm_src(src)
        now = _t.monotonic()
        with self._bad_key_lock:
            for k in [k for k, (_, until, seen) in store.items()
                      if now >= until and now - seen > _BAD_KEY_TTL_SECS]:
                del store[k]
            n = store.get(src, (0, 0.0, 0.0))[0] + 1
            until = now + _BAD_KEY_LOCKOUT_SECS if n >= threshold else 0.0
            store[src] = (n, until, now)

    def _clear_bad(self, store: dict, src: str) -> None:
        with self._bad_key_lock:
            store.pop(_norm_src(src), None)

    def _locked_out(self, store: dict, src: str) -> bool:
        import time as _t
        src = _norm_src(src)
        with self._bad_key_lock:
            until = store.get(src, (0, 0.0, 0.0))[1]
            if until <= 0.0:
                return False
            if _t.monotonic() < until:
                return True
            store.pop(src, None)
            return False

    def _mark_proven(self, src: str) -> None:
        """Record that `src` presented a VALID credential — exempting it from both
        lockouts for _PROVEN_TTL_SECS. Sweeps expired entries so it stays bounded."""
        import time as _t
        now = _t.monotonic()
        with self._bad_key_lock:
            for k in [k for k, exp in self._proven.items() if exp <= now]:
                del self._proven[k]
            self._proven[_norm_src(src)] = now + _PROVEN_TTL_SECS

    def _is_proven(self, src: str) -> bool:
        import time as _t
        with self._bad_key_lock:
            return self._proven.get(_norm_src(src), 0.0) > _t.monotonic()

    # Write-key wrappers (public names kept for the tests + call sites).
    def _note_bad_key(self, src: str = "?") -> None:
        self._note_bad(self._bad_key, src, _BAD_KEY_LOCKOUT_THRESHOLD)

    def _clear_bad_key(self, src: str = "?") -> None:
        self._clear_bad(self._bad_key, src)

    def _input_locked_out(self, src: str = "?") -> bool:
        # A proven source (recently authenticated) is exempt, so a hostile peer
        # sharing its /64 can't lock out the real operator's device.
        return not self._is_proven(src) and self._locked_out(self._bad_key, src)

    # Read-token wrappers (separate budget so guessing one secret can't consume the
    # other's cooldown, and a token guess is bounded just like a write-key guess).
    def _note_bad_token(self, src: str = "?") -> None:
        self._note_bad(self._bad_token, src, _BAD_TOKEN_LOCKOUT_THRESHOLD)

    def _token_locked_out(self, src: str = "?") -> bool:
        return not self._is_proven(src) and self._locked_out(self._bad_token, src)

    def broadcast(self, data: str) -> None:
        """Called from Textual's UI thread (MirrorDriver.write). MUST NOT block.
        Drop the oldest frame when the ingest queue is full. Best-effort: under
        concurrent producers strict FIFO is not guaranteed and the new frame may
        itself be dropped — never blocking the UI thread is the only invariant.
        In practice there is a single producer (the driver runs on the UI
        thread), so the queue stays FIFO."""
        self._ingest_put(data)

    def _ingest_put(self, item) -> None:
        """Non-blocking put shared by app frames (bare str) and typed pane
        frames. On overflow, do NOT drop a single oldest chunk: Textual splits
        one logical frame into multiple chunks, so dropping a MIDDLE chunk
        splices two unrelated byte ranges and permanently corrupts the server
        pyte mirror — and the pane byte stream has the same property. Discard
        the whole stale backlog and flag BOTH recoveries: the drain loop
        requests an app repaint (resets the app pyte cleanly) and a pane reseed
        (a _PaneReset resets every pane client). (#audit-mirror-broadcast-splice)"""
        try:
            self._ingest.put_nowait(item)
        except queue.Full:
            try:
                while True:
                    self._ingest.get_nowait()
            except queue.Empty:
                pass
            self._ingest_overflow = True
            self._pane_lost = True
            with self._pane_count_lock:
                self._pane_inflight = 0   # the flush discarded any queued pane data
            try:
                self._ingest.put_nowait(item)
            except queue.Full:
                pass   # never block the UI thread

    def _add_client(self):
        """Register a new SSE viewer. Returns (cq, snapshot), or (None, None) when
        the concurrent-viewer cap is hit — a single-user mirror never needs many
        streams, so the cap bounds a token-holder opening streams in a loop (each
        one forces a UI-thread repaint)."""
        cq: "queue.Queue[Optional[str]]" = queue.Queue(256)
        # Snapshot + registration ATOMIC vs the drain thread's feed, so no diff
        # is lost or applied twice across the join boundary.
        with self._mirror_lock:
            snapshot = _synth_full_frame(self._screen, self._cols, self._rows)
            with self._clients_lock:
                if len(self._clients) + len(self._pane_clients) >= _MAX_SSE_CLIENTS:
                    return None, None
                self._clients.add(cq)
        if self._repaint_request is not None:
            try:
                self._repaint_request()
            except Exception:
                pass
        self._notify_client_change()
        return cq, snapshot

    def _add_pane_client(self):
        """Register a pane-view SSE viewer (raw child stream, not app frames).
        Shares the viewer cap with app clients. Returns the client queue or None
        at the cap. The caller gets NO snapshot — pane state arrives as a
        _PaneReset once the app answers the reseed request fired here."""
        cq: "queue.Queue" = queue.Queue(256)
        with self._clients_lock:
            if len(self._clients) + len(self._pane_clients) >= _MAX_SSE_CLIENTS:
                return None
            self._pane_clients.add(cq)
        self._notify_client_change()
        self._request_pane_reseed()
        return cq

    def _remove_client(self, cq):
        with self._clients_lock:
            self._clients.discard(cq)
            self._pane_clients.discard(cq)
        self._notify_client_change()

    def client_count(self) -> int:
        """How many browsers currently hold the SSE stream open (≈ open tabs)."""
        with self._clients_lock:
            return len(self._clients) + len(self._pane_clients)

    def set_client_change_handler(self, fn) -> None:
        # Written from the UI thread (on_mount), read from the HTTP server thread.
        # Single attribute assign/read is GIL-atomic (same as set_input_handler).
        self._client_change_handler = fn

    def set_control_change_handler(self, fn) -> None:
        # Same GIL-atomic single-attribute pattern. Called with the new bool state
        # when the HUB (not the app) changes control — today only the idle auto-off
        # timer. App-initiated toggles sync via set_control_state's return value
        # instead, so this never fires on the UI thread (call_from_thread inside
        # the handler would raise there).
        self._control_change_handler = fn

    def _notify_client_change(self) -> None:
        # Called from the HTTP server thread on connect/disconnect. The handler is
        # _marshal-shaped (it bounces to the UI thread); best-effort, never raises.
        fn = self._client_change_handler
        if fn is not None:
            try:
                fn(self.client_count())
            except Exception:
                pass

    def _drain_loop(self):
        while not self._stopped.is_set():
            try:
                data = self._ingest.get(timeout=0.25)
            except queue.Empty:
                continue
            if isinstance(data, (_PaneData, _PaneReset, _PaneMeta)):
                self._drain_pane_frame(data)
                # A pane frame never touches the app pyte; fall through only to
                # the overflow recovery below.
                self._drain_overflow_recovery()
                continue
            with self._mirror_lock:
                self._stream.feed(data)
                with self._clients_lock:
                    targets = list(self._clients)
                snapshot = None      # computed lazily, under the lock, for resyncs
                ctrl = None
                for cq in targets:
                    try:
                        cq.put_nowait(data)
                    except queue.Full:
                        # Fallen behind: resync with a full repaint instead of
                        # drop-oldest (which splices the diff stream into permanent
                        # corruption). (#audit-mirror-sse-drop)
                        if snapshot is None:
                            snapshot = _synth_full_frame(self._screen, self._cols, self._rows)
                            if self._control_enabled:
                                ctrl = _Control(json.dumps(
                                    {"on": True, "target": self._control_target}))
                        self._resync_client(cq, snapshot, ctrl)
            self._drain_overflow_recovery()

    def _strip_pane_chunk(self, strip, text: str) -> str:
        """Strip child terminal queries from a pane chunk, HOLDING a DCS split
        across the chunk boundary. The reader thread already reassembles a split
        CSI/OSC query via _esc_carry (so those never arrive halved here), but not
        a DCS (ESC P … ST) — DECRQSS / XTGETTCAP could land split, sail past the
        stateless regex, and let the browser xterm auto-answer the child. Carry
        the trailing unterminated DCS to the next chunk. A STRIP-TARGET DCS
        ($q/+q) gets a large hold so a long query can't slip past the bound and
        reach the browser un-stripped (#review-dcs-bound); a non-target DCS
        (sixel — passed through anyway) is released at a small bound to cap
        latency/memory. Drain thread only. (#review-dcs-split)"""
        carry, self._pane_strip_carry = self._pane_strip_carry, ""
        if carry:
            text = carry + text
        p = text.rfind("\x1bP")
        if p != -1:
            tail = text[p:]
            if "\x07" not in tail and "\x1b\\" not in tail:
                limit = 8192 if _DCS_TARGET_RE.match(tail) else 512
                if len(tail) < limit:
                    self._pane_strip_carry = tail
                    text = text[:p]
        return strip.sub("", text)

    @staticmethod
    def _offer_sentinel(cq) -> None:
        """Deliver the stop() shutdown sentinel even to a FULL client queue: make
        room by dropping one frame (the client is stopping — its backlog is moot)
        so the SSE handler's cq.get() returns None and the thread exits, instead
        of looping on 30s keepalives until process death. (#review-stop-sentinel)"""
        try:
            cq.put_nowait(None)
        except queue.Full:
            try:
                cq.get_nowait()
            except queue.Empty:
                pass
            try:
                cq.put_nowait(None)
            except queue.Full:
                pass

    @staticmethod
    def _flush_pane_backlog(cq) -> None:
        """Drain a pane client's queue PRESERVING what a reseed cannot restore:
        the last unconsumed _Control (the banner/input gate is only ever sent on
        a state CHANGE, #audit-mirror-control-loss), the last _PaneMeta
        (geometry — deduped at source), and the stop() sentinel (its loss would
        leave the SSE thread looping on keepalives after shutdown). Pane data
        and stale resets are the only frames dropped — the reseed replaces
        exactly those. One helper for every pane flush site so the preservation
        can't drift per-branch. (#review-pane-frame-loss)"""
        ctrl = meta = None
        sentinel = False
        try:
            while True:
                item = cq.get_nowait()
                if item is None:
                    sentinel = True
                elif isinstance(item, _Control):
                    ctrl = item
                elif isinstance(item, _PaneMeta):
                    meta = item
        except queue.Empty:
            pass
        for it in (ctrl, meta):
            if it is not None:
                try:
                    cq.put_nowait(it)
                except queue.Full:
                    pass
        if sentinel:                 # last: the client handles frames, then exits
            try:
                cq.put_nowait(None)
            except queue.Full:
                pass

    def _drain_pane_frame(self, data) -> None:
        """Fan a typed pane frame out to the pane-view clients (drain thread).
        A fallen-behind client can't be healed by drop-oldest (splicing a raw
        byte stream corrupts it permanently) — flush its backlog (critical
        frames preserved) and ask the app for a reseed; the arriving _PaneReset
        replaces everyone's backlog with one clean full state. (#pane-direct)"""
        if isinstance(data, _PaneData):
            with self._pane_count_lock:
                self._pane_inflight = max(0, self._pane_inflight - 1)
            strip = self._pane_strip
            if strip is not None:
                try:
                    data = _PaneData(self._strip_pane_chunk(strip, data.data))
                except Exception:
                    pass
            if not data.data:
                return
        elif isinstance(data, _PaneReset):
            # A reseed is a STREAM BOUNDARY (fresh client / retarget / reopen /
            # overflow): drop any half-carried DCS from the OLD stream so it
            # can't prefix the new pane's first bytes and swallow them into a
            # bogus unterminated query. (#review-carry-boundary)
            self._pane_strip_carry = ""
        with self._clients_lock:
            targets = list(self._pane_clients)
        if not targets:
            return
        if isinstance(data, _PaneReset):
            for cq in targets:
                self._flush_pane_backlog(cq)
                try:
                    cq.put_nowait(data)
                except queue.Full:
                    pass
            return
        need_reseed = False
        for cq in targets:
            try:
                cq.put_nowait(data)
            except queue.Full:
                self._flush_pane_backlog(cq)
                need_reseed = True
                if isinstance(data, _PaneMeta):
                    try:
                        cq.put_nowait(data)   # newer than any meta the flush kept
                    except queue.Full:
                        pass
        if need_reseed:
            self._request_pane_reseed()

    def _drain_overflow_recovery(self) -> None:
        """Server pyte may have lost data on an ingest overflow → ask the app for
        a full repaint so a clean frame resets it; a flushed backlog also dropped
        any pane frames → ask for a pane reseed too. Done OFF the mirror lock and
        from the drain thread (broadcast() on the UI thread can't call
        _repaint_request, which marshals via call_from_thread). (#audit-mirror-broadcast-splice)"""
        if self._ingest_overflow:
            self._ingest_overflow = False
            fn = self._repaint_request
            if fn is not None:
                try:
                    fn()
                except Exception:
                    pass
        if self._pane_lost:
            self._pane_lost = False
            # the flush discarded queued pane data mid-stream — a half-carried
            # DCS from before the gap would corrupt the post-reseed bytes
            self._pane_strip_carry = ""
            self._request_pane_reseed()

    def serve(self) -> int:
        self._httpd = _Server((self._host, self._port), _Handler)
        self._httpd.hub = self
        self._port = self._httpd.server_address[1]
        # TLS termination: wrap the LISTENING socket so every accepted connection
        # does a handshake before any HTTP is read — closes the cleartext-sniffing
        # vector (token, write-key, keystrokes) on a hostile LAN. A plain-http
        # client that connects to the https port simply fails the handshake and is
        # dropped by the per-connection timeout. (#audit-mirror-tls)
        if self._tls is not None:
            import ssl
            certfile, keyfile = self._tls
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.minimum_version = ssl.TLSVersion.TLSv1_2   # explicit; don't inherit a weak default
            ctx.load_cert_chain(certfile, keyfile)
            # do_handshake_on_connect=False: the TLS handshake would otherwise run
            # inside accept() on the single serve_forever thread, so a peer that
            # completes the TCP connect then stalls the ClientHello would freeze the
            # ENTIRE accept loop (no per-connection timeout applies there). Deferring
            # it moves the handshake into the per-connection handler thread, under
            # both _CONN_TIMEOUT and the verify_request connection caps. (#audit-mirror-tls-accept)
            self._httpd.socket = ctx.wrap_socket(
                self._httpd.socket, server_side=True, do_handshake_on_connect=False)
        threading.Thread(target=self._httpd.serve_forever,
                         name="saikai-mirror-http", daemon=True).start()
        self._drain = threading.Thread(target=self._drain_loop,
                                       name="saikai-mirror-drain", daemon=True)
        self._drain.start()
        self._inject_drain = threading.Thread(target=self._inject_loop,
                                              name="saikai-mirror-inject",
                                              daemon=True)
        self._inject_drain.start()
        return self._port

    def stop(self) -> None:
        self._stopped.set()
        self._cancel_idle_timer()
        with self._clients_lock:
            for cq in list(self._clients) + list(self._pane_clients):
                self._offer_sentinel(cq)   # even a full queue must get the sentinel
        if self._httpd is not None:
            try:
                self._httpd.shutdown()
            except Exception:
                pass

    def set_size(self, cols: int, rows: int) -> None:
        with self._mirror_lock:
            if (cols, rows) == (self._cols, self._rows):
                return                        # unchanged — no reflow, no broadcast
            self._cols, self._rows = cols, rows
            self._screen.resize(rows, cols)   # pyte: (lines, columns)
        # Tell every live browser to resize its xterm (fresh clients read the
        # new size from the page's data-cols/rows on connect). (#mirror-resize)
        frame = _Size(json.dumps({"cols": cols, "rows": rows}))
        with self._clients_lock:
            targets = list(self._clients)
        for cq in targets:
            try:
                cq.put_nowait(frame)
            except queue.Full:
                pass

    # ── Pane direct view (#pane-direct) ──────────────────────────────────────
    def pane_feed(self, data: str) -> None:
        """Tee one scrubbed child-PTY chunk to the pane channel. Called from the
        pane's READER thread while it holds the AgentTerminal lock — the seed in
        pane_reset() is computed under that same lock, so 'chunk is in the seed'
        and 'chunk is enqueued after the seed' are mutually exclusive and the
        browser never applies a chunk twice. put_nowait only: never blocks,
        never marshals (safe under the terminal lock)."""
        if not data:
            return
        if not self._pane_clients:
            # Advisory zero-viewer gate (GIL-atomic set read): with the mirror
            # merely ENABLED but no pane browser connected — the steady state —
            # the tee must not tax the reader/drain threads for nothing. A
            # first client's connect fires a reseed, which restores full state,
            # so the dropped bytes are recoverable by design. (#review-pane-flood)
            return
        with self._pane_count_lock:
            if self._pane_inflight >= _PANE_INFLIGHT_CAP:
                # Cap this producer's share of the SHARED ingest queue so a
                # flooding child degrades only the pane channel (healed by the
                # reseed below), never the app view. (#review-pane-flood)
                self._pane_lost = True
                return
            self._pane_inflight += 1
        self._ingest_put(_PaneData(data))

    def pane_reset(self, seed: str) -> None:
        """Enqueue a full-state pane reseed (grid + cursor + terminal modes),
        CARRYING the current meta (see _PaneReset). Every pane client's backlog
        is replaced by it in the drain. Clears the pending-reseed flag — the
        request has been answered."""
        self._pane_reseed_pending = 0.0
        self._ingest_put(_PaneReset(seed, self._pane_meta_json))

    def send_clip(self, text: str) -> None:
        """Relay a child's OSC 52 clipboard write to the browsers (bounded).
        UI thread (the app's marshal target calls it). Best-effort: a clipboard
        payload is transient — a fallen-behind client just misses it."""
        if not text:
            return
        frame = _Clip(json.dumps(
            {"b64": base64.b64encode(text[:262144].encode("utf-8")).decode("ascii")}))
        with self._clients_lock:
            targets = list(self._clients) + list(self._pane_clients)
        for cq in targets:
            try:
                cq.put_nowait(frame)
            except queue.Full:
                pass

    def set_pane_strip(self, regex) -> None:
        # Child-emitted terminal QUERIES (DA/DSR/DECRQM/DECRQSS/…) must not
        # reach the browser xterm — it would auto-answer via onData and, with
        # pane input wired, the child receives every reply twice (a duplicated
        # CPR confuses claude's redraw probe). saikai owns the PTY and answers
        # them itself. Applied on the DRAIN thread so the reader never pays the
        # regex while holding the terminal lock. GIL-atomic single-attr set.
        self._pane_strip = regex

    def set_pane_meta(self, meta: dict) -> None:
        """Publish the followed pane's geometry/liveness ({open, cols, rows,
        title}). Deduped; rides the ingest queue so a size change stays ordered
        against the repaint bytes that follow it."""
        j = json.dumps(meta, sort_keys=True)
        if j == self._pane_meta_json:
            return
        self._pane_meta_json = j
        self._ingest_put(_PaneMeta(j))

    def set_pane_reseed_request(self, fn) -> None:
        # Written from the UI thread (on_mount), read from hub threads. The fn is
        # marshal-shaped (bounces to the UI thread, swallows exceptions).
        self._pane_reseed_request = fn

    def _request_pane_reseed(self) -> None:
        """Ask the app for a fresh pane seed (fresh client / fallen-behind client
        / ingest overflow). Deduped with a deadline, not a bool: a marshal lost
        to app teardown must not wedge the channel forever — after 2s a new
        request may fire. The deadline check runs FIRST so the hot fallen-behind
        path pays no lock inside the dedup window. No-op without pane clients or
        a wired callback."""
        import time as _t
        now = _t.monotonic()
        if now < self._pane_reseed_pending:
            return
        with self._clients_lock:
            if not self._pane_clients:
                return
        fn = self._pane_reseed_request
        if fn is None:
            return
        self._pane_reseed_pending = now + 2.0
        try:
            fn()
        except Exception:
            pass

    def set_repaint_request(self, fn) -> None:
        # Written from the UI thread (on_mount), read from the HTTP server thread
        # (_add_client). A single attribute assignment/read is atomic under the GIL.
        self._repaint_request = fn

    def set_input_handler(self, fn) -> None:
        # Written from the UI thread (on_mount), read from the input-drain thread.
        # A single attribute assignment/read is atomic under the GIL (same
        # rationale as set_repaint_request).
        self._input_handler = fn

    def set_mouse_handler(self, fn) -> None:
        # Written from the UI thread (on_mount), read from the inject-drain
        # thread. Single attribute assignment/read is atomic under the GIL
        # (same rationale as set_input_handler).
        self._mouse_handler = fn

    def set_key_handler(self, fn) -> None:
        # Same GIL-atomic single-attribute pattern as set_input_handler.
        self._key_handler = fn

    def set_regions(self, regions) -> None:
        """Publish the host's scrollable-region rectangles (cell coords,
        [{x,y,w,h,k}, …]) to every browser. Deduped: layout pushes ride hot
        paths (list rebuilds, status polls), so identical layouts are dropped
        here. Queue-full clients are skipped — regions repeat on the next
        change and a stale layout only mis-places an edge zone. (#mirror-regions)"""
        try:
            j = json.dumps(regions, separators=(",", ":"), sort_keys=True)
        except Exception:
            return
        if j == getattr(self, "_regions_json", None):
            return
        self._regions_json = j
        frame = _Regions(j)
        with self._clients_lock:
            targets = list(self._clients)
        for cq in targets:
            try:
                cq.put_nowait(frame)
            except queue.Full:
                pass

    def set_control_state(self, enabled: bool, target=None) -> bool:
        """Store the advisory control state + focused-pane title and broadcast a
        control frame to every connected browser. The app's UI-thread gate is the
        authority; this copy is what do_POST fast-rejects against. Returns the
        EFFECTIVE state after the LAN opt-in clamp below, so the caller can keep
        its own copy in sync instead of falsely believing control is ON when this
        gate silently forced it OFF."""
        # LAN input is opt-in: a non-loopback bind cannot ENABLE control unless
        # allow_lan_input was set at launch. Disabling is always honored.
        if enabled and not self._host_is_loopback() and not self.allow_lan_input:
            enabled = False
            target = None
        self._control_enabled = bool(enabled)
        self._control_target = target if enabled else None
        frame = _Control(json.dumps(
            {"on": self._control_enabled, "target": self._control_target}))
        self._broadcast_control_frame(frame)
        if self._control_enabled:
            self._arm_idle_timer()
        else:
            self._cancel_idle_timer()
        return self._control_enabled

    def update_control_target(self, target=None) -> None:
        """Refresh ONLY the control banner's 'typing into' target (focus moved
        while control stays ON) — broadcast a fresh control frame iff the target
        changed, WITHOUT re-arming the idle timer or flipping the gate. No-op when
        control is OFF or the target is unchanged, so focus churn neither spams a
        broadcast nor keeps the idle auto-disable from ever firing."""
        if not self._control_enabled or target == self._control_target:
            return
        self._control_target = target
        frame = _Control(json.dumps({"on": True, "target": self._control_target}))
        self._broadcast_control_frame(frame)

    def _broadcast_control_frame(self, frame) -> None:
        """Deliver a control frame to EVERY viewer — app view and pane view. A
        fallen-behind client can't drop-oldest (it could evict an unconsumed
        control frame and leave the banner stale, #audit-mirror-control-loss):
        an app client resyncs with a full repaint + the frame; a pane client is
        flushed, gets the frame, and a pane reseed is requested."""
        with self._clients_lock:
            app_targets = list(self._clients)
            pane_targets = list(self._pane_clients)
        snap = None
        for cq in app_targets:
            try:
                cq.put_nowait(frame)
            except queue.Full:
                if snap is None:
                    snap = self._snapshot()
                self._resync_client(cq, snap, frame)
        need_reseed = False
        for cq in pane_targets:
            try:
                cq.put_nowait(frame)
            except queue.Full:
                self._flush_pane_backlog(cq)   # preserves an older meta/sentinel
                need_reseed = True
                try:
                    cq.put_nowait(frame)
                except queue.Full:
                    pass
        if need_reseed:
            self._request_pane_reseed()

    def _arm_idle_timer(self) -> None:
        with self._idle_lock:
            if self._idle_timer is not None:
                self._idle_timer.cancel()
                self._idle_timer = None
            if self._idle_secs <= 0:        # 0/negative = no idle auto-disable
                return
            self._idle_timer = threading.Timer(self._idle_secs,
                                               self._on_idle_timeout)
            self._idle_timer.daemon = True
            self._idle_timer.start()

    def _cancel_idle_timer(self) -> None:
        with self._idle_lock:
            if self._idle_timer is not None:
                self._idle_timer.cancel()
                self._idle_timer = None

    def _on_idle_timeout(self) -> None:
        # No accepted input within the window: disable control + tell the browser.
        if self._control_enabled:
            self.set_control_state(False, None)
            # Hub-initiated change: nothing called set_control_state on the app's
            # behalf, so its return value can't sync the app's authoritative copy —
            # without this push the TUI keeps showing control ON while every POST
            # 409s, and the next Shift+F12 toggles from the stale True (one dead
            # press). Timer thread; the handler marshals to the UI thread.
            fn = self._control_change_handler
            if fn is not None:
                try:
                    fn(False)
                except Exception:
                    pass

    def inject(self, data: str) -> bool:
        """Accept browser keyboard input IFF control is on AND a handler is wired.

        Phase B keyboard path: enqueues the bare `str` so the single FIFO drain
        delivers it to the input handler in submission order. Non-blocking."""
        if self._input_handler is None or not self._control_enabled:
            return False
        return self._enqueue(data)

    def inject_mouse(self, col: int, row: int, button: int, kind: str) -> bool:
        """Accept a browser tap/scroll IFF control is on AND a mouse handler is
        wired. Enqueues a tagged ("mouse", col, row, button, kind) tuple onto the
        SAME FIFO so order is preserved across keyboard/mouse/key. Non-blocking."""
        if self._mouse_handler is None or not self._control_enabled:
            return False
        return self._enqueue(("mouse", col, row, button, kind))

    def inject_key(self, key: str) -> bool:
        """Accept a browser key-bar press IFF control is on AND a key handler is
        wired. Enqueues a tagged ("key", key) tuple onto the SAME FIFO.
        Non-blocking."""
        if self._key_handler is None or not self._control_enabled:
            return False
        return self._enqueue(("key", key))

    def inject_raw(self, data: str) -> bool:
        """Accept pane-view terminal bytes (xterm onData: keys, mouse reports,
        paste) IFF control is on AND a raw handler is wired. The app writes them
        VERBATIM to the followed pane's child PTY — the browser xterm acts as
        that child's real terminal, so no key translation happens on this path.
        Same FIFO, same rate cap, same idle re-arm as the other kinds. (#pane-direct)"""
        if self._raw_handler is None or not self._control_enabled:
            return False
        return self._enqueue(("raw", data))

    def set_raw_handler(self, fn) -> None:
        # Same GIL-atomic single-attribute pattern as set_input_handler.
        self._raw_handler = fn

    def _enqueue(self, item) -> bool:
        """Shared tail of inject/inject_mouse/inject_key: accepted-input rate cap,
        bounded put, idle-timer rearm. `item` is a bare str (keyboard) or a tagged
        tuple. Never blocks a handler thread."""
        import time as _t
        now = _t.monotonic()
        if self._min_accept_gap and (now - self._last_accept_t) < self._min_accept_gap:
            return False                       # accepted-input rate cap
        try:
            self._inject_q.put_nowait(item)
        except queue.Full:
            return False           # bounded; refuse rather than block a handler
        self._last_accept_t = now
        self._arm_idle_timer()                 # activity keeps control alive
        return True

    def _inject_loop(self):
        """Single drain worker: pop FIFO and dispatch by tag to the matching
        (advisory) handler. A bare str is the Phase B keyboard payload. The
        handlers are _marshal-shaped (capture the app, bail if gone, swallow
        exceptions), so this thread never blocks on future.result()."""
        while not self._stopped.is_set():
            try:
                item = self._inject_q.get(timeout=0.25)
            except queue.Empty:
                continue
            # Re-check the advisory gate at DISPATCH time, not just at enqueue: an
            # item queued the instant before idle auto-off (or a Shift+F12 toggle)
            # must not still be delivered after control went OFF. Fails closed —
            # the hub flag is set to False synchronously, before the app copy. (#audit-mirror-idle-race)
            if not self._control_enabled:
                continue
            try:
                if isinstance(item, tuple) and item:
                    tag = item[0]
                    if tag == "mouse":
                        fn = self._mouse_handler
                        if fn is not None:
                            _, col, row, button, kind = item
                            fn(col, row, button, kind)
                    elif tag == "key":
                        fn = self._key_handler
                        if fn is not None:
                            fn(item[1])
                    elif tag == "raw":                 # pane-view PTY bytes (#pane-direct)
                        fn = self._raw_handler
                        if fn is not None:
                            fn(item[1])
                    elif tag == "input":
                        fn = self._input_handler
                        if fn is not None:
                            fn(item[1])
                else:                                  # bare str = Phase B keyboard
                    fn = self._input_handler
                    if fn is not None:
                        fn(item)
            except Exception:
                pass               # never let one bad inject kill the drain

    def _host_is_loopback(self) -> bool:
        return self._host in ("127.0.0.1", "localhost", "::1", "")

    def url(self) -> str:
        # 0.0.0.0/"" is a bind wildcard, not browsable — resolve a reachable host
        # (the primary LAN/egress IP) so the URL works from another device.
        host = _lan_ip() if self._host in ("0.0.0.0", "::", "") else self._host
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"   # bracket an IPv6 literal so the URL authority parses
        return f"{self._scheme}://{host}:{self._port}/?token={self._token}"


def mirror_config(env: dict) -> tuple[bool, str]:
    """(enabled, host) from the environment. OFF unless SAIKAI_MIRROR is truthy.

    A wildcard bind (0.0.0.0 / ::) exposes the mirror on EVERY interface the host
    has — including a corporate VPN, a Docker/WSL bridge, or a Tailscale tunnel —
    which is far wider than 'my LAN'. Refuse it unless the operator explicitly
    opts in with SAIKAI_MIRROR_ALLOW_ALL_INTERFACES=1; otherwise fall back to the
    single detected LAN IP so the listen surface matches the QR the user shares.
    (#audit-mirror-wildcard-bind)"""
    val = str(env.get("SAIKAI_MIRROR", "")).strip().lower()
    enabled = val in ("1", "true", "yes", "on")
    requested = str(env.get("SAIKAI_MIRROR_HOST", "")).strip()
    host = requested or "127.0.0.1"
    if host in ("0.0.0.0", "::", ""):
        allow_all = str(env.get("SAIKAI_MIRROR_ALLOW_ALL_INTERFACES", "")
                        ).strip().lower() in ("1", "true", "yes", "on")
        if not allow_all:
            lan = _lan_ip()
            host = lan if lan != "127.0.0.1" else "127.0.0.1"
            # Don't silently rebind what the user explicitly typed — surface the
            # substitution (and the loopback-only fallback when offline) so a
            # "why can't my phone connect" isn't a silent mystery. (#audit-mirror-wildcard-bind)
            if enabled and requested in ("0.0.0.0", "::"):
                _msg = (f"  ⚠ SAIKAI_MIRROR_HOST={requested} (wildcard) → binding {host}"
                        + ("" if host != "127.0.0.1"
                           else " (no LAN IP detected → loopback only)")
                        + "; set SAIKAI_MIRROR_ALLOW_ALL_INTERFACES=1 to bind all interfaces")
                print(_msg, file=sys.stderr)
    return enabled, host


def mirror_port(env: dict) -> int:
    """Fixed mirror port from SAIKAI_MIRROR_PORT so a firewall rule can target a
    stable port; 0 (default) lets the OS pick a free ephemeral port."""
    try:
        p = int(str(env.get("SAIKAI_MIRROR_PORT", "")).strip())
    except ValueError:
        return 0
    return p if 0 < p < 65536 else 0


def mirror_tls_enabled(env: dict) -> bool:
    """Whether to encrypt the LAN transport with TLS — ON BY DEFAULT (opt-out).
    Encrypting the transport is what stops a passive sniffer from harvesting the
    token, the SSE-delivered write-key, and every keystroke — the one gap the
    app-layer controls can't close — and https also gives the browser a secure
    context (navigator.clipboard, etc.). Set SAIKAI_MIRROR_TLS to a falsy token
    (0/false/no/off) to force plain HTTP; unset / empty / truthy → TLS. When no
    cert can be minted (openssl absent and no user cert), the caller warns and
    falls back to HTTP rather than failing launch."""
    return str(env.get("SAIKAI_MIRROR_TLS", "")).strip().lower() not in (
        "0", "false", "no", "off")


def _openssl_run(argv: list, timeout: float) -> int:
    """Run openssl quietly (no console flash on Windows); return its exit code, or
    a nonzero sentinel on any failure."""
    extra = {"creationflags": 0x08000000} if sys.platform == "win32" else {}
    try:
        import subprocess
        return subprocess.run(argv, stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, timeout=timeout, **extra).returncode
    except Exception:
        return 1


def _cert_covers(certf: str, ips: set) -> bool:
    """True if the cert's SANs already cover every address in `ips` — so a cached
    cert is only reused while it still matches the current URL host. A network
    change (new DHCP lease / different Wi-Fi) otherwise reuses a cert whose SAN no
    longer matches, and the browser hard-fails past even the self-signed warning."""
    try:
        import ssl
        txt = ssl._ssl._test_decode_cert(certf)   # dict incl. subjectAltName
    except Exception:
        return False
    san_ips = {v for k, v in txt.get("subjectAltName", ()) if k == "IP Address"}
    return ips.issubset(san_ips)


def _cert_valid_for(certf: str, min_secs: float) -> bool:
    """True if certf is still valid for at least min_secs — WITHOUT openssl (uses
    only the stdlib ssl module), so a cached cert can be re-validated on a host
    (Windows) that has no openssl binary. (#review-tls-windows)"""
    try:
        import ssl
        import time as _t
        txt = ssl._ssl._test_decode_cert(certf)   # dict incl. notAfter
        na = txt.get("notAfter")
        if not na:
            return False
        return ssl.cert_time_to_seconds(na) - _t.time() > min_secs
    except Exception:
        return False


def _write_cert_key(certf: str, keyf: str, cert_pem: bytes, key_pem: bytes) -> bool:
    """Write the key (owner-only, before the world-readable cert) then the cert.
    The umask narrows the private key's mode for the window before the chmod — a
    TOCTOU a local user / backup job could otherwise exploit. POSIX-only; umask
    is effectively a no-op on Windows but harmless."""
    import os as _os
    old_umask = None
    try:
        old_umask = _os.umask(0o077)
    except (AttributeError, OSError):
        old_umask = None
    try:
        with open(keyf, "wb") as f:
            f.write(key_pem)
        with open(certf, "wb") as f:
            f.write(cert_pem)
    except OSError:
        return False
    finally:
        if old_umask is not None:
            try:
                _os.umask(old_umask)
            except OSError:
                pass
    try:
        _os.chmod(keyf, 0o600)
    except OSError:
        pass
    return True


def _gen_self_signed_py(certf: str, keyf: str, ips: set) -> bool:
    """Mint a P-256 self-signed cert/key IN-PROCESS via `cryptography` — no
    openssl binary, so TLS-by-default works on Windows (where openssl usually
    isn't on PATH) with nothing extra to install. Returns True on success, False
    if cryptography is unavailable or minting fails (caller then tries openssl).
    (#review-tls-windows)"""
    global _tls_reason
    try:
        import datetime
        import ipaddress as _ip
        from cryptography import x509
        from cryptography.x509.oid import NameOID
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec
    except Exception as e:
        _tls_reason = f"cryptography unavailable ({e.__class__.__name__}: {e})"
        return False
    try:
        key = ec.generate_private_key(ec.SECP256R1())
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "saikai-mirror")])
        sans = [x509.DNSName("localhost")]
        for ip in sorted(ips):
            try:
                sans.append(x509.IPAddress(_ip.ip_address(ip)))
            except ValueError:
                pass
        now = datetime.datetime.now(datetime.timezone.utc)
        cert = (x509.CertificateBuilder()
                .subject_name(name).issuer_name(name)
                .public_key(key.public_key())
                .serial_number(x509.random_serial_number())
                .not_valid_before(now - datetime.timedelta(minutes=5))
                .not_valid_after(now + datetime.timedelta(days=365))
                .add_extension(x509.SubjectAlternativeName(sans), critical=False)
                .sign(key, hashes.SHA256()))
        key_pem = key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption())
        cert_pem = cert.public_bytes(serialization.Encoding.PEM)
    except Exception as e:
        _tls_reason = f"cryptography mint failed ({e.__class__.__name__}: {e})"
        return False
    if not _write_cert_key(certf, keyf, cert_pem, key_pem):
        _tls_reason = f"cannot write cert/key under {certf!r} (permissions/disk?)"
        return False
    _tls_reason = "self-signed via cryptography"
    return True


def _gen_self_signed_openssl(certf: str, keyf: str, ips: set) -> bool:
    """Mint the cert/key via the openssl CLI — the fallback when `cryptography`
    isn't importable. An EC P-256 key keeps keygen near-instant on a Pi."""
    global _tls_reason
    import shutil
    if not shutil.which("openssl"):
        _tls_reason += "; openssl not on PATH"
        return False
    import os as _os
    san = "subjectAltName=" + ",".join(
        ["DNS:localhost"] + [f"IP:{ip}" for ip in sorted(ips)])
    old_umask = None
    try:
        old_umask = _os.umask(0o077)
    except (AttributeError, OSError):
        old_umask = None
    try:
        rc = _openssl_run(
            ["openssl", "req", "-x509", "-newkey", "ec",
             "-pkeyopt", "ec_paramgen_curve:P-256", "-sha256", "-days", "365",
             "-nodes", "-keyout", keyf, "-out", certf,
             "-subj", "/CN=saikai-mirror", "-addext", san], 30)
    finally:
        if old_umask is not None:
            try:
                _os.umask(old_umask)
            except OSError:
                pass
    if rc != 0:
        _tls_reason += f"; openssl req failed (rc={rc})"
        return False
    try:
        _os.chmod(keyf, 0o600)
    except OSError:
        pass
    _tls_reason += "; self-signed via openssl CLI"
    return True


def _gen_self_signed(cache_dir, host: str = "") -> "tuple[str, str] | None":
    """A cached self-signed cert/key under cache_dir. Minted IN-PROCESS via
    `cryptography` (works on every platform incl. Windows with no openssl binary),
    falling back to the openssl CLI, then to None (caller warns + stays HTTP).
    Reused across runs while still valid AND still covering the current host, so
    the browser isn't re-asked to trust every launch but a network change forces
    a fresh cert. (#review-tls-windows)"""
    global _tls_reason
    from pathlib import Path
    try:
        d = Path(cache_dir)
        d.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        _tls_reason = f"cannot create cache dir {cache_dir!r} ({e})"
        return None
    certf, keyf = str(d / "mirror-cert.pem"), str(d / "mirror-key.pem")
    ips = {"127.0.0.1"}
    lan = _lan_ip()
    if lan and lan != "127.0.0.1":
        ips.add(lan)
    if host and host not in ("0.0.0.0", "", "::", "127.0.0.1", "localhost"):
        ips.add(host)
    if (Path(certf).is_file() and Path(keyf).is_file()
            and _cert_valid_for(certf, 3600) and _cert_covers(certf, ips)):
        _tls_reason = "reused cached self-signed cert"
        return (certf, keyf)      # reuse: still valid for an hour AND covers host
    _tls_reason = ""              # minters append their outcomes below
    if _gen_self_signed_py(certf, keyf, ips) or \
            _gen_self_signed_openssl(certf, keyf, ips):
        return (certf, keyf)
    return None


# Why the last resolve_tls_paths call fell back to HTTP (or how it succeeded).
# The mint helpers swallow their exceptions by design (TLS is best-effort and
# must never break launch), which made an http-only mirror on some host an
# undiagnosable mystery — the caller now surfaces this string in the startup
# warning and the log. (#review-tls-reason)
_tls_reason = ""


def tls_reason() -> str:
    """Human-readable outcome of the LAST resolve_tls_paths call."""
    return _tls_reason


def resolve_tls_paths(env: dict, cache_dir, host: str = "") -> "tuple[str, str] | None":
    """(certfile, keyfile) for the mirror's TLS, or None to fall back to plain HTTP.

    Precedence: an explicit SAIKAI_MIRROR_TLS_CERT + _KEY pair (both must exist —
    a named-but-missing pair returns None rather than silently self-signing) → an
    in-process self-signed cert cached under cache_dir (cryptography, else the
    openssl CLI) → None. Only meaningful when mirror_tls_enabled(env). The
    outcome (incl. WHY a fallback happened) is readable via tls_reason()."""
    global _tls_reason
    import os as _os
    cert = str(env.get("SAIKAI_MIRROR_TLS_CERT", "")).strip()
    key = str(env.get("SAIKAI_MIRROR_TLS_KEY", "")).strip()
    if cert or key:
        if cert and key and _os.path.isfile(cert) and _os.path.isfile(key):
            _tls_reason = "user-provided cert/key"
            return (cert, key)
        _tls_reason = ("SAIKAI_MIRROR_TLS_CERT/_KEY set but "
                       + ("one is missing on disk" if (cert and key)
                          else "only one of the pair is set"))
        return None
    return _gen_self_signed(cache_dir, host)


def mirror_idle_secs(env: dict) -> float:
    """Browser-control auto-disable window in seconds, from SAIKAI_MIRROR_IDLE_SECS.
    Default 600 (10 min). **<= 0 disables the idle auto-disable entirely** — control
    then stays on until you toggle it off locally with Shift+F12."""
    raw = str(env.get("SAIKAI_MIRROR_IDLE_SECS", "")).strip()
    if not raw:
        return 600.0
    try:
        return float(raw)
    except ValueError:
        return 600.0


def _is_private_ipv4(ip: str) -> bool:
    """True for an RFC1918 LAN address (10/8, 172.16/12, 192.168/16) — the ranges a
    phone on the same Wi-Fi actually reaches. Excludes CGNAT/VPN (100.64/10) etc."""
    parts = ip.split(".")
    if len(parts) != 4 or not all(p.isdigit() for p in parts):
        return False
    a, b = int(parts[0]), int(parts[1])
    return a == 10 or (a == 172 and 16 <= b <= 31) or (a == 192 and b == 168)


_LOCAL_IPV4S_CACHE: "set | None" = None


def _hostname_ipv4s(timeout: float = 1.0) -> set:
    """IPv4s the machine's hostname resolves to, TIME-BOUNDED. getaddrinfo takes no
    timeout, and on macOS a runner's `.local` hostname can make it hang for a long
    time (mDNS/DNS) — run it in a daemon thread and abandon it past `timeout` so it
    can NEVER block the mirror's per-request host check (the macOS-CI hang:
    _allowed_hosts → _local_ipv4s → getaddrinfo on every request → recv timeout)."""
    import socket
    import threading
    out: set = set()

    def _work():
        try:
            for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
                out.add(info[4][0])
        except Exception:
            pass

    th = threading.Thread(target=_work, daemon=True)
    th.start()
    th.join(timeout)      # a slow resolver just yields {} — the UDP-trick IP remains
    return set(out)


def _local_ipv4s() -> set:
    """Every local IPv4 we can discover without extra deps: the default-route
    egress IP (UDP-connect trick, no packet sent) plus all addresses the hostname
    resolves to. Used to build the Host allow-list so a phone reaching us by ANY
    local IP isn't 403'd, and to pick the URL/QR host. MEMOISED (process-lifetime):
    _allowed_hosts calls this on every request and the hostname lookup can be slow;
    the addresses don't change within a mirror session. Caching doesn't weaken
    anti-rebinding — the allow-list is IP literals, an attacker's Host is a name."""
    global _LOCAL_IPV4S_CACHE
    if _LOCAL_IPV4S_CACHE is not None:
        return set(_LOCAL_IPV4S_CACHE)
    import socket
    ips: set = set()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))       # no packet sent; picks the egress iface
            ips.add(s.getsockname()[0])
        finally:
            s.close()
    except OSError:
        pass
    ips |= _hostname_ipv4s()                  # time-bounded (macOS .local can hang)
    ips.discard("0.0.0.0")
    _LOCAL_IPV4S_CACHE = set(ips)
    return set(ips)


def _lan_ip() -> str:
    """Best-effort LAN IPv4 for the URL/QR a 0.0.0.0-bound mirror advertises.
    Prefers a private (phone-reachable) address over a VPN/CGNAT egress IP — the
    UDP-connect trick alone picks whatever the default route is, which on a host
    running Tailscale/WireGuard is the tunnel address a LAN phone can't reach.
    Falls back to 127.0.0.1 when offline. No packet is sent."""
    ips = _local_ipv4s()
    priv = sorted(ip for ip in ips if _is_private_ipv4(ip))
    if priv:
        return priv[0]
    other = sorted(ip for ip in ips if not ip.startswith("127."))
    return other[0] if other else "127.0.0.1"


def qr_matrix(url: str, border: int = 2) -> list:
    """QR code of `url` as a list of rows of bool (True = dark module), with a
    light quiet-zone `border`. Used to render a scannable QR in the terminal so a
    phone can join the mirror without typing the tokened URL."""
    import segno
    qr = segno.make(url, error="m")
    return [[bool(v) for v in row] for row in qr.matrix_iter(border=border)]


def _base_driver_class():
    """The console driver Textual would auto-select for this platform."""
    if sys.platform == "win32":
        from textual.drivers.windows_driver import WindowsDriver
        return WindowsDriver
    from textual.drivers.linux_driver import LinuxDriver
    return LinuxDriver


def make_mirror_driver(base_cls, hub: "MirrorHub"):
    """Build a Driver subclass that copies every composited frame to `hub`
    (best-effort, non-blocking) then writes it to the real console unchanged."""
    class MirrorDriver(base_cls):
        def write(self, data: str) -> None:
            try:
                hub.broadcast(data)
            except Exception:
                pass            # mirror is best effort; never degrade local UI
            super().write(data)
    return MirrorDriver


# Browser page. xterm.js + the canvas-renderer addon are vendored and served
# from this origin (no CDN — works on locked-down/offline networks). The canvas
# renderer draws box-drawing + block glyphs (Textual's borders) as crisp vector
# shapes, unlike the default DOM renderer (which left borders looking thin).
# The page reads ?token= from its own URL and opens the SSE stream with it.
_PAGE_HTML = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>saikai mirror</title>
<link rel="stylesheet" href="/xterm.min.css">
<style>html,body{margin:0;height:100%;background:#000;overflow:hidden}
#t{height:100%;overflow:auto;touch-action:auto}
#kb button{min-height:44px;min-width:44px;padding:8px 14px;margin:0;
font:bold 16px monospace;flex:1 1 auto;border:1px solid #555;border-radius:6px;
background:#333;color:#eee;touch-action:manipulation;-webkit-tap-highlight-color:transparent}
#kb button:active{background:#3a3}
#selbar button{min-width:64px;padding:8px 18px;font:bold 16px monospace;
border:1px solid #4a4;border-radius:6px;background:#2c4a2c;color:#eee;
flex:0 0 auto;touch-action:manipulation;-webkit-tap-highlight-color:transparent}
#selbar button:active{background:#3a3}
#kb{flex-direction:column;align-items:stretch}
#kb .kb-row{display:flex;gap:4px}
#kb .sm{min-height:40px;font-size:14px;padding:6px 8px}
#kb .mid{min-height:46px;flex:0 1 150px;min-width:104px}
#kb .esc{border-color:#a66;color:#faa;min-width:96px;flex:0 0 auto;align-self:flex-end}
#kb .enter{border-color:#4a4;color:#9f9;min-width:96px;font-size:17px;flex:0 0 auto}
#kb .next{border-color:#2aa;color:#7ee}
#kb-row2{justify-content:flex-end}
#kb-arrows{display:grid;grid-template-areas:". up ." "left down right";gap:4px;flex:0 0 auto}
#kb-arrows>[data-k="up"]{grid-area:up}#kb-arrows>[data-k="down"]{grid-area:down}
#kb-arrows>[data-k="left"]{grid-area:left}#kb-arrows>[data-k="right"]{grid-area:right}
#kb-arrows>button{min-width:58px;padding:13px 0;flex:0 0 auto}</style></head>
<body data-cols="__COLS__" data-rows="__ROWS__"><div id="t"></div>
<script src="/xterm.min.js"></script>
<script src="/addon-canvas.js"></script>
<script>
const term = new Terminal({cols: parseInt(document.body.dataset.cols, 10),
                           rows: parseInt(document.body.dataset.rows, 10),
                           scrollback:0, convertEol:false});
term.open(document.getElementById('t'));
try {
  const _CA = (window.CanvasAddon && window.CanvasAddon.CanvasAddon) || window.CanvasAddon;
  term.loadAddon(new _CA());     // crisp box/block borders; falls back to DOM
} catch (e) {}
// Keep the keyboard wired to saikai: focus the terminal on load, and re-focus on
// every tap. Without this the xterm textarea can lose focus (mouse tracking eats
// the tap) and keys (Space, etc.) fall through to the browser instead of saikai.
// Focus policy: a MOUSE keeps the hardware keyboard wired (focus on load +
// every click). TOUCH must NOT focus the xterm textarea — focusing summons the
// soft keyboard/IME on every tap, covering the key bar (the reported phone
// pain). Phones type via the row-1 keyboard toggle instead. (#mirror-ime)
const coarse = !!(window.matchMedia && matchMedia('(pointer: coarse)').matches);
if (!coarse) { try { term.focus(); } catch (e) {} }
document.getElementById('t').addEventListener('pointerdown', (e) => {
  if (e.pointerType === 'mouse') { try { term.focus(); } catch (err) {} }
});
// ESC built at runtime (never a literal ESC byte in this served string — a lone
// CR/ESC once broke the page; the no-control-byte test guards it).
const ESC = String.fromCharCode(27);
// Pane view: honor the child's OSC 52 clipboard writes IN THE BROWSER — this
// is how claude's own "copy selection" lands on the device you're holding
// (the host tee also mirrors it to the HOST clipboard; both is right). The
// text is stashed too: a clipboard write outside a user gesture can be
// blocked, and the select bar's Copy button (a gesture) re-copies the stash.
// (#pane-native-select)
let lastOsc52 = '';
// Small transient toast for gestures that run OUTSIDE the select bar (the
// long-press selection, its auto-copy confirmation). (#longpress-select)
const flashEl = document.createElement('div');
flashEl.style.cssText = 'position:fixed;left:50%;transform:translateX(-50%);'+
  'bottom:35%;z-index:25;display:none;font:bold 14px monospace;color:#dfd;'+
  'background:#243a24;border:1px solid #4a4;border-radius:8px;padding:6px 14px';
document.body.appendChild(flashEl);
let flashT = null;
function flashHint(msg) {
  flashEl.textContent = msg;
  flashEl.style.display = 'block';
  if (flashT) clearTimeout(flashT);
  flashT = setTimeout(() => { flashEl.style.display = 'none'; flashT = null; }, 1600);
}
function copyNote(msg) {
  // route to the select bar's hint when it's visible, else the floating toast
  const hint = document.getElementById('sel-hint');
  if (hint && selBar.style.display !== 'none') hint.textContent = msg;
  else flashHint(msg);
}
// Pane view, non-tracking child (claude's prompt owns no mouse): xterm does the
// selection natively — auto-copy it on release so a drag "just copies", the
// same feel as the app-view pane. Stashed so the Copy button can re-copy inside
// a user gesture if the async write was blocked. (#pane-native-select)
try {
  term.onSelectionChange(() => {
    if (!paneView) return;
    let s = '';
    try { s = term.getSelection() || ''; } catch (e) {}
    if (!s) return;
    lastOsc52 = s;
    const ok = copyText(s);
    copyNote(ok ? ('copied ' + s.length + ' chars')
                : (s.length + ' chars selected — tap Copy'));
  });
} catch (e) {}
try {
  term.parser.registerOscHandler(52, (data) => {
    try {
      const i = data.indexOf(';');
      const b64 = i >= 0 ? data.slice(i + 1) : data;
      if (!b64 || b64 === '?') return true;      // read query — not a write
      const bin = atob(b64);
      const bytes = new Uint8Array(bin.length);
      for (let k = 0; k < bin.length; k++) bytes[k] = bin.charCodeAt(k);
      const text = new TextDecoder('utf-8').decode(bytes);
      if (text) {
        lastOsc52 = text;
        const ok = copyText(text);
        copyNote(ok ? ('claude copied ' + text.length + ' chars to your clipboard')
                    : ('claude copied ' + text.length + ' chars — tap Copy to take it'));
      }
    } catch (e) {}
    return true;
  });
} catch (e) {}

// Pane direct view (#pane-direct): ?view=pane joins the RAW child-PTY channel —
// this xterm then IS the claude pane's terminal (exact bytes, native mouse
// reporting, real alt-screen), not a re-render of the whole saikai app.
const paneView = new URLSearchParams(location.search).get('view') === 'pane';
// Fit-to-width state (used by the key bar toggle AND fitChrome's fitFont —
// declared up here so both are past its TDZ regardless of load order). (#kb-fit)
let fitOn = true;
try { fitOn = localStorage.getItem('saikai-fit') !== '0'; } catch (e) {}
// Turn on mouse tracking (VT200 button + SGR encoding) by writing the DECSET
// enable into the terminal: xterm's core mouse service then attaches its own
// DOM listeners and reports taps/scrolls as ESC[<b;col;row(M|m) via onData.
// NOT in pane view — there the CHILD owns the terminal modes; forcing tracking
// on would misstate the child's actual state.
if (!paneView) { try { term.write(ESC + '[?1000;1006h'); } catch (e) {} }
const token = new URLSearchParams(location.search).get('token');
// ── &debug=1: a one-line live diagnostic (control/zone/ticks/scrollTop) so a
//    misbehaving remote can be diagnosed from the phone/desktop itself without
//    DevTools. Counters are written by the edge/select machinery. ─────────────
window.__dbg = {zone: null, arm: 0, tick: 0, st1: 0, st2: 0};
if (new URLSearchParams(location.search).get('debug') === '1') {
  const dbgLine = document.createElement('div');
  dbgLine.style.cssText = 'position:fixed;left:0;bottom:0;z-index:30;'+
    'font:11px monospace;background:#001a33;color:#8cf;padding:2px 6px;opacity:.9';
  document.body.appendChild(dbgLine);
  setInterval(() => {
    const el = document.getElementById('t');
    dbgLine.textContent =
      'ctl=' + controlOn + ' sel=' + selectMode + ' zone=' + window.__dbg.zone +
      ' arm=' + window.__dbg.arm + ' tick=' + window.__dbg.tick +
      ' pan=' + window.__dbg.st1 + ' host=' + window.__dbg.st2 +
      ' scrollTop=' + (el ? el.scrollTop : '-');
  }, 300);
}
const es = new EventSource('/stream?token=' + encodeURIComponent(token) +
                           (paneView ? '&view=pane' : ''));

// ── Output: default-event base64 frames -> xterm. In pane view the same
//    channel carries the child's raw bytes, gated until the first full-state
//    seed arrives (frames racing ahead of it would paint on a blank grid). ───
let paneSeeded = false;
// ── Cursor calm-down (app view, #mirror-cursor-flicker): the HOST hid its
//    hardware cursor once at startup — before this client ever connected — so
//    the browser xterm's cursor stays visible and CHASES the paint position
//    through every repaint burst (a scroll = hundreds of KB of absolute moves):
//    the "flickering cursor". Hide it while frames are arriving; re-show at the
//    final (true) position after 150ms of quiet. Host-driven visibility, when
//    it ever appears in the stream, wins. ─────────────────────────────────────
let hostCurHidden = false;
let weHidCursor = false;
let curShowT = null;
function calmCursor(bin) {
  if (bin.indexOf(ESC + '[?25l') >= 0) hostCurHidden = true;
  if (bin.indexOf(ESC + '[?25h') >= 0) hostCurHidden = false;
  if (!weHidCursor) { term.write(ESC + '[?25l'); weHidCursor = true; }
  if (curShowT) clearTimeout(curShowT);
  curShowT = setTimeout(() => {
    curShowT = null; weHidCursor = false;
    if (!hostCurHidden) term.write(ESC + '[?25h');
  }, 150);
}
// ── DEC 2026 for the pane view (#mirror-sync-2026): claude brackets each frame
//    in ?2026h…?2026l, but xterm.js doesn't implement the mode — mid-frame
//    bytes render, so the cursor dances and frames tear. Buffer while inside a
//    sync block and write the COMPLETE frame at once. Markers can split across
//    SSE messages (a short carry reassembles); a safety timeout flushes a block
//    whose close was lost so the view can never wedge. ───────────────────────
let syncBuf = '';
let syncOn = false;
let syncT = null;
function syncFlush() {
  if (syncT) { clearTimeout(syncT); syncT = null; }
  syncOn = false;
  if (syncBuf) { const b = syncBuf; syncBuf = ''; writeBin(b); }
}
function paneSyncWrite(bin) {
  let s = syncBuf + bin;   // carry: an ESC[?2026x split across messages
  syncBuf = '';
  for (;;) {
    if (!syncOn) {
      const h = s.indexOf(ESC + '[?2026h');
      if (h === -1) break;
      writeBin(s.slice(0, h));
      s = s.slice(h);
      syncOn = true;
      if (syncT) clearTimeout(syncT);
      syncT = setTimeout(syncFlush, 200);
    } else {
      const l = s.indexOf(ESC + '[?2026l');
      if (l === -1) break;
      writeBin(s.slice(0, l + 8));
      s = s.slice(l + 8);
      if (syncT) { clearTimeout(syncT); syncT = null; }
      syncOn = false;
    }
  }
  if (syncOn) {
    syncBuf = s;                      // inside a block: hold until close/timeout
    if (syncBuf.length > 2097152) syncFlush();   // bound a runaway block
    return;
  }
  // Outside a block: hold ONLY a suffix that is a genuine prefix of a split
  // marker (rare) — never plain content, so quiet streams still render their
  // last bytes. A short timer flushes even that if nothing follows.
  const m = ESC + '[?2026';
  let keep = 0;
  for (let k = Math.min(7, s.length); k > 0; k--) {
    if (s.slice(s.length - k) === m.slice(0, k)) { keep = k; break; }
  }
  writeBin(keep ? s.slice(0, s.length - keep) : s);
  syncBuf = keep ? s.slice(s.length - keep) : '';
  if (keep) {
    if (syncT) clearTimeout(syncT);
    syncT = setTimeout(syncFlush, 100);
  }
}
function writeBin(bin) {
  if (!bin) return;
  const bytes = new Uint8Array(bin.length);
  for (let i=0;i<bin.length;i++) bytes[i]=bin.charCodeAt(i);
  term.write(bytes);
}
es.onmessage = (e) => {
  if (paneView && !paneSeeded) return;
  const bin = atob(e.data);
  if (paneView) { paneSyncWrite(bin); return; }
  calmCursor(bin);
  writeBin(bin);
};

// ── Phase B: write-key + control state over named SSE events ────────────────
let writeKey = null;
let controlOn = false;
let selectMode = false;         // drag = select text (not scroll) while ON
let banner = document.createElement('div');
banner.style.cssText =
  'position:fixed;top:0;left:0;right:0;font:bold 14px monospace;'+
  'padding:4px;text-align:center;z-index:9;color:#000;background:#555';
banner.textContent = 'CONTROL OFF (read-only)';
document.body.appendChild(banner);

function applyTouchAction() {
  // SELECT mode: capture the drag entirely (touch-action:none) so it selects
  // instead of panning. CONTROL on: claim the single-finger VERTICAL drag for
  // scroll (pan-x keeps horizontal pan, pinch-zoom keeps zoom). READ-ONLY: let
  // the browser pan freely so a viewer can move around the mirrored screen.
  try {
    document.getElementById('t').style.touchAction =
      selectMode ? 'none' : (controlOn ? 'pan-x pinch-zoom' : 'auto');
  } catch (e) {}
}

function setBanner(on, target) {
  controlOn = on;
  applyTouchAction();
  if (on) {
    banner.style.background = '#3a3';
    banner.textContent = 'CONTROL ON — typing into: ' + (target || '(no pane focused)');
  } else {
    banner.style.background = '#555';
    banner.textContent = 'CONTROL OFF (read-only)';
    // Drop any input buffered while the gates raced: a backlog surviving into
    // the next control-ON would replay stale keys (a buffered CR could accept
    // a live confirmation prompt). (#review-raw-gate)
    try { pendingRaw = ''; } catch (e) {}
    try { pending = ''; } catch (e) {}
  }
}

es.addEventListener('writekey', (e) => {
  try { writeKey = JSON.parse(e.data).key; } catch (_) {}
});
es.addEventListener('size', (e) => {          // host terminal resized (#mirror-resize)
  if (paneView) return;   // pane view sizes from pane-meta, never the host grid
  try {
    const s = JSON.parse(e.data);
    if (s.cols > 0 && s.rows > 0) { term.resize(s.cols, s.rows); fitChrome(); }
  } catch (_) {}
});
let hostRegions = [];        // host scrollable areas in CELL coords (#mirror-regions)
es.addEventListener('regions', (e) => {
  try { hostRegions = JSON.parse(e.data) || []; } catch (_) {}
});
es.addEventListener('control', (e) => {
  let s = {}; try { s = JSON.parse(e.data); } catch (_) {}
  setBanner(!!s.on, s.target);
});
// The child's OSC 52 copy, relayed by the host (#app-native-select). Pane view
// decodes OSC 52 straight off its raw stream — ignore the relay there.
es.addEventListener('clip', (e) => {
  if (paneView) return;
  try {
    const bin = atob(JSON.parse(e.data).b64);
    const bytes = new Uint8Array(bin.length);
    for (let i=0;i<bin.length;i++) bytes[i]=bin.charCodeAt(i);
    const text = new TextDecoder('utf-8').decode(bytes);
    if (!text) return;
    lastOsc52 = text;
    const ok = copyText(text);
    copyNote(ok ? ('copied ' + text.length + ' chars to your clipboard')
                : (text.length + ' chars copied — open Select and tap Copy to take it'));
  } catch (_) {}
});

// ── Pane view: full-state seed + geometry/liveness (#pane-direct) ───────────
let paneOpen = false;
const panePh = document.createElement('div');
panePh.style.cssText = 'position:fixed;top:40%;left:0;right:0;z-index:8;'+
  'display:none;text-align:center;font:bold 15px monospace;color:#9aa5ce';
panePh.textContent = 'no live pane - open a split pane at the host, or switch to App view';
document.body.appendChild(panePh);
let paneGen = null;
let seedRetryTimer = null;
// The reseed request is fire-and-forget server-side (a marshal lost to app
// teardown is swallowed), and until the CURRENT generation's seed arrives every
// pane frame is dropped — so an unseeded-but-open pane view would stay blank
// FOREVER. Client-side backstop: retry via reload (draft stashed), bounded so a
// truly dead server can't reload-loop. Re-armed on every generation change
// (retarget / reopen), not just the first connect. (#review-seed-retry #review-pane-gen)
function armSeedRetry() {
  if (!paneView) return;
  if (seedRetryTimer) clearTimeout(seedRetryTimer);
  seedRetryTimer = setTimeout(() => {
    seedRetryTimer = null;
    if (paneSeeded || !paneOpen) return;   // seeded, or placeholder is correct
    let n = 0;
    try { n = parseInt(sessionStorage.getItem('saikai-seed-retry') || '0', 10) || 0; } catch (_) {}
    if (n >= 3) return;
    try { sessionStorage.setItem('saikai-seed-retry', String(n + 1)); } catch (_) {}
    stashDraft();
    location.reload();
  }, 5000);
}
es.addEventListener('pane-reset', (e) => {
  if (!paneView) return;
  try {
    const bin = atob(JSON.parse(e.data).seed);
    const bytes = new Uint8Array(bin.length);
    for (let i=0;i<bin.length;i++) bytes[i]=bin.charCodeAt(i);
    term.reset();                    // deterministic base for the mode replay
    term.write(bytes);
    paneSeeded = true;
    if (seedRetryTimer) { clearTimeout(seedRetryTimer); seedRetryTimer = null; }
    try { sessionStorage.removeItem('saikai-seed-retry'); } catch (_) {}
  } catch (_) {}
});
if (paneView) armSeedRetry();
es.addEventListener('pane-meta', (e) => {
  if (!paneView) return;
  try {
    const m = JSON.parse(e.data);
    paneOpen = !!m.open;
    panePh.style.display = paneOpen ? 'none' : 'block';
    // A new generation means the pane the browser was seeded for is gone
    // (retarget / close+reopen): gate output until THIS generation's seed and
    // re-arm the blank-view backstop, so a lost seed can't leave a stale screen
    // treated as current. (#review-pane-gen)
    if (m.gen !== undefined && m.gen !== paneGen) {
      paneGen = m.gen;
      paneSeeded = false;
      try { sessionStorage.removeItem('saikai-seed-retry'); } catch (_) {}
      armSeedRetry();
    }
    if (paneOpen && m.cols > 0 && m.rows > 0
        && (m.cols !== term.cols || m.rows !== term.rows)) {
      term.resize(m.cols, m.rows);   // follower: the HOST pane owns the PTY size
      fitChrome();
    }
    if (m.title) { try { document.title = m.title + ' - saikai'; } catch (_) {} }
  } catch (_) {}
});

// ── Input: onData -> coalesce (~25ms, flush on control bytes) -> single-flight
//    FIFO POST /input with the write-key header. One POST in flight at a time. ──
let pending = '';
let flushTimer = null;
let sending = false;
let fatal = false;

function isControlByte(d) {
  // Flush immediately on ESC, CR, or any C0 control byte so interactive keys
  // (Ctrl-C, Enter, arrow escape sequences) are never batching-delayed.
  for (let i=0;i<d.length;i++) { if (d.charCodeAt(i) < 32) return true; }
  return false;
}

async function pump() {
  if (sending || fatal || !controlOn || writeKey === null) return;
  if (pending.length === 0) return;
  sending = true;
  const batch = pending; pending = '';
  try {
    const resp = await fetch('/input', {
      method: 'POST',
      headers: {'Content-Type': 'application/json', 'X-Mirror-Write-Key': writeKey},
      body: JSON.stringify({data: batch})
    });
    if (resp.status === 409) { setBanner(false, null); pending=''; }   // control off server-side
    else if (resp.status === 403) { fatal = true; banner.style.background='#a33';
      banner.textContent = 'CONTROL LOST (auth) — reload'; pending=''; }
    else if (resp.status === 400 || resp.status === 413) { /* drop this batch, continue */ }
  } catch (_) { /* transient; drop the batch, keep going */ }
  finally {
    sending = false;
    if (pending.length > 0) pump();     // drain anything queued while in flight
  }
}

// ── Pane view: single-flight raw pump -> POST /raw (#pane-direct). The bytes
//    go VERBATIM to the followed pane's child PTY, so keys, xterm's own mouse
//    reports and bracketed pastes behave exactly as at a local terminal. Same
//    coalescing + gates as pump(); separate latch so app-view /input and
//    pane-view /raw can't starve each other. ─────────────────────────────────
let pendingRaw = '';
let rawFlushTimer = null;
let rawSending = false;
async function pumpRaw() {
  if (rawSending || fatal || !controlOn || writeKey === null) return;
  if (pendingRaw.length === 0) return;
  rawSending = true;
  const batch = pendingRaw; pendingRaw = '';
  try {
    const resp = await fetch('/raw', {
      method: 'POST',
      headers: {'Content-Type': 'application/json', 'X-Mirror-Write-Key': writeKey},
      body: JSON.stringify({data: batch})
    });
    reactStatus(resp.status);   // shared 409 banner-off / 403 fatal reactions
    if (resp.status === 409 || resp.status === 403) { pendingRaw = ''; }
  } catch (_) { /* transient; drop the batch, keep going */ }
  finally {
    rawSending = false;
    if (pendingRaw.length > 0) pumpRaw();
  }
}
function sendRaw(d) {
  // Same admission gate as postKey/pump: input while control is OFF is DROPPED,
  // not buffered — a backlog accumulated read-only (hold-to-repeat fires every
  // 80ms) would burst verbatim into the child the moment control turns on.
  // (#review-raw-gate)
  if (fatal || !controlOn || writeKey === null) return;
  pendingRaw += d;
  if (isControlByte(d)) {
    if (rawFlushTimer) { clearTimeout(rawFlushTimer); rawFlushTimer = null; }
    pumpRaw();
  } else if (!rawFlushTimer) {
    rawFlushTimer = setTimeout(() => { rawFlushTimer = null; pumpRaw(); }, 25);
  }
}

// ── Phase C: single-flight senders for /mouse and /key ──────────────────────
//    Each mirrors pump()'s gate (controlOn/fatal/writeKey) + the write-key
//    header + the 409->banner-off / 403->fatal reactions. One in-flight POST
//    PER ENDPOINT (a separate latch each) so a swipe spamming /mouse never
//    blocks a keystroke on /input.
function reactStatus(status) {
  if (status === 409) { setBanner(false, null); }
  else if (status === 403) { fatal = true; banner.style.background='#a33';
    banner.textContent = 'CONTROL LOST (auth) — reload'; }
}
let keySending = false, keyPending = null;
async function postKey(key) {
  if (fatal || !controlOn || writeKey === null || !key) return;
  if (keySending) { keyPending = key; return; }      // coalesce: last key wins
  keySending = true;
  try {
    const resp = await fetch('/key', {
      method: 'POST',
      headers: {'Content-Type':'application/json','X-Mirror-Write-Key':writeKey},
      body: JSON.stringify({key: key})
    });
    reactStatus(resp.status);
  } catch (_) { /* transient; drop */ }
  finally {
    keySending = false;
    if (keyPending !== null) { const k = keyPending; keyPending = null; postKey(k); }
  }
}
let mouseSending = false;
const mouseQueue = [];          // pending /mouse msgs, drained single-flight in order
async function postMouse(col, row, button, kind) {
  if (fatal || !controlOn || writeKey === null) return;
  const msg = {col: col, row: row, button: button, kind: kind};
  if (mouseSending) {
    // Coalesce ONLY consecutive same-direction scroll ticks; NEVER drop a
    // down/up. The old "last report wins" could collapse a down..up (or up..down)
    // pair to a single report, leaving the host pane frozen / the split divider
    // captured with no matching release. Bounded so a wild burst can't grow forever.
    const last = mouseQueue[mouseQueue.length - 1];
    if ((kind === 'scrollup' || kind === 'scrolldown') && last && last.kind === kind) return;
    if (kind === 'move' && last && last.kind === 'move') {   // drag: newest wins
      mouseQueue[mouseQueue.length - 1] = msg;
      return;
    }
    mouseQueue.push(msg);
    if (mouseQueue.length > 64) mouseQueue.shift();
    return;
  }
  mouseSending = true;
  try {
    const resp = await fetch('/mouse', {
      method: 'POST',
      headers: {'Content-Type':'application/json','X-Mirror-Write-Key':writeKey},
      body: JSON.stringify(msg)
    });
    reactStatus(resp.status);
  } catch (_) { /* transient; drop */ }
  finally {
    mouseSending = false;
    const n = mouseQueue.shift();
    if (n !== undefined) { postMouse(n.col, n.row, n.button, n.kind); }
  }
}

// ── Input split: SGR mouse -> /mouse, everything else -> Phase B /input ─────
// SGR report: ESC [ < b ; col ; row (M=press, m=release). Built from the ESC
// const + a BACKSLASH-FREE body. A backslash inside a new RegExp('...') string
// arg does NOT survive Python -> served-JS -> JS-string-literal -> RegExp: JS
// string parsing drops the backslash (a bracket-escape becomes a class opener,
// a digit-escape becomes the letter d), throwing at load and blanking the page.
// So the body uses char classes with no backslash: [[] matches a literal '[',
// [0-9] a digit. Anchored ^ (each SGR report is its own onData chunk; mode 1000
// reports no motion) so it never misroutes keyboard data containing the prefix.
const sgrMouseRe = new RegExp('^' + ESC + '[[]<([0-9]+);([0-9]+);([0-9]+)([Mm])');
term.onData((d) => {
  if (!controlOn || fatal) return;      // disabled until a control on-frame
  if (paneView) {
    // Everything — keys, xterm-generated mouse reports (the CHILD enabled
    // tracking; its DECSETs rode the raw stream into this term), pastes —
    // goes verbatim to the child's PTY. Mouse reports flow in Select mode
    // too: pane-view selection IS the child's own smart selection (claude
    // highlights, edge-scrolls its transcript, and copies via OSC 52) — not
    // the local block overlay. (#pane-native-select)
    sendRaw(d);
    return;
  }
  const mm = d.match(sgrMouseRe);
  if (mm) {
    if (selectMode) return;             // taps drive selection, not the host, while selecting
    const b = parseInt(mm[1], 10);
    const col = parseInt(mm[2], 10) - 1;     // 1-based xterm -> 0-based cell
    const row = parseInt(mm[3], 10) - 1;
    const press = mm[4] === 'M';
    let kind, button;
    if (b === 64) { kind = 'scrollup'; button = 0; }
    else if (b === 65) { kind = 'scrolldown'; button = 0; }
    else { kind = press ? 'down' : 'up'; button = b & 3; }
    postMouse(col, row, button, kind);
    return;
  }
  pending += d;                         // keyboard: the unchanged Phase B pump
  if (isControlByte(d)) { if (flushTimer) { clearTimeout(flushTimer); flushTimer=null; } pump(); }
  else if (!flushTimer) { flushTimer = setTimeout(() => { flushTimer=null; pump(); }, 25); }
});

// ── Drag to scroll (touch + mouse): a touch-swipe emits NO wheel events, and a
//    mouse drag under mode 1000 reports a press/release but NO motion, so xterm
//    reports nothing for either and #t's overflow:auto only pans the rendered
//    image. Translate a single-finger / held-left-button VERTICAL drag into the
//    same scrollup/scrolldown the wheel uses, at the dragged cell so the list (or
//    the pane) under the pointer scrolls. Two-finger (pinch) + horizontal pans
//    fall through to the browser via touch-action. Control-gated: read-only
//    (where touch-action is 'auto') never drives the host. The mouse path
//    coexists with xterm's SGR press/release (a plain click stays a tap; only a
//    real drag adds scroll the mouse otherwise can't produce). ─────────────────
(function () {
  const el = document.getElementById('t');
  const STEP = 22;                       // px of vertical drag per scroll tick
  let lastY = null, accum = 0, scol = 0, srow = 0;
  function cellAt(x, y) {
    const scr = el.querySelector('.xterm-screen') || el;   // actual cell area
    const r = scr.getBoundingClientRect();
    const c = r.width  ? Math.floor((x - r.left) / (r.width  / term.cols)) : 0;
    const w = r.height ? Math.floor((y - r.top)  / (r.height / term.rows)) : 0;
    return [Math.max(0, Math.min(term.cols - 1, c)),
            Math.max(0, Math.min(term.rows - 1, w))];
  }
  let pressX = 0, pressY = 0;
  let selPane = false;                // app view: this drag is the CHILD's selection
  let tSelApp = null;                 // last forwarded cell of that drag
  function paneRegionAt(c, r) {
    for (const rg of hostRegions) {
      if (rg.k === 'pane' && c >= rg.x && c < rg.x + rg.w
          && r >= rg.y && r < rg.y + rg.h) return rg;
    }
    return null;
  }
  function begin(x, y) {
    lastY = y; accum = 0; pressX = x; pressY = y;
    const cc = cellAt(x, y); scol = cc[0]; srow = cc[1];
    // Pane view: selection is the CHILD's own (claude) — no local overlay.
    // Mouse drags reach it through xterm's reporting; touch is synthesized in
    // the touch handlers below. (#pane-native-select)
    if (selectMode && !paneView) {
      // App view, anchor INSIDE the claude pane: delegate the drag to the
      // child's own selection (down/move/up ride /mouse; the pane forwards
      // them per ?1002) — content-anchored, self-scrolling, and the copy
      // comes back via the 'clip' event. Elsewhere (session list): the local
      // block overlay. (#app-native-select)
      if (controlOn && !fatal && paneRegionAt(scol, srow)) {
        selPane = true; tSelApp = [scol, srow];
        postMouse(scol, srow, 1, 'down');
        return;
      }
      selPane = false;
      selA = {c: scol, r: srow}; selB = selA; drawSel();
    }
  }
  // Touch → the SGR reports a mouse drag would produce, so a child that TRACKS
  // the mouse (a fullscreen TUI) runs its own selection for a finger too. Sent
  // ONLY when tracking is on — claude's normal prompt does NOT track the mouse
  // (the terminal owns selection there), so synthesizing reports would just
  // type garbage into it. Motion deduped per cell. (#pane-native-select)
  let tSel = null;                    // last synthesized cell, null = no drag
  function paneTracks() {
    try { return (term.modes.mouseTrackingMode || 'none') !== 'none'; }
    catch (e) { return false; }
  }
  function paneSelReport(kind, x, y) {
    let cc = tSel || [0, 0];
    if (kind !== 'up') cc = cellAt(x, y);
    if (kind === 'move' && tSel && tSel[0] === cc[0] && tSel[1] === cc[1]) return;
    const b = kind === 'move' ? 32 : 0;
    sendRaw(ESC + '[<' + b + ';' + (cc[0] + 1) + ';' + (cc[1] + 1) +
            (kind === 'up' ? 'm' : 'M'));
    tSel = kind === 'up' ? null : cc;
  }
  // SELECT mode: extend the RECTANGLE to the cell under the pointer (block
  // selection — see the model above). Near the top/bottom edge, auto-scroll the
  // HOST under the fixed rectangle (Chrome-like edge scroll; control only —
  // read-only has no host to scroll). The mirror has no local scrollback, so
  // the copy is always exactly the on-screen cells in the rectangle.
  let edgeT = null;
  function stopEdge() { if (edgeT) { clearInterval(edgeT); edgeT = null; } }
  function anchorRegion() {
    // The host region (cell rect) containing the selection anchor — a drag
    // inside the claude pane must treat the PANE's rows as its edges, not the
    // canvas's. (#mirror-regions)
    const a = selA;
    if (!a) return null;
    for (const rg of hostRegions) {
      if (a.c >= rg.x && a.c < rg.x + rg.w && a.r >= rg.y && a.r < rg.y + rg.h) return rg;
    }
    return null;
  }
  function edgeZone(y) {
    // Zone bounds = the anchor's host region (converted cells→pixels) when
    // published, else the whole canvas — in both cases clamped to #t's padded
    // viewport (on phones the canvas is BIGGER than the display and #t pans
    // it, so raw canvas edges can sit off-screen). (#mirror-edgezone)
    const scr = el.querySelector('.xterm-screen') || el;
    const r = scr.getBoundingClientRect();
    const tr = el.getBoundingClientRect();
    let top = r.top, bottom = r.bottom;
    const rg = anchorRegion();
    if (rg) {
      const ch = r.height / term.rows;
      top = r.top + rg.y * ch;
      bottom = r.top + (rg.y + rg.h) * ch;
    }
    top = Math.max(top, tr.top + (parseFloat(el.style.paddingTop) || 0));
    bottom = Math.min(bottom, tr.bottom - (parseFloat(el.style.paddingBottom) || 0));
    const band = Math.min(36, Math.max(18, (bottom - top) / 5));
    return (y < top + band) ? 'up' : (y > bottom - band) ? 'down' : null;
  }
  function edgeScroll(y) {
    const dir = edgeZone(y);
    window.__dbg.zone = dir;
    if (!dir) { stopEdge(); return; }
    if (edgeT) return;
    window.__dbg.arm++;
    edgeT = setInterval(() => {
      window.__dbg.tick++;
      if (!selectMode || fatal) { stopEdge(); return; }
      const d = edgeZone(lastPY);          // finger may have left the zone
      if (!d) { stopEdge(); return; }
      // Stage 1 (Chrome-like, works read-only too): pan #t locally while the
      // oversized canvas still has hidden pixels in that direction.
      const before = el.scrollTop;
      el.scrollTop = before + (d === 'down' ? 28 : -28);
      if (el.scrollTop !== before) {
        window.__dbg.st1++;
        selectTo(lastPX, lastPY);          // grow the selection under the still finger
        return;
      }
      // Stage 2: the canvas edge is on screen — scroll the HOST (control only).
      if (!controlOn) {
        // read-only CANNOT drive the host (the server would 409 anyway) — say
        // so instead of silently not scrolling. (#mirror-edge-hint)
        const hint = document.getElementById('sel-hint');
        if (hint) hint.textContent =
          'edge reached — scrolling the host needs CONTROL ON (Shift+F12 at the terminal)';
        return;
      }
      window.__dbg.st2++;
      let at = selB || {c: scol, r: srow};
      if (paneView) {
        // The whole canvas IS the pane here — scroll the child directly via a
        // raw wheel report (regions/mouse coords are app-view concepts).
        paneWheel(at.c, at.r, d);
        return;
      }
      const rg = anchorRegion();
      if (rg) {
        at = {c: Math.max(rg.x, Math.min(rg.x + rg.w - 1, at.c)),
              r: Math.max(rg.y, Math.min(rg.y + rg.h - 1, at.r))};
      }
      postMouse(at.c, at.r, 0, d === 'down' ? 'scrolldown' : 'scrollup');
    }, 150);
  }
  let lastPX = 0, lastPY = 0;
  function selectTo(x, y) {
    lastPX = x; lastPY = y;
    const cc = cellAt(x, y);
    selB = {c: cc[0], r: cc[1]};
    drawSel();
    edgeScroll(y);
  }
  function paneWheel(col, row, dir) {
    // Pane view: a drag-scroll becomes the SGR wheel report a local terminal
    // would emit at that cell (64=up 65=down), sent raw to the child — claude's
    // own scroll handling runs, not an emulation. Only when the child actually
    // tracks the mouse; otherwise return false so the caller leaves the drag
    // to the browser/xterm (native pan + local scrollback). (#pane-direct)
    let mode = 'none';
    try { mode = term.modes.mouseTrackingMode || 'none'; } catch (e) {}
    if (mode === 'none') return false;
    sendRaw(ESC + '[<' + (dir === 'down' ? 65 : 64) + ';' + (col + 1) + ';' + (row + 1) + 'M');
    return true;
  }
  function drag(y, x) {                   // returns true once it consumes the move
    if (lastY === null) return false;
    // a real move before the long-press fires = a scroll gesture, not a hold
    if (Math.abs(y - pressY) > 10 || Math.abs(x - pressX) > 10) cancelLongPress();
    if (lpSel) {                          // one-shot long-press selection (#longpress-select)
      const cc = cellAt(x, y);
      if (!tSelApp || tSelApp[0] !== cc[0] || tSelApp[1] !== cc[1]) {
        tSelApp = cc;
        postMouse(cc[0], cc[1], 1, 'move');
      }
      return true;
    }
    if (selectMode) {
      // Pane view: a MOUSE drag already reaches the child through xterm's own
      // reporting (claude runs its selection) — do nothing here. App view:
      // delegated drag → forward motion per cell change; else extend the
      // local block overlay. (#pane-native-select #app-native-select)
      if (paneView) return true;
      if (selPane) {
        const cc = cellAt(x, y);
        if (!tSelApp || tSelApp[0] !== cc[0] || tSelApp[1] !== cc[1]) {
          tSelApp = cc;
          postMouse(cc[0], cc[1], 1, 'move');
        }
        return true;
      }
      selectTo(x, y);
      return true;
    }
    if (!controlOn || fatal) return false;
    accum += y - lastY; lastY = y;
    let moved = false;
    // pointer up (y decreases) -> see items below -> scroll the list DOWN.
    while (accum <= -STEP) {
      accum += STEP;
      if (paneView) { if (!paneWheel(scol, srow, 'down')) return false; }
      else postMouse(scol, srow, 0, 'scrolldown');
      moved = true;
    }
    while (accum >=  STEP) {
      accum -= STEP;
      if (paneView) { if (!paneWheel(scol, srow, 'up')) return false; }
      else postMouse(scol, srow, 0, 'scrollup');
      moved = true;
    }
    return moved;
  }
  function end() {
    if (selPane || lpSel) {           // finish a delegated drag with a release
      const cc = tSelApp || [scol, srow];
      postMouse(cc[0], cc[1], 1, 'up');
      selPane = false; lpSel = false; tSelApp = null;
    }
    lastY = null; cancelLongPress(); stopEdge();
  }

  // ── Context menu (long-press on touch / right-click on mouse): act on the row
  //    under the pointer. Open => tap that cell to SELECT the row, then show an
  //    overlay whose buttons post saikai's existing action keys (resume / copy /
  //    favorite / hide / rename) for that row. A drag (scroll) cancels the press.
  let lpTimer = null, menuEl = null;
  function cancelLongPress() { if (lpTimer) { clearTimeout(lpTimer); lpTimer = null; } }
  function closeMenu() { if (menuEl) { menuEl.remove(); menuEl = null; } }
  // Long-press INSIDE the claude pane = a one-shot selection drag — no Select
  // mode needed: hold ~500ms, drag, release → the pane's own selection runs and
  // auto-copies (the 'clip' relay). Elsewhere (list rows) a long-press keeps
  // opening the row context menu. (#longpress-select)
  let lpSel = false;
  function engageLpSel(c, r) {
    lpSel = true; tSelApp = [c, r];
    postMouse(c, r, 1, 'down');
    flashHint('selecting — drag, release to copy');
  }
  function armLongPress() {
    if (paneView) return;   // row actions are an app-view concept (#pane-direct)
    if (!controlOn || fatal) return;
    cancelLongPress();
    const c = scol, r = srow, px = pressX, py = pressY;
    lpTimer = setTimeout(() => {
      lpTimer = null;
      if (paneRegionAt(c, r)) engageLpSel(c, r);
      else openMenu(px, py, c, r);
    }, 500);
  }
  function openMenu(px, py, col, row) {
    if (!controlOn || fatal) return;
    closeMenu();
    postMouse(col, row, 0, 'down'); postMouse(col, row, 0, 'up');   // select that row
    menuEl = document.createElement('div');
    menuEl.id = 'ctxmenu';
    menuEl.style.cssText = 'position:fixed;z-index:20;background:#222;border:1px solid #666;'+
      'border-radius:8px;padding:6px;display:flex;flex-direction:column;gap:4px;'+
      'box-shadow:0 6px 20px rgba(0,0,0,.6)';
    const items = [['Resume','enter'],['Copy prompt','f9'],['Favorite','f6'],
                   ['Hide / show','f7'],['Rename','shift+f2'],['Close','']];
    items.forEach((it) => {
      const b = document.createElement('button');
      b.textContent = it[0];
      b.style.cssText = 'min-height:44px;font:bold 15px monospace;background:#333;color:#eee;'+
        'border:1px solid #555;border-radius:6px;padding:8px 16px;text-align:left';
      b.addEventListener('click', (e) => {
        e.preventDefault(); e.stopPropagation();
        if (it[1]) postKey(it[1]);
        closeMenu();
      });
      menuEl.appendChild(b);
    });
    document.body.appendChild(menuEl);
    const w = menuEl.offsetWidth, h = menuEl.offsetHeight;
    menuEl.style.left = Math.max(4, Math.min(px, window.innerWidth  - w - 4)) + 'px';
    menuEl.style.top  = Math.max(4, Math.min(py, window.innerHeight - h - 4)) + 'px';
  }
  // Dismiss on any pointerdown OUTSIDE the menu (capture phase, so it beats the
  // terminal's own handlers); a press INSIDE keeps it open so the button's click fires.
  document.addEventListener('pointerdown', (e) => {
    if (menuEl && !menuEl.contains(e.target)) closeMenu();
  }, true);

  el.addEventListener('touchstart', (e) => {
    if (e.touches.length !== 1) { lastY = null; cancelLongPress(); return; }  // pinch -> browser
    begin(e.touches[0].clientX, e.touches[0].clientY);
    if (selectMode && paneView && paneTracks()) {   // finger drives a TRACKING child
      paneSelReport('down', e.touches[0].clientX, e.touches[0].clientY);
      return;
    }
    if (!selectMode) armLongPress();      // hold without moving -> context menu (not while selecting)
  }, {passive: true});
  el.addEventListener('touchmove', (e) => {
    if (e.touches.length !== 1) return;
    const tx = e.touches[0].clientX, ty = e.touches[0].clientY;
    if (selectMode && paneView && (tSel || paneTracks())) {
      paneSelReport('move', tx, ty);
      e.preventDefault();
      return;
    }
    if (selectMode && paneView) return;   // non-tracking child: let xterm select
    if (Math.abs(ty - pressY) > 10 || Math.abs(tx - pressX) > 10) cancelLongPress();
    if (drag(ty, tx)) e.preventDefault();  // we consumed this drag (scroll or select)
  }, {passive: false});
  el.addEventListener('touchend', (e) => {
    if (selectMode && paneView && tSel) paneSelReport('up', 0, 0);
    end(e);
  }, {passive: true});
  // Mouse: a held LEFT-button drag scrolls the surface under the cursor. Listens
  // on #t in the bubble phase (xterm's own listeners run first, so taps still
  // become SGR press/release); mouseup is on window so a release outside #t ends
  // the drag. Right-click opens the same context menu (the desktop gesture).
  el.addEventListener('mousedown', (e) => {
    if (e.button !== 0) return;
    begin(e.clientX, e.clientY);
    if (!selectMode) armLongPress();   // long-CLICK: hold still ~500ms → in-pane
  });                                  // selection / row menu (#longpress-select)
  window.addEventListener('mousemove', (e) => {           // window: overlays cover #t
    if (lastY === null || !(e.buttons & 1)) return;        // only while left held
    if (drag(e.clientY, e.clientX)) e.preventDefault();
  });
  window.addEventListener('mouseup', end);
  el.addEventListener('contextmenu', (e) => {
    if (!controlOn || fatal || selectMode) return;         // no row-menu while selecting
    e.preventDefault();
    const cc = cellAt(e.clientX, e.clientY);
    // Inside the claude pane the ROW menu is meaningless, and a mobile
    // long-press synthesizes contextmenu right when the long-press selection
    // engages — swallow it there. (#longpress-select)
    if (lpSel || paneRegionAt(cc[0], cc[1])) return;
    openMenu(e.clientX, e.clientY, cc[0], cc[1]);
  });
})();

// ── BLOCK selection model (select mode v2). A linear reading-order selection
//    crossed the split divider and picked garbage from BOTH panes; a RECTANGLE
//    wraps at the column bounds the user draws — the "smart, pane-respecting"
//    selection claude's own alt-screen gives. Visuals are OUR overlay (xterm's
//    selection API is linear-only); the copied text is read straight from the
//    xterm buffer per row. Cells are SCREEN cells (alt-screen mirror: no local
//    scrollback), so what you see in the rectangle is what you copy. (#mirror-blocksel)
const selRect = document.createElement('div');
selRect.id = 'selrect';
selRect.style.cssText = 'position:fixed;display:none;pointer-events:none;z-index:8;'+
  'background:rgba(80,140,255,.30);border:1px solid #7ab8ff';
document.body.appendChild(selRect);
let selA = null, selB = null;                  // anchor / current cell {c,r}
function _selGeom() {
  const el = document.getElementById('t');
  const scr = el.querySelector('.xterm-screen') || el;
  const r = scr.getBoundingClientRect();
  return {left: r.left, top: r.top, cw: r.width / term.cols, ch: r.height / term.rows};
}
function drawSel() {
  if (!selA || !selB) { selRect.style.display = 'none'; return; }
  const g = _selGeom();
  const c1 = Math.min(selA.c, selB.c), c2 = Math.max(selA.c, selB.c);
  const r1 = Math.min(selA.r, selB.r), r2 = Math.max(selA.r, selB.r);
  selRect.style.display = 'block';
  selRect.style.left = (g.left + c1 * g.cw) + 'px';
  selRect.style.top = (g.top + r1 * g.ch) + 'px';
  selRect.style.width = ((c2 - c1 + 1) * g.cw) + 'px';
  selRect.style.height = ((r2 - r1 + 1) * g.ch) + 'px';
}
function clearSel() { selA = selB = null; drawSel(); }
function blockText() {
  if (!selA || !selB) return '';
  const c1 = Math.min(selA.c, selB.c), c2 = Math.max(selA.c, selB.c);
  const r1 = Math.min(selA.r, selB.r), r2 = Math.max(selA.r, selB.r);
  const out = [];
  for (let y = r1; y <= r2; y++) {
    let s = '';
    try {
      const line = term.buffer.active.getLine(y);
      s = line ? line.translateToString(true, c1, c2 + 1) : '';
    } catch (e) {}
    out.push(s.trimEnd ? s.trimEnd() : s);
  }
  while (out.length && out[out.length - 1] === '') out.pop();  // drop blank tail rows
  // NL built at runtime: this file forbids backslash escapes in the served JS
  // (Python's triple-quote would turn them into real control bytes).
  return out.join(String.fromCharCode(10));
}

// ── Copy to clipboard, LAN-safe: navigator.clipboard needs a secure context
//    (https / localhost) which a plain-http LAN mirror is NOT, so fall back to a
//    hidden-textarea + execCommand('copy'). Returns a promise-ish boolean. ──────
function copyText(s) {
  if (!s) return false;
  try {
    if (navigator.clipboard && window.isSecureContext) {
      navigator.clipboard.writeText(s); return true;
    }
  } catch (e) {}
  try {
    const ta = document.createElement('textarea');
    ta.value = s; ta.style.cssText = 'position:fixed;top:-1000px;opacity:0';
    document.body.appendChild(ta); ta.focus(); ta.select();
    const ok = document.execCommand('copy'); ta.remove(); return ok;
  } catch (e) { return false; }
}

// ── SELECT mode: a slim bar (Copy / Done) shown only while selecting, so the
//    default chrome stays minimal. Toggling drag semantics lives in selectMode. ─
const selBar = document.createElement('div');
selBar.id = 'selbar';
selBar.style.cssText =
  'position:fixed;left:0;right:0;display:none;gap:6px;justify-content:center;'+
  'padding:6px;background:#243;z-index:10;font:bold 15px monospace;'+
  'border-top:1px solid #4a4';
selBar.innerHTML =
  '<span id="sel-hint" style="align-self:center;color:#9d9;flex:1 1 auto;'+
    'text-align:left;padding-left:6px">Drag to select · then Copy</span>'+
  '<button id="sel-copy" style="min-height:44px">Copy</button>'+
  '<button id="sel-done" style="min-height:44px">Done</button>';
document.body.appendChild(selBar);
function setSelectMode(on) {
  selectMode = on;
  selBar.style.display = on ? 'flex' : 'none';
  applyTouchAction();
  if (!on) { clearSel(); }
  const sb = document.getElementById('kb-select');
  if (sb) sb.style.background = on ? '#3a3' : '';
  if (on) {
    const hint = document.getElementById('sel-hint');
    // Pane view = the CHILD's smart selection (content-anchored, claude
    // edge-scrolls its own transcript, copies on release). App view = the
    // local block overlay. (#pane-native-select)
    if (hint) hint.textContent = paneView
      ? 'drag in the pane: selects & auto-copies on release (CONTROL ON)'
      : 'drag in the claude pane: auto-copies on release (CONTROL ON) — elsewhere: block, tap Copy';
  }
  fitChrome();
}
document.getElementById('sel-copy').addEventListener('click', (e) => {
  e.preventDefault();
  const hint = document.getElementById('sel-hint');
  if (paneView) {
    // Non-tracking child (claude's normal prompt): xterm did the selection —
    // take its text. Tracking child: it copied via OSC 52 on release — take the
    // stash. Either way inside this user gesture (some browsers block the async
    // clipboard write the auto-copy attempted). (#pane-native-select)
    let s = '';
    try { s = term.getSelection() || ''; } catch (e) {}
    if (!s) s = lastOsc52;
    if (!s) { hint.textContent = 'drag to select first'; return; }
    hint.textContent = copyText(s) ? ('copied ' + s.length + ' chars') : 'copy failed';
    return;
  }
  const s = blockText();
  if (!s) {
    // a delegated in-pane drag leaves no local overlay — the child's OSC 52
    // copy is stashed instead; re-copy it inside this user gesture
    if (lastOsc52) {
      hint.textContent = copyText(lastOsc52)
        ? ('copied ' + lastOsc52.length + ' chars') : 'copy failed';
      return;
    }
    hint.textContent = 'nothing selected — drag first';
    return;
  }
  hint.textContent = copyText(s) ? ('copied ' + s.length + ' chars') : 'copy failed';
  if (!coarse) { try { term.focus(); } catch (_) {} }  // give the keyboard back
  //                                     (mouse only:焦点=soft-KB on touch)
});
document.getElementById('sel-done').addEventListener('click', (e) => {
  e.preventDefault(); setSelectMode(false);
});

// ── On-screen key bar: fixed-position buttons -> POST /key. This is the ONLY
//    channel for app-level keys: typed text rides /input -> the focused live
//    pane's PTY, so keys the app itself must see (Enter to resume, arrows to
//    move the list cursor, the release key) cannot come from the keyboard and
//    must be tapped here. Enter resumes + focuses the cursored session (and,
//    when a pane is already focused, is forwarded to claude as submit); "List"
//    sends ctrl+right_square_bracket — the Textual key name for saikai's DEFAULT
//    release key (ctrl+]); if you rebound [keys] release, tap the list area
//    instead — to drop pane focus back to the list.
//    Ctrl is a STICKY modifier: tap Ctrl to arm it; the next key is sent
//    ctrl-combined (e.g. "ctrl+c"), then Ctrl disarms. ─────────────────────────
let ctrlSticky = false;
const kbBar = document.createElement('div');
kbBar.id = 'kb';
kbBar.style.cssText =
  'position:fixed;bottom:0;left:0;right:0;display:flex;flex-wrap:wrap;gap:4px;'+
  'padding:4px;background:#222;z-index:9;font:bold 14px monospace';
// Action keys grouped on the left; the four arrows form a d-pad cross on the
// right (↑ over ←↓→) so list/dropdown navigation reads like a real keypad.
// Ergonomic v3 (user-flow measured): three tiers by thumb reach.
//   Row 1 (stretch zone, smaller): rare keys — Tab / Leader / Ctrl / Select / More.
//   Row 2 (shoulder, thumb side):  the LOOP keys — ☰List (pane→list, the only
//     way back: typed text rides to the pane) and !Next (shift+f3, jump to the
//     next needs-you session — saikai's hero flow, cyan like the TUI accent).
//   Row 3 (prime band): Esc far-left (rare + high-stakes interrupt: MAXIMUM
//     separation from confirm = error prevention), d-pad + tall green Enter in
//     the easy arc — choose→confirm is one thumb move. On touch, list scrolling
//     and row-picking ride swipe/tap directly, so the d-pad mainly serves
//     claude's own menus; arrow buttons get hold-to-repeat below.
kbBar.innerHTML =
  // Composer tray (hidden until ⌨): a VISIBLE textarea so a phone can use the
  // OS paste bubble and compose long/IME text comfortably — the xterm helper
  // textarea is 1px/invisible, so mobile paste was impossible and every tap
  // used to summon a blind keyboard. Send frames the text as a bracketed
  // paste when the host has ?2004h on (mirrored into term.modes), so embedded
  // newlines do not submit line-by-line; Send ⏎ appends a CR. (#mirror-composer)
  '<div class="kb-row" id="kb-comp" style="display:none;gap:4px">'+
    '<textarea id="comp-text" rows="2" enterkeyhint="send" style="flex:1;'+
      'background:#111;color:#eee;border:1px solid #555;border-radius:6px;'+
      'font:14px monospace;padding:6px;resize:vertical"></textarea>'+
    '<div style="display:flex;flex-direction:column;gap:4px">'+
      '<button id="comp-send-cr" style="min-height:40px">Send &#9166;</button>'+
      '<button id="comp-send" class="sm">Send</button>'+
    '</div>'+
  '</div>'+
  '<div class="kb-row" id="kb-row1">'+
    '<button class="sm" data-k="tab">Tab</button>'+
    '<button class="sm" data-k="space">Leader</button>'+
    '<button class="sm" id="kb-ctrl" data-k="">Ctrl</button>'+
    '<button class="sm" id="kb-select" data-k="">&#9986; Select</button>'+
    '<button class="sm" id="kb-kbd" data-k="">&#9000;</button>'+
    '<button class="sm" id="kb-more" data-k="">More</button>'+
  '</div>'+
  '<div class="kb-row" id="kb-row2">'+
    '<button class="mid" data-k="ctrl+right_square_bracket">&#9776; List</button>'+
    '<button class="mid next" data-k="shift+f3">! Next</button>'+
  '</div>'+
  '<div class="kb-row" id="kb-row3">'+
    '<button class="esc" data-k="escape">Esc</button>'+
    '<div style="flex:1"></div>'+
    '<div id="kb-arrows">'+
      '<button data-k="up">&#8593;</button>'+
      '<button data-k="left">&#8592;</button>'+
      '<button data-k="down">&#8595;</button>'+
      '<button data-k="right">&#8594;</button>'+
    '</div>'+
    '<button class="enter" data-k="enter">&#9166;<br>Enter</button>'+
  '</div>'+
  // Secondary row: saikai's OWN actions, hidden until 'More' so the default bar
  // stays compact. f5/f9/f10/shift+f4 are PRIORITY bindings (fire even with a
  // pane focused); "Find" (slash) opens search and PgUp/PgDn/Top/End page the
  // list (these work when the list, not a pane, is focused).
  '<div class="kb-row" id="kb2" style="display:none;flex-wrap:wrap">'+
    '<button id="kb-view" data-k="">Pane view</button>'+
    '<button id="kb-fit" data-k="">Fit</button>'+
    '<button id="kb-hand" data-k="">&#8644; Right</button>'+
    '<button data-k="slash">Find</button>'+
    '<button data-k="f5">Refresh</button>'+
    '<button data-k="f10">Close pane</button>'+
    '<button data-k="f9">Copy prompt</button>'+
    '<button data-k="shift+f2">Rename</button>'+
    '<button data-k="shift+f4">Restore</button>'+
    '<button data-k="f11">Notifs</button>'+
    '<button data-k="shift+f11">Compact</button>'+
    '<button data-k="checkpoint">Checkpoint</button>'+
    '<button data-k="f12">Mirror QR</button>'+
    '<button data-k="pageup">PgUp</button>'+
    '<button data-k="pagedown">PgDn</button>'+
    '<button data-k="home">Top</button>'+
    '<button data-k="end">End</button>'+
  '</div>';
document.body.appendChild(kbBar);
// Composer-draft persistence across the view-toggle / seed-retry reloads: the
// draft is typed on a phone keyboard — losing it to a navigation is expensive.
// (#review-composer-draft)
function stashDraft() {
  try {
    const ta = document.getElementById('comp-text');
    if (ta && ta.value) sessionStorage.setItem('saikai-draft', ta.value);
  } catch (e) {}
}
try {
  const draft = sessionStorage.getItem('saikai-draft');
  if (draft) {
    sessionStorage.removeItem('saikai-draft');
    const ta = document.getElementById('comp-text');
    if (ta) ta.value = draft;
  }
} catch (e) {}
const kbCtrl = document.getElementById('kb-ctrl');
const kbMore = document.getElementById('kb-more');
function applyFitLabel() {
  const fb = document.getElementById('kb-fit');
  if (!fb) return;
  fb.textContent = fitOn ? 'Fit' : '1:1';
  fb.style.background = fitOn ? '#3a3' : '';
}
applyFitLabel();
if (paneView) {   // the toggle names the view a press switches TO
  const vb = document.getElementById('kb-view');
  if (vb) { vb.textContent = 'App view'; vb.style.background = '#3a3'; }
  // Hide keys that act on the HOST APP — invisible from pane view, so tapping
  // them here would fire blind (dispatchKey drops them too; hiding keeps the
  // bar honest about what works). Terminal keys (PgUp/PgDn/Top/End, d-pad,
  // Esc/Enter/Tab) and the view/hand toggles stay. (#review-invisible-app-keys)
  const appOnly = ['slash', 'f5', 'f10', 'f9', 'shift+f2', 'shift+f4', 'f11',
                   'shift+f11', 'checkpoint', 'f12',
                   'ctrl+right_square_bracket', 'shift+f3'];
  kbBar.querySelectorAll('button[data-k]').forEach((b) => {
    if (appOnly.indexOf(b.getAttribute('data-k')) >= 0) b.style.display = 'none';
  });
  const r2 = document.getElementById('kb-row2');
  if (r2) r2.style.display = 'none';   // List/Next: both app-scoped
}
// ── Handedness: 'R' (default) puts the d-pad cluster under the RIGHT thumb;
//    'L' mirrors every bar (key bar, More row, select bar) for left-thumb
//    reach. Persisted per browser in localStorage. ──────────────────────────
let hand = 'R';
try { hand = localStorage.getItem('saikai-hand') === 'L' ? 'L' : 'R'; } catch (e) {}
function applyHand() {
  // The bar is a COLUMN of rows; mirror each row (and the select bar) so the
  // whole thumb map flips: shoulder keys, Esc↔cluster, More row. The row-3
  // spacer keeps Esc and the d-pad/Enter cluster at opposite edges in both.
  const dir = (hand === 'L') ? 'row-reverse' : 'row';
  kbBar.querySelectorAll('.kb-row').forEach((r) => { r.style.flexDirection = dir; });
  try { selBar.style.flexDirection = dir; } catch (e) {}
  const hb = document.getElementById('kb-hand');
  if (hb) hb.textContent = (hand === 'L') ? '\u21c4 Left' : '\u21c4 Right';
}
applyHand();
// Pane view (#pane-direct): a key-bar press that IS terminal input goes to the
// child's PTY as the raw sequence a local terminal would produce — honoring
// DECCKM (application cursor keys, mirrored into term.modes by the raw
// stream). saikai-level actions (checkpoint, f5, List, …) return null
// here and stay on the /key path, acting on the host app as before.
const CR = String.fromCharCode(13);
function paneRawSeq(k) {
  const app = !!(term.modes && term.modes.applicationCursorKeysMode);
  const arrows = {up: 'A', down: 'B', right: 'C', left: 'D'};
  if (arrows[k]) return ESC + (app ? 'O' : '[') + arrows[k];
  if (k === 'enter') return CR;
  if (k === 'escape') return ESC;
  if (k === 'tab') return String.fromCharCode(9);
  if (k === 'space') return ' ';
  if (k === 'backspace') return String.fromCharCode(127);
  if (k === 'pageup') return ESC + '[5~';
  if (k === 'pagedown') return ESC + '[6~';
  // Home/End are DECCKM cursor keys exactly like the arrows (xterm sends
  // SS3 H/F under application-cursor mode — terminfo khome/kend under smkx).
  if (k === 'home') return ESC + (app ? 'O' : '[') + 'H';
  if (k === 'end') return ESC + (app ? 'O' : '[') + 'F';
  if (k === 'slash') return '/';
  if (k.indexOf('ctrl+') === 0) {
    const c = k.slice(5);
    if (c === 'right_square_bracket') return null;    // List = saikai action
    if (c.length === 1 && c >= 'a' && c <= 'z')
      return String.fromCharCode(c.charCodeAt(0) - 96);
  }
  return null;   // saikai pseudo/function keys -> /key as before
}
function dispatchKey(k) {
  if (paneView) {
    const seq = paneRawSeq(k);
    if (seq !== null) { sendRaw(seq); }
    // A key with no raw encoding is DROPPED in pane view — never forwarded to
    // /key. The fallthrough target would be the INVISIBLE host app: a modal
    // opened blind steals the host's focus and can't even be dismissed from
    // here (Esc goes raw to the child). Safe default for future bar keys too.
    // (#review-invisible-app-keys)
    return;
  }
  postKey(k);
}
// Hold-to-repeat for the d-pad: buttons have no key-repeat, so a long menu
// or an in-pane scroll needed one tap per step. pointerdown fires the key at
// once, holding repeats it (400ms delay, then 80ms — physical-keyboard feel);
// pointerup/cancel/leave stops. These buttons are EXCLUDED from the generic
// click path below (a click after pointerdown would double-fire).
(function () {
  let rptT = null, rptI = null;
  function stopRepeat() {
    if (rptT) { clearTimeout(rptT); rptT = null; }
    if (rptI) { clearInterval(rptI); rptI = null; }
  }
  kbBar.querySelectorAll('#kb-arrows button').forEach((b) => {
    const k = b.getAttribute('data-k');
    b.addEventListener('pointerdown', (e) => {
      e.preventDefault();
      stopRepeat();
      dispatchKey(k);
      rptT = setTimeout(() => { rptI = setInterval(() => dispatchKey(k), 80); }, 400);
    });
    ['pointerup', 'pointercancel', 'pointerleave'].forEach((t) =>
      b.addEventListener(t, stopRepeat));
  });
})();
kbBar.querySelectorAll('button').forEach((b) => {
  if (b.closest && b.closest('#kb-arrows')) return;   // hold-to-repeat path above
  b.addEventListener('click', (e) => {
    e.preventDefault();
    if (b.id === 'kb-ctrl') {                 // arm/disarm the sticky modifier
      ctrlSticky = !ctrlSticky;
      kbCtrl.style.background = ctrlSticky ? '#3a3' : '';
      return;
    }
    if (b.id === 'kb-select') {               // toggle drag-to-select-text mode
      setSelectMode(!selectMode);
      return;
    }
    if (b.id === 'kb-kbd') {                  // toggle the composer tray
      const tray = document.getElementById('kb-comp');
      const show = tray.style.display === 'none';
      tray.style.display = show ? 'flex' : 'none';
      b.style.background = show ? '#3a3' : '';
      fitChrome();
      if (show) { try { document.getElementById('comp-text').focus(); } catch (e) {} }
      return;
    }
    if (b.id === 'comp-send' || b.id === 'comp-send-cr') {
      const ta = document.getElementById('comp-text');
      let v = ta.value;
      if (v !== '') {
        // Strip any bracketed-paste markers the user pasted INTO the draft
        // before framing — an embedded ESC[201~ would early-close the paste,
        // turning the rest (incl. newlines) into live keystrokes that submit
        // to the child. Loop until stable: a single pass can re-form a marker at
        // the deletion seam (e.g. ESC[2 + ESC[200~ + 00~). Mirrors the host's
        // _wrap_bracketed_paste. (#review-paste-marker #review-paste-overlap)
        let _prev;
        do {
          _prev = v;
          v = v.split(ESC + '[200~').join('').split(ESC + '[201~').join('');
        } while (v !== _prev);
        // frame as a bracketed paste when the HOST enabled ?2004h (the mode
        // rides the mirrored byte stream into term.modes) — else raw. Markers
        // built from ESC at runtime (no control bytes in this served string).
        let framed = v;
        try {
          if (term.modes && term.modes.bracketedPasteMode) {
            framed = ESC + '[200~' + v + ESC + '[201~';
          }
        } catch (e) {}
        if (b.id === 'comp-send-cr') framed += String.fromCharCode(13);
        // Pane view: composed text goes straight to the child's PTY (the
        // bracketed framing above already reflects the CHILD's ?2004 state,
        // mirrored into term.modes by the raw stream). (#pane-direct)
        if (paneView) { sendRaw(framed); }
        else { pending += framed; pump(); }
        ta.value = '';
      } else if (b.id === 'comp-send-cr') {
        if (paneView) { sendRaw(String.fromCharCode(13)); }
        else { pending += String.fromCharCode(13); pump(); }   // empty + Send⏎ = bare Enter
      }
      try { ta.focus(); } catch (e) {}        // keep composing (keyboard stays up)
      return;
    }
    if (b.id === 'kb-view') {                 // Pane <-> App view (#pane-direct)
      // The view is a CONNECTION property (the SSE stream carries either app
      // frames or raw pane bytes), so switching reloads with the param — a
      // clean terminal, seed and mode replay instead of an in-place migration.
      // The composer draft is NOT connection-scoped: stash it across the
      // navigation (restored on load). (#review-composer-draft)
      stashDraft();
      const u = new URL(location.href);
      if (paneView) { u.searchParams.delete('view'); }
      else { u.searchParams.set('view', 'pane'); }
      location.href = u.toString();
      return;
    }
    if (b.id === 'kb-fit') {                  // Fit-to-width <-> 1:1 glyphs (#kb-fit)
      fitOn = !fitOn;
      try { localStorage.setItem('saikai-fit', fitOn ? '1' : '0'); } catch (e) {}
      applyFitLabel();
      fitChrome();
      return;
    }
    if (b.id === 'kb-hand') {                 // mirror the bars for the other thumb
      hand = (hand === 'L') ? 'R' : 'L';
      try { localStorage.setItem('saikai-hand', hand); } catch (e) {}
      applyHand();
      return;
    }
    if (b.id === 'kb-more') {                 // reveal/hide the secondary action row
      const k2 = document.getElementById('kb2');
      const show = k2.style.display === 'none';
      k2.style.display = show ? 'flex' : 'none';
      kbMore.style.background = show ? '#3a3' : '';
      fitChrome();                            // bar height changed -> re-reserve padding
      return;
    }
    let k = b.getAttribute('data-k');
    if (ctrlSticky) {                         // next key goes ctrl-combined, then disarm
      if (k.indexOf('ctrl+') !== 0) { k = 'ctrl+' + k; }   // don't double-prefix List (already ctrl+...)
      ctrlSticky = false;
      kbCtrl.style.background = '';
    }
    dispatchKey(k);
  });
});

// Reserve space for the fixed top banner + bottom key bar so neither covers the
// terminal. The key bar wraps to several rows on a narrow phone, so its height
// is measured (not assumed) and re-measured on resize/rotate. #t scrolls
// (overflow:auto), so the reserved padding lets the last rows clear the bar
// instead of hiding under it.
// ── Fit-to-width (#kb-fit): scale the FONT so the whole grid fits the window
//    width — cols/rows never change (the browser is a follower; the HOST owns
//    the terminal size), only the glyph size does. Clamped to a readability
//    floor: below it the canvas overflows and pans exactly as before (phones
//    showing a 140-col host). Toggleable to 1:1 from the More row; persisted
//    per browser. Re-fit runs on every fitChrome trigger — window resize, the
//    host 'size' frame, pane-meta resize, bar open/close — so a HOST terminal
//    resize re-fits too. (fitOn is declared at the top of the script.) ───────
const FIT_BASE = 15, FIT_MIN = 9, FIT_MAX = 22;
function fitFont() {
  const scr = document.querySelector('.xterm-screen');
  if (!scr || !term.element) return;
  const cur = term.options.fontSize || FIT_BASE;
  if (!fitOn) {
    if (cur !== FIT_BASE) { try { term.options.fontSize = FIT_BASE; } catch (e) {} }
    return;
  }
  // Fit BOTH axes: the grid must clear the banner AND the key bar — a
  // width-only fit left the bottom rows hidden behind the bar on short
  // windows. (#review-fit-height)
  const availW = document.documentElement.clientWidth;
  let availH = window.innerHeight - banner.offsetHeight - kbBar.offsetHeight;
  if (selBar.style.display !== 'none') availH -= selBar.offsetHeight;
  const r = scr.getBoundingClientRect();
  if (!availW || availH <= 0 || !r.width || !r.height) return;
  let want = Math.floor(cur * Math.min(availW / r.width, availH / r.height));
  want = Math.max(FIT_MIN, Math.min(FIT_MAX, want));
  if (want !== cur) { try { term.options.fontSize = want; } catch (e) {} }
  // one correction pass: font metrics aren't perfectly linear in fontSize
  const r2 = scr.getBoundingClientRect();
  if ((r2.width > availW || r2.height > availH) && want > FIT_MIN) {
    const f2 = Math.max(FIT_MIN, Math.floor(
      want * Math.min(availW / r2.width, availH / r2.height)));
    try { term.options.fontSize = f2; } catch (e) {}
  }
}
function fitChrome() {
  const tdiv = document.getElementById('t');
  if (!tdiv) return;
  fitFont();   // font first — the paddings/hug below measure the scaled canvas
  // The select bar (when active) docks just under the status banner at the top.
  let top = banner.offsetHeight;
  if (selBar.style.display !== 'none') {
    selBar.style.top = top + 'px';
    top += selBar.offsetHeight;
  }
  tdiv.style.paddingTop = top + 'px';
  tdiv.style.paddingBottom = kbBar.offsetHeight + 'px';
  // Hug the TERMINAL's bottom edge when the whole canvas fits above the bar:
  // on a window taller than the mirrored screen the bar otherwise sits at the
  // viewport bottom with dead space between it and the content — keys far from
  // what they act on. When the canvas is TALLER than the viewport (phones),
  // the condition never holds mid-pan and the bar stays viewport-anchored; at
  // full pan both anchors coincide, so it never jumps. (#kb-hug)
  let hug = '';
  const scr = tdiv.querySelector('.xterm-screen');
  if (scr) {
    const r = scr.getBoundingClientRect();
    if (r.height > 0 && r.bottom + kbBar.offsetHeight <= window.innerHeight) {
      hug = Math.ceil(r.bottom) + 'px';
    }
  }
  if (hug) { kbBar.style.top = hug; kbBar.style.bottom = 'auto'; }
  else { kbBar.style.top = 'auto'; kbBar.style.bottom = '0'; }
}
fitChrome();
window.addEventListener('resize', fitChrome);
</script></body></html>"""


# CSP hash of the page's ONE inline <script>. script-src 'self' alone BLOCKS
# inline scripts — the hardening shipped with exactly that, so the mirror page
# rendered NOTHING in a real browser (string-asserting tests and a Node harness
# both bypass CSP; only a real browser run caught it). The script is fully
# static now (cols/rows ride <body data-*>), so one import-time hash is exact.
# (#audit-csp-inline)
def _inline_script_hash() -> str:
    import hashlib
    import base64 as _b64
    i = _PAGE_HTML.rindex("<script>") + len("<script>")
    j = _PAGE_HTML.index("</script>", i)
    digest = hashlib.sha256(_PAGE_HTML[i:j].encode("utf-8")).digest()
    return "sha256-" + _b64.b64encode(digest).decode("ascii")


_INLINE_SCRIPT_HASH = _inline_script_hash()


class _Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):           # silence default stderr logging
        pass

    # HTTP/1.1 so keep-alive + SSE behave; ALWAYS emit Content-Length or use 204.
    protocol_version = "HTTP/1.1"

    def handle_one_request(self) -> None:
        """Treat a client that vanished mid-request/response as a NORMAL
        disconnect, not an error. The SSE stream already catches these
        internally, but the one-shot GET/POST/send_error paths let
        BrokenPipeError / ConnectionResetError escape to socketserver, which
        printed a full traceback per dropped phone connection. (#audit-codex-disconnect)"""
        try:
            super().handle_one_request()
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
    # StreamRequestHandler.setup() applies this via socket.settimeout(), so a peer
    # that stalls on a header/body read (Slowloris) is dropped instead of parking a
    # blocked thread forever. Long enough for a real slow phone; short enough that a
    # flood can't accumulate. (#audit-mirror-dos)
    timeout = _CONN_TIMEOUT

    def end_headers(self) -> None:
        """Inject the defense-in-depth headers into EVERY response — including the
        stdlib send_error() rejections (403/404/413/503/…), which are exactly the
        responses an attacker's probing sees most — by hooking the one method all
        response paths funnel through, instead of a per-call-site header helper."""
        self._security_headers()
        super().end_headers()

    def _security_headers(self) -> None:
        """Block MIME sniffing, never leak the tokened URL via Referer, and lock the
        page's origins down with a CSP so a future template/asset bug can't
        exfiltrate or load off-origin. Idempotent enough: end_headers runs once per
        response, so each header is emitted exactly once."""
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; "
                         f"script-src 'self' '{_INLINE_SCRIPT_HASH}'; "
                         "connect-src 'self'; "
                         "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
                         "base-uri 'none'; form-action 'none'; frame-ancestors 'none'")

    def _token_ok(self) -> bool:
        # Per-source lockout on read-token guessing (its own budget, keyed to the
        # normalised peer) — the token gates the SSE stream that hands out the
        # write-key, so hammering it must be bounded like the write-key is. (#audit-mirror-ratecap)
        hub = self.server.hub
        src = self.client_address[0] if self.client_address else "?"
        if hub._token_locked_out(src):
            return False
        from urllib.parse import urlparse, parse_qs
        q = parse_qs(urlparse(self.path).query)
        got = (q.get("token") or [""])[0]
        ok = hmac.compare_digest(got, hub._token)
        if ok:
            hub._mark_proven(src)     # authenticated → exempt from lockouts (grace)
        else:
            hub._note_bad_token(src)
        return ok

    def _write_key_ok(self) -> bool:
        hub = self.server.hub
        src = self.client_address[0] if self.client_address else "?"
        # Enforce the per-source bad-key cooldown (was a hub-wide write-only
        # counter): a lost/guessed write-key can't be hammered once the threshold
        # trips, and one abusive peer can't lock out the real operator's own
        # correct key. (#audit-mirror-ratecap)
        if hub._input_locked_out(src):
            return False
        got = self.headers.get("X-Mirror-Write-Key", "")
        ok = hmac.compare_digest(got, hub._write_key)
        if ok:
            hub._clear_bad_key(src)                   # legit key → clear the streak
            hub._mark_proven(src)                     # + exempt from lockouts (grace)
        else:
            hub._note_bad_key(src)
        return ok

    def _allowed_hosts(self) -> set:
        """The exact Host header values we accept: loopback names + the LAN IP
        the mirror is reachable at, each with the actual served port. Anything
        else is a rebinding attempt and is refused on EVERY route."""
        port = self.server.hub._port
        hub_host = self.server.hub._host
        names = {"127.0.0.1", "localhost", "[::1]", "::1"}
        if hub_host in ("0.0.0.0", "::", ""):
            # Wildcard bind: allow EVERY local IPv4, not just the one url() picked —
            # a phone reaching us by a different local address (multi-NIC, VPN +
            # LAN) would otherwise 403 even though it connected fine. Host-literal
            # matching still blocks DNS-rebinding (an attacker's Host is a hostname,
            # not one of our IP literals).
            names |= _local_ipv4s()
        elif hub_host not in ("127.0.0.1", "localhost"):
            names.add(hub_host)              # the specific bound LAN IP
        allowed = set()
        for n in names:
            allowed.add(n)
            allowed.add(f"{n}:{port}")
        return allowed

    def _host_ok(self) -> bool:
        # Case-fold + strip a resolver's trailing dot so a browser sending
        # 'MyPi.local' / 'mypi.local.' against a configured 'mypi.local' still
        # matches. IP literals are unaffected by the fold.
        host = self.headers.get("Host", "").strip().rstrip(".").lower()
        return host in {h.lower() for h in self._allowed_hosts()}

    def _peer_is_loopback(self) -> bool:
        """True when the real TCP peer is the local host (v4, v4-mapped-v6, or v6
        loopback). Used to gate remote input per-connection."""
        ip = (self.client_address[0] if self.client_address else "") or ""
        return ip == "::1" or ip == "::ffff:127.0.0.1" or ip.startswith("127.")

    def _server_origins(self) -> set:
        """The exact Origin/Referer-host values that count as same-origin — under
        the server's actual scheme (https when TLS is on, else http)."""
        scheme = self.server.hub._scheme
        return {f"{scheme}://{h}" for h in self._allowed_hosts()}

    def _origin_ok(self) -> bool:
        """Fail-closed CSRF defense-in-depth: require an Origin (or, absent that,
        a Referer) whose scheme+host+port exactly equal this server's origin.
        Reject absent-both, literal 'null', cross-origin, and port mismatches."""
        from urllib.parse import urlparse
        allowed = self._server_origins()
        origin = self.headers.get("Origin")
        if origin is not None:
            return origin in allowed        # 'null' and mismatches fall through
        ref = self.headers.get("Referer")
        if ref:
            p = urlparse(ref)
            return f"{p.scheme}://{p.netloc}" in allowed
        return False                        # absent both -> reject

    _STATIC = {"/xterm.min.js": "application/javascript",
               "/addon-canvas.js": "application/javascript",
               "/xterm.min.css": "text/css"}

    def do_GET(self):
        if not self._host_ok():
            self.send_error(403, "forbidden")
            return
        path = self.path.split("?", 1)[0]
        if path in self._STATIC:               # public library asset; no token
            self._serve_static(path, self._STATIC[path])
            return
        if path == "/favicon.ico":             # tokenless browser request; not an auth failure
            self.send_error(404)
            return
        if not self._token_ok():
            self.send_error(403, "forbidden")
            return
        if path == "/":
            hub = self.server.hub
            body = (_PAGE_HTML
                    .replace("__COLS__", str(hub._cols))
                    .replace("__ROWS__", str(hub._rows))
                    .encode("utf-8"))
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")   # tokened page: never cache
            self.end_headers()
            self.wfile.write(body)
        elif path == "/stream":
            self._stream()
        else:
            self.send_error(404)

    def _serve_static(self, path, ctype):
        import os
        fpath = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "saikai_mirror_static", path.lstrip("/"))
        try:
            with open(fpath, "rb") as f:
                body = f.read()
        except OSError:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "max-age=86400")
        self.end_headers()
        self.wfile.write(body)

    def _stream(self):
        hub = self.server.hub
        # ?view=pane joins the raw child-PTY channel instead of app frames; the
        # page reloads with the param to switch views, so the choice is a
        # connection property, not migrated in-place. Exact param parse — a
        # substring test would let ?view=panel / ?preview=paneX select the raw
        # channel by accident. (#pane-direct)
        from urllib.parse import urlparse, parse_qs
        pane_view = parse_qs(urlparse(self.path).query).get("view") == ["pane"]
        if pane_view:
            cq, snapshot = hub._add_pane_client(), None
        else:
            cq, snapshot = hub._add_client()
        if cq is None:                       # viewer cap hit — don't hold a thread
            self._reject(503, "too many viewers", drain=False)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        try:
            if snapshot is not None:
                self._send_frame(snapshot)
            # Write-key (only ever over this authenticated channel) + current
            # control state, both as named raw-JSON events.
            self._send_event("writekey", json.dumps({"key": hub._write_key}))
            self._send_event("control", json.dumps(
                {"on": hub._control_enabled, "target": hub._control_target}))
            if pane_view:
                # Current pane geometry/liveness; the full pane STATE arrives as
                # a pane-reset once the app answers the reseed request that
                # _add_pane_client fired.
                self._send_event("pane-meta", hub._pane_meta_json)
            else:
                _rj = getattr(hub, "_regions_json", None)
                if _rj:
                    self._send_event("regions", _rj)   # current layout for a fresh client
            while True:
                try:
                    data = cq.get(timeout=30.0)
                except queue.Empty:
                    # Periodic SSE keepalive comment so idle connections (and any
                    # intermediaries) stay open between live frames.
                    self.wfile.write(b":\n\n")
                    self.wfile.flush()
                    continue
                if data is None:             # stop sentinel
                    break
                if isinstance(data, _Control):   # named control event, not output
                    self._send_event("control", data.json)
                    continue
                if isinstance(data, _Regions):   # host-layout event (#mirror-regions)
                    self._send_event("regions", data.json)
                    continue
                if isinstance(data, _Size):      # host resized (#mirror-resize)
                    self._send_event("size", data.json)
                    continue
                if isinstance(data, _PaneData):  # raw child bytes (#pane-direct)
                    self._send_frame(data.data)
                    continue
                if isinstance(data, _PaneReset):
                    # Geometry FIRST: the browser must resize before the seed
                    # paints, and this re-delivers a meta lost to any flush
                    # (set_pane_meta dedups at source). (#review-pane-meta-loss)
                    self._send_event("pane-meta", data.meta)
                    self._send_event("pane-reset", json.dumps(
                        {"seed": base64.b64encode(
                            data.seed.encode("utf-8")).decode("ascii")}))
                    continue
                if isinstance(data, _PaneMeta):
                    self._send_event("pane-meta", data.json)
                    continue
                if isinstance(data, _Clip):      # child OSC 52 copy (#app-native-select)
                    self._send_event("clip", data.json)
                    continue
                self._send_frame(data)
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            hub._remove_client(cq)

    def _send_frame(self, data: str):
        payload = base64.b64encode(data.encode("utf-8")).decode("ascii")
        self.wfile.write(b"data: " + payload.encode("ascii") + b"\n\n")
        self.wfile.flush()

    def _send_event(self, event: str, raw_json: str):
        """Emit a NAMED SSE event carrying raw JSON (consumed by the browser's
        addEventListener, NOT onmessage — so it never hits the base64 atob path)."""
        self.wfile.write(b"event: " + event.encode("ascii") + b"\n")
        self.wfile.write(b"data: " + raw_json.encode("utf-8") + b"\n\n")
        self.wfile.flush()

    _INPUT_CAP = 65536   # 64 KB paste cap; reject larger before reading

    def _drain_body(self):
        """Consume any declared request body so keep-alive doesn't desync on a
        rejected POST. Safe to call before sending an error."""
        try:
            n = int(self.headers.get("Content-Length", 0) or 0)
        except ValueError:
            n = 0
        remaining = n
        while remaining > 0:
            chunk = self.rfile.read(min(remaining, 65536))
            if not chunk:
                break
            remaining -= len(chunk)

    def _reject(self, code, msg, drain=True):
        # send_error() always emits Connection: close, so keep-alive does not
        # survive a reject; the drain only lets the client read the response
        # before the socket closes. Skip it for the oversized path (drain=False):
        # a >cap Content-Length is rejected BEFORE reading, and a client that
        # under-sends a lied-about length would otherwise block the drain.
        if drain:
            self._drain_body()
        self.send_error(code, msg)

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path == "/input":
            self._do_input()
        elif path == "/mouse":
            self._do_mouse()
        elif path == "/key":
            self._do_key()
        elif path == "/raw":
            self._do_raw()
        else:
            self._reject(405, "method not allowed")

    def _post_gate_and_json(self):
        """Shared POST prelude for /input, /mouse, /key: Host allow-list ->
        write-key -> Origin/Referer -> chunked/length/cap -> JSON parse. Returns
        the parsed object (dict) on success, or None AFTER having sent the error
        response (the caller just returns). Does NOT check control-on -- each
        route checks the hub's advisory gate itself so an empty keyboard batch
        can 204 without it (matches Phase B do_POST)."""
        if not self._host_ok():
            self._reject(403, "forbidden"); return None
        # Per-connection input gate (defense-in-depth over the app's advisory
        # control flag): a NON-loopback peer may drive input only when LAN input
        # was explicitly opted in. This is checked against the real TCP peer
        # (unspoofable on an established connection), so a wide 0.0.0.0 bind still
        # can't accept remote input without SAIKAI_MIRROR_ALLOW_LAN_INPUT even if
        # the advisory flag were somehow on. Viewing (GET) is unaffected.
        if not self._peer_is_loopback() and not self.server.hub.allow_lan_input:
            self._reject(403, "forbidden"); return None
        if not self._write_key_ok():
            self._reject(403, "forbidden"); return None
        if not self._origin_ok():
            self._reject(403, "forbidden"); return None
        if "chunked" in (self.headers.get("Transfer-Encoding", "") or "").lower():
            self._reject(411, "length required"); return None
        try:
            length = int(self.headers.get("Content-Length", 0) or 0)
        except ValueError:
            self._reject(400, "bad length"); return None
        if length < 0:
            self._reject(400, "bad length"); return None
        if length > self._INPUT_CAP:
            self._reject(413, "payload too large", drain=False); return None
        raw = self.rfile.read(length) if length else b""
        try:
            # RecursionError (a RuntimeError, NOT a ValueError) is reachable from a
            # small deeply-nested body well under the cap — catch it so a crafted
            # payload can't abort the handler uncleanly / spam a traceback. (#audit-mirror-json)
            obj = json.loads(raw.decode("utf-8")) if raw else {}
        except (ValueError, UnicodeDecodeError, RecursionError):
            self.send_error(400, "bad json"); return None
        if not isinstance(obj, dict):
            self.send_error(400, "bad json"); return None
        return obj

    def _do_input(self):
        obj = self._post_gate_and_json()
        if obj is None:
            return
        hub = self.server.hub
        data = obj.get("data")
        if data is None or not isinstance(data, str):
            self.send_error(400, "missing data")
            return
        if data == "":
            self._send_status(204)                      # no-op, but accepted
            return
        if not _paste_framing_ok(data):
            self.send_error(400, "bad paste framing")   # ESC smuggled inside a paste
            return
        if not hub._control_enabled:                    # advisory fast-reject
            self.send_error(409, "control off")
            return
        if not hub.inject(data):
            # rate cap / bounded queue full — the input was NOT accepted. A
            # silent 204 here made throttled keystrokes vanish with the browser
            # believing they landed. (#audit-codex-inject-429)
            self.send_error(429, "input throttled")
            return
        self._send_status(204)

    def _do_raw(self):
        """Pane-view terminal bytes (xterm onData) — written VERBATIM to the
        followed pane's child PTY by the app. No paste-framing check: unlike
        /input this stream legitimately carries ESC sequences (arrows, mouse
        reports, the bracketed-paste frame xterm itself emits), and it never
        passes through a key parser that framing could smuggle past — the pane's
        child sees exactly what a local keyboard would produce. Same gate chain
        (host, LAN opt-in, write key, origin, cap) as every other input route.
        (#pane-direct)"""
        obj = self._post_gate_and_json()
        if obj is None:
            return
        hub = self.server.hub
        data = obj.get("data")
        if data is None or not isinstance(data, str):
            self.send_error(400, "missing data")
            return
        if data == "":
            self._send_status(204)                      # no-op, but accepted
            return
        if not hub._control_enabled:                    # advisory fast-reject
            self.send_error(409, "control off")
            return
        if not hub.inject_raw(data):
            self.send_error(429, "input throttled")     # (#audit-codex-inject-429)
            return
        self._send_status(204)

    # "move" carries a held-button drag so the CHILD's own selection machinery
    # runs from the app view too (#app-native-select); the browser only sends it
    # per cell change during a select-in-pane drag.
    _MOUSE_KINDS = {"down", "up", "move", "scrollup", "scrolldown"}

    def _do_mouse(self):
        obj = self._post_gate_and_json()
        if obj is None:
            return
        hub = self.server.hub
        col, row, button, kind = (obj.get("col"), obj.get("row"),
                                  obj.get("button"), obj.get("kind"))
        # bool is an int subclass; reject it so {"col": true} can't pass as 1.
        if (not isinstance(col, int) or isinstance(col, bool)
                or not isinstance(row, int) or isinstance(row, bool)
                or not isinstance(button, int) or isinstance(button, bool)
                or kind not in self._MOUSE_KINDS):
            self.send_error(400, "bad mouse")
            return
        # Range-bound the coordinates at the mirror layer too (the app layer clamps
        # as well) so a crafted huge/negative col/row can't reach the encoder. (#audit-mirror-mouse-range)
        if not (0 <= col < 100000 and 0 <= row < 100000 and 0 <= button < 256):
            self.send_error(400, "bad mouse")
            return
        if not hub._control_enabled:
            self.send_error(409, "control off")
            return
        if not hub.inject_mouse(col, row, button, kind):
            self.send_error(429, "input throttled")   # (#audit-codex-inject-429)
            return
        self._send_status(204)

    _KEY_CAP = 64   # longest sensible key string ("ctrl+shift+pageup" << 64)

    def _do_key(self):
        obj = self._post_gate_and_json()
        if obj is None:
            return
        hub = self.server.hub
        key = obj.get("key")
        if not isinstance(key, str) or key == "" or len(key) > self._KEY_CAP:
            self.send_error(400, "bad key")
            return
        if not hub._control_enabled:
            self.send_error(409, "control off")
            return
        if not hub.inject_key(key):
            self.send_error(429, "input throttled")   # (#audit-codex-inject-429)
            return
        self._send_status(204)

    def _send_status(self, code):
        self.send_response(code)
        self.send_header("Content-Length", "0")
        self.end_headers()


class _Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True

    def __init__(self, *args, **kwargs):
        # Concurrent-connection accounting for the DoS caps. verify_request rejects
        # a socket BEFORE a handler thread is spawned, so an unauthenticated flood
        # can't park a blocked thread per connection and freeze the host UI thread.
        self._conn_lock = threading.Lock()
        self._conn_total = 0
        self._conn_per_ip: dict = {}
        self._conn_ip: dict = {}     # request socket -> normalised ip (for decrement)
        super().__init__(*args, **kwargs)

    def verify_request(self, request, client_address):
        ip = _norm_src(client_address[0]) if client_address else "?"
        with self._conn_lock:
            if (self._conn_total >= _MAX_CONNECTIONS
                    or self._conn_per_ip.get(ip, 0) >= _MAX_CONN_PER_IP):
                return False                 # over cap → socket closed, no thread
            self._conn_total += 1
            self._conn_per_ip[ip] = self._conn_per_ip.get(ip, 0) + 1
            self._conn_ip[request] = ip
        return True

    def shutdown_request(self, request):
        with self._conn_lock:
            ip = self._conn_ip.pop(request, None)   # only accepted sockets were counted
            if ip is not None:
                self._conn_total -= 1
                n = self._conn_per_ip.get(ip, 0) - 1
                if n <= 0:
                    self._conn_per_ip.pop(ip, None)
                else:
                    self._conn_per_ip[ip] = n
        super().shutdown_request(request)
    # POSIX: SO_REUSEADDR lets a restart rebind a port still in TIME_WAIT.
    # Windows: SO_REUSEADDR instead lets a SECOND process bind the same port
    # (hijack/share) — two saikai instances would then both "listen" on the
    # mirror port and connections land nondeterministically (the browser sees a
    # dead/"server stopped responding" socket). Refuse the reuse on Windows so a
    # second instance's bind fails cleanly and its mirror just stays off.
    allow_reuse_address = (sys.platform != "win32")
