# saikai Web Mirror — Phase B (Interactive Control) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the user operate the *focused* live saikai pane from a browser on the home LAN — typing keystrokes, Ctrl-C, and bracketed paste into the running Claude session — behind an explicit, default-OFF, idle-auto-disabling control toggle hardened for the fact that input injection is an RCE-equivalent capability.

**Architecture:** `PickerApp._control_enabled: bool = False` is the *authoritative* gate, re-checked on the Textual UI thread; a focus-independent priority `Binding` toggles it and pushes the new state (plus the focused-pane title, read on the UI thread) into `MirrorHub`, which keeps an *advisory* copy and broadcasts an SSE `control` frame. Browser `xterm.onData` → coalesced single-flight `POST /input` (carrying a header-only `X-Mirror-Write-Key` delivered over the authenticated SSE stream) → `do_POST` gate (Host allow-list → write-key → Origin/Referer → advisory control-on) → `MirrorHub.inject` enqueues onto one `queue.Queue` → one drain worker calls a `_marshal`-shaped handler → `app.call_from_thread(app._mirror_inject_input, data)` → `focused_pane._pty.write(data)` on the UI thread. In-order delivery is guaranteed by one in-flight client POST **and** the single server drain.

**Tech Stack:** Python stdlib `http.server` + `socketserver.ThreadingMixIn` (the existing `_Server`) + `queue` + `hmac` + `secrets` + `json` + `threading`; `pyte` (output mirror, unchanged); vendored `xterm.js` + canvas addon (browser); Textual 8.x (`PickerApp`, `call_from_thread`, priority `Binding`). **No new Python or JS dependency.**

---

## Scope

**In scope (MVP):** browser → focused live pane keyboard input (characters, Enter, Ctrl-C, arrows, bracketed paste); a runtime control toggle (focus-independent binding), default **OFF**, resets OFF on restart, **idle auto-disables** (~10 min); a separate header-only write-key; a Host allow-list on every route; Origin/Referer fail-closed; LAN input behind its own opt-in; visible control state in both the TUI and the browser.

**Out of scope (deferred):** driving saikai's own UI from the browser (pane switching, picker, scroll); routing input to a non-focused/explicitly-selected pane; persisting control state; WebSocket transport.

## Verified codebase facts (do not re-derive)

Read these once; every task below is consistent with them. Where the design summary and the real code differed, the **real code wins** and the divergence is noted inline.

- `class MirrorHub` — `saikai_mirror.py:119`. `__init__(self, token, host="127.0.0.1", port=0, cols=80, rows=24, ingest_cap=256)`. Existing fields: `_token`, `_host`, `_port`, `_cols`, `_rows`, `_ingest: queue.Queue[str]`, `_mirror_lock`, `_screen`, `_stream`, `_clients: set[queue.Queue]`, `_clients_lock`, `_httpd`, `_drain`, `_stopped = threading.Event()`, `_repaint_request = None`. **Phase B adds new fields in `__init__`; do not remove any existing field.**
- `MirrorHub.broadcast(data)` — `saikai_mirror.py:146`: non-blocking drop-oldest put onto `_ingest`. Output path is untouched by Phase B.
- `MirrorHub._add_client()` — `saikai_mirror.py:165`: returns `(cq, snapshot)`; snapshot+registration atomic under `_mirror_lock`+`_clients_lock`. The SSE `_stream` handler calls this; Phase B emits the `writekey`+`control` frames right after the snapshot, on the same connection.
- `MirrorHub._drain_loop()` — `saikai_mirror.py:184`: `while not self._stopped.is_set(): data = self._ingest.get(timeout=0.25) …`. **Copy this exact poll-with-timeout-against-`_stopped` shape for the new input drain loop** so `stop()` unblocks it.
- `MirrorHub.serve()` — `saikai_mirror.py:204`: starts the HTTP thread and the output drain thread, returns the bound port. Phase B starts the input drain thread here too.
- `MirrorHub.stop()` — `saikai_mirror.py:215`: sets `_stopped`, sends the `None` sentinel to every client queue, shuts the HTTP server. Phase B's input drain loop already exits on `_stopped`; no extra teardown needed for it (daemon thread).
- `MirrorHub.set_repaint_request(fn)` — `saikai_mirror.py:234`: single-attribute assign, written on the UI thread (on_mount), read on the HTTP thread; GIL-atomic. **`set_input_handler` mirrors this exact pattern.**
- `_Handler(http.server.BaseHTTPRequestHandler)` — `saikai_mirror.py:340`. `_token_ok()` reads `parse_qs(urlparse(self.path).query)` and `hmac.compare_digest(got, self.server.hub._token)` — `saikai_mirror.py:344`. `do_GET` — `saikai_mirror.py:354`. `_serve_static` — `:378`. `_stream` (SSE) — `:395`. `_send_frame(data)` base64-encodes and writes `b"data: " + payload + b"\n\n"` — `:422`. **There is no `protocol_version` set today (defaults to HTTP/1.0); Phase B sets `protocol_version = "HTTP/1.1"`.** Headers are read via `self.headers.get("Name")`; request bodies are read via `self.rfile.read(n)`.
- `_Server(socketserver.ThreadingMixIn, http.server.HTTPServer)` — `saikai_mirror.py:428`: `daemon_threads = True`; `allow_reuse_address = (sys.platform != "win32")`. **ThreadingMixIn is already present**, so SSE and POST run on independent threads concurrently — which is exactly why the single server-side input drain (not the handler thread) must own ordering.
- `_PAGE_HTML` — `saikai_mirror.py:315`: vendored xterm.js + canvas addon; reads `?token=` from `location.search`; `const es = new EventSource('/stream?token=' + …)`; `es.onmessage` does `atob` → bytes → `term.write(bytes)`. **Phase B adds `es.addEventListener('writekey', …)` and `es.addEventListener('control', …)` (named events bypass `onmessage`), `term.onData(...)`, and a single-flight POST sender — without disturbing the base64 `onmessage` output path.**
- `class PickerApp(App)` — `saikai.py:3400`; `BINDINGS` list — `saikai.py:3406-3471`. Focus-independence in this app is achieved with **`priority=True`** (e.g. `Binding("enter", "resume", … priority=True)` at `:3417`; the F-keys; `Binding("question_mark", "help", …, priority=True)` at `:3452`). **DIVERGENCE FROM SUMMARY:** the design says "modeled on `?`/F12 `action_mirror_info`", but the real `F12` binding at `saikai.py:3470` is **NOT** `priority=True` (it only fires from the list, where the table has focus). To be reachable while a *pane* is focused, the Phase B toggle MUST be `priority=True` like `?`/the F-keys. This plan uses `Binding("shift+f12", "toggle_mirror_control", …, priority=True)`.
- `action_mirror_info(self)` — `saikai.py:5895`: `_hub = getattr(self, "_mirror_hub", None); if _hub is None: return; …`. **Copy this no-hub guard pattern** in `action_toggle_mirror_control`.
- `_focused_terminal(self)` — `saikai.py:4714`: returns the focused live `AgentTerminal` or `None`; a DEAD pane deliberately returns `None`. Reads `self.focused`; UI-thread only.
- `on_mount` mirror-wiring block — `saikai.py:3685-3702`: `_hub = getattr(self, "_mirror_hub", None); if _hub is not None: _hub.set_size(...); _hub.set_repaint_request(lambda: self.call_from_thread(self.refresh, layout=True)); … self.call_after_refresh(self.action_mirror_info)`. **Phase B adds `_hub.set_input_handler(...)` inside this same `if _hub is not None:` block.**
- Launch/wiring block — `saikai.py:5969-6007`: under `if os.environ.get("SAIKAI_MIRROR")`, builds `MirrorHub(token=_secrets.token_urlsafe(32), host=_mir_host, port=_mirror.mirror_port(...))`, `_hub.serve()`, `atexit.register(_hub.stop)`, builds the mirror driver, writes `mirror-url.txt` (0600), prints the banner, then `_app._mirror_hub = _hub`. **Phase B adds a LAN-input opt-in gate here** (resolve `SAIKAI_MIRROR_ALLOW_LAN_INPUT`, pass it to the hub) and extends the banner.
- `saikai_terminal.py` `on_key` guard — `saikai_terminal.py:842-857`: `if self._pty is None or self.is_dead: return … data = encode_key(...); if data is None: return; try: self._pty.write(data) except Exception: pass; event.stop()`. `on_paste` — `:893-905`: `if self._pty is not None and not self.is_dead and text: … try: self._pty.write(text) except Exception: pass`. **`_mirror_inject_input` mirrors this guard exactly.** Confirmed `_pty.write` takes a **`str`** (`data`/`text` are str) — **do NOT add `.encode()`** (the design calls this out: PTY backends take str; lone-surrogate `UnicodeEncodeError` is contained by the `try/except`).
- **DIVERGENCE FROM SUMMARY — "`_marshal`-shaped":** there is **no** `_marshal` attribute on `PickerApp` (only `ClaudeTerminal` sets `ct._marshal`, e.g. `test_terminal_concurrency.py:37`). On the app, "`_marshal`-shaped" means *a closure that captures the app, bails if it's gone (`getattr(app, "is_running", True)` is False), calls `app.call_from_thread(...)`, and swallows every exception* — never a bare `call_from_thread` whose `future.result()` could block the drain/HTTP thread forever during shutdown. The handler passed to `set_input_handler` is built that way.
- Tests run headless WITHOUT textual/pyte/pywinpty and are executed directly: `uv run python tests/test_<name>.py`. **This repo does NOT use pytest.** Tests are plain module-level `def test_*()` functions plus an `if __name__ == "__main__":` runner that calls each and prints `PASS …` / `OK …`. "Verify it fails" = run the script and expect an `AssertionError`/traceback; "verify it passes" = expect the `PASS`/`OK` print. Real-hub HTTP tests use `urllib.request` on `127.0.0.1` (`tests/test_mirror_hub.py`); app-object tests build via `__new__` + a `FakePty` stub (`tests/test_terminal_concurrency.py`); app-flow tests use a skip-guarded Textual Pilot via `App.run` monkeypatch + `run_test()` (`tests/test_keyboard_leader.py`).
- Local git identity is already `m-morino` — **do not** set git identity in any command.

