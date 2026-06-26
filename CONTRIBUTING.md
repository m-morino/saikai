# Contributing to saikai

Thanks for your interest. saikai is a small Textual TUI, so the contribution
loop is deliberately lightweight.

## Architecture in one paragraph

The runtime has three modules: `saikai.py` (history, UI, config, CLI),
`saikai_terminal.py` (provider-neutral live PTY), and `saikai_provider.py`
(agent-specific contracts). Read [Architecture](docs/ARCHITECTURE.md) before
changing module boundaries, transcript semantics, or split-live behavior.

## Setup

Requires **Python ≥ 3.11** (for stdlib `tomllib`). [uv](https://docs.astral.sh/uv/)
is the easiest path:

```bash
uv run saikai.py            # run in place (deps auto-installed from the PEP-723 header)
```

Dependencies: `textual`, `pyte`, `platformdirs`, and a PTY backend
(`pywinpty` on Windows, `ptyprocess` elsewhere).

## Tests — run them before every push

Run **exactly what CI runs** — the whole `tests/test_*.py` glob, not a hand-picked
subset (a subset misses tests in files you forgot, which is how green-locally /
red-in-CI happens):

```bash
python -m py_compile saikai.py saikai_terminal.py saikai_provider.py saikai_mirror.py
for t in tests/test_*.py; do echo "== $t =="; uv run python "$t" || break; done
```

Better, let git do it for you — enable the bundled pre-push hook once per clone so
the full suite (and the identity guard) runs automatically on `git push`:

```bash
git config core.hooksPath .githooks      # runs .githooks/pre-push on every push
```

It blocks the push if any suite fails (override, discouraged: `SKIP_TESTS=1 git push`).

Most suites also run without textual / pyte / a PTY backend through soft
imports. With textual installed, `test_split_divider.py` and
`test_keyboard_leader.py` additionally use `App.run_test()` + `Pilot` to verify
real nested-App layout, focus, and key handling. Run the suite through `uv run`
before release so these Pilot paths do not skip.

Agent-specific launch behavior belongs in `saikai_provider.py`; PTY rendering,
input, resize, and teardown stay provider-neutral in `saikai_terminal.py`.
`test_pty_backend.py` is the exception to the headless suites: it opens the
platform's real PTY backend and verifies spawn, resize, output, and EOF.

## The concurrency invariants

Each split-live pane runs a background reader thread feeding pyte under
`self._lock`, while the UI thread renders the same screen. Read the canonical
[concurrency invariants](docs/ARCHITECTURE.md#concurrency-invariants) before
touching `saikai_terminal.py` or any threading, lock, async, or teardown code.
In short:

1. **Never** call `call_from_thread` / marshal — or any blocking cross-thread
   call — while holding `self._lock`. Compute under the lock, marshal outside it.
2. Never join the reader or close a POSIX `ptyprocess` from the UI thread.
3. Every process-tree reap must be tracked and joined at process exit.
4. Coalesce UI work driven by PTY output.

If you change threading, lock, or async behavior, **verify it yourself**
headlessly (see `tests/test_terminal_concurrency.py`) — don't ship an untested
batch, and don't "fix" a cosmetic race with a lock that can deadlock.

## Style

- Match the surrounding code: comment density, naming, idiom.
- Be meticulous about UX: terminal-width responsiveness, empty states,
  focus/cursor, and keeping a single source of truth for concurrent surfaces.
- Session and pane actions use **function keys**. Ordinary bare `Ctrl+letter`
  editing keys belong to readline / claude; the configurable pane-release key
  and app-level quit handling are deliberate exceptions.
- Small, individually-tested commits over a big batch.

## Pull requests

Open an issue first for anything non-trivial. In the PR, confirm the tests +
`py_compile` pass and that any threading change respects the invariants above.
By contributing you agree your work is licensed under the project's
[MIT License](LICENSE) and follows the [Code of Conduct](CODE_OF_CONDUCT.md).
