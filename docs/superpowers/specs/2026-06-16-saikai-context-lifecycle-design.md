# saikai Context-Lifecycle Assistant — Design (rev. 2, post expert review)

**Goal:** Help the user stay in the optimal context band and reset *on purpose*
instead of riding auto-compact on a bloated session — by (a) showing, at a glance
across all live panes, how full each session's context really is, and (b) making
"compact / checkpoint-and-refresh" a one-key, *safe* action. saikai surfaces the
signal and orchestrates standard commands; Claude Code owns the window.

**Status:** Revised after a 3-reviewer expert panel (Anthropic context-engineering,
Textual/concurrency, product/UX). The panel found rev.1 rested on a false premise
and an unsafe centerpiece; this revision fixes both. Key changes are marked **[rev2]**.

**Tech stack:** Python/Textual (saikai.py, saikai_terminal.py), sidecar JSON under
`CACHE_DIR`, no-pytest tests (`uv run python tests/test_x.py`).

---

## Motivation & verified grounding

- Anthropic does **not** blanket-prefer `/clear` over `/compact`. `/clear` = between
  *unrelated* tasks / after repeated corrections; `/compact` = same task continuing
  (auto-compact near the limit is standard). Proactive compaction at ~50-60% is the
  team's rule of thumb. Core principle: optimize for the **smallest set of
  high-signal tokens**; biggest levers are subagents + CLAUDE.md/memory, then
  resetting at task boundaries.
- User's real pain (verbatim): resetting + rehydrating a *grown* session is
  laborious, so they avoid it and ride auto-compact (degraded). Friction, not
  ignorance — so the high-value piece is making the reset itself trivial + safe.

**[rev2] CORRECTED premise — token counts are already on disk.** Each assistant
turn in the transcript JSONL carries a `usage` block (`input_tokens`,
`cache_read_input_tokens`, `cache_creation_input_tokens`, `output_tokens`); the
**last** such turn's `input + cache_read + cache_creation` is the *exact* live
context size (what `/context` shows). It also records
`{"type":"system","subtype":"compact_boundary","compactMetadata":{"trigger","preTokens"}}`.
Empirically (this repo's session): real **662,561** tokens vs rev.1's chars/4
estimate **965,025** (1.46x over). So the rev.1 estimator is deleted: **read the
real number; never estimate when `usage` is present.**

**Honest boundaries / non-goals:** saikai cannot *manage* the window except by
driving the pane. It reads ground-truth fill, surfaces it, and orchestrates
standard commands. An auto-nudge and a navigable lineage *tree* are **deferred**.

---

## Component (a) — Multi-pane context-fill gauge  [SHIP FIRST; the real unmet need]

`/context` is per-session; saikai uniquely sees *all live panes at once*.

**Files:** saikai.py (new pure fns + statusbar wiring + reuse the poll).

- **Ground-truth read** `_ctx_tokens_from_jsonl(path) -> int | None`: scan to the
  **last** assistant record bearing `usage`; return
  `input_tokens + cache_read_input_tokens + cache_creation_input_tokens`. None if no
  `usage` (old transcripts) -> caller may fall back to a clearly *labelled* chars/4
  estimate, but prefers the real number. **[rev2] no STARTUP_OVERHEAD, no chars/4 as
  the primary path.**
- **Compaction awareness:** the last turn's `input_tokens` already reflects the
  post-compaction window, so reading the last `usage` is correct across compactions.
  Optionally surface `compact_boundary` (e.g. "auto-compacted at 1.0M").
- **Window** `_ctx_window_for(model) -> int`: model id is on each assistant turn
  (`message.model`); map family -> window (200_000 default; **1_000_000 when the id
  carries the `[1m]` suffix**). Fallback `SAIKAI_CTX_WINDOW` (200_000).
- **Display** — a NEW pure formatter `_ctx_gauge_segment(tokens, window)`,
  **per-focused-pane** (NOT folded into the aggregate `_live_ram_segment`). In the
  statusbar builder, when `_focused_terminal()` is not None, resolve
  `.sid -> _sid_index[sid]["jsonl_path"]`, compute, append e.g. `ctx 662K/1.0M (66%)`,
  severity-coloured by reusing `_load_severity` / `_LOAD_COL`. **[rev2] with real
  numbers the percentage is trustworthy** (the panel's "band not number" advice was
  contingent on the estimate being wrong; it no longer is).
- **Cadence/caching:** reuse `_poll_live_status` / `_restat_live`; cache on
  `(mtime, size)`. UI-thread read-only; touches no lock.

**Tests:** `_ctx_tokens_from_jsonl` on a fixture with `usage` blocks (last turn's
sum; None when absent); `_ctx_window_for` (200K default, 1M for `[1m]`);
`_ctx_gauge_segment` formatting + green/yellow/red bands.

---

## Component (b) — One-key refresh, made SAFE  [rev2: autonomous /clear REMOVED]

**[rev2]** rev.1's "type a prompt -> trust a file -> autonomously `/clear`" was
data-loss-capable (existence-check != validity; mid-task injection; fire-and-forget,
no read-back) and reinvented `/compact`/`/handoff`/`/rewind`. Replaced by two modes,
neither clearing autonomously.

**Common gates (both modes):**
- Trigger: **Shift+F11** (`action_context_refresh`, priority, in
  `_MODAL_BLOCKED_ACTIONS`) on a focused live pane; or a mirror key-bar button.
- **Idle gate:** no-op + toast if the pane is mid-turn (streaming / tool running /
  awaiting a permission prompt). Reuse the pane's busy/idle status. Never inject into
  a busy pane.
- **Injection:** new `AgentTerminal.paste_text(s)` + `submit()` wrapping the body in
  bracketed paste (`ESC[200~ ... ESC[201~`), **gated on `_bracketed_paste`**, then one
  CR. **Never per-line enter keys** (each CR would submit a fragment). PTY writes
  stay on the UI thread.

**(b1) Compact — DEFAULT, non-destructive, zero data-loss.** Shift+F11 injects
`/compact` (standard, in-place). ~80% of the value at ~5% of the risk. The everyday
"stay lean" action.

**(b2) Checkpoint & fresh-start — OPT-IN, human-gated, for deliberate task
boundaries.** A tick-driven state machine (NOT a blocking wait):
1. Inject the existing **`/handoff` skill** (anchor on it: ~80-line cap, strips
   failed-attempt noise, emits a paste-ready `NEW SESSION PROMPT` block, ends with
   "/clear して"). **[rev2] /handoff is the building block, not a bespoke prompt.**
2. **Detect completion via the transcript** (saikai already parses these): when a new
   assistant turn appears and the pane is idle, read its last message and extract the
   `NEW SESSION PROMPT` block. No bespoke cache file, no terminal scraping.
3. **Human gate:** show the extracted prompt in a modal — "Refresh? old session stays
   reopenable. [Enter] clear+reseed / [Esc] cancel." **saikai never types `/clear`
   autonomously.**
4. On confirm: inject `/clear`; detect the child sid (see spike); inject the
   `NEW SESSION PROMPT` to reseed lean; record the recovery pointer (c).

**[rev2] SPIKE FIRST (task zero of b2):** verify in a *real* live pane that `/clear`
mints a new session-id detectable on disk, and how reliably the child sid emerges,
BEFORE building b2. Child detection must be falsifiable: capture the pre-existing sid
set; bind the *first new* sid whose first record's cwd matches the pane and whose
timestamp post-dates the clear; if 0 or >=2 candidates, **record nothing + toast**
(sibling panes / `claude -p` transcripts in the same project dir are real
contaminants).

**Failure-safety:** b1 is non-destructive. b2 `/clear`s only after the human confirms
a *shown* handoff; state is never *lost* (old transcript persists + the recovery
pointer). Honest boundary: after `/clear` the session IS mutated; recovery is via
"reopen previous", not in-place.

**Concurrency [rev2, blocker-grade]:** the b2 wait/poll MUST be a tick-based state
machine (off `_poll_live_status` or a self-cancelling `set_interval`), **never a
blocking UI-thread loop** — reader threads block on `call_from_thread` to the UI
thread, so a blocking wait freezes every pane. Run
`tests/test_terminal_concurrency.py` + `test_pty_backend.py` after b.

**Tests:** a pure sequence-builder for b2 (order: handoff -> detect -> gate -> clear
-> reseed; clear gated behind human-confirm); a Pilot test with a stub pane recording
injected text (idle-gate no-op when busy; b1 injects `/compact`; b2 does not inject
`/clear` before confirm); bracketed-paste helper unit test.

---

## Component (c) — Recovery pointer only  [rev2: tree CUT]

**[rev2]** The navigable lineage *tree* (group-by "Lineage", indent, title suffix,
mirror tree) was scope-creep for this pain and clashes with saikai's group-by (closed
enum + flat partition, mutually exclusive with tree-mode). **Cut.** Keep only the
recovery net that makes a deliberate `/clear` safe.

