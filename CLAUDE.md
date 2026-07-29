# saikai notes for Claude Code

Read [AGENTS.md](AGENTS.md), [CONTRIBUTING.md](CONTRIBUTING.md), and the
canonical [architecture and concurrency invariants](docs/ARCHITECTURE.md)
before changing code.

In particular: never marshal while holding `self._lock`, never close a POSIX
`ptyprocess` on the UI thread, and run `tests/test_terminal_concurrency.py`
after split-live changes.

Before touching pane rendering or the list viewport, read the
[rendering invariants](docs/ARCHITECTURE.md#rendering-invariants) — each one broke
silently once, with the symptom showing up somewhere else. For a display complaint,
start from [DEBUGGING.md](docs/DEBUGGING.md): `SAIKAI_DIAG=1` captures the whole
picture in one run, and that page also lists what each instrument *cannot*
distinguish.
