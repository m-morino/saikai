# recap — working notes for Claude Code

Single-file Textual TUI (`recap.py`) + the split-live terminal widget
(`recap_terminal.py`). Run interactively with `recap`; split-live (a live
`claude` PTY in the right pane) is **opt-in** via `RECAP_SPLIT_LIVE=1`.

## Concurrency invariants (split-live) — DO NOT VIOLATE

Each split-live pane runs a background **reader thread** (`ClaudeTerminal._read_loop`)
that feeds pyte under `self._lock`; the **UI thread** also takes `self._lock`
(`render_line`, `_current_screen`). Get this wrong and recap HARD-FREEZES.

1. **NEVER call `call_from_thread` / `self._marshal(...)` — or any blocking
   cross-thread call — while holding `self._lock`.** `call_from_thread` blocks
   the reader until the UI runs the callback, but the UI is blocked trying to
   take the lock the reader holds → deadlock. Compute under the lock, **marshal
   OUTSIDE it.** (This froze the app on 2026-06; root cause + regression test:
   `_update_status` and `tests/test_terminal_concurrency.py`.)
2. **Never join the reader thread from `on_unmount` / `kill()` on the UI thread.**
   The reader may be blocked in `_marshal → call_from_thread` waiting for the UI
   → same deadlock. The reader is a daemon and is unblocked by `pty.close(force=True)`.
3. **Every `kill()`'s `taskkill` reap must be tracked + joined at process exit**
   (module-level `_REAP_THREADS` + `atexit → join_all_reaps`). Otherwise recap
   exits before `taskkill /T` finishes and orphans claude's node workers (the
   SIGHUP-emulation concern from commit 0fd9fcf). `on_unmount`/exceptions/Ctrl-K
   do NOT route through the App's two quit actions.
4. Coalesce UI work driven by PTY output: per-chunk repaints via
   `_schedule_pane_refresh`, status-driven table rebuilds via `_request_refresh`.
   A streaming claude flips status / emits chunks many times per second — a full
   rebuild or a `call_from_thread` per event pegs the UI thread.

## Testing discipline (learned the hard way, 2026-06)

- **Verify threading / lock / async changes YOURSELF before committing.** Headless
  options: Textual `App.run_test()` + `Pilot` for keys/focus/UI; direct
  thread-interleaving tests for locks (see `tests/test_terminal_concurrency.py`).
  Do NOT outsource runtime testing to the user.
- **Do not "fix" a benign/cosmetic race.** A momentary status-glyph flicker is
  not worth a lock that can deadlock — weigh fix-risk vs bug-severity. (A
  cosmetic `_update_status` race "fix" introduced the freeze above.)
- **Small, individually-tested commits**, not a big untested batch (a 15-fix
  batch hid the freeze and was hard to bisect).
- The tests run WITHOUT textual/pyte/pywinpty (soft imports → `Widget` is
  `object`): `python tests/test_terminal_concurrency.py` and
  `python tests/test_resource_bounds.py`. Run them after touching the
  terminal/threading code; `python -m py_compile recap.py recap_terminal.py` too.

## Other gotchas

- **Timezone:** transcript timestamps are UTC (`…Z`). `_iso_dt` / `_iso_date`
  convert to LOCAL before any comparison against `datetime.now()` (Age filter,
  Date grouping) — using the UTC value mis-buckets near-midnight sessions.
- **"Last activity" = `_last_active_dt(s) = max(mtime, last_ts)`, never raw
  `last_ts`.** last_ts freezes at the last *timestamped* JSONL record, but claude
  appends untimed metadata (ai-title / permission-mode / last-prompt) that still
  bumps the file mtime. The Last column, Recency sort, Age filter and Date/Project
  grouping ALL key off `_last_active_dt` so they agree — keying any one off raw
  last_ts makes a freshly-touched session sort/bucket as old while the column
  shows "now" (2026-06, session 6019b00c). Regression: `tests/test_sort_recency.py`.
- **Textual default bindings shadow ours:** Ctrl+P (command palette — disabled
  via `ENABLE_COMMAND_PALETTE=False`), Screen's Ctrl+C (routed via `on_key`).
  Check `App`/`Screen`/`DataTable`/`Input` defaults before adding a binding.
- **`Select.BLANK` is literally `False` in Textual 8.2.7** — passing it as
  `Select(value=…)` raises `InvalidSelectValueError` on mount (would crash
  launch). To start a Select with no selection, OMIT `value=` entirely. The
  Group/Sort/Status/Age boxes are initialised from persisted state
  (`_sort_select_value` etc.) so they show the remembered choice; a Select built
  WITH a value also fires `Changed` on mount, so `on_select_changed` guards
  against re-applying / rebuilding for the value it already has.
- **`status` comes from claude's OSC-0 title** (leading braille spinner = busy,
  `✳` = idle), NOT from scraping the screen body. Verified via probe; claude
  emits no OSC 9;4 / OSC 133.
- Live status (busy/waiting/idle) only exists for recap-hosted split-live panes;
  sessions running elsewhere fall back to file-registry + transcript heuristic.
