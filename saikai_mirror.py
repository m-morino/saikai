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

# Brute-force throttle for the SSE write-key: after this many bad attempts,
# refuse input for a cooldown so a lost/guessed key can't be hammered. (#audit-mirror-ratecap)
_BAD_KEY_LOCKOUT_THRESHOLD = 20
_BAD_KEY_LOCKOUT_SECS = 30.0


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
                 idle_secs: float = 600.0) -> None:
        self._token = token
        self._host = host
        self._port = port
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
        self._bad_key_count = 0
        self._bad_key_lockout_until = 0.0   # monotonic; input refused until then
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

    def _note_bad_key(self) -> None:
        """Count a bad write-key attempt; arm a cooldown lockout at the threshold."""
        self._bad_key_count += 1
        if self._bad_key_count >= _BAD_KEY_LOCKOUT_THRESHOLD:
            import time as _t
            self._bad_key_lockout_until = _t.monotonic() + _BAD_KEY_LOCKOUT_SECS

    def _input_locked_out(self) -> bool:
        """True while the bad-key cooldown is active; auto-resets afterwards."""
        if self._bad_key_lockout_until <= 0.0:
            return False
        import time as _t
        if _t.monotonic() < self._bad_key_lockout_until:
            return True
        self._bad_key_lockout_until = 0.0
        self._bad_key_count = 0
        return False

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
        cq: "queue.Queue[Optional[str]]" = queue.Queue(256)
        # Snapshot + registration ATOMIC vs the drain thread's feed, so no diff
        # is lost or applied twice across the join boundary.
        with self._mirror_lock:
            snapshot = _synth_full_frame(self._screen, self._cols, self._rows)
            with self._clients_lock:
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

    def set_control_state(self, enabled: bool, target=None) -> None:
        """Store the advisory control state + focused-pane title and broadcast a
        control frame to every connected browser. The app's UI-thread gate is the
        authority; this copy is what do_POST fast-rejects against."""
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
        host = _lan_ip() if self._host in ("0.0.0.0", "") else self._host
        return f"http://{host}:{self._port}/?token={self._token}"


def mirror_config(env: dict) -> tuple[bool, str]:
    """(enabled, host) from the environment. OFF unless SAIKAI_MIRROR is truthy."""
    val = str(env.get("SAIKAI_MIRROR", "")).strip().lower()
    enabled = val in ("1", "true", "yes", "on")
    host = str(env.get("SAIKAI_MIRROR_HOST", "")).strip() or "127.0.0.1"
    return enabled, host


def mirror_port(env: dict) -> int:
    """Fixed mirror port from SAIKAI_MIRROR_PORT so a firewall rule can target a
    stable port; 0 (default) lets the OS pick a free ephemeral port."""
    try:
        p = int(str(env.get("SAIKAI_MIRROR_PORT", "")).strip())
    except ValueError:
        return 0
    return p if 0 < p < 65536 else 0


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


def _lan_ip() -> str:
    """Best-effort primary LAN/egress IPv4 (the interface used to reach the
    network), so a 0.0.0.0-bound mirror prints a URL reachable from another
    device. No packet is sent; falls back to 127.0.0.1 when offline."""
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


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
#kb{align-items:center}
#kb-arrows{display:grid;grid-template-areas:". up ." "left down right";gap:4px;flex:0 0 auto}
#kb-arrows>[data-k="up"]{grid-area:up}#kb-arrows>[data-k="down"]{grid-area:down}
#kb-arrows>[data-k="left"]{grid-area:left}#kb-arrows>[data-k="right"]{grid-area:right}
#kb-arrows>button{min-width:52px;padding:8px 0;flex:0 0 auto}</style></head>
<body><div id="t"></div>
<script src="/xterm.min.js"></script>
<script src="/addon-canvas.js"></script>
<script>
const term = new Terminal({cols: __COLS__, rows: __ROWS__, scrollback:0, convertEol:false});
term.open(document.getElementById('t'));
try {
  const _CA = (window.CanvasAddon && window.CanvasAddon.CanvasAddon) || window.CanvasAddon;
  term.loadAddon(new _CA());     // crisp box/block borders; falls back to DOM
} catch (e) {}
// Keep the keyboard wired to saikai: focus the terminal on load, and re-focus on
// every tap. Without this the xterm textarea can lose focus (mouse tracking eats
// the tap) and keys (Space, etc.) fall through to the browser instead of saikai.
try { term.focus(); } catch (e) {}
document.getElementById('t').addEventListener('pointerdown', () => {
  try { term.focus(); } catch (e) {}
});
// ESC built at runtime (never a literal ESC byte in this served string — a lone
// CR/ESC once broke the page; the no-control-byte test guards it).
const ESC = String.fromCharCode(27);
// Turn on mouse tracking (VT200 button + SGR encoding) by writing the DECSET
// enable into the terminal: xterm's core mouse service then attaches its own
// DOM listeners and reports taps/scrolls as ESC[<b;col;row(M|m) via onData.
try { term.write(ESC + '[?1000;1006h'); } catch (e) {}
const token = new URLSearchParams(location.search).get('token');
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
let banner = document.createElement('div');
banner.style.cssText =
  'position:fixed;top:0;left:0;right:0;font:bold 14px monospace;'+
  'padding:4px;text-align:center;z-index:9;color:#000;background:#555';
