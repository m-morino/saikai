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
    no interior ESC allowed. (#audit-mirror-paste-smuggle)"""
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
        try:
            self._ingest.put_nowait(data)
        except queue.Full:
            # The drain (pyte feed) has fallen behind. Do NOT drop a single oldest
            # chunk: Textual splits one logical frame into multiple chunks, so
            # dropping a MIDDLE chunk splices two unrelated byte ranges and
            # permanently corrupts the server pyte mirror. Discard the whole stale
            # backlog and flag a resync — the drain loop requests a full repaint
            # that resets pyte cleanly. (#audit-mirror-broadcast-splice)
            try:
                while True:
                    self._ingest.get_nowait()
            except queue.Empty:
                pass
            self._ingest_overflow = True
            try:
                self._ingest.put_nowait(data)
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
                if len(self._clients) >= _MAX_SSE_CLIENTS:
                    return None, None
                self._clients.add(cq)
        if self._repaint_request is not None:
            try:
                self._repaint_request()
            except Exception:
                pass
        self._notify_client_change()
        return cq, snapshot

    def _remove_client(self, cq):
        with self._clients_lock:
            self._clients.discard(cq)
        self._notify_client_change()

    def client_count(self) -> int:
        """How many browsers currently hold the SSE stream open (≈ open tabs)."""
        with self._clients_lock:
            return len(self._clients)

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
            # Server pyte may have lost data on an ingest overflow → ask the app for
            # a full repaint so a clean frame resets it. Done OFF the mirror lock and
            # from the drain thread (broadcast() on the UI thread can't call
            # _repaint_request, which marshals via call_from_thread). (#audit-mirror-broadcast-splice)
            if self._ingest_overflow:
                self._ingest_overflow = False
                fn = self._repaint_request
                if fn is not None:
                    try:
                        fn()
                    except Exception:
                        pass

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
            for cq in list(self._clients):
                try:
                    cq.put_nowait(None)
                except queue.Full:
                    pass
        if self._httpd is not None:
            try:
                self._httpd.shutdown()
            except Exception:
                pass

    def set_size(self, cols: int, rows: int) -> None:
        with self._mirror_lock:
            self._cols, self._rows = cols, rows
            self._screen.resize(rows, cols)   # pyte: (lines, columns)

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
        with self._clients_lock:
            targets = list(self._clients)
        snap = None
        for cq in targets:
            try:
                cq.put_nowait(frame)
            except queue.Full:
                # Fallen behind: resync (full repaint + this control frame) rather
                # than drop-oldest, which could evict an unconsumed control frame
                # and leave the browser banner stale. (#audit-mirror-control-loss)
                if snap is None:
                    snap = self._snapshot()
                self._resync_client(cq, snap, frame)
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
        with self._clients_lock:
            targets = list(self._clients)
        snap = None
        for cq in targets:
            try:
                cq.put_nowait(frame)
            except queue.Full:
                if snap is None:                    # resync, don't drop-oldest (#audit-mirror-control-loss)
                    snap = self._snapshot()
                self._resync_client(cq, snap, frame)

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


def _gen_self_signed(cache_dir, host: str = "") -> "tuple[str, str] | None":
    """A cached self-signed cert/key under cache_dir, generated via openssl (the
    stdlib can serve TLS but can't MINT a cert). Reused across runs while still
    valid AND still covering the current host, so the browser isn't asked to
    re-trust every launch but a network change forces a fresh cert. Returns
    (cert, key) or None when openssl is absent / generation fails → caller warns
    and stays HTTP. An EC P-256 key (near-instant keygen, cheap handshake) keeps
    startup fast on a Pi and blunts TLS-handshake CPU amplification."""
    import os as _os
    import shutil
    from pathlib import Path
    try:
        d = Path(cache_dir)
        d.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    certf, keyf = d / "mirror-cert.pem", d / "mirror-key.pem"
    ips = {"127.0.0.1"}
    lan = _lan_ip()
    if lan and lan != "127.0.0.1":
        ips.add(lan)
    if host and host not in ("0.0.0.0", "", "::", "127.0.0.1", "localhost"):
        ips.add(host)
    if certf.is_file() and keyf.is_file():
        # Reuse only if it's valid for another hour AND still covers this host.
        if (_openssl_run(["openssl", "x509", "-checkend", "3600", "-noout",
                          "-in", str(certf)], 5) == 0
                and _cert_covers(str(certf), ips)):
            return (str(certf), str(keyf))
    if not shutil.which("openssl"):
        return None
    san = "subjectAltName=" + ",".join(
        ["DNS:localhost"] + [f"IP:{ip}" for ip in sorted(ips)])
    # Restrict the umask so the private key is NEVER group/world-readable, even in
    # the window between openssl creating it and the chmod below (a TOCTOU an
    # unprivileged local user / backup job could otherwise exploit). POSIX-only;
    # os.umask is a no-op-ish on Windows but harmless.
    old_umask = None
    try:
        old_umask = _os.umask(0o077)
    except (AttributeError, OSError):
        old_umask = None
    try:
        rc = _openssl_run(
            ["openssl", "req", "-x509", "-newkey", "ec",
             "-pkeyopt", "ec_paramgen_curve:P-256", "-sha256", "-days", "365",
             "-nodes", "-keyout", str(keyf), "-out", str(certf),
             "-subj", "/CN=saikai-mirror", "-addext", san], 30)
    finally:
        if old_umask is not None:
            try:
                _os.umask(old_umask)
            except OSError:
                pass
    if rc != 0:
        return None
    try:
        _os.chmod(keyf, 0o600)
    except OSError:
        pass
    return (str(certf), str(keyf))


def resolve_tls_paths(env: dict, cache_dir, host: str = "") -> "tuple[str, str] | None":
    """(certfile, keyfile) for the mirror's TLS, or None to fall back to plain HTTP.

    Precedence: an explicit SAIKAI_MIRROR_TLS_CERT + _KEY pair (both must exist —
    a named-but-missing pair returns None rather than silently self-signing) → an
    openssl-generated self-signed cert cached under cache_dir → None. Only meaningful
    when mirror_tls_enabled(env)."""
    import os as _os
    cert = str(env.get("SAIKAI_MIRROR_TLS_CERT", "")).strip()
    key = str(env.get("SAIKAI_MIRROR_TLS_KEY", "")).strip()
    if cert or key:
        if cert and key and _os.path.isfile(cert) and _os.path.isfile(key):
            return (cert, key)
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
// Turn on mouse tracking (VT200 button + SGR encoding) by writing the DECSET
// enable into the terminal: xterm's core mouse service then attaches its own
// DOM listeners and reports taps/scrolls as ESC[<b;col;row(M|m) via onData.
try { term.write(ESC + '[?1000;1006h'); } catch (e) {}
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
const es = new EventSource('/stream?token=' + encodeURIComponent(token));

// ── Output (unchanged): default-event base64 frames -> xterm ────────────────
es.onmessage = (e) => {
  const bin = atob(e.data);
  const bytes = new Uint8Array(bin.length);
  for (let i=0;i<bin.length;i++) bytes[i]=bin.charCodeAt(i);
  term.write(bytes);
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
  }
}

