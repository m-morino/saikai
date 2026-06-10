# recap public-release implementation plan (config / summary / keymap+leader / CLI / UX / history-gate)

> **For agentic workers:** REQUIRED SUB-SKILL: use superpowers:executing-plans (inline, this codebase is concurrency-sensitive and single-file — fresh subagents lack the lock/marshal invariants). Steps use `- [ ]`.

**Goal:** Implement the approved public-release spec (`docs/superpowers/specs/2026-06-10-recap-public-release-design.md`) EXCEPT §E (OSS scaffolding) and §H (history clean + publish), which are deferred to last.

**Architecture:** recap is a single file (`recap.py`) + the live-pane widget (`recap_terminal.py`). Config is a TOML file read once at startup into a module-level dict; a `_cfg()` resolver gives env > config > default for the existing `RECAP_*` knobs (back-compat preserved). Keymap overrides + an opt-in leader use Textual's `Binding(id=)` / `set_keymap` + an `on_key` state machine. Everything new that is a PURE function gets a headless unit test; App-method / render / Textual-runtime changes are verified by `py_compile` + the existing suite + a manual restart (recap's nested-App methods are not unit-testable headless, per the established testing reality).

**Tech Stack:** Python ≥3.11 (stdlib `tomllib`), Textual, pyte, platformdirs (already a textual transitive).

**Testing reality:** pure module-level helpers → `tests/test_config.py` / `tests/test_sort_recency.py` headless. App methods / render / leader runtime → `python -m py_compile` + run all 4 suites + restart recap to eyeball. Never a big untested batch (the 2026-06 freeze lesson); one tested commit per task.

---

## Phase 1 — Config core (§A) — the foundation

**Files:** Modify `recap.py` (add config helpers near `_read_json`, ~line 73; add `platformdirs` import + dep). Create `tests/test_config.py`.

### Task 1.1 — config location + load (pure, tested)
- [ ] **Step 1 — failing test** `tests/test_config.py`:
```python
import os, sys, tempfile, importlib
from pathlib import Path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import recap

def test_config_path_honors_env(tmp=None):
    p = Path(tempfile.gettempdir()) / "recap-cfg-test.toml"
    os.environ["RECAP_CONFIG"] = str(p)
    try:
        assert recap._config_path() == p
    finally:
        os.environ.pop("RECAP_CONFIG", None)

def test_load_config_parses_and_degrades():
    d = Path(tempfile.mkdtemp())
    f = d / "config.toml"
    f.write_text('[summary]\nenabled = true\n[limits]\nmax_live = 9\n', encoding="utf-8")
    os.environ["RECAP_CONFIG"] = str(f)
    try:
        recap._reset_config_cache()
        c = recap._load_config()
        assert c["summary"]["enabled"] is True and c["limits"]["max_live"] == 9
    finally:
        os.environ.pop("RECAP_CONFIG", None); recap._reset_config_cache()
    # corrupt / missing → {} (no raise)
    bad = d / "bad.toml"; bad.write_text("this is not toml = = =", encoding="utf-8")
    os.environ["RECAP_CONFIG"] = str(bad)
    try:
        recap._reset_config_cache()
        assert recap._load_config() == {}
    finally:
        os.environ.pop("RECAP_CONFIG", None); recap._reset_config_cache()
```
- [ ] **Step 2 — run, expect fail** `python tests/test_config.py` → AttributeError `_config_path`.
- [ ] **Step 3 — implement** in `recap.py` (after `_read_json`):
```python
import tomllib  # stdlib (Python >= 3.11)

def _config_path():
    """Resolve the config file path: $RECAP_CONFIG, else platformdirs user config."""
    p = os.environ.get("RECAP_CONFIG")
    if p:
        return Path(p).expanduser()
    try:
        import platformdirs
        return Path(platformdirs.user_config_dir("recap")) / "config.toml"
    except Exception:
        return Path.home() / ".config" / "recap" / "config.toml"

_CONFIG_CACHE = None
def _reset_config_cache():
    global _CONFIG_CACHE
    _CONFIG_CACHE = None

def _load_config():
    """Parse the TOML config once (cached). Missing/corrupt → {} (logged), never raises."""
    global _CONFIG_CACHE
    if _CONFIG_CACHE is not None:
        return _CONFIG_CACHE
    cfg = {}
    try:
        p = _config_path()
        if p and p.is_file():
            with open(p, "rb") as f:
                cfg = tomllib.load(f)
    except Exception as e:
        _log(f"config: ignoring unreadable {p}: {e!r}")
        cfg = {}
    _CONFIG_CACHE = cfg if isinstance(cfg, dict) else {}
    return _CONFIG_CACHE
```
- [ ] **Step 4 — run, expect pass.**
- [ ] **Step 5 — commit** `feat(config): TOML config load + platformdirs location (stdlib tomllib)`.

