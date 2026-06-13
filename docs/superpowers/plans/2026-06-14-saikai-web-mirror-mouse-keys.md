# saikai Web Mirror — Phase C (Tap + Keys) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a phone drive saikai's *own* Textual UI from the mirror — tap to click (select a row, sort a header, focus a pane), swipe to scroll, and an on-screen key bar (Leader, Esc, Tab, arrows, Ctrl-sticky, F12) — by enqueuing **typed** items onto the existing single inject FIFO and posting synthesized Textual events (`MouseDown`/`MouseUp`/`MouseScroll*`/`Key`) into `PickerApp` on the UI thread, all behind the unchanged Phase B control gate.

**Architecture:** `MirrorHub` keeps ONE FIFO `_inject_q` but its items become typed tuples (`("input", str)`, `("mouse", col, row, button, kind)`, `("key", key)`); the single `_inject_loop` dispatches each by tag to `_input_handler` / `_mouse_handler` / `_key_handler`, preserving global submission order across keyboard/mouse/key. New `do_POST` routes `/mouse` and `/key` reuse the Phase B gate verbatim (Host allow-list → write-key → Origin → advisory control-on) and call `inject_mouse` / `inject_key`. On the app, `_MirrorControl._mirror_inject_mouse` / `_mirror_inject_key` re-check the authoritative `_control_enabled` on the UI thread and `post_message(...)` the synthesized events; `App.on_event` hit-tests `get_widget_at` and synthesizes the `Click` itself, so DataTable header-sort / row-selection / pane-focus all dispatch natively. Injection is marshaled onto the UI thread by guarded `call_from_thread` closures wired in `on_mount` next to the Phase B input handler.

**Tech Stack:** Python stdlib `http.server` + `socketserver.ThreadingMixIn` (the existing `_Server`) + `queue` + `json` + `threading`; `pyte` (output mirror, unchanged); vendored `xterm.js` + canvas addon (browser, SGR mouse already emitted by xterm when mouse tracking is on); Textual 8.2.7 events (`events.MouseDown`/`MouseUp`/`MouseScrollUp`/`MouseScrollDown`/`events.Key`, `App.post_message`, `App.call_from_thread`). **No new Python or JS dependency.**

---

## Scope

**In scope:** tap → click (`MouseDown`+`MouseUp` → the App synthesizes the `Click`); swipe → scroll (`MouseScrollUp`/`MouseScrollDown`); an on-screen key bar (Leader, Esc, Tab, ↑ ↓ ← →, Ctrl-sticky modifier, F12) → `events.Key`. All injected into saikai's app on the UI thread, behind the existing default-OFF control gate.

**Out of scope (deferred):** divider-drag (`MouseMove` sequences — none are injected); arbitrary full-keyboard remap; routing plain typed text through the app (text keeps the Phase B pane-PTY `/input` path); a more robust control-toggle key (Phase B follow-up).

## Verified codebase facts (do not re-derive)

Read these once; every task below is consistent with them. Where the task summary and the real code differed, the **real code wins** and the divergence is noted inline.

### `saikai_mirror.py` (post-Phase-B, the file as it stands on `feat/web-mirror-interactive`)

- `class MirrorHub` — `saikai_mirror.py:126`. `__init__(self, token, host="127.0.0.1", port=0, cols=80, rows=24, ingest_cap=256, idle_secs=600.0)`. Phase B fields already present (do **not** remove): `_control_enabled = False` (`:147`), `_input_handler = None` (`:148`), `_control_target = None` (`:149`), `_write_key = _secrets.token_urlsafe(32)` (`:152`), `_inject_q: queue.Queue[str] = queue.Queue(1024)` (`:153`), `_inject_drain = None` (`:154`), idle state `_idle_secs`/`_idle_timer`/`_idle_lock` (`:156-158`), `_bad_key_count`/`_last_accept_t`/`_min_accept_gap` (`:159-161`), `allow_lan_input = False` (`:162`).
- `MirrorHub.inject(self, data: str) -> bool` — `saikai_mirror.py:325-343`. EXACT current body (this is what Task 1 generalizes — Phase B keyboard must keep working):
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
          return False           # bounded; refuse rather than block a handler
      self._last_accept_t = now
      self._arm_idle_timer()                 # activity keeps control alive
      return True
  ```
  **The gate (`_input_handler is None or not _control_enabled`) keys off `_input_handler` specifically.** Task 1 generalizes the readiness check so a `/key` or `/mouse` POST is not refused merely because the keyboard handler is unset (in practice all three are wired together in `on_mount`, but the test wires only one) — see Task 1.
- `MirrorHub._inject_loop(self)` — `saikai_mirror.py:345-360`. EXACT current body:
  ```python
  def _inject_loop(self):
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
  **Copy this poll-with-`timeout=0.25`-against-`_stopped` shape** for the generalized loop so `stop()` still unblocks it. Task 1 changes only the *dispatch* (tagged tuple → the matching handler), not the loop skeleton.
- `MirrorHub.set_input_handler(self, fn)` — `saikai_mirror.py:270-274`: single-attribute assign, written on the UI thread (`on_mount`), read on the inject-drain thread; GIL-atomic. **`set_mouse_handler` / `set_key_handler` mirror this exact pattern** (one assignment each).
- `MirrorHub.serve()` — `saikai_mirror.py:230-243`: starts the HTTP thread, the output drain thread, AND the inject drain thread (`self._inject_drain = threading.Thread(target=self._inject_loop, name="saikai-mirror-inject", daemon=True); self._inject_drain.start()`), returns the bound port. **No change needed** — the same single thread drains the now-typed queue.
- `MirrorHub.stop()` — `saikai_mirror.py:245-258`: sets `_stopped`, cancels the idle timer, sends `None` sentinel to client queues, shuts the HTTP server. The inject drain exits on `_stopped` (daemon); no extra teardown.
- `MirrorHub.set_control_state(self, enabled, target=None)` — `saikai_mirror.py:276-303`: LAN-opt-in normalization, stores advisory `_control_enabled`/`_control_target`, broadcasts an `event: control` frame, arms/cancels the idle timer. **Unchanged** — mouse + keys gate on the same advisory copy.
- `_Handler(http.server.BaseHTTPRequestHandler)` — `saikai_mirror.py:540`. `protocol_version = "HTTP/1.1"` (`:545`).
  - Gate helpers (reuse verbatim): `_host_ok()` (`:577-579`, returns `self.headers.get("Host","") in self._allowed_hosts()`), `_write_key_ok()` (`:553-558`, `hmac.compare_digest` + increments `_bad_key_count` on miss), `_origin_ok()` (`:585-598`, fail-closed Origin/Referer exact-match).
  - Body helpers (reuse verbatim): `_INPUT_CAP = 65536` (`:695`), `_drain_body()` (`:697-709`), `_reject(code, msg, drain=True)` (`:711-719`), `_send_status(code)` (`:770-773`, sends the status line + `Content-Length: 0`).
  - `do_POST` — `saikai_mirror.py:721-768`. EXACT current shape (this is the template Tasks 2 & 3 extend — note it currently rejects every path except `/input` with 405 at the top):
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
            self._reject(413, "payload too large", drain=False)
            return
        raw = self.rfile.read(length) if length else b""
        try:
            obj = json.loads(raw.decode("utf-8")) if raw else {}
        except (ValueError, UnicodeDecodeError):
            self.send_error(400, "bad json")
            return
        data = obj.get("data") if isinstance(obj, dict) else None
        if data is None or not isinstance(data, str):
            self.send_error(400, "missing data")
            return
        if data == "":
            self._send_status(204)
            return
        if not hub._control_enabled:
            self.send_error(409, "control off")
            return
        hub.inject(data)
        self._send_status(204)
    ```
    **DIVERGENCE FROM SUMMARY:** the summary says do_POST "routes `/mouse` and `/key`". The real code hard-rejects anything but `/input` on line 2. Tasks 2 & 3 replace the `if path != "/input": ... 405` head with a **shared gate + body prelude** that runs for `/input`, `/mouse`, `/key` and 405s only truly-unknown paths. The plan refactors the common prefix (host/key/origin/chunked/length/cap/json parse) into a helper `_post_gate_and_json()` so all three routes share one verbatim gate (no copy-drift across the three). See Task 2.
- `_PAGE_HTML` — `saikai_mirror.py:441-537`. Vendored xterm.js + canvas addon; `term = new Terminal({cols: __COLS__, rows: __ROWS__, scrollback:0, convertEol:false})`; `es.onmessage` does `atob` → bytes → `term.write` (output path — **untouched**). Phase B JS present: `writeKey`/`controlOn` vars, `setBanner`, `es.addEventListener('writekey',…)` (`:487`) + `es.addEventListener('control',…)` (`:490`), the coalescing single-flight `pump()` + `term.onData` (`:531-536`) POSTing `/input` with the `X-Mirror-Write-Key` header, the `409`→banner-off / `403`→fatal reactions (`:520-523`). **Phase C edits `onData` to split SGR-mouse → `/mouse` from keyboard → `/input`, adds a `postKey`/`postMouse` single-flight pair, and appends the on-screen key bar.** The base64 `onmessage` output path stays byte-identical.
- The served page must contain **no raw C0 control byte except TAB/LF** — `tests/test_mirror_input.py::test_page_has_no_js_breaking_control_bytes` enforces it (a literal CR once ended a `//` comment early → blank page). Write all new JS escapes as `\\x1b` in the Python string (the SGR-mouse prefix `\x1b[<` must be the two-char source `\\x1b[<`, never a literal ESC).
- `_Server(socketserver.ThreadingMixIn, http.server.HTTPServer)` — `saikai_mirror.py:776`: `daemon_threads = True`; `allow_reuse_address = (sys.platform != "win32")`. ThreadingMixIn already present, so SSE + POSTs run on independent threads — which is exactly why the single server-side inject drain (not the handler thread) owns ordering.

