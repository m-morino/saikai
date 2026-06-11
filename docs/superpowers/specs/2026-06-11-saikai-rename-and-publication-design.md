# saikai rename + history anonymization + first publication — design

Date: 2026-06-11
Status: approved

## Goal

Publish the project at github.com/m-morino/saikai with a clean, anonymous
history that passes `scripts/check-history.sh`.

## Decisions

1. **Rename `recap` → `saikai`** (再開 "resume" / 再会 "reunion"). `recap` is
   taken on PyPI (a data-catalog tool) and is a crowded name on GitHub;
   `saikai` is free on PyPI and has no significant GitHub collision, names
   exactly what the tool does, and is 6 characters to type.
2. **Full rename, no compatibility shims.** First public release, zero outside
   users: modules (`saikai.py`, `saikai_terminal.py`), the command, all
   `SAIKAI_*` env vars (formerly `RECAP_*`), the platformdirs config dir.
   Local users copy their old `config.toml` over once by hand.
3. **History content stays as-is.** Old commit messages that say "recap" are
   the project's real history; only author/committer identities are rewritten.
   Dated docs under `docs/superpowers/` keep the old name for the same reason.
4. **Identity rewrite via `git filter-repo`** (mailmap kept outside the repo):
   every commit's author + committer becomes
   `m-morino <11384605+m-morino@users.noreply.github.com>`, which GitHub
   attributes to the account and `check-history.sh` allows. `Co-Authored-By`
   trailers (`noreply@anthropic.com`) are already on the allowlist.
5. **Publication**: rename the GitHub repo (`gh repo rename saikai`, old URL
   redirects), then force-push the rewritten `master`, overwriting the remote's
   previous contents. A `git clone --mirror` backup is taken before the
   rewrite.

## Verification

- `scripts/check-history.sh` exits 0 over all history.
- `python -m py_compile saikai.py saikai_terminal.py` and every
  `tests/test_*.py` exit 0 after the rename.
- After push: GitHub shows commits attributed to m-morino, no corporate
  identity anywhere in the history.
