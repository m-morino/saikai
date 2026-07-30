# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed
- **A session parked on claude's agents view is reported as "Needs input".** The view
  states its own aggregate in the OSC-0 title (`1 awaiting input · claude agents`), but
  with no title spinner and no permission prompt in the body every existing signal said
  idle — so a session where an agent was waiting for an answer sat in the Idle section.
  The counts in the title now decide: `N awaiting input` reads as needs-input (it
  outranks the spinner, since claude is saying a human is blocked) and `N working` as
  running. Read from the title only: "Agents" occurs thousands of times in ordinary
  conversation text, so a body scan would flag every session that merely discusses
  agents. Verified by replaying a real capture — the same bytes classified idle before
  and needs-input after.

## [0.6.1] — 2026-07-30

A cross-terminal audit of the pane's own contract: what it tells a child it is, and
what it actually does. Every item is a place the two disagreed, so the symptom only
appeared on some hosts or under some children.

### Fixed
- **A multi-line paste could be submitted line by line.** A PTY read that ended on a
  CSI *intermediate* byte (the `$` of `\x1b[?2004$p`) was released instead of held, so
  saikai's DECRQM scanners matched neither half and the query went unanswered — a child
  that sets bracketed paste and then verifies it concluded the mode was unsupported.
  pyte's own parser spans the split, which is why nothing looked broken.
- **DECRQM no longer claims modes saikai does not implement.** The position-accurate
  overlay recorded *every* private mode a chunk carried, so a set-then-verify in one
  write was told "1 = set" for a mode nothing here honours, while the same query one
  read later correctly got "0 = not recognised".
- **The alt screen is detected in a combined DECSET.** A child that writes its whole
  entry at once (`\x1b[?1049;1002;1006h`) produced no boundary, so pyte's single buffer
  kept the pre-alt frame under the child's new UI. `AltScreenTracker` had a second copy
  of the same pattern; it now shares one.
- **Arrow keys follow the child's DECCKM state.** The pane always sent CSI while the
  mirror replays `?1h` into xterm.js, so the same arrow reached the child as `\x1bOA`
  from a browser and `\x1b[A` from the pane.
- **A dropped image is reported.** The pane answers DA1 byte-identically to Windows
  Terminal — which advertises sixel — and then drops every DCS, so a graphics payload
  vanished silently. It now says so once per pane.
- **Terminal remote-control sockets no longer reach a pane child.** kitty and alacritty
  were scrubbed by their identity variables while `KITTY_LISTEN_ON` and
  `ALACRITTY_SOCKET` went through, which let a child drive the real outer window
  (`kitten @`, `alacritty msg`); `KONSOLE_DBUS_*` was the same over D-Bus. Whole
  families are stripped now, as WezTerm's already was.
- **The list's viewport no longer flicks to the cursor row under load.** The
  cursor-scroll suppression closes on a queued callback, so a rebuild that starts
  before the previous one's callbacks drain opens a second window — and a boolean let
  the first window's end re-arm the second's pending scroll. It counts now.

## [0.6.0] — 2026-07-29

Live panes render a real terminal, and this release is mostly about making that
faithful and cheap on Windows Terminal: a pane no longer redraws itself wholesale on
every chunk, a frame can no longer mix two states of the child's screen, and the
widths in the model now match the widths on screen.

### Fixed
- **Rows in a pane no longer land out of place.** `render_line` took the pane lock
  once *per row* while Textual calls it in a per-row loop, and the reader installs a
  whole child frame atomically under that same lock — so one composited image could
  carry rows from two pyte generations (measured: 40 of 40 render passes torn). The
  positions were always right and the *contents* were stale, which is why nothing in
  the output stream explained it. The visible grid is now pinned once at the frame
  boundary and read lock-free.
- **Emoji no longer shift the rest of their row.** `⚠` is one column but `⚠`+VS16 is
  an emoji-presentation sequence that Rich — so Textual, so the terminal — draws in
  two, and the zero-width merge left the cluster owning one. Every glyph after it sat
  a column right and the row's tail spilled past the pane. Measured on a captured
  session: the rows carrying `⚠️` handed Textual a 98-cell row for a 97-cell pane.
- **The pane no longer oscillates while scrolling.** pyte erases per cell, so
  overwriting half a double-width glyph left the other half behind — a state no
  terminal can display. Rendering it forced a choice between eating a character and
  shifting the row, and a redrawing child alternated between the two. saikai now
  erases the whole glyph when either half is overwritten, as a real terminal does.
- **Copying with the mouse no longer corrupts the pane.** The same per-row read let
  `_scroll` and the freeze snapshot flip mid-frame, mixing pinned and live rows.
