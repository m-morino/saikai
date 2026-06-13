# saikai

[![CI](https://github.com/m-morino/saikai/actions/workflows/ci.yml/badge.svg)](https://github.com/m-morino/saikai/actions/workflows/ci.yml)
[![PyPI version](https://img.shields.io/pypi/v/saikai)](https://pypi.org/project/saikai/)
[![GitHub release](https://img.shields.io/github/v/release/m-morino/saikai)](https://github.com/m-morino/saikai/releases/latest)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](https://github.com/m-morino/saikai/blob/master/LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)

**English** | [日本語](https://github.com/m-morino/saikai/blob/master/README.ja.md)

Claude Code remembers every conversation. Finding the right one again gets
harder once work spreads across repositories and worktrees, because its resume
flow starts from the current working directory.

I liked Claude Desktop's at-a-glance conversation experience, but wanted it
inside a Linux terminal without keeping a desktop app open. So I built saikai:
one place to find old sessions, resume them from their original working
directory, and see which live session needs attention.

**Find it. Resume it. Know what needs you.**

*saikai* = 再開 "resume" + 再会 "reunion" (Japanese).

![saikai demo](https://raw.githubusercontent.com/m-morino/saikai/master/docs/assets/saikai-demo.gif)

<sub>The demo uses fictional, leak-checked data. The current GIF is a
deterministic UI recording; see [the recording guide](https://github.com/m-morino/saikai/blob/master/docs/demo-recording.md)
for how public recordings are isolated and audited.</sub>

## What it changes

- **Find across every cwd.** Search Claude Code history across repositories and
  worktrees, then resume from the cwd where the session started.
- **Know what needs you.** Keep several real `claude` sessions in split-live
  tabs; `~`, `?`, and `!` distinguish working, waiting, and finished sessions.
- **Restore a working set.** Quit without losing the set of panes you had open,
  then reopen it later with `Shift+F4`.
- **Inspect the work, not just the title.** Preview transcripts, compare
  changes, reuse prompts, and follow inferred parent/child session branches.
- **Stay local and unobtrusive.** saikai reads Claude's own transcript files.
  It adds no daemon or database, and optional AI summaries are opt-in.

Title colors group related context; symbols report session state. In the
default view, the same title color means the same project. Press `?` for the
live legend.

The implementation is three small Python modules on
[Textual](https://github.com/Textualize/textual), with a RAM gate that warns
before another live pane would push the machine into thrashing.

> The live pane uses ConPTY on Windows and a POSIX PTY elsewhere — see
> [Platform support](#platform-support) for the per-OS verification status
> (Windows is the verified platform today).

## Install

Requires **Python ≥ 3.11**. The easiest path is
[uv](https://docs.astral.sh/uv/) — it installs the PyPI package in an isolated
environment, no manual venv:

```bash
uv tool install saikai   # stable release → `saikai` on PATH
```

From a clone:

```bash
uv run saikai.py          # run in place (deps auto-installed)
uv tool install .        # install the `saikai` command on your PATH, then: saikai
```

Prefer pip / pipx? Both work (deps come from `pyproject.toml`):

```bash
pipx install saikai   # isolated + on PATH
pip install saikai   # into the active environment
```

The split-live pane needs the PTY deps (`pyte`, and `pywinpty` on Windows /
`ptyprocess` elsewhere); they install automatically with any command above. If
they're somehow missing, saikai still runs in list-only mode (see below).

## Usage

```bash
saikai                 # every project, full history (the default)
saikai --here          # only the current project (git repo)
saikai --days 7        # only the last 7 days (one-shot; --save-defaults persists)
saikai --table         # static, non-interactive table
saikai --help
```

### Keys — learn three things

Everything else is on screen (the footer, the dropdowns, and the `␣` menu
that pops up when you pause).

1. **Keys you already know.** `↑` `↓` move · `Enter` resumes · `/` or just
   typing searches · `Tab` toggles the preview · `?` full key list · `Esc`
   leaves the current context (search → list → quit).
2. **`Space` is the menu.** Press `Space` in the list, then one mnemonic
   letter. Hesitate after `Space` and the whole menu appears in place,
   grouped by family (which-key style) — nothing to memorise:

   | Session | View | Panes |
   |---|---|---|
   | `f` ★ favorite | `s` cycle **s**ort column | `n` new session |
   | `h` hide | `o` flip sort **o**rder | `p` restore panes |
   | `e` rename (edit) | `g` cycle grouping | `z` freeze pane |
   | `y` copy prompt (yank) | `t` tree | `a` next attention |
   | `d` diff (changes) | `l` hide/show list | `x` close tab · `[` `]` tabs |
   | `r` refresh | `,` settings · `/` hide/show bar | `Space` mark for batch launch |

   Space is a deliberate menu key, not a claim that every TUI should use a
   Space leader: it only arms while the session list owns focus, so search
   fields and live agent panes keep a literal Space. It also avoids taking a
   letter away from type-to-search. The trade-off is that list marking becomes
   `Space Space`; set `[keys] leader = "none"` if conventional Space-to-mark is
   more important to you.

3. **`Ctrl+]` returns focus** from a live claude pane to the list (the pane
   owns every other key, so claude works normally inside it).

**Search tokens** (combine with text and each other): `:fav` `:hidden` `:open`
`:active` `:recent`. The filter bar — search box plus the Group / Sort /
Status / Age dropdowns — is visible by default: `Tab`/`Shift+Tab` walk into
the dropdowns, `Enter` opens one, `␣/` reclaims the rows (persists).

More keyboard parity: `Alt+←/→` nudges the list/pane divider (persists, like
dragging it). Every menu action also has a legacy F-key alias — `?` lists
them all, including your `[keys]` remaps.

Don't like the defaults? In `config.toml`: `[keys] leader = "none"` turns the
mode off (Space then marks directly, as before), `leader = "ctrl+g"` moves it,
`leader_defaults = false` empties the map, and any `action = "x"` single-letter
entry remaps one sequence. **Mouse extras** (never required): click a column
header to sort, drag the divider, click rows and dropdowns.

### Split-live (default)

![Split-live: the session list on the left with a live claude pane running on the right](https://raw.githubusercontent.com/m-morino/saikai/master/docs/assets/saikai-split-live.svg)

saikai runs real interactive `claude` processes in tabs beside the list whenever
its PTY deps (`pyte`, `pywinpty`/`ptyprocess`) are present — they ship as
dependencies, so this is the default. To opt out and use the lightweight
list-only browser (`Enter` = full-screen takeover resume), set the env var:

```bash
SAIKAI_SPLIT_LIVE=0 saikai     # also: false / no / off
```

| Key | Action |
|-----|--------|
| `Enter` | open / focus a live pane for the selected session |
| `Shift+F8` | start a NEW claude session in any folder / git worktree |
| `Shift+F4` | reopen the panes from your last session (snapshot + resume) — anytime |
| `F2` / `F3` | previous / next live tab |
| `Shift+F3` | jump to the next pane needing attention (`?` waiting / `!` finished) |
| `F4` | hide / show the session list (full-width pane) |
| `Ctrl+]` | return focus from a pane back to the list (`SAIKAI_RELEASE_KEY` to change) |
| `F10` / `Shift+F10` | close the active tab / close all tabs (explicit close — *not* restored) |
| `Esc` / `Ctrl+C` | quit: snapshot the open panes, then kill them all (`Shift+F4` reopens them next launch) |
| scroll up | freeze the pane (copy mode): select/copy while claude keeps running |

Markers in the list: `~` busy · `?` waiting for input · `!` finished (unanswered)
· `@` open · `+` active · `.` recent · `*` favorite · `x` hidden.

## Configuration (environment variables)

| Variable | Default | Meaning |
|---|---|---|
| `SAIKAI_SPLIT_LIVE` | on | live-pane mode; set `0`/`false`/`no`/`off` to disable → list-only browser + full-takeover resume |
| `SAIKAI_AUTO_REFRESH` | off | seconds between background re-scans |
| `SAIKAI_SUMMARIZE_CMD` | — | command to summarize with (prompt on stdin → summary on stdout) instead of `claude -p` |
| `SAIKAI_AUTO_PERMISSION` | off | opt in to adding `--permission-mode auto` for frequently used workspaces |
| `SAIKAI_MAX_MEM_LOAD` | 85 | refuse/warn opening a pane above this memory-load % (Win `dwMemoryLoad`; Linux/macOS derived) |
| `SAIKAI_MIN_COMMIT_MB` | 2048 | keep this much **commit headroom** free — the system-freeze guard (Win/Linux) |
| `SAIKAI_MIN_FREE_PHYS_PCT` | 8 | keep ≥ this % of physical RAM available (anti-thrash floor, machine-relative) |
| `SAIKAI_CLAUDE_MB` | 600 | estimated RAM per live pane |
| `SAIKAI_MIN_FREE_MB` | 0 | optional absolute physical floor (legacy; max'd with the % floor) |
| `SAIKAI_HARD_RAM_GATE` | off | `1` refuses (vs warns) when the gate would be crossed |
| `SAIKAI_MAX_LIVE` | 64 | hard cap on concurrent live panes (backstop) |
| `SAIKAI_SCROLLBACK` | 2000 | per-pane scrollback lines kept in memory. **Biggest lever on the live process's RAM** (a full pane ≈ cols×lines pyte cells); lower it (e.g. 1000) on a memory-tight machine, raise for deeper history |
| `SAIKAI_COLOR_BY` | project | what tints the session title: `project` / `worktree` / `topic` / `none` |
| `SAIKAI_SPLIT_RATIO` | 0.34 | initial list/pane split (drag the divider to change; the dragged value persists) |
| `SAIKAI_RELEASE_KEY` | `ctrl+]` | key that returns focus from a live pane to the list |

saikai also reads an optional **TOML config file** for these same knobs (with
`env > config > default` precedence) plus `[keys]` rebinds. Run `saikai
--init-config` to write a documented template, `saikai --print-config` to see the
resolved location and values — or press **`Space ,`** inside the app: the
Settings screen edits the list options in place and shows every config knob
with its resolved value and source (`e` there opens config.toml in your editor).

Claude Code is the currently integrated provider. Agent-specific launch and
status contracts live in `saikai_provider.py`; a Codex contract validates the
extension boundary, but Codex is not selectable until its history discovery and
live-state integration are complete. Claude history discovery respects
`CLAUDE_CONFIG_DIR`.

## Ecosystem

saikai focuses on finding, resuming, and supervising sessions over time.
[ccmanager](https://github.com/kbwo/ccmanager) focuses on coordinating a live
multi-agent roster. The two solve adjacent problems rather than competing for
the same workflow.

## Platform support

**Verified support is deliberately bounded to Windows 10 / 11 on Python ≥
3.11.** Linux, WSL2, and macOS remain implemented but experimental until their
real PTY paths receive sustained native testing. Other platforms are unsupported.

saikai itself is pure Python + Textual; the **split-live pane** is the only
platform-specific part (it drives a real PTY and the clipboard). Honest status:

| OS | Live-pane PTY | Clipboard (from a frozen pane) | RAM gate source | Status |
|---|---|---|---|---|
| **Windows** 10 / 11 | ConPTY (`pywinpty`) | Win32 `CF_UNICODETEXT` (codepage-safe) | `GlobalMemoryStatusEx` | ✅ **developed & daily-driven** (on WezTerm) |
| **Linux** *(incl. WSL2)* | POSIX PTY (`ptyprocess`) | OSC-52 *(terminal / tmux / SSH policy must allow it)* | `/proc/meminfo` | ⚠️ **experimental; native reviewers wanted** |
| **macOS** | POSIX PTY (`ptyprocess`) | local `pbcopy`; OSC-52 fallback for remote-capable terminals | `sysctl` + `vm_stat` *(load + physical; no commit limit)* | ⚠️ **experimental; macOS reviewers wanted** |

- **Terminal-agnostic on Windows:** the clipboard goes through the Win32
  `CF_UNICODETEXT` API and the PTY through ConPTY, neither of which depends on
  the host terminal — so **Windows Terminal** uses the same code paths as WezTerm
  (verified terminal) and is expected to behave identically.
- **List-only mode** (`SAIKAI_SPLIT_LIVE=0`) has no PTY dependency and should run
  anywhere Textual runs.
- The headless regression tests are platform-independent (they stub out
  textual / pyte / pywinpty) and pass on the dev machine — run them with plain
  `python` (no deps needed):

  ```bash
  python tests/test_config.py
  python tests/test_providers.py
  python tests/test_sort_recency.py
  python tests/test_split_divider.py
  python tests/test_terminal_concurrency.py
  python tests/test_resource_bounds.py
  python tests/test_terminal_watchdog.py
  python tests/test_keyboard_leader.py
  python tests/test_pty_backend.py
  ```

- Linux/WSL2/macOS reviewers are wanted. Please report the terminal, local vs
  SSH/tmux setup, PTY teardown result, key quirks, and clipboard behavior.
- CI installs each OS's real PTY backend and smoke-tests spawn, resize, output,
  and EOF on Windows, Linux, and macOS. This is not a substitute for interactive
  review across terminal emulators, SSH, tmux, IMEs, and clipboard policies.

## Contributing

Issues and PRs welcome — see [CONTRIBUTING.md](https://github.com/m-morino/saikai/blob/master/CONTRIBUTING.md) for the dev
setup, how to run the test suites, and the **concurrency invariants** that keep
the split-live pane from deadlocking (read those before touching threading code).
Changes are documented in [CHANGELOG.md](https://github.com/m-morino/saikai/blob/master/CHANGELOG.md).

## Security

Please report vulnerabilities privately — see [SECURITY.md](https://github.com/m-morino/saikai/blob/master/SECURITY.md). Don't
open a public issue for a security problem.

## License

saikai is released under the [MIT License](https://github.com/m-morino/saikai/blob/master/LICENSE). It depends on a few
third-party packages installed separately (textual, pyte, pywinpty/ptyprocess) —
see [THIRD-PARTY-NOTICES.md](https://github.com/m-morino/saikai/blob/master/THIRD-PARTY-NOTICES.md). Note `pyte` is LGPL-3.0; it
is used as an unmodified, separately-installed dependency, which keeps saikai's
own code MIT.
