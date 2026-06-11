<!-- Thanks for contributing to recap! Keep PRs small and focused. -->

## What & why

<!-- What does this change, and what problem does it solve? Link any issue. -->

Fixes #

## Checklist

- [ ] `python -m py_compile recap.py recap_terminal.py` passes
- [ ] All five test suites pass (`tests/test_config.py`, `test_sort_recency.py`,
      `test_split_divider.py`, `test_resource_bounds.py`,
      `test_terminal_concurrency.py`)
- [ ] New pure/helper logic has a unit test; App/render-only changes were
      verified by running recap
- [ ] If I touched threading / locks / async in `recap_terminal.py`, I respected
      the concurrency invariants in [`CLAUDE.md`](../CLAUDE.md) and verified no
      deadlock headlessly
- [ ] App shortcuts use function keys, not bare `Ctrl+letter`
- [ ] Commits are small and individually tested

## Notes for reviewers

<!-- Anything tricky, trade-offs, or areas you want extra eyes on. -->