### Task 1.2 — `_cfg()` precedence resolver (pure, tested)
- [ ] **Step 1 — failing test** (append to `tests/test_config.py`):
```python
def test_cfg_precedence_env_over_config_over_default():
    d = Path(tempfile.mkdtemp()); f = d / "config.toml"
    f.write_text('[limits]\nmax_live = 30\nclaude_mb = 700\n', encoding="utf-8")
    os.environ["RECAP_CONFIG"] = str(f); recap._reset_config_cache()
    try:
        os.environ["RECAP_MAX_LIVE"] = "12"            # env wins
        assert recap._cfg("limits","max_live","RECAP_MAX_LIVE", 64, int) == 12
        os.environ.pop("RECAP_MAX_LIVE", None)         # no env → config
        assert recap._cfg("limits","max_live","RECAP_MAX_LIVE", 64, int) == 30
        assert recap._cfg("limits","claude_mb","RECAP_CLAUDE_MB", 600.0, float) == 700.0
        assert recap._cfg("limits","missing","RECAP_NOPE", 5, int) == 5   # default
        os.environ["RECAP_MAX_LIVE"] = "bad"           # bad cast → default
        assert recap._cfg("limits","max_live","RECAP_MAX_LIVE", 64, int) == 64
    finally:
        for k in ("RECAP_CONFIG","RECAP_MAX_LIVE","RECAP_CLAUDE_MB"): os.environ.pop(k, None)
        recap._reset_config_cache()

def test_cfg_bool_parses_truthy_falsy():
    assert recap._cfg_bool(True) is True and recap._cfg_bool("true") is True
    assert recap._cfg_bool("0") is False and recap._cfg_bool(None, default=True) is True
```
- [ ] **Step 2 — run, expect fail.**
- [ ] **Step 3 — implement:**
```python
def _cfg(section, key, env_var, default, cast=str):
    """Resolve env_var > config[section][key] > default, cast-safe."""
    v = os.environ.get(env_var)
    if v is None or (isinstance(v, str) and v == ""):
        v = _load_config().get(section, {}).get(key, None)
    if v is None:
        return default
    try:
        return cast(v)
    except Exception:
        return default

def _cfg_bool(v, default=False):
    if v is None: return default
    if isinstance(v, bool): return v
    return str(v).strip().lower() in ("1","true","yes","on")
```
- [ ] **Step 4 — run, expect pass.**  **Step 5 — commit** `feat(config): _cfg precedence resolver (env > config > default)`.

### Task 1.3 — wire the existing knobs through `_cfg` (verify by reading + suite)
- [ ] Migrate each existing `os.environ.get("RECAP_*")` to `_cfg(...)`, mapping per spec §A:
  - RAM gate (`_spawn_live_pane` + statusbar): `RECAP_MAX_MEM_LOAD`→`_cfg("limits","max_memory_load",...,85.0,float)`, `RECAP_MIN_COMMIT_MB`→`limits.min_commit_headroom_mb` (2048), `RECAP_MIN_FREE_PHYS_PCT`→`limits.min_free_phys_pct` (8), `RECAP_CLAUDE_MB`→`limits.per_pane_mb` (600), `RECAP_MIN_FREE_MB`→`limits.min_free_mb` (0), `RECAP_HARD_RAM_GATE`→`limits.hard_ram_gate` (bool).
  - `RECAP_AUTO_REFRESH`→`display.auto_refresh`; `RECAP_MAX_LIVE`→`limits.max_live`; `RECAP_RELEASE_KEY`→`keys.release`.
  - split-live gate (`_split_live_disabled_by_env`): also honor `display.split_live=false`.
