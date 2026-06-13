# saikai Web Mirror — Phase B (Interactive Control) Design — v2

**Status:** Approved, hardened after adversarial expert-panel review (2026-06-13).
**Builds on:** Phase A read-only mirror (on master: `saikai_mirror.py` — server-side pyte mirror, base64 SSE frames, token auth, vendored xterm.js; the server is already `_Server(ThreadingMixIn, HTTPServer)` so SSE + POST run concurrently).
**Goal:** Let the user operate the *focused* live saikai pane from a browser on the home LAN — typing keystrokes, Ctrl-C, and paste into the running Claude session — gated behind an explicit, default-off control toggle, hardened for the fact that **input injection is a remote-code-execution capability** (it types into an agent that runs tools and edits files).

## v2 changelog (what the expert panel changed)
- **Security:** a **separate, header-only write-key** delivered over the SSE channel (the read token lives in the URL/QR/stderr/`mirror-url.txt`/logs, so it is *not* a write secret); a strict **Host-header allow-list** (defeats DNS rebinding, which otherwise bypasses Origin); a **custom-header secret as the primary CSRF defense** (forces a CORS preflight an attacker cannot satisfy) with **Origin/Referer fail-closed** as defense-in-depth; **idle auto-disable**; **LAN input behind its own opt-in** plus a specific-IP bind.
- **Correctness:** **guaranteed in-order input** (client single-flight + server single-drain queue); **fire-and-forget, `_marshal`-shaped injection** (a raw `call_from_thread` ends on `future.result()` with no timeout and can hang the HTTP thread during shutdown); **SSE control frame as a named `event: control`** carrying raw JSON (named events bypass `onmessage`, and the output path `atob()`s every payload).
- **Reachability:** the control toggle is a **focus-independent priority `Binding`**, not a leader-key — the leader only arms while the session *table* is focused, but a focused *pane* consumes the keypress, so a leader-letter toggle would be unreachable in exactly the state where control is used.
- Plus: HTTP/1.1 + request-body hygiene, dead/no-pane write guards, target-title computed on the UI thread, "focused-pane-only" reframed as a UX scope (not a security boundary), rate-limiting, and an expanded test matrix.

## Scope
**In scope (MVP):**
- Browser → focused live pane keyboard input: characters, Enter, Ctrl-C, arrows, bracketed paste.
- A runtime control toggle (focus-independent binding), default **OFF**, resets OFF on restart, and **idle auto-disables** (~10 min).
- A separate write-key; a Host allow-list; LAN-input opt-in.
- Visible control state in both the TUI and the browser.

**Out of scope (deferred):** driving saikai's own UI from the browser (pane switching, picker, scroll); routing input to a non-focused/explicitly-selected pane; persisting control state; WebSocket transport.

## Approved decisions
1. **Target:** focused pane only (a UX scope — see Security for why it is not a security boundary).
2. **Gate:** runtime toggle, default OFF, in-memory, **idle auto-disable ~10 min** (configurable).
3. **Transport:** `POST /input` + control state over the existing SSE stream (stdlib only).
4. **Write auth:** a **separate write-key** (not the read token), header-only (`X-Mirror-Write-Key`), delivered over the authenticated SSE channel.
5. **LAN input:** disabled unless `SAIKAI_MIRROR_ALLOW_LAN_INPUT=1`; when enabled, prefer binding to the specific phone-facing LAN IP rather than `0.0.0.0`.

## Architecture
`PickerApp._control_enabled: bool = False` is the **authoritative** gate, re-checked on the UI thread. A focus-independent `Binding` toggles it; the toggle pushes the new state (and the focused-pane title, computed on the UI thread) to `MirrorHub`, which keeps an **advisory** copy and broadcasts a control frame.

Input path: browser `xterm.onData` → coalesced, **single-flight** `POST /input` (carrying `X-Mirror-Write-Key`) → `do_POST` gate (Host → write-key → Origin → control-on) → `MirrorHub.inject` enqueues onto a **single-drain queue** → one drain worker calls a **`_marshal`-shaped** handler → `app.call_from_thread(app._mirror_inject_input, data)` → `focused_pane._pty.write(data)`.

The PTY write runs on the Textual UI thread, takes no lock, and is never the close path — satisfying "never marshal while holding `self._lock`" and "never close a POSIX ptyprocess on the UI thread." In-order delivery is guaranteed by one in-flight client POST at a time **and** the single server-side drain (two independent ThreadingMixIn handler threads otherwise have no ordering).

