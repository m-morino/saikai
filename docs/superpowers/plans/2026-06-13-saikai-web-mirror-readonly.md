# saikai Web Mirror (Phase A: read-only) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a single already-running saikai session be viewed live in a web browser on the same machine or a trusted home/tethering LAN, showing the exact same UI (picker + split-live panes) in lockstep, read-only, token-authenticated.

**Architecture:** Inject a `MirrorDriver` subclass of Textual's auto-selected console driver into the one running `PickerApp` via `driver_class=`. Its `write()` copies every composited ANSI frame (non-blocking, drop-oldest) into a `MirrorHub`, then calls `super().write()` so the local console is byte-identical and untouched. A daemon thread drains the copy into a server-side `pyte` screen mirror and fans the bytes to connected browsers over stdlib HTTP **Server-Sent Events** (base64-framed). A new browser first receives a synthesized full-screen snapshot from the pyte mirror (so late joiners see complete state), then the live diff stream. `xterm.js` renders the ANSI natively. No second App, no second PTY, no per-visitor spawn, no daemon that outlives the app, no new network code in `saikai_terminal.py`.

**Tech Stack:** Python 3.11+, Textual 8.2.7 (`textual.drivers.windows_driver.WindowsDriver` / `linux_driver.LinuxDriver`), `pyte` (already a dependency), stdlib `http.server` + `socketserver.ThreadingMixIn` + `queue` + `secrets` + `hmac`, `xterm.js` (browser, via pinned CDN with SRI). No new Python runtime dependency.

---

## Scope

**Phase A (this plan):** read-only mirror. Output only. No input back-channel, so no input-arbitration policy and a far smaller security surface. Produces working, shippable, testable software on its own.

**Phase B (separate plan, written after Phase A works):** interactive control — browser keystrokes → `app.call_from_thread` → synthetic `events.Key` / `term._pty.write(encode_key(...))`; single-writer arbitration; browser-driven pane close routed through `AgentTerminal.kill()`/`LiveSessionManager.note_reap`. Explicitly out of scope here.

## Verified codebase facts (do not re-derive)