### `saikai.py`

- `class _MirrorControl:` — `saikai.py:2920-2950`, **module scope** (PickerApp is defined inside `textual_pick` and inherits it, so the mixin must stay textual-free at import time for headless `__new__` tests). Has `_control_enabled: bool = False` (`:2932`, the AUTHORITATIVE gate) and `_mirror_inject_input(self, data)` (`:2934-2950`). EXACT current `_mirror_inject_input` (the guard shape Phase C copies):
  ```python
  def _mirror_inject_input(self, data: str) -> None:
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
  **Add `_mirror_inject_mouse` and `_mirror_inject_key` to THIS mixin** (Tasks 4 & 5), each re-checking `self._control_enabled` first, importing `events` function-locally (see next bullet), and `self.post_message(...)`.
- **No module-level `from textual import events`.** All textual imports in this file are **function-local** inside `textual_pick` — `from textual.app import App, ComposeResult` etc. at `saikai.py:2983-2992`. A `grep -n "events\." saikai.py` and `grep "from textual import events"` both return NOTHING. **Therefore the new mixin methods MUST `from textual import events` inside the method body** (not at module top, or the headless import of `_MirrorControl` would pull textual). The headless tests build the app via `__new__` and call the method; textual IS installed in `.venv`, so the in-method import succeeds — but the class definition stays import-safe.
- `class PickerApp(App, _MirrorControl):` — `saikai.py:3433`. `BINDINGS` — `saikai.py:3439-3511`. Context for the key bar: the leader is `Binding("space", "arm_leader", "Menu", key_display="␣")` (`:3457`); `Binding("f12", "mirror_info", "Mirror QR", id="mirror_info", show=False)` (`:3503`, **not** priority); `Binding("shift+f12", "toggle_mirror_control", …, priority=True)` (`:3509-3510`, the Phase B control toggle — local only, never a browser button). Many actions are priority bindings (`enter`/`tab`/`question_mark`/the F-keys) so they fire even while a pane is focused. The key bar posts `events.Key` whose `key` strings match Textual binding keys (`"escape"`, `"tab"`, `"up"`, `"down"`, `"left"`, `"right"`, `"f12"`, `"space"`, and `ctrl+`-prefixed combos).
- `on_mount` mirror-wiring block — `saikai.py:3725-3756`, inside `_hub = getattr(self, "_mirror_hub", None); if _hub is not None:`. The Phase B input handler is wired as a guarded `_marshal`-shaped closure (`saikai.py:3735-3743`):
  ```python
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
  **Task 6 adds two more closures of the SAME shape next to this** — one for `set_mouse_handler` (forwards `(col,row,button,kind)`), one for `set_key_handler` (forwards `(key,)`) — each guarded by `is_running` and wrapped in try/except. NEVER a bare `call_from_thread` (its `future.result()` could block the inject-drain thread forever during teardown).
- `_focused_terminal(self)` — `saikai.py:4768-4780`: returns the focused live `AgentTerminal` or `None`; a DEAD pane returns `None`. UI-thread only. (Not needed by mouse/key injection — `post_message` goes to the App, which hit-tests — but kept here for context.)
- `action_toggle_mirror_control(self)` — `saikai.py:5961-5986`: the Shift+F12 priority toggle; flips `self._control_enabled` and pushes `_hub.set_control_state(...)`. Unchanged by Phase C.

### Verified Textual 8.2.7 event signatures (quoted from `.venv/Lib/site-packages/textual/events.py`)

- `events.MouseEvent.__init__` — `events.py:367-381`:
  ```python
  def __init__(self, widget, x, y, delta_x, delta_y, button, shift, meta, ctrl,
               screen_x=None, screen_y=None, style=None) -> None:
  ```
  Body (`:382-405`): stores `self._x = x`, `self._y = y`, `self._screen_x = x if screen_x is None else screen_x`, `self._screen_y = y if screen_y is None else screen_y`. Properties `x`/`y` return `int(self._x)`/`int(self._y)`; `screen_x`/`screen_y` return `int(self._screen_x)`/`int(self._screen_y)` (`:407-435`).
- `MouseDown` (`:581`), `MouseUp` (`:590`), `MouseScrollDown` (`:599`), `MouseScrollUp` (`:608`) are **bare subclasses** of `MouseEvent` with NO extra `__init__` — they share the signature above exactly.
- **DIVERGENCE FROM SUMMARY (must fix):** the spec writes `events.MouseDown(None, x=col, y=row, 0, 0, button, …)` — that is **invalid Python** (a positional arg `0` follows keyword args `x=`/`y=`). Construct positionally:
  ```python
  events.MouseDown(None, col, row, 0, 0, button, False, False, False, screen_x=col, screen_y=row)
  events.MouseUp(None, col, row, 0, 0, button, False, False, False, screen_x=col, screen_y=row)
  events.MouseScrollUp(None, col, row, 0, 0, 0, False, False, False, screen_x=col, screen_y=row)
  events.MouseScrollDown(None, col, row, 0, 0, 0, False, False, False, screen_x=col, screen_y=row)
  ```
  (`widget=None`; `delta_x=delta_y=0`; click button = the SGR button index, scroll button = `0`; `shift/meta/ctrl=False`; `screen_x/screen_y` = the same cell coords because the App reads `event.x`/`event.y` for the hit-test and `screen_offset` for the up-widget match.)
- **0-based coordinate convention CONFIRMED:** `App.on_event` (`app.py:4069-4082`) sets `self.mouse_position = Offset(event.x, event.y)` then `self.get_widget_at(event.x, event.y)` for `MouseDown`. `get_widget_at` takes 0-based screen cells (top-left = `(0,0)`). xterm SGR reports **1-based** col/row → subtract 1 in the browser before POSTing (Task 8). The hub passes coords through unchanged; the App receives 0-based.
- **Click synthesis CONFIRMED:** `App.on_event` (`app.py:4073-4119`): on `MouseDown` it records `self._mouse_down_widget, _ = self.get_widget_at(event.x, event.y)`; it `self.screen._forward_event(event)`; then on a `MouseUp` whose `get_widget_at(*event.screen_offset)` is the same widget, it builds `click_event = events.Click.from_event(mouse_down_widget, event, chain=…)` and forwards it. So posting `MouseDown` then `MouseUp` at the same cell yields a native `Click` → DataTable header-sort / row cursor / pane focus, **for free**. We never construct `Click` ourselves.
- `events.Key.__init__` — `events.py:274-281`:
  ```python
  def __init__(self, key: str, character: str | None) -> None:
      super().__init__()
      self.key = key
      self.character = ((key if len(key) == 1 else None) if character is None else character)
  ```
  **`character` is a required positional.** For a printable single char pass `events.Key("space", " ")`; for a non-printable named key pass `character=None` (`events.Key("escape", None)`, `events.Key("up", None)`, `events.Key("ctrl+c", None)`). When `character is None` and `len(key)==1`, Textual auto-fills `character=key`. The App routes a `Key` to priority bindings / the focused widget (the same path `Pilot.press` uses).
- `events.Click.__init__(..., chain=1)` — `events.py:644-674`; synthesized by the App via `Click.from_event`, never by us.

### Test harness facts