## Components

### saikai_mirror.py
- `MirrorHub`:
  - fields: `_control_enabled=False` (advisory cache of the app's authority), `_input_handler=None`, `_write_key` = `secrets.token_urlsafe(32)` minted at init and **never placed in any URL/file/QR/log**, `_inject_q: queue.Queue`, one drain-worker thread, `_control_target=None`, and idle-timer state.
  - `set_input_handler(fn)` — wired at app mount (mirrors `set_repaint_request`).
  - `set_control_state(enabled, target=None)` — store state + target, broadcast a `control` frame; on enable (re)arm the idle timer, on disable cancel it.
  - `inject(data) -> bool` — if control on, enqueue onto `_inject_q` (non-blocking) and return True, else False. A single drain worker pops FIFO and calls `_input_handler(data)`; the handler is `_marshal`-shaped (capture app, bail if gone, swallow exceptions). Accepted input resets the idle timer.
  - idle auto-disable — a timer (~10 min, configurable) flips control off, broadcasts the off frame, and notifies when no input is accepted within the window.
- `_Handler`:
  - `protocol_version = "HTTP/1.1"` (also benefits SSE; always emit `Content-Length` or use 204 no-body).
  - **Host allow-list on every request** (page, SSE, POST): accept only `127.0.0.1[:port]`, `localhost[:port]`, and the exact bound LAN IP[:port]; reject otherwise (anti-DNS-rebinding).
  - `do_POST /input`: (1) Host ok; (2) `X-Mirror-Write-Key` present and `hmac.compare_digest` vs `_write_key`; (3) `Origin` (or `Referer` host) present and exactly equal to the server origin derived from the request `Host` — reject absent/`null`/mismatch (fail-closed); (4) control on (advisory) else 409; (5) require `Content-Type: application/json`, reject chunked (411), reject `Content-Length > cap` (64 KB) before reading (413), read ≤ cap bytes, parse `{"data": <str>}` (missing/non-str → 400, empty → 204 no-op), `inject(data)`. **Always drain the body even on reject** (avoid keep-alive desync). Status: 204 accept/no-op, 403 host/key/origin, 409 control off, 400/411/413 body, 405 non-POST. A per-connection failure counter + an accepted-input rate cap bound brute force and UI-thread flooding.
  - SSE `_stream`: on connect, emit (a) the **write-key** as a dedicated `event: writekey` raw-JSON record (only ever over this authenticated SSE channel), and (b) the current control state as `event: control`. Thereafter `set_control_state` pushes `event: control`. Terminal output frames stay exactly as today (default-event, base64 `data:`, consumed by `onmessage`). Control/writekey frames are **raw JSON via dedicated `addEventListener`** and are **not** base64-encoded.
- HTML/JS (`_PAGE_HTML`):
  - `addEventListener('writekey', …)` → store the key in a JS variable (memory only; never persisted).
  - `addEventListener('control', …)` → enable/disable `term` input, flip the **CONTROL ON / OFF** banner, show "typing into: ⟨target⟩" only while on.
  - `term.onData(d => enqueue(d))`; a single-flight FIFO sender: coalesce ~20–30 ms but flush immediately on any byte `< 0x20`, ESC, or `\r`; **one POST in-flight at a time** (await the response before the next); send `X-Mirror-Write-Key`. On 409 → flip to OFF + stop; on 403 → fatal "auth lost" banner + stop; on 400/413 → drop that batch + continue. Keyboard stays disabled until a `control` on-frame arrives.

### saikai.py
- `PickerApp`:
  - `_control_enabled: bool = False`.
  - a **priority `Binding`** (focus-independent, modeled on `?`/F12 `action_mirror_info`) → `action_toggle_mirror_control()` — flip `_control_enabled`; compute `target = (t.title if (t := self._focused_terminal()) else None)` on the UI thread; call `self._mirror_hub.set_control_state(self._control_enabled, target)` (guard when no hub); update a TUI indicator + `notify()`. Define behavior when no pane is focused (toggle still allowed; banner shows "no pane focused").
  - `on_mount`: inside the existing `if _hub is not None` guard, `hub.set_input_handler(...)` with a `_marshal`-shaped callback that does `self.call_from_thread(self._mirror_inject_input, d)` and swallows shutdown errors.
  - `_mirror_inject_input(data)` — re-check `self._control_enabled` (authority); `t = self._focused_terminal()`; `if t is None or t._pty is None or t.is_dead: return`; `try: t._pty.write(data) except Exception: pass`. Runs on the UI thread; mirrors `on_key`'s guard exactly.
  - TUI indicator while control is on.
- Launch wiring (~5975): when a non-loopback (LAN) host is requested, permit input only if `SAIKAI_MIRROR_ALLOW_LAN_INPUT=1`; otherwise input stays loopback-only even though the read mirror is LAN-exposed. When LAN input is allowed, prefer binding to the specific phone-facing LAN IP over `0.0.0.0`. Document a host-firewall rule scoping the port to known device IPs.

### saikai_terminal.py
No change. Reuse `_pty.write` (saikai_terminal.py:842–856 guard/try-except pattern) and `_focused_terminal()` (saikai.py:4714).

## Data flow
- **Output** (unchanged): pane → `MirrorDriver.write` → `broadcast` → default SSE frames → xterm `onmessage`.
- **Write-key:** SSE connect → `event: writekey` → browser memory.
- **Control:** binding → `_control_enabled` (+ target, UI thread) → `set_control_state` → `event: control` → browser.
- **Input:** `onData` → coalesce → single-flight `POST /input` (+ write-key) → gate → `inject` (enqueue) → single drain → `_marshal` handler → `call_from_thread` → `_mirror_inject_input` → `focused_pane._pty.write`.

## Security model
- **Input = RCE-equivalent.** The bar is "withstand a malicious web page the user opens while saikai runs on a Domain-tagged Wi-Fi," not "good enough for a toy."
- **Write-key is the primary credential** — minted per run, delivered only over the authenticated SSE stream, sent in the `X-Mirror-Write-Key` header, compared with `hmac.compare_digest`. It never appears in a URL/QR/file/log, so the read token's leakage does not grant write. Requiring a custom header also forces a CORS preflight that a cross-origin attacker cannot satisfy (the server emits no `Access-Control-Allow-*`).
- **Host allow-list** on every route defeats DNS rebinding (which keeps the attacker's Origin "same-origin" while pointing at the LAN IP).
- **Origin/Referer fail-closed** (present and exactly equal to the server origin; reject absent/`null`/mismatch) as cheap defense-in-depth.
- **Gate:** read-only is the default; control is OFF until toggled (a **local-only** binding — never a browser button), resets OFF on restart, and **idle auto-disables** to bound the exposure window on an unattended machine. Double-gate: `do_POST` fast-rejects on the advisory copy; `_mirror_inject_input` re-checks the authoritative `PickerApp._control_enabled` on the UI thread.
- **LAN exposure:** the home Wi-Fi is Zscaler-Domain-tagged, so "trusted home LAN" is weaker than assumed; LAN input requires its own opt-in and a specific-IP bind, with a documented firewall scope.
- **Rate-limit / lockout** on bad keys and on accepted-input frequency; bound concurrent POST handlers so a flood cannot saturate the UI thread via `call_from_thread`.
- **Focused-pane-only is UX scope, not a security boundary** — an attacker who can POST simply waits for or rides whatever pane the user focuses. Do not broadcast the focused session title while control is OFF (avoid leaking which sensitive context is live). The true human-in-the-loop boundary is the local default-OFF toggle + idle-disable.

## Concurrency & ordering
- **In-order:** one in-flight client POST + a single server drain worker → FIFO into the PTY. (ThreadingMixIn dispatches POSTs on independent threads with no inter-thread ordering; the queue is what guarantees order.)
- **Fire-and-forget:** the drain worker calls a `_marshal`-shaped handler; never a bare `call_from_thread` whose `future.result()` could block the HTTP/drain thread forever during shutdown.
- **Gate reads:** `_control_enabled` is a single bool; cross-thread reads are GIL-atomic (same rationale as `set_repaint_request`). The hub copy is an advisory fast-reject; `PickerApp._control_enabled` (UI-thread re-check) is authoritative, so a brief divergence is safe.
- **Teardown:** `kill()` sets `_pty = None` on the UI thread; `_mirror_inject_input` also runs on the UI thread, so they cannot interleave — the guard (`_pty is None` / `is_dead`) + try/except matches `on_key`. `target` and `self.focused` are read only on the UI thread, never from the hub/HTTP thread.

## Error handling / edge cases
- HTTP/1.1; always drain the request body (even on reject); reject chunked (411); enforce 413 before reading; require JSON; empty `data` → 204 no-op.
- No focused pane → 204 + browser banner "focus a pane on the laptop." Dead pane mid-typing → `_mirror_inject_input` no-ops; push a fresh `control` frame with `target:null` so the phone reflects it. Control toggled off mid-typing → in-flight POSTs get 409, browser flips OFF and stops.
- Astral/emoji input fidelity on the Windows winpty backend is **best-effort** (UTF-16 surrogate handling), not asserted parity; the `try/except` around `_pty.write` contains any `UnicodeEncodeError` from hostile lone surrogates. PTY backends take `str` (not bytes) — do not add `.encode()`.
- Large paste capped at 64 KB (chunk if larger); the write stays on the UI thread (same risk as a local paste).
- Fixed `SAIKAI_MIRROR_PORT` has a first-binder squat risk on a shared host; prefer the ephemeral default unless a firewall rule pins it.

## Testing — `tests/test_mirror_input.py` (+ app-level), repo style (script + `__main__`, no pytest)
- **In-order delivery:** three rapid `inject`/POSTs → stub receives exactly `["a","b","c"]` in order.
- **Write-key:** good → 204; bad → 403; absent → 403; handler not called on failure.
- **Control gate:** on → handler called with exact bytes (204); off → 409, not called.
- **Host allow-list:** allowed Host → ok; foreign Host → rejected (all routes).
- **Origin matrix:** matching → 204; cross-origin → 403; absent-Origin-with-matching-Referer → defined; absent-both → 403; port mismatch → 403.
- **Body validation:** missing `data` → 400; non-str `data` → 400; oversized (`Content-Length` > cap) → 413 without full read; empty → 204; chunked → 411; non-JSON → 400.
- **Double-gate authority:** hub copy stale-ON but app OFF → `_mirror_inject_input` no-ops (app re-check is authority).
- **Teardown race:** `inject` after `hub.stop()` (or handler raising) → no-op, no propagation; `_focused_terminal()` returning a dead/`_pty=None` terminal → no-op.
- **SSE framing (exact wire):** on connect a client receives `event: writekey` (raw JSON) and `event: control` `{"on":false,"target":null}`; after `set_control_state(True,"S")` → `{"on":true,"target":"S"}`; a normal output `broadcast` still arrives via `onmessage` (base64) and does not collide.
- **Concurrency coexistence:** a POST `/input` succeeds while an SSE stream is open (ThreadingMixIn regression).
- **Pilot toggle reachability (skip-guarded):** with a focused pane, the binding flips `_control_enabled` and calls `set_control_state` (stub hub) — catches the leader-unreachable bug.
- **Invariant:** `inject` reaches the PTY only via the handler → `call_from_thread`; never `_pty.write` on the HTTP/drain thread.
- Harnesses available: real `MirrorHub.serve()` on `127.0.0.1` (`tests/test_mirror_hub.py`), `__new__` + `_FakePty` (`tests/test_terminal_concurrency.py`), skip-guarded Pilot (`tests/test_keyboard_leader.py`). Browser JS verified manually on a phone (no browser in CI); assert the page contains the listener/handler strings.

## TDD task breakdown (seed for writing-plans)
1. Hub: input gate + `inject` (no transport) — `_control_enabled`, `_input_handler`, `set_input_handler`, `inject`→bool (no-handler/off/on tests).
2. Hub: FIFO single-drain injection — in-order test.
3. Hub: `POST /input` — write-key + Host + body + status codes.
4. Hub: Origin/Referer fail-closed — Origin matrix.
5. Hub: write-key + control over SSE — `event: writekey`/`event: control` framing, on-connect + on-change, coexistence with output frames.
6. Hub: idle auto-disable + rate-limit.
7. App: `_mirror_inject_input` + double-gate authority — `__new__`/`_FakePty`/stub focus (off/no-pane/dead-pane).
8. App: focus-independent toggle binding + `target` sourcing + `on_mount` `set_input_handler` wiring — skip-guarded Pilot.
9. Browser/JS: `onData`→coalesce→single-flight POST; `writekey`/`control` listeners; banner + disabled-until-on. String-asserted + manual phone check.

## Non-goals / future
WebSocket transport, full UI remote control, multi-pane routing, persisted control state.
