# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- A shared fictional demo fixture now generates the public screenshots,
  deterministic headless GIF, and recording workspace without reading the
  caller's real HOME or Claude history.
- An isolated real-Claude recording guide and asciinema cast auditor reject
  private paths, credentials, identities, and unapproved demo projects before
  conversion.
- `docs/ARCHITECTURE.md` is now the canonical contributor reference for module
  boundaries, history semantics, PTY lifecycle, and concurrency invariants.

### Changed
- The public story now starts with the cross-repository session-discovery and
  human-attention problem that motivated saikai.
- Help, Settings, and the READMEs explain the visual grammar consistently:
  title color groups context and ASCII symbols report state.
- Contributor-facing agent files are concise entrypoints instead of duplicate
  copies of the concurrency manual.

### Removed
- The incomplete global LLM cluster mode and its dangling UI/CLI controls.
- Internal launch-marketing notes and completed implementation plans from the
  public repository surface.

### Fixed
- Help no longer claims that the Textual Last column is color-coded when it is
  rendered as plain text.

## [0.2.2] — 2026-06-13

### Fixed
- PyPI's project description now uses absolute GitHub URLs for the demo images,
  Japanese README, license, changelog, contribution guide, security policy, and
  third-party notices. PyPI does not resolve repository-relative Markdown links,
  so these links and images were broken on the initial 0.2.1 PyPI page.

## [0.2.1] — 2026-06-13

### Added
- PyPI is now the primary installation channel: `uv tool install saikai` or
  `pipx install saikai`. A release-triggered GitHub Actions workflow builds,
  verifies, and publishes the universal wheel and source distribution through
  PyPI Trusted Publishing.

### Changed
- User-facing documentation calls the Space prefix a command menu rather than
  presenting editor-specific "leader" terminology as a general TUI convention.
- The README documents why Space is used only while the session list owns focus,
  its `Space Space` marking trade-off, and how to restore conventional
  Space-to-mark behavior.

### Fixed
- Command-menu choices now render with an explicit separator (`f → fav`) in
  both the delayed menu and `?` help instead of looking like misspelled commands
  such as `ffav`.
- The real-PTY backend smoke test now reports a skip, rather than a failure,
  when the platform PTY backend is intentionally unavailable.

## [0.2.0] — 2026-06-12

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
- Extracted agent-specific launch capabilities into `saikai_provider.py`.
  Claude remains the integrated provider; a non-selectable Codex contract
  validates the extension boundary without overstating support.
- Claude history discovery now respects `CLAUDE_CONFIG_DIR`.
- Missing or not-yet-created provider history roots now scan as empty instead
  of crashing.
- The split-live PTY widget is now agent-neutral and accepts an injected status
  classifier while retaining the previous `ClaudeTerminal` import alias.
- CI now installs and smoke-tests the real PTY backend on Windows, Linux, and
  macOS. Local macOS clipboard copy uses `pbcopy` before OSC-52 fallback.
- Modified navigation keys such as Ctrl/Alt+Arrow are forwarded to split-live
  children using xterm-compatible sequences.
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

[Unreleased]: https://github.com/m-morino/saikai/compare/v0.2.2...HEAD
[0.2.2]: https://github.com/m-morino/saikai/compare/v0.2.1...v0.2.2
[0.2.1]: https://github.com/m-morino/saikai/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/m-morino/saikai/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/m-morino/saikai/releases/tag/v0.1.0