- **This repo does NOT use pytest.** Tests are plain module-level `def test_*()` functions + an `if __name__ == "__main__":` runner that calls each and prints `PASS …` / `OK …`. Run a suite with `uv run python tests/test_<name>.py`. RED = an `AssertionError`/traceback when run; GREEN = the `OK`/`PASS` print.
- `tests/test_mirror_input.py` — real `MirrorHub.serve()` on `127.0.0.1`; `_post(port, path, body=None, headers=None, raw=None)` (urllib, same-origin Host+Origin) returns `(status, text)`; `_raw_request(port, method, path, headers)` (raw socket, exact headers, supports `_body`) returns the numeric status; page string-asserts via `urllib.request.urlopen(.../?token=…).read()`. Its `__main__` runner lists every `test_*` call then `print("OK test_mirror_input")` — **append new calls there.**
- `tests/test_terminal_concurrency.py` — app-object invariant tests via `__new__` + `FakePty`/stub recorders (no real PTY, no textual run loop). **The Task 4 & 5 app tests live in `tests/test_mirror_input.py`** instead (the spec's Testing section groups "App: `_mirror_inject_*` … via `__new__` + a stub recording `post_message`" with the hub tests, and `_MirrorControl` is import-safe headless) — building `saikai.PickerApp`-free by instantiating the bare mixin: `app = saikai._MirrorControl.__new__(saikai._MirrorControl)` then attach a `post_message` recorder. *Rationale:* the mixin is the unit under test, it imports without textual, and keeping these next to the hub gate tests matches how `test_mirror_input.py` already owns all Phase B gate + page tests. (If a reviewer prefers them in `test_terminal_concurrency.py`, they are mechanically movable — same `__new__` style — but this plan puts them in `test_mirror_input.py`.)
- `tests/test_keyboard_leader.py` — skip-guarded Textual Pilot: monkeypatch `App.run` with a `fake_run` that opens `self.run_test(size=(110,30))` and drives `pilot`, then `saikai.main()`. The existing `test_pilot_mirror_control_toggle` (`:368-435`) shows the `_StubHub` + `_write_demo_session()` pattern. **Task 7 adds one skip-guarded Pilot test here** driving the REAL `PickerApp` with synthesized `post_message`.
- Local git identity is already `m-morino` — **do not** set git identity in any command. Stay on `feat/web-mirror-interactive` (do NOT branch).

## File Structure

- **Modify `saikai_mirror.py`** — `MirrorHub`: add `_mouse_handler=None`, `_key_handler=None`; `set_mouse_handler(fn)`, `set_key_handler(fn)`; generalize `inject()` readiness + add `inject_mouse(col,row,button,kind)` / `inject_key(key)` (all enqueue tagged tuples onto the existing `_inject_q`); generalize `_inject_loop` to dispatch by tag. `_Handler`: refactor the `do_POST` common prefix into `_post_gate_and_json()`, add `/mouse` + `/key` routes. `_PAGE_HTML`/JS: split `onData` SGR-mouse → `/mouse`, add `postKey`/`postMouse` single-flight + the on-screen key bar.
- **Modify `saikai.py`** — `_MirrorControl`: add `_mirror_inject_mouse(col,row,button,kind)` and `_mirror_inject_key(key)` (function-local `from textual import events`, UI-thread `post_message`, re-check `_control_enabled`). `on_mount` (inside `if _hub is not None`): wire `_hub.set_mouse_handler(...)` + `_hub.set_key_handler(...)` as guarded `_marshal`-shaped closures next to the Phase B input handler.
- **Modify `tests/test_mirror_input.py`** — hub typed-dispatch + ordering test; `/mouse` + `/key` gate/status/coord tests; `_mirror_inject_mouse` / `_mirror_inject_key` double-gate `__new__`+stub tests; extended page string-asserts.
- **Modify `tests/test_keyboard_leader.py`** — one skip-guarded Pilot test: synthesized click on a header fires the sort / selects a row, and a synthesized `events.Key` fires a priority binding, driving the REAL `PickerApp`.
- **No new files.** No change to `saikai_terminal.py` or `tests/test_mirror_hub.py` (the latter is run unchanged in Task 9 as a Phase B regression).

---

### Task 1: Hub — typed inject queue + tagged dispatch (`inject_mouse`/`inject_key` + handlers)

Generalize the single FIFO so items carry a tag and the one drain dispatches by tag, while **Phase B keyboard inject stays byte-identical** (its two tests in `test_mirror_input.py` — `test_inject_gate_off_by_default_and_requires_handler`, `test_inject_is_fifo_via_single_drain` — must stay green). The keyboard path keeps enqueuing the *bare `str`* (those tests assert `hub._inject_q.get_nowait() == "b"` and `seen == ["a","b","c"]`), so the dispatcher treats a non-tuple item as `("input", item)`.

**Files:**
- Modify: `saikai_mirror.py` (`MirrorHub.__init__` ~`:147-162`; `inject` `:325-343`; `_inject_loop` `:345-360`; add `set_mouse_handler`/`set_key_handler` after `set_input_handler` ~`:274`; add `inject_mouse`/`inject_key` after `inject`)
- Test: `tests/test_mirror_input.py`

- [ ] **Step 1: Write the failing test** (append these two functions; add their calls to the `__main__` runner before `print("OK test_mirror_input")`):

```python
def test_typed_inject_dispatches_by_tag_in_order():
    """One FIFO drain dispatches keyboard/mouse/key items to the matching
    handler in global submission order. Bare-str keyboard items (Phase B) and
    tagged tuples interleave; ordering across handlers is preserved."""
    hub = m.MirrorHub(token="t")
    seen = []
    ev = threading.Event()

    def on_input(d): seen.append(("input", d))
    def on_mouse(col, row, button, kind):
        seen.append(("mouse", col, row, button, kind))
    def on_key(key):
        seen.append(("key", key))
        if len(seen) == 4:
            ev.set()

    hub.set_input_handler(on_input)
    hub.set_mouse_handler(on_mouse)
    hub.set_key_handler(on_key)
    hub._control_enabled = True
    hub.serve()
    try:
        assert hub.inject("a") is True                       # keyboard (bare str)
        assert hub.inject_mouse(5, 9, 0, "down") is True     # mouse
        assert hub.inject_mouse(5, 9, 0, "up") is True       # mouse
        assert hub.inject_key("escape") is True              # key
        assert ev.wait(timeout=3.0), f"drain did not deliver 4 items: {seen}"
        assert seen == [
            ("input", "a"),
            ("mouse", 5, 9, 0, "down"),
            ("mouse", 5, 9, 0, "up"),
            ("key", "escape"),
        ], seen
    finally:
        hub.stop()


def test_mouse_and_key_inject_gate_on_control_and_handler():
    """inject_mouse/inject_key refuse when control is OFF or no matching handler
    is wired, and accept (enqueue) when control is ON with the handler set."""
    hub = m.MirrorHub(token="t")
    # No handlers yet -> refuse even if enabled.
    hub._control_enabled = True
    assert hub.inject_mouse(1, 1, 0, "down") is False, "no mouse handler must refuse"
    assert hub.inject_key("tab") is False, "no key handler must refuse"
    assert hub._inject_q.empty(), "refused input must not be queued"
    hub.set_mouse_handler(lambda *a: None)
    hub.set_key_handler(lambda *a: None)
    # Control OFF -> refuse.
    hub._control_enabled = False
    assert hub.inject_mouse(1, 1, 0, "down") is False, "control OFF must refuse mouse"
    assert hub.inject_key("tab") is False, "control OFF must refuse key"
    assert hub._inject_q.empty()
    # Control ON + handlers -> accept (tagged tuple enqueued).
    hub._control_enabled = True
    assert hub.inject_mouse(2, 3, 0, "up") is True
    assert hub._inject_q.get_nowait() == ("mouse", 2, 3, 0, "up")
    assert hub.inject_key("f12") is True
    assert hub._inject_q.get_nowait() == ("key", "f12")
```

- [ ] **Step 2: Run test to verify it fails** — `uv run python tests/test_mirror_input.py`
  Expected: FAIL with `AttributeError: 'MirrorHub' object has no attribute 'set_mouse_handler'`.

- [ ] **Step 3: Write minimal implementation**

In `MirrorHub.__init__`, immediately after `self._input_handler = None` (`saikai_mirror.py:148`), add the two new handler fields:

```python
        self._input_handler = None             # _marshal-shaped, set at app mount
        self._mouse_handler = None             # _marshal-shaped, set at app mount
        self._key_handler = None               # _marshal-shaped, set at app mount
```

After `set_input_handler` (`saikai_mirror.py:270-274`), add:

```python
    def set_mouse_handler(self, fn) -> None:
        # Written from the UI thread (on_mount), read from the inject-drain
        # thread. Single attribute assignment/read is atomic under the GIL
        # (same rationale as set_input_handler).
        self._mouse_handler = fn

    def set_key_handler(self, fn) -> None:
        # Same GIL-atomic single-attribute pattern as set_input_handler.
        self._key_handler = fn
```

Replace `inject` (`saikai_mirror.py:325-343`) with a version whose readiness check no longer hard-requires the *keyboard* handler, plus a private `_enqueue` the typed injects share (so rate-cap + idle-arm logic lives in one place):

```python
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
```

Replace `_inject_loop` (`saikai_mirror.py:345-360`) with a tag-dispatching drain (same poll-with-`timeout=0.25`-against-`_stopped` skeleton; a non-tuple item is the Phase B bare-str keyboard payload → `input`):

```python
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
```

- [ ] **Step 4: Run test to verify it passes** — `uv run python tests/test_mirror_input.py`
  Expected: `OK test_mirror_input` (the two new tests pass AND the two Phase B inject tests still pass — bare-str enqueue + FIFO are preserved).

- [ ] **Step 5: Commit**
```
git add saikai_mirror.py tests/test_mirror_input.py
git commit -m "$(cat <<'EOF'
feat(mirror): typed inject FIFO — dispatch keyboard/mouse/key in order

One drain, tagged items. Phase B keyboard keeps enqueuing a bare str
(unchanged tests); inject_mouse/inject_key add tagged tuples; the single
_inject_loop dispatches by tag, preserving global submission order.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: Hub — `do_POST /mouse` (shared gate + body prelude)

Refactor the `do_POST` common prefix into `_post_gate_and_json()` so `/input`, `/mouse`, `/key` share ONE verbatim gate, then add the `/mouse` route. `/mouse` body `{col,row,button,kind}` with `kind ∈ {down,up,scrollup,scrolldown}`. Status matrix: 204 accept / 400 bad body / 403 host|key|origin / 409 control-off / 405 unknown path / 411 chunked / 413 oversized — identical to `/input`.

**Files:**
- Modify: `saikai_mirror.py` (`_Handler.do_POST` `:721-768`; add `_post_gate_and_json` + `_do_input`/`_do_mouse` helpers)
- Test: `tests/test_mirror_input.py`

- [ ] **Step 1: Write the failing test** (append; add the call to the `__main__` runner). Reuses the module's `_post` / `_raw_request` helpers:

```python
def test_post_mouse_gate_and_body_matrix():
    hub = m.MirrorHub(token="secret", host="127.0.0.1", port=0)
    got = []
    hub.set_mouse_handler(lambda col, row, button, kind: got.append((col, row, button, kind)))
    hub._control_enabled = True
    port = hub.serve()
    key = hub._write_key
    try:
        WK = {"X-Mirror-Write-Key": key}
        # Good key + good body -> 204; handler gets the exact 0-based coords.
        st, _ = _post(port, "/mouse", {"col": 5, "row": 9, "button": 0, "kind": "down"}, headers=WK)
        assert st == 204, st
        st, _ = _post(port, "/mouse", {"col": 5, "row": 9, "button": 0, "kind": "up"}, headers=WK)
        assert st == 204, st
        st, _ = _post(port, "/mouse", {"col": 2, "row": 3, "button": 0, "kind": "scrollup"}, headers=WK)
        assert st == 204, st
        # Bad key -> 403, not delivered.
        st, _ = _post(port, "/mouse", {"col": 1, "row": 1, "button": 0, "kind": "down"},
                      headers={"X-Mirror-Write-Key": "wrong"})
        assert st == 403, st
        # Missing field -> 400.
        st, _ = _post(port, "/mouse", {"col": 1, "row": 1, "button": 0}, headers=WK)
        assert st == 400, st
        # Non-int coord -> 400.
        st, _ = _post(port, "/mouse", {"col": "x", "row": 1, "button": 0, "kind": "down"}, headers=WK)
        assert st == 400, st
        # Unknown kind -> 400.
        st, _ = _post(port, "/mouse", {"col": 1, "row": 1, "button": 0, "kind": "wiggle"}, headers=WK)
        assert st == 400, st
        # Non-JSON -> 400.
        st, _ = _post(port, "/mouse", raw=b"not json", headers=WK)
        assert st == 400, st
        # Control OFF -> 409.
        hub._control_enabled = False
        st, _ = _post(port, "/mouse", {"col": 1, "row": 1, "button": 0, "kind": "down"}, headers=WK)
        assert st == 409, st
        hub._control_enabled = True
        # Only the three good taps reached the handler, in order.
        assert got == [(5, 9, 0, "down"), (5, 9, 0, "up"), (2, 3, 0, "scrollup")], got
        # Phase B regression: /input still 204 with a wired input handler.
        hub.set_input_handler(lambda d: None)
        st, _ = _post(port, "/input", {"data": "x"}, headers=WK)
        assert st == 204, st
    finally:
        hub.stop()


def test_post_mouse_host_and_origin_matrix():
    hub = m.MirrorHub(token="secret", host="127.0.0.1", port=0)
    hub.set_mouse_handler(lambda *a: None)
    hub._control_enabled = True
    port = hub.serve()
    key = hub._write_key
    good_host = f"127.0.0.1:{port}"
    body = json.dumps({"col": 1, "row": 1, "button": 0, "kind": "down"}).encode("utf-8")
    try:
        base = {"X-Mirror-Write-Key": key, "Content-Type": "application/json"}

        def H(**extra):
            h = dict(base); h["_body"] = body; h.update(extra); return h

        # Foreign Host -> 403.
        assert _raw_request(port, "POST", "/mouse",
                            H(Host="evil.example.com", Origin="http://evil.example.com")) == 403
        # Matching Host + Origin -> 204.
        assert _raw_request(port, "POST", "/mouse",
                            H(Host=good_host, Origin=f"http://{good_host}")) == 204
        # Cross-origin -> 403.
        assert _raw_request(port, "POST", "/mouse",
                            H(Host=good_host, Origin="http://attacker.test")) == 403
        # Absent Origin AND Referer -> 403 (fail-closed).
        assert _raw_request(port, "POST", "/mouse", H(Host=good_host)) == 403
    finally:
        hub.stop()
```

- [ ] **Step 2: Run test to verify it fails** — `uv run python tests/test_mirror_input.py`
  Expected: FAIL — `/mouse` currently hits `if path != "/input": self._reject(405, …)`, so the first `assert st == 204` fails (got `405`).

- [ ] **Step 3: Write minimal implementation**

Replace the whole `do_POST` (`saikai_mirror.py:721-768`) with a dispatcher + a shared gate/body helper + per-route handlers. `_post_gate_and_json()` returns the parsed dict on success, or `None` after it has already sent the error response (so each route just `return`s on `None`):

```python
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
        response (the caller just returns). Does NOT check control-on — each
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
```

(`_do_key` is added in Task 3; reference it now in the `do_POST` dispatcher so the dispatcher is written once — Task 3 only adds the method.)

- [ ] **Step 4: Run test to verify it passes** — `uv run python tests/test_mirror_input.py`
  Expected: `OK test_mirror_input`. **NOTE:** the dispatcher references `self._do_key`, which does not exist until Task 3 — so add a temporary stub `def _do_key(self): self._reject(405, "method not allowed")` at the end of `_Handler` in THIS task, and replace it with the real body in Task 3. (This keeps each task individually green.)

- [ ] **Step 5: Commit**
```
git add saikai_mirror.py tests/test_mirror_input.py
git commit -m "$(cat <<'EOF'
feat(mirror): POST /mouse behind the Phase B gate

Refactor the do_POST prelude into _post_gate_and_json (one verbatim
host/key/origin/body gate for all routes), add /mouse parsing
{col,row,button,kind} -> inject_mouse with the same 204/400/403/409 matrix.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 3: Hub — `do_POST /key`

Add the `/key` route (the dispatcher already calls `_do_key` from Task 2; replace the temporary stub with the real body). Body `{key}` (a non-empty str, capped length). Same gate + status matrix.

**Files:**
- Modify: `saikai_mirror.py` (`_Handler._do_key`)
- Test: `tests/test_mirror_input.py`

- [ ] **Step 1: Write the failing test** (append; add the call to the `__main__` runner):

```python
def test_post_key_gate_and_body_matrix():
    hub = m.MirrorHub(token="secret", host="127.0.0.1", port=0)
    got = []
    hub.set_key_handler(lambda key: got.append(key))
    hub._control_enabled = True
    port = hub.serve()
    key = hub._write_key
    try:
        WK = {"X-Mirror-Write-Key": key}
        st, _ = _post(port, "/key", {"key": "escape"}, headers=WK)
        assert st == 204, st
        st, _ = _post(port, "/key", {"key": "ctrl+c"}, headers=WK)
        assert st == 204, st
        # Bad key header -> 403.
        st, _ = _post(port, "/key", {"key": "tab"}, headers={"X-Mirror-Write-Key": "wrong"})
        assert st == 403, st
        # Missing 'key' -> 400.
        st, _ = _post(port, "/key", {"nope": 1}, headers=WK)
        assert st == 400, st
        # Non-str 'key' -> 400.
        st, _ = _post(port, "/key", {"key": 123}, headers=WK)
        assert st == 400, st
        # Empty 'key' -> 400 (never post a garbage Key).
        st, _ = _post(port, "/key", {"key": ""}, headers=WK)
        assert st == 400, st
        # Over-long 'key' -> 400 (defensive cap).
        st, _ = _post(port, "/key", {"key": "x" * 65}, headers=WK)
        assert st == 400, st
        # Control OFF -> 409.
        hub._control_enabled = False
        st, _ = _post(port, "/key", {"key": "up"}, headers=WK)
        assert st == 409, st
        hub._control_enabled = True
        # Foreign Host -> 403.
        assert _raw_request(port, "POST", "/key",
                            {"X-Mirror-Write-Key": key, "Content-Type": "application/json",
                             "Host": "evil.example.com", "Origin": "http://evil.example.com",
                             "_body": json.dumps({"key": "up"}).encode("utf-8")}) == 403
        # Only the two good keys reached the handler, in order.
        assert got == ["escape", "ctrl+c"], got
        # Unknown path is still 405.
        assert _raw_request(port, "POST", "/nope",
                            {"X-Mirror-Write-Key": key, "Content-Type": "application/json",
                             "Host": f"127.0.0.1:{port}", "Origin": f"http://127.0.0.1:{port}",
                             "_body": b"{}"}) == 405
    finally:
        hub.stop()
```

- [ ] **Step 2: Run test to verify it fails** — `uv run python tests/test_mirror_input.py`
  Expected: FAIL — the Task 2 stub `_do_key` 405s, so the first `assert st == 204` fails (got `405`).

- [ ] **Step 3: Write minimal implementation** — replace the temporary `_do_key` stub in `_Handler` with:

```python
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
```

- [ ] **Step 4: Run test to verify it passes** — `uv run python tests/test_mirror_input.py`
  Expected: `OK test_mirror_input`.

- [ ] **Step 5: Commit**
```
git add saikai_mirror.py tests/test_mirror_input.py
git commit -m "$(cat <<'EOF'
feat(mirror): POST /key behind the Phase B gate

{key} -> inject_key, same host/key/origin gate; reject empty/non-str/over-cap
keys so a garbage events.Key is never posted. 204/400/403/409 matrix.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 4: App — `_MirrorControl._mirror_inject_mouse(col,row,button,kind)`

Add the mouse-injection method to the **module-scope `_MirrorControl` mixin** (`saikai.py:2920`). Re-check the authoritative `_control_enabled` on the UI thread; for a click (`down`/`up`) post `MouseDown`/`MouseUp`; for a scroll post `MouseScrollUp`/`MouseScrollDown`. Coords are 0-based (the browser already subtracted 1). Import `events` function-locally (no module-level textual). Clamp/ignore obviously bad coords (negative) so a malformed cell can't post a negative-coord event.

**Files:**
- Modify: `saikai.py` (`_MirrorControl`, after `_mirror_inject_input` ~`:2950`)
- Test: `tests/test_mirror_input.py`

- [ ] **Step 1: Write the failing test** (append; add the call to the `__main__` runner). Builds the bare mixin via `__new__` (textual-free import; the in-method `from textual import events` resolves against `.venv`) and records `post_message`:

```python
def test_mirror_inject_mouse_double_gate_and_events():
    """_mirror_inject_mouse no-ops when control is OFF (UI-thread re-check), and
    when ON posts MouseDown+MouseUp for a click / MouseScroll* for a scroll, at
    the given 0-based coords. Built via __new__ + a post_message recorder (no
    textual run loop)."""
    try:
        from textual import events
    except Exception:
        print("SKIP test_mirror_inject_mouse_double_gate_and_events (textual unavailable)")
        return
    import saikai
    app = saikai._MirrorControl.__new__(saikai._MirrorControl)
    posted = []
    app.post_message = lambda ev: posted.append(ev)

    # Control OFF -> no-op (double-gate).
    app._control_enabled = False
    app._mirror_inject_mouse(5, 9, 0, "down")
    assert posted == [], "control OFF must post nothing"

    # Control ON, click down+up -> MouseDown then MouseUp at (5,9), button 0.
    app._control_enabled = True
    app._mirror_inject_mouse(5, 9, 0, "down")
    app._mirror_inject_mouse(5, 9, 0, "up")
    assert len(posted) == 2, posted
    assert isinstance(posted[0], events.MouseDown), posted
    assert isinstance(posted[1], events.MouseUp), posted
    for ev in posted:
        assert ev.x == 5 and ev.y == 9, (ev.x, ev.y)
        assert ev.screen_x == 5 and ev.screen_y == 9, (ev.screen_x, ev.screen_y)
        assert ev.button == 0, ev.button

    # Scroll -> MouseScrollUp / MouseScrollDown (button 0).
    posted.clear()
    app._mirror_inject_mouse(2, 3, 0, "scrollup")
    app._mirror_inject_mouse(2, 3, 0, "scrolldown")
    assert isinstance(posted[0], events.MouseScrollUp), posted
    assert isinstance(posted[1], events.MouseScrollDown), posted
    assert posted[0].x == 2 and posted[0].y == 3, (posted[0].x, posted[0].y)

    # Negative coord -> ignored (no event).
    posted.clear()
    app._mirror_inject_mouse(-1, 4, 0, "down")
    app._mirror_inject_mouse(4, -1, 0, "down")
    assert posted == [], "out-of-range cell must be ignored"

    # Unknown kind -> ignored.
    app._mirror_inject_mouse(1, 1, 0, "wiggle")
    assert posted == [], "unknown kind must post nothing"
```

- [ ] **Step 2: Run test to verify it fails** — `uv run python tests/test_mirror_input.py`
  Expected: FAIL with `AttributeError: '_MirrorControl' object has no attribute '_mirror_inject_mouse'`.

- [ ] **Step 3: Write minimal implementation** — add to `_MirrorControl`, right after `_mirror_inject_input` (`saikai.py:2950`):

```python
    def _mirror_inject_mouse(self, col: int, row: int, button: int, kind: str) -> None:
        """Post a synthesized Textual mouse event into the App so it routes
        natively (App.on_event hit-tests get_widget_at and synthesizes the Click
        for a down+up pair -> DataTable sort / row cursor / pane focus).

        Runs on the Textual UI thread (the mouse handler marshals here via
        call_from_thread). Re-checks the AUTHORITATIVE _control_enabled (the hub's
        copy is advisory). Coords are 0-based screen cells (the browser already
        converted xterm's 1-based report). events is imported here, not at module
        scope, so this mixin stays importable without textual."""
        if not self._control_enabled:
            return
        if col < 0 or row < 0:                 # out-of-range cell: ignore
            return
        from textual import events
        if kind == "down":
            cls = events.MouseDown
        elif kind == "up":
            cls = events.MouseUp
        elif kind == "scrollup":
            cls = events.MouseScrollUp
        elif kind == "scrolldown":
            cls = events.MouseScrollDown
        else:
            return                             # unknown kind: never post garbage
        # Scroll has no pressed button (0); a click carries the SGR button index.
        btn = button if kind in ("down", "up") else 0
        ev = cls(None, col, row, 0, 0, btn, False, False, False,
                 screen_x=col, screen_y=row)
        try:
            self.post_message(ev)
        except Exception:
            pass                               # app tearing down between gate + post
```

- [ ] **Step 4: Run test to verify it passes** — `uv run python tests/test_mirror_input.py`
  Expected: `OK test_mirror_input`.

- [ ] **Step 5: Commit**
```
git add saikai.py tests/test_mirror_input.py
git commit -m "$(cat <<'EOF'
feat(mirror): _mirror_inject_mouse posts synthesized Textual mouse events

UI-thread method on _MirrorControl: re-checks _control_enabled, posts
MouseDown/MouseUp (App synthesizes the Click) or MouseScroll* at 0-based
coords; ignores out-of-range cells and unknown kinds. events imported in-body
to keep the mixin textual-free at import.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 5: App — `_MirrorControl._mirror_inject_key(key)`

Add the key-injection method to `_MirrorControl`. Re-check `_control_enabled`; build `events.Key(key, character)` and `post_message` it. The App routes it to priority bindings / the focused widget (the `Pilot.press` path). A single printable char carries itself as `character`; a named/modified key carries `character=None`.

**Files:**
- Modify: `saikai.py` (`_MirrorControl`, after `_mirror_inject_mouse`)
- Test: `tests/test_mirror_input.py`

- [ ] **Step 1: Write the failing test** (append; add the call to the `__main__` runner):

```python
def test_mirror_inject_key_double_gate_and_event():
    """_mirror_inject_key no-ops when control is OFF; when ON posts events.Key
    with the right key/character (printable char carries itself; a named key
    carries character=None). Built via __new__ + a post_message recorder."""
    try:
        from textual import events
    except Exception:
        print("SKIP test_mirror_inject_key_double_gate_and_event (textual unavailable)")
        return
    import saikai
    app = saikai._MirrorControl.__new__(saikai._MirrorControl)
    posted = []
    app.post_message = lambda ev: posted.append(ev)

    # Control OFF -> no-op.
    app._control_enabled = False
    app._mirror_inject_key("escape")
    assert posted == [], "control OFF must post nothing"

    # Control ON: a named key -> Key(key="escape"), non-printable (character None).
    app._control_enabled = True
    app._mirror_inject_key("escape")
    assert len(posted) == 1 and isinstance(posted[0], events.Key), posted
    assert posted[0].key == "escape", posted[0].key
    assert posted[0].is_printable is False, "named key must be non-printable"

    # A single printable char -> Key carries itself as character.
    posted.clear()
    app._mirror_inject_key(" ")               # space (the leader)
    assert posted[0].key == " " and posted[0].character == " ", posted[0]

    # A modified key -> Key(key="ctrl+c"), non-printable.
    posted.clear()
    app._mirror_inject_key("ctrl+c")
    assert posted[0].key == "ctrl+c" and posted[0].is_printable is False, posted[0]

    # Empty / non-str -> ignored (never post a garbage Key).
    posted.clear()
    app._mirror_inject_key("")
    app._mirror_inject_key(None)
    assert posted == [], "empty/None key must post nothing"
```

- [ ] **Step 2: Run test to verify it fails** — `uv run python tests/test_mirror_input.py`
  Expected: FAIL with `AttributeError: '_MirrorControl' object has no attribute '_mirror_inject_key'`.

- [ ] **Step 3: Write minimal implementation** — add to `_MirrorControl`, right after `_mirror_inject_mouse`:

```python
    def _mirror_inject_key(self, key: str) -> None:
        """Post a synthesized events.Key into the App so it routes to priority
        bindings / the focused widget (the same path Pilot.press uses) -> saikai's
        leader, F-keys, arrows, Esc/Tab all dispatch natively.

        Runs on the Textual UI thread (the key handler marshals here via
        call_from_thread). Re-checks the AUTHORITATIVE _control_enabled. A single
        printable char carries itself as the Key.character; a named/modified key
        ('escape', 'tab', 'up', 'ctrl+c', 'f12') carries character=None (Textual's
        Key.__init__ leaves it None for len != 1). events imported in-body to keep
        the mixin textual-free at import."""
        if not self._control_enabled:
            return
        if not isinstance(key, str) or key == "":
            return                             # never post a garbage Key
        from textual import events
        character = key if len(key) == 1 else None
        try:
            self.post_message(events.Key(key, character))
        except Exception:
            pass                               # app tearing down between gate + post
```

- [ ] **Step 4: Run test to verify it passes** — `uv run python tests/test_mirror_input.py`
  Expected: `OK test_mirror_input`.

- [ ] **Step 5: Commit**
```
git add saikai.py tests/test_mirror_input.py
git commit -m "$(cat <<'EOF'
feat(mirror): _mirror_inject_key posts a synthesized events.Key

UI-thread method on _MirrorControl: re-checks _control_enabled, posts
events.Key(key, character) so the App routes it to priority bindings / the
focused widget; ignores empty/non-str keys. events imported in-body.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 6: App — `on_mount` wiring for the mouse + key handlers

Wire `_hub.set_mouse_handler(...)` and `_hub.set_key_handler(...)` next to the Phase B input handler (`saikai.py:3735-3743`), each a guarded `_marshal`-shaped closure: bail if `not is_running`, marshal onto the UI thread with `call_from_thread`, swallow teardown exceptions. NEVER a bare `call_from_thread`.

**Files:**
- Modify: `saikai.py` (`on_mount`, inside `if _hub is not None:`, after `_hub.set_input_handler(_inject_handler)` at `:3743`)
- Test: `tests/test_keyboard_leader.py` (verified by the Task 7 Pilot test, which drives the real `on_mount`; no separate unit test — `on_mount` is a closure-wiring block exercised end-to-end)

- [ ] **Step 1: (covered by Task 7's Pilot test)** — Task 7 asserts a synthesized click/key reaches a real widget, which only works if `on_mount` wired the handlers. Write Task 7's test FIRST if executing strictly red-green; otherwise wire here and let Task 7 prove it. No standalone failing test for this block.

- [ ] **Step 2: Write the implementation** — immediately after `_hub.set_input_handler(_inject_handler)` (`saikai.py:3743`), add:

```python
                _hub.set_input_handler(_inject_handler)
                # Phase C: deliver browser taps + key-bar presses into the App as
                # synthesized Textual events. Same _marshal shape as the input
                # handler — capture the app, bail if it's gone, marshal onto the
                # UI thread, swallow shutdown errors. NEVER a bare call_from_thread
                # (whose future.result() could block the inject-drain thread).
                def _mouse_handler(col, row, button, kind, _app=_app_ref):
                    if not getattr(_app, "is_running", False):
                        return
                    try:
                        _app.call_from_thread(
                            _app._mirror_inject_mouse, col, row, button, kind)
                    except Exception:
                        pass
                _hub.set_mouse_handler(_mouse_handler)

                def _key_handler(key, _app=_app_ref):
                    if not getattr(_app, "is_running", False):
                        return
                    try:
                        _app.call_from_thread(_app._mirror_inject_key, key)
                    except Exception:
                        pass
                _hub.set_key_handler(_key_handler)
```

- [ ] **Step 3: Run the existing Pilot suite to confirm nothing regressed** — `uv run python tests/test_keyboard_leader.py`
  Expected: `ALL PASS` (the existing `test_pilot_mirror_control_toggle` `_StubHub` defines `set_input_handler`/`set_size`/`set_repaint_request`/`url` but NOT `set_mouse_handler`/`set_key_handler` — **so this Task must also add those two no-op methods to that test's `_StubHub`** or `on_mount` raises `AttributeError` and the toggle test fails. Update `_StubHub` (`tests/test_keyboard_leader.py:385-398`) minimally:

```python
        def set_input_handler(self, *a):
            pass
        def set_mouse_handler(self, *a):
            pass
        def set_key_handler(self, *a):
            pass
```

This is a Phase B test the Phase C change touches — flagged here and updated minimally per the rules.)

- [ ] **Step 4: Commit**
```
git add saikai.py tests/test_keyboard_leader.py
git commit -m "$(cat <<'EOF'
feat(mirror): wire mouse + key handlers in on_mount

Two guarded _marshal-shaped closures next to the Phase B input handler:
set_mouse_handler / set_key_handler each bail if !is_running, marshal onto
the UI thread, swallow teardown errors. _StubHub in the toggle test gains the
matching no-op methods.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 7: Pilot — synthesized click + key drive the REAL PickerApp

A skip-guarded Textual Pilot test (in `tests/test_keyboard_leader.py`) that, with the real `PickerApp` running, calls `_mirror_inject_mouse` on a column-header cell and asserts the table re-sorted (or a row got selected), and calls `_mirror_inject_key` with a priority-binding key and asserts the action fired. This confirms saikai routes injected events to the right widget (Click synthesis + Key dispatch), end-to-end — including the Task 6 `on_mount` wiring.

**Files:**
- Modify: `tests/test_keyboard_leader.py` (add `test_pilot_mirror_tap_and_key_drive_ui`; add its call + `print` to the `__main__` runner)

- [ ] **Step 1: Write the failing test** — append, and add to the `__main__` runner. It drives the real app, enables control on the UI thread, posts a synthesized `events.Key("f6")` (the `favorite` priority binding — focus-independent) and asserts the favorite flips, then posts a header click and asserts the sort changed. Header geometry is read from the live `DataTable` so the click lands on a real header cell:

```python
def test_pilot_mirror_tap_and_key_drive_ui():
    """End-to-end: with control ON, a synthesized events.Key fires a priority
    binding (F6 favorite) and a synthesized click is dispatched by App.on_event
    (mouse_position updates to the clicked cell) — proving on_mount wired the
    handlers and the App routes injected Key + Mouse events natively. Drives the
    REAL PickerApp."""
    try:
        from textual.app import App  # noqa: F401
    except Exception:
        print("SKIP test_pilot_mirror_tap_and_key_drive_ui (textual unavailable)")
        return

    import asyncio
    from textual.app import App

    sid = _write_demo_session()
    facts: dict = {}

    def fake_run(self, *a, **kw):
        async def go():
            async with self.run_test(size=(110, 30)) as pilot:
                await pilot.pause(0.4)
                # Turn control ON directly (the toggle is local-only; we exercise
                # the injection path, not the keybinding).
                self._control_enabled = True
                # 1) A priority binding via a synthesized Key: F6 = favorite.
                before = sid in (saikai._read_json(saikai.FAVORITE_FILE, []) or [])
                self._mirror_inject_key("f6")
                await pilot.pause(0.3)
                after = sid in (saikai._read_json(saikai.FAVORITE_FILE, []) or [])
                facts["fav_before"] = before
                facts["fav_after"] = after
                # 2) A synthesized click is DISPATCHED by App.on_event, which sets
                # self.mouse_position = Offset(event.x, event.y) on MouseDown — a
                # side-effect-free, widget-agnostic proof the click routed.
                table = self.query_one("#table")
                region = table.region                 # screen region of the table
                col_x = region.x + 2                  # a real on-screen cell
                row_y = region.y + 2                  # a row inside the table
                self._mirror_inject_mouse(col_x, row_y, 0, "down")
                self._mirror_inject_mouse(col_x, row_y, 0, "up")
                await pilot.pause(0.3)
                mp = self.mouse_position
                facts["mouse_xy"] = (mp.x, mp.y)
                facts["click_target"] = (col_x, row_y)
                facts["still_running"] = self.is_running
        asyncio.run(go())

    orig_run, App.run = App.run, fake_run
    orig_argv = sys.argv
    try:
        sys.argv = ["saikai", "--all"]
        saikai.main()
    finally:
        App.run = orig_run
        sys.argv = orig_argv

    # The Key reached the favorite priority binding (focus-independent).
    assert facts.get("fav_before") is False, facts
    assert facts.get("fav_after") is True, f"synthesized F6 did not favorite: {facts}"
    # The synthesized click was dispatched by App.on_event: it set mouse_position
    # to the clicked cell (proves routing, widget-agnostic, no side-effect), and
    # the app survived (no crash).
    assert facts.get("still_running") is True, f"app crashed on injected click: {facts}"
    assert facts.get("mouse_xy") == facts.get("click_target"), \
        f"injected click did not reach App.on_event (mouse_position): {facts}"
```

**Implementation note:** the click assertion uses `App.mouse_position` — Textual sets it in `App.on_event` on every `MouseDown` (app.py:4069) — a side-effect-free, widget-agnostic proof the synthesized click reached the App's event dispatch. No app-specific sort/cursor attribute is needed for the click half (and no write to the user's sort state); the F6 favorite is the only persisted side-effect, on a throwaway demo session.

