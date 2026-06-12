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

### Changed
- **Calmer chrome, lower learning load:** the footer shows only the four core
  keys (`⏎` `Tab` `?` `Esc`) — everything else lives in `?` help and the
  leader hint; the status bar drops the OFF-state noise, keeps Sort/Group
  visible, and gains a standing `␣ leader · ? keys` breadcrumb.
- Date group headers are locale-neutral English (`Jun 11`, `2025-12-03`)
  instead of Japanese (`6月11日`).
- README screenshots now show Date grouping, the sort indicator, and a pinned
  favorite, so the table features are visible at a glance.

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