banner.textContent = 'CONTROL OFF (read-only)';
document.body.appendChild(banner);

function setBanner(on, target) {
  controlOn = on;
  // When controlling, claim a single-finger VERTICAL drag for list scroll
  // (pan-x keeps horizontal pan, pinch-zoom keeps zoom); read-only lets the
  // browser pan freely so a viewer can move around the mirrored screen.
  try { document.getElementById('t').style.touchAction = on ? 'pan-x pinch-zoom' : 'auto'; } catch (e) {}
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
  }
  function drag(y) {                     // returns true once it emits >=1 scroll
    if (lastY === null || !controlOn || fatal) return false;
    accum += y - lastY; lastY = y;
    let moved = false;
    // pointer up (y decreases) -> see items below -> scroll the list DOWN.
    while (accum <= -STEP) { accum += STEP; postMouse(scol, srow, 0, 'scrolldown'); moved = true; }
    while (accum >=  STEP) { accum -= STEP; postMouse(scol, srow, 0, 'scrollup');   moved = true; }
    return moved;
  }
  function end() { lastY = null; cancelLongPress(); }

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
    armLongPress();                       // hold without moving -> context menu
  }, {passive: true});
  el.addEventListener('touchmove', (e) => {
    if (e.touches.length !== 1) return;
    const ty = e.touches[0].clientY;
    if (Math.abs(ty - pressY) > 10 || Math.abs(e.touches[0].clientX - pressX) > 10) cancelLongPress();
    if (drag(ty)) e.preventDefault();     // we consumed this drag
  }, {passive: false});
  el.addEventListener('touchend', end, {passive: true});
  // Mouse: a held LEFT-button drag scrolls the surface under the cursor. Listens
  // on #t in the bubble phase (xterm's own listeners run first, so taps still
  // become SGR press/release); mouseup is on window so a release outside #t ends
  // the drag. Right-click opens the same context menu (the desktop gesture).
  el.addEventListener('mousedown', (e) => { if (e.button === 0) begin(e.clientX, e.clientY); });
  el.addEventListener('mousemove', (e) => {
    if (lastY === null || !(e.buttons & 1)) return;        // only while left held
    if (drag(e.clientY)) e.preventDefault();
  });
  window.addEventListener('mouseup', end);
  el.addEventListener('contextmenu', (e) => {
    if (!controlOn || fatal) return;
    e.preventDefault();
    const cc = cellAt(e.clientX, e.clientY);
    openMenu(e.clientX, e.clientY, cc[0], cc[1]);
  });
})();

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
kbBar.innerHTML =
  '<button data-k="escape">Esc</button>'+
  '<button data-k="tab">Tab</button>'+
  '<button data-k="enter">&#9166; Enter</button>'+
  '<button data-k="space">Leader</button>'+
  '<button id="kb-ctrl" data-k="">Ctrl</button>'+
  '<button data-k="ctrl+right_square_bracket">&#9776; List</button>'+
  '<button id="kb-more" data-k="">More</button>'+
  '<button data-k="f12">F12</button>'+
  '<div id="kb-arrows">'+
    '<button data-k="up">&#8593;</button>'+
    '<button data-k="left">&#8592;</button>'+
    '<button data-k="down">&#8595;</button>'+
    '<button data-k="right">&#8594;</button>'+
  '</div>'+
  // Secondary row: saikai's OWN actions, hidden until 'More' so the default bar
  // stays compact. f5/f9/f10/shift+f3/shift+f4 are PRIORITY bindings (fire even
  // with a pane focused); "Find" (slash) opens search and PgUp/PgDn/Top/End page
  // the list (these work when the list, not a pane, is focused).
  '<div id="kb2" style="display:none;flex-basis:100%;flex-wrap:wrap;gap:4px">'+
    '<button data-k="slash">Find</button>'+
    '<button data-k="f5">Refresh</button>'+
    '<button data-k="shift+f3">Next!</button>'+
    '<button data-k="f10">Close</button>'+
    '<button data-k="f9">Copy</button>'+
    '<button data-k="shift+f2">Rename</button>'+
    '<button data-k="shift+f4">Restore</button>'+
    '<button data-k="f11">Notifs</button>'+
    '<button data-k="shift+f11">Refresh</button>'+
    '<button data-k="pageup">PgUp</button>'+
    '<button data-k="pagedown">PgDn</button>'+
    '<button data-k="home">Top</button>'+
    '<button data-k="end">End</button>'+
  '</div>';