- [ ] **Step 2: Run test to verify it fails** — `uv run python tests/test_keyboard_leader.py`
  Expected without Tasks 4-6 in place: `AttributeError` on `set_mouse_handler` during `on_mount`, or `fav_after`/`mouse_xy` never update (handlers/methods missing). With Tasks 1-6 done it goes GREEN.

- [ ] **Step 3: Minimal implementation** — none in product code (Tasks 4-6 already added the methods + wiring). This task only adds the end-to-end Pilot test.

- [ ] **Step 4: Run test to verify it passes** — `uv run python tests/test_keyboard_leader.py`
  Expected: `ALL PASS` (including the new test; the F6 favorite + the survived/re-sorted click both hold).

- [ ] **Step 5: Commit**
```
git add tests/test_keyboard_leader.py
git commit -m "$(cat <<'EOF'
test(mirror): Pilot — synthesized click + key drive the real PickerApp

End-to-end: control ON, a synthesized events.Key fires the F6 favorite
priority binding and a synthesized header click re-sorts the table, proving
on_mount wiring + native event routing (Key dispatch + Click synthesis).

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 8: Browser JS — split `onData` (SGR mouse → `/mouse`), key bar → `/key`

Edit `_PAGE_HTML` (`saikai_mirror.py:441-537`): in `onData`, detect an SGR mouse sequence (`\x1b[<b;col;row` then `M` (press) or `m` (release)) and route it to `POST /mouse` (parse `b`/`col`/`row`, convert 1-based→0-based, map to a `kind`); everything else stays the Phase B keyboard `POST /input`. Add a `postMouse`/`postKey` single-flight pair (each with the `X-Mirror-Write-Key` header + the same 409/403 reactions) and an on-screen, fixed-position **key bar** (Leader, Esc, Tab, ↑ ↓ ← →, Ctrl-sticky, F12) wired to `POST /key`. Disabled until a `control` on-frame. The base64 `onmessage` output path stays byte-identical. **All new ESC bytes are the two-char source `\\x1b`** (the no-stray-CR/control-byte test guards this).

xterm must emit SGR mouse: enable mouse tracking so taps/swipes produce `\x1b[<…M/m` in `onData`. Add `term.options.<mouse opt>` or write the DECSET enable on open — **verify the xterm.js API in the vendored build** (`saikai_mirror_static/xterm.min.js`) before choosing the exact call; the served-page test only asserts the routing/handlers/buttons exist, and a manual phone check (noted) confirms real taps.

SGR decode (1-based xterm → 0-based cells; low 2 bits of `b` select the button, bit 6 / `b>=64` marks a wheel event, `M`=press / `m`=release):
- wheel up: `b == 64` → `kind="scrollup"`, `button=0`
- wheel down: `b == 65` → `kind="scrolldown"`, `button=0`
- press (`M`, `b < 64`): `kind="down"`, `button = b & 3`
- release (`m`, `b < 64`): `kind="up"`, `button = b & 3`
- `col0 = col - 1`, `row0 = row - 1`

**Files:**
- Modify: `saikai_mirror.py` (`_PAGE_HTML`)
- Test: `tests/test_mirror_input.py` (extend `test_page_contains_input_listeners_and_sender` + add a key-bar/mouse-routing string-assert test; the no-control-byte test `test_page_has_no_js_breaking_control_bytes` already runs and must stay green)

- [ ] **Step 1: Write the failing test** (append a new page test; add its call to the `__main__` runner):

```python
def test_page_routes_mouse_and_has_key_bar():
    """No browser in CI: assert the served page (a) routes SGR mouse in onData to
    POST /mouse (parsing b;col;row, 1-based -> 0-based, M/m -> kind) while keeping
    keyboard on /input, (b) has a postKey single-flight to /key with the write-key
    header, and (c) renders the on-screen key bar buttons (Leader/Esc/Tab/arrows/
    Ctrl/F12). Manual phone verification covers real tap/scroll fidelity."""
    hub = m.MirrorHub(token="secret", host="127.0.0.1", port=0, cols=80, rows=24)
    hub.set_input_handler(lambda d: None)
    hub.set_mouse_handler(lambda *a: None)
    hub.set_key_handler(lambda *a: None)
    port = hub.serve()
    try:
        page = urllib.request.urlopen(
            f"http://127.0.0.1:{port}/?token=secret", timeout=3.0
        ).read().decode("utf-8")
        # (a) SGR mouse routing in onData: the parser + the /mouse endpoint.
        assert "/mouse" in page, page
        assert "[<" in page, "page must detect the SGR mouse prefix ESC[<"   # \x1b[<
        # 1-based -> 0-based conversion is present (a subtraction by 1).
        assert "- 1" in page, page
        # press/release distinguished (M vs m).
        assert "'M'" in page or '"M"' in page, page
        # keyboard still routes to /input (unchanged Phase B path).
        assert "/input" in page, page
        # (b) postKey single-flight to /key with the write-key header.
        assert "/key" in page and "X-Mirror-Write-Key" in page, page
        # (c) the on-screen key bar buttons.
        for label in ("Leader", "Esc", "Tab", "Ctrl", "F12"):
            assert label in page, f"key bar missing {label}: {page[:200]}"
        # arrows present (any of the glyphs or names).
        assert ("↑" in page or "up" in page), page    # ↑ / "up"
        # the gate reactions are still wired for the new senders.
        assert "409" in page and "403" in page, page
        # output path untouched.
        assert "es.onmessage" in page and "atob" in page, page
    finally:
        hub.stop()
```

- [ ] **Step 2: Run test to verify it fails** — `uv run python tests/test_mirror_input.py`
  Expected: FAIL — current page has no `/mouse`, no `/key`, no key-bar labels.

- [ ] **Step 3: Write minimal implementation** — edit `_PAGE_HTML`. Concretely:

  1. **Enable mouse tracking** after `term.open(...)` (verify the exact vendored-xterm call first; the common form is `term.options.<…>` or a DECSET write). The string-assert test does not depend on which; the manual phone check validates it.
  2. **Add the key-bar DOM + CSS** (fixed-position bar; one `<button>` per key; a sticky Ctrl flag). Append before `</body>` (after the existing banner block), e.g.:
     ```html
     <div id="kb" style="position:fixed;bottom:0;left:0;right:0;display:flex;
       flex-wrap:wrap;gap:4px;padding:4px;background:#222;z-index:9">
       <button data-k="space">Leader</button>
       <button data-k="escape">Esc</button>
       <button data-k="tab">Tab</button>
       <button data-k="up">&#8593;</button>
       <button data-k="down">&#8595;</button>
       <button data-k="left">&#8592;</button>
       <button data-k="right">&#8594;</button>
       <button id="kb-ctrl" data-k="">Ctrl</button>
       <button data-k="f12">F12</button>
     </div>
     ```
  3. **Add `postKey` / `postMouse` single-flight senders** mirroring `pump()` (the existing 409→`setBanner(false,null)` / 403→fatal reactions, the `X-Mirror-Write-Key` header, `controlOn`/`fatal`/`writeKey` guards). Example shape (full code, no cross-ref):
     ```js
     let ctrlSticky = false;
     const kbCtrl = document.getElementById('kb-ctrl');
     function reactStatus(status) {
       if (status === 409) { setBanner(false, null); }
       else if (status === 403) { fatal = true; banner.style.background='#a33';
         banner.textContent = 'CONTROL LOST (auth) — reload'; }
     }
     async function postKey(key) {
       if (fatal || !controlOn || writeKey === null || !key) return;
       try {
         const resp = await fetch('/key', {
           method: 'POST',
           headers: {'Content-Type':'application/json','X-Mirror-Write-Key':writeKey},
           body: JSON.stringify({key: key})
         });
         reactStatus(resp.status);
       } catch (_) {}
     }
     async function postMouse(col, row, button, kind) {
       if (fatal || !controlOn || writeKey === null) return;
       try {
         const resp = await fetch('/mouse', {
           method: 'POST',
           headers: {'Content-Type':'application/json','X-Mirror-Write-Key':writeKey},
           body: JSON.stringify({col: col, row: row, button: button, kind: kind})
         });
         reactStatus(resp.status);
       } catch (_) {}
     }
     // Key bar: each button posts its key; Ctrl is a sticky modifier applied to
     // the NEXT key (and the next tap, applied server-side is out of scope — the
     // modifier only composes key-bar keys here).
     document.querySelectorAll('#kb button').forEach((b) => {
       b.addEventListener('click', (e) => {
         e.preventDefault();
         if (b.id === 'kb-ctrl') {
           ctrlSticky = !ctrlSticky;
           kbCtrl.style.background = ctrlSticky ? '#3a3' : '';
           return;
         }
         let k = b.getAttribute('data-k');
         if (ctrlSticky) { k = 'ctrl+' + k; ctrlSticky = false; kbCtrl.style.background=''; }
         postKey(k);
       });
     });
     ```
  4. **Split `onData`** so SGR mouse goes to `/mouse` and the rest keeps the Phase B `/input` pump. Replace the existing `term.onData((d) => { … })` (`saikai_mirror.py:531-536`) with:
     ```js
     // SGR mouse: ESC [ < b ; col ; row (M=press, m=release). Route to /mouse;
     // everything else is keyboard -> the Phase B coalescing /input pump.
     const SGR = /\\x1b\\[<(\\d+);(\\d+);(\\d+)([Mm])/;   // built as a string below
     term.onData((d) => {
       if (!controlOn || fatal) return;
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
       pending += d;
       if (isControlByte(d)) { if (flushTimer) { clearTimeout(flushTimer); flushTimer=null; } pump(); }
       else if (!flushTimer) { flushTimer = setTimeout(() => { flushTimer=null; pump(); }, 25); }
     });
     ```
     **CRITICAL (no-stray-control-byte test):** do NOT bake a literal ESC into the regex literal. Build the regex from a string so the source contains the two-char `\\x1b` (Python) → `\x1b` (JS string) → a real ESC only at runtime:
     ```js
     const sgrMouseRe = new RegExp('\\x1b\\[<(\\d+);(\\d+);(\\d+)([Mm])');
     ```
     In the Python `_PAGE_HTML` triple-quoted string, write that line with doubled backslashes so the served bytes are `new RegExp('\x1b\[<(\d+)...')` as TEXT (no literal ESC byte). The `test_page_has_no_js_breaking_control_bytes` test will FAIL if a real ESC/CR leaks in — run it.

- [ ] **Step 4: Run tests to verify GREEN** — `uv run python tests/test_mirror_input.py`
  Expected: `OK test_mirror_input` — the new routing/key-bar test passes, AND `test_page_contains_input_listeners_and_sender` + `test_page_has_no_js_breaking_control_bytes` + `test_page_injects_terminal_size` (in `test_mirror_hub.py`, run in Task 9) still pass (output path + size injection untouched, no stray control bytes).
  **Manual phone verification (noted, not in CI):** open the mirror on a phone, enable control (Shift+F12 on the host); tap a session row → it selects; tap a column header → it sorts; swipe the list → it scrolls; tap Leader then a letter → the leader menu acts; tap Esc/Tab/arrows/F12 → saikai responds.

- [ ] **Step 5: Commit**
```
git add saikai_mirror.py tests/test_mirror_input.py
git commit -m "$(cat <<'EOF'
feat(mirror): browser tap + key bar — onData splits SGR mouse, key bar -> /key

onData routes ESC[<b;col;row(M/m) to POST /mouse (1-based->0-based, kind from
b/M/m) and keeps keyboard on the Phase B /input pump; adds postMouse/postKey
single-flight senders (write-key header, 409/403 reactions) and a fixed
on-screen key bar (Leader/Esc/Tab/arrows/Ctrl-sticky/F12). Output path
unchanged; SGR regex built from a string so no literal ESC leaks into the page.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 9: Full suite — Phase B regression + Phase C green

Run every affected suite headless and confirm all pass: the Phase C additions AND every Phase B test untouched.

**Files:** none (verification only).

- [ ] **Step 1: Run all four suites**
```
uv run python tests/test_mirror_input.py
uv run python tests/test_terminal_concurrency.py
uv run python tests/test_keyboard_leader.py
uv run python tests/test_mirror_hub.py
```
Expected prints:
- `OK test_mirror_input` (Phase B inject/gate/SSE/page tests + Phase C typed-dispatch, `/mouse`, `/key`, `_mirror_inject_mouse`, `_mirror_inject_key`, page-routing/key-bar)
- the `test_terminal_concurrency` runner's final OK/PASS line (unchanged; no Phase C code there)
- `ALL PASS` (`test_keyboard_leader` — incl. the updated `_StubHub` + the new `test_pilot_mirror_tap_and_key_drive_ui`)
- `OK test_mirror_hub` (broadcast/token/static/size — output path proven unchanged)

- [ ] **Step 2: Confirm no regression in the explicit Phase B assertions** — in `test_mirror_input.py`: `test_inject_gate_off_by_default_and_requires_handler` (bare-str enqueue + refusals), `test_inject_is_fifo_via_single_drain` (`seen == ["a","b","c"]`), `test_post_input_write_key_and_body_matrix`, `test_host_allow_list_and_origin_matrix`, `test_sse_emits_writekey_and_control_without_colliding_output`, `test_page_contains_input_listeners_and_sender`, `test_page_has_no_js_breaking_control_bytes`. All must still print as part of `OK test_mirror_input`. If any went RED, the typed-queue generalization (Task 1) or the page edit (Task 8) broke a Phase B contract — fix the impl, do not relax the Phase B test.

- [ ] **Step 3: Commit (only if Steps 1-2 required a follow-up fix; otherwise skip)**
```
git add -A
git commit -m "$(cat <<'EOF'
test(mirror): Phase C suite green + Phase B regression confirmed

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Spec coverage map (every design section → a task)

- **Mechanism — Click (MouseDown+Up → synthesized Click):** Tasks 4 (post), 7 (end-to-end), 8 (browser tap). Signature corrected to all-positional (verified `MouseEvent.__init__`).
- **Mechanism — Scroll (MouseScrollUp/Down):** Tasks 4, 8.
- **Mechanism — Key (events.Key → bindings/focused widget):** Tasks 5, 7, 8.
- **Coordinates 0-based; xterm 1-based → −1:** Task 8 (browser converts), Tasks 2/4 (pass-through + assert 0-based), verified against `app.py:4069-4082`.
- **Thread-safety (UI-thread via guarded call_from_thread):** Task 6 (closures), Tasks 4/5 (UI-thread re-check + post_message). Never marshal holding a lock; the drain owns no lock when calling handlers.
- **Hub typed queue + tagged dispatch + set_mouse_handler/set_key_handler + inject_mouse/inject_key:** Task 1.
- **do_POST /mouse + /key behind the same gate; status matrix:** Tasks 2, 3.
- **`_PAGE_HTML` onData split + key bar + single-flight + write-key + disabled-until-on + 409/403:** Task 8.
- **PickerApp `_mirror_inject_mouse` / `_mirror_inject_key` (re-check `_control_enabled`, UI-thread, clamp/ignore):** Tasks 4, 5.
- **on_mount wiring:** Task 6.
- **Security (gate reused verbatim; default OFF; idle; LAN opt-in; write-key; Host; Origin):** Tasks 2/3 reuse `_host_ok`/`_write_key_ok`/`_origin_ok` + the advisory `_control_enabled` fast-reject unchanged; the double-gate UI re-check in Tasks 4/5.
- **Error handling (control-off→409; malformed→400; oversized→413; out-of-range→clamp/ignore; dropped MouseUp harmless; unknown key→ignore):** Tasks 2/3 (HTTP), Tasks 4/5 (app-side ignore).
- **Ordering (single FIFO preserves keyboard/mouse/key order):** Task 1 (`test_typed_inject_dispatches_by_tag_in_order`).
- **Divider-drag OUT (no MouseMove):** honored — no task injects `MouseMove`.
- **Testing (Hub / App / Pilot / Browser):** Tasks 1-3 (Hub), 4-5 (App via `__new__`+stub), 7 (Pilot), 8 (Browser page asserts + manual note).
- **Control-toggle-reliability note / robust-toggle follow-up:** explicitly Phase B follow-up, out of scope — no task (correct).
