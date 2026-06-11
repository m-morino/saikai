# recap

[![CI](https://github.com/m-morino/recap/actions/workflows/ci.yml/badge.svg)](https://github.com/m-morino/recap/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)

A terminal session browser for [Claude Code](https://claude.com/claude-code).
recap scans `~/.claude/projects`, shows your past sessions in a searchable,
sortable, groupable table with an AI-generated one-line summary per session, and
resumes any of them. By default (**split-live**) it also hosts live `claude`
panes side-by-side so you can run and watch several sessions at once.

> Single-file [Textual](https://github.com/Textualize/textual) app
> (`recap.py` + `recap_terminal.py`). The live pane uses ConPTY on Windows and a
> POSIX PTY elsewhere — see [Platform support](#platform-support) for the
> per-OS verification status (Windows is the verified platform today).

## Install

Requires **Python ≥ 3.11**. The easiest path is
[uv](https://docs.astral.sh/uv/) — it resolves the deps from the inline PEP-723
header, no manual venv:

```bash
uv run recap.py          # run in place (deps auto-installed)
uv tool install .        # install the `recap` command on your PATH, then: recap
```

Prefer pip / pipx? Both work (deps come from `pyproject.toml`):

```bash
pipx install .           # isolated + on PATH  (recommended for pip users)
pip install .            # into the active environment
```

The split-live pane needs the PTY deps (`pyte`, and `pywinpty` on Windows /
`ptyprocess` elsewhere); they install automatically with any command above. If
they're somehow missing, recap still runs in list-only mode (see below).

## Usage

```bash
recap                 # sessions for the current project (git repo)
recap --all-projects  # every project under ~/.claude/projects
recap --table         # static, non-interactive table
recap --help
```

### Keys

| Key | Action |
|-----|--------|
| `↑` `↓` / `Enter` | move / resume the selected session |
| `/` or just type | open the search & filter bar (`Esc` closes it, keeps the filter) |
| `F5` | refresh · `F6` ★ favorite · `F7` hide · `F8` changes (diff) · `F9` copy opening prompt |
| `Shift+F2` | rename the session — type your own name (empty clears it → back to auto title) |
| `Shift+F5/F6/F7` | tree / cluster / cycle grouping |
| `Tab` | preview: full ↔ summary · `?` help · `Esc` quit |

**Search tokens** (combine with text and each other): `:fav` `:hidden` `:open`
`:active` `:recent`. Group / Sort / Status / Age also have top-bar dropdowns.

### Split-live (default)

recap runs real interactive `claude` processes in tabs beside the list whenever
its PTY deps (`pyte`, `pywinpty`/`ptyprocess`) are present — they ship as
dependencies, so this is the default. To opt out and use the lightweight
list-only browser (`Enter` = full-screen takeover resume), set the env var:

```bash
RECAP_SPLIT_LIVE=0 recap     # also: false / no / off
```

| Key | Action |
|-----|--------|
| `Enter` | open / focus a live pane for the selected session |
| `Shift+F8` | start a NEW claude session in any folder / git worktree |
| `Shift+F4` | reopen the panes from your last session (snapshot + resume) — anytime |
| `F2` / `F3` | previous / next live tab |
| `Shift+F3` | jump to the next pane needing attention (`?` waiting / `!` finished) |
| `F4` | hide / show the session list (full-width pane) |
| `Ctrl+]` | return focus from a pane back to the list (`RECAP_RELEASE_KEY` to change) |
| `F10` / `Shift+F10` | close the active tab / close all tabs (explicit close — *not* restored) |
| `Esc` / `Ctrl+C` | quit: snapshot the open panes, then kill them all (`Shift+F4` reopens them next launch) |
| scroll up | freeze the pane (copy mode): select/copy while claude keeps running |

Markers in the list: `~` busy · `?` waiting for input · `!` finished (unanswered)
· `@` open · `+` active · `.` recent · `*` favorite · `x` hidden.

## Configuration (environment variables)

| Variable | Default | Meaning |
|---|---|---|
| `RECAP_SPLIT_LIVE` | on | live-pane mode; set `0`/`false`/`no`/`off` to disable → list-only browser + full-takeover resume |
| `RECAP_AUTO_REFRESH` | off | seconds between background re-scans |
| `RECAP_SUMMARIZE_CMD` | — | command to summarize with (prompt on stdin → summary on stdout) instead of `claude -p` |
| `RECAP_MAX_MEM_LOAD` | 85 | refuse/warn opening a pane above this memory-load % (Win `dwMemoryLoad`; Linux/macOS derived) |
| `RECAP_MIN_COMMIT_MB` | 2048 | keep this much **commit headroom** free — the system-freeze guard (Win/Linux) |
| `RECAP_MIN_FREE_PHYS_PCT` | 8 | keep ≥ this % of physical RAM available (anti-thrash floor, machine-relative) |
| `RECAP_CLAUDE_MB` | 600 | estimated RAM per live pane |
| `RECAP_MIN_FREE_MB` | 0 | optional absolute physical floor (legacy; max'd with the % floor) |
| `RECAP_HARD_RAM_GATE` | off | `1` refuses (vs warns) when the gate would be crossed |
| `RECAP_MAX_LIVE` | 64 | hard cap on concurrent live panes (backstop) |
| `RECAP_COLOR_BY` | project | what tints the session title: `project` / `worktree` / `topic` / `none` |
| `RECAP_SPLIT_RATIO` | 0.34 | initial list/pane split (drag the divider to change; the dragged value persists) |

recap also reads an optional **TOML config file** for these same knobs (with
`env > config > default` precedence) plus `[keys]` rebinds. Run `recap
--init-config` to write a documented template, `recap --print-config` to see the
resolved location and values.

## Platform support

**Supported platforms (deliberately bounded): Windows, Linux — including WSL —
and macOS, on Python ≥ 3.11.** Other platforms are *unsupported*: recap may still
run on another POSIX OS via the generic POSIX path (and it degrades safely rather
than crashing), but that's untested and not a target.

recap itself is pure Python + Textual; the **split-live pane** is the only
platform-specific part (it drives a real PTY and the clipboard). Honest status:

| OS | Live-pane PTY | Clipboard (from a frozen pane) | RAM gate source | Status |
|---|---|---|---|---|
| **Windows** 10 / 11 | ConPTY (`pywinpty`) | Win32 `CF_UNICODETEXT` (codepage-safe) | `GlobalMemoryStatusEx` | ✅ **developed & daily-driven** (on WezTerm) |
| **Linux** *(incl. WSL)* | POSIX PTY (`ptyprocess`) | OSC-52 *(needs an OSC-52-capable terminal)* | `/proc/meminfo` | ⚠️ code-complete, **not yet run by the author** |
| **macOS** | POSIX PTY (`ptyprocess`) | OSC-52 *(iTerm2 / kitty / WezTerm fine; Terminal.app needs it enabled)* | `sysctl` + `vm_stat` *(load + physical; no commit limit)* | ⚠️ code-complete, **not yet run by the author** |

- **Terminal-agnostic on Windows:** the clipboard goes through the Win32
  `CF_UNICODETEXT` API and the PTY through ConPTY, neither of which depends on
  the host terminal — so **Windows Terminal** uses the same code paths as WezTerm
  (verified terminal) and is expected to behave identically.
- **List-only mode** (`RECAP_SPLIT_LIVE=0`) has no PTY dependency and should run
  anywhere Textual runs.
- The headless regression tests are platform-independent (they stub out
  textual / pyte / pywinpty) and pass on the dev machine — run them with plain
  `python` (no deps needed):

  ```bash
  python tests/test_sort_recency.py
  python tests/test_terminal_concurrency.py
  python tests/test_resource_bounds.py
  python tests/test_terminal_watchdog.py
  ```

- Ran it on Linux or macOS? Please open an issue with the result (and any PTY /
  clipboard quirks) so these rows can move to ✅.

## Contributing

Issues and PRs welcome — see [CONTRIBUTING.md](CONTRIBUTING.md) for the dev
setup, how to run the test suites, and the **concurrency invariants** that keep
the split-live pane from deadlocking (read those before touching threading code).
Changes are documented in [CHANGELOG.md](CHANGELOG.md).

## Security

Please report vulnerabilities privately — see [SECURITY.md](SECURITY.md). Don't
open a public issue for a security problem.

## License

recap is released under the [MIT License](LICENSE). It depends on a few
third-party packages installed separately (textual, pyte, pywinpty/ptyprocess) —
see [THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md). Note `pyte` is LGPL-3.0; it
is used as an unmodified, separately-installed dependency, which keeps recap's
own code MIT.
