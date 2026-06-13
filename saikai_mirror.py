"""Opt-in, loopback/LAN, read-only web mirror of a running saikai session.

Lives in the application layer. It tees the bytes Textual's driver is already
about to write, so the local console is byte-identical and untouched. No second
App, no second PTY, no daemon outliving the App, no transcript writes. Provider-
neutral terminal code (saikai_terminal.py) gains ZERO network code.
"""
from __future__ import annotations

import base64
import hmac
import http.server
import queue
import socketserver
import sys
import threading
import pyte
from typing import Optional


# pyte stores fg/bg as names or 6-hex strings; map names -> SGR base codes.
_FG = {"black": 30, "red": 31, "green": 32, "brown": 33, "blue": 34,
       "magenta": 35, "cyan": 36, "white": 37, "default": 39}
_BG = {k: v + 10 for k, v in _FG.items()}


def _color_sgr(value: str, table: dict, truecolor_lead: int) -> list[int]:
    if value in table:
        return [table[value]]
    if len(value) == 6:
        try:
            r, g, b = (int(value[i:i + 2], 16) for i in (0, 2, 4))
            return [truecolor_lead, 2, r, g, b]   # 38;2;r;g;b or 48;2;r;g;b
        except ValueError:
            pass
    return [table["default"]]


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
    fg_codes = _color_sgr(fg, _FG, 38)
    if fg_codes != [_FG["default"]]:   # skip ESC[39m (already covered by reset)
        parts.append("\x1b[" + ";".join(str(c) for c in fg_codes) + "m")
    bg_codes = _color_sgr(bg, _BG, 48)
    if bg_codes != [_BG["default"]]:   # skip ESC[49m
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
                 cols: int = 80, rows: int = 24, ingest_cap: int = 256) -> None:
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
        return self._port

    def stop(self) -> None:
        self._stopped.set()
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


# Browser page. xterm.js loaded from a pinned CDN (no SRI here; offline
# vendoring + SRI is an optional follow-up). The page reads ?token= from its
# own URL and opens the SSE stream with it.
_PAGE_HTML = """<!doctype html><html><head><meta charset="utf-8">
<title>saikai mirror</title>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/@xterm/xterm@5.5.0/css/xterm.min.css">
<style>html,body{margin:0;height:100%;background:#000}#t{height:100%}</style></head>
<body><div id="t"></div>
<script src="https://cdn.jsdelivr.net/npm/@xterm/xterm@5.5.0/lib/xterm.min.js"></script>
<script>
const term = new Terminal({scrollback:0, convertEol:false});
term.open(document.getElementById('t'));
const token = new URLSearchParams(location.search).get('token');
const es = new EventSource('/stream?token=' + encodeURIComponent(token));
es.onmessage = (e) => {
  const bin = atob(e.data);
  const bytes = new Uint8Array(bin.length);
  for (let i=0;i<bin.length;i++) bytes[i]=bin.charCodeAt(i);
  term.write(bytes);
};
</script></body></html>"""


class _Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):           # silence default stderr logging
        pass

    def _token_ok(self) -> bool:
        from urllib.parse import urlparse, parse_qs
        q = parse_qs(urlparse(self.path).query)
        got = (q.get("token") or [""])[0]
        return hmac.compare_digest(got, self.server.hub._token)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if not self._token_ok():
            self.send_error(403, "forbidden")
            return
        if path == "/":
            body = _PAGE_HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path == "/stream":
            self._stream()
        else:
            self.send_error(404)

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
                self._send_frame(data)
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            hub._remove_client(cq)

    def _send_frame(self, data: str):
        payload = base64.b64encode(data.encode("utf-8")).decode("ascii")
        self.wfile.write(b"data: " + payload.encode("ascii") + b"\n\n")
        self.wfile.flush()


class _Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True