## Design decisions (review these)

- **Write-key is the primary credential**, not the read token. Minted per run as `secrets.token_urlsafe(32)`, delivered **only** over the authenticated SSE stream as `event: writekey`, sent back in the `X-Mirror-Write-Key` request header, compared with `hmac.compare_digest`. It never appears in any URL/QR/file/log. Requiring a custom header also forces a CORS preflight a cross-origin page cannot satisfy (the server emits no `Access-Control-Allow-*`).
- **Host allow-list on every route** (page, SSE, POST) defeats DNS rebinding (which keeps the attacker's Origin "same-origin" while pointing at the LAN IP). Allowed: `127.0.0.1[:port]`, `localhost[:port]`, and the exact bound LAN IP`[:port]`.
- **Origin/Referer fail-closed** as cheap defense-in-depth: present and exactly equal to the server origin derived from the request `Host`; reject absent / `null` / mismatch / port-mismatch.
- **Double-gate:** `do_POST` fast-rejects on the advisory `MirrorHub._control_enabled`; `_mirror_inject_input` re-checks the authoritative `PickerApp._control_enabled` on the UI thread. A brief divergence is safe because the UI-thread re-check is authority.
- **Fire-and-forget, `_marshal`-shaped injection** (never `future.result()`), single client-side in-flight POST + single server-side drain for FIFO ordering.
- **SSE control frame as a named `event: control`** carrying raw JSON (named events bypass `onmessage`, so they don't collide with the base64 output path that `atob`s every payload). Likewise `event: writekey`.
- **LAN input behind `SAIKAI_MIRROR_ALLOW_LAN_INPUT=1`**; when the read mirror is LAN-exposed but input is not opted in, input stays loopback-only (the hub refuses to enable control for non-loopback Hosts). Document a host-firewall rule scoping the port to known device IPs.
- **Idle auto-disable ~10 min** (configurable / injectable short interval for tests) bounds exposure on an unattended machine.

## File Structure

- **Modify `saikai_mirror.py`** — `MirrorHub`: add `_control_enabled=False`, `_input_handler=None`, `_write_key=secrets.token_urlsafe(32)`, `_control_target=None`, `_inject_q: queue.Queue`, `_inject_drain` thread, idle-timer state (`_idle_timer`, `_idle_secs`, `_idle_lock`), bad-key/rate counters; methods `set_input_handler(fn)`, `set_control_state(enabled, target=None)`, `inject(data)->bool`, `_inject_loop()`, `_arm_idle_timer()`/`_cancel_idle_timer()`, `allow_lan_input` flag + `_host_is_loopback()`. `_Handler`: `protocol_version="HTTP/1.1"`, `_host_ok()`, `_origin_ok()`, `_write_key_ok()`, `do_POST` (`/input`), and SSE emission of `event: writekey` + `event: control` (raw-JSON `_send_event`). `_PAGE_HTML`/JS: `writekey`/`control` listeners, `onData` coalescing single-flight POST sender, CONTROL ON/OFF banner.
- **Modify `saikai.py`** — `PickerApp`: add `_control_enabled: bool = False`; a focus-independent `Binding("shift+f12", "toggle_mirror_control", …, priority=True)`; `action_toggle_mirror_control()`; `_mirror_inject_input(data)`; in `on_mount` (inside `if _hub is not None`) wire `_hub.set_input_handler(...)` with a `_marshal`-shaped closure. Launch block (`saikai.py:5969-6007`): resolve `SAIKAI_MIRROR_ALLOW_LAN_INPUT`, set it on the hub, extend the banner.
- **Create `tests/test_mirror_input.py`** — all hub + HTTP-server tests (gate, FIFO drain, `do_POST` status matrix, Host allow-list, Origin matrix, SSE writekey/control framing + coexistence, idle auto-disable, rate-limit, concurrency coexistence).
- **Extend `tests/test_terminal_concurrency.py`** — app-object tests for `_mirror_inject_input` double-gate authority + teardown race (built via `PickerApp.__new__` + a `FakePty` + a stub `_focused_terminal`). *Rationale:* this file is the home of the `__new__`+`FakePty` invariant tests and already imports the right primitives; the new `_mirror_inject_input` tests are the same kind of UI-thread/PTY-guard invariant test.
- **Extend `tests/test_keyboard_leader.py`** — one skip-guarded Pilot test (`test_pilot_mirror_control_toggle`) that, with a focused pane, presses the toggle and asserts `_control_enabled` flips and a stub hub's `set_control_state` was called. *Rationale:* this file already owns the skip-guarded `App.run`-monkeypatch Pilot harness and a `_write_demo_session()` fixture.
- **No change to `saikai_terminal.py`.**

---

### Task 1: Hub input gate + `inject()` (no transport)

Adds the gate fields and `inject()`/`set_input_handler()` with **no HTTP and no drain thread yet** — `inject` must call the handler synchronously here so the gate logic is provable in isolation; Task 2 swaps the synchronous call for the FIFO drain.

**Files:**
- Modify: `saikai_mirror.py` (`MirrorHub.__init__` ~`:119-136`; add methods after `set_repaint_request` ~`:234`)
- Test: `tests/test_mirror_input.py` (create)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_mirror_input.py
import os, sys, threading, time, json
import urllib.request, urllib.error
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import saikai_mirror as m


def test_inject_gate_off_by_default_and_requires_handler():
    """inject() returns False when no handler is wired (nothing to deliver to)
    and when control is OFF; only an enabled hub WITH a handler accepts input."""
    hub = m.MirrorHub(token="t")
    got = []
    # No handler yet -> refuse, even if somehow enabled.
    hub._control_enabled = True
    assert hub.inject("x") is False, "no handler must refuse"
    hub.set_input_handler(lambda d: got.append(d))
    # Handler present but control OFF (default) -> refuse.
    hub._control_enabled = False
    assert hub.inject("a") is False, "control OFF must refuse"
    assert got == []
    # Control ON + handler -> accept and deliver.
    hub._control_enabled = True
    assert hub.inject("b") is True
    assert got == ["b"], got


if __name__ == "__main__":
    test_inject_gate_off_by_default_and_requires_handler()
    print("OK test_mirror_input")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python tests/test_mirror_input.py`
Expected: FAIL with `AttributeError: 'MirrorHub' object has no attribute 'set_input_handler'` (and/or no `inject`).

- [ ] **Step 3: Write minimal implementation**

Add the new fields at the end of `MirrorHub.__init__` (after `self._repaint_request = None` at `saikai_mirror.py:136`):

```python
        self._repaint_request = None
        # ── Phase B: interactive control (default OFF; app is the authority) ──
        import secrets as _secrets
        self._control_enabled = False          # advisory cache of the app's gate
        self._input_handler = None             # _marshal-shaped, set at app mount
        self._control_target = None            # focused-pane title (advisory)
        # Write-key: NEVER placed in any URL/file/QR/log; delivered only over the
        # authenticated SSE stream and required as the X-Mirror-Write-Key header.
        self._write_key = _secrets.token_urlsafe(32)
```

Add these methods right after `set_repaint_request` (`saikai_mirror.py:237`):

```python
    def set_input_handler(self, fn) -> None:
        # Written from the UI thread (on_mount), read from the input-drain thread.
        # A single attribute assignment/read is atomic under the GIL (same
        # rationale as set_repaint_request).
        self._input_handler = fn

    def inject(self, data: str) -> bool:
        """Accept browser input IFF control is on AND a handler is wired.

        Returns True when accepted (delivered to the handler), False when the
        gate is closed. The advisory _control_enabled here is a fast-reject; the
        app re-checks its authoritative gate on the UI thread."""
        if self._input_handler is None or not self._control_enabled:
            return False
        # Task 2 replaces this direct call with a FIFO single-drain enqueue.
        self._input_handler(data)
        return True
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run python tests/test_mirror_input.py`
Expected: `OK test_mirror_input`.

- [ ] **Step 5: Commit**

```
git add saikai_mirror.py tests/test_mirror_input.py && git commit -m "$(cat <<'EOF'
feat(mirror): add input gate and inject() to MirrorHub (Phase B)

Default-OFF advisory gate + handler wiring; inject() refuses without a
handler or when control is off, delivers otherwise. No transport yet.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: Hub FIFO single-drain worker

Replace `inject`'s synchronous handler call with an enqueue onto one `queue.Queue`, drained in order by a single worker thread (started in `serve()`). This is what guarantees ordering even though ThreadingMixIn dispatches POSTs on independent threads.

**Files:**
- Modify: `saikai_mirror.py` (`__init__`; `inject`; add `_inject_loop`; `serve` ~`:204-213`; `stop` already covers it via `_stopped`)
- Test: `tests/test_mirror_input.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_mirror_input.py`:

```python
def test_inject_is_fifo_via_single_drain():
    """Three rapid injects are delivered to the handler in submission order by a
    single drain worker (independent POST threads otherwise have no ordering)."""
    hub = m.MirrorHub(token="t")
    seen = []
    ev = threading.Event()

    def handler(d):
        seen.append(d)
        if len(seen) == 3:
            ev.set()

    hub.set_input_handler(handler)
    hub._control_enabled = True
    hub.serve()                       # starts the input-drain worker
    try:
        assert hub.inject("a") is True
        assert hub.inject("b") is True
        assert hub.inject("c") is True
        assert ev.wait(timeout=3.0), f"drain did not deliver 3 items: {seen}"
        assert seen == ["a", "b", "c"], seen
    finally:
        hub.stop()
```

And register it in `__main__` (before the `print`):

```python
    test_inject_is_fifo_via_single_drain()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python tests/test_mirror_input.py`
Expected: FAIL — the test asserts the worker drains the queue off the calling thread (`hub._inject_q` is read in the assertion below), but Task 1's `inject` has no `_inject_q` and delivers inline, so the run raises `AttributeError: 'MirrorHub' object has no attribute '_inject_q'`. To make that explicit, add this line at the very end of the test (after `assert seen == ["a", "b", "c"]`, inside the `try`) so the RED is unambiguous and the GREEN proves the queue path:

```python
        assert hasattr(hub, "_inject_q"), "inject must route through a FIFO queue"
```

That assertion fails now (the attribute does not exist); after Step 3 it passes and `seen == ["a", "b", "c"]` proves in-order single-drain delivery.

- [ ] **Step 3: Write minimal implementation**

Add to `MirrorHub.__init__` (right after the `self._write_key = …` line from Task 1):

```python
        self._inject_q: queue.Queue[str] = queue.Queue(1024)
        self._inject_drain = None
```

Replace the Task 1 `inject` body's delivery line so it enqueues instead of calling inline:

```python
    def inject(self, data: str) -> bool:
        """Accept browser input IFF control is on AND a handler is wired.

        Enqueues onto a single FIFO queue drained by one worker, so input
        reaches the PTY in submission order even though ThreadingMixIn
        dispatches POSTs on independent threads. Non-blocking."""
        if self._input_handler is None or not self._control_enabled:
            return False
        try:
            self._inject_q.put_nowait(data)
        except queue.Full:
            return False           # bounded; refuse rather than block a handler
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
```

Start the worker in `serve()` — add right after the output `_drain` is started (`saikai_mirror.py:212`, before `return self._port`):

```python
        self._inject_drain = threading.Thread(target=self._inject_loop,
                                              name="saikai-mirror-inject",
                                              daemon=True)
        self._inject_drain.start()
        return self._port
```

(`stop()` already sets `_stopped`, which unblocks `_inject_loop`'s `get(timeout=0.25)`. No change to `stop()`.)

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run python tests/test_mirror_input.py`
Expected: `OK test_mirror_input` (both tests; in-order `["a","b","c"]`).

- [ ] **Step 5: Commit**

```
git add saikai_mirror.py tests/test_mirror_input.py && git commit -m "$(cat <<'EOF'
feat(mirror): FIFO single-drain worker for injected input

inject() now enqueues onto one bounded queue drained by a single worker
thread started in serve(); guarantees in-order delivery to the PTY despite
ThreadingMixIn's independent POST threads. Drain exits on _stopped.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 3: `do_POST /input` — write-key + body + status codes

Add the POST endpoint with write-key auth (Host + Origin land in Tasks 4) and the full body-hygiene status matrix. `protocol_version = "HTTP/1.1"`, always drain the body even on reject.

**Files:**
- Modify: `saikai_mirror.py` (`_Handler`: add `protocol_version`, `_write_key_ok`, `_read_json_body`, `do_POST` ~ after `do_GET`/`_serve_static`)
- Test: `tests/test_mirror_input.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_mirror_input.py`:

```python
def _post(port, path, body=None, headers=None, raw=None):
    """POST helper returning (status, body_text). Uses a same-origin Host+Origin
    so this test isolates the write-key/body checks (Host/Origin get their own
    tests in Tasks 4)."""
    url = f"http://127.0.0.1:{port}{path}"
    data = raw if raw is not None else (
        json.dumps(body).encode("utf-8") if body is not None else b"")
    h = {"Host": f"127.0.0.1:{port}",
         "Origin": f"http://127.0.0.1:{port}",
         "Content-Type": "application/json"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, data=data, headers=h, method="POST")
    try:
        r = urllib.request.urlopen(req, timeout=3.0)
        return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def test_post_input_write_key_and_body_matrix():
    hub = m.MirrorHub(token="secret", host="127.0.0.1", port=0)
    delivered = []
    hub.set_input_handler(lambda d: delivered.append(d))
    hub._control_enabled = True
    port = hub.serve()
    key = hub._write_key
    try:
        WK = {"X-Mirror-Write-Key": key}
        # Good key + good body -> 204 accept, handler gets exact bytes.
        st, _ = _post(port, "/input", {"data": "ls\r"}, headers=WK)
        assert st == 204, st
        # Bad key -> 403, not delivered.
        st, _ = _post(port, "/input", {"data": "rm"},
                      headers={"X-Mirror-Write-Key": "wrong"})
        assert st == 403, st
        # Absent key -> 403.
        st, _ = _post(port, "/input", {"data": "rm"})
        assert st == 403, st
        # Missing 'data' -> 400.
        st, _ = _post(port, "/input", {"nope": 1}, headers=WK)
        assert st == 400, st
        # Non-str 'data' -> 400.
        st, _ = _post(port, "/input", {"data": 123}, headers=WK)
        assert st == 400, st
        # Empty 'data' -> 204 no-op (accepted, nothing delivered).
        st, _ = _post(port, "/input", {"data": ""}, headers=WK)
        assert st == 204, st
        # Non-JSON body -> 400.
        st, _ = _post(port, "/input", raw=b"not json", headers=WK)
        assert st == 400, st
        # Oversized Content-Length -> 413 (declared > 64 KB cap).
        big = {"X-Mirror-Write-Key": key, "Content-Length": str(70000)}
        st, _ = _post(port, "/input", raw=b"x" * 10, headers=big)
        assert st == 413, st
        # Chunked transfer -> 411 (we require a Content-Length).
        ch = {"X-Mirror-Write-Key": key, "Transfer-Encoding": "chunked"}
        st, _ = _post(port, "/input", raw=b"5\r\nhello\r\n0\r\n\r\n", headers=ch)
        assert st == 411, st
        # GET on /input is 405 (only POST).
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/input", timeout=3.0)
            assert False, "GET /input should 405"
        except urllib.error.HTTPError as e:
            assert e.code in (403, 405), e.code   # token-gated GET path -> 403/405
        # Only the one good "ls\r" reached the handler.
        assert delivered == ["ls\r"], delivered
    finally:
        hub.stop()
```

Register in `__main__`:

```python
    test_post_input_write_key_and_body_matrix()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python tests/test_mirror_input.py`
Expected: FAIL — `do_POST` does not exist, so `BaseHTTPRequestHandler` returns 501 for POST; the first `assert st == 204` fails.

- [ ] **Step 3: Write minimal implementation**

Set the protocol version on `_Handler` — add right after `class _Handler(...)`'s `log_message` (`saikai_mirror.py:342`):

```python
    # HTTP/1.1 so keep-alive + SSE behave; ALWAYS emit Content-Length or use 204.
    protocol_version = "HTTP/1.1"
```

Add a write-key check next to `_token_ok` (`saikai_mirror.py:348`):

```python
    def _write_key_ok(self) -> bool:
        got = self.headers.get("X-Mirror-Write-Key", "")
        return hmac.compare_digest(got, self.server.hub._write_key)
```

Add the body reader + `do_POST` after `_serve_static` (`saikai_mirror.py:393`). The body is ALWAYS drained before any reject return (keep-alive desync otherwise). `_INPUT_CAP = 65536`.

```python
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

    def _reject(self, code, msg):
        self._drain_body()
        self.send_error(code, msg)

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path != "/input":
            self._reject(405, "method not allowed")
            return
        # (Host + Origin gates are added in the next task; for now write-key only.)
        if not self._write_key_ok():
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
        if length > self._INPUT_CAP:
            self._reject(413, "payload too large")     # reject BEFORE reading
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
```

Add `import json` to the module imports (`saikai_mirror.py:10-18`, alongside `import base64`):

```python
import json
```

(The 409 control-off branch is exercised in Task 5's coexistence tests once control toggling is observable; the matrix here keeps control ON.)

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run python tests/test_mirror_input.py`
Expected: `OK test_mirror_input` (all tests; matrix green; only `"ls\r"` delivered).

- [ ] **Step 5: Commit**

```
git add saikai_mirror.py tests/test_mirror_input.py && git commit -m "$(cat <<'EOF'
feat(mirror): POST /input endpoint with write-key + body hygiene

HTTP/1.1; X-Mirror-Write-Key checked via hmac.compare_digest; JSON {"data":str}
with 204/400/403/409/411/413/405 status matrix; 64 KB cap rejected before read;
chunked -> 411; body always drained before reject (keep-alive safe).

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 4: Host allow-list (all routes) + Origin/Referer fail-closed

Add `_host_ok()` enforced on **every** request (GET page, SSE, POST) and `_origin_ok()` enforced on `do_POST`. Host allow-list defeats DNS rebinding; Origin/Referer fail-closed is defense-in-depth.

**Files:**
- Modify: `saikai_mirror.py` (`_Handler`: add `_host_ok`, `_origin_ok`; call `_host_ok` at the top of `do_GET` and `do_POST`; call `_origin_ok` in `do_POST`)
- Test: `tests/test_mirror_input.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_mirror_input.py` (uses a raw socket so we can forge `Host`/omit `Origin`, which `urllib` won't let us do cleanly):

```python
def _raw_request(port, method, path, headers):
    """Send a raw HTTP/1.1 request with EXACT headers (so we can forge Host or
    omit Origin); return the numeric status from the response line."""
    import socket
    body = b""
    if headers.get("_body") is not None:
        body = headers.pop("_body")
        headers["Content-Length"] = str(len(body))
    lines = [f"{method} {path} HTTP/1.1"]
    lines += [f"{k}: {v}" for k, v in headers.items() if not k.startswith("_")]
    lines += ["Connection: close", "", ""]
    raw = ("\r\n".join(lines)).encode("ascii") + body
    s = socket.create_connection(("127.0.0.1", port), timeout=3.0)
    try:
        s.sendall(raw)
        resp = b""
        while b"\r\n" not in resp:
            chunk = s.recv(256)
            if not chunk:
                break
            resp += chunk
        first = resp.split(b"\r\n", 1)[0].decode("ascii", "replace")
        return int(first.split(" ")[1])     # "HTTP/1.1 <code> <reason>"
    finally:
        s.close()


def test_host_allow_list_and_origin_matrix():
    hub = m.MirrorHub(token="secret", host="127.0.0.1", port=0)
    hub.set_input_handler(lambda d: None)
    hub._control_enabled = True
    port = hub.serve()
    key = hub._write_key
    good_host = f"127.0.0.1:{port}"
    body = json.dumps({"data": "x"}).encode("utf-8")
    try:
        base = {"X-Mirror-Write-Key": key, "Content-Type": "application/json"}

        def H(**extra):
            h = dict(base); h["_body"] = body; h.update(extra); return h

        # Foreign Host on the PAGE route -> 403 (anti DNS-rebinding, all routes).
        assert _raw_request(port, "GET", "/?token=secret",
                            {"Host": "evil.example.com"}) == 403
        # Foreign Host on POST -> 403 even with a valid key + Origin.
        assert _raw_request(port, "POST", "/input",
                            H(Host="evil.example.com",
                              Origin="http://evil.example.com")) == 403
        # Matching Host + matching Origin -> 204.
        assert _raw_request(port, "POST", "/input",
                            H(Host=good_host,
                              Origin=f"http://{good_host}")) == 204
        # Cross-origin (Origin != server origin) -> 403.
        assert _raw_request(port, "POST", "/input",
                            H(Host=good_host,
                              Origin="http://attacker.test")) == 403
        # Absent Origin AND absent Referer -> 403 (fail-closed).
        assert _raw_request(port, "POST", "/input",
                            H(Host=good_host)) == 403
        # Absent Origin but matching Referer host -> 204 (Referer fallback).
        assert _raw_request(port, "POST", "/input",
                            H(Host=good_host,
                              Referer=f"http://{good_host}/")) == 204
        # Origin host matches but PORT differs -> 403 (exact origin equality).
        assert _raw_request(port, "POST", "/input",
                            H(Host=good_host,
                              Origin="http://127.0.0.1:1")) == 403
        # Literal "null" Origin -> 403.
        assert _raw_request(port, "POST", "/input",
                            H(Host=good_host, Origin="null")) == 403
    finally:
        hub.stop()
```

Register in `__main__`:

```python
    test_host_allow_list_and_origin_matrix()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python tests/test_mirror_input.py`
Expected: FAIL — without `_host_ok`, the foreign-Host page request returns 200/403-by-token (not the required 403-by-host) and the absent-Origin POST returns 204 instead of 403; the first foreign-Host assertion fails.

- [ ] **Step 3: Write minimal implementation**

Add `_host_ok` and `_origin_ok` next to `_token_ok` (`saikai_mirror.py:348`):

```python
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
```

Enforce `_host_ok` at the very top of `do_GET` (`saikai_mirror.py:354`, before the static-asset check):

```python
    def do_GET(self):
        if not self._host_ok():
            self.send_error(403, "forbidden")
            return
        path = self.path.split("?", 1)[0]
        if path in self._STATIC:               # public library asset; no token
```

Add `_host_ok` + `_origin_ok` to `do_POST`, immediately after the `path != "/input"` guard and BEFORE the write-key check (so a forged Host/Origin is refused regardless of key):

```python
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
        # … (unchanged body hygiene from Task 3) …
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run python tests/test_mirror_input.py`
Expected: `OK test_mirror_input` (all tests; full Host + Origin matrix green).

- [ ] **Step 5: Commit**

```
git add saikai_mirror.py tests/test_mirror_input.py && git commit -m "$(cat <<'EOF'
feat(mirror): Host allow-list on all routes + Origin/Referer fail-closed

_host_ok (loopback names + bound LAN IP, exact port) enforced on GET and POST
defeats DNS rebinding; _origin_ok requires an exact same-origin Origin (or
Referer host fallback), rejecting absent/null/cross-origin/port-mismatch.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 5: write-key + control over SSE (named events)

Emit `event: writekey` (raw JSON) and `event: control` (raw JSON `{on,target}`) on SSE connect, and push `event: control` from `set_control_state`. Output frames stay exactly as today (default-event, base64, via `onmessage`) and must not collide.

**Files:**
- Modify: `saikai_mirror.py` (`MirrorHub`: `set_control_state`, broadcast control to client queues via a typed sentinel; `_Handler._stream` emits the two named events on connect and forwards control frames; add `_send_event(event, raw_json)`)
- Test: `tests/test_mirror_input.py`

> **Design note on the control-broadcast mechanism (real-code-aware):** today every item placed on a client queue is a `str` that `_send_frame` base64-encodes for `onmessage`. To carry a *control* frame over the same per-client queue without changing the output path, wrap control payloads in a tiny typed object and have `_stream` branch on its type. We add a `_Control` namedtuple-shaped marker; `set_control_state` puts a `_Control(json_str)` onto each client queue, and `_stream` sends it as `event: control`. Plain `str` items remain output frames. The `None` sentinel (stop) is unchanged.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_mirror_input.py`:

```python
def _read_sse(resp, deadline_s=3.0, until=b"event: control"):
    """Read SSE bytes until `until` has appeared (after the snapshot), or time out."""
    import time as _t
    end = _t.time() + deadline_s
    seen = b""
    while _t.time() < end and until not in seen:
        seen += resp.read1(128)
    return seen.decode("utf-8", "replace")


def test_sse_emits_writekey_and_control_without_colliding_output():
    import base64
    hub = m.MirrorHub(token="secret", host="127.0.0.1", port=0, cols=10, rows=2)
    hub.set_input_handler(lambda d: None)
    port = hub.serve()
    try:
        resp = urllib.request.urlopen(
            f"http://127.0.0.1:{port}/stream?token=secret", timeout=3.0)
        text = _read_sse(resp, until=b"event: control")
        # On connect: a writekey event (raw JSON, NOT base64) and a control event
        # reflecting the default OFF state with a null target.
        assert "event: writekey" in text, text
        assert hub._write_key in text, "write-key must arrive over SSE"
        assert "event: control" in text, text
        assert '"on": false' in text or '"on":false' in text, text
        assert '"target": null' in text or '"target":null' in text, text
        # A normal output frame still arrives as a base64 default-event.
        hub.broadcast("\x1b[32mGO\x1b[0m")
        out = _read_sse(resp, until=b"data: ")
        payloads = [ln[6:] for ln in out.splitlines() if ln.startswith("data: ")]
        joined = "".join(base64.b64decode(p).decode("utf-8", "replace")
                         for p in payloads)
        assert "GO" in joined, joined        # output path intact, not collided
    finally:
        hub.stop()


def test_set_control_state_pushes_control_frame():
    hub = m.MirrorHub(token="secret", host="127.0.0.1", port=0, cols=10, rows=2)
    hub.set_input_handler(lambda d: None)
    port = hub.serve()
    try:
        resp = urllib.request.urlopen(
            f"http://127.0.0.1:{port}/stream?token=secret", timeout=3.0)
        _read_sse(resp, until=b"event: control")        # drain the on-connect frames
        hub.set_control_state(True, "Session S")
        text = _read_sse(resp, until=b'"on": true')
        assert "event: control" in text, text
        assert '"on": true' in text or '"on":true' in text, text
        assert "Session S" in text, text
    finally:
        hub.stop()
```

Register both in `__main__`:

```python
    test_sse_emits_writekey_and_control_without_colliding_output()
    test_set_control_state_pushes_control_frame()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python tests/test_mirror_input.py`
Expected: FAIL — `_stream` emits only the base64 snapshot today; there is no `event: writekey`/`event: control`, so `assert "event: writekey" in text` fails. (`set_control_state` doesn't exist yet → second test fails with `AttributeError`.)

- [ ] **Step 3: Write minimal implementation**

Add the typed control marker near the top of `saikai_mirror.py` (after the imports, e.g. before `_BASIC` at `:23`):

```python
import collections
# A control frame travels over the SAME per-client queue as output frames, but
# wrapped so _stream can send it as a named SSE event instead of base64 output.
_Control = collections.namedtuple("_Control", ["json"])
```

Add `set_control_state` to `MirrorHub` (after `set_input_handler` from Task 1):

```python
    def set_control_state(self, enabled: bool, target=None) -> None:
        """Store the advisory control state + focused-pane title and broadcast a
        control frame to every connected browser. The app's UI-thread gate is the
        authority; this copy is what do_POST fast-rejects against."""
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
```

Add `_send_event` to `_Handler` (next to `_send_frame` at `saikai_mirror.py:422`):

```python
    def _send_event(self, event: str, raw_json: str):
        """Emit a NAMED SSE event carrying raw JSON (consumed by the browser's
        addEventListener, NOT onmessage — so it never hits the base64 atob path)."""
        self.wfile.write(b"event: " + event.encode("ascii") + b"\n")
        self.wfile.write(b"data: " + raw_json.encode("utf-8") + b"\n\n")
        self.wfile.flush()
```

Extend `_stream` (`saikai_mirror.py:395-420`) to send the two named events right after the snapshot, and to branch on `_Control` in the loop:

```python
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
                    self.wfile.write(b":\n\n")
                    self.wfile.flush()
                    continue
                if data is None:                 # stop sentinel
                    break
                if isinstance(data, _Control):   # named control event, not output
                    self._send_event("control", data.json)
                    continue
                self._send_frame(data)
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            hub._remove_client(cq)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run python tests/test_mirror_input.py`
Expected: `OK test_mirror_input` (writekey+control on connect; control push on `set_control_state(True,"Session S")`; output `GO` still arrives base64 via `onmessage`).

- [ ] **Step 5: Commit**

```
git add saikai_mirror.py tests/test_mirror_input.py && git commit -m "$(cat <<'EOF'
feat(mirror): deliver write-key + control state over SSE as named events

On connect _stream emits `event: writekey` and `event: control` (raw JSON);
set_control_state broadcasts an `event: control` to every client via a typed
_Control queue marker. Output frames stay default-event base64 (onmessage) and
do not collide with the named-event path.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 6: Idle auto-disable + bad-key / rate counters

Add an idle timer (default ~600 s, injectable short for the test) that flips control OFF (and broadcasts the off frame) when no input is accepted within the window, plus a simple bad-key failure counter and an accepted-input rate cap.

**Files:**
- Modify: `saikai_mirror.py` (`MirrorHub.__init__` idle/rate fields + `_idle_secs` ctor arg; `set_control_state` arms/cancels the timer; `inject` resets the idle timer + enforces the rate cap; `_Handler._write_key_ok` bumps a bad-key counter)
- Test: `tests/test_mirror_input.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_mirror_input.py`:

```python
def test_idle_auto_disable_flips_control_off():
    """With a short idle window and no accepted input, control auto-disables and
    an OFF control frame is broadcast."""
    hub = m.MirrorHub(token="secret", host="127.0.0.1", port=0, idle_secs=0.3)
    hub.set_input_handler(lambda d: None)
    port = hub.serve()
    try:
        hub.set_control_state(True, "S")        # arms the idle timer
        assert hub._control_enabled is True
        time.sleep(0.7)                          # no input within the window
        assert hub._control_enabled is False, "idle window must auto-disable"
    finally:
        hub.stop()


def test_accepted_input_resets_idle_timer():
    hub = m.MirrorHub(token="secret", host="127.0.0.1", port=0, idle_secs=0.4)
    hub.set_input_handler(lambda d: None)
    port = hub.serve()
    try:
        hub.set_control_state(True, "S")
        for _ in range(3):                       # keep poking before the window
            time.sleep(0.2)
            assert hub.inject("x") is True
        assert hub._control_enabled is True, "activity must keep control alive"
    finally:
        hub.stop()


def test_bad_write_key_increments_failure_counter():
    hub = m.MirrorHub(token="secret", host="127.0.0.1", port=0)
    hub.set_input_handler(lambda d: None)
    hub._control_enabled = True
    port = hub.serve()
    try:
        before = hub._bad_key_count
        _post(port, "/input", {"data": "x"},
              headers={"X-Mirror-Write-Key": "wrong"})
        assert hub._bad_key_count == before + 1, hub._bad_key_count
    finally:
        hub.stop()
```

Register in `__main__`:

```python
    test_idle_auto_disable_flips_control_off()
    test_accepted_input_resets_idle_timer()
    test_bad_write_key_increments_failure_counter()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python tests/test_mirror_input.py`
Expected: FAIL — `MirrorHub.__init__` has no `idle_secs` kwarg (`TypeError`), and there is no `_bad_key_count`/idle timer.

- [ ] **Step 3: Write minimal implementation**

Extend the `__init__` signature (`saikai_mirror.py:120`) and add fields (after the Task 1/2 block):

```python
    def __init__(self, token: str, host: str = "127.0.0.1", port: int = 0,
                 cols: int = 80, rows: int = 24, ingest_cap: int = 256,
                 idle_secs: float = 600.0) -> None:
```

```python
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
```

Add the idle-timer helpers + rate-cap to `MirrorHub` (after `set_control_state`):

```python
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
```

Update `set_control_state` to (re)arm or cancel the timer — append to its body (after the broadcast loop):

```python
        if self._control_enabled:
            self._arm_idle_timer()
        else:
            self._cancel_idle_timer()
```

Update `inject` to reset the idle timer + enforce the rate cap on accept:

```python
    def inject(self, data: str) -> bool:
        if self._input_handler is None or not self._control_enabled:
            return False
        import time as _t
        now = _t.monotonic()
        if self._min_accept_gap and (now - self._last_accept_t) < self._min_accept_gap:
            return False                       # accepted-input rate cap
        try:
            self._inject_q.put_nowait(data)
        except queue.Full:
            return False
        self._last_accept_t = now
        self._arm_idle_timer()                 # activity keeps control alive
        return True
```

Bump the bad-key counter in `_write_key_ok` (`_Handler`):

```python
    def _write_key_ok(self) -> bool:
        got = self.headers.get("X-Mirror-Write-Key", "")
        ok = hmac.compare_digest(got, self.server.hub._write_key)
        if not ok:
            self.server.hub._bad_key_count += 1     # GIL-atomic increment
        return ok
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run python tests/test_mirror_input.py`
Expected: `OK test_mirror_input` (idle flips OFF; activity keeps it ON; bad key bumps the counter).

- [ ] **Step 5: Commit**

```
git add saikai_mirror.py tests/test_mirror_input.py && git commit -m "$(cat <<'EOF'
feat(mirror): idle auto-disable + bad-key/rate counters

set_control_state(True) arms a threading.Timer (default 600s, injectable);
accepted input re-arms it; the timeout flips control OFF and broadcasts the
off frame. Bad write-key attempts bump a counter; an optional accepted-input
rate cap bounds UI-thread flooding.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 7: App `_mirror_inject_input` + double-gate authority

Add `PickerApp._mirror_inject_input(data)` that re-checks the authoritative `_control_enabled`, finds the focused pane, and writes via `_pty.write` with the EXACT `on_key` guard. Prove the double-gate: a stale-ON hub copy must not let injection through when the app's gate is OFF.

**Files:**
- Modify: `saikai.py` (`PickerApp`: add `_control_enabled: bool = False` class attr near other class-level state; add `_mirror_inject_input` near `_focused_terminal` ~`:4714`)
- Test: `tests/test_terminal_concurrency.py` (extend; built via `PickerApp.__new__` + `FakePty` + stub `_focused_terminal`)

> **Why this file:** `test_terminal_concurrency.py` is the established home of `__new__`+`FakePty` UI-thread/PTY-guard invariant tests; `_mirror_inject_input` is exactly that kind of guard. It imports `saikai_terminal as rt`; add `import saikai` at the top for the app object.

- [ ] **Step 1: Write the failing test**

Add `import saikai` to the imports of `tests/test_terminal_concurrency.py` (after `import saikai_terminal as rt` at `:12`):

```python
import saikai
```

Add these tests (anywhere among the `def test_*` functions):

```python
def test_mirror_inject_input_writes_only_when_app_gate_on():
    """_mirror_inject_input re-checks the AUTHORITATIVE PickerApp._control_enabled
    on the UI thread; the hub's advisory copy being stale-ON must NOT inject."""
    writes = []

    class _FakePty:
        def write(self, data):
            writes.append(data)

    class _Term:
        def __init__(self):
            self._pty = _FakePty()
            self.is_dead = False

    app = saikai.PickerApp.__new__(saikai.PickerApp)
    term = _Term()
    app._focused_terminal = lambda: term

    # App gate OFF -> no write, even though a focused live pane exists.
    app._control_enabled = False
    app._mirror_inject_input("rm -rf /\r")
    assert writes == [], "app gate OFF must not inject (authority re-check)"

    # App gate ON + alive pane -> exact bytes written.
    app._control_enabled = True
    app._mirror_inject_input("ls\r")
    assert writes == ["ls\r"], writes


def test_mirror_inject_input_noops_without_pane_or_when_dead():
    """No focused pane, a dead pane, or _pty is None -> no-op (mirrors on_key)."""
    writes = []

    class _FakePty:
        def write(self, data):
            writes.append(data)

    app = saikai.PickerApp.__new__(saikai.PickerApp)
    app._control_enabled = True

    # No focused pane.
    app._focused_terminal = lambda: None
    app._mirror_inject_input("a")
    assert writes == [], "no pane must no-op"

    # Dead pane.
    class _Dead:
        _pty = _FakePty()
        is_dead = True
    app._focused_terminal = lambda: _Dead()
    app._mirror_inject_input("b")
    assert writes == [], "dead pane must no-op"

    # _pty is None.
    class _NoPty:
        _pty = None
        is_dead = False
    app._focused_terminal = lambda: _NoPty()
    app._mirror_inject_input("c")
    assert writes == [], "_pty None must no-op"


def test_mirror_inject_input_swallows_pty_write_errors():
    """A hostile/torn write (UnicodeEncodeError, child gone) is contained, exactly
    like on_key's try/except — no exception escapes to the UI thread."""
    class _BoomPty:
        def write(self, data):
            raise RuntimeError("child went away")

    class _Term:
        _pty = _BoomPty()
        is_dead = False

    app = saikai.PickerApp.__new__(saikai.PickerApp)
    app._control_enabled = True
    app._focused_terminal = lambda: _Term()
    app._mirror_inject_input("x")     # must not raise
```

Register in `__main__` (add three lines with their `print("PASS …")` after an existing block):

```python
    test_mirror_inject_input_writes_only_when_app_gate_on()
    print("PASS test_mirror_inject_input_writes_only_when_app_gate_on")
    test_mirror_inject_input_noops_without_pane_or_when_dead()
    print("PASS test_mirror_inject_input_noops_without_pane_or_when_dead")
    test_mirror_inject_input_swallows_pty_write_errors()
    print("PASS test_mirror_inject_input_swallows_pty_write_errors")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python tests/test_terminal_concurrency.py`
Expected: FAIL with `AttributeError: 'PickerApp' object has no attribute '_mirror_inject_input'`.

- [ ] **Step 3: Write minimal implementation**

Add the class attribute to `PickerApp` — put it right after `MAX_LIVE = …` (`saikai.py:3477`):

```python
        # Phase B web-mirror interactive control. AUTHORITATIVE gate, default OFF,
        # in-memory, re-checked on the UI thread in _mirror_inject_input. The hub
        # keeps only an advisory copy for do_POST's fast-reject.
        _control_enabled: bool = False
```

Add `_mirror_inject_input` right after `_focused_terminal` (`saikai.py:4726`):

```python
        def _mirror_inject_input(self, data: str) -> None:
            """Write browser-injected bytes into the focused live pane's PTY.

            Runs on the Textual UI thread (the input handler marshals here via
            call_from_thread). Re-checks the AUTHORITATIVE _control_enabled (the
            hub's copy is advisory), then mirrors on_key's guard EXACTLY: bail on
            no pane / dead pane / _pty is None, and contain any write error. The
            PTY backend takes str — do NOT .encode()."""
            if not self._control_enabled:
                return
            t = self._focused_terminal()
            if t is None or getattr(t, "_pty", None) is None or getattr(t, "is_dead", False):
                return
            try:
                t._pty.write(data)
            except Exception:
                pass
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run python tests/test_terminal_concurrency.py`
Expected: the existing `PASS …` lines plus `PASS test_mirror_inject_input_writes_only_when_app_gate_on`, `PASS test_mirror_inject_input_noops_without_pane_or_when_dead`, `PASS test_mirror_inject_input_swallows_pty_write_errors`.

- [ ] **Step 5: Commit**

```
git add saikai.py tests/test_terminal_concurrency.py && git commit -m "$(cat <<'EOF'
feat(mirror): app-authoritative _mirror_inject_input with double-gate

PickerApp._control_enabled is the authority; _mirror_inject_input re-checks it
on the UI thread, mirrors on_key's guard (no pane / dead / _pty None -> no-op),
writes str via _pty.write, and contains write errors. Hub copy stale-ON cannot
inject when the app gate is OFF.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 8: App focus-independent toggle binding + `on_mount` wiring

Add the `priority=True` toggle `Binding` and `action_toggle_mirror_control` (computes the target via `_focused_terminal().title` on the UI thread, calls `set_control_state`, guards no-hub, notifies + sets an indicator). Wire `set_input_handler` in `on_mount` with a `_marshal`-shaped closure. Prove reachability with a skip-guarded Pilot test.

**Files:**
- Modify: `saikai.py` (`PickerApp.BINDINGS` add the toggle ~ after `:3470`; `action_toggle_mirror_control` near `action_mirror_info` ~`:5895`; `on_mount` wiring inside `if _hub is not None` ~`:3686-3702`)
- Test: `tests/test_keyboard_leader.py` (extend; skip-guarded Pilot)

> **DIVERGENCE FROM SUMMARY:** the design names "F12" as the model, but the real F12 binding (`saikai.py:3470`) is non-priority and only reachable from the list. A focused *pane* consumes keys, so to be reachable exactly when control is used, the toggle MUST be `priority=True` (like `?`/the F-keys). We bind **`shift+f12`** (`f12` is taken by `mirror_info`; Claude Code binds no F-keys, so Shift+F-keys are safe even over a focused pane — see the BINDINGS comment at `saikai.py:3432`). Give it `id="mirror_control"` so it is remappable via `[keys]` like the other ids.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_keyboard_leader.py` (after the other Pilot tests, before `__main__`):

```python
def test_pilot_mirror_control_toggle():
    """A focus-independent priority binding toggles _control_enabled and pushes
    the new state into the hub EVEN WHILE A PANE IS FOCUSED. This catches the
    'leader letter is unreachable over a focused pane' bug: the toggle must be a
    priority Binding, not a leader letter."""
    try:
        from textual.app import App  # noqa: F401
    except Exception:
        print("SKIP test_pilot_mirror_control_toggle (textual unavailable)")
        return

    import asyncio
    from textual.app import App

    _write_demo_session()
    facts: dict = {}

    class _StubHub:
        def __init__(self):
            self.calls = []
        def set_control_state(self, enabled, target=None):
            self.calls.append((enabled, target))
        # on_mount also wires these; provide no-op stand-ins.
        def set_size(self, *a):
            pass
        def set_repaint_request(self, *a):
            pass
        def set_input_handler(self, *a):
            pass
        def url(self):
            return "http://127.0.0.1:0/?token=x"

    def fake_run(self, *a, **kw):
        async def go():
            self._mirror_hub = _StubHub()
            async with self.run_test(size=(110, 30)) as pilot:
                await pilot.pause(0.4)
                facts["start_enabled"] = self._control_enabled
                # Simulate a focused live pane: a stub the binding will read for
                # its target title. (_focused_terminal is overridden so we don't
                # need a real PTY.)
                class _T:
                    title = "Demo session"
                self._focused_terminal = lambda: _T()
                await pilot.press("shift+f12")        # the priority toggle
                await pilot.pause(0.2)
                facts["after_enabled"] = self._control_enabled
                facts["hub_calls"] = list(self._mirror_hub.calls)
                await pilot.press("shift+f12")        # toggle back off
                await pilot.pause(0.2)
                facts["after_off"] = self._control_enabled
                facts["hub_calls2"] = list(self._mirror_hub.calls)
        asyncio.run(go())

    orig_run, App.run = App.run, fake_run
    orig_argv = sys.argv
    try:
        sys.argv = ["saikai", "--all"]
        saikai.main()
    finally:
        App.run = orig_run
        sys.argv = orig_argv

    assert facts.get("start_enabled") is False, facts
    assert facts.get("after_enabled") is True, f"toggle did not enable: {facts}"
    assert facts.get("hub_calls") == [(True, "Demo session")], facts
    assert facts.get("after_off") is False, f"toggle did not disable: {facts}"
    assert facts.get("hub_calls2")[-1] == (False, None), facts
```

Register in `__main__` (with its `print`):

```python
    test_pilot_mirror_control_toggle()
    print("PASS test_pilot_mirror_control_toggle")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python tests/test_keyboard_leader.py`
Expected: FAIL — there is no `shift+f12` binding / `action_toggle_mirror_control`, so `_control_enabled` stays `False`; `assert facts.get("after_enabled") is True` fails. (On a machine without textual it prints `SKIP …`; run where textual is installed to get the real RED.)

- [ ] **Step 3: Write minimal implementation**

Add the binding to `PickerApp.BINDINGS` — right after the `f12` mirror_info line (`saikai.py:3470`):

```python
            Binding("f12", "mirror_info", "Mirror QR", id="mirror_info", show=False),
            # Phase B: toggle web-mirror INTERACTIVE control. priority=True so it
            # fires even while a live pane is focused (a leader letter would be
            # swallowed by the focused pane — unreachable exactly when control is
            # used). Default OFF; Shift+F12 because F12 is the QR. Local only —
            # never a browser button.
            Binding("shift+f12", "toggle_mirror_control", "Mirror control",
                    id="mirror_control", show=False, priority=True),
```

Add `action_toggle_mirror_control` right after `action_mirror_info` (`saikai.py:5905`):

```python
        def action_toggle_mirror_control(self) -> None:
            """Shift+F12 — flip web-mirror interactive control (default OFF). The
            app's _control_enabled is the authority; push the new state + the
            focused-pane title (read HERE on the UI thread) into the hub, which
            keeps an advisory copy and broadcasts a control frame. No-op when the
            mirror is off."""
            _hub = getattr(self, "_mirror_hub", None)
            if _hub is None:
                return
            self._control_enabled = not self._control_enabled
            t = self._focused_terminal()
            target = (getattr(t, "title", None) if t is not None else None)
            try:
                _hub.set_control_state(self._control_enabled, target)
            except Exception:
                pass
            if self._control_enabled:
                msg = (f"Mirror control ON — typing into: {target}" if target
                       else "Mirror control ON — no pane focused")
                self.notify(msg, title="saikai mirror", severity="warning",
                            timeout=6)
            else:
                self.notify("Mirror control OFF (read-only)",
                            title="saikai mirror", timeout=4)
```

Wire `set_input_handler` in `on_mount` — inside the existing `if _hub is not None:` block, right after the `set_repaint_request(...)` call (`saikai.py:3689`):

```python
                _hub.set_repaint_request(
                    lambda: self.call_from_thread(self.refresh, layout=True))
                # Phase B: deliver browser input to the focused pane. The handler
                # is _marshal-shaped — capture the app, bail if it's gone, marshal
                # onto the UI thread, and swallow shutdown errors. NEVER a bare
                # call_from_thread (whose future.result() could block the
                # input-drain/HTTP thread forever during teardown).
                _app_ref = self
                def _inject_handler(d, _app=_app_ref):
                    if not getattr(_app, "is_running", False):
                        return
                    try:
                        _app.call_from_thread(_app._mirror_inject_input, d)
                    except Exception:
                        pass            # app tearing down between the guard + call
                _hub.set_input_handler(_inject_handler)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run python tests/test_keyboard_leader.py`
Expected: the existing `PASS …` lines plus `PASS test_pilot_mirror_control_toggle` (toggle flips `_control_enabled` and calls the stub hub `set_control_state` with `(True,"Demo session")` then `(False, None)`).

- [ ] **Step 5: Commit**

```
git add saikai.py tests/test_keyboard_leader.py && git commit -m "$(cat <<'EOF'
feat(mirror): focus-independent control toggle + on_mount input wiring

Shift+F12 priority Binding -> action_toggle_mirror_control flips the
authoritative gate, reads the focused-pane title on the UI thread, and pushes
set_control_state (advisory copy + control frame). on_mount wires a
_marshal-shaped input handler (guard app, marshal, swallow) — never a bare
call_from_thread. Pilot test proves reachability over a focused pane.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 9: Launch LAN-input opt-in gate

Gate input enablement behind `SAIKAI_MIRROR_ALLOW_LAN_INPUT=1` when the read mirror is LAN-exposed; loopback always allows input. Set `allow_lan_input` on the hub and have `set_control_state` refuse to enable control for a non-loopback bind when input is not opted in. Extend the banner.

**Files:**
- Modify: `saikai_mirror.py` (`MirrorHub`: `_host_is_loopback()`, gate in `set_control_state`); `saikai.py` launch block (`:5969-6007`)
- Test: `tests/test_mirror_input.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_mirror_input.py`:

```python
def test_lan_input_requires_opt_in():
    """A LAN-exposed mirror (non-loopback host) refuses to ENABLE control unless
    allow_lan_input was opted in; loopback always allows it."""
    # Loopback: control may enable freely.
    lo = m.MirrorHub(token="t", host="127.0.0.1", port=0)
    lo.set_input_handler(lambda d: None)
    lo.set_control_state(True, "S")
    assert lo._control_enabled is True, "loopback control must enable"

    # LAN bind, NOT opted in: enabling control is refused (stays OFF).
    lan = m.MirrorHub(token="t", host="192.168.1.50", port=0)
    lan.set_input_handler(lambda d: None)
    lan.allow_lan_input = False
    lan.set_control_state(True, "S")
    assert lan._control_enabled is False, "LAN input must require opt-in"

    # LAN bind, opted in: enabling control is allowed.
    lan2 = m.MirrorHub(token="t", host="192.168.1.50", port=0)
    lan2.set_input_handler(lambda d: None)
    lan2.allow_lan_input = True
    lan2.set_control_state(True, "S")
    assert lan2._control_enabled is True, "opted-in LAN control must enable"
```

Register in `__main__`:

```python
    test_lan_input_requires_opt_in()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python tests/test_mirror_input.py`
Expected: FAIL — `set_control_state` currently enables regardless of host; `assert lan._control_enabled is False` fails (it is True).

- [ ] **Step 3: Write minimal implementation**

Add `_host_is_loopback` to `MirrorHub` (near `url()` at `saikai_mirror.py:239`):

```python
    def _host_is_loopback(self) -> bool:
        return self._host in ("127.0.0.1", "localhost", "::1", "")
```

Gate the enable in `set_control_state` — at the very top, before storing state:

```python
    def set_control_state(self, enabled: bool, target=None) -> None:
        # LAN input is opt-in: a non-loopback bind cannot ENABLE control unless
        # allow_lan_input was set at launch. Disabling is always honored.
        if enabled and not self._host_is_loopback() and not self.allow_lan_input:
            enabled = False
            target = None
        self._control_enabled = bool(enabled)
        self._control_target = target if enabled else None
        # … (unchanged broadcast + idle-timer arm/cancel from Tasks 5/6) …
```

Wire the launch opt-in in `saikai.py` — inside the `if _mir_on:` block, right after the hub is built and before/around `_hub.serve()` (`saikai.py:5975-5978`):

```python
                if _mir_on:
                    _hub = _mirror.MirrorHub(
                        token=_secrets.token_urlsafe(32), host=_mir_host,
                        port=_mirror.mirror_port(os.environ))
                    # LAN input is its own opt-in: a LAN-exposed mirror stays
                    # read-only unless SAIKAI_MIRROR_ALLOW_LAN_INPUT=1. Loopback
                    # always permits input.
                    _allow_lan_in = str(os.environ.get(
                        "SAIKAI_MIRROR_ALLOW_LAN_INPUT", "")).strip().lower() in (
                        "1", "true", "yes", "on")
                    _hub.allow_lan_input = _allow_lan_in
                    _hub.serve()
```

Extend the banner to state the input mode — replace the `_mode` line and the banner print (`saikai.py:5982`, `:5997`):

```python
                    _mode = "LAN-exposed" if _mir_host != "127.0.0.1" else "loopback only"
                    _in_mode = ("input ON" if (_mir_host == "127.0.0.1" or _allow_lan_in)
                                else "input OFF (set SAIKAI_MIRROR_ALLOW_LAN_INPUT=1)")
```

```python
                    print(_c(f"  ⚠ saikai mirror LIVE ({_mode}, {_in_mode}; "
                             f"control default OFF, Shift+F12): {_hub.url()}",
                             YELLOW), file=sys.stderr)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run python tests/test_mirror_input.py`
Expected: `OK test_mirror_input` (loopback enables; un-opted LAN stays OFF; opted-in LAN enables).

- [ ] **Step 5: Commit**

```
git add saikai_mirror.py saikai.py tests/test_mirror_input.py && git commit -m "$(cat <<'EOF'
feat(mirror): gate LAN input behind SAIKAI_MIRROR_ALLOW_LAN_INPUT

set_control_state refuses to enable control on a non-loopback bind unless
allow_lan_input is set; loopback always allows. Launch resolves the env opt-in
onto the hub and the banner shows the input mode + the Shift+F12 control hint.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 10: Browser/JS — onData → coalesce → single-flight POST + listeners + banner

Add the client side: `writekey`/`control` listeners, an `onData` coalescer that flushes on control bytes, a single-flight FIFO POST sender with `X-Mirror-Write-Key`, a CONTROL ON/OFF banner with "typing into: ⟨target⟩", input disabled until a `control` on-frame, and the 409/403/400/413 client reactions. No browser in CI, so the test string-asserts the page contains the listeners/handlers (like `test_mirror_hub.py` asserts page contents); manual phone verification is noted.

**Files:**
- Modify: `saikai_mirror.py` (`_PAGE_HTML` JS at `:315-337`)
- Test: `tests/test_mirror_input.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_mirror_input.py`:

```python
def test_page_contains_input_listeners_and_sender():
    """No browser in CI: assert the served page wires the writekey/control SSE
    listeners, the onData single-flight POST sender (with the write-key header),
    coalescing/flush-on-control-byte, the CONTROL banner, and the 409/403
    reactions. Manual phone verification covers actual keystroke fidelity."""
    hub = m.MirrorHub(token="secret", host="127.0.0.1", port=0, cols=80, rows=24)
    hub.set_input_handler(lambda d: None)
    port = hub.serve()
    try:
        page = urllib.request.urlopen(
            f"http://127.0.0.1:{port}/?token=secret", timeout=3.0
        ).read().decode("utf-8")
        # SSE named-event listeners (NOT onmessage) for writekey + control.
        assert "addEventListener('writekey'" in page, page
        assert "addEventListener('control'" in page, page
        # Input capture + single-flight POST to /input with the write-key header.
        assert "term.onData" in page, page
        assert "/input" in page and "X-Mirror-Write-Key" in page, page
        # Coalescing + flush on control bytes (ESC / CR / <0x20).
        assert "0x20" in page or "charCodeAt(0) < 32" in page, page
        # CONTROL banner + target + disabled-until-on.
        assert "CONTROL ON" in page and "CONTROL OFF" in page, page
        assert "typing into" in page, page
        # Client reactions to the server gate.
        assert "409" in page and "403" in page, page
        # The output path is untouched (still base64 via onmessage).
        assert "es.onmessage" in page and "atob" in page, page
    finally:
        hub.stop()
```

Register in `__main__`:

```python
    test_page_contains_input_listeners_and_sender()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python tests/test_mirror_input.py`
Expected: FAIL — today's `_PAGE_HTML` has no `addEventListener('writekey'`/`onData`/`X-Mirror-Write-Key`; the first listener assertion fails.

- [ ] **Step 3: Write minimal implementation**

Replace the `<script>` block at the end of `_PAGE_HTML` (`saikai_mirror.py:322-337`, from `const term = …` through `</script></body></html>`) with:

```python
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
  for (let i=0;i<d.length;i++) { if (d.charCodeAt(0) < 32 || d.charCodeAt(i) === 0x1b) return true; }
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run python tests/test_mirror_input.py`
Expected: `OK test_mirror_input` (page string assertions green). **Manual phone verification (no browser in CI):** with `SAIKAI_MIRROR=1` (loopback) launch saikai, open the URL on a phone, press Shift+F12 on the laptop, confirm the banner flips to CONTROL ON with the pane title, type `echo hi` + Enter and a Ctrl-C and confirm they land in the focused claude pane in order; toggle off and confirm the banner flips and typing is ignored.

- [ ] **Step 5: Commit**

```
git add saikai_mirror.py tests/test_mirror_input.py && git commit -m "$(cat <<'EOF'
feat(mirror): browser input — onData coalescer + single-flight POST + banner

writekey/control SSE listeners; onData coalesces ~25ms but flushes on ESC/CR/C0
bytes; one in-flight POST /input at a time carrying X-Mirror-Write-Key; CONTROL
ON/OFF banner with the target title; input disabled until a control on-frame;
409 -> off, 403 -> fatal, 400/413 -> drop+continue. Output path unchanged.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 11: Run the full suite

Confirm every affected test script passes — the new input suite, the extended app-object + Pilot suites, and the existing read-only mirror suite (regression: output + ThreadingMixIn coexistence still green).

**Files:** none (verification only).

- [ ] **Step 1: Run the new + extended suites**

```
uv run python tests/test_mirror_input.py
uv run python tests/test_terminal_concurrency.py
uv run python tests/test_keyboard_leader.py
```

Expected: `OK test_mirror_input`; the full `PASS …` list from `test_terminal_concurrency.py` including the three `_mirror_inject_input` lines; the full `PASS …` list from `test_keyboard_leader.py` ending with `ALL PASS` and including `PASS test_pilot_mirror_control_toggle` (or `SKIP …` if textual is unavailable on this host).

- [ ] **Step 2: Run the existing mirror regression suite**

```
uv run python tests/test_mirror_hub.py
uv run python tests/test_mirror_driver.py
uv run python tests/test_mirror_snapshot.py
```

Expected: `OK test_mirror_hub` (output streaming + bad-token reject + ThreadingMixIn behaviors unchanged) and the driver/snapshot suites' usual pass prints.

- [ ] **Step 3: Confirm and report**

All of the above print their `OK …` / `PASS …` / `ALL PASS` lines with no `AssertionError`/traceback. If any `SKIP` appears for the Pilot test, re-run on a host with `textual` installed before declaring Phase B done (the reachability bug is invisible under SKIP).

- [ ] **Step 4 (optional): squash/verify the branch**

Do not push. Leave the commits on `feat/web-mirror-interactive` for review per the project's git workflow (push only on explicit request).

---

## Invariants honored (cross-checked against `docs/ARCHITECTURE.md` and the spec)

- **Never marshal while holding `self._lock`.** `_mirror_inject_input` takes no lock; `_pty.write` runs on the UI thread exactly like `on_key`. The input handler marshals onto the UI thread and is `_marshal`-shaped (guard app, `call_from_thread`, swallow), never holding any hub lock during the marshal.
- **Never close a POSIX `ptyprocess` on the UI thread.** Phase B only ever *writes* to `_pty`; it never calls `close()`/`kill()`. Teardown (`kill()` setting `_pty=None`) and `_mirror_inject_input` both run on the UI thread, so they cannot interleave; the `_pty is None` / `is_dead` guard + try/except matches `on_key`.
- **`_pty.write` takes `str`, no lock.** Confirmed against `saikai_terminal.py:852,902`; no `.encode()` is added — a hostile lone-surrogate `UnicodeEncodeError` is contained by the try/except.
- **Fire-and-forget injection.** The drain worker (`_inject_loop`) and the `_marshal`-shaped handler never call `future.result()`, so a shutdown can't hang the drain/HTTP thread.
- **In-order delivery** = one in-flight client POST (the JS single-flight `sending` flag) + the single server `_inject_loop` drain. ThreadingMixIn's independent POST threads never reorder because they all enqueue onto the one `_inject_q`.
- **Gate authority** = `PickerApp._control_enabled` re-checked on the UI thread; the hub copy is advisory (GIL-atomic bool read, same rationale as `set_repaint_request`).
- **Read-only output path is untouched** — `broadcast`/`_drain_loop`/`_send_frame`/`onmessage` are unchanged; control/writekey ride named SSE events that bypass `onmessage`.