- [ ] **Verify:** `py_compile` + all 4 suites green (no behavior change with no config file). Manual: a config with `[limits] max_live=3` caps panes.
- [ ] **Commit** `refactor(config): read RAM/display/limits/keys knobs via _cfg (env back-compat)`.

---

## Phase 2 — Summary default-OFF (§B)

**Files:** `recap.py` (`main()` summary gate ~line 6018, `_prewarm_previews`, first-run hint).

### Task 2.1 — summary defaults OFF, opt-in via config/env
- [ ] Add `_summary_enabled()`: `True` only if `_cfg_bool(_cfg("summary","enabled","RECAP_SUMMARIZE_ENABLED", False))` (default **False**) AND not `--no-summary`. A custom `summary.command`/`RECAP_SUMMARIZE_CMD` set also implies enabled.
- [ ] Gate the prewarm + summary background work (line ~6018 `if not (args.no_summary or args.related):` → `if _summary_enabled() and not args.related:`).
- [ ] **Verify:** default run makes NO `claude -p` call (grep the log: no summarizer spawn); `[summary] enabled=true` (or `RECAP_SUMMARIZE_ENABLED=1`) restores it. List Title already uses `_list_title` (no claude -p) — unchanged.
- [ ] **Commit** `feat(summary): default OFF (opt-in); list title already claude-p-free`.

### Task 2.2 — one-time first-run hint
- [ ] On first launch with no config file AND summary off, `notify` once (persist a flag `CACHE_DIR/.hinted-summary`): "AI summaries are off (they call `claude -p`, spending credits). `recap --init-config` then set `[summary] enabled = true`, or `RECAP_SUMMARIZE_ENABLED=1`."
- [ ] **Verify:** restart shows the toast once; second launch silent. **Commit** `feat(summary): one-time credits/enable hint`.

---

## Phase 3 — CLI: --version / --init-config / --print-config (§D)

**Files:** `recap.py` top (`__version__`), `pyproject.toml` (`dynamic=["version"]`), argparse in `main()`.

### Task 3.1 — `__version__` + `--version` + hatch dynamic
- [ ] Add `__version__ = "0.1.0"` near the top of `recap.py` (after the docstring).
- [ ] `pyproject.toml`: `[project]` add `dynamic = ["version"]`, REMOVE `version = "0.1.0"`; add `[tool.hatch.version]\npath = "recap.py"` (regex source — does NOT import the module).
- [ ] argparse: `--version` action printing `__version__`.
- [ ] **Verify:** `python recap.py --version` prints `0.1.0`; `python -c "import tomllib,recap"` still imports. **Commit** `feat(cli): --version + hatch dynamic version from recap.py`.

### Task 3.2 — `--init-config` + `--print-config`
- [ ] `--init-config`: write a commented template (the §A schema, `[summary] enabled=false` with the credits comment) to `_config_path()` (`mkdir(parents=True)`; refuse overwrite without `--force`); print the path.
- [ ] `--print-config`: print each resolved setting + source (default/config/env) via a small table over the known keys.
- [ ] **Verify:** `--init-config` writes the file; `--print-config` shows values+sources; re-`--init-config` without `--force` refuses. **Commit** `feat(cli): --init-config (template) + --print-config (resolved+source)`.

---

## Phase 4 — Keymap remap + leader + responsive/MECE help + palette (§C)

**Files:** `recap.py` (`BINDINGS`, `on_mount`/`on_key`, help modal, a `DEFAULT_KEYMAP` + `_list_title`-style helpers; a docked hint Static).

