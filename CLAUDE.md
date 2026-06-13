# saikai notes for Claude Code

Read [AGENTS.md](AGENTS.md), [CONTRIBUTING.md](CONTRIBUTING.md), and the
canonical [architecture and concurrency invariants](docs/ARCHITECTURE.md)
before changing code.

In particular: never marshal while holding `self._lock`, never close a POSIX
`ptyprocess` on the UI thread, and run `tests/test_terminal_concurrency.py`
after split-live changes.
