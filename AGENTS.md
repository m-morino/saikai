# saikai agent notes

Default to concise Japanese when working with the repository owner.

Before changing code, read:

- [CONTRIBUTING.md](CONTRIBUTING.md) for the development loop.
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for module boundaries, history
  semantics, split-live lifecycle, and concurrency invariants.

Non-negotiable split-live rules:

- Never marshal or call `call_from_thread` while holding `self._lock`.
- Never join the reader or close a POSIX `ptyprocess` on the UI thread.
- Track and join every process-tree reap.
- Coalesce PTY-driven UI work.

Keep provider-specific launch/status behavior in `saikai_provider.py`; keep PTY
rendering, input, resize, and teardown provider-neutral in
`saikai_terminal.py`. Run the relevant tests yourself before committing, with
`tests/test_terminal_concurrency.py` and `tests/test_resource_bounds.py`
mandatory after terminal/threading changes.
