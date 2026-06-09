# recap public release — config, customization & OSS conventions (design)

- **Date:** 2026-06-10
- **Status:** design approved in brainstorm; spec for review
- **Scope:** make recap a properly customizable, conventionally-structured
  open-source project, then publish a clean history (Model A).

## Goal

Today recap is configured only by `RECAP_*` environment variables, has no config
file, always tries to summarize via `claude -p` (spends credits), hard-codes its
keymap on Function keys, and lacks the usual OSS scaffolding. Before going public
we add: a TOML config file, an optional + pluggable summarizer, remappable
keybindings with an opt-in leader/prefix mode, a few CLI conveniences, and
Standard OSS scaffolding. Then we clean the git history and make the repo public.

## Non-goals

- A GUI / settings screen — config is a hand-edited TOML file (+ a generator).
- Per-project config — one user-level config file (v1).
- Replacing env vars — they keep working (back-compat); config is the new primary.
- CODE_OF_CONDUCT / pre-commit / ruff (deferred; "Full" tier rejected as
  community-scale ceremony + lint churn on a single-file codebase).

---

## A. Configuration system

**Format:** TOML (matches `pyproject.toml`; comments + types; user-editable).
Parse with stdlib `tomllib` (Python ≥ 3.11) or `tomli` (3.10). Add
`tomli ; python_version < "3.11"` to both the PEP-723 inline header and
`pyproject.toml` dependencies.

**File location** — first that exists wins:
1. `$RECAP_CONFIG` (explicit path)
2. `$XDG_CONFIG_HOME/recap/config.toml`
3. `~/.config/recap/config.toml`  ← default (parallels the existing
   `~/.cache/recap/` convention; works on Windows too)

A missing/empty/corrupt file is non-fatal: recap logs a one-line warning and
falls back to env + defaults (same resilience as `_read_json`).

**Precedence (highest → lowest):** CLI flag → environment variable → config file
→ built-in default. Implemented by a single resolver so every setting reads from
one place.

**Schema:**

```toml
[summary]
enabled = false      # default OFF — claude -p spends credits (opt-in)
command = ""         # custom backend: prompt on stdin → summary on stdout; "" = claude -p
model   = "haiku"    # model used when command is empty

[display]
auto_refresh = 0     # seconds between background re-scans; 0 = off
split_live   = true  # false = list-only browser (full-takeover resume)

[limits]
min_free_mb   = 1536
claude_mb     = 600
hard_ram_gate = false
max_live      = 64

[keys]               # see section C; action = "key" overrides + optional leader
# leader  = "ctrl+g"
# refresh = "f5"
```

**Env-var ↔ config mapping** (env overrides config):

| env var | config key |
|---|---|
| `RECAP_SPLIT_LIVE` | `display.split_live` (tri-state opt-out, unchanged) |
| `RECAP_AUTO_REFRESH` | `display.auto_refresh` |
| `RECAP_SUMMARIZE_CMD` | `summary.command` |
| `RECAP_MIN_FREE_MB` / `RECAP_CLAUDE_MB` | `limits.min_free_mb` / `limits.claude_mb` |
| `RECAP_HARD_RAM_GATE` | `limits.hard_ram_gate` |
| `RECAP_MAX_LIVE` | `limits.max_live` |
| `RECAP_RELEASE_KEY` | `keys.release` |
| *(new)* `RECAP_SUMMARIZE_ENABLED` | `summary.enabled` |

---

## B. Summarizer — optional + pluggable

- **`summary.enabled = false` (default):** recap does **not** call any
  summarizer. The Title column degrades to the existing first-user-message
  heuristic (`_first_msg` / `_pane_title`), so it is never blank.
- **`summary.command`:** a custom backend (the existing `RECAP_SUMMARIZE_CMD`
  contract — prompt on stdin, one-line summary on stdout). An internal or custom
  summarizer plugs in by setting `command = "<your-tool> …"` in the user's
  **local, uncommitted** config — the public source stays generic.
- **`summary.model`:** model passed to `claude -p` when `command` is empty.
- **First-run UX:** on first launch with no config file present *and* summaries
  off, show a one-time hint (notify): summaries are off, they call `claude -p`
  and spend credits; run `recap --init-config` then set `[summary] enabled =
  true`, or `RECAP_SUMMARIZE_ENABLED=1`. Persist a "hint shown" flag in the
  cache dir so it shows once.

---

## C. Keybindings — remap + opt-in leader mode

**Why not single letters / why a leader:** recap hosts an interactive `claude`
REPL (like tmux/zellij), so the inner program owns the letter+Ctrl key space; the
list also uses type-to-search. Function keys are the collision-safe default but
are not mnemonic and are stolen by macOS hardware functions. The modern idiom for
REPL-hosting TUIs is a leader/prefix key — added here as an opt-in.

**Default keymap (single source of truth — `DEFAULT_KEYMAP`):**

| action | default | action | default |
|---|---|---|---|
| `refresh` | `f5` | `prev_tab` | `f2` |
| `favorite` | `f6` | `next_tab` | `f3` |
| `hide` | `f7` | `attention_jump` | `shift+f3` |
| `diff` | `f8` | `toggle_list` | `f4` |
| `copy_prompt` | `f9` | `new_session` | `shift+f8` |
| `tree` | `shift+f5` | `restore_panes` | `shift+f4` |
| `cluster` | `shift+f6` | `close_active` | `f10` |
| `cycle_group` | `shift+f7` | `close_all` | `shift+f10` |
| `freeze` | `shift+f9` | `help` | `?` |
| `release` | `ctrl+]` | `quit` | `esc` / `ctrl+c` (fixed) |

`quit` stays `esc`/`ctrl+c` and is **not** remappable (safety). The rest are
remappable via `[keys]`.