### Task 4.1 — binding IDs + `set_keymap` overrides (validated)
- [ ] Give each remappable `Binding` an `id=` (e.g. `Binding("f5","refresh","Refresh",id="refresh")`). Keep `BINDINGS` static. `quit` (esc/ctrl+c) gets NO id (not remappable).
- [ ] Add `DEFAULT_KEYMAP` (action-id → default key) for reference + validation.
- [ ] In `on_mount`: read `[keys]`, validate (unknown id / duplicate key / bare `ctrl+<letter>` except `ctrl+c`,`ctrl+]`; release-key exempt), then `self.set_keymap({id: key})`. Invalid → `notify` error + skip that override (don't crash).
- [ ] **Test (pure):** `tests/test_sort_recency.py::test_keymap_validation` — a `_validate_keymap(overrides, default_ids)` pure helper returns (applied, errors); unknown id / dup / reserved rejected. Implement `_validate_keymap` module-level + test it.
- [ ] **Verify:** `py_compile` + suites; `test_no_app_binding_steals_a_readline_ctrl_key` still green (static BINDINGS unchanged). Manual: `[keys] refresh="f1"` rebinds F5→F1.
- [ ] **Commit** `feat(keys): remappable bindings via Binding(id)+set_keymap, validated`.

### Task 4.2 — opt-in leader mode (`on_key` state machine + hint bar)
- [ ] `[keys] leader` (default unset). When set: `App.on_key` (NOT a priority binding), gated on `self.focused is the DataTable` (list focus) — pane focus passes through to claude. Pending-state handled at the TOP of `on_key`, before type-to-search / space / enter.
- [ ] On leader: set `_leader_pending=True`, show a docked `Static` hint of the bound letters; next key → dispatch the mapped action via `self.run_action(...)` or cancel (Esc / unmapped / `set_timer(1.5s)`).
- [ ] Single-letter `[keys]` values are interpreted as leader-then-letter (validated unique); F-key values stay direct.
- [ ] **Test (pure):** `_leader_map(keys_cfg)` → {letter: action-id}, dup letters rejected — unit-test it.
- [ ] **Verify:** `py_compile` + suites; manual: `[keys] leader="ctrl+g"` + `refresh="r"` → ctrl+g then r refreshes from the list; inside a pane ctrl+g goes to claude.
- [ ] **Commit** `feat(keys): opt-in leader/prefix mode (list-focus on_key state machine)`.

### Task 4.3 — single-source MECE + width-responsive help, re-enable palette
- [ ] Generate BOTH the footer labels and the `?` overlay from `DEFAULT_KEYMAP`(+overrides) so they can't drift. Replace the hand-written help string with a generated one.
- [ ] `?` overlay = scrollable `ModalScreen`; below a width threshold (`self.app.size.width`) switch from multi-column to one-binding-per-line; ellipsise long descriptions; tiny terminals still show "press ? for help".
- [ ] Add a colour legend to the overlay (Title hue = project, Last colour = recency, markers) — feeds Phase 6.
- [ ] Re-enable the command palette on a free key (the leader key) with a `Provider` exposing the meta actions.
- [ ] **Verify:** `py_compile` + suites; manual at 3 widths (wide / narrow / tiny). **Commit** `feat(keys/help): single-source MECE + width-responsive help + command palette`.

---

## Phase 5 — UX polish (§I): colour legend done in 4.3; color_by, category-skip, split divider

### Task 5.1 — `[display] color_by` selector
- [ ] `_cfg("display","color_by","RECAP_COLOR_BY","project")` ∈ {project, worktree, topic, none}. In `_do_refresh_table`, pick the colour-map source by it; `none` → no Title tint.
- [ ] **Test (pure):** `_color_key_for(s, mode)` returns the value to colour by (project_name/worktree_label/primary_topic/"") — unit-test the 4 modes.
- [ ] **Verify** + **commit** `feat(display): color_by selector (project/worktree/topic/none)`.

### Task 5.2 — skip category (group-header) rows in navigation + Enter
- [ ] Identify the group-header rows (the `_build_groups` header rows added to the DataTable). On cursor move (`on_data_table_row_highlighted`) skip a header row to the next data row; Enter on a header = no-op (or toggle-collapse if cheap).
- [ ] **Verify** (manual, grouped view): arrows never land on a header; Enter on a header doesn't resume. **Commit** `fix(list): skip non-selectable category rows in navigation/Enter`.

### Task 5.3 — draggable split divider (+ persist ratio)
- [ ] Pure helper `_split_ratio_from_x(x, total, lo=0.2, hi=0.8)` → clamped fraction; `_clamp_ratio`. Unit-test.
- [ ] A thin divider widget between `#table` and `#right`; `on_mouse_down`→`capture_mouse`, `on_mouse_move`→set `#table` width `f"{pct}%"` (clamped 20–80), `on_mouse_up`→`release_mouse` + persist to `CACHE_DIR/split-ratio`. Restore on launch (overrides the static 34%). F4 toggle unchanged.
- [ ] **Verify:** `py_compile` + suites + the ratio-math test; manual drag. **Commit** `feat(ui): draggable list↔pane split divider (persisted ratio)`.

---

## Phase 6 — History PII gate (§F, non-CI parts) + high-value stretches

### Task 6.1 — history-level PII gate script (no codenames in the published script)
- [ ] Create `scripts/check-history.sh`: fail if any commit author/committer email is outside an allowlist (`*@users.noreply.github.com`, `noreply@anthropic.com`), or author/committer name has non-ASCII or `/`; plus an OUT-OF-REPO `$RECAP_HISTORY_DENY` patterns file for specific codenames. Generic patterns only (the script ships publicly).
- [ ] Document a `pre-push` hook rejecting a corporate-domain author/committer on new commits.
- [ ] **Verify:** run the script on the repo (currently FAILS on the corporate identity — expected until §H rewrites history; the script is the gate for §H). **Commit** `chore(ci): history-level PII gate script (generic patterns)`.

### Task 6.2 — atexit terminal-restore (belt-and-suspenders)
- [ ] Register an `atexit` that writes the mouse/focus/cursor restore sequence (`\x1b[?1000l\x1b[?1002l\x1b[?1003l\x1b[?1004l\x1b[?1006l\x1b[?1015l\x1b[?25h`) to the real terminal, so a crash-exit (Python exception path) never leaves the terminal in mouse-tracking mode. (Hard kills can't be helped — documented.)
- [ ] **Verify:** force an exception exit in a scratch run → terminal not stuck. **Commit** `fix(tty): atexit restores mouse/focus tracking on crash-exit`.

### Task 6.3 — runtime memory pressure watch
- [ ] In `_poll_live_status` (or a dedicated tick): when `_mem_status().load >= max_memory_load` while ≥1 pane is open, toast once per crossing "memory pressure NN% — consider closing panes (F10)"; optionally auto-decline new opens (the gate already does).
- [ ] **Verify:** manual (or simulate via a low `RECAP_MAX_MEM_LOAD`). **Commit** `feat(split-live): runtime memory-pressure toast`.

---

## Deferred (NOT in this plan, last): §E OSS scaffolding (CI/CONTRIBUTING/CHANGELOG/templates/badges/SECURITY/urls/.gitattributes) · §H history clean + publish.

## Self-review notes
- Spec coverage: §A→P1, §B→P2, §D→P3, §C→P4, §I→P5 (+legend in 4.3), §F(non-CI)→P6.1, stretches→P6.2/6.3. §E/§H deferred per user.
- Pure-helper tests: `_config_path`/`_load_config`/`_cfg`/`_cfg_bool`/`_validate_keymap`/`_leader_map`/`_color_key_for`/`_split_ratio_from_x`. UI/App-method tasks verified by py_compile+suite+restart (recap reality).
- Order: config first (P1) — P2/P4/P5 read it; P3 surfaces it. P6 independent.