- **Sidecar** `CACHE_DIR/lineage.json`: `{ child_sid: {"parent": sid, "parent_jsonl":
  path, "ts": iso8601} }`. Reuse the custom-titles pattern (`_read_json`/`_write_json`,
  mtime-cached, atomic, strict-read). **Use `CACHE_DIR`, not a hardcoded `~/.cache`.**
- **One action:** "reopen previous / open parent" — from a b2 child, jump the cursor
  to (or open) the parent for deep-dive when the lean handoff misses a detail. One
  binding; no tree UI.

**Tests:** lineage sidecar round-trip + mtime-cache invalidation; "open parent"
resolves child -> parent.

---

## Deferred (explicitly out of scope here)

- **Auto-nudge toast** — parasitic on (b) (the user knows it's heavy; a nudge only
  helps if acting is trivial). Ship after b1 exists + is trusted; then it triggers the
  same Shift+F11. Threshold ~55% (aligned with 50-60% proactive-compact), window-aware,
  per-sid hysteresis (clone the memory-pressure toast pattern).
- **Lineage tree / group-by "Lineage" / mirror tree** — a future session-management
  feature with its own justification.
- **Bespoke handoff prompt / `~/.cache/saikai/handoff/*.md` protocol** — dropped in
  favour of the existing `/handoff` skill.

---

## Config & build order

- **Config/env:** `SAIKAI_CTX_WINDOW` (200000). (Nudge thresholds deferred with the
  nudge.)
- **Build order:** **(a) gauge [standalone MVP, ground-truth usage]** -> **(c)
  recovery pointer sidecar** -> **(b1) `/compact` one-key** -> **/clear spike** ->
  **(b2) human-gated checkpoint** (only if the spike is positive). Each step is
  independently testable + shippable; the destructive b2 is last and gated on the
  spike.

## Open items resolved / to verify

1. `/handoff` output: anchor on it; detect completion + extract the `NEW SESSION
   PROMPT` block from the transcript's last assistant message (no bespoke file).
2. model-id field for window detection: `message.model`; fall back to
   `SAIKAI_CTX_WINDOW`.
3. **`/clear` new-sid behaviour: a real-pane SPIKE is task zero of b2** — load-bearing;
   a negative result reshapes b2 + (c).
