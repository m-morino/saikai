# recap

A terminal session browser for [Claude Code](https://claude.com/claude-code).
recap scans `~/.claude/projects`, shows your past sessions in a searchable,
sortable, groupable table with an AI-generated one-line summary per session, and
resumes any of them. By default (**split-live**) it also hosts live `claude`
panes side-by-side so you can run and watch several sessions at once.

> Single-file [Textual](https://github.com/Textualize/textual) app
> (`recap.py` + `recap_terminal.py`). Works on **Windows, Linux, and macOS**
> (the live pane uses ConPTY on Windows, a POSIX PTY elsewhere).

## Install

```bash
# Run directly with uv (recap.py declares its deps inline, PEP 723):
uv run recap.py

# …or install as a tool:
uv tool install .        # then: recap
pip install .            # then: recap
```

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
| `RECAP_MIN_FREE_MB` / `RECAP_CLAUDE_MB` | 1536 / 600 | free-RAM floor / estimated RAM per live pane |
| `RECAP_HARD_RAM_GATE` | off | `1` refuses to open a pane that would cross the RAM floor |
| `RECAP_MAX_LIVE` | 64 | hard cap on concurrent live panes (backstop) |

## License

recap is released under the [MIT License](LICENSE). It depends on a few
third-party packages installed separately (textual, pyte, pywinpty/ptyprocess) —
see [THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md). Note `pyte` is LGPL-3.0; it
is used as an unmodified, separately-installed dependency, which keeps recap's
own code MIT.