document.body.appendChild(kbBar);
const kbCtrl = document.getElementById('kb-ctrl');
const kbMore = document.getElementById('kb-more');
kbBar.querySelectorAll('button').forEach((b) => {
  b.addEventListener('click', (e) => {
    e.preventDefault();
    if (b.id === 'kb-ctrl') {                 // arm/disarm the sticky modifier
      ctrlSticky = !ctrlSticky;
      kbCtrl.style.background = ctrlSticky ? '#3a3' : '';
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
  tdiv.style.paddingTop = banner.offsetHeight + 'px';
  tdiv.style.paddingBottom = kbBar.offsetHeight + 'px';
}
fitChrome();
window.addEventListener('resize', fitChrome);
</script></body></html>"""


class _Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):           # silence default stderr logging
        pass

    # HTTP/1.1 so keep-alive + SSE behave; ALWAYS emit Content-Length or use 204.
    protocol_version = "HTTP/1.1"

    def _token_ok(self) -> bool:
        from urllib.parse import urlparse, parse_qs
        q = parse_qs(urlparse(self.path).query)
        got = (q.get("token") or [""])[0]
        return hmac.compare_digest(got, self.server.hub._token)

    def _write_key_ok(self) -> bool:
        hub = self.server.hub
        # Enforce the bad-key cooldown (was a write-only counter): a lost/guessed
        # write-key can't be hammered once the threshold trips. (#audit-mirror-ratecap)
        if hub._input_locked_out():
            return False
        got = self.headers.get("X-Mirror-Write-Key", "")
        ok = hmac.compare_digest(got, hub._write_key)
        if ok:
            hub._bad_key_count = 0                    # legit key → clear the streak
        else:
            hub._note_bad_key()
        return ok

    def _allowed_hosts(self) -> set:
        """The exact Host header values we accept: loopback names + the LAN IP
        the mirror is reachable at, each with the actual served port. Anything
        else is a rebinding attempt and is refused on EVERY route."""
        port = self.server.hub._port
        hub_host = self.server.hub._host
        names = {"127.0.0.1", "localhost", "[::1]", "::1"}
        if hub_host in ("0.0.0.0", ""):
            names.add(_lan_ip())             # wildcard bind: allow the LAN IP url() advertises
        elif hub_host not in ("127.0.0.1", "localhost"):
            names.add(hub_host)              # the specific bound LAN IP
        allowed = set()
        for n in names:
            allowed.add(n)
            allowed.add(f"{n}:{port}")
        return allowed

    def _host_ok(self) -> bool:
        host = self.headers.get("Host", "")
        return host in self._allowed_hosts()

    def _server_origins(self) -> set:
        """The exact Origin/Referer-host values that count as same-origin."""
        return {f"http://{h}" for h in self._allowed_hosts()}

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
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        cq, snapshot = hub._add_client()
        try:
            self._send_frame(snapshot)
            # Write-key (only ever over this authenticated channel) + current
            # control state, both as named raw-JSON events.
            self._send_event("writekey", json.dumps({"key": hub._write_key}))
            self._send_event("control", json.dumps(
                {"on": hub._control_enabled, "target": hub._control_target}))
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
            obj = json.loads(raw.decode("utf-8")) if raw else {}
        except (ValueError, UnicodeDecodeError):
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
        if not hub._control_enabled:                    # advisory fast-reject
            self.send_error(409, "control off")
            return
        hub.inject(data)
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
        if not hub._control_enabled:
            self.send_error(409, "control off")
            return
        hub.inject_mouse(col, row, button, kind)
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
        hub.inject_key(key)
        self._send_status(204)

    def _send_status(self, code):
        self.send_response(code)
        self.send_header("Content-Length", "0")
        self.end_headers()


class _Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    # POSIX: SO_REUSEADDR lets a restart rebind a port still in TIME_WAIT.
    # Windows: SO_REUSEADDR instead lets a SECOND process bind the same port
    # (hijack/share) — two saikai instances would then both "listen" on the
    # mirror port and connections land nondeterministically (the browser sees a
    # dead/"server stopped responding" socket). Refuse the reuse on Windows so a
    # second instance's bind fails cleanly and its mirror just stays off.
    allow_reuse_address = (sys.platform != "win32")
