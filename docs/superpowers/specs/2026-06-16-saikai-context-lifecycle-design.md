# saikai Context-Lifecycle Assistant — Design

**Goal:** Make Anthropic's recommended "keep context lean + reset on purpose"
workflow frictionless, so a user stops riding auto-compact on a bloated session.
saikai (a) shows how full each live session's context is and nudges before the
bloat zone, (b) turns "checkpoint the grown state -> clear -> rehydrate lean" into
one action, and (c) tracks session lineage as a navigable tree.

**Architecture:** saikai is an *external orchestrator*, not a context manager --
Claude Code owns the window (`/clear`, `/compact`, `/context`). saikai estimates
context fill from the transcript JSONL, drives standard commands into the live
pane via the existing key-injection path, and records parent->child lineage in a
small sidecar. No `claude -p` (separately billed); the handoff is written by the
*already-running* session.

**Tech stack:** Python/Textual (saikai.py, saikai_terminal.py), sidecar JSON in
`~/.cache/saikai/`, no-pytest tests (`uv run python tests/test_x.py`).

---

## Motivation & best-practice grounding (verified)

Sources: code.claude.com/docs/best-practices, /context-window, /memory;
anthropic.com/engineering/effective-context-engineering-for-ai-agents.

- Anthropic does **not** blanket-prefer `/clear` over `/compact`. `/clear` =
  between *unrelated* tasks / after repeated corrections; `/compact` = same task
  continuing (auto-compact near the limit is standard).
- Core principle: **context is a finite resource; optimize for the smallest set
  of high-signal tokens.** Noise (failed attempts, stale files) dilutes signal.
- Biggest levers: **subagents** (isolated context), **CLAUDE.md / memory**
  (persistent), then `/clear` at task boundaries. `/context` shows live usage.
- The user's real pain: clearing + rehydrating the *grown* working state is
  laborious, so they avoid it and ride auto-compact (degraded). This feature
  removes that friction.

**Honest boundaries (non-goals):** saikai cannot read live context usage from an
API (none exists) -- it *estimates* from the transcript. saikai does not manage
the window; it nudges + orchestrates standard commands. An "offload to subagent"
nudge is out of scope (future).

---

## Component (a): Context-fill gauge + proactive nudge

**Files:** saikai.py (new pure fns + statusbar/list wiring + poll hook).

- **Estimate** per live session: `est_tokens ~= STARTUP_OVERHEAD + transcript_tokens`,
  where `transcript_tokens ~= sum(len(text_content)) / 4` (chars-per-token
  heuristic; no tokenizer dependency) and `STARTUP_OVERHEAD ~= 10_000` (system
  prompt + tools + CLAUDE.md, roughly constant). Pure fn
  `_estimate_ctx_tokens(jsonl_path)`.
- **Window** per session: detect the model from the transcript (assistant turns
  carry a model id) -> map known model -> window (200_000 default; 1_000_000 for
  `*[1m]` / Opus extended-context). Fall back to `SAIKAI_CTX_WINDOW` (default
  200_000). Pure fn `_ctx_window_for(model)`.
- **Gauge**: `pct = est_tokens / window`. Surface for the focused live pane in the
  statusbar next to the RAM segment, e.g. `ctx ~96K/200K (48%)`, severity-coloured
  in green/yellow/red bands (same shape as `_load_severity`).
- **Nudge threshold**: `SAIKAI_CTX_NUDGE_PCT` (default 0.55 -- comfortably before
  the ~70-100K auto-compact zone on a 200K window). When a focused live session
  crosses it, a once-per-crossing toast (hysteresis like the memory-pressure
  toast): "context ~55% -- checkpoint & refresh (Shift+F11) to stay lean".
- **Cadence**: reuse `_poll_live_status` (1.5s). Estimate is cheap (byte/4 over the
  transcript; cache by mtime like other parses).
- **Caveat (documented):** estimate only -- excludes the exact system/tool token
  cost; for the precise breakdown the user runs `/context` in the pane.

**Tests:** `_estimate_ctx_tokens` on a fixture JSONL (known content -> expected
band); `_ctx_window_for` mapping (200K default, 1M for `[1m]`); the gauge string
formatter (pure) for green/yellow/red + the K/window text.

---

## Component (b): One-action checkpoint -> clear -> rehydrate

**Files:** saikai.py (new `action_context_refresh` + orchestration helper),
binding, mirror key-bar button.

Triggered by **Shift+F11** (`action_context_refresh`, priority + in
`_MODAL_BLOCKED_ACTIONS`) on a focused live pane, or from the (a) nudge toast.
Sequence, driven into the live pane via the existing key/text injection -- all
standard commands, no `claude -p`:

