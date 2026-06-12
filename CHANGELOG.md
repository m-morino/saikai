# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- **Keyboard-first by default.** `Space` (in the list) is now a leader key with
  a built-in mnemonic map — `Space f` favorite, `Space h` hide, `Space s` /
  `Space o` cycle the sort column / direction (previously mouse-only),
  `Space Space` batch-mark, and more; the first presses hint the full map and
  `?` always shows it live. `Alt+←/→` resizes the list/pane split from the
  keyboard (persisted like a drag). Configure via `[keys]`: `leader = "none"`
  disables, `leader_defaults = false` empties the map, single letters remap.
- **Japanese documentation**: full `README.ja.md`, cross-linked from the
  English README.

- **In-app Settings** on `Space ,`: list options (Group / Sort / Status / Age /
  Tree / Cluster) editable in place and applied instantly; every config.toml /
  env knob shown read-only with its resolved value and source; `e` opens
  config.toml in your editor (created from the template when absent).

### Changed
- **The key system is now "learn three things":** (1) keys you already know
  (`↑↓ ⏎ / Tab ? Esc`), (2) `Space` = the menu — shown in the footer as
  `␣ Menu`, arms from any non-typing widget, and pops up the family-grouped
  map when you pause, (3) `Ctrl+]` = pane → list. `Esc` now means "leave the
  current context" (search/dropdown → list, list → quit) — with the bar
  visible by default a single `Esc` quits again; `␣/` is the deliberate bar
  toggle. F-keys remain as compatibility aliases, listed only in `?`.
- **The filter bar (search + Group/Sort/Status/Age dropdowns) is visible by
  default** — the dropdowns are how the grouping/sorting features get
  discovered, and hidden-until-`/` meant nobody found them. `Space /` toggles
  the bar and that choice persists. The table still owns focus on launch, so
  the leader and search-as-you-type are unchanged.
- **Grouping defaults to State and sorting defaults to Recency descending.**
  The initial view prioritizes sessions needing input / running now, then what
  was touched most recently. Explicit persisted choices still win.
- **The leader hint is now which-key style:** it fires only when you hesitate
  (0.6 s after `Space`), every time, and shows the map grouped into
  Session / View / Panes families instead of one alphabetical line. `?` help
  renders the same grouped map, leads with the leader letters (`␣f`, `␣h`, …)
  and compacts the aliases to `⇧F7`-style notation.
- **Calmer chrome, lower learning load:** the footer shows only the four core
  keys (`⏎` `Tab` `?` `Esc`) — everything else lives in `?` help and the
  leader hint; the status bar drops the OFF-state noise, keeps Sort/Group
  visible, and gains a standing `␣ leader · ? keys` breadcrumb.
- Date group headers are locale-neutral English (`Jun 11`, `2025-12-03`)
  instead of Japanese (`6月11日`).
- README screenshots show grouping, the sort indicator, and a pinned favorite,
  so the table features are visible at a glance.

### Fixed
- Config values shown by Settings / `--print-config` now match runtime:
  `summary.model` and `keys.release` are applied, while `split_ratio` and
  `scrollback_lines` are included in the resolved-settings list.
- A custom leader key no longer leaves `Space` acting as a second hidden leader.
- `--reset-options` now forgets only saved CLI scope defaults and preserves the
  split ratio and filter-bar visibility.
- Automatic `--permission-mode auto` is now disabled by default and requires
  explicit `[launch] auto_permission=true` / `SAIKAI_AUTO_PERMISSION=1`.
- **Linux/macOS: quitting (`Esc` / `Ctrl+C`) or closing a tab (`F10`) with a
  live pane open hard-froze saikai.** ptyprocess buffers the PTY master fd in an
  `io.BufferedRWPair`: the background reader blocks in `read()` holding the
  buffer lock, and `pty.close()` — which saikai called on the UI thread — takes
  that same lock before the child is signalled, deadlocking the UI forever. The
  POSIX kill path now only posts signals (SIGHUP/SIGTERM to the process group,
  the `taskkill /T` analog) from the UI thread and runs the blocking close on a
  tracked reaper thread, with SIGKILL escalation. Windows was never affected
  (pywinpty's close cancels console I/O natively).

## [0.1.0] — 2026-06-11

Initial public release. Developed pre-release under the working name `recap`;
published as **saikai** (再開, "resume") because `recap` was already taken on
PyPI. Everything uses the new name: the modules (`saikai.py` /
`saikai_terminal.py`), the `saikai` command, all `SAIKAI_*` environment
variables, and the config directory.

### Added
- **Session browser** for Claude Code: scans `~/.claude/projects` and shows past
  sessions in a searchable, sortable, groupable table (by Date / Project / State),
  with per-session markers (open / active / recent / favorite / hidden) and an
  optional one-line title.
- **Split-live (default):** host live `claude` panes beside the list, switch via
  tabs, and see each pane's status at a glance (busy / waiting-for-input / idle).
  Includes snapshot + restore of the open pane set (`Shift+F4`), saikai-owned
  drag-selection copy from a streaming pane, and a memory-pressure-aware gate on
  how many panes may open. Opt out with `SAIKAI_SPLIT_LIVE=0`.
- **Configurable layout & colour:** draggable list/pane divider (position
  persisted), `display.color_by` to tint titles by project / worktree / topic /
  none, and category (group-header) rows that the cursor skips over.
- **TOML config** (`--init-config` / `--print-config`) with `env > config >
  default` precedence for every `SAIKAI_*` knob; cross-platform config location
  via `platformdirs`.
- **Remappable key bindings** plus an opt-in leader/prefix mode.
- **Optional LLM summaries** (off by default; opt in via config / env).
- **Cross-platform PTY:** ConPTY on Windows, POSIX PTY on Linux/macOS, with a
  per-OS system-memory gate; graceful list-only fallback when PTY deps are absent.

### Fixed
- Windows clipboard copy (freeze-copy + `F9` copy-prompt) now uses the Win32
  `CF_UNICODETEXT` API, so multibyte text (CJK / emoji) no longer garbles under a
  UTF-8 console code page.

[Unreleased]: https://github.com/m-morino/saikai/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/m-morino/saikai/releases/tag/v0.1.0
