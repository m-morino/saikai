<!-- Thanks for contributing to saikai! Keep PRs small and focused. -->

## What & why

<!-- What does this change, and what problem does it solve? Link any issue. -->

Fixes #

## Checklist

- [ ] `python -m py_compile saikai.py saikai_terminal.py saikai_provider.py` passes
- [ ] All suites listed in `CONTRIBUTING.md` pass, including the provider,
      threading, Pilot, and real-platform PTY smoke tests
- [ ] New pure/helper logic has a unit test; App/render-only changes were
      verified by running saikai
- [ ] If I touched threading / locks / async in `saikai_terminal.py`, I respected
      the concurrency invariants in [`CLAUDE.md`](../CLAUDE.md) and verified no
      deadlock headlessly
- [ ] App shortcuts use function keys, not bare `Ctrl+letter`
- [ ] Commits are small and individually tested

## Notes for reviewers

<!-- Anything tricky, trade-offs, or areas you want extra eyes on. -->