1. **Write a high-signal handoff (in-session).** saikai types a fixed prompt:
   "Write a concise handoff for a fresh session to the file
   `~/.cache/saikai/handoff/<sid>.md` -- goal, current state, key files+decisions,
   next steps, open threads -- then reply DONE. Write nothing else." The running
   session (already billed; full context) authors it. **File-based capture** so
   saikai never parses terminal output. (Alternative to evaluate: the standard
   `/handoff` skill -- VERIFY its output target; default to the explicit
   file-write prompt, which is robust regardless.)
2. **Wait for the file** (`<cache>/handoff/<sid>.md`) with a timeout (e.g. 60s,
   poll 0.5s). On timeout: abort with a toast, change nothing (safe).
3. **`/clear`** typed into the pane (standard -> new session id, same pane, fresh
   context; old transcript preserved on disk).
4. **Detect the child sid.** After `/clear`, scan the pane's cwd dir under
   `~/.claude/projects/<enc>/` for the newest transcript that is not the parent
   (mtime-ordered, short retry to cover the race). That sid = the child.
5. **Rehydrate (seed) the fresh session**: type "Continue the work. Read
   `~/.cache/saikai/handoff/<parent_sid>.md` for context, then proceed." The new
   session starts **lean + high-signal** (reads a small handoff, not the full
   transcript).
6. **Record lineage** (Component c): `parent_sid -> child_sid`.

**Why `/clear`-in-place (not launch-new):** honours the user's "clear" + stays in
one pane; the cost is child-sid detection (step 4), handled by the
newest-transcript scan + retry. Launch-a-new-pane was considered (saikai would own
the new sid directly) but spawns a second ~400 MB claude and reads less like
"clear". Documented as the considered alternative.

**Failure handling:** any step failing leaves the session untouched (handoff file
+ old transcript persist); a toast reports which step failed. `/clear` is only
typed *after* the handoff file exists (never lose state).

**Tests:** the sequence builder is a pure fn returning the ordered steps (assert
the handoff-write prompt, that the wait-for-file gate precedes `/clear`, that the
rehydrate prompt references the parent handoff path); a Pilot test with a stub
pane recording injected text asserts the order + that `/clear` is not sent until
the (mocked) handoff file appears; child-sid detection on a fixture dir.

---

## Component (c): Lineage tree

**Files:** saikai.py (sidecar read/write + group-by + a marker), reuse group-by.

- **Sidecar** `~/.cache/saikai/lineage.json`:
  `{ child_sid: {"parent": parent_sid, "ts": iso8601} }`. Helpers
  `_load_lineage()` / `_set_lineage(child, parent)` (mtime-cached, atomic write --
  same pattern as custom-titles.json).
- **Display**: a new **group-by "Lineage"** option (reuse the Group dropdown /
  leader group cycling) that nests a child under its parent with a `>` indent
  marker; and a `_list_title` suffix `(from <short parent>)` otherwise. Sessions
  with no lineage render flat (unchanged).
- **Navigate**: a key / context-menu item "open parent" jumps the cursor to the
  parent sid (deep-dive the grown session if the lean one lacks detail).
- **Mirror**: lineage is list data -> already mirrored; the (b) trigger gets a
  "Refresh ctx" button on the mirror key-bar (data-k="shift+f11").

**Tests:** lineage sidecar round-trip + mtime cache invalidation; group-by
"Lineage" nests child under parent (pure grouping fn on a fixture session set);
the "(from ...)" title suffix.

---

## Data, config, build order

- **Cache**: `~/.cache/saikai/handoff/<sid>.md`, `~/.cache/saikai/lineage.json`
  (repo never touched -- the "do not pollute" requirement).
- **Config/env**: `SAIKAI_CTX_WINDOW` (200000), `SAIKAI_CTX_NUDGE_PCT` (0.55),
  `SAIKAI_CTX_HIGH_PCT` (0.75), mirrored under a `[context]` config table.
- **Build order (for the plan):** (a) gauge [independent] -> (c) lineage
  sidecar+tree [needed by b's record/display] -> (b) orchestration [uses c]. Each
  is independently testable and shippable.

## Open items to verify during implementation (decisions made; verification noted)

1. The standard `/handoff` skill's output target -- default to the explicit
   file-write prompt regardless; evaluate `/handoff` as a nicety only.
2. The transcript's model-id field name (for window detection) -- fall back to
   `SAIKAI_CTX_WINDOW` if absent.
3. Child-sid detection race after `/clear` -- newest-transcript scan + short
   retry; verify against a real `/clear` in a live pane during implementation.
