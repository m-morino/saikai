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
  search, preview, panes and the web mirror work unchanged. `◇` title badge.
- `_build_resume_invocation` dispatches by the row's provider — panes get the
  `generic` status classifier via the provider registry.
- Freshness: new rollouts bump their day dir → `_codex_dirs_mtime()` stats the
  root + newest year/month/day chain; appends to listed files ride the
  existing `_sid_index` jsonl-stat layer.
- Scope: `--all` lists every thread; `--here` keeps threads whose cwd is
  inside the current repo.
- Gated OFF for codex rows (honest, not broken): `!` attention, LLM titling
  (`claude -p` must not bill codex threads), checkpoint (injects claude's
  `/handoff`+`/clear`), live/open detection (codex has no pid registry and
  `codex resume`'s argv lacks the sid → no reliable pid↔thread mapping).

## C2 candidates

- Attention marker from the rollout tail (`task_complete` vs `user_message`).
- Codex agent threads (`thread_spawn`) folded under their parent like claude
  agents; guardian visibility as an opt-in.
- Live/open detection via `app-server-control.sock` (if the app-server exposes
  a thread list) — speculative, needs a protocol probe.
- New-codex-session (`n`) — codex cannot preassign a thread id
  (`can_preassign_id=False`), so row-linking needs a scan-back heuristic.
