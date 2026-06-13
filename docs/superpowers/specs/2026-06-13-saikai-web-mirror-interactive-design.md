# saikai Web Mirror — Phase B (Interactive Control) Design

**Status:** Approved (2026-06-13)
**Builds on:** Phase A read-only mirror (shipped on master: `saikai_mirror.py` — server-side pyte mirror, base64 SSE frames, token auth, vendored xterm.js).
**Goal:** Let the user operate the *focused* live saikai pane from a browser on the home LAN — typing keystrokes, Ctrl-C, and paste into the running Claude session — gated behind an explicit, default-off control toggle.

## Scope

**In scope (MVP):**
- Send keyboard input (characters, Enter, Ctrl-C, arrows, bracketed paste) from the browser to the **currently focused** live pane's PTY.
- A runtime **control toggle** in saikai (leader-key), default **OFF**, resetting OFF on restart.
- Visible control on/off state in both the TUI and the browser.

**Out of scope (deferred):**
- Driving saikai's own UI from the browser (pane switching, the picker, scrolling) — laptop-only for now.
- Routing input to a non-focused or explicitly selected pane.
- Persisting the control state across restarts.
- A separate write-token (same token as read for MVP).

## Approved decisions
1. **Target:** focused pane only.
2. **Gate:** runtime toggle, default OFF, in-memory (resets on restart).
3. **Transport:** `POST /input` + control state over the existing SSE stream (Approach 1 — all stdlib, no new deps).

## Architecture
`PickerApp._control_enabled: bool = False` is the single source of truth. A leader-key action flips it and pushes the new state to `MirrorHub`, which (a) stores it to gate the input endpoint and (b) broadcasts a control frame to SSE clients.

Input path: browser `xterm.onData` → `POST /input` → `MirrorHub.inject` (gate check) → input handler → `app.call_from_thread(app._mirror_inject_input, data)` → `focused_pane._pty.write(data)`. The PTY write runs on the Textual UI thread and takes no lock, satisfying the "never marshal while holding `self._lock`" and "never close a POSIX ptyprocess on the UI thread" invariants (those concern the lock and the close path, not writes).

## Components

### `saikai_mirror.py`
- `MirrorHub`:
  - new fields `_control_enabled: bool = False`, `_input_handler: Callable[[str], None] | None = None`.
  - `set_input_handler(fn)` — wired at app mount.
  - `set_control_state(enabled, target=None)` — store state + broadcast a control frame to SSE clients.
  - `inject(data) -> bool` — if `_control_enabled` and a handler is set, call `handler(data)` and return True; else False. Called by `do_POST`.
- `_Handler`:
  - `do_POST` — route `/input`: verify token (header `X-Mirror-Token`), verify `Origin`/`Referer` is same-origin, read + size-cap the body, parse `{"data": "..."}`, call `hub.inject`. Responses: 204 accept, 403 bad token, 403 bad origin, 400/413 bad/oversized body, 409 control off.
  - SSE `_stream`: on connect, emit the current control state as a control frame; thereafter `set_control_state` pushes changes. Control frame: an SSE `event: control` record carrying `{"on": bool, "target": "<focused session title|null>"}`.
- HTML/JS (`_PAGE_HTML`):
  - `term.onData(d => queueInput(d))` with coalescing (~20–30 ms flush; flush immediately on `\r`/control bytes); send via `POST /input` with the token header.
  - SSE `control` listener → enable/disable `term` input, toggle a **CONTROL ON/OFF** banner, and show "typing into: ⟨session title⟩".
  - Keyboard stays disabled until a control frame reports on.

### `saikai.py`
- `PickerApp`:
  - field `_control_enabled: bool = False`.
  - binding → `action_toggle_mirror_control()` — flip `_control_enabled`, call `self._mirror_hub.set_control_state(...)` (guard when no hub), update a TUI indicator, and `notify(...)` a toast. Leader-key: an unused letter (final choice fixed in the plan).
  - `on_mount`: if a hub is present, `hub.set_input_handler(lambda d: self.call_from_thread(self._mirror_inject_input, d))`.
  - `_mirror_inject_input(data)` — re-check `self._control_enabled` (authority); `term = self._focused_terminal()`; if alive, `term._pty.write(data)` in try/except. Runs on the UI thread.
  - TUI indicator shown while control is on (footer/title marker).

### `saikai_terminal.py`
- No change. Reuse `_pty.write` (saikai_terminal.py:852) and `_focused_terminal()` (saikai.py:4714).

## Data flow
- **Output** (unchanged): pane → `MirrorDriver.write` → `hub.broadcast` → SSE → xterm.
- **Control state**: leader-key → `_control_enabled` → `hub.set_control_state` → SSE control frame → browser (enable keyboard + banner + target hint).
- **Input**: `xterm.onData` → `POST /input {data}` (+ token header) → `do_POST` gate → `hub.inject` → handler → `call_from_thread` → `_mirror_inject_input` → `focused_pane._pty.write`.

## Security model
- Read-only remains the default; control is OFF until toggled and **resets OFF on restart** (in-memory only, never persisted).
- **Token**: required on `POST /input`, carried in the `X-Mirror-Token` header (kept out of URLs/logs); constant-time compare (`hmac.compare_digest`); same token as read.
- **Origin/Referer check**: reject POSTs whose `Origin` (or `Referer` host) is not the mirror's own origin. The token — which a cross-site attacker cannot know — is the primary defense; the Origin check is defense-in-depth.
- **Double gate**: `do_POST` rejects when control is off (fast), and `_mirror_inject_input` re-checks before writing (authority). The browser also disables its keyboard when off (UX).
- **Blast radius**: input reaches only the focused live pane; it cannot navigate saikai or reach other panes. LAN bind unchanged. Both ends show a visible control indicator — control is never silently on.

## Error handling / edge cases
- No focused pane / dead pane → input silently dropped (204; optionally a browser hint).
- Control-off race (browser thinks on, server off) → 409; browser already disabled.
- **Concurrency**: SSE (long-lived GET) must coexist with POST → the server must be threading (`ThreadingHTTPServer`); verify/ensure during implementation (Phase A already serves SSE + page loads concurrently, so this likely holds — confirm).
- Coalescing: batch `onData` within ~20–30 ms, flush immediately on Enter/control bytes; cap POST body size (e.g., 64 KB); malformed/oversized → 400/413.
- `call_from_thread` guarded against app-shutdown (try/except), mirroring the existing `_marshal` pattern.

## Testing
`tests/test_mirror_input.py` (new), Phase A style (script + `__main__`, no pytest):
- POST + good token + control ON → `inject` calls the handler with the exact bytes; 204.
- Bad token → 403, handler not called.
- Control OFF → 409, handler not called.
- Cross-origin `Origin` → rejected, handler not called.
- `set_control_state(True/False)` broadcasts a control frame an SSE client receives.
- `inject` is a no-op (returns False) when no handler is set.
- Invariant: `inject` reaches the PTY only via the handler (which marshals through `call_from_thread`); it never calls `_pty.write` directly on the HTTP thread.
- App-level (best-effort, pilot-style): `_mirror_inject_input` writes to a stub focused terminal when control on; no-op when off or no focused pane.

## Non-goals / future
WebSocket transport, full UI remote control, multi-pane routing, a separate write-token, and persisted control state are all deferred — each can layer on top of Approach 1 later.
