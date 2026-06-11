# Contributing to recap

Thanks for your interest! recap is a small, single-file Textual TUI, so the
contribution loop is deliberately lightweight.

## Architecture in one paragraph

recap is **two files**: `recap.py` (the session browser — scanning, the table
UI, config, CLI) and `recap_terminal.py` (the split-live pane widget — the PTY
reader thread, pyte screen, status classification, clipboard). `recap.py`
imports `recap_terminal` lazily and **degrades gracefully**: if the PTY deps
are missing, the live pane is disabled and recap still runs as a list browser.
Keep that separation — `recap_terminal` must not import `recap`.

## Setup

Requires **Python ≥ 3.11** (for stdlib `tomllib`). [uv](https://docs.astral.sh/uv/)
is the easiest path:

```bash
uv run recap.py            # run in place (deps auto-installed from the PEP-723 header)
```

Dependencies: `textual`, `pyte`, `platformdirs`, and a PTY backend
(`pywinpty` on Windows, `ptyprocess` elsewhere).

## Tests — run them before every commit

```bash
python -m py_compile recap.py recap_terminal.py
python tests/test_config.py
python tests/test_sort_recency.py
python tests/test_split_divider.py
python tests/test_resource_bounds.py
python tests/test_terminal_concurrency.py
```

**Testing reality:** the suites are written to run **without** textual / pyte /
the PTY backend (soft imports make `Widget` fall back to `object`), so they test
the pure module-level helpers and the threading model headlessly. The one
exception is the `test_split_divider.py` *runtime smoke*, which needs textual
installed (it **skips cleanly** otherwise). Anything that only lives inside the
nested Textual `App` (render, key handling, layout) is **not** unit-testable
headless — verify those by `py_compile` + the suites + actually running recap.

Only pure functions get unit tests; don't try to unit-test App methods.

## The concurrency invariants — DO NOT VIOLATE

Each split-live pane runs a background reader thread feeding pyte under a lock,
while the UI thread also takes that lock. Get this wrong and recap **hard-freezes**.
The rules (with the regression that taught us each) are documented at the top of
[`CLAUDE.md`](CLAUDE.md) — read that section before touching
`recap_terminal.py` or any threading / lock code. In short:

1. **Never** call `call_from_thread` / marshal — or any blocking cross-thread
   call — while holding `self._lock`. Compute under the lock, marshal outside it.
2. Never join the reader thread from the UI thread (`on_unmount` / `kill`).
3. Every `kill()`'s `taskkill` reap must be tracked + joined at process exit.
4. Coalesce UI work driven by PTY output (per-chunk repaint / status rebuild).

If you change threading, lock, or async behavior, **verify it yourself**
headlessly (see `tests/test_terminal_concurrency.py`) — don't ship an untested
batch, and don't "fix" a cosmetic race with a lock that can deadlock.

## Style

- Match the surrounding code: comment density, naming, idiom.
- Be meticulous about UX: terminal-width responsiveness, empty states,
  focus/cursor, and keeping a single source of truth for concurrent surfaces.
- App shortcuts use **function keys**, never bare `Ctrl+letter` (readline /
  claude own those).
- Small, individually-tested commits over a big batch.

## Pull requests

Open an issue first for anything non-trivial. In the PR, confirm the tests +
`py_compile` pass and that any threading change respects the invariants above.
By contributing you agree your work is licensed under the project's
[MIT License](LICENSE).