**Override application:** build `BINDINGS` from `DEFAULT_KEYMAP` merged with
validated `[keys]` overrides (the App is created inside `textual_pick`, so the
loaded config is available before bindings are used).

**Validation (startup, fail loud):**
- Unknown action name → error listing valid actions.
- Duplicate: two actions mapped to the same key → error.
- Reserved key → error: bare `ctrl+<letter>` (readline) except the historically
  allowed `ctrl+c` / `ctrl+]`; this extends the existing
  `test_no_app_binding_steals_a_readline_ctrl_key` rule to user config.

**Leader mode (opt-in):**
- `[keys] leader` (default unset/`""` = disabled; suggested `"ctrl+g"`).
- **Active only when the list is focused** (search bar closed). When a live pane
  is focused the leader is **passed through to claude** — so the leader never
  steals a key from the REPL/readline (this reconciles a leader with recap's
  no-readline-collision rule; unlike tmux, recap does not globally grab it).
- Press leader → enter "pending" state, show a one-line hint bar of the bound
  letters; the next key fires the mapped action, or `Esc` / unmapped key / a
  short timeout cancels.
- When leader is enabled, single-letter `[keys]` values (e.g. `refresh = "r"`)
  are interpreted as **leader-then-letter**; F-key values stay as direct global
  bindings. Both can coexist (F5 *and* leader-r both refresh). Post-leader
  letters are validated for uniqueness.

---

## D. CLI additions

- `recap --version` → prints `__version__`. Single source: `__version__` in
  `recap.py`; `pyproject.toml` reads it via hatchling `dynamic = ["version"]`.
- `recap --init-config` → writes a commented `config.toml` template to the
  default location (never overwrites without `--force`); prints the path.
- `recap --print-config` → prints the resolved settings and the source of each
  (default / config / env / cli) — a debugging aid.

---

## E. OSS scaffolding (Standard + `.gitattributes`)

- `.github/workflows/ci.yml` — matrix **Python 3.10–3.13 × {ubuntu, windows,
  macos}**; steps: `py_compile` + the four headless test files (no textual
  needed). This also gives real Linux/macOS signal for the core logic and lets
  the Platform-support table cite CI.
- `CONTRIBUTING.md` — setup (uv), running tests, house-style notes, PR flow.
- `CHANGELOG.md` — Keep a Changelog format; seed with `0.1.0`.
- `.github/ISSUE_TEMPLATE/bug_report.md` (+ `feature_request.md`) — bug report
  **requires OS / terminal / Python version / split-live on-off**.
- `.github/PULL_REQUEST_TEMPLATE.md`.
- README badges: CI status, license, Python version.
- `.gitattributes`: `* text=auto eol=lf` — ends the recurring
  "LF will be replaced by CRLF" churn.

---

## F. Testing (all textual-free, pure logic)

New headless tests (added to the existing suite):
- Config precedence: CLI > env > config > default, per setting.
- Config file location resolution (`RECAP_CONFIG` / `XDG_CONFIG_HOME` /
  `~/.config`) + missing/corrupt file falls back without raising.
- Env↔config mapping (each env var still wins over the config value).
- Keymap override: valid override applies; unknown action, duplicate key, and
  reserved (`ctrl+<letter>`) key each rejected with a clear error.
- Leader parsing: single-letter values become leader-sequences when leader set;
  post-leader duplicates rejected; leader unset → letters rejected as direct
  binds (must be F-key/combo).
- Summary disabled → Title falls back to the first-message heuristic (not blank).
- Existing guard `test_no_internal_identifiers_in_source` continues to pass.

---

## G. Implementation sequence

1. **Config core** — `__version__`, location resolver, TOML load, `Settings`
   resolver (CLI>env>config>default) + tests. Add `tomli` dep marker.
2. **Wire existing knobs** through `Settings` (summary/display/limits); env vars
   keep winning. No behavior change when no config file exists.
3. **Summary optional** — `summary.enabled` default off, heuristic Title
   fallback, first-run hint.
4. **Keymap** — `DEFAULT_KEYMAP`, `[keys]` overrides + validation, then **leader
   mode** (pending-state machine + hint bar) + tests.
5. **CLI** — `--version`, `--init-config`, `--print-config`.
6. **OSS scaffolding** — CI, CONTRIBUTING, CHANGELOG, templates, badges,
   `.gitattributes`.
7. **(Separate final phase) history clean + publish** — see H.

Each step is a small, individually-tested commit (per repo testing discipline).

---

## H. History clean + publish (Model A — separate phase)

After the code work, with explicit go-ahead (destructive + outward-facing):
- Install `git-filter-repo` (`uv tool install git-filter-repo`).
- Rewrite **all** commits' author/committer to the chosen public identity
  (`m-morino` + GitHub `…@users.noreply.github.com`) and scrub the internal
  codename(s) from all history content. Keep the full granular commit graph
  ("nicely-developed history" preserved).
- The scrub-rules file (which names the internal codename(s)) lives **outside**
  the repo.
- A pre-rewrite backup already exists: tag `pre-public-backup` + bundle
  `../recap-prepublic-backup.bundle`.
- Force-push the cleaned history to `m-morino/recap`, then flip visibility to
  public.
- `docs/superpowers/` is **kept** (no PII; shows design rationale).

---

## Open questions / risks

- **Leader default key:** `ctrl+g` suggested but unset by default; confirm at
  implementation (must be free on the list and not a macOS hardware key).
- **Leader state machine** is the largest new piece; the pending-state must not
  interfere with the reader-thread lock invariants (it is pure UI-thread state).
- **`dynamic` version** via hatchling needs the `[tool.hatch.version]` path set
  to `recap.py`'s `__version__`.
