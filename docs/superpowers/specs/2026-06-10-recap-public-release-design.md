# recap public release — config, customization & OSS conventions (design)

- **Date:** 2026-06-10
- **Status:** design approved in brainstorm; **revised after a 3-expert review**
  (Textual/TUI, Python packaging/config, security/git-history). Spec for final
  user review before writing the implementation plan.
- **Scope:** make recap a properly customizable, conventionally-structured
  open-source project, then publish a clean history to a fresh public repo.

## Goal

Today recap is configured only by `RECAP_*` environment variables, has no config
file, always tries to summarize via `claude -p` (spends credits), hard-codes its
keymap on Function keys, and lacks the usual OSS scaffolding. Its git history
carries an internal codename (in 3 commits' content *and messages*) and a
corporate author identity on every commit. Before going public we add: a TOML
config file, an optional + pluggable summarizer, remappable keybindings with an
opt-in leader/prefix mode, a few CLI conveniences, and Standard OSS scaffolding;
then we publish a cleaned history to a fresh repo.

## Decisions (locked)

- Config format: **TOML**. Summaries: **default OFF** (opt-in; credits). Keymap:
  **F-key default + full remap + opt-in leader**. OSS: **Standard + `.gitattributes`**.
- Distribution: **GitHub source for v1; PyPI later** → ship PyPI-*ready* metadata
  now, defer the publish workflow.
- Publish target: **a fresh public repo** (safest; GitHub never holds the dirty
  objects). Exact old-repo disposition + naming: see §H (being decided with user).
- Public identity: **`m-morino` / `11384605+m-morino@users.noreply.github.com`**
  (ID-based noreply — username-only form breaks if the account is ever renamed).

## Non-goals

- GUI/settings screen; per-project config; replacing env vars (kept, back-compat).
- ruff / pre-commit / CODE_OF_CONDUCT / `py.typed` / dependabot — confirmed
  overkill for a solo single-file app by the packaging reviewer.

---

## A. Configuration system

**Format:** TOML; parse with **stdlib `tomllib`**. The project floor is **Python
≥ 3.11** (decided to keep deps minimal), so there is **no TOML-parser dependency**
(`tomli` is not needed).

**Location — use `platformdirs`, not a hand-rolled `~/.config`** *(reviewer fix:
`XDG_CONFIG_HOME` is unset on Windows, so `~/.config` is non-idiomatic there;
`platformdirs` resolves `%APPDATA%` on Windows, `~/.config` on Linux,
`~/Library/Application Support` on macOS).* `platformdirs` is small and already a
transitive dep of textual — but declare it **explicitly** in deps (don't rely on
a transitive). Resolution:
1. `$RECAP_CONFIG` (explicit path) →
2. `platformdirs.user_config_dir("recap")/config.toml`

Also move the **cache** from the hand-rolled `~/.cache/recap` to
`platformdirs.user_cache_dir("recap")`, with a one-time **legacy fallback**: if
the old `~/.cache/recap` exists and the new dir does not, keep using the old path
(no forced migration, no data loss).

Missing/empty/corrupt config → one-line warning, fall back to env + defaults.

**Precedence (high→low):** CLI flag → env var → config → built-in default
(single resolver).

**Schema:**
```toml
[summary]
enabled = false      # default OFF — claude -p spends credits (opt-in)
command = ""         # custom backend: prompt on stdin → summary on stdout; "" = claude -p
model   = "haiku"
[display]
auto_refresh = 0     # seconds; 0 = off
split_live   = true  # false = list-only
[limits]                            # Windows-principled RAM gate — see §A.1
max_memory_load        = 85         # dwMemoryLoad % high-water (Windows' pressure metric)
min_commit_headroom_mb = 2048       # keep this much process commit free (the freeze cause)
min_free_phys_pct      = 8          # keep >= this % of physical RAM available (anti-thrash)
per_pane_mb            = 600        # est. commit per claude tree (measured when possible)
hard_ram_gate          = false      # warn (default) vs hard-refuse
max_live               = 64         # backstop cap
[keys]               # see §C: action = "key" overrides + optional leader
# leader  = "ctrl+g"
# refresh = "f5"
```

**Env↔config mapping** (env overrides config): `RECAP_SPLIT_LIVE` →
`display.split_live` (tri-state opt-out, unchanged); `RECAP_AUTO_REFRESH`,
`RECAP_SUMMARIZE_CMD`→`summary.command`, `RECAP_MIN_FREE_MB`/`RECAP_CLAUDE_MB`,
`RECAP_HARD_RAM_GATE`, `RECAP_MAX_LIVE`, `RECAP_RELEASE_KEY`→`keys.release`, new
`RECAP_SUMMARIZE_ENABLED`→`summary.enabled`. The legacy `RECAP_MIN_FREE_MB` /
`RECAP_CLAUDE_MB` still map (back-compat) but the gate is redesigned — see §A.1.

---

## A.1 RAM gate — derived from Windows resource management

**Problem:** with several live `claude` panes the machine sometimes goes
unresponsive. The current gate is `ullAvailPhys − RECAP_CLAUDE_MB(600) <
RECAP_MIN_FREE_MB(1536)` — ad-hoc magic numbers on the wrong signal.

**First-principles (verified against MS docs — `MEMORYSTATUSEX`, "Introduction to
page files"):**
- `ullAvailPhys` is **standby + free + zero lists** — it *includes reclaimable
  cache*, so it over-states real headroom; gating on it lets panes open while the
  system is already under commit pressure.
- The documented cause of system-wide **freezing/stalls** is the **system commit
  charge approaching the commit limit** (RAM + page file). The relevant signal is
  **commit headroom** = `ullAvailPageFile` (per-process available commit). When it
  nears zero the OS pages hard and (at 90% of the limit) auto-grows the page file
  — a disk-I/O storm that *is* the "PC went heavy".
- `dwMemoryLoad` (0–100) is Windows' own physical-pressure metric — machine-
  relative, no magic MB.

**Redesigned gate — refuse/warn opening another pane if ANY holds:**
1. `dwMemoryLoad >= max_memory_load` (default 85) — already under pressure;
2. `ullAvailPageFile/MB − per_pane_mb < min_commit_headroom_mb` — would eat into
   the commit headroom that prevents freezing (the primary, principled check);
3. `ullAvailPhys/MB − per_pane_mb < min_free_phys_pct% × ullTotalPhys/MB` —
   anti-thrash physical floor, **relative** to the machine, not a fixed 1536.

All three thresholds are relative/Windows-derived. `per_pane_mb` should be
**measured** when possible — sum the actual private commit of recap's spawned
claude PIDs via `GetProcessMemoryInfo` (`PrivateUsage`) — falling back to the
estimate. `_avail_ram_mb()` is extended to return the full `(load, avail_phys,
avail_commit, total_phys)` tuple (POSIX: derive load/commit from
`/proc/meminfo` MemAvailable + Commit*; macOS: load only → physical+load checks,
commit check skipped). **Stretch:** a runtime watcher toasts "memory pressure —
consider closing panes" when `dwMemoryLoad` crosses the high-water *after* open
(the gate is open-time only; already-open claude trees can grow).

---

## B. Summarizer — optional + pluggable

- **`summary.enabled = false` (default):** no summarizer call. Title degrades to
  the existing first-user-message heuristic (never blank).
- **`summary.command`:** custom backend (existing `RECAP_SUMMARIZE_CMD` contract,
  stdin→stdout). An internal/custom summarizer plugs in via `command = "<tool> …"`
  in the user's **local, uncommitted** config — public source stays generic.
- **`summary.model`:** model for `claude -p` when `command` is empty.
- **UX (reviewer fix):** a heuristic Title must be visually distinguishable from
  an AI one (subtle dim/prefix) so first-run users don't read it as "summaries
  broken"; the enable hint is a **persistent footer affordance**, not only a
  one-time notify. `--init-config` writes `enabled = false` with the credits
  rationale inline.

---

## C. Keybindings — remap (via `set_keymap`) + opt-in leader

**Override mechanism — use Textual's first-class API, not closure-rebuilt
BINDINGS** *(reviewer fix):* give each default `Binding` a stable `id`
(`Binding("f5","refresh","Refresh",id="refresh")`), keep `BINDINGS` **static**,
and apply validated `[keys]` overrides in `on_mount` via `App.set_keymap(Keymap)`
(`Keymap = {binding_id: key_string}`). This keeps the static-analysis guard
(`test_no_app_binding_steals_a_readline_ctrl_key`) reading a stable list.

**Default keymap (`DEFAULT_KEYMAP`, action→key, each a binding id):** refresh
`f5`, favorite `f6`, hide `f7`, diff `f8`, copy_prompt `f9`, tree `shift+f5`,
cluster `shift+f6`, cycle_group `shift+f7`, freeze `shift+f9`, release `ctrl+]`,
prev_tab `f2`, next_tab `f3`, attention_jump `shift+f3`, toggle_list `f4`,
new_session `shift+f8`, restore_panes `shift+f4`, close_active `f10`, close_all
`shift+f10`, help `?`. **`quit` (`esc`/`ctrl+c`) is fixed, not remappable.**

**Validation (startup, fail loud):** unknown action id → error listing valid
ids; duplicate key → error; reserved key → error for bare `ctrl+<letter>`
(readline) except `ctrl+c`/`ctrl+]`. The **release key** (`keys.release`,
default `ctrl+]`) is exempt from the bare-ctrl rule, must be a key ConPTY
delivers, and must stay popped from the pane's `_KEYMAP` (recap_terminal.py).

**Leader mode (opt-in) — a manual `on_key` state machine** *(reviewer fix:
Textual has NO native chord/sequence binding; comma in a KeyString means
*alternatives*, not a sequence).*
- `[keys] leader` (default unset/`""` = disabled; suggested `"ctrl+g"`, which is
  non-printable so it won't trigger type-to-search).
- Implemented in **`App.on_key`, NOT a priority Binding** — a priority binding
  would fire even over a focused pane; plain `on_key` lets us gate it. Active
  **only when the list (DataTable) is focused**; when a pane is focused the key
  bubbles to claude (pass-through) — so the leader never steals a REPL key.
- The pending-state branch must be handled **at the top of `on_key`, before** the
  type-to-search / `space` (batch-mark) / `enter` (resume) branches, or the first
  post-leader letter double-fires.
- On leader: enter pending state, show a docked `Static` **hint bar** of bound
  letters; next key fires the mapped action, or `Esc`/unmapped/`set_timer(~1.5s)`
  timeout cancels. Pure UI-thread state (does not touch `self._lock`).
- When leader is set, single-letter `[keys]` values are interpreted as
  leader-then-letter (validated unique); F-key values stay direct global binds —
  both coexist (F5 *and* leader-r refresh).

**Command palette (reviewer add):** re-enable Textual's command palette on a
free key (the leader key works) with a `Provider` exposing the meta actions — a
discoverable, self-documenting complement to the leader (recap disabled it only
for the `ctrl+p` collision; `COMMAND_PALETTE_BINDING` is overridable).

**Help & footer — single source, MECE, width-aware** *(user requirement: the
footer and the `?` overlay currently disagree — the overlay is a hand-written
string that drifts from the auto-generated footer):*
- Generate BOTH the always-visible footer AND the `?` overlay from the **same**
  `DEFAULT_KEYMAP` (+ active overrides), so they can never drift. Each binding
  carries a `key_display`, a short label (footer) and a long description
  (overlay), grouped: Navigation / List actions / Split-live / Filter.
- **Responsive to terminal size** (read `self.app.size`): as width shrinks the
  footer drops lowest-priority bindings first and falls back to abbreviated
  `key_display` (priority-ordered so the most-used keys survive); the `?` overlay
  is a **scrollable** `ModalScreen` (never overflows a short terminal) and, below
  a width threshold, switches from the multi-column table to a compact
  one-binding-per-line list with ellipsised descriptions.
- Degenerate cases: a very small terminal still shows something usable (at least
  a "press ? for help" hint when the footer is truncated); the overlay scrolls
  rather than clipping. Document the two layers — **global** (F-keys, release)
  vs **list-only** (leader sequences).

---

## D. CLI additions

- `recap --version` → prints `__version__`. Define `__version__ = "0.1.0"` in
  `recap.py`; in `pyproject.toml` set `dynamic = ["version"]`, **remove the static
  `version = "0.1.0"`** (they conflict), and use the hatch **`regex`** source
  (`[tool.hatch.version] path = "recap.py"`). *Reviewer fix: NOT the `code`
  source — it imports the module, which would fail at build (recap.py imports
  textual at top-level).*
- `recap --init-config` → writes a commented template to the `platformdirs`
  config path (`mkdir(parents=True)`; never overwrite without `--force`).
- `recap --print-config` → resolved settings + per-setting source
  (default/config/env/cli).

---

## E. OSS scaffolding (Standard + `.gitattributes` + reviewer adds)

- `.github/workflows/ci.yml` — matrix **Python 3.11–3.13 × {ubuntu, windows,
  macos}**: `py_compile` + the four headless test files + the **history PII gate**
  (§F). Gives real Linux/macOS signal for the core logic.
- `CONTRIBUTING.md`, `CHANGELOG.md` (Keep a Changelog, seed `0.1.0`),
  `.github/ISSUE_TEMPLATE/bug_report.md` (requires OS/terminal/Python/split-live)
  `+ feature_request.md`, `.github/PULL_REQUEST_TEMPLATE.md`.
- README badges (CI, license, Python); `.gitattributes`: `* text=auto eol=lf`.
- **Reviewer adds:** `SECURITY.md`; `[project.urls]` (Homepage/Source/Issues);
  per-version Python classifiers + `Operating System :: OS Independent`.
- **PyPI-ready, deferred:** metadata above makes a future PyPI release trivial;
  the Trusted-Publishing (OIDC) release workflow is **deferred** (v1 installs from
  GitHub: `uv tool install .` / `pipx install git+https://github.com/m-morino/recap`).

---

## F. Testing + history hygiene gate

All textual-free, in the existing suite:
- Config precedence (CLI>env>config>default) per setting; location resolution via
  `platformdirs` + `$RECAP_CONFIG`; missing/corrupt file falls back without raising.
- Env↔config mapping (env wins).
- Keymap: valid override applies via `set_keymap`; unknown id / duplicate / bare
  `ctrl+<letter>` rejected; release-key exemption honored.
- Leader: single-letter values become sequences when leader set; duplicates
  rejected; leader unset → letters rejected as direct binds.
- Summary disabled → Title = heuristic (not blank).
- Existing HEAD guard `test_no_internal_identifiers_in_source` (scans shipped
  `.py` + docs; derives PII at runtime; codenames split-concatenated).

**History-level PII gate (reviewer fix — HEAD scan can't see history):**
`scripts/check-history.sh` + a CI job, failing on any historical leak. The
published script uses **generic** patterns only (so it leaks no codename):
- author/committer email not on an allowlist (`*@users.noreply.github.com`,
  `noreply@anthropic.com`) → catches any corporate domain;
- author/committer name containing non-ASCII or `/` → catches a name+org+dept;
- plus an **out-of-repo** deny-patterns file (gitignored / CI secret) for
  specific codenames.
Also a documented `pre-push` hook rejecting new commits whose author/committer is
a corporate domain.

---

## G. Implementation sequence

1. `__version__` + `platformdirs` config/cache location + TOML load + `Settings`
   resolver (CLI>env>config>default) + tests; dep: `platformdirs` (already a
   textual transitive → zero install weight; no `tomli`, stdlib `tomllib`).
2. Wire existing knobs through `Settings` (env still wins); no behavior change
   without a config file.
3. Summary optional (default off) + heuristic Title (visually distinct) + footer hint.
4. Keymap: `Binding(id=…)` + `set_keymap` overrides + validation; then leader
   (`on_key` state machine + hint bar) + command-palette re-enable + tests.
5. CLI: `--version` (hatch regex dynamic) / `--init-config` / `--print-config`.
6. OSS scaffolding (CI incl. history gate, docs, templates, badges, SECURITY,
   urls, classifiers, `.gitattributes`).
7. **(Separate) clean history + publish to a fresh repo** — §H.

Small, individually-tested commits (repo discipline).

---

## H. History clean + publish (fresh repo — expert sequence)

Destructive + outward-facing → run only with explicit go-ahead. Backups already
exist: tag `pre-public-backup` + `../recap-prepublic-backup.bundle`.

**Tooling:** `uv tool install git-filter-repo`.

**Two out-of-repo rules files** (never inside the repo — they name the codename):
`--replace-text` (codename in content) and **`--replace-message`** (codename +
the corporate-org string in commit *messages* — required: commits `f311fc1` and
`077137c` carry the codename/org in their messages, which a content-only scrub
misses).

**Identity:** `--mailmap` rewrites **both author and committer**, name+email →
`m-morino` / `11384605+m-morino@users.noreply.github.com`; the corporate email
domain and the department string must appear nowhere after.

**Recommended safe sequence:**
1. Land all code/scaffolding commits (§G 1–6) while private.
2. Write the two out-of-repo rules files.
3. **Fresh clone** for the rewrite (`git clone --no-local . ../recap-rewrite`) so
   reflogs / unreachable dirty objects don't follow.
4. `git filter-repo --replace-text … --replace-message … --mailmap …`.
5. **Verify (all empty/clean):** `git log --all --format='%an|%ae|%cn|%ce'|sort -u`;
   `git log --all -i --grep` for the org; `git log --all --format=%B` scan;
   `scripts/check-history.sh`; the HEAD guard test.
6. Delete the `pre-public-backup` tag locally; confirm `git for-each-ref` shows
   only `master`. **Push `master` ONLY — never `--tags`/`--mirror`** (they'd drag
   the dirty tag + old SHAs back).
7. **Fresh public repo** (user-chosen): push the clean `master` to a brand-new
   empty repo so GitHub's object store never held the dirty commits. Old-repo
   disposition (rename→private archive vs delete) + the new repo's name: **being
   decided with the user** (the `recap` name is currently taken by the existing
   private repo).
8. Flip the new repo to **public** only after server-side verification.
9. Keep the `.bundle` in a private, non-synced location.

---

## I. List-UX polish (from review)

- **Colour legend + configurable basis.** The list colours nothing in the help:
  the **Title hue = project** (palette per project), **Last-column colour =
  recency** (green <5m / yellow <30m / grey older), the Wt/Topic columns hue by
  worktree/topic — all undocumented in the TUI (`?` explains only the marker
  glyphs). Add the colour key to the `?` overlay (folded into §C's MECE+responsive
  help). Make the Title basis configurable: `[display] color_by =
  "project" | "worktree" | "topic" | "none"` (default `project`; `none` for users
  who find the hues noisy — the colour maps already exist, so this is a selector).
  Optionally expose the recency thresholds (`active_secs` / `recent_secs`).
- **Skip category (group-header) rows.** When grouping is on, the group-header
  rows are not selectable sessions; arrow navigation and Enter must **skip** them
  (cursor jumps header→first child; Enter on a header is a no-op or toggles the
  group). Verify current behaviour and make non-data rows non-landable.
- **Split width.** A draggable divider between the list and the pane (persist
  `display.split_ratio`) plus the existing F4 show/hide and a clickable affordance
  (see the earlier UX discussion).

---

## Open questions / risks

- **Publish mechanics (with user):** keep the old private `m-morino/recap` as a
  renamed private archive vs delete it; the new public repo's name; create-private
  -then-flip vs create-public.
- **Leader default key:** `ctrl+g` is the candidate (non-printable; free on the
  list); confirm it isn't a macOS hardware key and isn't needed by claude when
  *list*-focused (it isn't — claude only sees keys when a pane is focused).
- **`platformdirs` cache move** needs the legacy `~/.cache/recap` fallback so
  existing users keep their data.
- **Leader state machine** must stay pure UI-thread (no `self._lock`,
  no `call_from_thread`) per the recap concurrency invariants.