- **The session list no longer jumps back while you scroll it.** Three separate places
  moved the viewport during a rebuild: the rebuild itself (`clear()` zeroes `scroll_y`),
  a deferred restore that re-applied a pre-wheel position a frame later, and
  `move_cursor(scroll=False)`, which did not actually prevent scrolling. The user's
  scroll position is now owned state rather than a snapshot guarded by a timer —
  measured on the real app under constant rebuilds: 0 backwards jumps and full travel,
  where before most of the travel was lost.
- **A resize that changes nothing is ignored.** Textual re-posts `Resize` on any
  relayout, and each one dropped the pane's scrollback offset, dirtied every pyte line
  and sent the child `SIGWINCH`. A real session logged seven identical `36x97` resizes
  in eight seconds.
- **The native cursor no longer flickers over the list.** Frames are bracketed in DEC
  2026 synchronized output on Windows (Textual's Windows driver never probes for it),
  the list's mouse-hover repaint is gone, and the periodic anchor no longer re-asserts
  `?25h` on every tick.
- **IME anchoring follows the caret.** The anchor rides the repaint, freezes while the
  child is mid-frame, settles on the busy→idle transition, re-anchors when leaving
  scrollback and after a resize, and heals a drifted cursor-visibility state. The pane
  presents itself as Windows Terminal, keeping `WT_SESSION` so claude tracks the caret.
- **Terminal queries are answered like a terminal.** Primary and secondary DA, DECRQM
  with real mode state, DSR at the position the child asked from, in the order asked,
  once per split unit; DCS is scrubbed before pyte without leaking payloads or opening
  a deletion seam.
- **The PTY write path never blocks the UI thread.** Oversized writes go to the pane's
  writer thread, the input queue is bounded, a refused write is logged instead of
  vanishing, and a writer that fails to start no longer swallows the data it was given.
- **Panes flagged "needs input" again** for claude's resume and permission gates on the
  alt screen.
- **Quitting takes a deliberate double press.** A single `Esc` (or `Ctrl+C`) on the
  list quit saikai outright — too easy to fire by reflex (`Esc` interrupts claude in a
  pane; Claude Code itself exits only on a *second* `Ctrl+C`). The first press arms and
  shows a hint; a second within ~2.5s quits, and any other key disarms. `Esc` still
  leaves search / dropdown / pane → list as before.

### Changed
- **A pane repaints the rows that changed, not all of them.** pyte tracks per-line
  damage and saikai never read it; a spinner frame touches one or two rows out of
  thirty-six. Measured per pane during an agent-mode storm: 115–670 ms of UI thread per
  second before, 8–33 ms after. The traps this has to survive are listed in the
  rendering invariants — a cursor move dirties nothing, `resize` leaves out-of-range
  indices, a child scroll dirties everything.
- **The web mirror no longer forces a full-screen relayout per frame.** Its
  overflow-recovery repaint was the only `refresh(layout=True)` in the repository and
  fired on essentially every drain during a storm; it is now rate-limited, gated on a
  connected viewer, and asks for a repaint rather than a relayout. The statusbar also
  stopped requesting a layout pass on every update.

### Added
- **`SAIKAI_DIAG=1` captures the whole display picture in one run** — per-pane child
  bytes, what saikai wrote to the terminal, per-second render accounting, a scroll /
  focus / resize trail, rows whose rendered width disagrees with the pane (with the
  row's text), and an automatic model dump at the first such row. One directory per
  launch. `SAIKAI_FULL_REPAINT=1` and `SAIKAI_NO_SYNC=1` isolate the two riskiest
  behaviours in a single variable.
- **Documented rendering invariants and a debugging guide.**
  [ARCHITECTURE.md](docs/ARCHITECTURE.md#rendering-invariants) lists the five
  invariants with the symptom each produces when it breaks;
  [DEBUGGING.md](docs/DEBUGGING.md) covers reading the diagnostics, replaying a capture
  offline, attributing a defect to saikai or to the child, and what each instrument
  cannot distinguish.

### Known limitations
- **East Asian Ambiguous characters (`①`, and others) can overhang their cell.** Their
  width is not defined by the standard: the terminal advances one column while a CJK
  fallback font may draw two. This reproduces with claude in Windows Terminal *without*
  saikai, and inside saikai the model and the renderer agree on every one of the 639
  distinct non-ASCII characters in a captured session — so there is nothing for saikai
  to fix here.
- **claude's own transient renders show through.** Its spinner cycles, and its
  transcript briefly re-renders at a shifted offset; saikai reproduces the child's
  screen faithfully, including those.

> Releases 0.4.0 – 0.5.2 were tagged without CHANGELOG entries. This entry covers the
> work since 0.5.2 only; `git log v0.3.0..v0.5.2` is the record for that gap.

## [0.3.0] — 2026-06-15

### Added
- **Web mirror (opt-in).** saikai can mirror its live UI to a phone or another
  browser over the LAN — **off by default** and **token-authenticated**. It is
  **read-only** until you arm browser control at the terminal with `Shift+F12`;
  then the browser drives saikai by tap (select a row, sort a column), a
  single-finger swipe to scroll, an on-screen key bar, and a
  terminal-equivalent physical keyboard (arrows, Home/End, F-keys, Ctrl/Alt
  combos, `Ctrl+]` to leave a pane, `Ctrl+C` to interrupt claude). Launch shows a
  scannable QR code; press `F12` to bring it back.
- The mirror is **safe by construction**: only the local `Shift+F12` can enable
  control (a browser can never arm itself), control auto-disables after
  inactivity, a LAN bind stays read-only unless you also set
  `SAIKAI_MIRROR_ALLOW_LAN_INPUT=1`, the per-run write-key for input travels only
  over the authenticated stream (never the URL, QR, or logs), and the status bar
  shows a live connected-browser count with a toast on each connect. New env
  vars: `SAIKAI_MIRROR`, `SAIKAI_MIRROR_HOST`, `SAIKAI_MIRROR_PORT`,
  `SAIKAI_MIRROR_ALLOW_LAN_INPUT`.
- A shared fictional demo fixture now generates the public screenshots,
  deterministic headless GIF, and recording workspace without reading the
  caller's real HOME or Claude history.
- An isolated real-Claude recording guide and asciinema cast auditor reject
  private paths, credentials, identities, and unapproved demo projects before
  conversion.
- `docs/ARCHITECTURE.md` is now the canonical contributor reference for module
  boundaries, history semantics, PTY lifecycle, and concurrency invariants.

### Changed
- **The live-pane memory gate now reasons per-OS instead of projecting
  Windows' commit-charge model onto Linux/macOS.** Linux: commit headroom
  (`CommitLimit − Committed_AS`) is consulted only under strict overcommit
  (`vm.overcommit_memory=2`) — under the default heuristic mode the limit is
  not enforced and `Committed_AS` routinely exceeds it on a healthy machine,
  which read as negative headroom and falsely closed the gate. A new
  pressure check (`SAIKAI_MAX_MEM_PRESSURE`, default 10) reads Linux PSI
  (`/proc/pressure/memory` `some avg10`, the stall-time metric systemd-oomd
  acts on) and macOS's kernel pressure level (gates on *critical*), refusing
  a new pane when tasks are measurably stalling regardless of occupancy
  numbers. The memory-load high-water default is now 95 on Linux/macOS
  (85 stays on Windows): the POSIX load % is derived from the same
  availability figure as the physical floor, so the old shared default
  closed the gate while ~15% of RAM was still genuinely available.
- The public story now leads with a one-line "Mission control for Claude Code"
  value statement and the hero demo — which opens on the cross-project session
  list — with the `claude --resume` rationale moved just below.
- Help, Settings, and the READMEs explain the visual grammar consistently:
  title color groups context and ASCII symbols report state.
- Contributor-facing agent files are concise entrypoints instead of duplicate
  copies of the concurrency manual.

### Removed
- The incomplete global LLM cluster mode and its dangling UI/CLI controls.
- Internal launch-marketing notes and completed implementation plans from the
  public repository surface.

### Fixed
- The filter bar no longer mis-sizes: the Sort/Status/Age dropdowns are wide
  enough that long labels (e.g. "Alphabetically", "All time") no longer wrap,
  and the search box is capped so it does not expand to dwarf them.
- Claude Desktop session sync (`--sync-desktop`) derives its store location
  per-OS (macOS `~/Library/Application Support`, Linux XDG) instead of only the
  Windows path, so it no longer reports "not found" on macOS where Desktop runs.
- The mirror-URL host-clipboard copy on Linux tries `wl-copy` (Wayland), then
  `xclip`, then `xsel`, instead of assuming `xclip` is installed.
- Public docs, in-app marker help, contributor test commands, and CI now agree
  with the current marker semantics, terminal-support boundaries, and complete
  `tests/test_*.py` suite.
- **Tree mode no longer chains unrelated same-repo sessions into one long
  list.** Parent assignment treated "same cwd + recent" as kinship and scored
  a `main`↔`main` branch match as strong evidence, so in a single-repository
  history every session linked to its nearest predecessor. A parent now
  requires continuation evidence — a shared *feature* branch (`main`/`master`/
  detached `HEAD` count as no-information), or title/topic overlap — and
  sessions without such evidence stay roots. Genuine continuation chains
  (e.g. a series of sessions iterating on the same prompt) still nest.
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
- The README documents why Space does not steal input from search fields,
  dropdowns, or live panes, its `Space Space` marking trade-off, and how to
  restore conventional Space-to-mark behavior.

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
  `Space Space` batch-mark, and more; pausing after Space hints the full map and
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
