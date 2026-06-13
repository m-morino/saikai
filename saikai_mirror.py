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
import queue
import socketserver
import sys
import threading
import pyte
from typing import Optional


# A control frame travels over the SAME per-client queue as output frames, but
# wrapped so _stream can send it as a named SSE event instead of base64 output.
_Control = collections.namedtuple("_Control", ["json"])


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
        self._last_accept_t = 0.0
        self._min_accept_gap = 0.0    # accepted-input rate cap (seconds between)
        self.allow_lan_input = False  # set True only via the launch opt-in

    def _feed(self, data: str) -> None:
        with self._mirror_lock:
            self._stream.feed(data)

    def _snapshot(self) -> str:
        with self._mirror_lock:
            return _synth_full_frame(self._screen, self._cols, self._rows)

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
            try:
                self._ingest.get_nowait()   # drop oldest
            except queue.Empty:
                pass
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
        return cq, snapshot

    def _remove_client(self, cq):
        with self._clients_lock:
            self._clients.discard(cq)

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
            for cq in targets:
                try:
                    cq.put_nowait(data)
                except queue.Full:
                    try:
                        cq.get_nowait()
                        cq.put_nowait(data)
                    except (queue.Empty, queue.Full):
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
        for cq in targets:
            try:
                cq.put_nowait(frame)
            except queue.Full:
                try:
                    cq.get_nowait()
                    cq.put_nowait(frame)
                except (queue.Empty, queue.Full):
                    pass
        if self._control_enabled:
            self._arm_idle_timer()
        else:
            self._cancel_idle_timer()

    def _arm_idle_timer(self) -> None:
        with self._idle_lock:
            if self._idle_timer is not None:
                self._idle_timer.cancel()
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
        """Accept browser input IFF control is on AND a handler is wired.

        Enqueues onto a single FIFO queue drained by one worker, so input
        reaches the PTY in submission order even though ThreadingMixIn
        dispatches POSTs on independent threads. Non-blocking."""
        if self._input_handler is None or not self._control_enabled:
            return False
        import time as _t
        now = _t.monotonic()
        if self._min_accept_gap and (now - self._last_accept_t) < self._min_accept_gap:
            return False                       # accepted-input rate cap
        try:
            self._inject_q.put_nowait(data)
        except queue.Full:
            return False           # bounded; refuse rather than block a handler
        self._last_accept_t = now
        self._arm_idle_timer()                 # activity keeps control alive
        return True

    def _inject_loop(self):
        """Single drain worker: pop FIFO and call the (advisory) handler. The
        handler is _marshal-shaped (captures the app, bails if gone, swallows
        exceptions), so this thread never blocks on future.result()."""
        while not self._stopped.is_set():
            try:
                data = self._inject_q.get(timeout=0.25)
            except queue.Empty:
                continue
            fn = self._input_handler
            if fn is None:
                continue
            try:
                fn(data)
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
<title>saikai mirror</title>
<link rel="stylesheet" href="/xterm.min.css">
<style>html,body{margin:0;height:100%;background:#000}#t{height:100%}</style></head>
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
  // (Ctrl-C = \x03, Enter = \r, arrows = ESC[…) are never batching-delayed.
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

term.onData((d) => {
  if (!controlOn || fatal) return;      // disabled until a control on-frame
  pending += d;
  if (isControlByte(d)) { if (flushTimer) { clearTimeout(flushTimer); flushTimer=null; } pump(); }
  else if (!flushTimer) { flushTimer = setTimeout(() => { flushTimer=null; pump(); }, 25); }
});
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
        got = self.headers.get("X-Mirror-Write-Key", "")
        ok = hmac.compare_digest(got, self.server.hub._write_key)
        if not ok:
            self.server.hub._bad_key_count += 1     # GIL-atomic increment
        return ok

    def _allowed_hosts(self) -> set:
        """The exact Host header values we accept: loopback names + the bound
        LAN IP, each with the actual served port. Anything else is a rebinding
        attempt and is refused on EVERY route."""
        port = self.server.hub._port
        hub_host = self.server.hub._host
        names = {"127.0.0.1", "localhost", "[::1]", "::1"}
        if hub_host not in ("0.0.0.0", "", "127.0.0.1", "localhost"):
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
        if path != "/input":
            self._reject(405, "method not allowed")
            return
        if not self._host_ok():
            self._reject(403, "forbidden")
            return
        if not self._write_key_ok():
            self._reject(403, "forbidden")
            return
        if not self._origin_ok():
            self._reject(403, "forbidden")
            return
        hub = self.server.hub
        # Body hygiene: chunked unsupported (require Content-Length); cap size.
        if "chunked" in (self.headers.get("Transfer-Encoding", "") or "").lower():
            self._reject(411, "length required")
            return
        try:
            length = int(self.headers.get("Content-Length", 0) or 0)
        except ValueError:
            self._reject(400, "bad length")
            return
        if length < 0:
            self._reject(400, "bad length")
            return
        if length > self._INPUT_CAP:
            self._reject(413, "payload too large", drain=False)  # reject BEFORE reading
            return
        raw = self.rfile.read(length) if length else b""
        try:
            obj = json.loads(raw.decode("utf-8")) if raw else {}
        except (ValueError, UnicodeDecodeError):
            self.send_error(400, "bad json")            # body already fully read
            return
        data = obj.get("data") if isinstance(obj, dict) else None
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
