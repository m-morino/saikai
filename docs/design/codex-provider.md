# Codex provider — Codex CLI threads in the one list (0.6 goal)

Status: **C1 implemented** on branch `feature/codex-provider` (list + search +
preview + `Enter` resume in a live pane). Attention / live-state are C2.

## Verified facts (codex-cli 0.144.1, real data, 2026-07-12)

On-disk layout under `$CODEX_HOME ?? ~/.codex`:

- `sessions/YYYY/MM/DD/rollout-<ts>-<uuid>.jsonl` — one file per rollout.
  Line 1 is `session_meta`: `{id, cwd, timestamp, source, originator, git,
  instructions}`. A RESUMED rollout adds `session_id` (= the ROOT thread id)
  and `parent_thread_id` — codex's resume writes a NEW file, so a thread is a
  CHAIN of rollouts sharing a root.
- Clean user turns are `event_msg {type:"user_message", message}` records.
  The `response_item` user messages carry AGENTS.md / permissions preambles
  and must NOT feed search/preview.
- `session_index.jsonl` = `{id: root thread id, thread_name, updated_at}` —
  sparse (only some threads get names) but free titles where present.
- `history.jsonl` = `{session_id, ts, text}` — user prompts across sessions
  (not used by C1; rollouts are authoritative).
- `source` classifies a thread: `"cli"` / `"vscode"` / `"exec"` are user
  threads; `{"subagent":{"thread_spawn":{parent_thread_id, agent_nickname,…}}}`
  is codex's own agents feature (mapped onto saikai's existing lineage
  fields); `{"subagent":{"other":…}}` (guardian approval assessors) is
  internal noise → excluded, and the exclusion is disk-cached.

Resume semantics (PTY-probed):

- `codex resume <root-id>` loads the thread's **latest** state across chained
  rollouts (the index's `updated_at` also tracks the newest chain link).
- codex prompts "choose working directory" unless the process cwd matches the
  recorded cwd — saikai spawns the pane from the recorded cwd when it still
  exists, so the prompt is skipped; a vanished cwd surfaces the prompt INSIDE
  the pane (survivable by design).
- Resuming boots the thread's MCP servers; no model call until input is sent.

## C1 wiring (what landed)

- `load_codex_sessions` folds rollouts into one row per thread root; rows are
  ordinary session dicts with `provider="codex"`, so favorites, hide, rename,
  search, preview, panes and the web mirror work unchanged. In a MIXED list
  every row carries its provider glyph (`✻` claude / `⌬` codex — both
  East-Asian-width Neutral, so 1 cell even on CJK terminals); an all-claude
  list stays unbadged.
- `_build_resume_invocation` dispatches by the row's provider — panes get the
  `codex` status classifier via the provider registry (see C2 below).
- Freshness: new rollouts bump their day dir → `_codex_dirs_mtime()` stats the
  root + newest year/month/day chain; appends to listed files ride the
  existing `_sid_index` jsonl-stat layer.
- Scope: `--all` lists every thread; `--here` keeps threads whose cwd is
  inside the current repo.
- Gated OFF for codex rows (honest, not broken): `!` attention, LLM titling
  (`claude -p` must not bill codex threads), checkpoint (injects claude's
  `/handoff`+`/clear`), live/open detection (codex has no pid registry and
  `codex resume`'s argv lacks the sid → no reliable pid↔thread mapping).

## C2 — Claude and Codex in parallel (2026-10-22, codex-cli 0.163 / rollouts 0.157)

- **Rollout format change, measured on real data.** Rollouts written by codex
  0.157 (Codex Desktop) contain NO `event_msg/user_message`. The clean human
  turn is `event_msg/item_completed` with `item.type == "UserMessage"` and
  `content[].text`; like the old event it carries none of the AGENTS.md /
  environment preamble. The parser reads the legacy form and, only when that
  is empty, the new form (never both, so a version emitting both cannot double
  every prompt). `_CODEX_CACHE_SCHEMA` invalidates caches written before this.
- **Status profile `codex`** (`classify_codex_status`), pinned to screens
  captured from the real 0.163 TUI (`tests/fixtures/codex_screens_0163.json`):
  waiting = a numbered option under the selection cursor (`› 1. Yes, proceed
  (y)` for command approval, `› 1. Trust and continue` for folder trust);
  busy = `Working (Ns • esc to interrupt)`; idle otherwise (`Worked for 7s`).
  The generic classifier it replaces called ANY two numbered lines a forced
  choice — a finished answer with a list sat at "needs you" forever — and
  accepted "Would you like", which Codex also uses to close a normal answer.
- **New codex session.** `Shift+F8` → `Shift+F8` again switches the agent.
  Codex cannot be handed a thread id, and writes no rollout until the first
  prompt (measured: none after 12s idle). The pane opens under a
  `codex-new-<hex>` placeholder with a provider=codex stub row (inserted BEFORE
  the spawn so the pane gets the codex classifier); a 2s poll runs the codex
  scan off the UI thread and `_match_new_codex_thread` re-keys the pane onto
  the ONE thread in that cwd started after the launch and not already listed —
  two candidates → refuse rather than guess. A placeholder is never persisted
  to the restore set. E2E: re-keyed 2s after the first prompt.
- **Session memory isolation.** Codex caches into `parsed/codex-<uuid>.json`;
  session memory reads that directory for deleted CLAUDE sessions, so without
  `_is_claude_record_file` every codex thread reappeared as a fake expired
  claude row, and the sweep kept codex caches forever.

## C3 candidates

- Attention marker from the rollout tail (`task_complete` vs `user_message`).
- Codex agent threads (`thread_spawn`) folded under their parent like claude
  agents; guardian visibility as an opt-in.
- Live/open detection via `app-server-control.sock` (if the app-server exposes
  a thread list) — speculative, needs a protocol probe.
- New-codex-session (`n`) — codex cannot preassign a thread id
  (`can_preassign_id=False`), so row-linking needs a scan-back heuristic.