- `class PickerApp(App)` — `saikai.py:3336`; `on_mount` — `saikai.py:3504`; launch site `chosen = PickerApp().run()` — `saikai.py:5868`; `_resume_claude(chosen, ...)` runs AFTER the app exits — `saikai.py:5887`.
- `PickerApp` defines no `__init__`, so `PickerApp(driver_class=X)` reaches `App.__init__(driver_class=X)`.
- `App._build_driver` uses `self.driver_class` directly on the production (non-headless, non-inline) path — `app.py:3343`. Passing `driver_class=X` bypasses platform auto-detect, so `X` MUST already subclass the correct platform driver.
- `App.get_driver_class()` — `app.py:1573`: honors `TEXTUAL_DRIVER` env, else `WindowsDriver` on `WINDOWS` else `LinuxDriver`. saikai never sets `TEXTUAL_DRIVER` in production.
- `WindowsDriver.write(self, data: str)` — `windows_driver.py:47`: `self._writer_thread.write(data)`. `data` is `str`.
- `LinuxDriver.write(self, data: str)` — `linux_driver.py:187`: identical shape (`self._writer_thread.write(data)`, same `WriterThread`). macOS (`darwin`) and Linux both use `LinuxDriver`, so the tee subclass + `super().write()` is platform-uniform; `_base_driver_class()` must return `WindowsDriver` on win32 else `LinuxDriver`, matching `App.get_driver_class()`.
- `WriterThread.write(text)` is `self._queue.put(text)` with `Queue(MAX_QUEUED_WRITES=30)` — `_writer_thread.py:20,9`. (Textual's own queue CAN block when full; our copy MUST NOT — use `put_nowait` + drop-oldest.)
- The composited frame reaches the driver at `self._driver.write(terminal_sequence)` — `app.py:3883`.
- `App.refresh(*, repaint=True, layout=False, recompose=False)` — `app.py:3770`. NOTE: a plain refresh emits a compositor diff, NOT guaranteed to be a full frame for a new observer — this is exactly why the pyte snapshot path exists for late joiners.
- `AgentTerminal` — `saikai_terminal.py:605`; alias `ClaudeTerminal = AgentTerminal` — `saikai_terminal.py:1476` (existing tests build via `rt.ClaudeTerminal.__new__`).
- Tests run headless WITHOUT textual/pyte/pywinpty and are executed directly: `uv run python tests/test_<name>.py` (see `docs/ARCHITECTURE.md` Verification). Match that style: module-level `test_*` functions plus a `__main__` runner.
- saikai already registers `atexit` handlers in `main()` (`saikai.py:5860-5861`).

## Design decisions (review these)

- **Transport = SSE (one-way), stdlib only.** Read-only needs only server→browser push; SSE avoids WebSocket handshake/framing and adds zero dependency. Phase B will add a POST input endpoint or upgrade to WS.
- **Late-join correctness = server-side `pyte` mirror.** The driver stream is incremental; a late browser would see garbage. The hub feeds a `pyte` screen and synthesizes a full styled frame for each new client before streaming diffs.
- **`xterm.js` via pinned CDN + SRI.** Simplest MVP; home/tethering devices have internet. Vendoring for full offline is an optional follow-up task (Task 8).
- **Token in URL query.** `EventSource` cannot set headers. Acceptable for home/tethering trust domain; documented. Token is a 32-byte `secrets.token_urlsafe`, compared with `hmac.compare_digest`.
- **Bind host is explicit.** Default OFF. `SAIKAI_MIRROR=1` enables on `127.0.0.1`. `SAIKAI_MIRROR_HOST=0.0.0.0` (or a specific LAN IP) is required to expose on the LAN — never implicit.
- **Cross-platform.** `saikai_mirror.py` is pure stdlib + pyte with zero OS-specific code; it runs wherever saikai + Textual run — Windows, Linux, macOS — because it sits on Textual's driver layer (`WindowsDriver` on win32, `LinuxDriver` on Linux AND macOS). Caveat: saikai's own POSIX support is *experimental* (pyproject classifiers; POSIX uses `ptyprocess`), so the mirror inherits that status on Linux/macOS — it adds no new platform risk. Manual-smoke env syntax differs: `SAIKAI_MIRROR=1 uv run ...` (bash/macOS/Linux) vs `$env:SAIKAI_MIRROR=1; uv run ...` (Windows PowerShell).

## File Structure

- **Create `saikai_mirror.py`** (app layer, ~320 lines): `MirrorHub` (ingest queue, drain thread, pyte mirror, per-client SSE queues, HTTP server), `make_mirror_driver(base_cls, hub)` tee factory, `_base_driver_class()`, `_synth_full_frame(screen, cols, rows)` styled ANSI synthesizer, `_PAGE_HTML` constant. NO import of saikai application policy. NO change to `saikai_terminal.py`.
- **Modify `saikai.py`**: lazy-import `saikai_mirror`; in `main()` build hub+driver, start server, print banner, `atexit` stop, pass `driver_class`; in `PickerApp.on_mount` set size + repaint callback.
- **Modify `pyproject.toml`**: add `saikai_mirror.py` to wheel/sdist includes.
- **Modify `docs/ARCHITECTURE.md`**: document the mirror contract.
- **Create `tests/test_mirror_hub.py`**, **`tests/test_mirror_driver.py`**, **`tests/test_mirror_snapshot.py`**.

---

### Task 1: `MirrorHub` ingest queue — non-blocking, drop-oldest

**Files:**
- Create: `saikai_mirror.py`
- Test: `tests/test_mirror_hub.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_mirror_hub.py
import os, sys, threading
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import saikai_mirror as m


def test_broadcast_is_nonblocking_and_drops_oldest():
    """broadcast() runs on Textual's UI thread; it must NEVER block, even if the
    drain side is stalled. When the bounded ingest queue is full it drops the
    OLDEST item and enqueues the newest, returning immediately."""
    hub = m.MirrorHub(token="t", ingest_cap=4)
    # Do NOT start the drain thread: simulate a fully stalled consumer.
    for i in range(1000):
        start = threading.Event()
        done = threading.Event()

        def call():
            start.set()
            hub.broadcast(f"frame-{i}")
            done.set()

        th = threading.Thread(target=call)
        th.start()
        done_ok = done.wait(timeout=2.0)
        th.join(timeout=2.0)
        assert done_ok, f"broadcast() blocked on iteration {i} (cap full)"
    # Queue never exceeds its cap; only the newest survive.
    assert hub._ingest.qsize() <= 4
    drained = []
    while not hub._ingest.empty():
        drained.append(hub._ingest.get_nowait())
    assert drained == ["frame-996", "frame-997", "frame-998", "frame-999"]


if __name__ == "__main__":
    test_broadcast_is_nonblocking_and_drops_oldest()
    print("OK test_mirror_hub")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python tests/test_mirror_hub.py`
Expected: FAIL with `ModuleNotFoundError: No module named 'saikai_mirror'`.

- [ ] **Step 3: Write minimal implementation**

```python
# saikai_mirror.py
"""Opt-in, loopback/LAN, read-only web mirror of a running saikai session.

Lives in the application layer. It tees the bytes Textual's driver is already
about to write, so the local console is byte-identical and untouched. No second
App, no second PTY, no daemon outliving the App, no transcript writes. Provider-
neutral terminal code (saikai_terminal.py) gains ZERO network code.
"""
from __future__ import annotations

import queue
from typing import Optional


class MirrorHub:
    def __init__(self, token: str, host: str = "127.0.0.1", port: int = 0,
                 cols: int = 80, rows: int = 24, ingest_cap: int = 256) -> None:
        self._token = token
        self._host = host
        self._port = port
        self._cols = cols
        self._rows = rows
        self._ingest: "queue.Queue[str]" = queue.Queue(ingest_cap)

    def broadcast(self, data: str) -> None:
        """Called from Textual's UI thread (MirrorDriver.write). MUST NOT block.
        Drop the oldest frame when the ingest queue is full."""
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run python tests/test_mirror_hub.py`
Expected: `OK test_mirror_hub`

- [ ] **Step 5: Commit**

```bash
git add saikai_mirror.py tests/test_mirror_hub.py
git commit -m "feat(mirror): non-blocking drop-oldest ingest queue for web mirror"
```

---

### Task 2: pyte server-side mirror + styled full-frame synthesizer

**Files:**
- Modify: `saikai_mirror.py`
- Test: `tests/test_mirror_snapshot.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_mirror_snapshot.py
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import saikai_mirror as m


def test_snapshot_reproduces_fed_text_and_color():
    hub = m.MirrorHub(token="t", cols=20, rows=3)
    # Feed plain text + a red "HI" via SGR 31, into the server-side pyte mirror.
    hub._feed("hello")
    hub._feed("\x1b[31mHI\x1b[0m")
    frame = hub._snapshot()
    # Full repaint clears + homes the cursor, contains the visible text and a
    # red SGR for the colored cells.
    assert frame.startswith("\x1b[2J\x1b[H")
    assert "hello" in frame
    assert "HI" in frame
    assert "\x1b[31m" in frame   # red foreground re-emitted


if __name__ == "__main__":
    test_snapshot_reproduces_fed_text_and_color()
    print("OK test_mirror_snapshot")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python tests/test_mirror_snapshot.py`
Expected: FAIL with `AttributeError: 'MirrorHub' object has no attribute '_feed'`.

- [ ] **Step 3: Write minimal implementation**

Add to `saikai_mirror.py` (imports at top, `_feed`/`_snapshot`/lock in `__init__`, plus the synthesizer):

```python
import threading
import pyte

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


def _synth_full_frame(screen: "pyte.Screen", cols: int, rows: int) -> str:
    """Render a pyte screen to a self-contained full-repaint ANSI string so a
    late-joining browser gets complete state before the live diff stream."""
    out = ["\x1b[2J\x1b[H"]   # clear + home
    for y in range(rows):
        line = screen.buffer[y]
        out.append(f"\x1b[{y + 1};1H")   # absolute row, col 1
        for x in range(cols):
            ch = line[x]
            sgr = [0]
            if ch.bold:
                sgr.append(1)
            if ch.italics:
                sgr.append(3)
            if ch.underscore:
                sgr.append(4)
            if ch.reverse:
                sgr.append(7)
            sgr += _color_sgr(ch.fg, _FG, 38)
            sgr += _color_sgr(ch.bg, _BG, 48)
            out.append("\x1b[" + ";".join(str(c) for c in sgr) + "m")
            out.append(ch.data or " ")
    out.append("\x1b[0m")
    cy, cx = screen.cursor.y, screen.cursor.x
    out.append(f"\x1b[{cy + 1};{cx + 1}H")
    return "".join(out)
```

In `MirrorHub.__init__` append:

```python
        self._mirror_lock = threading.Lock()
        self._screen = pyte.Screen(cols, rows)
        self._stream = pyte.Stream(self._screen)
```

Add methods:

```python
    def _feed(self, data: str) -> None:
        with self._mirror_lock:
            self._stream.feed(data)

    def _snapshot(self) -> str:
        with self._mirror_lock:
            return _synth_full_frame(self._screen, self._cols, self._rows)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run python tests/test_mirror_snapshot.py`
Expected: `OK test_mirror_snapshot`

- [ ] **Step 5: Commit**

```bash
git add saikai_mirror.py tests/test_mirror_snapshot.py
git commit -m "feat(mirror): pyte server-side mirror + styled full-frame synthesizer"
```

---

### Task 3: `MirrorDriver` tee factory

**Files:**
- Modify: `saikai_mirror.py`
- Test: `tests/test_mirror_driver.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_mirror_driver.py
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import saikai_mirror as m


class _FakeBaseDriver:
    """Stand-in for WindowsDriver/LinuxDriver: records what reaches super().write."""
    def __init__(self, *a, **k):
        self.written = []

    def write(self, data):
        self.written.append(data)


def test_mirror_driver_tees_then_delegates():
    sent = []
    hub = type("H", (), {"broadcast": lambda self, d: sent.append(d)})()
    Drv = m.make_mirror_driver(_FakeBaseDriver, hub)
    d = Drv()                     # base __init__ takes *a, **k
    d.write("\x1b[31mX")
    # Tee'd to the hub AND delegated to the real console writer, in that order.
    assert sent == ["\x1b[31mX"]
    assert d.written == ["\x1b[31mX"]


def test_mirror_driver_never_lets_broadcast_break_console():
    """If broadcast raises, the console write MUST still happen (mirror is best
    effort and must never degrade the local UI)."""
    class _Boom:
        def broadcast(self, d):
            raise RuntimeError("drain exploded")
    Drv = m.make_mirror_driver(_FakeBaseDriver, _Boom())
    d = Drv()
    d.write("data")               # must not raise
    assert d.written == ["data"]


if __name__ == "__main__":
    test_mirror_driver_tees_then_delegates()
    test_mirror_driver_never_lets_broadcast_break_console()
    print("OK test_mirror_driver")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python tests/test_mirror_driver.py`
Expected: FAIL with `AttributeError: module 'saikai_mirror' has no attribute 'make_mirror_driver'`.

- [ ] **Step 3: Write minimal implementation**

Add to `saikai_mirror.py`:

```python
import sys


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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run python tests/test_mirror_driver.py`
Expected: `OK test_mirror_driver`

- [ ] **Step 5: Commit**

```bash
git add saikai_mirror.py tests/test_mirror_driver.py
git commit -m "feat(mirror): MirrorDriver tee factory (copy frame, then delegate)"
```

---

### Task 4: HTTP + SSE server with token auth, drain thread, atomic snapshot-then-stream

**Files:**
- Modify: `saikai_mirror.py`
- Test: `tests/test_mirror_hub.py` (extend)

- [ ] **Step 1: Write the failing test** (append to `tests/test_mirror_hub.py`, and add to `__main__`)

```python
import urllib.request, urllib.error, base64, time


def _get(url):
    return urllib.request.urlopen(url, timeout=3.0)


def test_server_rejects_bad_token_and_streams_with_good_token():
    hub = m.MirrorHub(token="secret", host="127.0.0.1", port=0, cols=10, rows=2)
    port = hub.serve()
    try:
        base = f"http://127.0.0.1:{port}"
        # Wrong token on the page and the stream → 403.
        for path in ("/", "/stream"):
            try:
                _get(f"{base}{path}?token=nope")
                assert False, f"{path} accepted a bad token"
            except urllib.error.HTTPError as e:
                assert e.code == 403
        # Good token: a frame fed now must appear (base64) in the SSE stream,
        # and the stream must open with the full-frame snapshot.
        hub.broadcast("\x1b[32mGO\x1b[0m")
        resp = _get(f"{base}/stream?token=secret")
        deadline = time.time() + 3.0
        seen = b""
        while time.time() < deadline and b"\n\n" not in seen[1:]:
            seen += resp.read(64)
        text = seen.decode("utf-8", "replace")
        assert text.startswith("data: ")
        payloads = [base64.b64decode(ln[6:]).decode("utf-8", "replace")
                    for ln in text.splitlines() if ln.startswith("data: ")]
        joined = "".join(payloads)
        assert "\x1b[2J\x1b[H" in joined   # snapshot first
        assert "GO" in joined
    finally:
        hub.stop()
```

Add to `__main__`:

```python
    test_server_rejects_bad_token_and_streams_with_good_token()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python tests/test_mirror_hub.py`
Expected: FAIL with `AttributeError: 'MirrorHub' object has no attribute 'serve'`.

- [ ] **Step 3: Write minimal implementation**

Add imports + methods to `saikai_mirror.py`:

```python
import hmac
import base64
import http.server
import socketserver
import threading


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
        client = hub._add_client()           # registers + returns (queue, snapshot)
        cq, snapshot = client
        try:
            self._send_frame(snapshot)
            while True:
                data = cq.get()
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
```

Add to `MirrorHub.__init__`:

```python
        self._clients: "set[queue.Queue]" = set()
        self._clients_lock = threading.Lock()
        self._httpd: Optional["_Server"] = None
        self._drain: Optional[threading.Thread] = None
        self._stopped = threading.Event()
```

Add `MirrorHub` methods:

```python
    def _add_client(self):
        cq: "queue.Queue[Optional[str]]" = queue.Queue(256)
        # Snapshot + registration ATOMIC vs the drain thread's feed, so no diff
        # is lost or applied twice across the join boundary.
        with self._mirror_lock:
            snapshot = _synth_full_frame(self._screen, self._cols, self._rows)
            with self._clients_lock:
                self._clients.add(cq)
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

    def url(self) -> str:
        host = "127.0.0.1" if self._host in ("0.0.0.0", "") else self._host
        return f"http://{host}:{self._port}/?token={self._token}"
```

Add a minimal page constant (xterm via pinned CDN + SRI; reads `?token=` from its own URL):

```python
_PAGE_HTML = """<!doctype html><html><head><meta charset="utf-8">
<title>saikai mirror</title>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/@xterm/xterm@5.5.0/css/xterm.min.css"
 integrity="sha384-7y1v6Z7m4y0kS0o0kS0o0kS0o0kS0o0kS0o0kS0o0kS0o0kS0o0kS0o0kS0o0kS0o" crossorigin="anonymous">
<style>html,body{margin:0;height:100%;background:#000}#t{height:100%}</style></head>
<body><div id="t"></div>
<script src="https://cdn.jsdelivr.net/npm/@xterm/xterm@5.5.0/lib/xterm.min.js"
 integrity="sha384-7y1v6Z7m4y0kS0o0kS0o0kS0o0kS0o0kS0o0kS0o0kS0o0kS0o0kS0o0kS0o0kS0o" crossorigin="anonymous"></script>
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
```

> **Execution note for SRI:** the two `integrity="sha384-..."` placeholders above are NOT real hashes. Before this task's commit, fetch the real SRI hashes for `@xterm/xterm@5.5.0` `xterm.min.js` and `xterm.min.css` (jsdelivr shows them, or compute `openssl dgst -sha384 -binary file | openssl base64 -A`) and replace both. Verify the page loads xterm without an SRI console error in Step 4.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run python tests/test_mirror_hub.py`
Expected: `OK test_mirror_hub`
Then manual: temporarily start a hub in a REPL, open `http://127.0.0.1:<port>/?token=<t>` in Edge, confirm xterm loads with no SRI error in DevTools console.

- [ ] **Step 5: Commit**

```bash
git add saikai_mirror.py tests/test_mirror_hub.py
git commit -m "feat(mirror): stdlib SSE server, token auth, drain thread, atomic snapshot-then-stream"
```

---

### Task 5: Wire the mirror into saikai's launch (opt-in flag, banner, atexit, size)

**Files:**
- Modify: `saikai.py` (`main()` near `5868`; `PickerApp.on_mount` near `3504`)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_mirror_hub.py  (append; add call to __main__)
def test_env_gate_default_off():
    import saikai_mirror as m
    # Helper that reads the env and returns (enabled, host). Default OFF.
    assert m.mirror_config({}) == (False, "127.0.0.1")
    assert m.mirror_config({"SAIKAI_MIRROR": "1"}) == (True, "127.0.0.1")
    assert m.mirror_config({"SAIKAI_MIRROR": "0", "SAIKAI_MIRROR_HOST": "0.0.0.0"}) == (False, "0.0.0.0")
    assert m.mirror_config({"SAIKAI_MIRROR": "1", "SAIKAI_MIRROR_HOST": "0.0.0.0"}) == (True, "0.0.0.0")
```

Add to `__main__`: `test_env_gate_default_off()`.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python tests/test_mirror_hub.py`
Expected: FAIL with `AttributeError: module 'saikai_mirror' has no attribute 'mirror_config'`.

- [ ] **Step 3: Write minimal implementation**

Add to `saikai_mirror.py`:

```python
def mirror_config(env: dict) -> tuple[bool, str]:
    """(enabled, host) from the environment. OFF unless SAIKAI_MIRROR is truthy."""
    val = str(env.get("SAIKAI_MIRROR", "")).strip().lower()
    enabled = val in ("1", "true", "yes", "on")
    host = str(env.get("SAIKAI_MIRROR_HOST", "")).strip() or "127.0.0.1"
    return enabled, host
```

In `saikai.py`, replace the launch block at `5867-5868` (`try:` / `chosen = PickerApp().run()`) with mirror setup before the app and teardown after. Exact replacement:

```python
    try:
        import secrets as _secrets
        import saikai_mirror as _mirror
        _mir_on, _mir_host = _mirror.mirror_config(os.environ)
        _hub = None
        _app_kwargs = {}
        if _mir_on:
            _hub = _mirror.MirrorHub(token=_secrets.token_urlsafe(32), host=_mir_host)
            _port = _hub.serve()
            atexit.register(_hub.stop)
            _Drv = _mirror.make_mirror_driver(_mirror._base_driver_class(), _hub)
            _app_kwargs["driver_class"] = _Drv
            _host_disp = "127.0.0.1" if _mir_host in ("0.0.0.0", "") else _mir_host
            print(_c(f"  ⚠ saikai mirror LIVE (read-only): "
                     f"http://{_host_disp}:{_port}/?token=... — "
                     f"{'LAN-exposed' if _mir_host not in ('127.0.0.1','') else 'loopback only'}",
                     YELLOW), file=sys.stderr)
            print(_c(f"    open: {_hub.url()}", YELLOW), file=sys.stderr)
        _app = PickerApp(**_app_kwargs)
        _app._mirror_hub = _hub
        chosen = _app.run()
```

> The existing `except KeyboardInterrupt` / `except Exception` handlers and `if chosen: _resume_claude(...)` below stay unchanged. `_c`, `YELLOW`, `atexit`, `os`, `sys` are already imported in `saikai.py`.

In `PickerApp.on_mount` (`saikai.py:3504`), append at the END of the method:

```python
            # Web mirror (opt-in): hand the live size to the hub and let a newly
            # connected browser force a full repaint so it never starts blank.
            _hub = getattr(self, "_mirror_hub", None)
            if _hub is not None:
                _hub.set_size(self.size.width, self.size.height)
                _hub.set_repaint_request(
                    lambda: self.call_from_thread(self.refresh, layout=True))
```

Add the `set_repaint_request` setter + invoke it on client connect. In `saikai_mirror.py` `MirrorHub.__init__` add `self._repaint_request = None`, add:

```python
    def set_repaint_request(self, fn) -> None:
        self._repaint_request = fn
```

and in `_add_client`, after registering the client (outside `_mirror_lock`), nudge a full repaint so subsequent diffs land on a fresh, complete frame for everyone:

```python
        if self._repaint_request is not None:
            try:
                self._repaint_request()
            except Exception:
                pass
        return cq, snapshot
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run python tests/test_mirror_hub.py`
Expected: `OK test_mirror_hub`
Run: `uv run python -m compileall -q saikai.py saikai_mirror.py`
Expected: no output (compiles clean).

- [ ] **Step 5: Commit**

```bash
git add saikai.py saikai_mirror.py tests/test_mirror_hub.py
git commit -m "feat(mirror): opt-in launch wiring, LAN banner, size + repaint hookup"
```

---

### Task 6: Real end-to-end smoke (manual) + concurrency-invariant guard test

**Files:**
- Test: `tests/test_mirror_driver.py` (extend)

- [ ] **Step 1: Write the failing test** (append; add to `__main__`)

```python
def test_broadcast_does_not_touch_mirror_lock_on_ui_thread():
    """The UI-thread path (broadcast) must only enqueue — it must NOT take the
    mirror lock (that lock is held by the drain thread during pyte.feed and by
    snapshot; taking it on the UI thread could stall the UI under load)."""
    hub = m.MirrorHub(token="t", ingest_cap=8)
    hub._mirror_lock.acquire()      # simulate drain/snapshot holding it
    try:
        import threading
        done = threading.Event()
        threading.Thread(target=lambda: (hub.broadcast("x"), done.set())).start()
        assert done.wait(timeout=2.0), "broadcast() blocked on the mirror lock"
    finally:
        hub._mirror_lock.release()


if __name__ == "__main__":
    test_mirror_driver_tees_then_delegates()
    test_mirror_driver_never_lets_broadcast_break_console()
    test_broadcast_does_not_touch_mirror_lock_on_ui_thread()
    print("OK test_mirror_driver")
```

- [ ] **Step 2: Run test to verify it fails (or passes)**

Run: `uv run python tests/test_mirror_driver.py`
Expected: PASS if Task 1's `broadcast` only touches `self._ingest` (it does). If it FAILS, `broadcast` is wrongly taking `_mirror_lock` — fix `broadcast` to enqueue only.

- [ ] **Step 3: Manual end-to-end smoke**

```bash
SAIKAI_MIRROR=1 uv run python saikai.py
```
- Confirm the yellow banner prints a `127.0.0.1:<port>` URL with a token.
- Open the URL in Edge. Confirm the FULL saikai UI (search row, session list, tabs, statusbar) appears, not blank.
- Arrow-key in the terminal; confirm the browser updates in lockstep.
- Open a split-live pane (Enter on a session); confirm the live Claude pane mirrors into the browser.
- Refresh the browser mid-session; confirm it re-paints full state immediately (snapshot path), not garbage.
- Then: `SAIKAI_MIRROR=1 SAIKAI_MIRROR_HOST=0.0.0.0 uv run python saikai.py`, open `http://<LAN-ip>:<port>/?token=...` from a phone on the same hotspot; confirm it mirrors.

- [ ] **Step 4: Run the full mandatory suite**

Run:
```bash
uv run python tests/test_mirror_hub.py
uv run python tests/test_mirror_driver.py
uv run python tests/test_mirror_snapshot.py
uv run python tests/test_terminal_concurrency.py
uv run python tests/test_resource_bounds.py
uv run python tests/test_pty_backend.py
```
Expected: all print OK / pass. (Mirror adds no path that the terminal-concurrency or resource-bounds tests should regress.)

- [ ] **Step 5: Commit**

```bash
git add tests/test_mirror_driver.py
git commit -m "test(mirror): guard that UI-thread broadcast never takes the mirror lock"
```

---

### Task 7: Package + document the mirror contract

**Files:**
- Modify: `pyproject.toml` (wheel `only-include` ~`48-49`; sdist `include` ~`52-61`)
- Modify: `docs/ARCHITECTURE.md`

- [ ] **Step 1: Add `saikai_mirror.py` to the wheel and sdist**

In `pyproject.toml` `[tool.hatch.build.targets.wheel]`:

```toml
[tool.hatch.build.targets.wheel]
# saikai.py imports both sibling modules — all must ship.
only-include = ["saikai.py", "saikai_terminal.py", "saikai_provider.py", "saikai_mirror.py"]
```

In `[tool.hatch.build.targets.sdist]` `include`, add `"saikai_mirror.py",` after `"saikai_provider.py",`.

- [ ] **Step 2: Document the contract** — append a new section to `docs/ARCHITECTURE.md`:

```markdown
## Web mirror (opt-in, read-only)

`saikai_mirror.py` (app layer) can mirror the running UI to a browser. It is
OFF unless `SAIKAI_MIRROR` is truthy, binds `127.0.0.1` unless
`SAIKAI_MIRROR_HOST` is set, and authenticates with a per-run token.

Contract:

- A `MirrorDriver` subclass of the platform console driver copies each
  composited frame to the hub (non-blocking, drop-oldest) then calls
  `super().write()`. The local console is byte-identical and untouched.
- `broadcast()` runs on the UI thread and only enqueues — it never takes the
  mirror lock, never marshals, never touches `self._lock` or the PTY. pyte
  feeding and fan-out happen on a separate drain thread.
- A server-side pyte mirror lets a late-joining browser receive a full styled
  snapshot before the live diff stream; client registration and snapshot are
  atomic w.r.t. the drain feed.
- It is ephemeral: a daemon HTTP thread that dies with the App. No daemon
  outlives the process, no database, no transcript writes.
- It does NOT cover the post-resume foreground Claude: full-takeover resume
  (`action_resume_detached`, or Enter when split-live is disabled) exits the
  App and `subprocess.run(claude_argv)` (`saikai.py`) — Textual/driver/pyte are
  gone, so the mirror goes dark until the App returns. Work in split-live panes
  to stay mirrored.
- Read-only: no browser input path exists in this phase (no input arbitration).
```

- [ ] **Step 3: Verify the package builds**

Run: `uv build`
Expected: builds an sdist + wheel with no error; `saikai_mirror.py` present in both.

- [ ] **Step 4: Commit**

```bash
git add pyproject.toml docs/ARCHITECTURE.md
git commit -m "docs(mirror): package saikai_mirror; document the read-only mirror contract"
```

---

### Task 8 (optional): Vendor xterm.js for full offline use

**Files:**
- Create: `saikai_mirror_static/xterm.min.js`, `saikai_mirror_static/xterm.min.css`
- Modify: `saikai_mirror.py` (serve `/xterm.min.js` + `/xterm.min.css` from disk; point `_PAGE_HTML` at them), `pyproject.toml` (ship the static dir)

- [ ] Only do this if offline use is required. Download the pinned `@xterm/xterm@5.5.0` assets, add `/xterm.min.js` and `/xterm.min.css` routes to `_Handler.do_GET` (same token gate, correct content-types), change the two CDN URLs in `_PAGE_HTML` to the local paths, drop the SRI attributes (same-origin), and add the static dir to the hatch build includes. Commit.

---

## Self-Review

**1. Spec coverage:**
- Same-UI mirror of running session → Tasks 3+5 (driver tee → SSE → xterm). ✓
- Picker + split-live panes covered → driver tee carries the whole composited frame. ✓
- Read-only → no input route anywhere. ✓
- LAN-only + token → Task 4 token gate + Task 5 explicit `SAIKAI_MIRROR_HOST`. ✓
- Off by default → `mirror_config` default OFF + Task 5. ✓
- Late-join not blank → Task 2 snapshot + Task 4 atomic registration + Task 5 repaint nudge. ✓
- Concurrency invariants (no UI-thread block, no `_lock`/marshal in tee) → Task 1 non-blocking, Task 6 lock-guard test, doc in Task 7. ✓
- `saikai_terminal.py` untouched / module boundary → only `saikai.py` + new `saikai_mirror.py` modified. ✓
- Ephemeral / no daemon → daemon threads + `atexit` stop; documented. ✓
- Resume blackout honestly documented → Task 7 doc. ✓

**2. Placeholder scan:** Only intentional, flagged placeholders are the two SRI hashes in Task 4, with an explicit Step to replace + verify before commit. No "TODO"/"handle errors"/"similar to" left.

**3. Type consistency:** `MirrorHub(token, host, port, cols, rows, ingest_cap)`, `broadcast(data:str)`, `_feed`/`_snapshot`, `_add_client()->(queue, snapshot)`, `_remove_client(cq)`, `serve()->int`, `stop()`, `set_size(cols,rows)`, `set_repaint_request(fn)`, `url()`, `mirror_config(env)->(bool,str)`, `make_mirror_driver(base_cls, hub)`, `_base_driver_class()`, `_synth_full_frame(screen, cols, rows)` — names match across all tasks. pyte resize order `(rows, cols)` noted. `self.size.width/height` (Textual `Size`) used in on_mount.

## Open items for Phase B (not this plan)
- Browser input back-channel (POST or WS upgrade) → `app.call_from_thread` → synthetic `events.Key` (app-level keys) and/or `term._pty.write(encode_key(...))` for the focused `AgentTerminal` (resolve via `LiveSessionManager.get(sid)`); reproduce bracketed paste.
- Single-writer input arbitration (local-wins default); browser "close pane" routed through `AgentTerminal.kill()`/`LiveSessionManager.note_reap`; join the server thread + reaps at exit.
- Optional: suppress `action_resume_detached` while the mirror is live to avoid an accidental blackout.
