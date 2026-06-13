# saikai Web Mirror — Phase C (Tap + Keys → saikai's own UI) Design

**Status:** Approved (2026-06-14).
**Builds on:** Phase B interactive control (branch `feat/web-mirror-interactive` / PR #1): `POST /input` → focused-pane PTY, header-only write-key, Host allow-list, Origin fail-closed, default-OFF control toggle + idle auto-disable, LAN-input opt-in, `MirrorDriver`, vendored xterm.js.
**Goal:** Let a phone **drive saikai's own Textual UI** from the mirror — tap to click (select a session, sort a column, focus a pane), swipe to scroll, and an on-screen **key bar** for keys a soft keyboard can't produce (leader, Esc, Tab, arrows, Ctrl, F12) — by injecting **synthesized Textual events into saikai's app** so it routes them natively.

## Scope
**In:** tap→click (MouseDown+Up → the App synthesizes the Click); swipe→scroll (MouseScrollUp/Down); an on-screen key bar (Leader, Esc, Tab, ↑ ↓ ← →, Ctrl-modifier, F12) → `events.Key`. All injected into saikai's app, behind the existing control gate.
**Out (deferred):** divider-drag (MouseMove sequences); arbitrary full-keyboard remap; routing plain typed text through the app (text keeps the Phase B pane-PTY path).

## Mechanism (from investigation, Textual 8.2.7)
Synthesize Textual events and `app.post_message(...)`; do **not** feed raw bytes (the byte→event parser is Windows-blocked — the win32 driver reads console records, no SGR parser, so mechanism A is dead cross-platform).
- **Click:** `post_message(events.MouseDown(None, x=col, y=row, 0, 0, button, False, False, False, screen_x=col, screen_y=row))` then the paired `MouseUp`. `App.on_event` (app.py:4060-4119) hit-tests `get_widget_at(x, y)` and **synthesizes the Click itself** → DataTable header-sort, row selection, pane focus — all for free.
- **Scroll:** `post_message(events.MouseScrollUp/Down(None, x, y, 0, 0, 0, False, False, False, screen_x=x, screen_y=y))` (button 0).
- **Key:** `post_message(events.Key(key, character))` — the App routes to priority bindings / the focused widget (the same path Textual's `Pilot.press` uses). saikai's leader / F-keys / pane keys all dispatch natively.
- **Coordinates:** 0-based screen cells; browser cell `(col,row)` → `(x=col, y=row)`. xterm reports 1-based → subtract 1.
- **Thread-safety:** inject on the UI thread via `app.call_from_thread(...)` from the drain thread, guarded (`is_running` + try/except) exactly like Phase B's input handler. Never marshal while holding a lock; `post_message` + downstream run on the loop thread.

## Components
### `saikai_mirror.py`
- `MirrorHub`: add `_mouse_handler=None`, `_key_handler=None`; `set_mouse_handler(fn)`, `set_key_handler(fn)`; `inject_mouse(col,row,button,kind)` and `inject_key(key)`. These enqueue **typed items** onto the existing `_inject_q` (e.g. `("mouse", col,row,button,kind)`, `("key", key)`; Phase B keyboard becomes/stays `("input", data)` or a bare `str`). The single `_inject_loop` dispatches by item type to the matching handler — preserving **global FIFO order** across keyboard / mouse / key.
- `_Handler`: `do_POST` routes `/mouse` and `/key` behind the **same gate** as `/input` (Host allow-list + write-key + Origin fail-closed + control-on). `/mouse` body `{col,row,button,kind}` with `kind ∈ {down,up,scrollup,scrolldown}`; `/key` body `{key}` (e.g. `"escape"`, `"tab"`, `"up"`, `"f12"`, `"ctrl+c"`, `"space"`). Validate types/ranges; responses 204/400/403/409.
- `_PAGE_HTML` JS: in `onData`, split — an SGR mouse sequence (`\x1b[<b;col;row` + `M`/`m`) → parse → `POST /mouse`; otherwise → `POST /input` (Phase B keyboard, unchanged). Add the **key bar**: fixed-position on-screen buttons (Leader, Esc, Tab, ↑ ↓ ← →, Ctrl, F12) → `POST /key`; Ctrl is a sticky modifier (the next key/tap is ctrl-combined). Single-flight per endpoint; `X-Mirror-Write-Key` header; disabled until a `control` on-frame; 409/403 reactions mirror Phase B.
### `saikai.py`
- `PickerApp._mirror_inject_mouse(col,row,button,kind)`: re-check the authoritative `_control_enabled`; `post_message(MouseDown then MouseUp)` for a click, or `MouseScrollUp/Down` for scroll. UI thread; guarded; coords clamped to the screen.
- `PickerApp._mirror_inject_key(key)`: re-check `_control_enabled`; build `events.Key(key, character)` and `post_message` it. UI thread; guarded; unknown key → ignore.
- `on_mount`: inside the existing `if _hub is not None` block, wire `hub.set_mouse_handler(...)` and `hub.set_key_handler(...)` with guarded `call_from_thread` closures (same shape as the Phase B input handler).

## Data flow
- **Tap:** `onData` mouse SGR → `POST /mouse` → `inject_mouse` → typed `_inject_q` → single drain → `call_from_thread` → `_mirror_inject_mouse` → `post_message(MouseDown/Up | Scroll)` → App hit-tests + dispatches (Click synthesized).
- **Key bar:** button → `POST /key` → `inject_key` → drain → `_mirror_inject_key` → `post_message(events.Key)` → App routes (priority binding / focused pane).
- **Text typing (unchanged):** `onData` keyboard → `POST /input` → `_mirror_inject_input` → focused-pane PTY.

## Security
Reuses Phase B's control gate **verbatim** — default OFF, Shift+F12 toggle, idle auto-disable, LAN-input opt-in, write-key, Host allow-list, Origin fail-closed. Mouse + keys are gated identically (control-off → 409). No exposure beyond "operate the UI you already mirror" — the explicit intent, behind the one gate.
- *Note:* the control-toggle key's reliability is terminal-dependent (Shift+F12 works on this Windows console; on terminals that fold Shift+F-keys to F13–F24 it would not). A more robust toggle (e.g. an unshifted priority F-key or the command palette) is a **Phase B follow-up**, out of scope here.

## Error handling / edge cases
- control off → 409; malformed/oversized body → 400/413; out-of-range cell → clamp or ignore; a dropped `MouseUp` is harmless (saikai handlers release on their own Up); unknown key name → ignore (never post a garbage `Key`).
- `events.Key`/`events.Mouse*` construction must match Textual 8.2.7 field shapes — verify against `events.py` during the build (the investigation captured the signatures + the 0-based coordinate convention).
- **Ordering:** the single FIFO `_inject_loop` preserves keyboard/mouse/key order (a tap-then-type sequence arrives in order).
- Divider-drag is explicitly out — no `MouseMove` events are injected.

## Testing
- **Hub:** `inject_mouse`/`inject_key` enqueue typed items and the single drain dispatches each to the right handler **in submission order**; `/mouse` + `/key` gate matrix (Host / write-key / Origin / control-on → 204/400/403/409); coordinate pass-through (1-based browser → 0-based).
- **App:** `_mirror_inject_mouse` posts MouseDown+Up (click) / MouseScroll (scroll) at the right 0-based coords; `_mirror_inject_key` posts `events.Key`; both **no-op when control is OFF** (double-gate) — via `__new__` + a stub recording `post_message`.
- **Pilot (skip-guarded):** a synthesized click on a session-row cell selects it; a synthesized click on a column header fires the sort (`HeaderSelected`); a synthesized `events.Key` fires a priority binding. Confirms saikai routes injected events to the right widget.
- **Browser:** page JS routes mouse SGR → `/mouse` and key-bar buttons → `/key` (string + logic asserts); the key-bar buttons are present in the served page. Manual phone check: tap a session row → selects; tap a column header → sorts; swipe → scrolls; tap Leader/Esc/arrows → saikai responds.

## Non-goals / future
Divider-drag; full-keyboard remap; routing text through the app; a more robust control-toggle key (Phase B follow-up).