es.addEventListener('writekey', (e) => {
  try { writeKey = JSON.parse(e.data).key; } catch (_) {}
});
let hostRegions = [];        // host scrollable areas in CELL coords (#mirror-regions)
es.addEventListener('regions', (e) => {
  try { hostRegions = JSON.parse(e.data) || []; } catch (_) {}
});
es.addEventListener('control', (e) => {
  let s = {}; try { s = JSON.parse(e.data); } catch (_) {}
  setBanner(!!s.on, s.target);
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
  function begin(x, y) {
    lastY = y; accum = 0; pressX = x; pressY = y;
    const cc = cellAt(x, y); scol = cc[0]; srow = cc[1];
    if (selectMode) { selA = {c: scol, r: srow}; selB = selA; drawSel(); }
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
  function drag(y, x) {                   // returns true once it consumes the move
    if (lastY === null) return false;
    // Selection is LOCAL (never touches the host) — usable in read-only too.
    if (selectMode) { selectTo(x, y); return true; }
    if (!controlOn || fatal) return false;
    accum += y - lastY; lastY = y;
    let moved = false;
    // pointer up (y decreases) -> see items below -> scroll the list DOWN.
    while (accum <= -STEP) { accum += STEP; postMouse(scol, srow, 0, 'scrolldown'); moved = true; }
    while (accum >=  STEP) { accum -= STEP; postMouse(scol, srow, 0, 'scrollup');   moved = true; }
    return moved;
  }
  function end() { lastY = null; cancelLongPress(); stopEdge(); }

  // ── Context menu (long-press on touch / right-click on mouse): act on the row
  //    under the pointer. Open => tap that cell to SELECT the row, then show an
  //    overlay whose buttons post saikai's existing action keys (resume / copy /
  //    favorite / hide / rename) for that row. A drag (scroll) cancels the press.
  let lpTimer = null, menuEl = null;
  function cancelLongPress() { if (lpTimer) { clearTimeout(lpTimer); lpTimer = null; } }
  function closeMenu() { if (menuEl) { menuEl.remove(); menuEl = null; } }
  function armLongPress() {
    if (!controlOn || fatal) return;
    cancelLongPress();
    const c = scol, r = srow, px = pressX, py = pressY;
    lpTimer = setTimeout(() => { lpTimer = null; openMenu(px, py, c, r); }, 500);
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
    if (!selectMode) armLongPress();      // hold without moving -> context menu (not while selecting)
  }, {passive: true});
  el.addEventListener('touchmove', (e) => {
    if (e.touches.length !== 1) return;
    const tx = e.touches[0].clientX, ty = e.touches[0].clientY;
    if (Math.abs(ty - pressY) > 10 || Math.abs(tx - pressX) > 10) cancelLongPress();
    if (drag(ty, tx)) e.preventDefault();  // we consumed this drag (scroll or select)
  }, {passive: false});
  el.addEventListener('touchend', end, {passive: true});
  // Mouse: a held LEFT-button drag scrolls the surface under the cursor. Listens
  // on #t in the bubble phase (xterm's own listeners run first, so taps still
  // become SGR press/release); mouseup is on window so a release outside #t ends
  // the drag. Right-click opens the same context menu (the desktop gesture).
  el.addEventListener('mousedown', (e) => { if (e.button === 0) begin(e.clientX, e.clientY); });
  window.addEventListener('mousemove', (e) => {           // window: overlays cover #t
    if (lastY === null || !(e.buttons & 1)) return;        // only while left held
    if (drag(e.clientY, e.clientX)) e.preventDefault();
  });
  window.addEventListener('mouseup', end);
  el.addEventListener('contextmenu', (e) => {
    if (!controlOn || fatal || selectMode) return;         // no row-menu while selecting
    e.preventDefault();
    const cc = cellAt(e.clientX, e.clientY);
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
  fitChrome();
}
document.getElementById('sel-copy').addEventListener('click', (e) => {
  e.preventDefault();
  const s = blockText();
  const hint = document.getElementById('sel-hint');
  if (!s) { hint.textContent = 'nothing selected — drag first'; return; }
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
const kbCtrl = document.getElementById('kb-ctrl');
const kbMore = document.getElementById('kb-more');
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
      postKey(k);
      rptT = setTimeout(() => { rptI = setInterval(() => postKey(k), 80); }, 400);
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
        pending += framed;
        pump();
        ta.value = '';
      } else if (b.id === 'comp-send-cr') {
        pending += String.fromCharCode(13);   // empty + Send⏎ = bare Enter
        pump();
      }
      try { ta.focus(); } catch (e) {}        // keep composing (keyboard stays up)
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
    postKey(k);
  });
});

// Reserve space for the fixed top banner + bottom key bar so neither covers the
// terminal. The key bar wraps to several rows on a narrow phone, so its height
// is measured (not assumed) and re-measured on resize/rotate. #t scrolls
// (overflow:auto), so the reserved padding lets the last rows clear the bar
// instead of hiding under it.
function fitChrome() {
  const tdiv = document.getElementById('t');
  if (!tdiv) return;
  // The select bar (when active) docks just under the status banner at the top.
  let top = banner.offsetHeight;
  if (selBar.style.display !== 'none') {
    selBar.style.top = top + 'px';
    top += selBar.offsetHeight;
  }
  tdiv.style.paddingTop = top + 'px';
  tdiv.style.paddingBottom = kbBar.offsetHeight + 'px';
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
            self._send_frame(snapshot)
            # Write-key (only ever over this authenticated channel) + current
            # control state, both as named raw-JSON events.
            self._send_event("writekey", json.dumps({"key": hub._write_key}))
            self._send_event("control", json.dumps(
                {"on": hub._control_enabled, "target": hub._control_target}))
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

    _MOUSE_KINDS = {"down", "up", "scrollup", "scrolldown"}

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
