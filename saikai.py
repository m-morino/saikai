#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "textual>=0.50",
#   "pyte>=0.8",                          # PTY byte-stream -> screen grid (split-live)
#   "pywinpty>=2.0 ; sys_platform == 'win32'",   # Windows ConPTY backend
#   "ptyprocess>=0.7 ; sys_platform != 'win32'", # POSIX PTY backend
#   "platformdirs>=3.6",                  # cross-platform config dir (textual transitive)
# ]
# ///
"""
saikai — Claude Code session history viewer with LLM summarization
Usage:
  saikai [--days N] [--all-projects] [--pick] [--project PATH]
        [--no-summary] [--refresh-summary]
"""

__version__ = "0.2.2"

import argparse
import io
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
import tomllib
import uuid
from collections import Counter, defaultdict, namedtuple
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from pathlib import Path

from saikai_provider import get_provider

# Force UTF-8 output on Windows
if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

# CREATE_NO_WINDOW prevents a console window flash when launching command-line
# helpers (git, taskkill) from a GUI terminal on Windows. No-op on POSIX.
NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

# Claude remains the only user-selectable provider until another provider has
# normalized history discovery wired into the session list.
_ACTIVE_PROVIDER = get_provider("claude")

# ── ANSI helpers ────────────────────────────────────────────────────────────
RESET = "\033[0m"
BOLD  = "\033[1m"
DIM   = "\033[2m"
CYAN  = "\033[36m"
GREEN = "\033[32m"
YELLOW= "\033[33m"
GRAY  = "\033[90m"
MAGENTA= "\033[35m"
RED   = "\033[31m"
GOLD  = "\033[93m"  # bright yellow for favorite stars
HIDDEN_DIM = "\033[2;90m"  # dim + gray for hidden sessions

_ANSI_RE = re.compile(r"\033\[[0-9;]*m")

def _c(text, *codes):
    return "".join(codes) + str(text) + RESET


# ── Small shared helpers ────────────────────────────────────────────────────
def _trim_sid(s: str) -> str:
    """Strip trailing field padding from a session-id arg."""
    return s.strip().split()[0] if s and s.strip() else ""


def _first_msg(s: dict, n: int = 60) -> str:
    """First real user message truncated to N chars, or empty string."""
    msgs = s.get("real_msgs")
    return msgs[0][:n] if msgs else ""


def _list_title(s: dict) -> str:
    """Title for the session LIST. A user-typed name (Shift+F2, `custom_title`)
    wins; otherwise claude's OWN data only — NO `claude -p` summary — falling
    through native ai-title → first user message → project label → short id, so a
    freshly-opened session shows the project immediately (never blank) and fills
    in as claude writes the first message and its own ai-title.
    (project_short / _first_msg resolved at call time.)"""
    return (s.get("custom_title") or s.get("ai_title") or _first_msg(s)
            or project_short(s.get("project_name") or "")
            or (s.get("id") or "")[:8])


def _color_key_for(s: dict, mode: str) -> str:
    """The value a session's TITLE hue is keyed on, per [display] color_by
    (project | worktree | topic | none)."""
    if mode == "worktree":
        return s.get("worktree_label") or ""
    if mode == "topic":
        return s.get("primary_topic") or "(none)"
    if mode == "none":
        return ""
    return project_short(s.get("project_name") or "")   # default: project


def _color_legend(color_by: str) -> str:
    """Plain-language explanation shared by help and Settings."""
    labels = {
        "project": "Same title color = same project.",
        "worktree": "Same title color = same worktree.",
        "topic": "Same title color = same topic.",
        "none": "Title colors are disabled.",
    }
    return labels.get(color_by, labels["project"]) + " Symbols show state."


def _first_selectable_row(table, start: int, step: int):
    """Index of the nearest non-header (selectable) row from `start`
    (exclusive), walking by `step` (+1 down / -1 up). Header rows carry a
    `__hdr__*` row key; sessions carry their sid. Returns None when there is no
    selectable row that way, so the caller can fall back to the other side.

    Pure over the DataTable's `row_count` + `coordinate_to_cell_key` contract —
    unit-tested with a fake table in tests/test_sort_recency.py."""
    r = start + step
    n = table.row_count
    while 0 <= r < n:
        try:
            key, _ = table.coordinate_to_cell_key((r, 0))
        except Exception:
            return None
        if key and not str(key.value).startswith("__hdr__"):
            return r
        r += step
    return None


# Draggable list/pane divider: the table's width as a fraction of #main. Banded
# so neither pane can be dragged shut (keeps the grip reachable + claude usable).
_SPLIT_RATIO_LO, _SPLIT_RATIO_HI = 0.15, 0.85


def _split_ratio_from_x(screen_x, main_left, main_width,
                        lo=_SPLIT_RATIO_LO, hi=_SPLIT_RATIO_HI):
    """Table-width fraction for a divider drag at absolute column `screen_x`,
    given #main's left edge + width. Clamped to [lo, hi]. Pure — unit-tested."""
    if main_width <= 0:
        return lo
    return max(lo, min(hi, (screen_x - main_left) / main_width))


def _nudge_split_ratio(cur: float, delta: float,
                       lo=_SPLIT_RATIO_LO, hi=_SPLIT_RATIO_HI) -> float:
    """Keyboard nudge (Alt+←/→) for the list/pane divider — same clamp as a
    mouse drag. Pure — unit-tested."""
    return max(lo, min(hi, cur + delta))


def _read_json(path: Path, default):
    """Read JSON file, returning `default` on any error (missing/corrupt/etc.)."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


# ── TOML config (opt-in; env vars still win) ─────────────────────────────────
def _config_path() -> Path:
    """Resolve the config file path: $SAIKAI_CONFIG, else the platform config dir
    (platformdirs — Windows %APPDATA%, Linux ~/.config, macOS Application Support).
    platformdirs is a textual transitive dep; fall back to ~/.config if absent."""
    p = os.environ.get("SAIKAI_CONFIG")
    if p:
        return Path(p).expanduser()
    try:
        import platformdirs
        return Path(platformdirs.user_config_dir("saikai")) / "config.toml"
    except Exception:
        return Path.home() / ".config" / "saikai" / "config.toml"


_CONFIG_CACHE = None


def _reset_config_cache() -> None:
    """Drop the cached config (tests; or after --init-config writes a new file)."""
    global _CONFIG_CACHE
    _CONFIG_CACHE = None


def _load_config() -> dict:
    """Parse the TOML config once (cached). Missing/empty/corrupt → {} (logged),
    never raises — saikai must launch even with a broken config file."""
    global _CONFIG_CACHE
    if _CONFIG_CACHE is not None:
        return _CONFIG_CACHE
    cfg = {}
    p = _config_path()
    try:
        if p and p.is_file():
            with open(p, "rb") as f:
                cfg = tomllib.load(f)
    except Exception as e:
        _log(f"config: ignoring unreadable {p}: {e!r}")
        cfg = {}
    _CONFIG_CACHE = cfg if isinstance(cfg, dict) else {}
    return _CONFIG_CACHE


def _cfg(section: str, key: str, env_var: str, default, cast=str):
    """Resolve a setting by precedence: env var > config[section][key] > default,
    cast-safe (a bad value → default). Empty env string is treated as unset."""
    v = os.environ.get(env_var)
    if v is None or (isinstance(v, str) and v == ""):
        v = _load_config().get(section, {}).get(key, None)
    if v is None:
        return default
    try:
        return cast(v)
    except Exception:
        return default


def _cfg_bool(v, default: bool = False) -> bool:
    """Coerce a config/env value to bool (truthy = 1/true/yes/on, case-insensitive)."""
    if v is None:
        return default
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("1", "true", "yes", "on")


_CONFIG_TEMPLATE = (
    "# saikai configuration (TOML). Env vars (SAIKAI_*) override these; CLI flags win.\n\n"
    "[summary]\n"
    "enabled = false   # AI summaries call `claude -p` and spend credits — opt-in.\n"
    'command = ""      # custom backend: prompt on stdin -> summary on stdout ("" = claude -p)\n'
    'model   = "haiku"\n\n'
    "[display]\n"
    "auto_refresh = 0          # seconds between background re-scans (0 = off; minimum active value = 2)\n"
    "split_live   = true       # false = list-only browser (Enter = full-takeover resume)\n"
    'color_by     = "project"  # title hue: project | worktree | topic | none\n'
    "split_ratio  = 0.34       # initial list share; dragging / Alt+arrows persists over it\n\n"
    "[launch]\n"
    "auto_permission = false   # true = add --permission-mode auto in frequent workspaces\n\n"
    "[limits]                       # live-pane memory gate (per-OS signals)\n"
    "# max_memory_load      = 85    # refuse/warn above this % memory load (default 85 Win / 95 POSIX)\n"
    "max_memory_pressure    = 10    # Linux PSI some-avg10 % / macOS critical -> refuse (no effect on Win)\n"
    "min_commit_headroom_mb = 2048  # keep this much commit headroom free (Win; Linux only if strict overcommit)\n"
    "min_free_phys_pct      = 8     # keep >= this % of physical RAM free/available\n"
    "per_pane_mb            = 600   # estimated RAM per live pane\n"
    "hard_ram_gate          = false # true = refuse (vs warn) when crossed\n"
    "max_live               = 64    # hard cap on concurrent live panes\n"
    "scrollback_lines       = 2000  # per-pane scrollback kept in memory (biggest RAM lever)\n\n"
    "[keys]\n"
    "# Keyboard-first: Space (in the list) is a LEADER key by default — press it,\n"
    "# then a mnemonic letter: f=favorite h=hide e=rename r=refresh d=diff y=copy\n"
    "# s=sort o=order g=group t=tree n=new p=restore z=freeze\n"
    "# a=attention l=list x=close [=prev-tab ]=next-tab Space=mark. Press ? in the\n"
    "# app for the live map. Everything below is optional fine-tuning:\n"
    '# leader          = "ctrl+g"  # use another leader ("none" disables the mode)\n'
    "# leader_defaults = false     # start from an EMPTY letter map\n"
    '# release         = "ctrl+]"  # return focus from a live pane to the session list\n'
    '# favorite        = "v"       # single letter = leader sequence remap\n'
    '# refresh         = "f5"      # multi-char    = direct key rebind\n'
)


_RESERVED_KEY_RE = re.compile(r"^ctrl\+[a-z]$")   # readline/claude editing keys


def _validate_keymap(overrides, valid_ids):
    """Validate [keys] action→key overrides. Returns (applied, errors). Drops +
    reports: an unknown action id, an empty key, a bare ctrl+<letter> (reserved
    for readline / claude), and a key already bound to another action. 'leader' is
    skipped here (handled by the leader state machine, not a Textual binding)."""
    applied, errors, seen = {}, [], {}
    valid = set(valid_ids)
    for action_id, key in (overrides or {}).items():
        if action_id == "leader":
            continue
        if action_id not in valid:
            errors.append(f"[keys] unknown action '{action_id}'")
            continue
        k = str(key or "").strip().lower()
        if not k:
            errors.append(f"[keys] '{action_id}': empty key")
            continue
        if _RESERVED_KEY_RE.match(k):
            errors.append(f"[keys] '{action_id}': '{k}' is a reserved readline key")
            continue
        if k in seen:
            errors.append(f"[keys] '{action_id}': '{k}' already bound to '{seen[k]}'")
            continue
        seen[k] = action_id
        applied[action_id] = k
    return applied, errors


def _leader_map(letters_cfg, id_to_action):
    """Turn the single-letter [keys] entries into a leader map {letter: action_name}.
    Multi-char values (F-keys/combos) are handled as direct rebinds elsewhere, not
    here. Unknown action ids and duplicate letters are dropped + reported. Returns
    (mapping, errors)."""
    out, errs, seen = {}, [], set()
    for action_id, key in (letters_cfg or {}).items():
        k = str(key or "").lower()
        if k != " ":          # a literal space IS a valid letter (leader-leader = mark)
            k = k.strip()
        if len(k) != 1:
            continue
        action = id_to_action.get(action_id)
        if not action:
            errs.append(f"[keys] leader: unknown action '{action_id}'")
            continue
        if k in seen:
            errs.append(f"[keys] leader: letter '{k}' already used")
            continue
        seen.add(k)
        out[k] = action
    return out, errs


# Keyboard-first defaults: the table fast path handles the leader while the
# session table is focused; the App binding also allows it in other non-input,
# non-dropdown saikai controls. A claude pane, Input, or Select keeps Space.
# Everything here is overridable from [keys]; leader = "none" turns the mode off,
# leader_defaults = false starts from an empty letter map.
DEFAULT_LEADER_KEY = "space"
DEFAULT_LEADER_LETTERS = {           # action id -> letter (config orientation)
    "favorite": "f", "hide": "h", "rename": "e", "refresh": "r",
    "diff": "d", "copy": "y", "sort": "s", "order": "o",
    "group": "g", "tree": "t", "new": "n",
    "restore": "p", "freeze": "z", "attention": "a", "toggle_list": "l",
    "close": "x", "prev_tab": "[", "next_tab": "]", "mark": " ",
    "settings": ",", "search_bar": "/",
}
# Leader-only action ids (no Binding / F-key behind them): id -> action name.
LEADER_VIRTUAL_ACTIONS = {"sort": "sort", "order": "order", "mark": "toggle_mark",
                          "settings": "settings",
                          "search_bar": "toggle_search_bar"}

# Leader families: action name -> family, in display order. The which-key hint
# and the ? help render the map grouped this way (Session / View / Panes)
# instead of an alphabetical soup — the LETTERS stay flat (two keystrokes),
# only the presentation is systematic. Unknown actions (user remaps of new ids)
# fall into the last family rather than vanishing from the hint.
LEADER_FAMILY_ORDER = ("Session", "View", "Panes")
LEADER_FAMILY_OF = {
    "toggle_fav": "Session", "toggle_hide": "Session", "rename": "Session",
    "copy_prompt": "Session", "preview_changes": "Session", "refresh": "Session",
    "sort": "View", "order": "View", "cycle_group": "View",
    "toggle_tree": "View", "toggle_list": "View",
    "settings": "View", "toggle_search_bar": "View",
    "new_session": "Panes", "restore_panes": "Panes", "freeze_pane": "Panes",
    "next_attention": "Panes", "close_live": "Panes", "prev_tab": "Panes",
    "next_tab": "Panes", "toggle_mark": "Panes",
}


def _leader_groups(actions: dict) -> list:
    """Group a resolved {letter: action_name} leader map by family for the
    which-key hint / help: returns [(family, [(letter, label), …]), …] in
    LEADER_FAMILY_ORDER, families with no letters omitted, letters within a
    family sorted. Pure — unit-tested."""
    fams: dict = {f: [] for f in LEADER_FAMILY_ORDER}
    for letter, act in sorted((actions or {}).items()):
        fam = LEADER_FAMILY_OF.get(act, LEADER_FAMILY_ORDER[-1])
        fams[fam].append((letter, _leader_label(act)))
    return [(f, fams[f]) for f in LEADER_FAMILY_ORDER if fams[f]]


def _leader_hint_item(key: str, label: str) -> str:
    """Render one menu choice with an unambiguous key/action separator."""
    shown_key = "␣" if key == " " else key.replace("[", "\\[")
    return f"[yellow]{shown_key}[/yellow] [dim]→[/dim] {label}"


def _resolve_leader(keys_cfg, id_to_action):
    """Resolve the leader key + letter map: built-in defaults, then the user's
    [keys] single-letter entries layered over them (a user letter replaces both
    its action's default letter AND any default action sitting on that letter).
    Returns (leader_key, {letter: action_name}, errors). Pure — unit-tested."""
    kc = keys_cfg if isinstance(keys_cfg, dict) else {}
    id2act = dict(id_to_action or {})
    for vid, act in LEADER_VIRTUAL_ACTIONS.items():
        id2act.setdefault(vid, act)
    ld = str(kc.get("leader") or "").strip().lower()
    if ld in ("none", "off", "false", "0"):
        return "", {}, []
    leader = ld or DEFAULT_LEADER_KEY
    letters = (dict(DEFAULT_LEADER_LETTERS)
               if _cfg_bool(kc.get("leader_defaults"), True) else {})
    for act_id, key in kc.items():
        if act_id in ("leader", "leader_defaults", "release"):
            continue
        k = str(key or "").lower()
        if k != " ":
            k = k.strip()
        if len(k) != 1:
            continue                  # multi-char values are direct rebinds
        letters = {a: v for a, v in letters.items() if v != k and a != act_id}
        letters[act_id] = k
    mapping, errs = _leader_map(letters, id2act)
    return leader, mapping, errs


def _leader_label(action: str) -> str:
    """Short human label for a leader hint / help row, derived from the action
    name (toggle_fav → fav, preview_changes → diff, next_attention → next!)."""
    special = {"toggle_fav": "fav", "preview_changes": "diff",
               "copy_prompt": "copy", "next_attention": "next!",
               "new_session": "new", "restore_panes": "restore",
               "freeze_pane": "freeze", "close_live": "close",
               "prev_tab": "tab◀", "next_tab": "tab▶",
               "toggle_list": "list", "toggle_mark": "mark",
               "toggle_search_bar": "bar"}
    if action in special:
        return special[action]
    for pre in ("toggle_", "cycle_"):
        if action.startswith(pre):
            return action[len(pre):]
    return action


def _init_config(force: bool = False) -> int:
    """Write the commented config template to _config_path(); exit code for the CLI."""
    p = _config_path()
    if p.exists() and not force:
        print(_c(f"  config already exists: {p}  (use --force to overwrite)", YELLOW))
        return 1
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(_CONFIG_TEMPLATE, encoding="utf-8")
        _reset_config_cache()
        print(_c(f"  wrote config template: {p}", GREEN))
        return 0
    except Exception as e:
        print(_c(f"  could not write {p}: {e!r}", RED))
        return 1


# Every config-file/env knob with its env var + default — the single source the
# CLI --print-config AND the in-app Settings screen render from (they must not
# disagree about what exists or what won).
_CONFIG_SPECS = [
    ("summary", "enabled", "SAIKAI_SUMMARIZE_ENABLED", False),
    ("summary", "command", "SAIKAI_SUMMARIZE_CMD", ""),
    ("summary", "model", "SAIKAI_SUMMARIZE_MODEL", "haiku"),
    ("display", "auto_refresh", "SAIKAI_AUTO_REFRESH", 0),
    ("display", "split_live", "SAIKAI_SPLIT_LIVE", True),
    ("display", "color_by", "SAIKAI_COLOR_BY", "project"),
    ("display", "split_ratio", "SAIKAI_SPLIT_RATIO", 0.34),
    ("launch", "auto_permission", "SAIKAI_AUTO_PERMISSION", False),
    ("limits", "max_memory_load", "SAIKAI_MAX_MEM_LOAD",
     85 if sys.platform == "win32" else 95),
    ("limits", "max_memory_pressure", "SAIKAI_MAX_MEM_PRESSURE", 10),
    ("limits", "min_commit_headroom_mb", "SAIKAI_MIN_COMMIT_MB", 2048),
    ("limits", "min_free_phys_pct", "SAIKAI_MIN_FREE_PHYS_PCT", 8),
    ("limits", "per_pane_mb", "SAIKAI_CLAUDE_MB", 600),
    ("limits", "min_free_mb", "SAIKAI_MIN_FREE_MB", 0),
    ("limits", "hard_ram_gate", "SAIKAI_HARD_RAM_GATE", False),
    ("limits", "max_live", "SAIKAI_MAX_LIVE", 64),
    ("limits", "scrollback_lines", "SAIKAI_SCROLLBACK", 2000),
    ("keys", "release", "SAIKAI_RELEASE_KEY", "ctrl+]"),
]


def _resolved_settings() -> list:
    """[(section, key, value, source), …] resolved env > config > default,
    one row per _CONFIG_SPECS entry. Pure given the env + config file."""
    cfg = _load_config()
    out = []
    for sec, key, env, default in _CONFIG_SPECS:
        ev = os.environ.get(env)
        if ev not in (None, ""):
            src, val = "env", ev
        elif cfg.get(sec, {}).get(key) is not None:
            src, val = "config", cfg[sec][key]
        else:
            src, val = "default", default
        out.append((sec, key, val, src))
    return out


def _print_config() -> int:
    """Print each resolved setting + its source (default / config / env)."""
    print(f"  config: {_config_path()}  "
          f"({'exists' if _config_path().is_file() else 'absent'})")
    for sec, key, val, src in _resolved_settings():
        print(f"  [{sec}] {key:<22} = {val!r:<14} ({src})")
    return 0


def _split_live_disabled_by_env(env_value) -> bool:
    """SAIKAI_SPLIT_LIVE is a tri-state opt-OUT switch (split-live is the default):
    unset / empty / truthy → split-live stays ON; an explicit falsy token
    (0/false/no/off, case-insensitive, trimmed) → OFF = legacy full-takeover
    resume. Split-live still also requires its PTY deps; this only governs the
    user opt-out, not the dependency fallback."""
    return (env_value or "").strip().lower() in ("0", "false", "no", "off")


def _summary_model() -> str:
    """Configured model for ordinary one-session summaries."""
    return _cfg("summary", "model", "SAIKAI_SUMMARIZE_MODEL", SUMMARY_MODEL, str)


def _release_focus_key() -> str:
    """Configured human-form key that releases a focused live pane."""
    return _cfg("keys", "release", "SAIKAI_RELEASE_KEY", "ctrl+]", str)


def _at_live_capacity(live_count: int, pending: int, max_live: int) -> bool:
    """True if opening one more live pane would hit the cap, counting BOTH
    registered panes (live_count) and in-flight opens (pending — scheduled but not
    yet registered). Registration is deferred to an async mount worker, so without
    counting the in-flight ones a batch / Shift+F4-restore loop reads a stale count
    and blows straight past the cap. Pure + module-level so it is unit-tested."""
    return (int(live_count) + int(pending)) >= int(max_live)


def _write_json(path: Path, obj) -> None:
    """Atomically write JSON to path. Uses tempfile + os.replace so concurrent
    readers/writers (worker pool, concurrent reads) cannot observe a torn write."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(obj, indent=2, ensure_ascii=False)
    tmp = path.with_suffix(path.suffix + f".tmp.{os.getpid()}")
    try:
        tmp.write_text(payload, encoding="utf-8")
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except Exception:
                pass


# ── Cache ───────────────────────────────────────────────────────────────────
CACHE_DIR = Path.home() / ".cache" / "saikai"
SUMMARY_MODEL = "haiku"
HIDDEN_FILE = CACHE_DIR / "hidden.json"
FAVORITE_FILE = CACHE_DIR / "favorite.json"
CUSTOM_TITLES_FILE = CACHE_DIR / "custom-titles.json"   # sid -> user-typed name (Shift+F2)
OPEN_PANES_FILE = CACHE_DIR / "open-panes.json"   # split-live: sids open last session (restore)
VIEW_MODE_FILE = CACHE_DIR / "view-mode.txt"
TREE_MODE_FILE = CACHE_DIR / "tree-mode.txt"
GROUP_BY_FILE = CACHE_DIR / "group-by.txt"
STATUS_FILTER_FILE = CACHE_DIR / "status-filter.txt"
LASTACT_FILTER_FILE = CACHE_DIR / "lastact-filter.txt"
SORT_FILE = CACHE_DIR / "sort.json"
OPTIONS_FILE = CACHE_DIR / "options.json"
RESUME_HISTORY_FILE = CACHE_DIR / "resume-history.tsv"
LOG_FILE = CACHE_DIR / "saikai.log"

# Sort columns selectable via Ctrl-1/2/3. "-" = inactive (no sort at this priority).
SORT_COLS = ("-", "date", "last", "proj", "wt", "title", "turns", "fav", "topic")
# Default: Recency ("last" = _last_active_dt) descending — "what was I just
# doing" is the question saikai answers; creation time is a column click away.
SORT_DEFAULT = [
    {"col": "last", "dir": "desc"},
    {"col": "-",    "dir": "desc"},
    {"col": "-",    "dir": "desc"},
]
PARSED_DIR = CACHE_DIR / "parsed"
PREVIEW_DIR = CACHE_DIR / "preview"
PREVIEW_FULL_DIR = CACHE_DIR / "preview-full"


def _log(msg: str) -> None:
    """Append a timestamped line to CACHE_DIR/saikai.log. TUI-safe (a FILE, never
    stdout — that would corrupt the Textual display), best-effort, and size-bounded
    (rotates at ~1 MB, one backup) so it can neither fail saikai nor grow without
    limit. Always on, so the trail is already there when something like 'all
    sessions vanished' happens unexpectedly."""
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        try:
            if LOG_FILE.stat().st_size > 1_000_000:
                os.replace(LOG_FILE, LOG_FILE.with_name(LOG_FILE.name + ".1"))
        except OSError:
            pass
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S}  {msg}\n")
    except Exception:
        pass


def _load_options() -> dict:
    return _read_json(OPTIONS_FILE, {})


def _save_options(opts: dict) -> None:
    """Merge `opts` into the persisted options so future fields aren't dropped
    by an older saikai version that doesn't know about them."""
    merged = _load_options()
    merged.update(opts)
    _write_json(OPTIONS_FILE, merged)


def _reset_saved_cli_options() -> None:
    """Forget only saved CLI scope filters, preserving unrelated UI preferences."""
    opts = _load_options()
    if not isinstance(opts, dict):
        opts = {}
    opts.pop("days", None)
    opts.pop("scope", None)
    if opts:
        _write_json(OPTIONS_FILE, opts)
    else:
        try:
            OPTIONS_FILE.unlink()
        except FileNotFoundError:
            pass


# Custom session titles (Shift+F2): a saikai-side overlay keyed by sid. Cached
# with mtime invalidation so the per-session lookup in _enrich_session — called
# for EVERY session on every load / rescan — costs one stat, not a JSON re-read.
_CUSTOM_TITLES_CACHE: "dict | None" = None
_CUSTOM_TITLES_MTIME: "float | None" = None


def _load_custom_titles() -> dict:
    """sid -> user-typed title. saikai-side only — never touches claude's
    transcript. Re-read only when the file mtime changes (or after a write)."""
    global _CUSTOM_TITLES_CACHE, _CUSTOM_TITLES_MTIME
    try:
        m = CUSTOM_TITLES_FILE.stat().st_mtime
    except OSError:
        _CUSTOM_TITLES_CACHE, _CUSTOM_TITLES_MTIME = {}, None
        return {}
    if _CUSTOM_TITLES_CACHE is not None and m == _CUSTOM_TITLES_MTIME:
        return _CUSTOM_TITLES_CACHE
    raw = _read_json(CUSTOM_TITLES_FILE, {})
    _CUSTOM_TITLES_CACHE = raw if isinstance(raw, dict) else {}
    _CUSTOM_TITLES_MTIME = m
    return _CUSTOM_TITLES_CACHE


def _set_custom_title(sid: str, name: str) -> None:
    """Set (or clear, when `name` is blank) the custom title for `sid`. Strict
    read so a transiently-unreadable file isn't clobbered to a 1-entry map
    (mirrors _toggle_in_set's guard for favorites / hidden)."""
    global _CUSTOM_TITLES_CACHE, _CUSTOM_TITLES_MTIME
    name = (name or "").strip()
    if CUSTOM_TITLES_FILE.exists():
        try:
            raw = json.loads(CUSTOM_TITLES_FILE.read_text(encoding="utf-8"))
        except Exception as e:
            raise RuntimeError(
                f"{CUSTOM_TITLES_FILE.name} exists but is unreadable ({e!r}); "
                f"not writing (won't risk erasing your names)") from e
        d = raw if isinstance(raw, dict) else {}
    else:
        d = {}
    if name:
        d[sid] = name
    else:
        d.pop(sid, None)
    _write_json(CUSTOM_TITLES_FILE, d)
    _CUSTOM_TITLES_CACHE, _CUSTOM_TITLES_MTIME = None, None   # force reload next read


def _load_set(path: Path) -> set[str]:
    return set(_read_json(path, []))


def _save_set(path: Path, ids: set[str]) -> None:
    _write_json(path, sorted(ids))


def _toggle_in_set(path: Path, sid: str) -> bool:
    """Toggle membership of `sid` in the set stored at `path`. Returns new state.

    Refuses (raises) when the file EXISTS but can't be parsed: _read_json swallows
    every read error to [], so toggling a transiently-unreadable (locked / mid-
    write / corrupt) populated file would otherwise save a 1-element set and ERASE
    every other entry. Failing the toggle is far better than wiping the user's
    favorites / hidden. (Display callers keep the lenient _load_set — a degraded
    read there only drops a star for one paint and never persists.)"""
    if path.exists():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            raise RuntimeError(f"{path.name} exists but could not be read "
                               f"({e!r}); not toggling (won't risk erasing it)") from e
        s = set(raw) if isinstance(raw, list) else set()
    else:
        s = set()
    now_present = sid not in s
    (s.add if now_present else s.discard)(sid)
    _save_set(path, s)
    return now_present


def _load_hidden() -> set[str]:
    return _load_set(HIDDEN_FILE)


def _load_favorites() -> set[str]:
    return _load_set(FAVORITE_FILE)


def _get_view_mode() -> str:
    try:
        return VIEW_MODE_FILE.read_text(encoding="utf-8").strip() or "default"
    except Exception:
        return "default"


def _toggle_view_mode() -> str:
    new_mode = "show-hidden" if _get_view_mode() == "default" else "default"
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    VIEW_MODE_FILE.write_text(new_mode, encoding="utf-8")
    return new_mode


def _get_tree_mode() -> bool:
    """Saved nested-tree display preference. False (flat) by default."""
    try:
        return TREE_MODE_FILE.read_text(encoding="utf-8").strip() == "on"
    except Exception:
        return False


def _toggle_tree_mode() -> bool:
    new = not _get_tree_mode()
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    TREE_MODE_FILE.write_text("on" if new else "off", encoding="utf-8")
    return new


def _get_group_by() -> str:
    """Saved grouping axis: 'none' | 'date' | 'project' | 'state'. Default is
    State: with split-live the question is "who needs me / what's running",
    and the State sections (Needs input / Running / Open / Recent / Idle /
    Archived) answer it at a glance — Date is one ␣g away. An explicit choice —
    including 'none' — is persisted by _set_group_by and wins from then on."""
    try:
        v = GROUP_BY_FILE.read_text(encoding="utf-8").strip()
        return v if v in ("none", "date", "project", "state") else "state"
    except Exception:
        return "state"


def _set_group_by(value: str) -> None:
    if value not in ("none", "date", "project", "state"):
        value = "none"
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    GROUP_BY_FILE.write_text(value, encoding="utf-8")


def _get_split_ratio() -> float:
    """Persisted list/pane divider position as a table-width fraction.
    Precedence: options.json (last drag) > [display] split_ratio /
    SAIKAI_SPLIT_RATIO > 0.34 (split-live's default table share). Always clamped
    to the [_SPLIT_RATIO_LO, _SPLIT_RATIO_HI] band."""
    v = _load_options().get("split_ratio")
    if v is None:
        v = _cfg("display", "split_ratio", "SAIKAI_SPLIT_RATIO", 0.34, float)
    try:
        v = float(v)
    except (TypeError, ValueError):
        v = 0.34
    return max(_SPLIT_RATIO_LO, min(_SPLIT_RATIO_HI, v))


def _set_split_ratio(v: float) -> None:
    """Persist the dragged divider position (clamped) to options.json."""
    v = max(_SPLIT_RATIO_LO, min(_SPLIT_RATIO_HI, float(v)))
    _save_options({"split_ratio": round(v, 4)})


def _get_status_filter() -> str:
    """Claude-Desktop 'Status' filter: 'active' (non-archived, default) |
    'archived' (only hidden/archived) | 'all'."""
    try:
        v = STATUS_FILTER_FILE.read_text(encoding="utf-8").strip()
        return v if v in ("active", "archived", "all") else "active"
    except Exception:
        return "active"


def _set_status_filter(value: str) -> None:
    if value not in ("active", "archived", "all"):
        value = "active"
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    STATUS_FILTER_FILE.write_text(value, encoding="utf-8")


def _get_lastact_days() -> int:
    """Claude-Desktop 'Last activity' window in days (0 = All time, default).
    Clamped to the dropdown option set — a stray/negative persisted value (e.g.
    -3 makes the cutoff a FUTURE time that hides EVERY row) must not silently
    empty the list, and the box must not show a value it can't represent."""
    try:
        v = int(LASTACT_FILTER_FILE.read_text(encoding="utf-8").strip())
    except Exception:
        return 0
    return v if v in (0, 1, 3, 7, 30) else 0


def _set_lastact_days(days: int) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    LASTACT_FILTER_FILE.write_text(str(int(days)), encoding="utf-8")


def _iso_dt(ts_iso: str):
    """Parse an ISO timestamp to a naive LOCAL datetime. Transcripts are UTC
    ('…Z'); converting to local makes Age-window and date-bucket comparisons —
    both against datetime.now() (local) — correct in non-UTC timezones."""
    if not ts_iso:
        return None
    try:
        dt = datetime.fromisoformat(ts_iso.replace("Z", "+00:00"))
    except Exception:
        try:
            # Fallback for an odd/truncated form: the leading 19 chars are UTC
            # (transcripts are UTC). Tag UTC so the astimezone() below converts —
            # without it the value was returned naive and mistaken for LOCAL,
            # off by the full TZ offset (mis-bucketing near-midnight sessions).
            dt = datetime.fromisoformat(ts_iso[:19]).replace(tzinfo=timezone.utc)
        except Exception:
            return None
    if dt.tzinfo is not None:
        dt = dt.astimezone().replace(tzinfo=None)   # -> local wall-clock, naive
    return dt


def _iso_date(ts_iso: str):
    """Local calendar date of an ISO timestamp (see _iso_dt re: UTC→local — the
    old UTC-date version mis-bucketed near-midnight sessions by the TZ offset)."""
    dt = _iso_dt(ts_iso)
    return dt.date() if dt is not None else None


def _date_label(d, now) -> str:
    """Claude-Desktop-style date-section label from a local date (None -> '—'):
    Today / Yesterday / 'Jun 11' (this year) / 'YYYY-MM-DD' (older)."""
    if d is None:
        return "—"
    today = now.date()
    if d == today:
        return "Today"
    if (today - d).days == 1:
        return "Yesterday"
    if d.year == today.year:
        return f"{d.strftime('%b')} {d.day}"
    return d.isoformat()


def _date_bucket(ts_iso: str, now) -> str:
    return _date_label(_iso_date(ts_iso), now)


def _compute_last_active_dt(s: dict):
    cands = []
    mt = s.get("mtime") or 0.0
    if mt:
        try:
            cands.append(datetime.fromtimestamp(mt))   # local naive
        except (OverflowError, OSError, ValueError):
            pass
    lt = _iso_dt(s.get("last_ts"))                      # local naive or None
    if lt is not None:
        cands.append(lt)
    return max(cands) if cands else None


def _last_active_dt(s: dict):
    """Unified 'last activity' as a naive LOCAL datetime: the LATER of the file
    mtime and the last message timestamp.

    The Last column, Recency sort, Age filter and Date grouping ALL key off this
    so they never disagree. last_ts freezes at the last *timestamped* record, but
    Claude appends untimed metadata (ai-title / permission-mode / last-prompt)
    that still bumps the file mtime — so a freshly-touched session whose tail is
    metadata-only must sort/bucket by mtime (what the Last column already shows),
    not by its stale last-message ts. max() also guards a restored backup whose
    mtime predates its newest message.

    Memoised: _enrich_session stamps the value onto the session dict as
    'last_active_dt' (once per parse/reload), so the per-refresh hot paths read it
    instead of re-parsing last_ts + rebuilding a datetime for every session on
    every keystroke — and there is ONE definition of 'last activity', so a new
    call site can't silently drift (the bug class that started all this). Falls
    back to computing for dicts that skipped _enrich_session (e.g. unit tests)."""
    v = s.get("last_active_dt")
    return v if v is not None else _compute_last_active_dt(s)


def _is_recent_now(s: dict, now_ts: float) -> bool:
    """True if the session was touched < 30 min ago, evaluated against the CURRENT
    time — not the load-time is_recent snapshot, which goes stale as the picker
    stays open (a ':recent' search 40 min in would otherwise return the set that
    was recent AT LAUNCH). Uses the stored mtime; a reload re-stats it."""
    return (now_ts - (s.get("mtime") or 0.0)) < 1800


def _is_active_now(s: dict, now_ts: float) -> bool:
    """True if running (live-registry snapshot) or touched < 5 min ago, evaluated
    against the current time (see _is_recent_now re: staleness)."""
    return bool(s.get("is_open")) or (now_ts - (s.get("mtime") or 0.0)) < 300


def _build_groups(sessions: list[dict], group_by: str, favorites: set, now):
    """Partition already-sorted `sessions` into Claude-Desktop-like sections.
    Returns an ordered list of (header_label | None, members); members keep
    their incoming order so the active Sort spec decides within-group order.

    - group_by='none'  -> one unlabelled group (plain list, no Pinned header).
    - otherwise        -> a 'Pinned' section first (favorites), then date
      buckets (Today, Yesterday, dates desc) or project buckets (projects
      ordered by most-recent activity)."""
    if group_by == "none":
        return [(None, list(sessions))]
    groups: list = []
    rest = list(sessions)
    # Pinned shortcut section. In STATE grouping the live / actionable states
    # (Needs input / Running / Open) must NOT be hoisted out — every running or
    # waiting session must stay visible in its own state group so you don't miss
    # one that needs you; pin is shown as a ★ badge on the row (marker column)
    # instead. Only NON-live pinned sessions (Recent / Idle / Archived) get the
    # Pinned shortcut there. Date / project grouping has no actionability axis, so
    # all favorites form the Pinned section as before.
    _LIVE_STATES = ("Needs input", "Running", "Open")
    if group_by == "state":
        pinned = [s for s in rest if s["id"] in favorites
                  and (s.get("_state") or "Idle") not in _LIVE_STATES]
    else:
        pinned = [s for s in rest if s["id"] in favorites]
    if pinned:
        groups.append(("Pinned", pinned))
        _pset = {s["id"] for s in pinned}
        rest = [s for s in rest if s["id"] not in _pset]
    if group_by == "date":
        buckets: dict = {}
        bmax: dict = {}     # newest activity per bucket, tracked in the assign pass
        for s in rest:
            _la = _last_active_dt(s)
            lbl = _date_label(_la.date() if _la else None, now)
            buckets.setdefault(lbl, []).append(s)
            _k = _la or datetime.min
            if _k > bmax.get(lbl, datetime.min):
                bmax[lbl] = _k
        order = []
        if "Today" in buckets:
            order.append("Today")
        if "Yesterday" in buckets:
            order.append("Yesterday")
        dated = [l for l in buckets if l not in ("Today", "Yesterday", "—")]
        dated.sort(key=lambda l: bmax[l], reverse=True)   # no second member re-scan
        order += dated
        if "—" in buckets:
            order.append("—")
        for l in order:
            groups.append((l, buckets[l]))
    elif group_by == "state":
        # Sessions are pre-tagged with s["_state"] by the caller (needs live /
        # transcript info). Emit a fixed, attention-first section order.
        buckets = {}
        for s in rest:
            buckets.setdefault(s.get("_state") or "Idle", []).append(s)
        for l in ("Needs input", "Running", "Open", "Recent", "Idle", "Archived"):
            if buckets.get(l):
                groups.append((l, buckets[l]))
    else:  # project
        buckets = {}
        bmax = {}
        for s in rest:
            key = project_short(s.get("project_name") or "") or "(none)"
            buckets.setdefault(key, []).append(s)
            _k = _last_active_dt(s) or datetime.min
            if _k > bmax.get(key, datetime.min):
                bmax[key] = _k
        for l in sorted(buckets, key=lambda l: bmax[l], reverse=True):
            groups.append((l, buckets[l]))
    return groups


def _read_last_jsonl_record(path):
    """Cheaply read the last JSON record of a transcript (tail seek, no full
    parse). None on any error."""
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 65536))   # large enough to hold a big final record
            tail = f.read().decode("utf-8", "replace")
        for line in reversed(tail.splitlines()):
            line = line.strip()
            if line:
                return json.loads(line)
    except Exception:
        return None
    return None


def _needs_attention(s: dict, cache: dict) -> bool:
    """Heuristic 'this session needs you': the transcript's last record is a
    user turn — the assistant didn't get the last word, so the session was
    interrupted / left unanswered and is worth resuming. Cached by mtime so a
    file is tail-read at most once per change."""
    sid = s.get("id")
    mt = s.get("mtime", 0)
    hit = cache.get(sid)
    if hit is not None and hit[0] == mt:
        return hit[1]
    val = False
    path = s.get("jsonl_path")
    if path:
        rec = _read_last_jsonl_record(path)
        if rec is not None:
            role = rec.get("type") or (rec.get("message") or {}).get("role")
            val = role == "user"
            if val:
                # type:"user" records aren't always a human prompt awaiting a
                # reply: Claude Code writes tool_result turns as type:"user", and
                # writes a '[Request interrupted by user...]' marker when you
                # Esc-interrupt a turn (the user STOPPED it — not waiting on us).
                # Neither should flag the session as needing attention.
                content = (rec.get("message") or {}).get("content")
                if isinstance(content, list):
                    is_tool_result = any(
                        isinstance(b, dict) and b.get("type") == "tool_result"
                        for b in content)
                    text = "".join(
                        b.get("text", "") for b in content
                        if isinstance(b, dict) and b.get("type") == "text")
                    if is_tool_result or text.strip().startswith(
                            "[Request interrupted by user"):
                        val = False
                elif isinstance(content, str) and content.strip().startswith(
                        "[Request interrupted by user"):
                    val = False
    cache[sid] = (mt, val)
    if len(cache) > 4096:        # bound memory: drop oldest (dict is insertion-ordered)
        try:
            del cache[next(iter(cache))]
        except (StopIteration, KeyError):
            pass
    return val


def _load_sort() -> list[dict]:
    """Load the 3-level sort spec from disk, or fall back to defaults.
    Always returns exactly 3 entries; bad/missing entries get filled from the
    defaults so the rest of the code doesn't have to defensively check."""
    raw = _read_json(SORT_FILE, None)
    out = [dict(d) for d in SORT_DEFAULT]   # mutable copies
    if isinstance(raw, list):
        for i, entry in enumerate(raw[:3]):
            if isinstance(entry, dict):
                col = entry.get("col")
                if col in SORT_COLS:
                    out[i]["col"] = col
                if entry.get("dir") in ("asc", "desc"):
                    out[i]["dir"] = entry["dir"]
    return out


def _sort_select_value():
    """Primary sort column as a #sortsel option ('last'|'date'|'title'), or None
    if the saved sort LEADS WITH a column the dropdown can't show (e.g. a
    header-click sort by turns/fav). Only the primary (priority-0) key counts:
    scanning lower priorities would make the box show a SECONDARY column, and the
    echo-guard in on_select_changed would then swallow a genuine re-pick of it
    (the user clicks the shown option, v == this value → early-return, nothing
    happens). None → compose omits value= and the box shows the prompt."""
    keys = _load_sort()
    primary = keys[0].get("col") if keys else None
    return primary if primary in ("last", "date", "title") else None


def _save_sort(keys: list[dict]) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    _write_json(SORT_FILE, keys[:3])


def _cycle_sort_col(priority: int) -> dict:
    """Advance priority (1/2/3) to the next column in SORT_COLS; returns new entry."""
    idx = max(1, min(3, priority)) - 1
    keys = _load_sort()
    current = keys[idx]["col"]
    try:
        next_idx = (SORT_COLS.index(current) + 1) % len(SORT_COLS)
    except ValueError:
        next_idx = 1   # fall back to first real column
    keys[idx]["col"] = SORT_COLS[next_idx]
    _save_sort(keys)
    return keys[idx]


def _toggle_sort_dir(priority: int) -> dict:
    idx = max(1, min(3, priority)) - 1
    keys = _load_sort()
    keys[idx]["dir"] = "asc" if keys[idx]["dir"] == "desc" else "desc"
    _save_sort(keys)
    return keys[idx]


def _reset_sort() -> None:
    try:
        SORT_FILE.unlink()
    except FileNotFoundError:
        pass


def _promote_sort_col(col: str) -> None:
    """3-state click cycle for a column at priority 1:
        click 1 (new column) → default direction (desc for time/count, asc for text)
        click 2 (same col)   → opposite direction
        click 3 (same col)   → remove from sort spec (priorities 2/3 shift up)
        click 4 (new again)  → back to click-1 state

    Clicking a column that is NOT currently priority 1 promotes it: the
    previous priority 1 becomes priority 2, the previous 2 becomes 3, the
    previous 3 drops off. Duplicate columns are removed so the same key
    can't occupy two priorities."""
    if col not in SORT_COLS or col == "-":
        return
    keys = _load_sort()
    default_dir = "desc" if col in ("date", "last", "turns", "fav") else "asc"

    if keys[0]["col"] == col:
        current_dir = keys[0]["dir"]
        if current_dir == default_dir:
            # Click 2: flip to the opposite of the default.
            keys[0]["dir"] = "asc" if default_dir == "desc" else "desc"
        else:
            # Click 3: remove this column; promote the lower priorities up.
            keys = keys[1:] + [{"col": "-", "dir": "desc"}]
    else:
        # Click 1 on a fresh column.
        filtered = [k for k in keys if k["col"] != col]
        keys = ([{"col": col, "dir": default_dir}] + filtered)[:3]
        while len(keys) < 3:
            keys.append({"col": "-", "dir": "desc"})
    _save_sort(keys)


def _apply_sort(sessions: list[dict], keys: list[dict]) -> None:
    """Stable multi-level sort, lowest priority applied first so the highest
    priority wins on tie-breaks. '-' entries contribute nothing."""
    active = [k for k in keys if k["col"] != "-"]
    if not active:
        return
    # Cache disk-backed lookups once per sort invocation.
    favs = _load_favorites() if any(k["col"] == "fav" for k in active) else set()

    def keyfn(s: dict, col: str):
        # All branches return a non-None comparable value, so sort() never
        # raises TypeError on mixed None/str even when a session is missing
        # a timestamp or other field.
        if col == "date":  return s.get("first_ts") or ""
        if col == "last":  return _last_active_dt(s) or datetime.min
        if col == "proj":  return (s.get("project_name") or "").lower()
        if col == "title": return (s.get("ai_title") or _first_msg(s) or "").lower()
        if col == "turns": return s.get("n_turns") or 0
        if col == "fav":   return 1 if s["id"] in favs else 0
        if col == "topic": return s.get("primary_topic") or "~"
        if col == "wt":    return (s.get("worktree_label") or "").lower()
        return 0

    for k in reversed(active):
        sessions.sort(key=lambda s, c=k["col"]: keyfn(s, c),
                      reverse=(k["dir"] == "desc"))

def _load_cache(sid: str, mtime: float, last_ts: str = "") -> str | None:
    d = _read_json(CACHE_DIR / f"{sid}.json", None)
    if d is None:
        return None
    # Validity keys on last_ts (the last *content* timestamp), not mtime. Untimed
    # metadata appends (ai-title / permission-mode / last-prompt) bump the file
    # mtime but NOT last_ts, so an mtime key needlessly re-summarises (Haiku cost)
    # on those; last_ts also closes the sub-second staleness window the old mtime
    # tolerance left. Legacy caches (no stored last_ts) fall back to the mtime
    # tolerance so they stay valid without a one-time re-summarise of everything.
    if "last_ts" in d:
        valid = d.get("last_ts") == (last_ts or "")
    else:
        valid = abs(d.get("mtime", 0) - mtime) < 1.0
    return (d.get("summary", "") or None) if valid else None


def _save_cache(sid: str, mtime: float, summary: str, last_ts: str = ""):
    _write_json(CACHE_DIR / f"{sid}.json", {
        "session_id": sid,
        "summary": summary,
        "mtime": mtime,
        "last_ts": last_ts or "",
        "model": _summary_model(),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    })


# ── Session parsing ──────────────────────────────────────────────────────────
SKIP_MARKERS = (
    "<local-command-caveat", "<command-name>", "<command-message>",
    "<command-args>", "<system-reminder>", "<local-command-stdout>",
    "<local-command-stderr>", "<task-notification>",
    "[Request interrupted",
    "Caveat: The messages below",
    "Base directory for this skill",
    "# Expert Team", "# Expert Debate", "# Brainstorming",
    "# Test-Driven", "# Systematic Debugging", "# Keybindings Skill",
    "# Simplify:", "# Update", "# Plan", "# Write Plan", "# Execute",
    "Use when ", "You MUST use this", "<SUBAGENT-STOP>",
    "## Auto Mode Active",
    "週報作成ワークフロー",
)

def _extract_text(content) -> str:
    """Get the textual content from a user message (string or list)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [b.get("text", "") for b in content
                 if isinstance(b, dict) and b.get("type") == "text"]
        return " ".join(p for p in parts if p)
    return ""


def _is_real_user_msg(text: str) -> bool:
    # Floor at 6 chars so legitimate short prompts ("approve", "go ahead", "yes")
    # still surface — they were previously dropped, leaving rows showing "(empty)".
    if not text or len(text) < 6:
        return False
    if any(m in text for m in SKIP_MARKERS):
        return False
    return True


# Distinct prompt patterns from automation hooks (personal-names, etc.) that
# spawn `claude -p` and leave behind JSONL files in ~/.claude/projects/.
# Sessions whose first user message matches these are filtered out of saikai.
HOOK_PROMPT_MARKERS = (
    "以下は git commit で **新しく追加される行のみ**",  # personal-names hook
    "実在する個人情報 (実在人名 kanji",                    # personal-names hook variant
    "回答は JSON のみで",                                  # generic JSON-only hook prompt
    "Reply with ONLY",                                     # English JSON-only hook
    "Extract 3-5 short topic keywords",                    # saikai's own topic extractor
)


def _is_hook_session(real_msgs: list[str], n_turns: int) -> bool:
    """Detect ephemeral sessions created by automation hooks (claude -p calls).
    Pattern: ≤2 user turns AND first message starts with a known hook prompt."""
    if n_turns > 2 or not real_msgs:
        return False
    head = real_msgs[0][:200]
    return any(m in head for m in HOOK_PROMPT_MARKERS)


def _is_pid_alive(pid: int) -> bool:
    """Check if a PID is currently a running process."""
    if pid <= 0:
        return False
    if sys.platform != "win32":
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    try:
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        h = ctypes.windll.kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h:
            return False
        exit_code = ctypes.c_ulong()
        ok = ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(exit_code))
        ctypes.windll.kernel32.CloseHandle(h)
        return bool(ok) and exit_code.value == STILL_ACTIVE
    except Exception:
        return False


# ── Terminal-death watchdog (Windows SIGHUP emulation) ───────────────────────
# POSIX delivers SIGHUP to the foreground process group when the controlling
# terminal closes, so saikai and any resumed `claude --resume` child die with the
# tab. Windows has no such cascade: closing a wezterm tab kills that tab's shell
# (pwsh) but leaves the orphaned cmd→uv→python(saikai)→claude chain running
# forever — saikai blocked in subprocess.run(claude), claude idle on a dead pty.
# Confirmed 2026-06-05 via reaper.log: 12 such pairs survived ~24h, and
# reap-orphan-claude.py is structurally blind to them (it excludes python/uv
# parents after the 2026-05-23 live-session false-positive incident). This
# watchdog restores the SIGHUP semantic: find this tab's shell, poll it, and
# when it dies taskkill saikai's OWN subtree (the claude child included). It only
# ever targets os.getpid()'s tree, so it can never touch another session.
_SHELL_ANCESTOR_NAMES = frozenset({
    "pwsh.exe", "powershell.exe", "cmd.exe", "bash.exe", "sh.exe", "zsh.exe",
})
# Terminal emulators sit ABOVE the tab shell and survive a single-tab close, so
# the ancestor walk stops here — the tab shell is the last shell seen before the
# emulator (anchoring on the emulator would only fire on whole-window close).
_TERM_EMULATOR_NAMES = frozenset({
    "wezterm-gui.exe", "windowsterminal.exe", "openconsole.exe",
    "alacritty.exe", "kitty.exe",
})


def _win_pid_index() -> dict[int, tuple[str, int]]:
    """{pid: (image_name_lower, ppid)} from one CreateToolhelp32Snapshot call.
    Fast (~ms, no subprocess spawn — startup stays instant). Returns {} on any
    failure so the caller treats it as "no anchor" and the watchdog stays off."""
    import ctypes
    from ctypes import wintypes

    TH32CS_SNAPPROCESS = 0x00000002
    INVALID = ctypes.c_void_p(-1).value

    class PROCESSENTRY32(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", ctypes.c_char * 260),
        ]

    k32 = ctypes.windll.kernel32
    k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    k32.Process32First.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32)]
    k32.Process32Next.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32)]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]

    snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snap or snap == INVALID:
        return {}
    out: dict[int, tuple[str, int]] = {}
    try:
        entry = PROCESSENTRY32()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32)
        ok = k32.Process32First(snap, ctypes.byref(entry))
        while ok:
            name = entry.szExeFile.decode("ascii", "replace").lower()
            out[int(entry.th32ProcessID)] = (name, int(entry.th32ParentProcessID))
            ok = k32.Process32Next(snap, ctypes.byref(entry))
    finally:
        k32.CloseHandle(snap)
    return out


def _find_terminal_anchor(pid_index: dict[int, tuple[str, int]], start_pid: int,
                          shell_names: frozenset = _SHELL_ANCESTOR_NAMES,
                          term_names: frozenset = _TERM_EMULATOR_NAMES) -> int:
    """Return the PID of this tab's shell: the OUTERMOST shell ancestor below the
    terminal emulator. That is exactly the process that dies on tab/window close
    — an inner shim (saikai.cmd's cmd.exe, the bash wrapper) merely orphans
    alongside us, so anchoring on it would never fire. Returns 0 when no shell
    ancestor exists (headless: test runner / scheduled task) so the watchdog
    stays disabled. Cycle- and broken-chain-safe via the visited set."""
    cur = start_pid
    seen: set[int] = set()
    anchor = 0
    while cur and cur not in seen:
        seen.add(cur)
        info = pid_index.get(cur)
        if not info:
            break
        name, ppid = info
        if name in term_names:
            break  # reached the terminal emulator; tab shell already recorded
        if cur != start_pid and name in shell_names:
            anchor = cur
        cur = ppid
    return anchor


def _start_terminal_watchdog(poll_sec: float = 12.0) -> None:
    """Start the Windows terminal-death watchdog. No-op on POSIX (real SIGHUP),
    when no tab shell is found (headless), or when SAIKAI_NO_TERMINAL_WATCHDOG is
    set. See the module comment above _SHELL_ANCESTOR_NAMES for the why."""
    if sys.platform != "win32" or os.environ.get("SAIKAI_NO_TERMINAL_WATCHDOG"):
        return
    try:
        anchor = _find_terminal_anchor(_win_pid_index(), os.getpid())
    except Exception:
        anchor = 0
    if not anchor:
        return
    self_pid = os.getpid()

    def _watch() -> None:
        while True:
            time.sleep(poll_sec)
            if _is_pid_alive(anchor):
                continue
            # Tab/window closed → emulate SIGHUP: kill our OWN subtree (the
            # resumed claude child included), then exit hard so a daemon thread
            # blocked elsewhere can't keep the interpreter alive.
            try:
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(self_pid)],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               creationflags=NO_WINDOW, timeout=5)
            except Exception:
                pass
            os._exit(0)

    import threading as _thr
    _thr.Thread(target=_watch, daemon=True, name="saikai-terminal-watchdog").start()


_active_sessions_cache: dict[str, str] | None = None


def _load_active_sessions() -> dict[str, str]:
    """Read Claude Code's `~/.claude/sessions/<pid>.json` registry and return
    {sessionId: status} for every PID still alive.  Claude Code writes one
    file per running interactive session with `status` = "busy" | "idle"."""
    global _active_sessions_cache
    if _active_sessions_cache is not None:
        return _active_sessions_cache
    out: dict[str, str] = {}
    sessions_dir = Path.home() / ".claude" / "sessions"
    scanned_ok = False
    try:
        if sessions_dir.exists():
            for f in sessions_dir.glob("*.json"):
                try:
                    d = json.loads(f.read_text(encoding="utf-8"))
                    pid = d.get("pid")
                    sid = d.get("sessionId")
                    status = d.get("status", "")
                    if pid and sid and _is_pid_alive(int(pid)):
                        out[sid] = status
                except Exception:
                    continue
            scanned_ok = True
    except Exception:
        scanned_ok = False   # glob/exists itself failed (transient FS hiccup)
    # Only memoise a CLEAN scan. A transient failure (dir momentarily absent on a
    # roaming/OneDrive profile, glob error) yields an empty/partial registry; if
    # cached, EVERY session freezes as is_open=False for the whole process and
    # live panes render as dead. Leave the cache unset so the next call retries.
    if scanned_ok:
        _active_sessions_cache = out
    return out


def _invalidate_active_sessions() -> None:
    """Drop the memoised live-session registry so the next _load_active_sessions
    re-reads ~/.claude/sessions. Called on reload — otherwise is_open / is_active
    stay frozen at the launch-time snapshot for the whole picker lifetime (a
    session that exited elsewhere keeps showing Open/Running)."""
    global _active_sessions_cache
    _active_sessions_cache = None


def _enrich_session(sid: str, parsed: dict, jsonl_path: Path, mtime: float) -> dict:
    """Wrap parsed session data with runtime state (active/recent/status)."""
    # Clock skew (NTP correction, restored backup) can put mtime in the future
    # → negative age_sec was incorrectly < 300 → falsely is_active. Floor at 0.
    age_sec = max(0.0, time.time() - mtime)
    active = _load_active_sessions()
    cwd = parsed.get("cwd", "")
    # origin_cwd = where Claude originally indexed the session (first cwd in JSONL).
    # Required for `claude --resume` to find the session on disk: Claude derives
    # the projects/<key>/ directory from the cwd it was invoked in. Sessions that
    # later moved into a worktree have a different LAST cwd, so resume from
    # last cwd → "No conversation found".
    origin_cwd = parsed.get("origin_cwd") or cwd
    result = {
        "id": sid,
        "provider": parsed.get("provider") or _ACTIVE_PROVIDER.id,
        "first_ts": parsed["first_ts"],
        "last_ts": parsed.get("last_ts") or parsed["first_ts"],
        "ai_title": parsed.get("ai_title", ""),
        "custom_title": _load_custom_titles().get(sid, ""),   # Shift+F2 overlay (cached)
        "real_msgs": parsed.get("real_msgs", []),
        # n_turns = human prompts, derived from the already-filtered real_msgs so
        # tool_result records (also type:"user") don't inflate it 10-50x. Deriving
        # here (not trusting parsed["n_turns"]) self-heals OLD caches too: real_msgs
        # was always _is_real_user_msg-filtered, only the counter was wrong.
        "n_turns": len(parsed.get("real_msgs") or []),
        "jsonl_path": jsonl_path,
        "mtime": mtime,
        "cwd": cwd,
        "origin_cwd": origin_cwd,
        "git_branch": parsed.get("git_branch", ""),
        "is_open": sid in active,
        "session_status": active.get(sid, ""),
        "is_active": (sid in active) or age_sec < 300,
        "is_recent": age_sec < 1800,
    }
    result["last_active_dt"] = _compute_last_active_dt(result)
    if "topics" in parsed:
        result["topics"] = parsed["topics"]
    return result


def parse_session(jsonl_path: Path) -> dict | None:
    sid = jsonl_path.stem
    mtime = jsonl_path.stat().st_mtime
    cache_file = PARSED_DIR / f"{sid}.json"

    # Disk cache: skip JSONL re-parsing if mtime is unchanged AND schema is current.
    # `origin_cwd` was added 2026-04-30 to fix `claude --resume` for sessions
    # whose cwd changed mid-flight (e.g. moved into a worktree). Caches predating
    # that field force a re-parse.
    cached = _read_json(cache_file, None)
    if (cached and abs(cached.get("mtime", 0) - mtime) < 0.5
            and "origin_cwd" in cached):
        if _is_hook_session(cached.get("real_msgs", []), len(cached.get("real_msgs") or [])):
            return None
        return _enrich_session(sid, cached, jsonl_path, mtime)

    # Preserve topics across re-parse (JSONL append shouldn't invalidate Haiku-derived topics)
    prior_topics = (cached or {}).get("topics") or []

    first_ts = last_ts = ai_title = cwd = origin_cwd = git_branch = None
    real_msgs: list[str] = []

    try:
        with open(jsonl_path, "rb") as f:
            first = True
            for line in f:
                # Tolerate a UTF-8 BOM on the first line. Claude Code itself
                # doesn't emit one, but a user editing a JSONL in Notepad can
                # introduce one and would otherwise silently lose the session
                # (json.loads fails, all subsequent lines never link to ts/cwd).
                if first:
                    if line.startswith(b"\xef\xbb\xbf"):
                        line = line[3:]
                    first = False
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                t = obj.get("type", "")
                ts = obj.get("timestamp", "")
                if ts:
                    if first_ts is None:
                        first_ts = ts
                    last_ts = ts
                if isinstance(obj.get("cwd"), str):
                    # origin_cwd = first non-null cwd (used for `claude --resume` so
                    # Claude finds the session in the project dir it was indexed in).
                    # cwd = last non-null cwd (reflects branch-switches / worktree moves
                    # for display + relation scoring).
                    if origin_cwd is None:
                        origin_cwd = obj["cwd"]
                    cwd = obj["cwd"]
                if isinstance(obj.get("gitBranch"), str):
                    git_branch = obj["gitBranch"]
                if t == "ai-title":
                    ai_title = obj.get("aiTitle", "") or ai_title
                if t == "user":
                    text = _extract_text((obj.get("message") or {}).get("content", ""))
                    if _is_real_user_msg(text):
                        real_msgs.append(text[:800].replace("\n", " "))
    except Exception:
        # A late/mid read error (locked tail, half-written multibyte line while
        # claude appends) must NOT drop a session we already parsed valid records
        # from — keep the partial parse; the first_ts guard below still drops a
        # genuinely empty read.
        pass

    if first_ts is None:
        return None

    if _is_hook_session(real_msgs, len(real_msgs)):
        return None

    parsed = {
        "mtime": mtime,
        "first_ts": first_ts,
        "last_ts": last_ts or first_ts,
        "ai_title": ai_title or "",
        "real_msgs": real_msgs,
        "n_turns": len(real_msgs),
        "cwd": cwd or "",
        "origin_cwd": origin_cwd or cwd or "",
        "git_branch": git_branch or "",
    }
    if prior_topics:
        parsed["topics"] = prior_topics
    else:
        # Defensive: a transient miss of `cached` (its read raced a concurrent
        # _save_topics_to_cache) must not drop topics already on disk — re-read
        # just before writing and preserve any we find (topics are Haiku-derived).
        try:
            _existing = _read_json(cache_file, None)
            if isinstance(_existing, dict) and _existing.get("topics"):
                parsed["topics"] = _existing["topics"]
        except Exception:
            pass
    try:
        _write_json(cache_file, parsed)
    except Exception:
        pass

    return _enrich_session(sid, parsed, jsonl_path, mtime)


def load_sessions_in_dir(project_dir: Path, since: datetime | None) -> list[dict]:
    sessions = []
    for jsonl in project_dir.glob("*.jsonl"):
        try:
            if since is not None:
                mtime = datetime.fromtimestamp(jsonl.stat().st_mtime, tz=timezone.utc)
                if mtime < since:
                    continue
        except Exception:
            continue
        s = parse_session(jsonl)
        if s:
            s["project_name"] = project_dir.name
            sessions.append(s)
    return sessions


# ── LLM summarization via claude -p ──────────────────────────────────────────
PROJECTS_ROOT = _ACTIVE_PROVIDER.history_roots()[0]


# UUID v4 shape — prevents glob metacharacters in `claude -p` JSON output
# from being interpreted by rglob and mass-deleting unrelated JSONLs.
_UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def _delete_session_files(session_id: str):
    """Remove any JSONL created by an ephemeral claude -p call."""
    if not session_id or not _UUID_RE.fullmatch(session_id):
        return
    matches = list(PROJECTS_ROOT.rglob(f"{session_id}.jsonl"))
    # A `claude -p` call creates exactly ONE transcript. More than one match means
    # this UUID also names a real user session in another project — refuse to
    # touch anything rather than risk deleting their history on a (vanishingly
    # rare) id collision.
    if len(matches) != 1:
        return
    try:
        matches[0].unlink()
    except Exception:
        pass


_haiku_missing_warned = False  # surface "claude not on PATH" at most once per run
_bg_summarize: dict = {}  # {"thread": Thread | None, "pending": int}


def _kill_process_tree(proc: subprocess.Popen) -> None:
    """Kill a process and ALL descendants.

    On Windows, `proc.kill()` calls TerminateProcess only on the immediate child,
    so `claude.exe`'s `node.exe` workers (and any Haiku helpers under them) become
    orphans that keep running — observed as saikai-originated zombies after Haiku
    summarisation timeouts. Use `taskkill /F /T` to walk the tree. POSIX kernels
    reap descendants when the session/group leader dies, so this is Windows-only."""
    if sys.platform == "win32":
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=NO_WINDOW,
                timeout=5,
            )
        except Exception:
            pass
    try:
        proc.kill()   # idempotent — taskkill above may already have terminated it
    except Exception:
        pass


def call_claude_haiku(prompt: str, timeout: int = 45, raw: bool = False,
                      model: str | None = None) -> str:
    """Call claude -p with the given (or default Haiku) model and return its
    `result` text. Suppresses all side effects: hooks, MCP, skills, session
    persistence. Uses Popen + communicate(timeout); on timeout calls
    _kill_process_tree so grandchildren (node.exe workers under claude.exe)
    don't leak as zombies.

    Default behaviour (raw=False) is summary-friendly: returns the first
    non-fence line, truncated to 100 chars. Pass `raw=True` to get the full
    stripped output — required for structured (e.g. JSON) replies that
    span multiple lines.

    `model` overrides SUMMARY_MODEL when reasoning quality matters more than
    cost.

    The prompt is delivered over STDIN, not as a command-line argument —
    Windows' CreateProcess caps argv at 32,767 chars total, which a large
    multi-session prompt bumps right up against. Stdin has no such limit.

    Set SAIKAI_SUMMARIZE_CMD to a shell command to use a different summarizer
    backend (any LLM CLI / proxy) instead of `claude -p` — e.g. to avoid your
    personal quota. The command receives the prompt on STDIN and must emit the
    summary as plain text on STDOUT (see call_external_summarizer)."""
    _ext = _cfg("summary", "command", "SAIKAI_SUMMARIZE_CMD", "", str)
    if _ext:
        return call_external_summarizer(_ext, prompt, timeout=timeout, raw=raw)
    cmd = ["claude", "-p", "--model", model or _summary_model(),
           "--setting-sources", "",
           "--strict-mcp-config",
           "--disable-slash-commands",
           "--no-session-persistence",
           "--output-format", "json"]
    extra: dict = {}
    if sys.platform == "win32":
        extra["creationflags"] = NO_WINDOW  # no inherited console handles

    session_id = ""
    try:
        with subprocess.Popen(cmd, stdin=subprocess.PIPE,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              **extra) as proc:
            try:
                raw_out, _ = proc.communicate(input=prompt.encode("utf-8"),
                                              timeout=timeout)
            except subprocess.TimeoutExpired:
                _kill_process_tree(proc)
                # Drain pipes so the with-block's __exit__ doesn't hang. Short
                # timeout in case the descendant kill didn't release the pipe.
                try:
                    proc.communicate(timeout=5)
                except Exception:
                    pass
                return ""
        raw_text = raw_out.decode("utf-8", errors="replace")
        if proc.returncode != 0:
            return ""
        try:
            payload = json.loads(raw_text)
            session_id = payload.get("session_id", "") or ""
            text = (payload.get("result") or "").strip()
        except Exception:
            text = raw_text.strip()
        if raw:
            return text
        for line in text.split("\n"):
            line = line.strip()
            if line and not line.startswith("```"):
                return line[:100]
        return ""
    except FileNotFoundError:
        # `claude` binary not on PATH — warn once per run so the user understands
        # why summaries are empty instead of seeing a silent fallback to first msg.
        global _haiku_missing_warned
        if not _haiku_missing_warned:
            _haiku_missing_warned = True
            print(_c("  warn: `claude` not found on PATH — summaries will be raw user msgs", YELLOW),
                  file=sys.stderr)
        return ""
    except Exception:
        return ""
    finally:
        _delete_session_files(session_id)


def call_external_summarizer(cmd_str: str, prompt: str, timeout: int = 45,
                             raw: bool = False) -> str:
    """Generic pluggable summarizer backend (SAIKAI_SUMMARIZE_CMD). Runs an
    arbitrary command — any LLM CLI / proxy — feeding the prompt on STDIN and
    reading the summary from STDOUT as plain text, so you can point saikai at a
    backend other than `claude -p` (e.g. to avoid your personal quota). The
    command is parsed with shlex; wrap a JSON-returning CLI yourself so it emits
    plain text (e.g. `your-cli chat | jq -r .text`)."""
    import shlex
    try:
        cmd = shlex.split(cmd_str, posix=(sys.platform != "win32"))
    except Exception:
        return ""
    if not cmd:
        return ""
    extra: dict = {}
    if sys.platform == "win32":
        extra["creationflags"] = NO_WINDOW
    try:
        with subprocess.Popen(cmd, stdin=subprocess.PIPE,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              **extra) as proc:
            try:
                raw_out, _ = proc.communicate(input=prompt.encode("utf-8"),
                                              timeout=timeout)
            except subprocess.TimeoutExpired:
                _kill_process_tree(proc)
                try:
                    proc.communicate(timeout=5)
                except Exception:
                    pass
                return ""
        if proc.returncode != 0:
            return ""
        text = raw_out.decode("utf-8", errors="replace").strip()
        if raw:
            return text
        for line in text.split("\n"):
            line = line.strip()
            if line and not line.startswith("```"):
                return line[:100]
        return ""
    except FileNotFoundError:
        global _haiku_missing_warned
        if not _haiku_missing_warned:
            _haiku_missing_warned = True
            print(_c(f"  warn: SAIKAI_SUMMARIZE_CMD ({cmd[0]}) not found — "
                     f"summaries will be raw user msgs", YELLOW), file=sys.stderr)
        return ""
    except Exception:
        return ""


def _looks_like_refusal(text: str) -> bool:
    """Detect Haiku refusal/apology responses that should not be cached as a summary."""
    if not text:
        return False
    head = text[:40]
    return any(m in head for m in (
        "申し訳", "ご質問", "要約できません", "できませんでした",
        "I'm sorry", "I cannot", "I am unable", "I apologize",
    ))


_CJK_RE = re.compile(r"[぀-ヿ一-鿿]")


def _has_cjk(text: str) -> bool:
    """True if the string contains Hiragana, Katakana, or CJK ideographs."""
    return bool(_CJK_RE.search(text or ""))


_SUMMARY_FORCED_OFF = False   # set by --no-summary (CLI beats config)


def _summary_enabled() -> bool:
    """AI summaries are OPT-IN — `claude -p` (or a custom backend) spends credits /
    quota. Enabled iff a custom summarizer command is configured (summary.command /
    SAIKAI_SUMMARIZE_CMD) OR [summary] enabled=true (SAIKAI_SUMMARIZE_ENABLED). Default
    OFF; --no-summary forces it off regardless of config."""
    if _SUMMARY_FORCED_OFF:
        return False
    if _cfg("summary", "command", "SAIKAI_SUMMARIZE_CMD", ""):
        return True
    return _cfg("summary", "enabled", "SAIKAI_SUMMARIZE_ENABLED", False, _cfg_bool)


def _set_summary_forced_off(v: bool) -> None:
    """--no-summary forces summaries off for this run (CLI beats config)."""
    global _SUMMARY_FORCED_OFF
    _SUMMARY_FORCED_OFF = bool(v)


def summarize_session(s: dict) -> str:
    """Get summary for a session: cache → AI title → LLM (only if summaries are
    enabled — otherwise claude's own data, no `claude -p`).

    Claude Code's `aiTitle` follows the language of the first user message,
    so English-mode sessions produce English titles that bypass the
    Japanese Haiku prompt below. Treat non-CJK titles as "needs Haiku"
    and fall through; CJK titles still short-circuit for cost.
    """
    if s["ai_title"] and _has_cjk(s["ai_title"]) and not _looks_like_refusal(s["ai_title"]):
        return s["ai_title"]

    # Active sessions: JSONL mtime changes every turn → cache always invalid → skip LLM
    if s.get("is_open"):
        return _first_msg(s)

    mtime = s["mtime"]
    cached = _load_cache(s["id"], mtime, s.get("last_ts", ""))
    if cached is not None and not _looks_like_refusal(cached):
        return cached

    if not s["real_msgs"]:
        # No content to summarize — cache empty so we don't retry next time
        _save_cache(s["id"], mtime, "", s.get("last_ts", ""))
        return ""

    if not _summary_enabled():
        # Summaries are opt-in (no `claude -p`): use claude's own ai-title or the
        # first user message. Do NOT cache (no LLM result to memoise).
        return s["ai_title"] or _first_msg(s)

    sample = "\n---\n".join(s["real_msgs"][:5])[:3000]
    prompt = (
        "以下はClaude Codeセッションでのユーザー発言の冒頭です。"
        "このセッションで何をしようとしていたかを、日本語の体言止め1フレーズ"
        "(40字以内)で要約してください。前置きや「要約:」等は不要、"
        "要約フレーズのみを1行で出力してください。\n\n"
        f"{sample}"
    )

    summary = call_claude_haiku(prompt)
    if summary and not _looks_like_refusal(summary):
        _save_cache(s["id"], mtime, summary, s.get("last_ts", ""))
        return summary
    return _first_msg(s)


def summarize_all_parallel(sessions: list[dict], max_workers: int = 5):
    """Summarize all sessions in parallel, showing progress."""
    pending = [s for s in sessions if not s["ai_title"]
               and not s.get("is_open")   # active JSONL mtime changes → cache always stale
               and _load_cache(s["id"], s["mtime"], s.get("last_ts", "")) is None]
    if not pending:
        for s in sessions:
            s["summary"] = summarize_session(s)  # cache hit / ai_title
        return

    print(_c(f"  Summarizing {len(pending)} sessions via Claude Haiku...", DIM),
          file=sys.stderr)

    done = 0
    ok = 0
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = {ex.submit(summarize_session, s): s for s in pending}
        for fut in as_completed(futures):
            s = futures[fut]
            try:
                s["summary"] = fut.result()
            except Exception:
                s["summary"] = _first_msg(s)
            # A real Haiku summary is cached by summarize_session; a fall-back to
            # the first message is NOT. Re-read the cache to count honest wins so
            # the UI can say "showing first messages" instead of falsely "ready".
            if _load_cache(s["id"], s["mtime"], s.get("last_ts", "")):
                ok += 1
            done += 1
            print(f"\r  [{done}/{len(pending)}] ", end="", file=sys.stderr, flush=True)
    print(file=sys.stderr)
    _bg_summarize["succeeded"] = ok
    _bg_summarize["attempted"] = len(pending)

    for s in sessions:
        if "summary" not in s:
            s["summary"] = summarize_session(s)


# ── Git correlation ──────────────────────────────────────────────────────────
_git_commits_cache: list[tuple] | None = None


def _load_all_commits(repo: Path, since_days: int = 60) -> list[tuple]:
    """Run `git log` once and cache (sha, datetime, msg) for the whole session.
    Per-session filtering becomes O(N) Python walk instead of N subprocess calls."""
    global _git_commits_cache
    if _git_commits_cache is not None:
        return _git_commits_cache
    try:
        result = subprocess.run(
            ["git", "log", "--all", "--format=%h\t%cI\t%s",
             f"--since={since_days}.days.ago"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            cwd=repo, timeout=15, creationflags=NO_WINDOW,
        )
        out = []
        for line in result.stdout.splitlines():
            parts = line.split("\t", 2)
            if len(parts) != 3:
                continue
            sha, iso, msg = parts
            try:
                dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
                out.append((sha, dt, msg))
            except Exception:
                pass
        _git_commits_cache = out
    except Exception:
        _git_commits_cache = []
    return _git_commits_cache


def git_commits_in_range(start_iso: str, end_iso: str, repo: Path) -> list[str]:
    try:
        s = datetime.fromisoformat(start_iso.replace("Z", "+00:00"))
        e = datetime.fromisoformat(end_iso.replace("Z", "+00:00"))
    except Exception:
        return []
    out = []
    for sha, dt, msg in _load_all_commits(repo):
        if s <= dt <= e:
            out.append(f"{sha} {msg}")
            if len(out) >= 3:
                break
    return out


# ── Formatting ───────────────────────────────────────────────────────────────
def fmt_ts(iso: str) -> str:
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return dt.astimezone().strftime("%m/%d %H:%M")
    except Exception:
        return iso[:16]


def short_id(sid: str) -> str:
    return sid[:8]


def _cell_width(ch: str) -> int:
    """Approximate terminal display cell count for a single char.

    Treats only "always wide" code-point ranges as 2 cells (CJK proper,
    Hangul, fullwidth forms — all > 0x2E80). East-Asian-Ambiguous chars
    like `★ ◉ ● ○ ✗` and box-drawing `│ └ ├ ─` are LEFT at 1 cell because
    WezTerm/Windows Terminal default to narrow for them, and treating them
    as 2 cells caused favorite/activity rows to mis-align relative to empty
    rows (the empty placeholder `\\u3000` would always be 2 cells, but
    `★` would be 1 cell, so columns drifted by 1)."""
    return 2 if ord(ch) > 0x2E80 else 1


def visible_len(s: str) -> int:
    return sum(_cell_width(ch) for ch in _ANSI_RE.sub("", s))


def pad(s: str, width: int) -> str:
    return s + " " * max(0, width - visible_len(s))


def truncate_visual(s: str, width: int) -> str:
    """Truncate to visual width, accounting for wide chars (CJK + ambiguous symbols) and ANSI escapes."""
    out = []
    cur = 0
    i = 0
    while i < len(s):
        if s[i] == "\033" and i + 1 < len(s) and s[i + 1] == "[":
            j = i + 2
            while j < len(s) and (s[j].isdigit() or s[j] == ";"):
                j += 1
            if j < len(s):
                j += 1
            out.append(s[i:j])
            i = j
            continue
        ch = s[i]
        w = _cell_width(ch)
        if cur + w > width:
            break
        out.append(ch)
        cur += w
        i += 1
    return "".join(out)


def project_short(name: str) -> str:
    """Strip the encoded home-dir prefix so the column shows a recognizable
    suffix, e.g. <home>-myrepo → myrepo. Derived from Path.home() so it works for
    any user / OS. Case-INSENSITIVE because Claude Code lowercases the Windows
    drive letter in the encoded project-dir name (`c--Users-…` vs `C:\\Users\\…`)."""
    home_enc = re.sub(r"[:/\\.]", "-", str(Path.home()))
    if name.lower().startswith(home_enc.lower()):
        return (name[len(home_enc):].lstrip("-") or name)[:14]
    return name[:14]


def label_for(s: dict) -> str:
    summary = s.get("summary", "") or ""
    if summary:
        return summary
    fallback = _first_msg(s, 80)
    return fallback if fallback else _c("(empty)", GRAY)


def _pane_title(s: "dict | None", sid: str, term=None) -> str:
    """Human label for a live pane's tab — custom name (Shift+F2) → ai_title →
    summary → first user message → the term's launch title (e.g. a new session's
    folder name) → a short id only as a last resort, so a tab never shows just a
    bare session id."""
    if s:
        t = (s.get("custom_title") or s.get("ai_title") or s.get("summary")
             or _first_msg(s) or "").strip()
        if t:
            return t
    if term is not None:
        tt = (getattr(term, "title", "") or "").strip()
        if tt:
            return tt
    return sid[:8]


def _new_session_stub(sid: str, cwd: str, title: str) -> dict:
    """A placeholder session row for a just-launched NEW session whose JSONL is
    not scanned yet (it may even live under an out-of-scope project dir). Lets the
    list show the session immediately; the live-pane preserve in
    _apply_fresh_sessions keeps it across reloads until the real JSONL is found
    (same id) or the pane closes. Mirrors _enrich_session's field set so the
    render / sort / group / forest paths don't choke."""
    now = time.time()
    iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    enc = re.sub(r"[:/\\.]", "-", str(cwd))
    if sys.platform == "win32" and len(enc) >= 2 and enc[0].isalpha() and enc[1] == "-":
        enc = enc[0].lower() + enc[1:]   # Claude lowercases the Windows drive letter
    s = {
        "id": sid, "provider": _ACTIVE_PROVIDER.id,
        "first_ts": iso, "last_ts": iso, "ai_title": "",
        "summary": title, "real_msgs": [], "n_turns": 0,
        "jsonl_path": PROJECTS_ROOT / enc / f"{sid}.jsonl",
        "mtime": now, "cwd": cwd, "origin_cwd": cwd, "git_branch": "",
        "is_open": True, "session_status": "open", "is_active": True,
        "is_recent": True, "project_name": enc, "worktree_label": "",
        "topics": [], "primary_topic": "",
        "parent_id": None, "parent_score": 0.0, "parent_reasons": [],
    }
    s["last_active_dt"] = _compute_last_active_dt(s)
    return s


# ── Display ──────────────────────────────────────────────────────────────────
def _find_session_jsonl(sid_prefix: str) -> Path | None:
    sid_prefix = _trim_sid(sid_prefix)
    projects = PROJECTS_ROOT
    for p in projects.rglob(f"{sid_prefix}*.jsonl"):
        if "subagents" not in str(p):
            return p
    return None


def _extract_edited_files(jsonl_path, limit: int = 8) -> list[str]:
    """Scan a transcript for files the assistant edited/created (Edit / Write /
    MultiEdit / NotebookEdit tool calls). Returns up to `limit` unique basenames
    in first-seen order. Best-effort; [] on any error."""
    seen: list[str] = []
    try:
        with open(jsonl_path, "rb") as f:
            for line in f:
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                if obj.get("type") != "assistant":
                    continue
                content = (obj.get("message") or {}).get("content", [])
                if not isinstance(content, list):
                    continue
                for b in content:
                    if not (isinstance(b, dict) and b.get("type") == "tool_use"):
                        continue
                    if b.get("name") not in ("Edit", "Write", "MultiEdit",
                                             "NotebookEdit"):
                        continue
                    inp = b.get("input") or {}
                    fp = inp.get("file_path") or inp.get("notebook_path")
                    if fp:
                        base = os.path.basename(str(fp))
                        if base and base not in seen:
                            seen.append(base)
    except Exception:
        pass
    return seen[:limit]


def _render_header(s: dict) -> list[str]:
    found = s["jsonl_path"]
    hidden_tag = "  [HIDDEN]" if s["id"] in _load_hidden() else ""
    lines = [
        f"\033[1m{s['ai_title'] or '(no AI title)'}\033[0m{hidden_tag}",
        f"  id:       {s['id']}",
    ]
    pid = s.get("parent_id")
    if pid:
        score = s.get("parent_score", 0.0)
        reasons = s.get("parent_reasons", [])
        # Confidence marker reuses --related's legend: ● green ≥0.70, ● yellow
        # ≥0.40, ○ gray ≥0.20 — so a low-confidence "parent" link is visually
        # distinguishable from a strong one (the forest is heuristic).
        marker = _confidence_marker(score)
        rs = "  ·  ".join(reasons) if reasons else ""
        lines.append(f"  parent:   {marker} {pid[:8]}  [score {score:.2f}]  {_c(rs, GRAY)}")
    lines.append(f"  project:  {found.parent.name}")
    branch = s.get("git_branch") or ""
    if branch:
        lines.append(f"  branch:   {branch}")
    wt = s.get("worktree_label") or ""
    if wt:
        lines.append(f"  worktree: {_c(wt, GRAY)}")
    # Model + entry surface, read from the transcript like a statusline. Cheap in
    # practice: the preview is rendered then cached, so this runs once per session
    # until its mtime changes.
    try:
        _ep, _model = _session_surface_model(found)
    except Exception:
        _ep = _model = None
    if _model or _ep:
        _meta = [m for m in (_model, (f"via {_ep}" if _ep else "")) if m]
        lines.append(f"  model:    {_c('  ·  '.join(_meta), GRAY)}")
    lines.extend([
        f"  cwd:      {s.get('cwd','')}",
        f"  start:    {fmt_ts(s['first_ts'])}",
        f"  last:     {fmt_last_active(s)} ago  ({fmt_ts(s['last_ts'])})",
        f"  turns:    {s['n_turns']}",
    ])
    edited = _extract_edited_files(found)
    if edited:
        lines.append(f"  edited:   {_c(', '.join(edited), GRAY)}")
    lines.append("")
    return lines


def _render_preview(s: dict) -> str:
    """Condensed preview text: header + first/last user msgs."""
    lines = _render_header(s)
    lines.append("\033[36m── First user message ──\033[0m")
    if s["real_msgs"]:
        lines.append(s["real_msgs"][0][:1500])
    else:
        lines.append("(no real user messages)")
    if len(s["real_msgs"]) > 1:
        lines.append("")
        lines.append(f"\033[36m── Last user message  (#{len(s['real_msgs'])}) ──\033[0m")
        lines.append(s["real_msgs"][-1][:1500])
    lines.append("")
    lines.append("\033[2mTab: full/summary  ·  F8: changes (transcript diff)\033[0m")
    return "\n".join(lines)


def _render_preview_full(s: dict) -> str:
    """Full conversation text: every user msg + first ~400 chars per assistant reply."""
    lines = _render_header(s)
    lines.append("\033[36m── Full conversation ──\033[0m")
    found = s["jsonl_path"]
    n = 0
    try:
        with open(found, "rb") as f:
            for line in f:
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                t = obj.get("type", "")
                if t == "user":
                    text = _extract_text((obj.get("message") or {}).get("content", ""))
                    if _is_real_user_msg(text):
                        n += 1
                        lines.append(f"\033[36m▶ user [{n}]:\033[0m {text[:1200]}")
                elif t == "assistant":
                    content = (obj.get("message") or {}).get("content", [])
                    if isinstance(content, list):
                        for b in content:
                            if isinstance(b, dict) and b.get("type") == "text":
                                txt = b.get("text", "").strip()
                                if txt:
                                    lines.append(f"\033[33m◀ assistant:\033[0m {txt[:400]}")
                                    break
    except Exception:
        pass
    lines.append("")
    lines.append("\033[2mTab: full/summary  ·  F8: changes (transcript diff)\033[0m")
    return "\n".join(lines)


def _extract_session_changes(jsonl_path, max_ops: int = 40):
    """Reconstruct what a session changed from its OWN transcript: Edit /
    MultiEdit / Write / NotebookEdit tool calls record old_string / new_string /
    content. Returns an ordered list of (file_path, kind, old, new) — reliable
    for a session of any age, no git/worktree needed. Best-effort."""
    ops = []
    try:
        with open(jsonl_path, "rb") as f:
            for line in f:
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                if obj.get("type") != "assistant":
                    continue
                content = (obj.get("message") or {}).get("content", [])
                if not isinstance(content, list):
                    continue
                for b in content:
                    if not (isinstance(b, dict) and b.get("type") == "tool_use"):
                        continue
                    name = b.get("name", "")
                    inp = b.get("input") or {}
                    if name == "Edit":
                        ops.append((inp.get("file_path", ""), "edit",
                                    inp.get("old_string", ""), inp.get("new_string", "")))
                    elif name == "MultiEdit":
                        fp = inp.get("file_path", "")
                        for e in (inp.get("edits") or []):
                            if isinstance(e, dict):
                                ops.append((fp, "edit", e.get("old_string", ""),
                                            e.get("new_string", "")))
                                if len(ops) >= max_ops:   # cap INSIDE the inner loop too
                                    return ops
                    elif name == "Write":
                        ops.append((inp.get("file_path", ""), "write", "",
                                    inp.get("content", "")))
                    elif name == "NotebookEdit":
                        ops.append((inp.get("notebook_path", ""), "edit",
                                    inp.get("old_source", ""), inp.get("new_source", "")))
                    if len(ops) >= max_ops:
                        return ops
    except Exception:
        return ops
    return ops


def _render_preview_changes(s: dict) -> str:
    """Preview mode (F8): a diff-like view of what THIS session changed,
    reconstructed from the transcript's own Edit/Write records."""
    lines = _render_header(s)
    lines.append("\033[36m── Changes this session made (from transcript) ──\033[0m")
    ops = _extract_session_changes(s["jsonl_path"])
    if not ops:
        lines.append("(no file edits recorded in this session)")
    else:
        cur = None
        for fp, kind, old, new in ops:
            base = os.path.basename(fp) or fp or "(unknown)"
            if base != cur:
                lines.append("")
                lines.append(f"\033[1m{base}\033[0m")
                cur = base
            if kind == "write":
                nl = (new.count("\n") + 1) if new else 0
                lines.append(f"  \033[32m+ new / overwrite, {nl} lines\033[0m")
                for ln in new.splitlines()[:6]:
                    lines.append(f"  \033[32m+\033[0m {ln[:100]}")
            else:
                for ln in (old.splitlines()[:4] if old else []):
                    lines.append(f"  \033[31m-\033[0m {ln[:100]}")
                for ln in (new.splitlines()[:4] if new else []):
                    lines.append(f"  \033[32m+\033[0m {ln[:100]}")
    lines.append("")
    lines.append("\033[2mTab: full/summary  ·  F8: changes (this view)\033[0m")
    return "\n".join(lines)


def _write_if_stale(path: Path, mtime: float, render) -> None:
    """Write `render()` to path only if path is missing or its mtime drifts from `mtime`."""
    if path.exists():
        try:
            if abs(path.stat().st_mtime - mtime) < 1.0:
                return
        except Exception:
            pass
    tmp = None
    try:
        import tempfile
        path.parent.mkdir(parents=True, exist_ok=True)
        # Atomic write: concurrent warmers (the background pre-warm thread and
        # the UI-thread on-demand fallback) must never observe a torn file.
        fd, tmp = tempfile.mkstemp(dir=str(path.parent),
                                   prefix=path.name + ".", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(render())
        os.replace(tmp, path)
        tmp = None
        os.utime(path, (mtime, mtime))
    except Exception:
        if tmp is not None:
            try:
                os.unlink(tmp)
            except Exception:
                pass


def _write_preview_cache(s: dict) -> None:
    # Pre-render so the preview pane can read a cached file instead of cold-starting Python (~150ms → ~5ms per cursor move).
    # Both files are mtime-gated; reloads (F5/F6/F7) skip rewrites for unchanged sessions.
    sid = s["id"]
    mtime = s.get("mtime", 0.0)
    _write_if_stale(PREVIEW_DIR / f"{sid}.txt", mtime, lambda: _render_preview(s))
    _write_if_stale(PREVIEW_FULL_DIR / f"{sid}.txt", mtime, lambda: _render_preview_full(s))


def _preview_impl(session_id: str, cache_dir: Path, render) -> None:
    sid = _trim_sid(session_id)
    if not sid:
        # Group-header / separator rows carry an empty SID column. Returning
        # silently avoids the caller spinning a "loading" indicator while it
        # waits for the preview command to do nothing useful.
        return
    # Exact cache hit (full UUID — fast path)
    cache_file = cache_dir / f"{sid}.txt"
    if cache_file.exists():
        sys.stdout.write(cache_file.read_text(encoding="utf-8"))
        return
    # Cache miss: probably a partial SID typed by the user on the CLI. Resolve
    # to the full SID via the JSONL filename and retry cache before falling
    # back to a fresh parse (which is missing forest-derived parent info, so
    # the user would otherwise see a degraded preview).
    found = _find_session_jsonl(sid)
    if not found:
        print(f"(session {sid[:8]} not found)")
        return
    full_cache = cache_dir / f"{found.stem}.txt"
    if full_cache.exists():
        sys.stdout.write(full_cache.read_text(encoding="utf-8"))
        return
    s = parse_session(found)
    if not s:
        print("(unable to parse session)")
        return
    print(render(s))


def preview_session(session_id: str) -> None:
    _preview_impl(session_id, PREVIEW_DIR, _render_preview)


def preview_session_full(session_id: str) -> None:
    _preview_impl(session_id, PREVIEW_FULL_DIR, _render_preview_full)


_MARKER_BLANK = " "


# Markers are intentionally ASCII (1 cell, terminal-width-independent). The
# previous Unicode glyphs (◉●○★✗) were East-Asian-Ambiguous, which made their
# cell count depend on the terminal's CJK-width setting — and saikai can't
# reliably probe that, so columns drifted whenever the user's terminal didn't
# match the static assumption. Letters trade a bit of visual flair for
# reliable column alignment everywhere.
def _activity_marker(s: dict) -> str:
    """Activity column: open-busy / open-idle / active / recent."""
    if s.get("is_open"):
        if s.get("session_status") == "busy":
            return _c("@", CYAN, BOLD)   # open & currently responding
        return _c("@", GREEN, BOLD)      # open & idle in another Claude window
    if s.get("is_active"):
        return _c("+", GREEN)
    if s.get("is_recent"):
        return _c(".", YELLOW)
    return _MARKER_BLANK


def _state_marker(s: dict, hidden: set, favorites: set) -> str:
    """State column: favorite or hidden (mutually exclusive)."""
    sid = s["id"]
    if sid in favorites:
        return _c("*", GOLD)
    if sid in hidden:
        return _c("x", RED)
    return _MARKER_BLANK


def fmt_last_active(s: dict) -> str:
    """Human-friendly 'last activity' column: '5m', '2h', '3d', '04/22'.
    Keyed on _last_active_dt so the column matches the Recency sort exactly."""
    dt = _last_active_dt(s)
    if dt is None:
        return ""
    age = max(0.0, (datetime.now() - dt).total_seconds())
    if age < 60:
        return "now"
    if age < 3600:
        return f"{int(age/60)}m"
    if age < 86400:
        return f"{int(age/3600)}h"
    if age < 86400 * 7:
        return f"{int(age/86400)}d"
    return dt.strftime("%m/%d")


def display_table(sessions: list[dict], repo: Path | None, show_project: bool,
                  flat: bool = False):
    title_col_width = 44 if show_project else 54
    hidden = _load_hidden()
    print()
    if show_project:
        header = (f"   {'Start':<11}  {'Last':<5}  {'Project':<14}  {'ID':<10} "
                  f"{'Turns':>5}  {'Title':<{title_col_width}}  Git commits")
    else:
        header = (f"   {'Start':<11}  {'Last':<5}  {'ID':<10} "
                  f"{'Turns':>5}  {'Title':<{title_col_width}}  Git commits")
    print(_c(header, BOLD))
    print("  " + "─" * (122 if show_project else 106))
    favorites = _load_favorites()
    walked: list[tuple[dict, str]] = (
        [(s, "") for s in sessions] if flat else _tree_walk(sessions)
    )
    for s, tree_prefix in walked:
        is_hidden = s["id"] in hidden
        # Two-column marker: activity + favorite/hidden state
        act = _activity_marker(s)
        st  = _state_marker(s, hidden, favorites)
        marker = f"{act}{st}"
        start = fmt_ts(s["first_ts"])
        last = fmt_last_active(s)
        sid8  = short_id(s["id"])
        turns = str(s["n_turns"]) if s["n_turns"] > 0 else "?"
        prefix_w = visible_len(tree_prefix)
        lbl_raw = tree_prefix + label_for(s)
        lbl = truncate_visual(lbl_raw, title_col_width + prefix_w)

        commits = ""
        if repo:
            cc = git_commits_in_range(s["first_ts"], s["last_ts"], repo)
            if cc:
                commits = _c(truncate_visual(cc[0], 50), GRAY)
                if len(cc) > 1:
                    commits += _c(f" +{len(cc)-1}", DIM)

        if is_hidden:
            # Whole row in dim+gray so hidden state is unmistakable
            col_start = pad(start, 13)
            col_last  = pad(last, 7)
            col_id    = pad(sid8, 12)
            col_turns = f"{turns:>5}"
            col_lbl   = pad(lbl, title_col_width)
            if show_project:
                col_proj = pad(project_short(s.get("project_name") or ""), 16)
                row = f" {marker} {HIDDEN_DIM}{col_start} {col_last} {col_proj} {col_id} {col_turns}  {col_lbl}  (hidden){RESET}"
            else:
                row = f" {marker} {HIDDEN_DIM}{col_start} {col_last} {col_id} {col_turns}  {col_lbl}  (hidden){RESET}"
            print(row)
        else:
            col_start = pad(_c(start, CYAN), 13)
            col_last  = pad(_c(last, GREEN if s.get("is_active") else (YELLOW if s.get("is_recent") else GRAY)), 7)
            col_id    = pad(_c(sid8, YELLOW), 12)
            col_turns = f"{turns:>5}"
            col_lbl   = pad(lbl, title_col_width)
            if show_project:
                col_proj = pad(_c(project_short(s.get("project_name") or ""), MAGENTA), 16)
                print(f" {marker} {col_start} {col_last} {col_proj} {col_id} {col_turns}  {col_lbl}  {commits}")
            else:
                print(f" {marker} {col_start} {col_last} {col_id} {col_turns}  {col_lbl}  {commits}")
    print()
    view_mode = _get_view_mode()
    mode_tag = _c(" [show-hidden mode]", RED) if view_mode == "show-hidden" else ""
    legend = (f"  {len(sessions)} sessions{mode_tag}  ·  "
              f"{_c('*', GOLD)} fav  {_c('+', GREEN)} active(<5m)  "
              f"{_c('.', YELLOW)} recent(<30m)  {_c('x', RED)} hidden  "
              f"{_c('@', CYAN)} open  ·  saikai to resume")
    print(_c(legend, DIM))
    print()


def _reset_terminal_modes() -> None:
    """Emit ANSI disable sequences for terminal modes the picker may have enabled.

    Targets focus tracking (?1004), all mouse tracking variants (?1000/1002/1003/
    1006/1015), bracketed paste (?2004), and ensures the cursor is visible (?25).
    These are no-ops if the mode is already off — safe to send unconditionally.

    Why this exists: on Windows, the picker occasionally exits without sending the matching
    'l' sequence for focus / mouse SGR, so the shell receives literal '[I' (focus
    in) or stray 'm' (SGR mouse release terminator) characters.

    Also registered as an atexit (see main): Textual restores these on a clean OR
    exception exit, so this is belt-and-suspenders for the paths that bypass its
    teardown (driver crash, SystemExit, watchdog). Hard kills (taskkill/OOM)
    can't run atexit — a fresh terminal is the only cure there. Guarded on
    isatty so a redirected stderr never receives escape bytes."""
    try:
        out = sys.stderr
        if out is None or (hasattr(out, "isatty") and not out.isatty()):
            return
        if sys.platform == "win32":
            # Re-arm VT processing so the sequence is INTERPRETED, not printed,
            # even if the console mode was reset on the way down.
            try:
                import ctypes
                k32 = ctypes.windll.kernel32
                h = k32.GetStdHandle(-12)        # STD_ERROR_HANDLE
                mode = ctypes.c_uint32()
                if k32.GetConsoleMode(h, ctypes.byref(mode)):
                    k32.SetConsoleMode(h, mode.value | 0x0004)  # ENABLE_VIRTUAL_TERMINAL_PROCESSING
            except Exception:
                pass
        out.write(
            "\033[?1000l\033[?1002l\033[?1003l\033[?1004l"
            "\033[?1006l\033[?1015l\033[?2004l\033[?25h"
        )
        out.flush()
    except Exception:
        pass


# Threshold for "frequent cwd": when auto-permission is explicitly enabled, a
# directory must have hosted at least this many sessions before saikai adds
# --permission-mode auto on resume. Tuned by eye on a working history of
# ~hundreds of sessions; a handful of long-lived repos comfortably clear it
# while one-off cwds (downloaded folders, temp experiments) don't. Override
# with SAIKAI_FREQ_CWD_MIN env var.
FREQ_CWD_MIN_DEFAULT = 5


def _canonical_workspace(cwd: str) -> str:
    """Collapse a git worktree path back to its parent repo.

    The user thinks of `feature-x` as a branch of `myrepo`, but saikai sees
    `myrepo/.worktrees/feature-x/` as a distinct cwd. Without this, every
    worktree splits its parent repo's session count, so a workspace the user
    visits constantly (just on different branches) can fall below the frequent
    threshold. Collapse to the parent so the count reflects user intent."""
    if not cwd:
        return cwd
    norm = _norm_cwd(cwd)
    marker = os.sep + ".worktrees" + os.sep
    idx = norm.find(marker)
    return norm[:idx] if idx >= 0 else norm


def _frequent_cwds(sessions: list[dict]) -> set[str]:
    """Return the set of normalised workspaces that appear in >= N sessions.

    Used to identify candidate working directories when auto-permission is
    explicitly enabled. Worktrees are folded into their parent repo via
    `_canonical_workspace` so branch-switching doesn't split the count."""
    try:
        min_count = max(2, int(os.environ.get("SAIKAI_FREQ_CWD_MIN") or FREQ_CWD_MIN_DEFAULT))
    except ValueError:
        min_count = FREQ_CWD_MIN_DEFAULT
    counts = Counter(_canonical_workspace(s.get("cwd") or "") for s in sessions)
    return {cwd for cwd, n in counts.items() if cwd and n >= min_count}


def _assign_primary_topic(sessions: list[dict]) -> None:
    """Pick the most widely-shared topic for each session in place.

    'Primary' = the topic from this session's topics list that the maximum
    number of OTHER sessions also have. This groups rows around common-
    interest topics (e.g. 'email', 'backend') rather than singleton labels
    from session-unique topics. Sets s["primary_topic"] (lowercase) or ""
    when the session has no cached topics yet. Feeds the Topic sort column
    and `display.color_by = topic` title tinting; topics themselves come
    from the (opt-in) summary pipeline's cache, so with summaries off every
    session simply stays ""."""
    topic_count: Counter = Counter()
    for s in sessions:
        for t in (s.get("topics") or []):
            topic_count[t.lower()] += 1
    for s in sessions:
        topics = [t.lower() for t in (s.get("topics") or [])]
        s["primary_topic"] = max(topics, key=lambda t: topic_count[t]) if topics else ""


# Colour palettes for Textual cell tinting. Distinct values inside a column
# (project / topic) get a stable hash-derived colour so the same value renders
# in the same colour every time and the eye can group rows by colour even
# faster than by reading the cell text.
_PROJECT_PALETTE = ("cyan", "yellow", "green", "magenta", "bright_blue",
                    "bright_red", "white", "bright_cyan", "bright_yellow",
                    "bright_green", "bright_magenta")
_TOPIC_PALETTE = ("magenta", "cyan", "yellow", "green", "bright_blue",
                  "bright_red", "white", "bright_magenta", "bright_cyan",
                  "bright_yellow", "bright_green")


def _stable_color(value: str, palette) -> str:
    """Hash-only fallback when no collision-resolving mapping is in play.
    Prefer _build_color_map for the picker: it linearly probes the palette
    so distinct values are guaranteed distinct colours when count ≤ palette
    size."""
    if not value:
        return ""
    import hashlib
    h = int(hashlib.md5(value.encode("utf-8")).hexdigest(), 16)
    return palette[h % len(palette)]


def _build_color_map(values, palette) -> dict[str, str]:
    """Assign each unique value a distinct palette colour (linear-probe over
    the hash slot). When unique_count ≤ palette size we get an injective
    mapping — same input → same colour across runs, and no two visible
    values share a colour. Values overflow by hashing to the same slot only
    once the palette is full."""
    import hashlib
    used: dict[str, str] = {}
    occupied: set[str] = set()
    # Sort so the assignment is deterministic regardless of iteration order
    # of the input collection (sets etc.).
    for v in sorted({v for v in values if v}):
        h = int(hashlib.md5(v.encode("utf-8")).hexdigest(), 16) % len(palette)
        for i in range(len(palette)):
            c = palette[(h + i) % len(palette)]
            if c not in occupied:
                used[v] = c
                occupied.add(c)
                break
        else:
            used[v] = palette[h]   # palette exhausted — wrap
    return used


# System memory snapshot for the live-pane gate. Any field may be None on a
# platform that can't supply it (the gate skips that constraint). The PRIMARY
# signal is per-OS, derived from how each kernel actually fails:
#   Windows — NO overcommit: every private allocation needs commit backing
#     (RAM+pagefile); commit exhaustion = allocation failures = the documented
#     system-wide freeze. avail_commit_mb (ullAvailPageFile) is the primary
#     signal, NOT avail_phys_mb (standby+free+zero over-states headroom).
#   Linux — heuristic overcommit by default (vm.overcommit_memory=0):
#     CommitLimit is NOT enforced and Committed_AS routinely exceeds it (V8
#     reserves huge never-touched ranges), so commit headroom is noise unless
#     strict mode (=2) is on. MemAvailable is the kernel's own purpose-built
#     "allocatable without swapping" estimate → the physical floor is the
#     primary gate, and PSI (/proc/pressure/memory, the metric systemd-oomd
#     kills on) directly measures stall time when thrash actually happens.
#   macOS — dynamic swap + memory compressor, no fixed commit limit; the OS
#     publishes its own verdict via kern.memorystatus_vm_pressure_level.
# pressure_pct: 0-100 stall/pressure measure (Linux PSI some avg10; macOS 100
# when the kernel reports critical), None where unsupported (Windows).
_MemStatus = namedtuple(
    "_MemStatus",
    "load avail_phys_mb avail_commit_mb total_phys_mb pressure_pct",
    defaults=(None,))
_MB = 1024 * 1024


def _parse_macos_vm_stat(vm_stat_text, total_bytes, pressure_level=None):
    """Parse `vm_stat` output + `sysctl hw.memsize` total into a _MemStatus (macOS).
    Available ≈ reclaimable pages (free + inactive + speculative + purgeable) × page
    size; load = used/total. macOS has no fixed commit limit (dynamic swap), so
    avail_commit_mb is None and that gate check is skipped. pressure_level is
    kern.memorystatus_vm_pressure_level (1 normal / 2 warn / 4 critical) — the
    kernel's own verdict, the same signal apps get via dispatch sources; only
    CRITICAL maps to a gating pressure_pct (warn fires routinely under benign
    cache pressure). Pure → unit-testable (subprocess calls live in _mem_status)."""
    m = re.search(r"page size of (\d+) bytes", vm_stat_text or "")
    pagesize = int(m.group(1)) if m else 4096

    def _pages(label):
        mm = re.search(rf"{re.escape(label)}:\s+(\d+)", vm_stat_text or "")
        return int(mm.group(1)) if mm else 0
    avail_pages = (_pages("Pages free") + _pages("Pages inactive")
                   + _pages("Pages speculative") + _pages("Pages purgeable"))
    total = int(total_bytes or 0)
    if total <= 0:
        return None
    avail = max(0, min(avail_pages * pagesize, total))
    load = max(0.0, min(100.0, (total - avail) / total * 100.0))
    pressure = 100.0 if (pressure_level is not None and pressure_level >= 4) else None
    return _MemStatus(load, avail / _MB, None, total / _MB, pressure)


def _parse_linux_psi_some_avg10(psi_text):
    """`some avg10` percent out of /proc/pressure/memory (kernel >= 4.20), or
    None when absent/unparseable (old kernel, masked /proc in a container).
    "some avg10" = % of the last 10s in which at least one task was STALLED
    waiting on memory — the direct observation of thrash (the same metric
    systemd-oomd acts on), as opposed to occupancy proxies. Pure."""
    m = re.search(r"^some\s+avg10=([0-9.]+)", psi_text or "", re.M)
    try:
        return float(m.group(1)) if m else None
    except ValueError:
        return None


def _parse_linux_meminfo(meminfo_text, overcommit_mode=None, psi_text=None):
    """Parse /proc/meminfo (+ optional overcommit mode and PSI text) into a
    _MemStatus (Linux). MemAvailable is the kernel's purpose-built "allocatable
    without swapping" estimate (reclaimable cache/slab already accounted), so
    it feeds both the load % and the physical floor. Commit headroom
    (CommitLimit − Committed_AS) is only meaningful under STRICT overcommit
    (vm.overcommit_memory == 2) — in the default heuristic mode the limit is
    not enforced and Committed_AS routinely exceeds it (V8/node reserve huge
    never-touched ranges), which would read as negative headroom and falsely
    zero the gate on a healthy box. Pure → unit-testable."""
    info = {}
    for line in (meminfo_text or "").splitlines():
        k, _, rest = line.partition(":")
        info[k.strip()] = rest

    def _kb(key):
        try:
            return int(info[key].split()[0]) / 1024   # KB → MB
        except Exception:
            return None
    total, availp = _kb("MemTotal"), _kb("MemAvailable")
    load = (max(0.0, min(100.0, (total - availp) / total * 100.0))
            if (total and availp is not None) else None)
    commit = None
    if overcommit_mode == 2:
        climit, ccur = _kb("CommitLimit"), _kb("Committed_AS")
        if climit is not None and ccur is not None:
            commit = climit - ccur
    return _MemStatus(load, availp, commit, total,
                      _parse_linux_psi_some_avg10(psi_text))


def _mem_status():
    """Return a _MemStatus (None if wholly unknown). Windows: GlobalMemoryStatusEx
    (load=dwMemoryLoad, commit=ullAvailPageFile). Linux: /proc/meminfo (load from
    MemAvailable; commit only under strict overcommit) + /proc/pressure/memory
    (PSI). macOS: sysctl hw.memsize + vm_stat + kern.memorystatus_vm_pressure_level
    (no fixed commit limit → that check skipped). Any probe failure → None → the
    gate is simply disabled (safe degradation)."""
    try:
        if sys.platform == "win32":
            import ctypes

            class _MS(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong),
                            ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong),
                            ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong),
                            ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong),
                            ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
            ms = _MS()
            ms.dwLength = ctypes.sizeof(_MS)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(ms)):
                return _MemStatus(float(ms.dwMemoryLoad), ms.ullAvailPhys / _MB,
                                  ms.ullAvailPageFile / _MB, ms.ullTotalPhys / _MB)
        elif sys.platform == "darwin":
            import subprocess
            total = int(subprocess.run(
                ["sysctl", "-n", "hw.memsize"], capture_output=True, text=True,
                timeout=2).stdout.strip())
            vmstat = subprocess.run(
                ["vm_stat"], capture_output=True, text=True, timeout=2).stdout
            level = None
            try:
                level = int(subprocess.run(
                    ["sysctl", "-n", "kern.memorystatus_vm_pressure_level"],
                    capture_output=True, text=True, timeout=2).stdout.strip())
            except Exception:
                pass   # sysctl absent/denied → pressure check skipped
            return _parse_macos_vm_stat(vmstat, total, level)
        else:
            with open("/proc/meminfo", encoding="utf-8") as f:
                meminfo = f.read()
            overcommit = None
            try:
                with open("/proc/sys/vm/overcommit_memory", encoding="utf-8") as f:
                    overcommit = int(f.read().strip())
            except Exception:
                pass   # unreadable (container) → treat as heuristic, skip commit
            psi = None
            try:
                with open("/proc/pressure/memory", encoding="utf-8") as f:
                    psi = f.read()
            except Exception:
                pass   # kernel < 4.20 or masked /proc → PSI check skipped
            return _parse_linux_meminfo(meminfo, overcommit, psi)
    except Exception:
        return None
    return None


def _avail_ram_mb():
    """Available physical RAM in MB (None if unknown) — thin wrapper over
    _mem_status() kept for callers that only need the headline number."""
    st = _mem_status()
    return st.avail_phys_mb if st is not None else None


def _ram_fit(st, per_pane_mb, *, max_load, min_commit_mb,
             min_free_phys_pct, min_free_phys_mb=0.0, max_pressure=10.0):
    """How many more ~per_pane_mb live panes fit before a resource-gate
    threshold trips, and which one binds. Returns (count, binding_reason).

    Constraints (a None field skips its check): (0) measured memory PRESSURE
    (Linux PSI some avg10 / macOS critical level) at/above max_pressure —
    tasks are already stalling on memory, the direct observation of thrash,
    so no new pane regardless of occupancy numbers; (1) memory-load
    high-water — already-pressured → 0; (2) commit headroom must stay above
    min_commit_mb — the documented system-freeze cause, the PRIMARY check on
    Windows (skipped on Linux unless strict overcommit; never on macOS); (3) a
    RELATIVE physical floor (min_free_phys_pct of total, but at least
    min_free_phys_mb) — anti-thrash, machine-relative not a fixed MB. None st
    → unbounded."""
    if st is None or per_pane_mb <= 0:
        return (999, "")
    pressure = getattr(st, "pressure_pct", None)
    if pressure is not None and pressure >= max_pressure:
        return (0, f"memory pressure {pressure:.0f}% ≥ {max_pressure:.0f}% (PSI)")
    if st.load is not None and st.load >= max_load:
        return (0, f"memory load {st.load:.0f}% ≥ {max_load:.0f}%")
    cand = []   # (fit_count_float, reason)
    if st.avail_commit_mb is not None:
        cand.append(((st.avail_commit_mb - min_commit_mb) / per_pane_mb,
                     f"commit headroom {st.avail_commit_mb:.0f}MB (keep {min_commit_mb:.0f})"))
    if st.avail_phys_mb is not None and st.total_phys_mb:
        floor = max(min_free_phys_pct / 100.0 * st.total_phys_mb, min_free_phys_mb)
        cand.append(((st.avail_phys_mb - floor) / per_pane_mb,
                     f"free RAM {st.avail_phys_mb:.0f}MB (keep {floor:.0f})"))
    if not cand:
        return (999, "")
    fit_f, reason = min(cand, key=lambda c: c[0])
    return (max(0, int(fit_f)), reason if fit_f < 1 else "")


def _ram_gate_decision(st, per_pane_mb, **kw):
    """(ok, reason) for opening ONE more live pane — ok iff at least one more fits
    under _ram_fit. Pure; the gate + the statusbar 'fit' indicator share this math."""
    fit, reason = _ram_fit(st, per_pane_mb, **kw)
    return (fit >= 1, reason)


# Default memory-load high-water, per OS. Windows' dwMemoryLoad is an
# INDEPENDENT kernel signal (standby-aware) → 85 is a real early warning.
# On Linux/macOS saikai DERIVES load from the same availability number the
# physical floor uses, so a strict load cutoff just double-counts the floor
# and closes the gate while ~15% is still genuinely available (the "saikai
# is stingier than Linux actually is" effect). 95 keeps it as a backstop
# against a misconfigured floor; the floor + PSI are the real POSIX gates.
_DEFAULT_MAX_LOAD = 85.0 if sys.platform == "win32" else 95.0


def _ram_gate_kwargs() -> dict:
    """Live-pane gate thresholds resolved env > config > default (spec §A.1). Shared
    by the open-gate and the statusbar 'fit' indicator so they can't disagree."""
    return dict(
        max_load=_cfg("limits", "max_memory_load", "SAIKAI_MAX_MEM_LOAD", _DEFAULT_MAX_LOAD, float),
        min_commit_mb=_cfg("limits", "min_commit_headroom_mb", "SAIKAI_MIN_COMMIT_MB", 2048.0, float),
        min_free_phys_pct=_cfg("limits", "min_free_phys_pct", "SAIKAI_MIN_FREE_PHYS_PCT", 8.0, float),
        min_free_phys_mb=_cfg("limits", "min_free_mb", "SAIKAI_MIN_FREE_MB", 0.0, float),
        max_pressure=_cfg("limits", "max_memory_pressure", "SAIKAI_MAX_MEM_PRESSURE", 10.0, float),
    )


def _ram_per_pane_mb() -> float:
    """Estimated RAM per live pane (env > config > default)."""
    return _cfg("limits", "per_pane_mb", "SAIKAI_CLAUDE_MB", 600.0, float)


def _copy_to_host_clipboard(text: str) -> bool:
    """Copy `text` to the HOST OS clipboard via the platform clip tool, so the
    tokened mirror URL pastes cleanly. Returns True only on a clean exit, so the
    QR screen can tell the truth about whether the copy worked (e.g. `xclip` may
    be absent on Linux). Bounded by a timeout: this runs on the Textual UI thread
    (F12 / startup), and `xclip` can otherwise daemonize and block the event loop
    holding the X selection — a timeout caps the worst case and reports False."""
    import subprocess as _sp
    clip = (["clip"] if sys.platform == "win32"
            else ["pbcopy"] if sys.platform == "darwin"
            else ["xclip", "-selection", "clipboard"])
    try:
        return _sp.run(clip, input=text.encode("utf-8"), timeout=2.0).returncode == 0
    except Exception:
        return False


class _MirrorControl:
    """Phase B web-mirror interactive control, mixed into PickerApp.

    Lives at module scope (PickerApp itself is defined inside textual_pick, so it
    needs textual and is unreachable headless) so the UI-thread/PTY-guard invariant
    can be tested without textual via __new__ + a FakePty, exactly like the
    ClaudeTerminal guards in tests/test_terminal_concurrency.py.
    """

    # AUTHORITATIVE gate, default OFF, in-memory, re-checked on the UI thread in
    # _mirror_inject_input. The hub keeps only an advisory copy for do_POST's
    # fast-reject.
    _control_enabled: bool = False

    def _mirror_inject_input(self, data: str) -> None:
        """Drive saikai from the browser keyboard, terminal-equivalently. Parse the
        browser's terminal byte stream with Textual's OWN XTermParser and post the
        resulting Key/Paste events to the App, which routes them EXACTLY as the host
        terminal does:
          * a focused live pane (AgentTerminal.on_key) -> encode the key to its
            child PTY (claude), RELEASE focus on the release key (Ctrl+]), or
            interrupt claude on Ctrl+C -- the very same on_key path as the host;
          * the list / search box / dialogs -> navigation, search-as-you-type,
            bindings.
        ONE path, no pane/no-pane special-casing: every key the browser produces
        (printables, arrows, Home/End, Page keys, Delete, F-keys, Shift+Tab,
        Ctrl/Alt combos, Enter, Backspace, bracketed paste) behaves as if typed at
        the host terminal. Runs on the UI thread (the input handler marshals here);
        re-checks the AUTHORITATIVE _control_enabled first (the hub copy is
        advisory). The parser is stateful (reassembles a sequence split across POST
        batches), created once per app. textual + the parser import in-body so the
        mixin stays importable headless; mouse SGR never arrives here (the browser
        routes taps to /mouse), so only Key/Paste tokens are forwarded. If the
        (private) parser API is ever unavailable, degrade to printable characters."""
        if not self._control_enabled:
            return
        from textual import events
        if data == "\x1b":
            # A bare Esc keypress arrives as its OWN /input batch (the browser
            # flushes ESC, a C0 control byte). A lone ESC fed to the stateful
            # XTermParser buffers with no escape-timeout flush (the real driver has
            # one) and then SWALLOWS every following key -- the keyboard goes dead
            # after any Esc. Emit Escape directly; never poison the parser. (xterm
            # emits a full escape SEQUENCE in one onData, so only a bare Esc lands
            # here as a lone ESC.)
            try:
                self.post_message(events.Key("escape", None))
            except Exception:
                pass
            return
        parser = getattr(self, "_mirror_parser", None)
        if parser is None:
            try:
                from textual._xterm_parser import XTermParser
                parser = XTermParser()
            except Exception:
                parser = False                 # parser API gone: remember + fall back
            self._mirror_parser = parser
        if parser:
            try:
                tokens = list(parser.feed(data))
            except Exception:
                return
            for ev in tokens:
                if isinstance(ev, (events.Key, events.Paste)):
                    try:
                        self.post_message(ev)
                    except Exception:
                        pass
            return
        for ch in data:                        # fallback: printable characters only
            if ch.isprintable():
                try:
                    self.post_message(events.Key(ch, ch))
                except Exception:
                    pass

    def _mirror_inject_mouse(self, col: int, row: int, button: int, kind: str) -> None:
        """Post a synthesized Textual mouse event into the App so it routes
        natively (App.on_event hit-tests get_widget_at and synthesizes the Click
        for a down+up pair -> DataTable sort / row cursor / pane focus).

        Runs on the Textual UI thread (the mouse handler marshals here via
        call_from_thread). Re-checks the AUTHORITATIVE _control_enabled (the hub's
        copy is advisory). Coords are 0-based screen cells (the browser already
        converted xterm's 1-based report). events is imported here, not at module
        scope, so this mixin stays importable without textual."""
        if not self._control_enabled:
            return
        if col < 0 or row < 0:                 # out-of-range cell: ignore
            return
        from textual import events
        if kind == "down":
            cls = events.MouseDown
        elif kind == "up":
            cls = events.MouseUp
        elif kind == "scrollup":
            cls = events.MouseScrollUp
        elif kind == "scrolldown":
            cls = events.MouseScrollDown
        else:
            return                             # unknown kind: never post garbage
        # Scroll has no pressed button (0); a click carries the SGR button index.
        btn = button if kind in ("down", "up") else 0
        ev = cls(None, col, row, 0, 0, btn, False, False, False,
                 screen_x=col, screen_y=row)
        try:
            self.post_message(ev)
        except Exception:
            pass                               # app tearing down between gate + post

    def _mirror_inject_key(self, key: str) -> None:
        """Post a synthesized events.Key into the App so it routes to priority
        bindings / the focused widget (the same path Pilot.press uses) -> saikai's
        leader, F-keys, arrows, Esc/Tab all dispatch natively.

        Runs on the Textual UI thread (the key handler marshals here via
        call_from_thread). Re-checks the AUTHORITATIVE _control_enabled. A single
        printable char carries itself as the Key.character; a named/modified key
        ('escape', 'tab', 'up', 'ctrl+c', 'f12') carries character=None (Textual's
        Key.__init__ leaves it None for len != 1). events imported in-body to keep
        the mixin textual-free at import."""
        if not self._control_enabled:
            return
        if not isinstance(key, str) or key == "":
            return                             # never post a garbage Key
        from textual import events
        character = key if len(key) == 1 else None
        try:
            self.post_message(events.Key(key, character))
        except Exception:
            pass                               # app tearing down between gate + post

    def _mirror_clients_changed(self, n: int) -> None:
        """A browser connected to / disconnected from the mirror (now `n` viewers).
        Runs on the UI thread (the hub's change handler marshals here). Toast on a
        NEW connection so the user notices an unexpected viewer, and remember the
        count so the F12 QR screen can show how many are watching."""
        prev = getattr(self, "_mirror_clients", 0)
        self._mirror_clients = n
        if n > prev:
            try:
                self.notify(f"\N{GLOBE WITH MERIDIANS} mirror: a browser connected "
                            f"— {n} now viewing", title="saikai", timeout=6)
            except Exception:
                pass


def textual_pick(sessions: list[dict], repo: Path | None, show_project: bool,
                 flat: bool = False, reload_fn=None) -> None:
    """Textual-based picker (status bar, mouse-click column sort, ? help overlay).

    Layout:
      ┌─────────────────────────────────────────┐
      │ Search:                                 │  Input — filter title/msgs/sid/proj
      ├──────────────────┬──────────────────────┤
      │ DataTable        │ Preview (RichLog)    │
      │ ↑↓ Enter         │ Updates on cursor    │
      └──────────────────┴──────────────────────┘
      Footer (key bindings)

    All toggles reflect in-app (no restart required):
      Enter        resume                Esc (saikai controls) leave / quit
      Ctrl-]       pane → list           Ctrl-C        pane interrupt / app quit
      F7           hide/unhide row       F6            favorite toggle
      Shift-F5     toggle tree display
      Tab          preview full/summary  ?             help overlay
      (ordinary Ctrl+letter editing keys stay with the search box / live
       claude; app shortcuts use function keys plus the configurable release key)
      (":hidden" in the search box reveals hidden rows;
       click a column header to sort, click again to reverse)

    Mouse — click a column header to promote it to priority 1; click the
    same header again to flip its direction. The previous priority 1
    becomes priority 2, and so on (priority 3 drops off). The column
    label shows the current state, e.g. "Start 1v" = priority 1, desc.
    """
    try:
        from textual.app import App, ComposeResult
        from textual.actions import SkipAction
        from textual.binding import Binding
        from textual.containers import Horizontal, Vertical, VerticalScroll
        from textual.screen import ModalScreen
        from textual.widgets import (DataTable, Footer, Input, OptionList, RichLog,
                                     Select, Static, Switch, TabbedContent,
                                     TabPane, Tabs)
        from textual.widgets.option_list import Option
        from rich.text import Text
    except ImportError as e:
        print(_c(f"  textual is required but not installed ({e}). "
                 f"Install it with: uv tool install textual "
                 f"(or: pip install 'textual>=0.50')", RED), file=sys.stderr)
        sys.exit(1)

    # Live split-terminal support is OPTIONAL and degrades gracefully: if
    # saikai_terminal or its PTY/pyte deps are missing, _LIVE_TERM stays None and
    # the picker behaves exactly as before (static preview, Enter = full-takeover
    # resume). The import lives beside saikai.py (single-file script's sibling).
    _LIVE_TERM = None
    _LIVE_TERM_REASON = "saikai_terminal module not found"
    try:
        import saikai_terminal as _LIVE_TERM  # type: ignore
        if not _LIVE_TERM.TERMINAL_AVAILABLE:
            _LIVE_TERM_REASON = _LIVE_TERM.unavailable_reason() or "unavailable"
            _LIVE_TERM = None
    except Exception as _lte:  # pragma: no cover - missing sibling / dep
        _LIVE_TERM_REASON = repr(_lte)
        _LIVE_TERM = None
    if _LIVE_TERM is not None:
        _LIVE_TERM.configure_release_focus_key(_release_focus_key())
    # Split-live is now the DEFAULT whenever its PTY deps are present. The legacy
    # full-takeover path is NOT removed — it survives as three things: (a) the
    # automatic fallback ABOVE when pyte/pywinpty/ptyprocess are missing, (b) an
    # explicit opt-out (SAIKAI_SPLIT_LIVE=0/false/no/off → list-only + Enter =
    # full-takeover resume), and (c) a per-session escape hatch even inside
    # split-live (action_resume_detached). So SAIKAI_SPLIT_LIVE is a tri-state
    # opt-OUT: unset/truthy → on, explicit falsy → off.
    _sl_env = os.environ.get("SAIKAI_SPLIT_LIVE")
    if _sl_env not in (None, ""):
        _split_off = _split_live_disabled_by_env(_sl_env)   # env present → env decides (tri-state)
    else:
        _split_off = (_load_config().get("display", {}).get("split_live") is False)  # else config
    if _LIVE_TERM is not None and _split_off:
        _LIVE_TERM = None
        _LIVE_TERM_REASON = "split-live disabled (SAIKAI_SPLIT_LIVE=0 / [display] split_live=false)"

    # Per-pane pyte scrollback depth drives the live process's memory (a full
    # 5000-line history was ~95 MB PER pane). Resolve env > config > default and
    # push it into the widget BEFORE any pane is created. Clamp to a sane band.
    if _LIVE_TERM is not None:
        _sb = _cfg("limits", "scrollback_lines", "SAIKAI_SCROLLBACK", 2000, int)
        _LIVE_TERM.SCROLLBACK_LINES = max(200, min(50000, _sb))

    # Emulate POSIX SIGHUP on Windows: if this tab's shell dies (tab closed)
    # while the picker is open or a resumed `claude` is running, take saikai and
    # its claude child down instead of orphaning the pair (see
    # _start_terminal_watchdog). No-op on POSIX / headless.
    _start_terminal_watchdog()

    all_sessions = list(sessions)

    # Send Textual's internal logs to a file so we have a trail to inspect
    # when something goes wrong inside the framework's event loop.
    os.environ.setdefault("TEXTUAL_LOG", str(CACHE_DIR / "textual-debug.log"))

    class SplitGrip(Static):
        """A 1-column draggable divider between the session list and the right
        pane. Mouse-down captures the pointer; subsequent moves resize the list
        (the pane is `1fr`, so it absorbs the remainder); mouse-up persists the
        ratio. can_focus stays False so it never steals keyboard focus."""
        _dragging = False

        def on_mouse_down(self, event) -> None:
            self._dragging = True
            self.capture_mouse()
            event.stop()

        def on_mouse_move(self, event) -> None:
            if self._dragging:
                self.app._drag_split(event.screen_x)
                event.stop()

        def on_mouse_up(self, event) -> None:
            if self._dragging:
                self._dragging = False
                self.release_mouse()
                self.app._commit_split_ratio()
                event.stop()

    class HelpScreen(ModalScreen):
        CSS = """
        HelpScreen { align: center middle; }
        #help-content {
            background: $panel;
            border: solid $accent;
            padding: 1 2;
            /* width:auto collapses a VerticalScroll to 0 in Textual 8.x (the box
               showed as a bare vertical bar with no content) — use a definite
               width like SettingsScreen does. */
            width: 92%;
            max-height: 90%;
        }
        """
        # max-width/height are RELATIVE so the modal fits a narrow / short terminal;
        # VerticalScroll scrolls when content exceeds max-height.
        BINDINGS = [
            Binding("escape", "dismiss", show=False),
            Binding("question_mark", "dismiss", show=False),
        ]

        def compose(self) -> ComposeResult:
            _cby = _cfg("display", "color_by", "SAIKAI_COLOR_BY", "project")
            if _cby not in ("project", "worktree", "topic", "none"):
                _cby = "project"
            body = (
                "[bold]Learn THREE things — the rest is on screen:[/bold]  "
                "keys you already know ([yellow]↑↓ ⏎ / Esc ?[/yellow])  ·  "
                "[yellow]␣[/yellow] = the menu (pause to see it)  ·  "
                "[yellow]Ctrl-][/yellow] = pane → list\n\n"
                "[bold cyan]Navigation[/bold cyan]\n"
                "  [yellow]↑[/yellow] [yellow]↓[/yellow]         Move rows\n"
                "  [yellow]Enter[/yellow]       Resume session\n"
                "  [yellow]/[/yellow]           Jump to search (or just start typing) · [yellow]␣/[/yellow] hides/shows the bar\n"
                "  [yellow]Esc[/yellow]         Leave the current context: search/dropdown → list · list → quit\n"
                "  [yellow]?[/yellow]           Help (this screen)\n\n"
                "[bold cyan]Session ops[/bold cyan]  [dim](␣x = Space then x; F-keys are the aliases)[/dim]\n"
                "  [yellow]␣f[/yellow] [dim]F6[/dim]     Toggle ★ favorite   ([dim]:fav[/dim] in search to filter)\n"
                "  [yellow]␣h[/yellow] [dim]F7[/dim]     Toggle hide/unhide  ([dim]:hidden[/dim] in search to find them)\n"
                "  [yellow]␣e[/yellow] [dim]⇧F2[/dim]    Rename — type your own name (empty clears → auto-title)\n"
                "  [yellow]␣y[/yellow] [dim]F9[/dim]     Copy this session's opening prompt\n"
                "  [yellow]␣d[/yellow] [dim]F8[/dim]     Show what this session changed (transcript diff)\n"
                "  [yellow]␣r[/yellow] [dim]F5[/dim]     Refresh list  (auto: SAIKAI_AUTO_REFRESH=secs)\n\n"
                "[bold cyan]View[/bold cyan]\n"
                "  [yellow]␣g[/yellow] [dim]⇧F7[/dim]    Cycle grouping: Date / Project / State / none\n"
                "  [yellow]␣s[/yellow] / [yellow]␣o[/yellow]    Cycle the sort column / flip its direction\n"
                "  [yellow]␣t[/yellow] [dim]⇧F5[/dim]    Tree (parent/child) mode\n"
                "  [yellow]␣,[/yellow]         Settings — list options + the resolved config\n"
                "  [yellow]Tab[/yellow]        Preview: full ↔ summary\n\n"
                "[bold cyan]Split-live (default · SAIKAI_SPLIT_LIVE=0 to disable)[/bold cyan]\n"
                "  [yellow]Enter[/yellow]      Open / focus the live claude pane\n"
                "  [yellow]␣n[/yellow] [dim]⇧F8[/dim]    New claude session in a folder / git worktree\n"
                "  [yellow]␣p[/yellow] [dim]⇧F4[/dim]    Reopen the panes from your last session (resume) — anytime\n"
                "  [yellow]␣\\[ ␣][/yellow] [dim]F2/F3[/dim] Prev / next live tab   ·   [yellow]␣a[/yellow] [dim]⇧F3[/dim]  Next pane needing attention (?/!)\n"
                "  [yellow]␣l[/yellow] [dim]F4[/dim]     Hide / show the session list\n"
                "  [yellow]Alt-←/→[/yellow]    Resize the list/pane split — or drag the divider (persists)\n"
                "  [yellow]Ctrl-][/yellow]     Return focus: pane → list  (SAIKAI_RELEASE_KEY to change)\n"
                "  [yellow]␣x[/yellow] [dim]F10[/dim]    Close the active tab   ·   [dim]⇧F10[/dim]  Close ALL tabs\n"
                "  [yellow]Esc[/yellow]        from the list: quit + snapshot panes (␣p reopens)\n"
                "  [yellow]Ctrl-C[/yellow]     interrupt claude in a focused pane; from saikai controls, quit-all\n"
                "  [yellow]␣z[/yellow] [dim]⇧F9[/dim]    Freeze the pane in place (copy mode): Shift+drag selects while\n"
                "             claude streams · scroll up also freezes · ␣z / typing resumes\n\n"
                "[bold cyan]Filter / Group / Sort (top-right dropdowns, Desktop-style)[/bold cyan]\n"
                "  Group by  Date / Project / State / None   (␣g cycles)\n"
                "  Sort by   Recency / Created time / Alphabetically\n"
                "  Status    Active / Archived / All\n"
                "  Age       last 1d / 3d / 7d / 30d / All time\n"
                "  Search    [yellow]/[/yellow] or type to open the bar; tokens AND with text + each other —\n"
                "            :fav  :hidden  :open  :active  :recent   (Esc clears)\n"
                "  Markers   @ open elsewhere · + active · . recent · live ~ busy · ? waiting · ! reply due · = idle · * fav · x hidden\n"
                "  [yellow]/[/yellow] shows the bar with the dropdowns; [yellow]Tab[/yellow]/[yellow]Shift-Tab[/yellow] walk into them, [yellow]Enter[/yellow]\n"
                "  opens one. Leader [yellow]s[/yellow]/[yellow]o[/yellow] cycles the sort column / direction without the bar\n"
                "  (a column-header click still sorts too)\n\n"
                f"[bold cyan]Colours[/bold cyan]  {_color_legend(_cby)} "
                "([dim]display.color_by = project/worktree/topic/none[/dim])\n\n")
            # Reflect live remaps + leader so the help can't drift from [keys] config.
            try:
                app = self.app
                _rm = getattr(app, "_applied_keymap", {}) or {}
                if _rm:
                    body += ("[bold cyan]Your remaps[/bold cyan]  " + " · ".join(
                        f"{a}→[yellow]{k}[/yellow]" for a, k in list(_rm.items())[:12]) + "\n")
                if getattr(app, "_leader_key", ""):
                    _lk = "Space" if app._leader_key == "space" else app._leader_key
                    body += (f"[bold cyan]Menu key[/bold cyan]  [yellow]{_lk}[/yellow] "
                             "in the list, then one letter (pause to see this map in place):\n")
                    _groups = _leader_groups(getattr(app, "_leader_actions", {}))
                    for _fam, _pairs in _groups:
                        _seq = "  ".join(_leader_hint_item(k, lbl)
                                        for k, lbl in _pairs)
                        body += f"  {_fam:<8} {_seq}\n"
                    if not _groups:
                        body += "  (no letters mapped)\n"
                    body += ("  [dim]([keys] in config: leader = \"none\" disables · "
                             "leader_defaults = false clears · any  action = \"x\"  remaps)[/dim]\n")
                if _rm or getattr(app, "_leader_key", ""):
                    body += "\n"
            except Exception:
                pass
            body += "[dim]Press ? or Esc to close · scroll for more[/dim]"
            with VerticalScroll(id="help-content"):
                yield Static(body)

    class SettingsScreen(ModalScreen):
        """␣, — Settings, hybrid by design. TOP: the list options saikai itself
        persists (Group / Sort / Status / Age / Tree) are editable in
        place and apply instantly — the Selects forward into the top-bar
        dropdowns, so there is exactly ONE apply/persist path. BOTTOM: the
        config.toml / env knobs, read-only with their resolved value + source
        (rewriting the TOML would destroy its comments — `e` opens the file in
        an editor instead; changes there apply on the next launch)."""
        CSS = """
        SettingsScreen { align: center middle; }
        #set-box { background: $panel; border: solid $accent; padding: 1 2;
                   width: 92; max-width: 95%; height: auto; max-height: 90%; }
        #set-rows { height: auto; }
        #set-rows Select { width: 20; }
        #set-toggles { height: 3; }
        #set-toggles Static { width: auto; padding: 1 1 0 2; }
        #set-config { height: auto; max-height: 18; }
        """
        BINDINGS = [
            Binding("escape", "dismiss", show=False),
            Binding("e", "edit_config", show=False, priority=True),
        ]

        def compose(self) -> ComposeResult:
            with Vertical(id="set-box"):
                yield Static("[bold cyan]Settings[/bold cyan]   [dim]top: applies "
                             "instantly · bottom: config.toml — [/dim][yellow]e[/yellow]"
                             "[dim] opens it (applies on restart) · Esc closes[/dim]\n")
                with Horizontal(id="set-rows"):
                    yield Select(
                        [("Date", "date"), ("Project", "project"),
                         ("State", "state"), ("None", "none")],
                        prompt="Group", id="set-group", value=_get_group_by(),
                    )
                    _kw = {"prompt": "Sort", "id": "set-sort"}
                    _sv = _sort_select_value()
                    if _sv is not None:
                        _kw["value"] = _sv
                    yield Select(
                        [("Recency", "last"), ("Created time", "date"),
                         ("Alphabetically", "title")],
                        **_kw,
                    )
                    yield Select(
                        [("Active", "active"), ("Archived", "archived"),
                         ("All", "all")],
                        prompt="Status", id="set-status",
                        value=_get_status_filter(),
                    )
                    yield Select(
                        [("All time", "0"), ("1d", "1"), ("3d", "3"),
                         ("7d", "7"), ("30d", "30")],
                        prompt="Age", id="set-age",
                        value=str(_get_lastact_days()),
                    )
                with Horizontal(id="set-toggles"):
                    yield Static("Tree")
                    yield Switch(value=_get_tree_mode(), id="set-tree")
                _cby = _cfg("display", "color_by", "SAIKAI_COLOR_BY", "project")
                yield Static(f"[dim]{_color_legend(_cby)}[/dim]")
                _p = _config_path()
                _state = ("exists" if _p.is_file()
                          else "absent — e creates it from the template")
                body = (f"[bold cyan]config.toml[/bold cyan]  [dim]{_p}  "
                        f"({_state})[/dim]\n")
                for sec, key, val, src in _resolved_settings():
                    _sc = {"env": "yellow", "config": "green"}.get(src, "dim")
                    body += (f"  [dim]\\[{sec}][/dim] {key:<22} = {val!r:<14} "
                             f"[{_sc}]({src})[/{_sc}]\n")
                with VerticalScroll(id="set-config"):
                    yield Static(body)

        def on_select_changed(self, event) -> None:
            # Forward into the matching TOP-BAR dropdown: its on_select_changed
            # is the one true apply/persist path (it guards same-value re-fires).
            tgt = {"set-group": "#groupsel", "set-sort": "#sortsel",
                   "set-status": "#statussel", "set-age": "#lastsel"}.get(
                       event.select.id or "")
            if not tgt or event.value in (None, False):   # False == Select.BLANK
                return
            try:
                self.app.query_one(tgt, Select).value = event.value
            except Exception:
                pass

        def on_switch_changed(self, event) -> None:
            try:
                if event.switch.id == "set-tree":
                    if _get_tree_mode() != bool(event.value):
                        self.app.action_toggle_tree()
            except Exception:
                pass

        def action_edit_config(self) -> None:
            """Open config.toml in an editor (create from the template first if
            absent). $VISUAL/$EDITOR run with the TUI suspended; otherwise fall
            back to the OS opener. Changes apply on the next saikai launch."""
            p = _config_path()
            if not p.is_file():
                try:
                    p.parent.mkdir(parents=True, exist_ok=True)
                    p.write_text(_CONFIG_TEMPLATE, encoding="utf-8")
                except Exception as e:
                    self.notify(f"could not create {p}: {e!r}", severity="error")
                    return
            ed = os.environ.get("VISUAL") or os.environ.get("EDITOR")
            try:
                if ed:
                    with self.app.suspend():
                        subprocess.run([*ed.split(), str(p)])
                elif sys.platform == "win32":
                    os.startfile(str(p))                      # noqa: S606
                else:
                    opener = "open" if sys.platform == "darwin" else "xdg-open"
                    subprocess.Popen([opener, str(p)],
                                     stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL)
                self.notify("config.toml changes apply on the next launch",
                            timeout=6)
            except Exception as e:
                self.notify(f"could not open an editor: {e!r}", severity="error")

    class NewSessionScreen(ModalScreen):
        """Pick a folder / git worktree, then start a FRESH claude session there.
        Type a path + Enter to launch it, or pick a worktree / recent dir from the
        list; Esc cancels. Returns the chosen path (or None) via dismiss()."""
        CSS = """
        NewSessionScreen { align: center middle; }
        #new-box { background: $panel; border: solid $accent; padding: 1 2;
                   width: 84; height: auto; max-height: 28; }
        #new-path { margin: 1 0; border: tall $accent; }
        #new-dirs { height: auto; max-height: 16; }
        """
        BINDINGS = [Binding("escape", "cancel", show=False)]

        def __init__(self, base_dir, candidates):
            super().__init__()
            self._base_dir = base_dir
            self._candidates = candidates           # list[(label, path)]

        def compose(self) -> ComposeResult:
            with Vertical(id="new-box"):
                yield Static("[bold cyan]New claude session[/bold cyan]   "
                             "[dim]type a folder + Enter, or pick a worktree / "
                             "recent dir below · Esc cancels[/dim]")
                yield Input(value=self._base_dir, placeholder="folder path",
                            id="new-path")
                if self._candidates:
                    yield OptionList(*[Option(lbl) for lbl, _p in self._candidates],
                                     id="new-dirs")

        def on_mount(self) -> None:
            try:
                self.query_one("#new-path", Input).focus()
            except Exception:
                pass

        def action_cancel(self) -> None:
            self.dismiss(None)

        def on_input_submitted(self, event) -> None:
            self.dismiss((event.value or "").strip() or None)

        def on_option_list_option_selected(self, event) -> None:
            try:
                self.dismiss(self._candidates[event.option_index][1])
            except Exception:
                self.dismiss(None)

    class RenameScreen(ModalScreen):
        """Type a custom name for the selected session (Shift+F2). Enter saves;
        an EMPTY value clears the custom name (reverts to the auto title); Esc
        cancels. Returns the typed string (possibly "") on submit, None on
        cancel — the caller distinguishes clear ("") from cancel (None)."""
        CSS = """
        RenameScreen { align: center middle; }
        #rename-box { background: $panel; border: solid $accent; padding: 1 2;
                      width: 72; height: auto; }
        #rename-input { margin: 1 0; border: tall $accent; }
        """
        BINDINGS = [Binding("escape", "cancel", show=False)]

        def __init__(self, current: str):
            super().__init__()
            self._current = current

        def compose(self) -> ComposeResult:
            with Vertical(id="rename-box"):
                yield Static("[bold cyan]Rename session[/bold cyan]   "
                             "[dim]Enter saves · empty clears (back to auto) · "
                             "Esc cancels[/dim]")
                yield Input(value=self._current, placeholder="custom name",
                            id="rename-input")

        def on_mount(self) -> None:
            try:
                self.query_one("#rename-input", Input).focus()
            except Exception:
                pass

        def action_cancel(self) -> None:
            self.dismiss(None)

        def on_input_submitted(self, event) -> None:
            self.dismiss(event.value or "")     # "" = clear; None only via cancel

    def _render_qr(matrix):
        """Render a QR bool-matrix as Rich Text using upper-half-block cells
        (fg paints the top module, bg the bottom), explicit black-on-white so it
        scans regardless of the terminal theme."""
        from rich.text import Text
        from rich.style import Style
        t = Text(no_wrap=True)
        for y in range(0, len(matrix), 2):
            top = matrix[y]
            bot = matrix[y + 1] if y + 1 < len(matrix) else [False] * len(top)
            for x in range(len(top)):
                fg = "black" if top[x] else "white"
                bg = "black" if bot[x] else "white"
                t.append("▀", Style(color=fg, bgcolor=bg))
            if y + 2 < len(matrix):
                t.append("\n")
        return t

    class MirrorScreen(ModalScreen):
        CSS = """
        MirrorScreen { align: center middle; }
        #mirror-box {
            background: $panel;
            border: solid $accent;
            padding: 1 2;
            /* definite width (auto collapses a scroll container to 0 in Textual
               8.x); 60 comfortably fits a QR (<=45 cells) + the wrapped URL. */
            width: 60;
            height: auto;
            max-width: 98%;
            max-height: 98%;
        }
        """
        BINDINGS = [
            Binding("escape", "dismiss", show=False),
            Binding("f12", "dismiss", show=False),
        ]

        def __init__(self, url, matrix, copied=True, clients=0):
            super().__init__()
            self._url = url
            self._matrix = matrix
            self._copied = copied
            self._clients = clients

        def compose(self) -> ComposeResult:
            with VerticalScroll(id="mirror-box"):
                _conn = (f" · [b]{self._clients}[/b] browser(s) connected"
                         if self._clients else " · no browser connected")
                yield Static("[bold]Web mirror — scan to connect[/bold] "
                             f"[dim](read-only){_conn}[/dim]")
                yield Static(_render_qr(self._matrix), id="mirror-qr")
                _tail = ("URL copied to clipboard" if self._copied
                         else "copy the URL above")
                yield Static(f"or open: [cyan]{self._url}[/cyan]\n"
                             f"[dim]{_tail} · Esc / F12 to close[/dim]")

    class PickerApp(App, _MirrorControl):
        TITLE = "saikai"
        # Textual's built-in command palette binds Ctrl+P. saikai leaves ordinary
        # editing keys to the search box / live claude (Ctrl+P = readline
        # previous-history), so disable the palette to keep Ctrl+P free.
        ENABLE_COMMAND_PALETTE = False
        BINDINGS = [
            Binding("escape", "quit", "Quit"),
            Binding("ctrl+c", "quit_all", show=False),
            # Resume only on Enter. We deliberately do NOT use RowSelected, so
            # a stray mouse click on a row never triggers resume — that was
            # the "screen disappears when I click around" symptom: the click
            # was meant for a header but landed on a row, posted RowSelected,
            # exited the app, and launched claude.
            # priority=True so Enter resumes regardless of which widget has
            # focus — without it, focus on the Search Input swallows Enter
            # into Input.Submitted and the picker never exits.
            Binding("enter", "resume", "Resume", priority=True),
            # ␣ Menu in the footer — THE one entry point to every command. The
            # on_key fast path arms the leader when the table is focused; this
            # (non-priority) binding catches the key when it bubbles unconsumed
            # from other non-input, non-dropdown widgets (Tabs, the grip…), so
            # "Space did nothing" can't happen there. A focused Input, Select,
            # or claude pane consumes space first, exactly as designed.
            Binding("space", "arm_leader", "Menu", key_display="␣"),
            # Session/pane actions live on FUNCTION KEYS. Ordinary readline
            # Ctrl+letters pass through; release and app-level quit handling are
            # deliberate exceptions.
            # Ctrl+W/K/R/D/Y/P/G/T/O/L/X are all readline editing keys the user
            # types constantly — and claude itself binds Ctrl+R (history search),
            # Ctrl+T (todos), Ctrl+L (clear). Stealing them broke editing in the
            # search box and inside live panes, and Ctrl+K once nuked every pane.
            # Claude Code binds NO F-keys (verified), so F5-F10 / Shift+F5-7 are
            # safe to capture (priority) even while a claude pane is focused; the
            # ordinary Ctrl+letters now pass straight through to Input / claude.
            # id= makes each remappable via [keys] in config (App.set_keymap in
            # on_mount). The id is the user-facing name typed in [keys]. quit /
            # quit_all / resume / preview have NO id (core nav, not remappable).
            # show=False on the F-keys: the footer stays at the four core keys
            # (⏎ Tab ? Esc — low learning load); the full set lives in ? help,
            # the leader hint, and the statusbar's "␣ leader · ? keys" crumb.
            Binding("f5", "refresh", "Refresh", id="refresh", show=False, priority=True),
            Binding("f6", "toggle_fav", "★", id="favorite", show=False, priority=True),
            Binding("f7", "toggle_hide", "Hide", id="hide", show=False, priority=True),
            Binding("f8", "preview_changes", "Changes", id="diff", show=False, priority=True),
            Binding("f9", "copy_prompt", "Copy", id="copy", show=False, priority=True),
            Binding("shift+f5", "toggle_tree", "Tree", id="tree", show=False, priority=True),
            Binding("shift+f7", "cycle_group", "Group", id="group", show=False, priority=True),
            Binding("shift+f8", "new_session", "New", id="new", show=False, priority=True),
            Binding("shift+f9", "freeze_pane", "Freeze", id="freeze", show=False, priority=True),
            Binding("shift+f4", "restore_panes", "Restore", id="restore", show=False, priority=True),
            Binding("tab", "toggle_preview", "Preview", priority=True),  # priority overrides Textual's default focus-cycling
            Binding("question_mark", "help", "Help", id="help", priority=True),
            # Split-live tab management (opt-in). F10 closes the ACTIVE tab;
            # Shift+F10 closes ALL — two keys apart so a single stray press can't
            # wipe every pane (that was the accidental "全件終了"). Esc from the
            # list snapshots + quits; Ctrl+] returns focus pane → list.
            Binding("f10", "close_live", "Close tab", id="close", show=False, priority=True),
            Binding("shift+f10", "close_all_live", "Close all", id="close_all", show=False, priority=True),
            Binding("f2", "prev_tab", "◀Tab", id="prev_tab", show=False, priority=True),
            Binding("f3", "next_tab", "Tab▶", id="next_tab", show=False, priority=True),
            Binding("shift+f3", "next_attention", "Next!", id="attention", show=False, priority=True),
            Binding("f4", "toggle_list", "Hide list", id="toggle_list", show=False, priority=True),
            Binding("shift+f2", "rename", "Rename", id="rename", show=False, priority=True),
            # Keyboard divider — footer-hidden (documented in ? help); the
            # actions SkipAction-forward when a pane / input is focused.
            Binding("alt+left", "shrink_list", "List◀", id="shrink_list",
                    show=False, priority=True),
            Binding("alt+right", "grow_list", "▶List", id="grow_list",
                    show=False, priority=True),
            Binding("f12", "mirror_info", "Mirror QR", id="mirror_info", show=False),
            # Phase B: toggle web-mirror INTERACTIVE control. priority=True so it
            # fires even while a live pane is focused (a leader letter would be
            # swallowed by the focused pane — unreachable exactly when control is
            # used). Default OFF; Shift+F12 because F12 is the QR. Local only —
            # never a browser button.
            Binding("shift+f12", "toggle_mirror_control", "Mirror control",
                    id="mirror_control", show=False, priority=True),
        ]
        # The practical limit on concurrent live claude panes is MEMORY — each
        # is a full node process tree that sits CPU-idle waiting for input — so
        # the real gate is a free-RAM check at spawn time (see
        # _open_or_attach_live), NOT a fixed count or core count. MAX_LIVE is
        # only a runaway backstop; set SAIKAI_MAX_LIVE for a stricter hard cap.
        MAX_LIVE = _cfg("limits", "max_live", "SAIKAI_MAX_LIVE", 64, int)
        CSS = """
        Screen { layout: vertical; }
        #searchrow { dock: top; height: 3; }   /* visible by default (the dropdowns ARE the discoverability); Space / toggles it and the last state persists */
        #search { width: 1fr; border: tall $accent; }
        #groupsel { width: 15; }
        #sortsel { width: 17; }
        #statussel { width: 14; }
        #lastsel { width: 12; }
        #statusbar { height: 1; background: $surface; color: $warning; }
        #main { layout: horizontal; height: 1fr; }
        #table { width: 60%; }                /* default; inline style overrides on mount/drag */
        #main.split #table { width: 34%; }    /* split-live: give the live pane the room */
        /* #grip is the draggable divider; #right is 1fr so it absorbs the rest. */
        #grip { width: 1; background: $panel; }
        #grip:hover { background: $accent; }
        #right { width: 1fr; }
        .right { width: 1fr; }
        /* F4 hides the session list (+ its grip) so the pane is full-width;
           #right is 1fr → it fills 100% automatically once the list is gone. */
        #main.nolist #table { display: none; }
        #main.nolist #grip { display: none; }
        #preview { padding: 0 1; height: 1fr; }
        AgentTerminal { width: 1fr; height: 1fr; }
        """

        preview_mode = "summary"   # "summary" or "full"
        _sid_index: dict = {}      # sid -> session; populated in on_mount
        _na_cache: dict = {}       # sid -> (mtime, needs_attention); Group-by-State
        _last_status: dict = {}    # sid -> last live status; for waiting toasts

        def compose(self) -> ComposeResult:
            with Horizontal(id="searchrow"):
                yield Input(placeholder="Search title / msg / SID / proj    "
                                        "•  :fav  :hidden  :open  :active  :recent",
                            id="search")
                # Initialise each dropdown to the persisted selection so the box
                # shows what is actually applied (the choices ARE remembered on
                # disk + applied at startup; without value= the box just showed
                # the generic prompt, which read as "not remembered"). OMIT value=
                # when there is no representable selection — Select.BLANK is
                # literally `False` in Textual 8.2.7 and passing it raises
                # InvalidSelectValueError on mount (would crash launch whenever the
                # saved sort leads with a non-dropdown column, e.g. a header-click
                # sort by turns). Group/Status getters are always valid options.
                yield Select(
                    [("Date", "date"), ("Project", "project"),
                     ("State", "state"), ("None", "none")],
                    prompt="Group", id="groupsel", value=_get_group_by(),
                )
                _sort_kw = {"prompt": "Sort", "id": "sortsel"}
                _sv = _sort_select_value()
                if _sv is not None:
                    _sort_kw["value"] = _sv
                yield Select(
                    [("Recency", "last"), ("Created time", "date"),
                     ("Alphabetically", "title")],
                    **_sort_kw,
                )
                yield Select(
                    [("Active", "active"), ("Archived", "archived"), ("All", "all")],
                    prompt="Status", id="statussel", value=_get_status_filter(),
                )
                _last_kw = {"prompt": "Age", "id": "lastsel"}
                _lv = str(_get_lastact_days())
                if _lv in ("0", "1", "3", "7", "30"):
                    _last_kw["value"] = _lv
                yield Select(
                    [("All time", "0"), ("1d", "1"), ("3d", "3"),
                     ("7d", "7"), ("30d", "30")],
                    **_last_kw,
                )
            yield Static("", id="statusbar")
            with Horizontal(id="main", classes=("split" if _LIVE_TERM is not None else "")):
                yield DataTable(cursor_type="row", zebra_stripes=True, id="table")
                yield SplitGrip("", id="grip")   # draggable list/pane divider
                if _LIVE_TERM is not None:
                    # Split-live: the preview becomes the first tab; live claude
                    # panes are appended as TabPanes on Enter.
                    with TabbedContent(id="right", initial="tab-preview"):
                        with TabPane("Preview", id="tab-preview"):
                            yield RichLog(id="preview", wrap=True,
                                          highlight=False, markup=False)
                else:
                    # Legacy / graceful-fallback layout: bare preview pane.
                    # The RichLog keeps id="preview" (so _update_preview's
                    # query is identical in both layouts) and ALSO carries the
                    # .right class so it sizes as the 1fr pane beside the grip.
                    yield RichLog(id="preview", classes="right", wrap=True,
                                  highlight=False, markup=False)
            yield Footer()

        def on_mount(self) -> None:
            # sid -> session map so the preview pane can warm its own cache on
            # demand: rendered and cached on a cache miss.
            self._sid_index = {s.get("id"): s for s in all_sessions}
            self._marked: set = set()        # sids selected for batch launch (Space)
            self._opening_live_sid = None     # sid whose pane should grab focus on open
            self._unread: set = set()         # live panes finished (idle) but not yet responded to → ! marker
            self._busy_seen: set = set()      # sids observed busy since their last "done" toast (catches tasks shorter than the poll)
            self._opened_sids: set = set()    # sids opened + kept this session (snapshot source)
            self._opening_sids: set = set()   # opens in flight (register deferred to the mount worker) — counted by the capacity gate + has() dedup
            self._last_cursor_row = -1        # prior cursor row → header-skip direction
            self._mem_pressure_warned = False # memory-pressure toast: once per crossing
            # Restore the persisted list/pane divider position (drag → options.json).
            self._split_ratio = _get_split_ratio()
            self._apply_split_ratio(self._split_ratio)
            # Search/filter bar: VISIBLE by default — the Group/Sort/Status/Age
            # dropdowns living in it are the features' discoverability; hiding
            # them until '/' meant nobody found grouping. Space / toggles the
            # bar and that choice persists (options.json) for the next launch.
            if _load_options().get("search_bar") is False:
                try:
                    self.query_one("#searchrow").display = False
                except Exception:
                    pass
            # Keybindings from [keys]: F-key/combo values are DIRECT rebinds
            # (set_keymap); single-letter values are LEADER sequences. The leader is
            # ON BY DEFAULT (Space + DEFAULT_LEADER_LETTERS). The table fast path
            # and App binding allow it only in non-input, non-dropdown saikai
            # controls, so it cannot steal Space from a claude pane or input.
            # [keys] leader = "none" disables it.
            self._leader_key = ""          # resolved leader key; "" = off
            self._leader_actions = {}      # {letter: action_name} reached via the leader
            self._leader_pending = False   # waiting for the post-leader key
            self._applied_keymap = {}      # direct rebinds applied (shown in ? help)
            try:
                _kc = _load_config().get("keys", {})
                _kc = _kc if isinstance(_kc, dict) else {}
                _ids = {b.id for b in type(self).BINDINGS if getattr(b, "id", None)}
                _id2act = {b.id: b.action for b in type(self).BINDINGS
                           if getattr(b, "id", None)}
                _direct = {k: v for k, v in _kc.items()
                           if k not in ("leader", "leader_defaults", "release")
                           and len(str(v).strip()) != 1}
                _applied, _errs = _validate_keymap(_direct, _ids)
                if _applied:
                    self.set_keymap(_applied)
                    self._applied_keymap = _applied
                self._leader_key, self._leader_actions, _lerr = (
                    _resolve_leader(_kc, _id2act))
                _errs += _lerr
                for _e in _errs[:5]:
                    self.notify(_e, severity="warning", timeout=8)
            except Exception:
                pass
            # Previous session's open panes — for the Shift+F4 restore (split-live).
            self._restore_candidates = ((_read_json(OPEN_PANES_FILE, []) or [])
                                        if _LIVE_TERM is not None else [])
            # Live-terminal bookkeeping (None-safe: only used when _LIVE_TERM is
            # available). Pure data structure; the TabbedContent is the UI.
            self._live = (_LIVE_TERM.LiveSessionManager(max_live=self.MAX_LIVE)
                          if _LIVE_TERM is not None else None)
            self._refresh_table()
            self.query_one("#table", DataTable).focus()
            # Optional auto-refresh: SAIKAI_AUTO_REFRESH=<seconds> re-scans disk on
            # an interval so sessions started elsewhere appear without F5.
            _ar = _cfg("display", "auto_refresh", "SAIKAI_AUTO_REFRESH", "", str)
            if _ar:
                try:
                    _secs = float(_ar)
                    if _secs >= 2:
                        self.set_interval(_secs, self._auto_tick)
                except Exception:
                    pass
            # Poll live-pane status so a backgrounded pane that starts WAITING
            # for input raises a toast, and the list markers stay live.
            if self._live is not None:
                self.set_interval(1.5, self._poll_live_status)
                if self._restore_candidates:
                    self.notify(f"{len(self._restore_candidates)} pane(s) from last "
                                f"session — Shift+F4 to reopen", timeout=8)
            # One-time hint: AI summaries are opt-in (claude -p spends credits).
            # Show once, only when there's no config file and summaries are off.
            try:
                _hint = CACHE_DIR / ".hinted-summary"
                if (not _summary_enabled() and not _config_path().is_file()
                        and not _hint.exists()):
                    self.notify(
                        "AI summaries are off (they call `claude -p`, spending "
                        "credits). Enable with `saikai --init-config` → set "
                        "[summary] enabled = true, or SAIKAI_SUMMARIZE_ENABLED=1.",
                        title="saikai", timeout=10)
                    _hint.parent.mkdir(parents=True, exist_ok=True)
                    _hint.write_text("", encoding="utf-8")
            except Exception:
                pass
            # Pre-warm preview caches off the UI thread so scrolling stays
            # responsive; _update_preview's on-demand warm is the fallback for
            # rows this thread hasn't reached. Open sessions render fresh, skip.
            import threading as _thr
            _thr.Thread(target=self._prewarm_previews, daemon=True).start()
            # Build the cross-session forest OFF the pre-paint path (it only feeds
            # tree display + the related-header). parent_id was pre-set to None so
            # the flat list is correct meanwhile; this repaints once when done.
            if len(all_sessions) <= 1000 and not getattr(self, "_forest_built", False):
                _thr.Thread(target=self._build_forest_bg, daemon=True).start()
            # If background summarization is running, start a watcher thread
            # that refreshes the table when it finishes.
            bg = _bg_summarize.get("thread")
            if bg and bg.is_alive():
                pending = _bg_summarize.get("pending", 0)
                self.notify(
                    f"Summarizing {pending} new sessions in background …",
                    timeout=8,
                )
                import threading as _thr
                _thr.Thread(target=self._join_bg_summarize, daemon=True).start()
            _hub = getattr(self, "_mirror_hub", None)
            if _hub is not None:
                _hub.set_size(self.size.width, self.size.height)
                _hub.set_repaint_request(
                    lambda: self.call_from_thread(self.refresh, layout=True))
                # Phase B: deliver browser input to the focused pane. The handler
                # is _marshal-shaped — capture the app, bail if it's gone, marshal
                # onto the UI thread, and swallow shutdown errors. NEVER a bare
                # call_from_thread (whose future.result() could block the
                # input-drain/HTTP thread forever during teardown).
                _app_ref = self
                def _inject_handler(d, _app=_app_ref):
                    if not getattr(_app, "is_running", False):
                        return
                    try:
                        _app.call_from_thread(_app._mirror_inject_input, d)
                    except Exception:
                        pass            # app tearing down between the guard + call
                _hub.set_input_handler(_inject_handler)
                # Phase C: deliver browser taps + key-bar presses into the App as
                # synthesized Textual events. Same _marshal shape as the input
                # handler — capture the app, bail if it's gone, marshal onto the
                # UI thread, swallow shutdown errors. NEVER a bare call_from_thread
                # (whose future.result() could block the inject-drain thread).
                def _mouse_handler(col, row, button, kind, _app=_app_ref):
                    if not getattr(_app, "is_running", False):
                        return
                    try:
                        _app.call_from_thread(
                            _app._mirror_inject_mouse, col, row, button, kind)
                    except Exception:
                        pass
                _hub.set_mouse_handler(_mouse_handler)

                def _key_handler(key, _app=_app_ref):
                    if not getattr(_app, "is_running", False):
                        return
                    try:
                        _app.call_from_thread(_app._mirror_inject_key, key)
                    except Exception:
                        pass
                _hub.set_key_handler(_key_handler)

                def _client_change(n, _app=_app_ref):
                    if not getattr(_app, "is_running", False):
                        return
                    try:
                        _app.call_from_thread(_app._mirror_clients_changed, n)
                    except Exception:
                        pass
                _hub.set_client_change_handler(_client_change)
                # Show the QR so a phone can join without typing the tokened URL
                # (the stderr banner is alt-screen hidden). action_mirror_info also
                # copies the URL to the host clipboard, every time — F12 re-opens it.
                self.call_after_refresh(self.action_mirror_info)

        def _build_forest_bg(self) -> None:
            """Daemon: compute the cross-session forest off the pre-paint path,
            then repaint once. Pure CPU (no subprocess / pty / lock), so an
            abandoned run at exit leaks nothing. parent_id was pre-initialised to
            None, so tree display + the related-header just fill in when this
            lands; reads of a half-assigned forest are GIL-atomic (None or a valid
            id), never torn."""
            try:
                _build_forest(all_sessions)
                self._forest_built = True
            except Exception:
                return
            try:
                if getattr(self, "is_running", True):
                    self.call_from_thread(self._refresh_table)
            except Exception:
                pass

        def _prewarm_previews(self) -> None:
            """Daemon thread: render + cache previews for all non-open sessions
            so cursor movement doesn't trigger a synchronous render on the UI
            thread. mtime-gated, so already-cached sessions are skipped cheaply."""
            # Cap the cold-start burst to the most-recent N (all_sessions is
            # sorted recent-first); the rest warm on demand via _update_preview.
            # Bail if the app stopped, so we don't churn disk after quit.
            for s in all_sessions[:200]:
                if not getattr(self, "is_running", True):
                    break
                if not s.get("is_open"):
                    try:
                        _write_preview_cache(s)
                    except Exception:
                        pass

        def _join_bg_summarize(self) -> None:
            """Worker thread: waits for bg summarization, refreshes the table, and
            reports HONESTLY — if Haiku/network failed, every session quietly fell
            back to its first message, so don't claim "ready"."""
            thread = _bg_summarize.get("thread")
            if thread:
                thread.join()
            if not getattr(self, "is_running", True):
                return   # app quit while summarizing — don't marshal into a dead App
            ok = _bg_summarize.get("succeeded", 0)
            attempted = _bg_summarize.get("attempted", _bg_summarize.get("pending", 0))
            if attempted and ok == 0:
                msg, sev = (f"summaries unavailable ({attempted}) — Haiku/network "
                            "error; showing first messages", "warning")
            elif attempted:
                msg, sev = (f"summaries ready ({ok}/{attempted})", "information")
            else:
                msg, sev = ("summaries ready", "information")
            try:
                self.call_from_thread(self._refresh_table)
                self.call_from_thread(
                    lambda: self.notify(msg, severity=sev, timeout=4)
                )
            except Exception:
                pass

        # Status-filter prefixes: typed alongside text in the search input.
        # `:fav python` = favorites whose searchable text matches "python".
        # `:hidden` alone surfaces sessions normally skipped by default view.
        _STATUS_TOKENS = {":fav", ":hidden", ":open", ":active", ":recent"}

        def _parse_query(self, q: str) -> tuple[set[str], str]:
            tokens = q.strip().lower().split()
            statuses = {t for t in tokens if t in self._STATUS_TOKENS}
            # A word that LOOKS like a filter (:foo) but isn't a real one is almost
            # certainly a typo (:favorite, :recnt). Stash it so a zero-match result
            # can name it; it still falls through to a literal text search (which is
            # what produces the zero match that surfaces the hint).
            self._unknown_tokens = [t for t in tokens
                                    if t.startswith(":") and t not in self._STATUS_TOKENS]
            text = " ".join(t for t in tokens if t not in statuses)
            return statuses, text

        def _filter(self, q: str) -> list[dict]:
            statuses, text = self._parse_query(q)
            if not statuses and not text:
                return all_sessions
            now_ts = time.time()   # recompute recency NOW, not from the load snapshot
            favs = _load_favorites() if ":fav" in statuses else set()
            hidden = _load_hidden() if ":hidden" in statuses else set()

            def keep(s: dict) -> bool:
                sid = s["id"]
                if ":fav" in statuses and sid not in favs:
                    return False
                if ":hidden" in statuses and sid not in hidden:
                    return False
                if ":open" in statuses and not s.get("is_open"):
                    return False
                if ":active" in statuses and not _is_active_now(s, now_ts):
                    return False
                if ":recent" in statuses and not _is_recent_now(s, now_ts):
                    return False
                if text:
                    return (text in (s.get("custom_title") or "").lower()
                            or text in (s.get("ai_title") or "").lower()
                            or text in " ".join(s.get("real_msgs") or []).lower()
                            or text in sid
                            or text in (s.get("project_name") or "").lower()
                            or text in (s.get("worktree_label") or "").lower())
                return True

            return [s for s in all_sessions if keep(s)]

        def _refresh_table(self) -> None:
            # Catch-all so a row-build exception (bad session dict, Textual
            # rendering quirk, etc.) shows up as a toast instead of crashing
            # the app and dumping the user back to the shell ("the screen
            # disappears when I click around" bug).
            try:
                self._do_refresh_table()
            except Exception as e:
                _log(f"refresh error: {e!r}")
                import traceback
                self.notify(
                    f"refresh failed: {e!r}\n{traceback.format_exc()[-400:]}",
                    severity="error", title="saikai", timeout=15,
                )

        def _do_refresh_table(self) -> None:
            table = self.query_one("#table", DataTable)
            saved_cursor = table.cursor_row
            saved_sid = self._cursor_sid()   # restore by SESSION, not row index (headers/grouping shift it)
            table.clear(columns=True)

            # Read state first; layout mode decides whether the Topic column
            # is added, so we need to know it before defining columns.
            query = self.query_one("#search", Input).value
            visible = list(self._filter(query))   # copy: _filter may return the shared all_sessions; we sort/tag it in place
            # Re-stamp recency from the CURRENT time so the +/. markers and the
            # State "Recent" bucket agree with the :active / :recent search
            # tokens (which recompute live). The load-time is_active/is_recent
            # snapshot otherwise drifts as the picker stays open, so a row shows
            # "+"/"." that the :active/:recent filter would drop.
            _now_recency = time.time()
            for _s in visible:
                _s["is_active"] = _is_active_now(_s, _now_recency)
                _s["is_recent"] = _is_recent_now(_s, _now_recency)
            # Claude-Desktop 'Last activity' window: drop rows older than N days.
            hidden = _load_hidden()
            favorites = _load_favorites()
            _lastact = _get_lastact_days()
            if _lastact:
                _cut = datetime.now() - timedelta(days=_lastact)
                # Pinned favorites are exempt: Claude Desktop keeps pinned items
                # regardless of the activity window, so narrowing Age must not make
                # a user-pinned session vanish from the Pinned section.
                visible = [s for s in visible
                           if s["id"] in favorites
                           or (_last_active_dt(s) or datetime.min) >= _cut]
            view_mode = _get_view_mode()
            tree_mode = _get_tree_mode() and len(all_sessions) <= 1000
            group_by = _get_group_by()
            # Tree is its own layout and takes precedence; otherwise apply the
            # Claude-Desktop-style grouping (Pinned + date/project/state).
            grouping = "none" if tree_mode else group_by
            show_proj_col = show_project or (grouping == "project")
            # The split-live list pane is narrow (~34%): use a Desktop-style
            # minimal column set (status + relative-Last + title) and convey the
            # project by tinting the title instead of a wide Project column.
            narrow = _LIVE_TERM is not None
            tree_prefixes: dict[str, str] = {}

            _assign_primary_topic(visible)
            if not tree_mode:
                # Flat and grouped layouts honour the live Sort spec (tree is
                # structural and walks itself). visible is a COPY, so this never
                # mutates all_sessions; _build_groups only partitions while
                # preserving this order.
                # (Flat previously relied on main()'s one-time sort, so changing the
                # Sort dropdown re-ordered nothing until the next launch.)
                _apply_sort(visible, _load_sort())
            if tree_mode:
                walked = _tree_walk(visible)
                tree_prefixes = {s["id"]: p for s, p in walked}
                visible = [s for s, _ in walked]
            # else (flat): already ordered by _apply_sort above.

            # Column labels carry a sort indicator (priority + direction arrow)
            # so a glance at the header tells the user the current sort spec.
            sort_keys = _load_sort()
            def col_label(col_key: str, base: str) -> str:
                for i, k in enumerate(sort_keys, 1):
                    if k["col"] == col_key:
                        arrow = "v" if k["dir"] == "desc" else "^"
                        return f"{base} {i}{arrow}"
                return base

            # Column definitions. Fixed widths (Textual auto-width was producing
            # per-row widths in our cell mix). The ID column is intentionally
            # NOT included — the SID is already visible in the preview pane
            # and the row's RowKey carries it for resume; the user said it's
            # not useful as a sort/search target.
            # "Wt" column: only when --here loaded sessions from >1 worktree.
            if narrow:
                # Minimal list for the split pane: status + relative-Last + title.
                has_worktrees = False
                specs: list[tuple[str, str, int]] = [
                    ("", "_marker", 3),
                    (col_label("last", "Last"), "last", 6),
                    (col_label("title", "Title"), "title", 80),
                ]
            else:
                # "Wt" column: only when --here loaded sessions from >1 worktree.
                has_worktrees = (not show_project
                                 and any(s.get("worktree_label") for s in visible))
                specs = [
                    ("", "_marker", 3),
                    (col_label("date", "Start"), "date", 13),
                    (col_label("last", "Last"), "last", 7),
                ]
                if show_proj_col:
                    specs.append((col_label("proj", "Project"), "proj", 17))
                if has_worktrees:
                    specs.append((col_label("wt", "Wt"), "wt", 12))
                specs.append((col_label("title", "Title"), "title", 80))
            for label, key, width in specs:
                table.add_column(label, key=key, width=width)

            # Precompute one colour-mapping per column so a given project /
            # topic / worktree gets the same colour everywhere it appears.
            project_color: dict[str, str] = {}
            wt_color: dict[str, str] = {}
            if show_proj_col or narrow:
                project_color = _build_color_map(
                    (project_short(s.get("project_name") or "") for s in visible),
                    _PROJECT_PALETTE,
                )
            if has_worktrees:
                wt_color = _build_color_map(
                    (s.get("worktree_label") or "" for s in visible
                     if s.get("worktree_label")),
                    _PROJECT_PALETTE,
                )
            # Title hue follows [display] color_by (project | worktree | topic | none).
            _color_by = _cfg("display", "color_by", "SAIKAI_COLOR_BY", "project")
            if _color_by not in ("project", "worktree", "topic", "none"):
                _color_by = "project"
            if _color_by == "none":
                _title_color: dict[str, str] = {}
            else:
                _title_color = _build_color_map(
                    (_color_key_for(s, _color_by) for s in visible),
                    _TOPIC_PALETTE if _color_by == "topic" else _PROJECT_PALETTE,
                )

            # `:hidden` in the query is an explicit request for hidden rows,
            # so bypass the default-view auto-skip — otherwise the filter
            # would match them but the renderer would drop every one.
            show_hidden = (view_mode == "show-hidden"
                           or ":hidden" in self._parse_query(query)[0])
            # Claude-Desktop 'Status' filter: archived/all reveal hidden rows.
            _status = _get_status_filter()
            if _status in ("archived", "all"):
                show_hidden = True

            # Group-by-State needs a per-session state tag (live status +
            # needs-attention heuristic + open/active flags); compute it here
            # where self._live and the transcript are reachable.
            if grouping == "state":
                for _s in visible:
                    _live = (self._live.status(_s["id"])
                             if self._live is not None else "")
                    if _s["id"] in hidden:
                        _s["_state"] = "Archived"
                    elif _live == "busy":
                        _s["_state"] = "Running"
                    elif _live == "waiting":
                        _s["_state"] = "Needs input"
                    elif _s.get("is_open") or _live == "idle":
                        # Running now (live pane / open elsewhere): state is known
                        # and its JSONL is GROWING — skip the needs-attention
                        # tail-read, which would defeat the mtime cache every
                        # refresh (resource #6).
                        _s["_state"] = "Open"
                    elif _needs_attention(_s, self._na_cache):
                        _s["_state"] = "Needs input"   # idle session: stable mtime -> cached
                    elif _s.get("is_active") or _s.get("is_recent"):
                        _s["_state"] = "Recent"
                    else:
                        _s["_state"] = "Idle"
            # Claude-Desktop-style sections: partition the (already sorted) rows
            # into Pinned + date/project/state groups, then remember which row
            # each section header should precede. grouping='none' -> no headers.
            groups = (_build_groups(visible, grouping, set(favorites), datetime.now())
                      if grouping != "none" else [(None, visible)])
            header_before: dict[str, str] = {}
            flat: list[dict] = []
            for _hdr, _members in groups:
                _vis = [m for m in _members
                        if not (m["id"] in hidden and not show_hidden)
                        and not (_status == "archived" and m["id"] not in hidden)]
                if not _vis:
                    continue
                if _hdr is not None:
                    header_before[_vis[0]["id"]] = f"{_hdr} ({len(_vis)})"
                flat.extend(_vis)
            visible = flat

            n = 0
            n_sessions = 0
            first_session_row = None   # row index of the first real session (cursor-off-header)
            self._header_labels = {}
            for s in visible:
                # Emit a section-header row just before its first member.
                if s["id"] in header_before:
                    hdr_cells = ["" for _ in specs]
                    # Put the section label where there's room: the Title column
                    # in the narrow pane, else the (wider) Start column.
                    hdr_idx = (len(specs) - 1) if narrow else 1
                    hdr_cells[hdr_idx] = Text(header_before[s["id"]],
                                              style="bold #7aa2f7")
                    self._header_labels[f"__hdr__{n}"] = header_before[s["id"]]
                    table.add_row(*hdr_cells, key=f"__hdr__{n}")
                    n += 1
                is_hidden = s["id"] in hidden
                if is_hidden and not show_hidden:
                    continue
                # A live (saikai-hosted) pane's status takes precedence in the
                # marker so a backgrounded session needing input is loud in the
                # list: ? = waiting, ~ = busy, = = idle-but-live. Falls back to
                # the file-registry open/active/recent markers otherwise.
                live_status = (self._live.status(s["id"])
                               if self._live is not None else "")
                if live_status == "waiting":
                    marker_a = "?"
                elif live_status == "busy":
                    marker_a = "~"
                elif live_status == "idle":
                    # ! = claude finished and the user has not responded yet;
                    # = = idle live pane with no response due. Merely viewing a tab
                    # does not clear !. ASCII keeps the marker column aligned.
                    marker_a = "!" if s["id"] in getattr(self, "_unread", ()) else "="
                else:
                    marker_a = ("@" if s.get("is_open") else "+" if s.get("is_active")
                                else "." if s.get("is_recent") else " ")
                marker_s = ("*" if s["id"] in favorites
                            else "x" if is_hidden else " ")
                marker = f"{marker_a}{marker_s}"
                # Plain title; collapse any newline/tab so a multi-line ai_title
                # doesn't push the row to multiple terminal lines. _list_title uses
                # claude's own ai-title / first msg / project — never `claude -p` —
                # so a just-opened session shows the project, not a blank cell.
                raw_title = _list_title(s)[:80]
                raw_title = (raw_title.replace("\n", " ")
                                       .replace("\r", " ")
                                       .replace("\t", " "))
                if tree_mode and tree_prefixes.get(s["id"]):
                    # Strip ANSI from the tree prefix so the cell stays a plain
                    # str of consistent width.
                    raw_title = _ANSI_RE.sub("", tree_prefixes[s["id"]]) + raw_title
                if s["id"] in getattr(self, "_marked", ()):
                    raw_title = "▣ " + raw_title       # batch-launch selection (Space)
                _tstyle = _title_color.get(_color_key_for(s, _color_by), "")  # [display] color_by
                if narrow:
                    # marker · relative-Last · title (title tinted per color_by).
                    row = [marker, fmt_last_active(s), Text(raw_title, style=_tstyle)]
                    table.add_row(*row, key=s["id"])
                    if first_session_row is None:
                        first_session_row = n
                    n += 1
                    n_sessions += 1
                    continue
                row = [marker, fmt_ts(s["first_ts"]), fmt_last_active(s)]
                if show_proj_col:
                    proj_txt = project_short(s.get("project_name") or "")
                    row.append(Text(proj_txt, style=project_color.get(proj_txt, "")))
                if has_worktrees:
                    wt = s.get("worktree_label") or ""
                    row.append(Text(wt[:11], style=wt_color.get(wt, "") if wt else ""))
                row.append(Text(raw_title, style=_tstyle) if _tstyle else raw_title)
                table.add_row(*row, key=s["id"])
                if first_session_row is None:
                    first_session_row = n
                n += 1
                n_sessions += 1
            self._n_sessions = n_sessions
            # Restore the cursor onto the SAME session (its row index shifts when
            # grouping/filtering/headers change); fall back to the old clamp.
            restored = False
            if saved_sid:
                try:
                    table.move_cursor(row=table.get_row_index(saved_sid))
                    restored = True
                except Exception:
                    restored = False
            if not restored and n and 0 <= saved_cursor < n:
                try:
                    table.move_cursor(row=saved_cursor)
                except Exception:
                    pass
            if n_sessions and first_session_row is not None and self._cursor_sid() is None:
                # The restore (or the default row 0) landed on a section-header
                # row, which has no session → preview/Enter would act on nothing.
                # Nudge down to the first real session.
                try:
                    table.move_cursor(row=first_session_row)
                except Exception:
                    pass
            elif n_sessions == 0:
                # No session rows. A blank TABLE reads as "saikai broke", so add a
                # non-selectable placeholder ROW (keyed __hdr__ → _cursor_sid /
                # Enter / highlight all skip it) explaining WHY it's empty, and
                # clear the preview (which would otherwise keep the last session's
                # content and imply a match). Distinguish "filtered to zero" from
                # "no sessions exist at all".
                if not all_sessions:
                    msg = f"No sessions found under {PROJECTS_ROOT}"
                elif getattr(self, "_unknown_tokens", None):
                    msg = ("Unknown filter " + " ".join(self._unknown_tokens)
                           + " — valid: :fav :hidden :open :active :recent")
                else:
                    msg = "No sessions match — press Esc to clear the search, or widen Status / Age"
                try:
                    ph = ["" for _ in specs]
                    ph[(len(specs) - 1) if narrow else 1] = Text(msg, style="dim italic")
                    self._header_labels["__hdr__empty"] = msg
                    table.add_row(*ph, key="__hdr__empty")
                except Exception:
                    pass
                try:
                    pv = self.query_one("#preview", RichLog)
                    pv.clear()
                    pv.write(msg)
                except Exception:
                    pass
            self._update_subtitle()

        def _update_subtitle(self) -> None:
            table = self.query_one("#table", DataTable)
            # Section-header rows inflate row_count; use the tracked session count.
            n = getattr(self, "_n_sessions", table.row_count)

            # Sort: show first active sort key
            _COL_LABEL = {
                "date": "Start", "last": "Last", "title": "Title",
                "proj": "Proj", "topic": "Topic", "turns": "Turns", "fav": "Fav",
            }
            sort_keys = _load_sort()
            first = next((k for k in sort_keys if k["col"] != "-"), None)
            if first:
                arrow = "↓" if first["dir"] == "desc" else "↑"
                col_display = _COL_LABEL.get(first["col"], first["col"].capitalize())
                sort_str = f"Sort: {col_display}{arrow}"
            else:
                sort_str = "Sort: default"

            # Scope: "All projects" when --all-projects, else repo name
            scope = "All projects" if show_project else (repo.name if repo else "All projects")

            sep = "  [dim]·[/dim]  "
            # Show Tree only when ON (a row of OFFs is noise); Group stays
            # visible — dimmed when off — so the feature advertises itself.
            tree_str = f"{sep}Tree: [green]ON[/green]" if _get_tree_mode() else ""
            _GROUP_LABEL = {"date": "[green]Date[/green]",
                            "project": "[green]Project[/green]",
                            "state": "[green]State[/green]"}
            group_str = ("Group: " + _GROUP_LABEL[_get_group_by()]
                         if _get_group_by() in _GROUP_LABEL
                         else "[dim]Group: off[/dim]")
            # Active Desktop-style filters (only shown when non-default).
            _filt = []
            if _get_status_filter() != "active":
                _filt.append(_get_status_filter().capitalize())
            _la = _get_lastact_days()
            if _la:
                _filt.append(f"{_la}d")
            filt_str = (f"{sep}Filter: [yellow]" + "+".join(_filt) + "[/yellow]") if _filt else ""
            # Live-pane capacity (split-live only). The real limit is MEMORY, not
            # the MAX_LIVE backstop (each claude is a RAM-heavy node tree), so show
            # how many MORE fit in the free RAM above the floor — green while room
            # remains, red when RAM is at/below the floor. (~ because per-claude
            # RAM is an estimate; tune with SAIKAI_CLAUDE_MB.)
            live_str = ""
            if self._live is not None:
                cnt = self._live.count
                # At-a-glance attention counts so "something needs me" is visible
                # without scanning every row/tab: ?N = panes waiting (needs input),
                # !M = panes that finished and you haven't responded to yet.
                _st = self._live.statuses()
                _wait = sum(1 for v in _st.values() if v == "waiting")
                # Intersect with live sids so a just-closed/dead pane (forgotten from
                # statuses, but cleared from _unread only later by the reader's exit
                # callback) doesn't inflate the count — matches action_next_attention.
                _done = len(getattr(self, "_unread", set()) & set(_st))
                _att = ""
                if _wait:
                    _att += f"  [red]?{_wait}[/red]"
                if _done:
                    _att += f"  [yellow]!{_done}[/yellow]"
                _ms = _mem_status()
                if _ms is None or _ms.avail_phys_mb is None:
                    live_str = f"{sep}Live: {cnt}{_att}"
                else:
                    per = _ram_per_pane_mb()
                    # 'fit' from the SAME gate math (commit/load/phys), not a raw
                    # free-RAM floor, so the indicator matches what the gate allows.
                    fit, _ = _ram_fit(_ms, per, **_ram_gate_kwargs())
                    fit = min(fit, max(0, self._live.max_live - cnt))   # MAX_LIVE backstop
                    _col = "green" if fit > 0 else "red"
                    _load = f"{_ms.load:.0f}% load · " if _ms.load is not None else ""
                    live_str = (f"{sep}Live: {cnt}{_att}  [{_col}]~{fit} fit[/{_col}]"
                                f"  ({_load}{_ms.avail_phys_mb / 1024:.1f}GB free)")
            # Search: when the on-demand bar is hidden, surface the active text
            # query (so a filtered list isn't mistaken for "sessions missing");
            # otherwise hint how to open it.
            search_str = ""
            if not self._search_visible():
                try:
                    _q = self.query_one("#search", Input).value.strip()
                except Exception:
                    _q = ""
                if _q:
                    _qd = _q if len(_q) <= 30 else _q[:29] + "…"
                    search_str = f"{sep}[yellow]search: {_qd!r}[/yellow]"
                else:
                    search_str = f"{sep}[dim]/ search[/dim]"
            # Standing keyboard breadcrumb — the footer is trimmed to the core
            # four keys, so this is where leader/help discoverability lives.
            # When panes are open, prepend the release-key hint: the pane
            # swallows every key, so "how do I get back to the list" is the
            # single thing a new user must see without opening ? help.
            _kb_parts = []
            if self._live is not None and self._live.count > 0:
                _rk = _release_focus_key()   # e.g. "ctrl+]"
                _rk_disp = "+".join(
                    p.capitalize() if len(p) > 1 else p
                    for p in _rk.split("+"))  # "ctrl+]" → "Ctrl+]"
                _kb_parts.append(f"[bold]{_rk_disp}[/bold] [dim]list[/dim]")
            if self._leader_key:
                _kb_parts.append("[dim]␣ menu · ? keys[/dim]")
            else:
                _kb_parts.append("[dim]? keys[/dim]")
            _kb = " · ".join(_kb_parts)
            text = (f"  {n} sessions{search_str}{sep}{sort_str}{sep}"
                    f"{scope}{sep}{group_str}{filt_str}{tree_str}"
                    f"{live_str}{sep}{_kb}")
            self.query_one("#statusbar", Static).update(text)

        def _cursor_sid(self) -> str | None:
            table = self.query_one("#table", DataTable)
            if table.row_count == 0:
                return None
            try:
                row_key, _ = table.coordinate_to_cell_key((table.cursor_row, 0))
                if not row_key:
                    return None
                val = str(row_key.value)
                return None if val.startswith("__hdr__") else val
            except Exception:
                return None

        def _apply_split_ratio(self, ratio: float) -> None:
            """Set the list width to `ratio` of #main (the pane is 1fr → it
            absorbs the rest). Inline style beats the CSS width rule."""
            try:
                self.query_one("#table", DataTable).styles.width = f"{ratio * 100:.1f}%"
            except Exception:
                pass

        def _drag_split(self, screen_x: int) -> None:
            """Live divider drag: recompute the list/pane ratio from the pointer
            column and apply it (persisted only on mouse-up, in _commit)."""
            try:
                reg = self.query_one("#main").region
                self._split_ratio = _split_ratio_from_x(screen_x, reg.x, reg.width)
                self._apply_split_ratio(self._split_ratio)
            except Exception:
                pass

        def _commit_split_ratio(self) -> None:
            try:
                _set_split_ratio(getattr(self, "_split_ratio", _get_split_ratio()))
            except Exception:
                pass

        def _update_preview(self, sid: str | None) -> None:
            # Skip the clear + re-render when the SAME session is already shown in
            # the SAME mode. The 1.5s status poll rebuilds the table and re-fires a
            # highlight for the selected row every tick; without this guard a
            # static (non-live) preview gets cleared and rewritten each time —
            # visible flicker + a disk re-read for nothing. The Tab mode-toggle
            # changes preview_mode, so it still re-renders; F5 / row changes too.
            if (sid == getattr(self, "_last_preview_sid", object())
                    and self.preview_mode == getattr(self, "_last_preview_mode", None)):
                return
            self._last_preview_sid = sid
            self._last_preview_mode = self.preview_mode
            preview = self.query_one("#preview", RichLog)
            preview.clear()
            if not sid:
                return
            cache_dir = (PREVIEW_FULL_DIR if self.preview_mode == "full"
                         else PREVIEW_DIR)
            cache_file = cache_dir / f"{sid}.txt"
            # Rendering runs on the UI thread; guard it like the other handlers
            # in this class so one malformed session shows a per-row message
            # instead of tearing down the whole picker.
            try:
                s = self._sid_index.get(sid)
                if self.preview_mode == "changes" and s is not None:
                    # Transcript-reconstructed diff; render on demand (no cache).
                    preview.write(Text.from_ansi(_render_preview_changes(s)))
                    return
                # Open sessions grow every turn, so a cached preview goes stale.
                # Render them fresh each time (skip the cache entirely).
                if s is not None and s.get("is_open"):
                    render = (_render_preview_full if self.preview_mode == "full"
                              else _render_preview)
                    preview.write(Text.from_ansi(render(s)))
                    return
                if not cache_file.exists() and s is not None:
                    # Warm on demand (fallback for rows the background pre-warm
                    # has not reached yet) so the cache stays self-sufficient.
                    _write_preview_cache(s)
                if cache_file.exists():
                    preview.write(Text.from_ansi(cache_file.read_text(encoding="utf-8")))
                else:
                    preview.write(f"(no preview for {sid[:8]})")
            except Exception as e:
                preview.write(f"(preview failed for {sid[:8]}: {e})")

        # ── events ──────────────────────────────────────────────────────────

        def _search_visible(self) -> bool:
            try:
                return bool(self.query_one("#searchrow").display)
            except Exception:
                return False

        def _open_search(self, prefill: str | None = None) -> None:
            """Show the search/filter bar (docked top) and focus the box.
            prefill appends a just-typed char (search-as-you-type). The shown
            state persists so the next launch matches the user's last choice."""
            try:
                self.query_one("#searchrow").display = True
                search = self.query_one("#search", Input)
            except Exception:
                return
            _save_options({"search_bar": True})
            search.focus()
            if prefill:
                search.value = search.value + prefill
                search.cursor_position = len(search.value)

        def _hide_search(self) -> None:
            """Hide the bar so the table reclaims the rows. The query is KEPT (the
            list stays filtered; the statusbar shows it), so Esc dismisses the
            chrome, not the filter. The hidden state persists across launches."""
            try:
                self.query_one("#searchrow").display = False
            except Exception:
                pass
            _save_options({"search_bar": False})
            try:
                self.query_one("#table", DataTable).focus()
            except Exception:
                pass

        def on_key(self, event) -> None:
            # Ctrl+C / Ctrl+Q reaching the App means the LIST or search box is
            # focused (a focused live terminal consumes Ctrl+C first, to interrupt
            # claude). Route it to our force-quit (kill-all + join) so Textual's
            # built-in quit can't exit WITHOUT reaping the claude trees — the
            # Screen's default ctrl+c=quit would otherwise shadow our binding.
            if event.key in ("ctrl+c", "ctrl+q"):
                event.stop()
                self.action_quit_all()
                return
            # Leader/prefix (opt-in, [keys] leader). The leader arms a pending state;
            # the next key runs the mapped action. A focused claude pane consumes its
            # own keys, while the App binding may arm Space from other non-input,
            # non-dropdown saikai controls. Handled BEFORE search-as-you-type so
            # the post-leader letter doesn't fall through and open the search box.
            if self._leader_key:
                if self._leader_pending:
                    self._leader_pending = False
                    event.stop()
                    if event.key != "escape":
                        _act = self._leader_actions.get((event.character or "").lower())
                        _fn = getattr(self, "action_" + _act, None) if _act else None
                        if callable(_fn):
                            try:
                                _fn()
                            except Exception:
                                pass
                    return
                try:
                    _tbl = self.query_one("#table", DataTable)
                except Exception:
                    _tbl = None
                if event.key == self._leader_key and self.focused is _tbl:
                    self._leader_pending = True
                    # which-key style: hint only on HESITATION (no second key
                    # within 0.6 s). Fast fingers (Space-f, double-Space mark
                    # sprees) never see a toast; a user who pauses gets the map,
                    # grouped by family — every time, not just the first three.
                    self.set_timer(0.6, self._show_leader_hint)
                    self.set_timer(2.5, self._cancel_leader)
                    event.stop()
                    return
            # search-as-you-type: typing while the table is focused redirects into the
            # search input. Arrow keys / control bindings still route normally
            # because we only intercept printable single characters and
            # Backspace. Down arrow from the search input jumps to the table
            # so the user can drive a result without reaching for the mouse.
            try:
                search = self.query_one("#search", Input)
                table = self.query_one("#table", DataTable)
            except Exception:
                return
            if self.focused is table:
                if (event.key == "space" and _LIVE_TERM is not None
                        and not search.value):
                    # Space toggles a batch-launch mark (split-live only). With the
                    # default Space LEADER this is unreachable (leader consumed the
                    # key above; mark = leader→Space, i.e. double-Space) — it's the
                    # fallback for [keys] leader = "none" / another leader. Only when
                    # no query is in progress: once typing has started, Space must
                    # reach the search box so multi-word queries work.
                    self.action_toggle_mark()
                    event.stop()
                    return
                if event.key == "slash":
                    # '/' opens the on-demand search/filter bar EMPTY (vim-style;
                    # not a readline key). Other printable chars open it AND type
                    # in (search-as-you-type), so a leading '/' is the only char you
                    # can't search literally from the list (rare — type it after).
                    self._open_search()
                    event.stop()
                    return
                if event.character == "?":
                    # '?' opens the help screen. Guard here so Linux terminals that
                    # don't fire the priority `question_mark` binding before on_key
                    # don't swallow '?' into type-to-search instead.
                    self.action_help()
                    event.stop()
                    return
                char = event.character
                if char and len(char) == 1 and char.isprintable():
                    self._open_search(char)
                    event.stop()
                elif event.key == "backspace" and search.value:
                    self._open_search()
                    search.value = search.value[:-1]
                    search.cursor_position = len(search.value)
                    event.stop()
            elif self.focused is search and event.key == "down":
                table.focus()
                event.stop()

        def _cancel_leader(self) -> None:
            """Leader timed out / cancelled — drop the pending state (the next key
            types normally again)."""
            self._leader_pending = False

        def action_arm_leader(self) -> None:
            """The footer's ␣ Menu binding: arm the leader from any non-typing
            context. Fires only when space bubbled UNCONSUMED to the App (an
            Input or terminal keeps its space; the table fast path in on_key
            already stopped the event), so no double-arm and no stolen keys."""
            if self._leader_key != "space":
                raise SkipAction()
            if self._leader_pending:
                return
            if (self._focused_terminal() is not None
                    or isinstance(self.focused, (Input, Select))):
                return
            self._leader_pending = True
            self.set_timer(0.6, self._show_leader_hint)
            self.set_timer(2.5, self._cancel_leader)

        def _show_leader_hint(self) -> None:
            """Deferred which-key hint: fires 0.6 s after the leader press, and
            only if the sequence is STILL pending — the user hesitated, so show
            the map grouped by family. Completed / cancelled sequences (and a
            double-Space mark spree) never see a toast."""
            if not self._leader_pending or not self._leader_actions:
                return
            lines = []
            for fam, pairs in _leader_groups(self._leader_actions):
                seq = "  ".join(_leader_hint_item(k, lbl) for k, lbl in pairs)
                lines.append(f"[bold cyan]{fam:<7}[/bold cyan] {seq}")
            self.notify("\n".join(lines), title="Command menu · press one key",
                        timeout=4)

        def _over_tab_bar(self, event) -> bool:
            """True when the mouse is over the split-live tab bar. Textual's Tabs
            doesn't consume mouse-scroll, so such a scroll bubbles up to the App;
            panes and the list consume their OWN scroll, so anything reaching here
            over the tab strip is meant for tab navigation."""
            if _LIVE_TERM is None:
                return False
            try:
                w, _ = self.get_widget_at(event.screen_x, event.screen_y)
            except Exception:
                return False
            node = w
            while node is not None:
                if isinstance(node, Tabs):
                    return True
                node = node.parent
            return False

        def on_mouse_scroll_down(self, event) -> None:
            if self._over_tab_bar(event):
                self._cycle_tab(+1)        # wheel over the tab bar → next tab
                try:
                    event.stop()
                except Exception:
                    pass

        def on_mouse_scroll_up(self, event) -> None:
            if self._over_tab_bar(event):
                self._cycle_tab(-1)        # wheel over the tab bar → previous tab
                try:
                    event.stop()
                except Exception:
                    pass

        def on_data_table_row_highlighted(self, event) -> None:
            # Ignore STALE highlight events. A background rebuild (1.5s status
            # poll, esp. under Recency sort + Group-by-State which reorders every
            # tick) clears the cursor to row 0 and re-adds rows, QUEUEING a
            # RowHighlighted for each intermediate position; the synchronous cursor
            # RESTORE then moves to the saved session. By the time those queued
            # events run, the cursor has already moved on — acting on them
            # (header-skip / pane-switch) would drag the selection to the wrong row
            # ("the selected session keeps changing on its own"). If this event's
            # row is no longer where the cursor actually is, it's superseded: drop it.
            try:
                if event.cursor_row != self.query_one("#table", DataTable).cursor_row:
                    return
            except Exception:
                pass
            sid = str(event.row_key.value) if event.row_key else None
            # A row just opened via Enter wants focus on its PANE (cursor keys go
            # to claude), not the list — consume that marker one-shot here.
            just_opened = (sid is not None
                           and sid == getattr(self, "_opening_live_sid", None))
            if just_opened:
                self._opening_live_sid = None
            # If a live pane is focused, ignore highlight events from background
            # refreshes ENTIRELY (incl. header rows) — never switch the right pane
            # or steal focus while the user is interacting with claude.
            if self._focused_terminal() is not None:
                return
            if sid and sid.startswith("__hdr__"):
                # Category header rows are NOT selectable: skip the cursor past
                # them in the direction of travel so arrow-browsing (and the
                # initial mount highlight) never parks on a "no session" row.
                tbl = self.query_one("#table", DataTable)
                cur = tbl.cursor_row
                down = cur >= getattr(self, "_last_cursor_row", -1)
                tgt = _first_selectable_row(tbl, cur, 1 if down else -1)
                if tgt is None:                       # nothing that way → try back
                    tgt = _first_selectable_row(tbl, cur, -1 if down else 1)
                if tgt is not None:
                    tbl.move_cursor(row=tgt)          # re-fires highlight on a real row
                    return
                # No selectable session anywhere (empty state): show the label.
                if _LIVE_TERM is not None:
                    try:
                        self.query_one("#right", TabbedContent).active = "tab-preview"
                    except Exception:
                        pass
                try:
                    label = getattr(self, "_header_labels", {}).get(sid, "")
                    pv = self.query_one("#preview", RichLog)
                    pv.clear()
                    pv.write(Text(f"\n  ── {label} ──", style="bold #7aa2f7"))
                    pv.write(Text("  group header — no session selected", style="dim"))
                except Exception:
                    pass
                return
            # Remember where the cursor is now so the next header-skip knows
            # which way we're traveling.
            try:
                self._last_cursor_row = self.query_one("#table", DataTable).cursor_row
            except Exception:
                pass
            # Filtering must NOT yank the foreground. While the search box is
            # focused, every highlight change is filter-driven (auto): the user
            # only navigates results AFTER Down moves focus to the table. So if a
            # search filtered the foreground session out and the cursor auto-moved
            # to another row, keep the current foreground LIVE pane instead of
            # switching to the auto-selected one. (Enter-to-open sets just_opened
            # and is exempt; a preview-only foreground still follows the filter.)
            if not just_opened:
                try:
                    _searching = self.focused is self.query_one("#search", Input)
                except Exception:
                    _searching = False
                if _searching:
                    try:
                        _active = self.query_one("#right", TabbedContent).active or ""
                    except Exception:
                        _active = ""
                    if _active and _active != "tab-preview":
                        return    # foreground is a live pane — don't follow the filter
            # Claude-Desktop-like: highlighting a row shows its content on the
            # right — a LIVE session switches to its terminal tab, a non-live one
            # shows the static preview. Focus stays on the list so arrow-browsing
            # stays smooth; Enter is what focuses a live pane to type into it.
            if _LIVE_TERM is not None and self._live is not None and sid and self._live.has(sid):
                try:
                    self.query_one("#right", TabbedContent).active = self._live.pane_id(sid)
                    if just_opened:
                        term = self._live.get(sid)
                        if term is not None:
                            term.focus()        # Enter-opened → cursor keys to claude
                    else:
                        self.query_one("#table", DataTable).focus()   # browsing → stay on list
                    return
                except Exception:
                    pass
            if _LIVE_TERM is not None:
                try:
                    self.query_one("#right", TabbedContent).active = "tab-preview"
                except Exception:
                    pass
            self._update_preview(sid)

        def action_resume(self) -> None:
            # Enter on a focused dropdown (Group/Sort/Status/Age) belongs to the
            # Select (open/confirm its overlay), not resume — forward it.
            if isinstance(self.focused, Select):
                raise SkipAction()
            # When split-live is unavailable, keep the original behavior: exit
            # the picker and hand the bare terminal to a single blocking claude.
            if _LIVE_TERM is None:
                sid = self._cursor_sid()
                if sid:
                    # Probe BEFORE tearing down the picker: here resume runs only
                    # AFTER self.exit() leaves the alt-screen, so a "claude not on
                    # PATH" error would print into a half-restored terminal and
                    # scroll away — the user just sees saikai vanish. Surface it now.
                    if shutil.which("claude") is None:
                        self.notify("claude not found on PATH — cannot resume",
                                    severity="error", title="saikai", timeout=8)
                        return
                    self.exit(sid)
                return
            # Split-live: if a terminal is focused, Enter belongs to claude.
            # A plain `return` still counts as "handled", so the priority binding
            # would SWALLOW the key; raise SkipAction so Textual forwards it to
            # the focused AgentTerminal (whose on_key writes \r to the PTY).
            if self._focused_terminal() is not None:
                raise SkipAction()
            # Batch launch: if rows are marked (Space), open a pane for each.
            if self._marked:
                self._open_marked_live()
                return
            sid = self._cursor_sid()
            if sid:
                self._open_or_attach_live(sid)

        def action_resume_detached(self) -> None:
            """Legacy full-takeover: exit the picker and run claude in the bare
            terminal (alternate screen handed off). Kept as an escape hatch for
            users who want a full-screen claude instead of the split pane."""
            sid = self._cursor_sid()
            if sid:
                # Tear down any live panes first so their PTYs don't outlive the
                # picker as orphans once we exit into the foreground claude.
                if self._live is not None:
                    self._live.kill_all()
                self.exit(sid)

        def action_toggle_mark(self) -> None:
            """Toggle the cursor row's batch-launch selection (Space). Split-live
            only: batch launch opens one live pane per marked session."""
            if _LIVE_TERM is None:
                return
            sid = self._cursor_sid()
            if not sid:
                return
            if sid in self._marked:
                self._marked.discard(sid)
            else:
                self._marked.add(sid)
            self._request_refresh()   # repaint the ▣ marker (coalesced)

        def _open_marked_live(self) -> None:
            """Open a live pane for each marked session (in display order),
            honoring the pane cap, then clear the marks. The last one opened
            keeps focus (each open sets _opening_live_sid)."""
            if _LIVE_TERM is None or self._live is None:
                return
            order = [s["id"] for s in all_sessions if s["id"] in self._marked]
            self._marked.clear()
            opened = 0
            for sid in order:
                if (not self._live.has(sid) and sid not in self._opening_sids
                        and _at_live_capacity(self._live.count, len(self._opening_sids),
                                              self._live.max_live)):
                    self.notify(
                        f"opened {opened}; hit the {self._live.max_live}-pane "
                        f"backstop — close some (F10) or raise SAIKAI_MAX_LIVE",
                        severity="warning", timeout=6)
                    break
                self._open_or_attach_live(sid, refresh=False)   # repaint once below
                opened += 1
            self._refresh_table()

        # ── split-live helpers ────────────────────────────────────────────────
        def _focused_terminal(self):
            """Return the focused LIVE AgentTerminal, or None.

            A DEAD pane (claude exited) deliberately counts as None: otherwise
            Enter/Esc/Tab/? all SkipAction into a corpse and the user can neither
            relaunch nor leave. Treating it as not-focused lets Enter fall through
            to relaunch and Esc/F10 close the ✓ tab."""
            if _LIVE_TERM is None:
                return None
            foc = self.focused
            if isinstance(foc, _LIVE_TERM.AgentTerminal) and not getattr(foc, "is_dead", False):
                return foc
            return None

        def _focus_live_pane(self, sid: str) -> None:
            """Focus a live pane's terminal (deferred from open) so cursor keys go
            to claude; consume the just-opened marker."""
            if getattr(self, "_opening_live_sid", None) == sid:
                self._opening_live_sid = None
            if self._live is None:
                return
            term = self._live.get(sid)
            if term is not None:
                try:
                    term.focus()
                except Exception:
                    pass

        def _open_or_attach_live(self, sid: str, refresh: bool = True) -> None:
            """Resume an existing session as a live pane (or switch to it if it's
            already running)."""
            assert _LIVE_TERM is not None and self._live is not None
            if self._live.has(sid):                  # already running → switch
                tabs = self.query_one("#right", TabbedContent)
                tabs.active = self._live.pane_id(sid)
                self._opening_live_sid = sid
                self.call_after_refresh(lambda: self._focus_live_pane(sid))
                return
            if sid in self._opening_sids:
                # an open is already in flight for this sid (mount worker pending);
                # a second Enter / wheel must not spawn a duplicate — the worker
                # focuses it once mounted.
                return
            s = self._sid_index.get(sid)
            try:
                argv, cwd, env = _build_resume_invocation(sid, all_sessions)
            except Exception as e:
                self.notify(f"could not build resume command: {e!r}",
                            severity="error", timeout=8)
                return
            title = _pane_title(s, sid)
            self._spawn_live_pane(sid, argv, cwd, env, title, refresh=refresh)

        def _open_new_live(self, target_cwd: str) -> None:
            """Start a FRESH claude session in target_cwd as a live pane. A
            pre-generated --session-id keys the pane so the new session links to
            its list row once claude writes the JSONL (appears on the next scan)."""
            if _LIVE_TERM is None or self._live is None:
                return
            try:
                d = Path(target_cwd).expanduser()
                if not d.is_dir():
                    self.notify(f"not a directory: {target_cwd}",
                                severity="error", timeout=6)
                    return
            except Exception as e:
                self.notify(f"bad path: {e!r}", severity="error", timeout=6)
                return
            sid = str(uuid.uuid4())
            try:
                argv, cwd, env = _build_new_invocation(str(d), sid, all_sessions)
            except Exception as e:
                self.notify(f"could not build new-session command: {e!r}",
                            severity="error", timeout=8)
                return
            title = d.name or str(d)
            if self._spawn_live_pane(sid, argv, cwd, env, title, refresh=False):
                # Show the new session in the list NOW: its JSONL isn't scanned yet
                # (and may be under an out-of-scope project dir). Insert a stub row;
                # _apply_fresh_sessions preserves it across reloads (it's a live
                # pane), and a reload that finds the real JSONL replaces it (same id).
                stub = _new_session_stub(sid, str(d), title)
                all_sessions.append(stub)
                self._sid_index[sid] = stub
                self._refresh_table()
                self.notify(f"new claude session in {d}", timeout=4)

        def _spawn_live_pane(self, sid, argv, cwd, env, title, refresh=True) -> bool:
            """Capacity/RAM-gate, mount, register and focus a live pane for an
            already-built (argv, cwd, env). Shared by resume + new-session; returns
            True if it opened. Does NOT weaken the kill/reap lifecycle."""
            assert _LIVE_TERM is not None and self._live is not None
            tabs = self.query_one("#right", TabbedContent)
            pane_id = self._live.pane_id(sid)
            # Count in-flight opens too (register is deferred to _mount_live_pane);
            # otherwise a batch / restore loop reads a stale count and overruns the cap.
            if _at_live_capacity(self._live.count, len(self._opening_sids),
                                 self._live.max_live):
                self.notify(
                    f"hit the {self._live.max_live}-pane backstop; close one "
                    f"(F10) or raise SAIKAI_MAX_LIVE",
                    severity="warning", timeout=6)
                return False
            # The real limit is memory (each live pane is a node process tree).
            # Windows-principled gate (see _ram_fit / spec A.1): the system-freeze
            # cause is the commit charge nearing the commit limit, so gate on commit
            # headroom + dwMemoryLoad + a RELATIVE physical floor — NOT raw available-
            # physical (which counts reclaimable standby cache). Default = warn but
            # open; SAIKAI_HARD_RAM_GATE=1 makes it a hard stop. Legacy SAIKAI_MIN_FREE_MB
            # still honoured as an absolute floor; SAIKAI_CLAUDE_MB = est. per pane.
            _per = _ram_per_pane_mb()
            _ok, _why = _ram_gate_decision(_mem_status(), _per, **_ram_gate_kwargs())
            if not _ok:
                if _cfg("limits", "hard_ram_gate", "SAIKAI_HARD_RAM_GATE", False, _cfg_bool):
                    self.notify(
                        f"refusing to open — {_why}; ~{_per:.0f} MB/pane would cross "
                        f"the floor. Close a pane (F10), lower SAIKAI_CLAUDE_MB, or "
                        f"raise the thresholds.",
                        severity="error", timeout=9)
                    return False
                self.notify(
                    f"memory pressure — {_why}; each live claude is RAM-heavy "
                    f"(~{_per:.0f} MB). Close panes (F10) if it slows. "
                    f"SAIKAI_HARD_RAM_GATE=1 to block instead.",
                    severity="warning", timeout=7)
            term = _LIVE_TERM.AgentTerminal(
                argv, cwd=cwd, env=env, sid=sid, title=title,
                on_status=self._on_live_status, on_exit=self._on_live_exit,
                status_classifier=_LIVE_TERM.classifier_for_profile(
                    _ACTIVE_PROVIDER.status_profile),
            )
            pane = TabPane(_LIVE_TERM.tab_label(title, "idle"), term, id=pane_id)
            # Mount on the UI event loop in a worker so we can AWAIT the removal of
            # any lingering same-id dead pane BEFORE adding the new one. remove_pane()
            # is deferred (returns AwaitComplete), so the old synchronous
            # remove_pane()+add_pane() collided — the removal hadn't flushed, add_pane
            # raised DuplicateIds, and re-opening an EXITED session silently failed
            # (its dead pane is kept for the final frame, only forgotten from the
            # manager). The capacity/RAM gate above already decided this pane WILL
            # open, so return True now; the worker registers + focuses once the DOM
            # settles. Runs on the UI loop, never the reader thread → lock invariant
            # untouched. Repro/fix: tests/test_terminal_concurrency.py.
            # Mark the open in flight BEFORE scheduling so the capacity gate +
            # has() dedup count it until the worker registers (or gives up).
            self._opening_sids.add(sid)
            self.run_worker(
                self._mount_live_pane(tabs, pane_id, pane, term, sid, refresh),
                name=f"mount-{sid[:8]}", exit_on_error=False,
            )
            return True

        async def _mount_live_pane(self, tabs, pane_id, pane, term, sid,
                                   refresh=True) -> None:
            """Await-safe mount of a live pane (see _spawn_live_pane). Drops a
            lingering same-id dead pane and WAITS for the DOM to settle, then adds
            the new pane, registers it, and focuses it. On failure the half-built
            term is killed + reaped so no claude is orphaned untracked. `sid` stays
            in self._opening_sids (added by _spawn_live_pane) until this returns —
            the `finally` always clears it — so the capacity gate + has() dedup
            count this in-flight open while register() is pending."""
            try:
                try:
                    exists = False
                    try:
                        exists = tabs.get_pane(pane_id) is not None
                    except Exception:
                        exists = False
                    if exists:
                        # exited session being re-opened: remove the kept dead pane
                        # and WAIT (deferred removal) so add_pane can't DuplicateIds.
                        try:
                            await tabs.remove_pane(pane_id)
                        except Exception:
                            pass
                    await tabs.add_pane(pane)
                except Exception as e:
                    try:
                        self._live.note_reap(term.kill())
                    except Exception:
                        pass
                    self.notify(f"could not open tab: {e!r}", severity="error", timeout=8)
                    return
                # claude died DURING mount (pyte/ConPTY spawn failed, or an instant
                # EOF marshalled _finalize while we awaited add_pane). Do NOT register
                # it — that would re-add a dead 'idle' pane to the manager AND to the
                # Shift+F4 restore set. Drop the zombie tab and tell the user.
                if getattr(term, "is_dead", False):
                    try:
                        self._live.note_reap(term.kill())
                    except Exception:
                        pass
                    try:
                        await tabs.remove_pane(pane_id)
                    except Exception:
                        pass
                    self.notify(f"session {sid[:8]} could not start",
                                severity="warning", timeout=6)
                    return
                self._live.register(sid, term)
                _log(f"live open: {sid[:8]}  ({self._live.count}/{self._live.max_live})")
                self._opened_sids.add(sid)
                self._save_open_panes()
                tabs.active = pane_id
                # Focus the new pane so cursor keys go straight to claude. The
                # post-open _refresh_table re-emits a row-highlight that races this
                # deferred focus; mark the sid "just opened" so the highlight handler
                # focuses the PANE too — whichever runs first, focus lands on claude
                # (and the _focused_terminal guard then keeps it there).
                self._opening_live_sid = sid
                self.call_after_refresh(lambda: self._focus_live_pane(sid))
                # 1-shot teams-notify suppression so the first idle_prompt after
                # launch doesn't ping (mirrors _resume_claude). Best-effort.
                try:
                    _add_saikai_suppress_session(sid)
                except Exception:
                    pass
                # Refresh the table so the marker column shows this row is now live.
                # Batch launch passes refresh=False and repaints ONCE after all opens.
                if refresh:
                    self._refresh_table()
            finally:
                self._opening_sids.discard(sid)

        def _save_open_panes(self) -> None:
            """Persist {id, cwd} for panes open this session so Shift+F4 can reopen
            them after a restart/upgrade (cwd lets an out-of-scope session resume in
            the right dir). Best-effort; never blocks the UI."""
            try:
                rows = []
                for sid in sorted(self._opened_sids):
                    s = self._sid_index.get(sid) or {}
                    rows.append({"id": sid,
                                 "cwd": s.get("origin_cwd") or s.get("cwd") or ""})
                _write_json(OPEN_PANES_FILE, rows)
            except Exception:
                pass

        def action_restore_panes(self) -> None:
            """Shift+F4: reopen the PREVIOUS session's panes (snapshot loaded at
            startup) — resume each, skipping ones already open. Available anytime,
            not just at launch. An out-of-scope sid (different project dir) gets a
            stub injected with its saved cwd so resume targets the right dir."""
            if _LIVE_TERM is None or self._live is None:
                return
            opened = 0
            for row in list(getattr(self, "_restore_candidates", []) or []):
                sid = (row.get("id") if isinstance(row, dict) else row) or ""
                if not sid or self._live.has(sid):
                    continue
                cwd = row.get("cwd", "") if isinstance(row, dict) else ""
                if sid not in self._sid_index:
                    if cwd and Path(cwd).is_dir():
                        stub = _new_session_stub(sid, cwd, Path(cwd).name or sid[:8])
                        all_sessions.append(stub)
                        self._sid_index[sid] = stub
                    else:
                        continue   # not scanned and no usable cwd → can't resume
                self._open_or_attach_live(sid, refresh=False)
                opened += 1
            if opened:
                self._refresh_table()
                self.notify(f"reopened {opened} pane(s) from last session", timeout=4)
            else:
                self.notify("nothing to restore", timeout=3)

        def action_new_session(self) -> None:
            """Shift+F8: pick a folder / git worktree and start a FRESH claude
            session there as a live pane (split-live only)."""
            if _LIVE_TERM is None or self._live is None:
                self.notify(f"new session needs split-live — disabled: {_LIVE_TERM_REASON}",
                            severity="warning", timeout=6)
                return
            base = str(repo) if repo else str(Path.cwd())
            cands = self._new_session_candidates()

            def _go(path):
                if path:
                    self._open_new_live(path)
            self.push_screen(NewSessionScreen(base, cands), _go)

        def _new_session_candidates(self):
            """(label, path) rows for the new-session picker: git worktrees of the
            current repo first, then distinct recent session dirs. Existing only."""
            out, seen = [], set()
            try:
                r = subprocess.run(
                    ["git", "worktree", "list", "--porcelain"],
                    cwd=(str(repo) if repo else None),
                    capture_output=True, text=True, timeout=5)
                for line in r.stdout.splitlines():
                    if line.startswith("worktree "):
                        p = line[9:].strip()
                        if p and p not in seen and Path(p).is_dir():
                            seen.add(p)
                            out.append((f"worktree   {p}", p))
            except Exception:
                pass
            for s in all_sessions:
                p = s.get("origin_cwd") or s.get("cwd")
                if p and p not in seen and Path(p).is_dir():
                    seen.add(p)
                    out.append((f"recent     {p}", p))
                if len(out) >= 40:
                    break
            return out

        def _request_refresh(self) -> None:
            """Coalesce frequent table-refresh requests (live status flips, the
            1.5s poll) into ~one rebuild per frame, so a streaming claude can't
            trigger a full-DataTable-rebuild storm on the UI thread."""
            if getattr(self, "_refresh_req_pending", False):
                return
            self._refresh_req_pending = True
            try:
                self.call_after_refresh(self._do_req_refresh)
            except Exception:
                self._refresh_req_pending = False
                self._refresh_table()

        def _do_req_refresh(self) -> None:
            self._refresh_req_pending = False
            self._refresh_table()

        def _on_live_status(self, sid: str, status: str) -> None:
            """Called on the UI thread (terminal marshals it) when a pane's
            Busy/Waiting/Idle/dead status changes."""
            if self._live is None or not self._live.has(sid):
                return   # ignore a callback that lands after the pane was closed
            self._live.set_status(sid, status)
            # "!" = claude FINISHED its turn (went idle) and you haven't sent input
            # since. Flagged in the list EVEN for the currently-displayed tab — you
            # might be looking at the list or another pane, so you'd otherwise miss
            # WHAT finished. Cleared ONLY when the user sends input (claude goes
            # busy again), NOT by merely viewing the tab — viewing ≠ responding.
            if status == "idle":
                self._unread.add(sid)
            elif status == "busy":
                self._unread.discard(sid)
                self._busy_seen.add(sid)   # reader sees every transition (incl. tasks
                #                            shorter than the poll) → the "done" toast
                #                            in _poll_live_status keys off this, not a
                #                            1-tick prev-status snapshot it can miss.
            # Update the tab label.
            try:
                tabs = self.query_one("#right", TabbedContent)
                s = self._sid_index.get(sid)
                title = _pane_title(s, sid, self._live.get(sid))
                pane = tabs.get_pane(self._live.pane_id(sid))
                if pane is not None:
                    pane.label = _LIVE_TERM.tab_label(title, status)
            except Exception:
                pass
            # Mirror onto the DataTable marker so a backgrounded waiting session
            # is loud even when its tab isn't focused. Coalesced: a streaming
            # claude flips status many times/sec and each full rebuild is costly.
            self._request_refresh()

        def on_tabbed_content_tab_activated(self, event) -> None:
            # A live tab became active (F2/F3, Shift+F3, click) → move the list
            # cursor to the matching session so you can see WHICH one it is.
            # Deliberately does NOT clear the "!" finished-marker — that stays until
            # you respond (input → claude busy, handled in _on_live_status). The
            # row-highlight → tab-switch loop is broken naturally: F2/F3 focus the
            # pane, so the resulting RowHighlighted returns early via the
            # _focused_terminal guard; a plain click re-switches to the same tab
            # (a no-op, no re-fire).
            if self._live is None:
                return
            try:
                active = self.query_one("#right", TabbedContent).active or ""
            except Exception:
                return
            if not active or active == "tab-preview":
                return
            sid = next((s for s in self._live.statuses()
                        if self._live.pane_id(s) == active), None)
            if sid is None:
                return
            try:
                table = self.query_one("#table", DataTable)
                row = table.get_row_index(sid)
            except Exception:
                return
            if row != table.cursor_row:
                table.move_cursor(row=row)

        def _mark_not_open(self, sid: str) -> None:
            """A pane we hosted for `sid` is gone (explicit close OR its claude
            exited), so the session is no longer "open". Clear the load-time
            is_open stamp — the source of the @ marker — so the row stops showing
            Open immediately (is_active/recent still recompute from mtime, so a
            just-finished session correctly shows + / .). Invalidate the live-
            session cache too, or the next rescan would re-read a registry entry
            for the now-dead PID and resurrect the @."""
            s = self._sid_index.get(sid)
            if s is not None:
                s["is_open"] = False
            _invalidate_active_sessions()

        def _on_live_exit(self, sid: str) -> None:
            """Called on the UI thread when a pane's child exits. Keep the tab
            (so the user sees the final frame) but re-title it; drop it from the
            active set so a later Enter re-launches instead of attaching to a
            dead PTY."""
            if self._live is None:
                return
            self._unread.discard(sid)   # a dead pane is no longer a live unread answer
            self._busy_seen.discard(sid)  # …nor owed a "done" toast
            try:
                tabs = self.query_one("#right", TabbedContent)
                s = self._sid_index.get(sid)
                title = _pane_title(s, sid, self._live.get(sid))
                pane = tabs.get_pane(self._live.pane_id(sid))
                if pane is not None:
                    pane.label = _LIVE_TERM.tab_label(title, "dead")
            except Exception:
                pass
            self._live.forget(sid)
            self._mark_not_open(sid)         # exited → no longer Open (drop the @ marker)
            # A session whose claude EXITED on its own shouldn't reappear on the
            # next Shift+F4 restore (matches explicit-close in _close_live_sid).
            # The dead ✓ tab stays visible THIS session; re-launching it with Enter
            # re-adds it via _open_or_attach_live. Persist the trimmed snapshot now.
            self._opened_sids.discard(sid)
            self._save_open_panes()
            self._refresh_table()

        def on_agent_terminal_focus_released(self, event) -> None:
            """The terminal's Ctrl+] (SAIKAI_RELEASE_KEY) escape hatch: refocus the list."""
            self.query_one("#table", DataTable).focus()
            try:
                event.stop()
            except Exception:
                pass

        def _live_pane_ids(self) -> list:
            """All mounted live-pane ids (excludes the preview tab). Includes
            panes whose claude already EXITED (kept for the final frame), which
            are no longer in self._live.statuses() — so close paths can still
            target them instead of leaving them unclosable."""
            try:
                return [p.id for p in self.query_one("#right", TabbedContent).query(TabPane)
                        if p.id and p.id != "tab-preview"]
            except Exception:
                return []

        def _close_live_sid(self, sid) -> None:
            """Kill + remove one live session's tab. Afterwards show the next
            remaining live pane or the preview, and return focus to the list."""
            if self._live is None or sid is None:
                return
            _log(f"live close: {sid[:8]}")
            t = self._live.get(sid)
            if t is not None:
                try:
                    self._live.note_reap(t.kill())   # reap off-thread; UI stays snappy
                except Exception:
                    pass
            tabs = self.query_one("#right", TabbedContent)
            pane_id = self._live.pane_id(sid)
            # Land on the ADJACENT pane (DOM order), not the last-registered one,
            # so closing C in [A,B,C,D] goes to its neighbour, not whatever opened
            # last. _live_pane_ids() is DOM order and includes dead ✓ panes.
            ids_before = self._live_pane_ids()
            try:
                idx = ids_before.index(pane_id)
            except ValueError:
                idx = len(ids_before)
            self._live.forget(sid)
            self._mark_not_open(sid)         # closed → no longer Open (drop the @ marker)
            self._opened_sids.discard(sid)   # explicit close → drop from restore set
            self._unread.discard(sid)        # closed → not an unanswered finish (clears !N now, not on the deferred exit callback)
            self._busy_seen.discard(sid)
            self._save_open_panes()
            try:
                tabs.remove_pane(pane_id)
            except Exception:
                pass
            ids_after = [p for p in ids_before if p != pane_id]
            try:
                tabs.active = (ids_after[min(idx, len(ids_after) - 1)]
                               if ids_after else "tab-preview")
            except Exception:
                pass
            remaining = list(self._live.statuses().keys())
            try:
                self.notify(f"closed live session — {len(remaining)} still running",
                            timeout=2)
            except Exception:
                pass
            self.query_one("#table", DataTable).focus()
            self._refresh_table()

        def action_close_all_live(self) -> None:
            # Shift+F10: close ALL live panes at once (parallel kill) but STAY in
            # saikai — unlike Ctrl-C / Esc, which snapshot the set + quit. This is an
            # EXPLICIT close, so it CLEARS the restore snapshot (you won't get these
            # back via Shift+F4); quitting preserves it, this discards it. Removes
            # mounted panes incl. dead/exited ones (not just live statuses). It's
            # on a function key (not a readline key), so it fires from any focus —
            # no SkipAction forwarding needed, and a single stray press can't reach
            # it (Shift+F10 is deliberate), which is why all-panes-vanished is gone.
            if self._live is None:
                return
            tabs = self.query_one("#right", TabbedContent)
            ids = self._live_pane_ids()
            if not ids:
                return
            n = len(ids)
            for _sid in list(self._live.statuses().keys()):
                self._mark_not_open(_sid)   # all closed → drop their @ markers
            for pid in ids:
                try:
                    tabs.remove_pane(pid)
                except Exception:
                    pass
            self._live.kill_all()      # kill any still-live terms (parallel, non-blocking)
            self._opened_sids.clear()
            self._unread.clear()       # all closed → no unanswered finishes, no busy debt
            self._busy_seen.clear()
            self._save_open_panes()
            try:
                tabs.active = "tab-preview"
            except Exception:
                pass
            self.query_one("#table", DataTable).focus()
            self._refresh_table()
            self.notify(f"closed {n} live tab(s)", timeout=3)

        def action_close_live(self) -> None:
            """F10: close the active live tab. On a focused claude pane it closes
            THAT pane; from the list it closes the active tab. F10 is not a
            readline key, so unlike the old Ctrl+W it never needs to forward."""
            if _LIVE_TERM is None or self._live is None:
                return
            tabs = self.query_one("#right", TabbedContent)
            active = tabs.active or ""
            term = self._focused_terminal()
            sid = term.sid if term is not None else None
            if sid is None:
                # No terminal focused: act on the active tab if it's a live one.
                for s in list(self._live.statuses().keys()):
                    if self._live.pane_id(s) == active:
                        sid = s
                        break
            if sid is not None:
                self._close_live_sid(sid)
                return
            # A dead/forgotten pane (claude exited) is still mounted but absent
            # from statuses(); remove it directly so it isn't unclosable.
            if active and active != "tab-preview":
                try:
                    tabs.remove_pane(active)
                except Exception:
                    pass
                try:
                    tabs.active = "tab-preview"
                except Exception:
                    pass
                self.query_one("#table", DataTable).focus()
                self._refresh_table()

        def action_next_tab(self) -> None:
            self._cycle_tab(+1)

        def action_prev_tab(self) -> None:
            self._cycle_tab(-1)

        def action_next_attention(self) -> None:
            """Shift+F3: jump to the next live pane needing attention — waiting (?)
            or finished-unread (!) — in tab order, wrapping. Toast if none. Lets you
            step through exactly the panes that need you instead of every tab."""
            if _LIVE_TERM is None or self._live is None:
                return
            try:
                tabs = self.query_one("#right", TabbedContent)
            except Exception:
                return
            st = self._live.statuses()
            unread = getattr(self, "_unread", set())
            att_sids = {s for s in st if st.get(s) == "waiting" or s in unread}
            if not att_sids:
                self.notify("no live panes need attention", timeout=3)
                return
            ids = self._live_pane_ids()
            att_ids = [pid for pid in ids
                       if any(self._live.pane_id(s) == pid for s in att_sids)]
            if not att_ids:
                return
            active = tabs.active or ""
            cur_i = ids.index(active) if active in ids else -1
            nxt = next((pid for pid in att_ids if ids.index(pid) > cur_i), att_ids[0])
            tabs.active = nxt
            sid = next((s for s in att_sids if self._live.pane_id(s) == nxt), None)
            if sid is not None:
                self._opening_live_sid = sid
                self.call_after_refresh(lambda: self._focus_live_pane(sid))

        def action_freeze_pane(self) -> None:
            """Shift+F9: freeze / resume the focused live pane so it holds still for
            a Shift+drag copy while claude keeps streaming (any keypress also
            resumes). Without this a streaming pane repaints over your selection."""
            term = self._focused_terminal()
            if term is None and self._live is not None:
                try:
                    active = self.query_one("#right", TabbedContent).active or ""
                    for s in self._live.statuses():
                        if self._live.pane_id(s) == active:
                            term = self._live.get(s)
                            break
                except Exception:
                    term = None
            if term is None or getattr(term, "is_dead", False):
                self.notify("focus a live pane to freeze it", timeout=3)
                return
            frozen = term.toggle_freeze()
            self.notify(
                "pane frozen — Shift+drag to copy · Shift+F9 / type to resume"
                if frozen else "pane resumed", timeout=4)

        def _cycle_tab(self, step: int) -> None:
            if _LIVE_TERM is None:
                return
            try:
                tabs = self.query_one("#right", TabbedContent)
                ids = [p.id for p in tabs.query(TabPane)]
                if not ids:
                    return
                cur = tabs.active or ids[0]
                i = (ids.index(cur) + step) % len(ids) if cur in ids else 0
                tabs.active = ids[i]
                # Focus the terminal if the new tab hosts one, else the list.
                if self._live is not None:
                    for sid in self._live.statuses():
                        if self._live.pane_id(sid) == ids[i]:
                            t = self._live.get(sid)
                            if t is not None:
                                t.focus()
                            return
                self.query_one("#table", DataTable).focus()
            except Exception:
                pass

        def action_toggle_list(self) -> None:
            """F4: hide/show the left session list. Hidden -> the live pane (or
            preview) is full-width and the active live terminal takes focus so
            you can type; shown -> focus returns to the list."""
            main = self.query_one("#main")
            main.toggle_class("nolist")
            if main.has_class("nolist"):
                term = self._focused_terminal()
                if term is None and self._live is not None:
                    try:
                        active = self.query_one("#right", TabbedContent).active or ""
                        for s in self._live.statuses():
                            if self._live.pane_id(s) == active:
                                term = self._live.get(s)
                                break
                    except Exception:
                        term = None
                if term is not None and not getattr(term, "is_dead", False):
                    try:
                        term.focus()        # never focus a dead ✓ pane (keys would vanish)
                    except Exception:
                        pass
            else:
                self.query_one("#table", DataTable).focus()

        def on_data_table_header_selected(self, event) -> None:
            # Excel-like: click a sortable column → 3-state cycle
            # (default-dir → opposite-dir → removed) at priority 1, pushing
            # other priorities down. Non-sortable columns (marker, ID) carry
            # keys starting with "_" and are ignored. Wrapped in try/except so
            # a sort/render failure shows as a toast, never tears the app down.
            try:
                col_key = str(event.column_key.value) if event.column_key else ""
                if not col_key or col_key.startswith("_"):
                    return
                _promote_sort_col(col_key)
                _apply_sort(all_sessions, _load_sort())
                self._refresh_table()
            except Exception as e:
                import traceback
                self.notify(
                    f"sort failed: {e!r}\n{traceback.format_exc()[-400:]}",
                    severity="error", title="saikai", timeout=15,
                )

        def on_select_changed(self, event) -> None:
            # Two Claude-Desktop-like dropdowns (top-right): "Group" (grouping
            # axis) and "Sort" (within-group order). The list is too narrow to
            # sort by clicking column headers, so these drive it.
            sel = getattr(event, "select", None) or getattr(event, "control", None)
            sel_id = getattr(sel, "id", None)
            v = getattr(event, "value", None)
            if sel_id == "groupsel":
                if v in ("none", "date", "project", "state"):
                    if v == _get_group_by():
                        return   # already applied (e.g. action_cycle_group set .value) — no 2nd rebuild
                    if v != "none":   # grouping needs the flat display modes off
                        if _get_tree_mode():
                            _toggle_tree_mode()
                    _set_group_by(v)
                    self._refresh_table()
            elif sel_id == "sortsel":
                if v in ("last", "date", "title"):
                    if v == _sort_select_value():
                        return   # mount echo / re-pick of current primary — no rebuild
                    col, direction = {"last": ("last", "desc"),
                                      "date": ("date", "desc"),
                                      "title": ("title", "asc")}[v]
                    _save_sort([{"col": col, "dir": direction},
                                {"col": "-", "dir": "desc"},
                                {"col": "-", "dir": "desc"}])
                    self._refresh_table()
            elif sel_id == "statussel":
                if v in ("active", "archived", "all"):
                    if v == _get_status_filter():
                        return
                    _set_status_filter(v)
                    self._refresh_table()
            elif sel_id == "lastsel":
                if v in ("0", "1", "3", "7", "30"):
                    if int(v) == _get_lastact_days():
                        return
                    _set_lastact_days(int(v))
                    self._refresh_table()

        def on_input_changed(self, event) -> None:
            # Coalesce keystrokes: a burst of typing collapses to ~one rebuild per
            # frame instead of a full filter+sort+group+render over all sessions
            # PER keystroke. Reuses the proven poll-path coalescer.
            self._request_refresh()

        # ── actions ─────────────────────────────────────────────────────────

        def action_toggle_hide(self) -> None:
            sid = self._cursor_sid()
            if sid:
                try:
                    _toggle_in_set(HIDDEN_FILE, sid)
                except Exception as e:
                    self.notify(f"hide skipped: {e}", severity="error", timeout=6)
                    return
                self._refresh_table()

        def action_toggle_fav(self) -> None:
            sid = self._cursor_sid()
            if sid:
                try:
                    _toggle_in_set(FAVORITE_FILE, sid)
                except Exception as e:
                    self.notify(f"favorite skipped: {e}", severity="error", timeout=6)
                    return
                self._refresh_table()

        def action_rename(self) -> None:
            # A focused live pane owns Shift+F2 (it goes to claude); only rename
            # when the list has focus.
            if self._focused_terminal() is not None:
                raise SkipAction()
            sid = self._cursor_sid()
            if not sid:
                return
            s = self._sid_index.get(sid)
            current = (s or {}).get("custom_title") or ""

            def _save(name) -> None:
                if name is None:
                    return                       # Esc → no change
                try:
                    _set_custom_title(sid, name)
                except Exception as e:
                    self.notify(f"rename failed: {e!r}", severity="error", timeout=6)
                    return
                clean = name.strip()
                # Re-fetch the CURRENT dict: a background reload (SAIKAI_AUTO_REFRESH)
                # while the modal was open would have replaced _sid_index, leaving
                # the closure-captured `s` orphaned (its custom_title would never
                # show). _set_custom_title already persisted to disk; reflect it on
                # whichever dict the list renders now.
                _live_s = self._sid_index.get(sid) or s
                if _live_s is not None:
                    _live_s["custom_title"] = clean    # instant, on the rendered dict
                self._refresh_table()
                # Relabel an open live tab for this session too.
                try:
                    if self._live is not None and self._live.has(sid):
                        tabs = self.query_one("#right", TabbedContent)
                        pane = tabs.get_pane(self._live.pane_id(sid))
                        if pane is not None:
                            title = _pane_title(_live_s, sid, self._live.get(sid))
                            pane.label = _LIVE_TERM.tab_label(title, self._live.status(sid))
                except Exception:
                    pass
                self.notify("name cleared — back to auto-title" if not clean
                            else f"renamed: {clean[:40]}", title="saikai", timeout=3)

            self.push_screen(RenameScreen(current), _save)

        def action_toggle_view(self) -> None:
            _toggle_view_mode()
            self._refresh_table()

        def action_toggle_tree(self) -> None:
            new_on = _toggle_tree_mode()
            # Tree and grouping are mutually exclusive layouts.
            if new_on:
                _set_group_by("none")
            self._refresh_table()

        def _restat_live(self) -> bool:
            """Live panes append to their JSONL as claude works, but last_active_dt
            is memoised at load time — so the Last column / Recency sort FREEZE for
            a session you're actively updating. Re-stat the (few) live sessions and
            bump mtime + last_active_dt when the file grew. Returns True if any
            advanced, so the poll repaints even when the status is unchanged (a
            continuously-busy stream). Busy → Last tracks ~now; once idle it settles
            at when claude finished (mtime stops growing)."""
            if self._live is None:
                return False
            advanced = False
            for sid in list(self._live.statuses().keys()):
                s = self._sid_index.get(sid)
                jp = s.get("jsonl_path") if s else None
                if not jp:
                    continue
                try:
                    _st = jp.stat()
                except Exception:
                    continue
                mt = _st.st_mtime
                if mt > (s.get("mtime") or 0.0):
                    s["mtime"] = mt
                    s["last_active_dt"] = _compute_last_active_dt(s)
                    advanced = True
                    # Fill the Title from claude's OWN data (NO claude -p) while it's
                    # still missing: a just-opened session has no ai-title / first
                    # message yet, so re-extract from the growing JSONL until it has
                    # an ai-title (then it's settled + maybe large → stop). The size
                    # cap keeps the per-poll re-parse cheap (new sessions are tiny).
                    if not s.get("ai_title") and _st.st_size < 2_000_000:
                        try:
                            fresh = parse_session(jp)
                            if fresh:
                                if fresh.get("ai_title"):
                                    s["ai_title"] = fresh["ai_title"]
                                if fresh.get("real_msgs"):
                                    s["real_msgs"] = fresh["real_msgs"]
                        except Exception:
                            pass
            return advanced

        def _poll_live_status(self) -> None:
            # Detect background live panes transitioning into "waiting" (needs
            # input) and toast once per transition; keep the list markers live.
            if self._live is None:
                return
            # Re-classify each pane from its CURRENT screen first, so a pane that
            # went idle WITHOUT new output still flips out of "busy" (and the
            # debounce gets its second tick on this timer cadence).
            for term in self._live.all_terms():
                try:
                    term.refresh_status()
                    # refresh_status marshals its callback via call_from_thread,
                    # which Textual REJECTS from the app's own thread (this poll
                    # runs on it) — so the callback no-ops. Reconcile the manager
                    # dict directly from the term's freshly classified status here,
                    # else a pane that went idle/waiting with no new output never
                    # updates its marker (the whole point of this poll).
                    self._live.set_status(term.sid, getattr(term, "_status", ""))
                except Exception:
                    pass
            try:
                cur = dict(self._live.statuses())
            except Exception:
                return
            prev = self._last_status
            try:
                active = self.query_one("#right", TabbedContent).active or ""
            except Exception:
                active = ""
            for sid, st in cur.items():
                if st == "busy":
                    self._busy_seen.add(sid)   # record even for the active pane
                if self._live.pane_id(sid) == active:
                    # You're looking at this pane — its tab/marker suffice; if it just
                    # settled, drop the "done" debt so switching away later doesn't
                    # toast a finish you already watched.
                    if st != "busy":
                        self._busy_seen.discard(sid)
                    continue
                prev_st = prev.get(sid)
                sess = self._sid_index.get(sid) or {}
                title = (sess.get("ai_title") or _first_msg(sess) or sid[:8])[:50]
                if st == "waiting" and prev_st != "waiting":
                    self.notify(f"needs input: {title}", title="saikai", timeout=8)
                elif st == "idle" and sid in self._busy_seen:
                    # A backgrounded pane just FINISHED its turn (busy→idle) — toast
                    # so you notice WHAT completed without watching every tab. Keyed
                    # on _busy_seen (set by the reader on the busy edge) not a 1-tick
                    # prev snapshot, so a task shorter than the poll still toasts; a
                    # fresh load (→idle) or a y/n answer (waiting→idle, never busy)
                    # has no _busy_seen entry, so it doesn't masquerade as completed.
                    self.notify(f"done: {title}", title="saikai", timeout=6)
                    self._busy_seen.discard(sid)
            # Memory-pressure watch: with panes open, toast ONCE per crossing
            # when system load reaches the gate's ceiling (the open/launch gate
            # already declines new panes — this just tells you why, and to free
            # some with F10). Hysteresis (-5%) re-arms it after load recovers.
            if cur:
                try:
                    _ms = _mem_status()
                    _maxl = float(_ram_gate_kwargs().get("max_load") or 85.0)
                    if _ms is not None and _ms.load is not None:
                        if _ms.load >= _maxl and not self._mem_pressure_warned:
                            self._mem_pressure_warned = True
                            self.notify(
                                f"memory pressure {_ms.load:.0f}% — consider "
                                f"closing panes (F10)",
                                title="saikai", severity="warning", timeout=10)
                        elif _ms.load < _maxl - 5 and self._mem_pressure_warned:
                            self._mem_pressure_warned = False
                except Exception:
                    pass
            advanced = self._restat_live()   # live JSONLs grew → Last / Recency moved
            changed = (cur != prev)
            self._last_status = cur
            if changed or advanced:
                self._request_refresh()

        def action_copy_prompt(self) -> None:
            # F9: copy the selected session's opening user prompt to the
            # clipboard so it can be reused to start a similar task (Crystal-style
            # prompt reuse). On Windows use `clip` (reliable); OSC-52 elsewhere
            # (works over SSH).
            sid = self._cursor_sid()
            if not sid:
                return
            msgs = (self._sid_index.get(sid) or {}).get("real_msgs") or []
            if not msgs:
                self.notify("no user prompt to copy", timeout=3)
                return
            text = msgs[0]
            copied = False
            if sys.platform == "win32":
                # Textual's copy_to_clipboard is OSC-52, which many Windows console
                # hosts silently DROP while never raising — so the old code always
                # claimed success and the `clip` fallback was dead code. Set the
                # clipboard via Win32 CF_UNICODETEXT (codepage-safe: clip.exe under
                # chcp 65001 mis-decoded UTF-16LE bytes → multibyte text garbled);
                # fall back to clip.exe (UTF-8) if that fails.
                try:
                    import saikai_terminal as _rt
                    copied = _rt.set_clipboard_windows(text)
                except Exception:
                    copied = False
                if not copied:
                    try:
                        subprocess.run(["clip"], input=text.encode("utf-8"),
                                       check=True, stdout=subprocess.DEVNULL,
                                       stderr=subprocess.DEVNULL)
                        copied = True
                    except Exception:
                        copied = False
            elif sys.platform == "darwin":
                try:
                    import saikai_terminal as _rt
                    copied = _rt.set_clipboard_macos(text)
                except Exception:
                    copied = False
            if not copied:
                try:
                    self.copy_to_clipboard(text)   # OSC-52: SSH / non-Windows
                except Exception as e:
                    self.notify(f"copy failed: {e!r}", severity="error", timeout=4)
                    return
            self.notify(f"copied opening prompt ({len(text)} chars)", timeout=3)

        def _apply_fresh_sessions(self, fresh) -> None:
            nonlocal all_sessions
            # A re-scan that suddenly finds ZERO sessions while we currently HAVE
            # some is almost always transient (a glob race, a momentarily
            # unreadable projects dir, a project-resolution hiccup) — NOT the user
            # deleting everything. Refuse to clobber a populated list with an empty
            # scan; that was the "all sessions suddenly vanished" bug.
            if not fresh and all_sessions:
                _log(f"reload: 0 sessions returned — KEPT current {len(all_sessions)} (transient?)")
                try:
                    self.notify("re-scan returned 0 sessions — kept the current "
                                "list (likely transient; F5 to retry)",
                                severity="warning", timeout=6)
                except Exception:
                    pass
                return
            # Reassign the session list after a re-scan, but KEEP any live pane
            # whose sid fell out of the fresh scan (filter / --days window) so a
            # running pane doesn't vanish from the list or lose its title.
            if self._live is not None:
                have = {s.get("id") for s in fresh}
                for sid in list(self._live.statuses().keys()):
                    if sid not in have:
                        old = self._sid_index.get(sid)
                        if old is not None:
                            fresh.append(old)
            all_sessions = fresh
            self._sid_index = {s.get("id"): s for s in fresh}

        def _auto_tick(self) -> None:
            # Quiet periodic re-scan (SAIKAI_AUTO_REFRESH). Skips while a live pane
            # is focused so it doesn't disrupt typing into claude. The scan itself
            # — disk walk + parse + O(N^2) _build_forest — runs OFF the UI thread:
            # set_interval fires ON the UI thread, so doing it inline froze the
            # list every interval while the user read. Mirror _build_forest_bg —
            # reload_fn() builds a FRESH local list and touches no shared state, so
            # it's safe off-thread; only the swap (_apply_fresh_sessions mutates
            # all_sessions + repaint) is marshalled back. No PTY/terminal lock is
            # involved, so call_from_thread here can't deadlock. _auto_busy stops
            # overlapping scans if an interval is shorter than a scan.
            if reload_fn is None or self._focused_terminal() is not None:
                return
            if getattr(self, "_auto_busy", False):
                return
            self._auto_busy = True
            _invalidate_active_sessions()   # re-read the live registry, not the launch snapshot
            import threading as _thr

            def _work():
                try:
                    fresh = reload_fn()
                except Exception as e:
                    _log(f"auto-reload failed: {e!r}")
                    fresh = None

                def _apply():
                    self._auto_busy = False
                    if fresh is not None:
                        _log(f"auto-reload: {len(fresh)} sessions")
                        self._apply_fresh_sessions(fresh)
                        self._refresh_table()
                try:
                    if getattr(self, "is_running", True):
                        self.call_from_thread(_apply)
                    else:
                        self._auto_busy = False
                except Exception:
                    self._auto_busy = False

            _thr.Thread(target=_work, daemon=True).start()

        def action_refresh(self) -> None:
            # F5: re-scan ~/.claude/projects for new / updated sessions
            # (saikai loads once at startup; this picks up sessions started
            # elsewhere while the picker is open).
            if reload_fn is None:
                self._refresh_table()
                return
            _invalidate_active_sessions()
            try:
                fresh = reload_fn()
            except Exception as e:
                _log(f"refresh reload failed: {e!r}")
                self.notify(f"refresh failed: {e!r}", severity="error",
                            title="saikai", timeout=6)
                return
            _log(f"refresh reload: {len(fresh)} sessions")
            self._apply_fresh_sessions(fresh)
            self._refresh_table()
            self.notify(f"refreshed — {len(fresh)} sessions", timeout=3)

        def action_cycle_group(self) -> None:
            # Shift-F7 cycles the Claude-Desktop-style grouping: none -> Date ->
            # Project -> none. (The "Group" dropdown sets it explicitly too.)
            new = {"none": "date", "date": "project", "project": "state",
                   "state": "none"}.get(_get_group_by(), "date")
            if new != "none":
                if _get_tree_mode():
                    _toggle_tree_mode()
            _set_group_by(new)
            try:   # keep the dropdown's shown value in sync
                self.query_one("#groupsel").value = new
            except Exception:
                pass
            self._refresh_table()

        def action_preview_full(self) -> None:
            self.preview_mode = "full"
            self._update_preview(self._cursor_sid())

        def action_preview_summary(self) -> None:
            self.preview_mode = "summary"
            self._update_preview(self._cursor_sid())

        def action_preview_changes(self) -> None:
            # F8: show what this session changed (reconstructed from the
            # transcript's Edit/Write records — no git, works for any age).
            self.preview_mode = "changes"
            self._update_preview(self._cursor_sid())

        def action_quit(self) -> None:
            # Esc on the list = quit. With live panes it freezes the WHOLE open set
            # and exits at once (see action_quit_all) — it does NOT close one-by-one.
            # The old one-by-one was a "can't-nuke-everything" guard, but each close
            # dropped a sid from the restore snapshot, so quitting eroded it to empty
            # and Shift+F4 had nothing to reopen. The snapshot + Shift+F4 restore IS
            # the safety net now: an accidental Esc is fully recoverable next launch.
            # (Single-pane close is F10; Ctrl-C also routes here via action_quit_all.)
            # A live terminal normally consumes Esc. If it bubbles (for example
            # from a dead pane), return focus to the list.
            # Esc = "leave the current context": search box → list, dropdown →
            # list, list → quit. The bar is a FIXTURE now (visible by default),
            # so Esc no longer hides it — a single Esc from the list quits, and
            # the filter/query stays applied + visible. ␣/ toggles the bar.
            if isinstance(self.focused, Input):
                try:
                    self.query_one("#table", DataTable).focus()
                except Exception:
                    pass
                return
            if isinstance(self.focused, Select):
                try:
                    self.query_one("#table", DataTable).focus()
                except Exception:
                    pass
                return
            if self._focused_terminal() is not None:
                self.query_one("#table", DataTable).focus()
                return
            if self._live is not None and self._live.count > 0:
                self.action_quit_all()
                return
            if self._live is not None:
                self._live.join_reaps()   # join any reaps from earlier F10 closes
            _log("quit: Esc (no live panes)")
            self.exit(None)

        def action_quit_all(self) -> None:
            # Quit-all (Esc-on-list and Ctrl-C both land here). FREEZE the open-pane
            # set for Shift+F4 restore BEFORE killing — quitting must preserve the
            # working set, not erode it. _opened_sids is still the full set here
            # (Esc no longer closes one-by-one), so this snapshots everything open.
            # Then kill every live claude pane (in PARALLEL) and exit; wait=True
            # joins the reaps so no node worker is orphaned.
            try:
                self._save_open_panes()
            except Exception:
                pass
            _log(f"quit: all, live={self._live.count if self._live else 0}")
            if self._live is not None:
                self._live.kill_all(wait=True)
            self.exit(None)

        def action_toggle_preview(self) -> None:
            # Tab is a priority binding (overrides focus-cycling). On a focused
            # dropdown or the search box, Tab belongs to that widget (move/navigate
            # focus); on a focused live terminal it belongs to claude. SkipAction
            # forwards the key (a plain return would eat it).
            if isinstance(self.focused, (Select, Input)):
                raise SkipAction()
            if self._focused_terminal() is not None:
                raise SkipAction()
            self.preview_mode = "summary" if self.preview_mode == "full" else "full"
            self._update_preview(self._cursor_sid())

        def action_toggle_search_bar(self) -> None:
            """␣/ — show/hide the filter bar (search box + dropdowns). The bar
            is a default-visible fixture; this is the one deliberate way to
            reclaim its rows (Esc no longer hides it). State persists."""
            if self._search_visible():
                self._hide_search()
            else:
                self._open_search()

        def action_settings(self) -> None:
            """␣, — the Settings modal (list options + resolved config). Leader
            entry only fires from the LIST, so no extra focus guard is needed;
            the guard below covers any future direct binding."""
            if self._focused_terminal() is not None or isinstance(self.focused, Input):
                return
            self.push_screen(SettingsScreen())

        def action_mirror_info(self) -> None:
            # F12 — (re)show the web-mirror QR + URL. No-op when the mirror is off.
            _hub = getattr(self, "_mirror_hub", None)
            if _hub is None:
                return
            _url = _hub.url()
            # Copy on EVERY open (not just at startup) so F12 reliably puts the
            # tokened URL on the clipboard; tell the truth if the copy failed.
            _copied = _copy_to_host_clipboard(_url)
            try:
                import saikai_mirror as _m
                self.push_screen(MirrorScreen(_url, _m.qr_matrix(_url), _copied,
                                              _hub.client_count()))
            except Exception:
                self.notify(f"Web mirror: {_url}", title="saikai mirror",
                            timeout=12)

        def action_toggle_mirror_control(self) -> None:
            """Shift+F12 — flip web-mirror interactive control (default OFF). The
            app's _control_enabled is the authority; push the new state + the
            focused-pane title (read HERE on the UI thread) into the hub, which
            keeps an advisory copy and broadcasts a control frame. No-op when the
            mirror is off."""
            _hub = getattr(self, "_mirror_hub", None)
            if _hub is None:
                return
            self._control_enabled = not self._control_enabled
            # Designate a control target only while enabling; disabling clears it
            # (matches the hub's own `target if enabled else None` normalisation).
            t = self._focused_terminal() if self._control_enabled else None
            target = (getattr(t, "title", None) if t is not None else None)
            try:
                _hub.set_control_state(self._control_enabled, target)
            except Exception:
                pass
            if self._control_enabled:
                msg = (f"Mirror control ON — typing into: {target}" if target
                       else "Mirror control ON — no pane focused")
                self.notify(msg, title="saikai mirror", severity="warning",
                            timeout=6)
            else:
                self.notify("Mirror control OFF (read-only)",
                            title="saikai mirror", timeout=4)

        def action_help(self) -> None:
            # '?' is a priority binding; don't pop the help modal over a focused
            # terminal (typing into claude) or the search box (typing a '?' query).
            # SkipAction forwards the key to that widget rather than eating it.
            if self._focused_terminal() is not None or isinstance(self.focused, Input):
                raise SkipAction()
            self.push_screen(HelpScreen())

        def action_cycle_sort(self, priority: str) -> None:
            _cycle_sort_col(int(priority))
            _apply_sort(all_sessions, _load_sort())
            self._refresh_table()

        def action_toggle_dir(self, priority: str) -> None:
            _toggle_sort_dir(int(priority))
            _apply_sort(all_sessions, _load_sort())
            self._refresh_table()

        # Leader-only spellings (no F-key): keyboard parity with a header click.
        def action_sort(self) -> None:
            """Leader `s` — cycle the primary sort column."""
            self.action_cycle_sort("1")

        def action_order(self) -> None:
            """Leader `o` — reverse the primary sort direction."""
            self.action_toggle_dir("1")

        # Keyboard divider (Alt+←/→): same clamp + persistence as a mouse drag.
        # Priority bindings reach here even over a focused pane — forward the
        # key in that case (Alt+arrows may mean something to claude).
        def action_shrink_list(self) -> None:
            self._nudge_split(-0.04)

        def action_grow_list(self) -> None:
            self._nudge_split(+0.04)

        def _nudge_split(self, delta: float) -> None:
            if self._focused_terminal() is not None or isinstance(
                    self.focused, (Select, Input)):
                raise SkipAction()
            self._split_ratio = _nudge_split_ratio(
                getattr(self, "_split_ratio", None) or _get_split_ratio(), delta)
            self._apply_split_ratio(self._split_ratio)
            self._commit_split_ratio()

    # Belt-and-suspenders: even if run()'s teardown is bypassed (SystemExit,
    # driver crash, watchdog), atexit still disables mouse/focus tracking + shows
    # the cursor so the shell isn't left echoing '[I' / stray SGR bytes.
    import atexit
    atexit.register(_reset_terminal_modes)
    # Wrap the app's run() so a Textual / Rich crash never leaves the user
    # at a frozen alternate screen with no way out. On exception: reset
    # terminal modes, leave alternate screen, dump the traceback so we can
    # actually see what blew up (the prior failure was 'screen disappears
    # and doesn't come back' = unrecoverable terminal state).
    try:
        # Web mirror (opt-in, default OFF). Isolated in its own try so that a
        # broken mirror module or a serve() failure (e.g. port already in use)
        # NEVER blocks normal launch — it degrades to "no mirror". The import is
        # guarded by the env flag so users who never opt in don't even load it.
        _hub = None
        _app_kwargs = {}
        if os.environ.get("SAIKAI_MIRROR"):
            try:
                import secrets as _secrets
                import saikai_mirror as _mirror
                _mir_on, _mir_host = _mirror.mirror_config(os.environ)
                if _mir_on:
                    _hub = _mirror.MirrorHub(
                        token=_secrets.token_urlsafe(32), host=_mir_host,
                        port=_mirror.mirror_port(os.environ))
                    # LAN input is its own opt-in: a LAN-exposed mirror stays
                    # read-only unless SAIKAI_MIRROR_ALLOW_LAN_INPUT=1. Loopback
                    # always permits input.
                    _allow_lan_in = str(os.environ.get(
                        "SAIKAI_MIRROR_ALLOW_LAN_INPUT", "")).strip().lower() in (
                        "1", "true", "yes", "on")
                    _hub.allow_lan_input = _allow_lan_in
                    _hub.serve()
                    atexit.register(_hub.stop)
                    _Drv = _mirror.make_mirror_driver(_mirror._base_driver_class(), _hub)
                    _app_kwargs["driver_class"] = _Drv
                    _mode = "LAN-exposed" if _mir_host != "127.0.0.1" else "loopback only"
                    _in_mode = ("input ON" if (_mir_host == "127.0.0.1" or _allow_lan_in)
                                else "input OFF (set SAIKAI_MIRROR_ALLOW_LAN_INPUT=1)")
                    # Persist the URL so it's reachable even though the Textual alt
                    # screen hides this banner during the session; cleaned up at exit.
                    _url_file = CACHE_DIR / "mirror-url.txt"
                    try:
                        # The URL carries the access token, so create the file
                        # owner-only (0600) rather than at the default umask.
                        _url_file.parent.mkdir(parents=True, exist_ok=True)
                        _fd = os.open(str(_url_file),
                                      os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                        with os.fdopen(_fd, "w", encoding="utf-8") as _uf:
                            _uf.write(_hub.url() + "\n")
                        atexit.register(lambda f=_url_file: f.unlink(missing_ok=True))
                    except OSError:
                        _url_file = None
                    print(_c(f"  ⚠ saikai mirror LIVE ({_mode}, {_in_mode}; "
                             f"control default OFF, Shift+F12): {_hub.url()}",
                             YELLOW), file=sys.stderr)
                    if _url_file is not None:
                        print(_c(f"    (also saved to {_url_file})", YELLOW), file=sys.stderr)
            except Exception as _mir_err:   # mirror is best-effort; never block launch
                _hub = None
                _app_kwargs = {}
                print(_c(f"  saikai mirror disabled (setup failed: {_mir_err})",
                         YELLOW), file=sys.stderr)
        _app = PickerApp(**_app_kwargs)
        _app._mirror_hub = _hub
        chosen = _app.run()
    except KeyboardInterrupt:
        chosen = None
    except Exception:
        import traceback
        # Aggressive recovery: leave alternate screen, disable all modes,
        # finally full RIS (Reset to Initial State) as a last resort. Without
        # this the user is stuck at a frozen alternate screen with no input
        # echo — "doesn't come back".
        sys.stderr.write("\033[?1049l")   # leave alternate screen
        _reset_terminal_modes()
        sys.stderr.write("\033c")          # RIS — last-ditch full reset
        sys.stderr.flush()
        log_path = CACHE_DIR / "textual-debug.log"
        print(_c("\n  textual UI crashed:", RED), file=sys.stderr)
        traceback.print_exc()
        print(_c(f"  textual debug log: {log_path}", YELLOW), file=sys.stderr)
        return
    if chosen:
        _resume_claude(chosen, all_sessions)


def _persist_resume_id(full_id: str, target_cwd: str | None) -> Path:
    """Append the resume ID to a TSV history file. Long-term audit + a
    safety net the user can reach via `tail -1` from any terminal after
    the resumed session has crashed. Best-effort; failure never blocks
    the resume."""
    RESUME_HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().astimezone().isoformat(timespec="seconds")
    try:
        with RESUME_HISTORY_FILE.open("a", encoding="utf-8") as f:
            f.write(f"{full_id}\t{ts}\t{target_cwd or ''}\n")
    except OSError:
        pass
    return RESUME_HISTORY_FILE


_SAIKAI_SUPPRESS_PATH = Path.home() / ".claude" / "state" / "_saikai_resume_oneshot.json"
_SAIKAI_SUPPRESS_TTL = 3600.0  # 1h. teams-notify.py 側の SAIKAI_SUPPRESS_TTL と同期


def _add_saikai_suppress_session(session_id: str) -> None:
    """teams-notify.py に「次の Notification 1 件だけ silent」 を伝える 1-shot file.

    `SAIKAI_RESUME=1` env だけだと session lifetime 全体で Notification 抑止に
    なる過去の事故 (2026-05-24 検出) を構造的に防ぐ。 session_id ごとに 1 件
    だけ「最初の idle_prompt 抑止」 を予約する設計。

    pid 付き tmp + os.replace で並行 saikai launch race にも安全。 古い entry
    (>1h) は ついでに prune (= claude が即 crash した stale を回収)。
    """
    import json as _json
    import time as _time
    _SAIKAI_SUPPRESS_PATH.parent.mkdir(parents=True, exist_ok=True)
    state: dict[str, float] = {}
    if _SAIKAI_SUPPRESS_PATH.is_file():
        try:
            data = _json.loads(_SAIKAI_SUPPRESS_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                state = {k: float(v) for k, v in data.items()
                         if isinstance(v, (int, float))}
        except (OSError, _json.JSONDecodeError, ValueError, TypeError):
            state = {}
    now = _time.time()
    state = {k: v for k, v in state.items() if now - v < _SAIKAI_SUPPRESS_TTL}
    state[session_id] = now
    tmp = _SAIKAI_SUPPRESS_PATH.with_suffix(f".{os.getpid()}.tmp")
    try:
        tmp.write_text(_json.dumps(state, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, _SAIKAI_SUPPRESS_PATH)
    except Exception:
        try:                       # don't leave a .<pid>.tmp behind on failure
            tmp.unlink()
        except OSError:
            pass


def _resolve_resume_cwd(full_id: str, sessions: list[dict]) -> str | None:
    """origin_cwd-first cwd resolution for `claude --resume`.

    Try origin_cwd first (where Claude originally indexed the session — required
    for --resume to find it on disk). Fall back to last cwd, then to an existing
    sibling directory under the same projects/<key>/ dir (handles sessions whose
    original cwd was deleted but a sibling/parent still exists). Returns an
    existing directory path, or None.

    This is load-bearing: resuming from the wrong cwd yields
    "No conversation found" for worktree-moved sessions.
    """
    selected = next((s for s in sessions if s["id"] == full_id), None)
    candidates: list[str] = []
    if selected:
        for k in ("origin_cwd", "cwd"):
            v = selected.get(k)
            if v and Path(v).is_dir():
                candidates.append(v)
        if not candidates and selected.get("jsonl_path"):
            project_dir = selected["jsonl_path"].parent
            for other in sessions:
                if other.get("jsonl_path") and other["jsonl_path"].parent == project_dir:
                    v = other.get("origin_cwd") or other.get("cwd")
                    if v and Path(v).is_dir():
                        candidates.append(v)
                        break
    return candidates[0] if candidates else None


def _build_claude_invocation(
    session_args: list[str], target_cwd: str | None, sessions: list[dict]
) -> tuple[list[str], str | None, dict]:
    """Single source of truth for HOW to launch `claude` — resume OR new.

    ``session_args`` is the session selector, e.g. ``['--resume', sid]`` or
    ``['--session-id', uuid]``. Returns ``(argv, cwd, env)``: argv =
    ``[claude_bin, *session_args, *auto_perm]``, cwd = target dir (or None), env =
    a prepared os.environ copy (SAIKAI_RESUME set, ephemeral VIRTUAL_ENV stripped
    from both the var and PATH). NO side effects — no chdir / print / subprocess.
    Shared by resume (_build_resume_invocation) and new (_build_new_invocation) so
    cwd / auto-permission / venv-strip logic can never drift between them.
    """
    # Optional --permission-mode auto for frequent workspaces. Frequency alone
    # is not a trust boundary, so this is disabled unless explicitly opted in.
    extra_args: list[str] = []
    if (_cfg("launch", "auto_permission", "SAIKAI_AUTO_PERMISSION",
             False, _cfg_bool)
            and target_cwd
            and not os.environ.get("SAIKAI_NO_AUTO_PERMISSION")
            and _canonical_workspace(target_cwd) in _frequent_cwds(sessions)):
        extra_args = ["--permission-mode", "auto"]

    env = os.environ.copy()
    env["SAIKAI_RESUME"] = "1"   # signal to teams-notify.py: suppress the first idle_prompt
    # Strip uv's ephemeral VIRTUAL_ENV so the launched session's `uv` doesn't
    # warn about a stale venv.
    leaked_venv = env.pop("VIRTUAL_ENV", None)
    env.pop("VIRTUAL_ENV_PROMPT", None)
    if leaked_venv:
        bin_dir = "Scripts" if sys.platform == "win32" else "bin"
        venv_bin = str(Path(leaked_venv) / bin_dir)
        cmp = (lambda p: p.lower()) if sys.platform == "win32" else (lambda p: p)
        parts = [p for p in env.get("PATH", "").split(os.pathsep) if cmp(p) != cmp(venv_bin)]
        env["PATH"] = os.pathsep.join(parts)

    if len(session_args) == 2 and session_args[0] == "--resume":
        spec = _ACTIVE_PROVIDER.build_resume(
            session_args[1], cwd=target_cwd, env=env, extra_args=extra_args)
    elif len(session_args) == 2 and session_args[0] == "--session-id":
        spec = _ACTIVE_PROVIDER.build_new(
            cwd=target_cwd, requested_id=session_args[1], env=env,
            extra_args=extra_args)
    else:
        raise ValueError(f"unsupported Claude session selector: {session_args!r}")
    return spec.argv, spec.cwd, spec.env


def _build_resume_invocation(
    full_id: str, sessions: list[dict]
) -> tuple[list[str], str | None, dict]:
    """Launch a RESUMED `claude --resume <id>` from its origin cwd. Used by both
    the legacy full-takeover path (_resume_claude) and the split-live pane."""
    target_cwd = _resolve_resume_cwd(full_id, sessions)
    return _build_claude_invocation(["--resume", full_id], target_cwd, sessions)


def _build_new_invocation(
    target_cwd: str, session_id: str, sessions: list[dict]
) -> tuple[list[str], str | None, dict]:
    """Launch a FRESH `claude` in ``target_cwd`` with a pre-assigned
    ``--session-id``, so saikai can key the live pane by that uuid and the new
    session links to its list row once claude writes the JSONL (next scan)."""
    return _build_claude_invocation(["--session-id", session_id], target_cwd, sessions)


def _resume_claude(full_id: str, sessions: list[dict]) -> None:
    """Resume `claude --resume <full_id>` from the right cwd. Self-terminating:
    `sys.exit`s with claude's return code. Shared by every picker frontend so
    cwd resolution / auto-permission / venv strip / terminal reset stay in
    exactly one place (now via _build_resume_invocation)."""
    claude_argv, target_cwd, env = _build_resume_invocation(full_id, sessions)
    if not target_cwd:
        print(_c(f"  warn: session's recorded cwd no longer exists — running from current dir", YELLOW),
              file=sys.stderr)
    auto_perm_note = (_c("  [--permission-mode auto: frequent cwd]", DIM)
                      if "--permission-mode" in claude_argv else "")

    hist_path = _persist_resume_id(full_id, target_cwd)
    print(f"\nResuming {full_id}"
          + (f"  (cwd: {target_cwd})" if target_cwd else "")
          + f"\n  resume ID logged → {hist_path}"
          + (f"\n{auto_perm_note}" if auto_perm_note else ""))
    # 1-shot 抑止 file への session_id 追加. env だけだと session 全体 lifetime で
    # Notification 全件 silent になり、 復帰後の真の入力待ちも消える (2026-05-24
    # 検出 bug)。 file ベースの fine-grain control で「最初の 1 件のみ silent、
    # 以降は通常通知」 を実現する。 (env SAIKAI_RESUME はゲートのみ — 実際の抑止は
    # この file 登録が必須。split-live 経路 3314 と対。これが無いと復帰直後の
    # idle_prompt が毎回 Teams へ誤通知される。)
    try:
        _add_saikai_suppress_session(full_id)
    except Exception:
        pass
    if target_cwd:
        # TOCTOU: directory passed `is_dir()` earlier but could vanish before chdir.
        try:
            os.chdir(target_cwd)
        except OSError as e:
            print(_c(f"  warn: chdir to {target_cwd} failed ({e}); "
                    f"resuming from current dir instead", YELLOW), file=sys.stderr)
            target_cwd = None

    sys.stderr.write(_c("  Loading claude session ...", DIM) + "\n")
    sys.stderr.flush()
    _reset_terminal_modes()

    # Use subprocess.run instead of execvpe so the python parent stays alive
    # until claude exits. On Windows, execvpe replaces the process but the
    # parent chain (uv → cmd → pwsh) sees that exit immediately and unwinds
    # while the orphaned new claude.exe keeps running attached to the pty.
    # That out-of-order unwind makes wezterm's "Process exited" message
    # surface during the live claude session, which is visually broken.
    # Subprocess keeps the call ordering correct: claude exits → python
    # exits → chain unwinds → pwsh's trailing `exit 99` (set in wezterm
    # launch_menu) runs *after* claude is gone, holding the pane open.
    # Leak risk re-introduced here is mitigated by the reap-orphan-claude.py
    # SessionStart hook, which now flags python/uv-parented claude.exe as
    # orphan candidates.
    try:
        rc = subprocess.run(claude_argv, env=env).returncode
    except FileNotFoundError:
        print(_c("  error: claude not on PATH", RED), file=sys.stderr)
        rc = 127
    except KeyboardInterrupt:
        # Ctrl-C while claude is running is a normal user exit — claude
        # already received the signal and is winding down. Don't dump a
        # Python traceback over its shutdown output.
        rc = 130
    finally:
        _reset_terminal_modes()
    sys.exit(rc)


# ── Project lookup ───────────────────────────────────────────────────────────
def _project_dirs(projects_root: Path) -> list[Path]:
    """Return provider history directories, or an empty list before first use."""
    try:
        return [d for d in projects_root.iterdir()
                if d.is_dir() and d.name != "memory"]
    except OSError:
        return []


def _path_to_key(p: Path) -> str:
    s = str(p.resolve()).lower()
    return re.sub(r"[\\/:.\-]+", "-", s).strip("-")


def find_project_dir(cwd: Path) -> Path | None:
    projects_root = PROJECTS_ROOT
    cwd_key = _path_to_key(cwd)
    best, best_len = None, 0
    for cand in _project_dirs(projects_root):
        cand_key = re.sub(r"[\-\.]+", "-", cand.name.lower()).strip("-")
        if cwd_key.endswith(cand_key) or cand_key in cwd_key:
            if len(cand_key) > best_len:
                best, best_len = cand, len(cand_key)
    return best


def _worktree_project_dirs(cwd: Path, projects_root: Path,
                            exclude: Path | None) -> list[tuple[Path, str]]:
    """Return (project_dir, label) for all git worktrees of the repo at *cwd*,
    excluding *exclude*. Label is the worktree basename (e.g. 'feature-x');
    used to populate the TUI 'Wt' column."""
    try:
        r = subprocess.run(
            ["git", "worktree", "list", "--porcelain"],
            capture_output=True, text=True, cwd=cwd, timeout=5,
            creationflags=NO_WINDOW,
        )
        if r.returncode != 0:
            return []
    except Exception:
        return []

    result: list[tuple[Path, str]] = []
    seen: set[Path] = {exclude} if exclude else set()
    for line in r.stdout.splitlines():
        if not line.startswith("worktree "):
            continue
        wt_path = Path(line[len("worktree "):].strip())
        proj = find_project_dir(wt_path)
        if proj and proj not in seen and proj.exists():
            result.append((proj, wt_path.name))
            seen.add(proj)
    return result


# ── Related sessions ─────────────────────────────────────────────────────────
def _norm_cwd(s: str) -> str:
    """Normalize path so mixed separators / case differences (Windows) compare equal."""
    if not s:
        return ""
    return os.path.normcase(os.path.normpath(s))


def _cwd_similarity(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    a, b = _norm_cwd(a), _norm_cwd(b)
    if a == b:
        return 1.0
    # Conservative on prefix matches — siblings under a common parent shouldn't dominate
    sep = os.sep
    if a.startswith(b + sep) or b.startswith(a + sep):
        return 0.5
    return 0.0


def _interval_dts(s: dict) -> tuple:
    """Return (first_dt, last_dt) parsed once and memoised on the session dict.
    Hot path: O(N^2) forest scoring rebuilds these otherwise."""
    cached = s.get("_dts")
    if cached is not None:
        return cached
    try:
        first = datetime.fromisoformat(s["first_ts"].replace("Z", "+00:00"))
        last  = datetime.fromisoformat(s["last_ts"].replace("Z", "+00:00"))
    except Exception:
        first = last = None
    s["_dts"] = (first, last)
    return (first, last)


def _interval_gap_minutes(a: dict, b: dict) -> float:
    """Minutes between two session intervals; 0.0 if they overlapped."""
    af, al = _interval_dts(a)
    bf, bl = _interval_dts(b)
    if af is None or bf is None:
        return float("inf")
    if al < bf:
        return (bf - al).total_seconds() / 60.0
    if bl < af:
        return (af - bl).total_seconds() / 60.0
    return 0.0


def _title_bigrams(s: dict) -> set:
    """Build (and memoise on the session dict) bigrams over title + first msg.
    Cached so O(N²) `_build_forest` doesn't rebuild N-1 times per session."""
    cached = s.get("_bigrams")
    if cached is not None:
        return cached
    text = (s.get("ai_title") or "")
    if s.get("real_msgs"):
        text += " " + s["real_msgs"][0][:200]
    text = text.lower()
    bg = {text[i:i+2] for i in range(len(text)-1)} if len(text) >= 2 else set()
    s["_bigrams"] = bg
    return bg


def _title_similarity(a: dict, b: dict) -> float:
    """Bigram Jaccard on ai_title + first user msg (lowercased)."""
    sa, sb = _title_bigrams(a), _title_bigrams(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


# ── Topic keywords (Option B) ────────────────────────────────────────────────

def _get_cached_topics(sid: str) -> list[str] | None:
    c = _read_json(PARSED_DIR / f"{sid}.json", None)
    return c.get("topics") if isinstance(c, dict) and "topics" in c else None


def _save_topics_to_cache(sid: str, topics: list[str]) -> None:
    cache_file = PARSED_DIR / f"{sid}.json"
    c = _read_json(cache_file, None)
    if not isinstance(c, dict):
        return
    c["topics"] = topics
    try:
        _write_json(cache_file, c)
    except Exception:
        pass


def _extract_topics_haiku(s: dict) -> list[str]:
    """Call Haiku to extract 3-5 topic keywords from a session."""
    title = s.get("ai_title") or ""
    msgs = " | ".join((s.get("real_msgs") or [])[:3])
    if not title and not msgs:
        return []
    prompt = (
        "Extract 3-5 short topic keywords (nouns/noun phrases, lowercase, English) "
        "from this Claude Code session. Reply with ONLY comma-separated keywords, nothing else.\n"
        f"Title: {title}\nMessages: {msgs[:400]}"
    )
    raw = call_claude_haiku(prompt, timeout=30)
    if not raw:
        return []
    return [t.strip().lower() for t in raw.split(",") if t.strip()][:5]


def batch_ensure_topics(sessions: list[dict], show_progress: bool = False) -> None:
    """Populate s['topics'] for all sessions: disk cache first, Haiku for the rest."""
    for s in sessions:
        if "topics" not in s:
            cached = _get_cached_topics(s["id"])
            if cached is not None:
                s["topics"] = cached

    missing = [s for s in sessions if "topics" not in s]
    if not missing:
        return

    if show_progress:
        print(_c(f"  Extracting topics for {len(missing)} sessions via Haiku...", DIM),
              file=sys.stderr)

    done_count = [0]

    def extract_one(s: dict) -> None:
        topics = _extract_topics_haiku(s)
        s["topics"] = topics if topics else []
        if topics:
            _save_topics_to_cache(s["id"], topics)
        done_count[0] += 1
        if show_progress and done_count[0] % 10 == 0:
            print(f"\r  [{done_count[0]}/{len(missing)}] ", end="", file=sys.stderr, flush=True)

    with ThreadPoolExecutor(max_workers=5) as ex:
        list(ex.map(extract_one, missing))

    if show_progress:
        print(file=sys.stderr)


def _topic_similarity(a: dict, b: dict) -> float:
    """Jaccard similarity of pre-loaded topic keyword sets."""
    ta = set(a.get("topics") or [])
    tb = set(b.get("topics") or [])
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


# Structural-similarity weights (must sum to 1.0). Tuned so cross-cwd same-branch
# pairs still surface at "med" tier when paired with strong topic overlap.
_W_CWD, _W_BRANCH, _W_TITLE, _W_TOPIC = 0.45, 0.35, 0.10, 0.10
_TIME_TAU_MIN = 4320.0  # 3 days; exp decay constant for the time factor
RELATION_FLOOR = 0.20    # min score to qualify in --related and --tree
RELATION_TOP_K = 50      # max candidates to extract topics for in --related


def _fmt_gap(gap_min: float) -> str | None:
    if gap_min == 0.0:
        return _c("⚠ concurrent", YELLOW)
    if gap_min == float("inf"):
        return None
    if gap_min < 60:
        return f"{int(gap_min)}m gap"
    if gap_min < 60 * 24:
        return f"{gap_min/60:.1f}h gap"
    return f"{int(gap_min/(60*24))}d gap"


# Branch names that identify NO particular strand of work: every repo has a
# main/master, and detached HEAD records the literal string "HEAD" — two
# unrelated sessions matching on any of these is the base rate, not evidence.
# A shared FEATURE branch, by contrast, is the strongest structural signal
# saikai has that one session continues another's work.
_NO_INFO_BRANCHES = frozenset({"", "HEAD", "main", "master"})


def _structural_components(a: dict, b: dict) -> dict:
    """Cheap pairwise similarity components (no Haiku required for topic_s=0).
    Reused by _score_relation / _build_forest and cmd_related's prefilter so
    the formula lives in one place."""
    ab, bb = a.get("git_branch", ""), b.get("git_branch", "")
    branch_s = 1.0 if (ab not in _NO_INFO_BRANCHES and ab == bb) else 0.0
    return {
        "cwd_s":    _cwd_similarity(a.get("cwd", ""), b.get("cwd", "")),
        "branch_s": branch_s,
        "title_s":  _title_similarity(a, b),
        "gap_min":  _interval_gap_minutes(a, b),
    }


def _relation_metrics(target: dict, other: dict) -> dict:
    """Full pairwise metrics: structural components + topic similarity +
    time-damped combined score. Single source for _score_relation (--related)
    and _build_forest (--tree) so the two features can't disagree."""
    c = _structural_components(target, other)
    c["topic_s"] = _topic_similarity(target, other)
    time_s = (math.exp(-c["gap_min"] / _TIME_TAU_MIN)
              if c["gap_min"] != float("inf") else 0.0)
    structural = (_W_CWD*c["cwd_s"] + _W_BRANCH*c["branch_s"]
                  + _W_TITLE*c["title_s"] + _W_TOPIC*c["topic_s"])
    # Time factor: small floor keeps far-past matches discoverable but heavily damped
    c["time_factor"] = 0.10 + 0.90 * time_s
    c["score"] = structural * c["time_factor"]
    return c


# Content-evidence gate for PARENT assignment (tree mode only — --related has
# no gate because "same repo, recently" IS a useful related-list entry).
# Same-cwd + recency alone must never imply parentage: in a single-repo
# history every session matches every earlier one on cwd, and the time decay
# then picks the nearest predecessor — the "tree" degenerates into one long
# bogus chain of unrelated sessions. Require at least one signal that the
# WORK is continuous: a shared feature branch, or textual/topical overlap.
_PARENT_MIN_TITLE_S = 0.25
_PARENT_MIN_TOPIC_S = 0.25


def _qualifies_as_parent(m: dict) -> bool:
    """True iff the metrics carry continuation evidence beyond location+time."""
    return (m["branch_s"] >= 1.0
            or m["title_s"] >= _PARENT_MIN_TITLE_S
            or m["topic_s"] >= _PARENT_MIN_TOPIC_S)


def _score_relation(target: dict, other: dict) -> tuple[float, list[str]]:
    m = _relation_metrics(target, other)
    reasons: list[str] = []
    if m["cwd_s"] == 1.0:
        reasons.append("same cwd")
    elif m["cwd_s"] >= 0.7:
        reasons.append("same project")
    if m["branch_s"] == 1.0:
        reasons.append(f"branch {other.get('git_branch','')}")
    gap_label = _fmt_gap(m["gap_min"])
    if gap_label:
        reasons.append(gap_label)
    if m["title_s"] >= 0.3:
        reasons.append(f"title sim {m['title_s']:.0%}")
    if m["topic_s"] >= 0.3:
        reasons.append(f"topic sim {m['topic_s']:.0%}")
    return (m["score"], reasons)


def _confidence_marker(score: float) -> str:
    if score >= 0.7:
        return _c("●", GREEN)
    if score >= 0.4:
        return _c("●", YELLOW)
    if score >= 0.2:
        return _c("○", GRAY)
    return " "


def cmd_related(target_id_prefix: str, sessions: list[dict]) -> None:
    target_id_prefix = _trim_sid(target_id_prefix)
    target = next((s for s in sessions if s["id"].startswith(target_id_prefix)), None)
    if not target:
        print(f"(session {target_id_prefix[:8]} not found in current scope)", file=sys.stderr)
        sys.exit(1)

    tb = target.get("git_branch", "")
    branch_label = "(detached HEAD)" if tb == "HEAD" else (tb or "(none)")
    print(_c("Target:  ", BOLD) + f"{short_id(target['id'])}  {label_for(target)}")
    print(f"  cwd:    {target.get('cwd','') or '(none)'}")
    print(f"  branch: {branch_label}")
    print(f"  time:   {fmt_ts(target['first_ts'])} → {fmt_ts(target['last_ts'])}")
    print()

    # Prefilter: cheap structural-only score (no topic). Drop sessions whose
    # best-case score (assuming perfect topic match) cannot clear the floor;
    # pay for Haiku topic extraction only on the top-K survivors.
    prefiltered: list[tuple[dict, float]] = []
    for s in sessions:
        if s["id"] == target["id"]:
            continue
        c = _structural_components(target, s)
        time_s = math.exp(-c["gap_min"] / _TIME_TAU_MIN) if c["gap_min"] != float("inf") else 0.0
        struct_no_topic = _W_CWD*c["cwd_s"] + _W_BRANCH*c["branch_s"] + _W_TITLE*c["title_s"]
        time_factor = 0.10 + 0.90 * time_s
        max_possible = (struct_no_topic + _W_TOPIC) * time_factor
        if max_possible >= RELATION_FLOOR:
            prefiltered.append((s, max_possible))

    prefiltered.sort(key=lambda x: -x[1])
    top_candidates = [s for s, _ in prefiltered[:RELATION_TOP_K]]
    if top_candidates:
        batch_ensure_topics([target] + top_candidates, show_progress=True)

    candidates: list[tuple[dict, float, list[str]]] = []
    for s in top_candidates:
        score, reasons = _score_relation(target, s)
        if score >= RELATION_FLOOR:
            candidates.append((s, score, reasons))
    candidates.sort(key=lambda x: -x[1])

    if not candidates:
        print(_c(f"(no related sessions found above confidence floor {RELATION_FLOOR:.2f})", GRAY))
        return

    print(_c(f"Related ({len(candidates)} candidates, sorted by score):", BOLD))
    print(_c("  " + _c("●", GREEN) + " high (≥0.70)   " +
            _c("●", YELLOW) + " med (≥0.40)   " +
            _c("○", GRAY) + " low (≥0.20)", DIM))
    print()
    for s, score, reasons in candidates[:20]:
        marker = _confidence_marker(score)
        sid8 = short_id(s["id"])
        start = fmt_ts(s["first_ts"])
        title = truncate_visual(label_for(s) or "(empty)", 50)
        print(f" {marker} [{score:.2f}]  {start}  {_c(sid8, YELLOW)}  {title}")
        if reasons:
            print(f"          {_c(' · '.join(reasons), GRAY)}")
    if len(candidates) > 20:
        print()
        print(_c(f"  ... and {len(candidates)-20} more (showing top 20)", DIM))


# ── In-session sidechain tree ────────────────────────────────────────────────
def _read_subagent_summary(agent_file: Path) -> dict:
    """Return {agent_id, n_msgs, first_user, last_assistant, first_ts, last_ts}
    for a single subagent JSONL. All sidechain messages have isSidechain=true."""
    agent_id = agent_file.stem.replace("agent-", "")
    summary = {
        "agent_id": agent_id, "n_msgs": 0,
        "first_user": "", "last_assistant": "",
        "first_ts": "", "last_ts": "",
    }
    try:
        with open(agent_file, "rb") as f:
            for line in f:
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                summary["n_msgs"] += 1
                ts = obj.get("timestamp") or ""
                if ts and not summary["first_ts"]:
                    summary["first_ts"] = ts
                if ts:
                    summary["last_ts"] = ts
                t = obj.get("type", "")
                text = _extract_text((obj.get("message") or {}).get("content", "")) or ""
                text = " ".join(text.split())
                if t == "user" and not summary["first_user"]:
                    summary["first_user"] = text
                elif t == "assistant" and text:
                    summary["last_assistant"] = text
    except Exception:
        pass
    return summary


def cmd_sidechain_tree(target_id_prefix: str) -> None:
    """Show subagent (sidechain) branches for a single session.

    Subagents spawned via the Task tool persist to
    `<project>/<sid>/subagents/agent-<agentId>.jsonl` + `.meta.json`.
    Each file is a branch — we list them with agentType, description, prompt,
    and last reply. Uses confirmed metadata only (no heuristic scoring)."""
    sid = _trim_sid(target_id_prefix)
    jsonl = _find_session_jsonl(sid)
    if not jsonl:
        print(f"(session {sid[:8]} not found)", file=sys.stderr)
        sys.exit(1)

    full_sid = jsonl.stem
    subagents_dir = jsonl.parent / full_sid / "subagents"
    agent_files = sorted(subagents_dir.glob("agent-*.jsonl")) if subagents_dir.exists() else []

    print(_c("Sidechain (subagent) tree:", BOLD) + f"  {short_id(full_sid)}")
    print(f"  jsonl:    {jsonl}")
    print(f"  agents:   {len(agent_files)}")
    print()

    if not agent_files:
        print(_c("(no subagent invocations found — session has no Task-tool calls)", GRAY))
        return

    # Order branches by first timestamp so the tree reads chronologically.
    summaries = [(af, _read_subagent_summary(af)) for af in agent_files]
    summaries.sort(key=lambda x: x[1]["first_ts"] or "")

    for i, (agent_file, summary) in enumerate(summaries):
        meta_file = agent_file.with_suffix(".meta.json")
        meta = {}
        if meta_file.exists():
            try:
                meta = json.loads(meta_file.read_text(encoding="utf-8"))
            except Exception:
                pass
        agent_type = meta.get("agentType", "?")
        description = meta.get("description", "")

        is_last = (i == len(summaries) - 1)
        # ASCII glyphs (same rationale as _tree_walk) for terminal-width safety
        branch = "\\- " if is_last else "+- "
        cont = "   " if is_last else "|  "
        ts = (summary["first_ts"] or "")[:19].replace("T", " ")
        head = (_c(branch, GRAY) + _c(agent_type, CYAN, BOLD) +
                _c(f"  ({summary['agent_id'][:8]}, {summary['n_msgs']} msgs, {ts})", DIM))
        print(head)
        if description:
            print(_c(cont + "  description: ", DIM) + description[:120])
        if summary["first_user"]:
            print(_c(cont + "  prompt:      ", DIM) + summary["first_user"][:120])
        if summary["last_assistant"]:
            print(_c(cont + "  last reply:  ", DIM) + summary["last_assistant"][:120])
        if not is_last:
            print(_c(cont, GRAY))


# ── Forest building ──────────────────────────────────────────────────────────
def _build_forest(sessions: list[dict], floor: float = 0.20) -> None:
    """Mutates sessions in place: assigns each its highest-scoring earlier session as parent.
    Adds keys: parent_id (or None), parent_score (0.0 if root), parent_reasons (list).

    A candidate must pass _qualifies_as_parent (shared feature branch, or
    title/topic overlap) BEFORE its score counts: same-cwd + recency alone
    matches every earlier session in a single-repo history and would chain
    them all into one bogus linked list. Sessions without continuation
    evidence stay roots — a mostly-flat tree is the truthful answer for a
    history of independent tasks.

    Topics are NOT batch-extracted here — for N up to 1000 sessions that would
    be a 30+ minute Haiku call. _topic_similarity returns 0 for missing topics,
    so the forest still builds on cwd/branch/title. Run --related <sid> to get
    topic-aware scoring on demand."""
    by_time = sorted(sessions, key=lambda s: s["first_ts"])
    max_struct = _W_CWD + _W_BRANCH + _W_TITLE + _W_TOPIC   # ceiling of `structural`
    for i, s in enumerate(by_time):
        best_score, best_parent = floor, None
        for j in range(i):
            p = by_time[j]
            # Exact prune: score = structural * time_factor, and structural is
            # bounded by max_struct, so if the time-damped ceiling can't beat the
            # current best there is no point paying for the cwd/title/topic
            # scoring. gap uses the memoised _interval_dts so this check is O(1);
            # it never drops a pair that could actually win (>, not >=).
            gap = _interval_gap_minutes(s, p)
            tf = 0.10 + 0.90 * (math.exp(-gap / _TIME_TAU_MIN)
                                if gap != float("inf") else 0.0)
            if max_struct * tf <= best_score:
                continue
            m = _relation_metrics(s, p)
            if m["score"] > best_score and _qualifies_as_parent(m):
                best_score, best_parent = m["score"], p
        s["parent_id"] = best_parent["id"] if best_parent else None
        s["parent_score"] = best_score if best_parent else 0.0
        s["parent_reasons"] = (_score_relation(s, best_parent)[1]
                               if best_parent else [])


def _tree_walk(sessions: list[dict]) -> list[tuple[dict, str]]:
    """Pre-order traversal returning [(session, ansi_prefix), ...].
    Trees with the newest descendant float to the top; siblings ordered newest-first."""
    by_id = {s["id"]: s for s in sessions}
    children: dict[str, list[str]] = {}
    roots: list[str] = []
    for s in sessions:
        pid = s.get("parent_id")
        if pid and pid in by_id:
            children.setdefault(pid, []).append(s["id"])
        else:
            roots.append(s["id"])

    newest_cache: dict[str, str] = {}
    def newest_in_tree(sid: str) -> str:
        if sid in newest_cache:
            return newest_cache[sid]
        ts = by_id[sid]["first_ts"]
        for c in children.get(sid, []):
            ct = newest_in_tree(c)
            if ct > ts:
                ts = ct
        newest_cache[sid] = ts
        return ts

    out: list[tuple[dict, str]] = []

    # Tree glyphs are ASCII (`| \-  +- \.`) instead of box-drawing `│ └─ ├─ └┄`
    # so the prefix has predictable 1-cell-per-char width. Box-drawing chars
    # are East-Asian-Ambiguous and WezTerm/Windows Terminal render them at 1
    # or 2 cells depending on `cjk_width` settings — which saikai can't probe,
    # so deep tree branches drifted by 1 cell per level when the terminal
    # disagreed with our static assumption.
    def walk(sid: str, prefix: str, is_last: bool):
        s = by_id.get(sid)
        if not s:
            return
        if prefix:
            base = "\\-" if is_last else "+-"
            score = s.get("parent_score", 0.0)
            if score >= 0.7:
                glyph = _c(base, GREEN)
            elif score >= 0.4:
                glyph = _c(base, YELLOW)
            else:
                # weak parent link: low-confidence glyph (dot instead of dash)
                glyph = _c(base[0] + ".", GRAY)
            node_prefix = prefix + glyph + " "
        else:
            node_prefix = ""
        out.append((s, node_prefix))
        kids = sorted(children.get(sid, []), key=newest_in_tree, reverse=True)
        for i, kid in enumerate(kids):
            cont = "   " if is_last else "|  "
            walk(kid, prefix + cont, i == len(kids) - 1)

    # newest_in_tree / walk recurse once per tree DEPTH; a long single-project
    # parent chain can be ~len(sessions) deep and overflow Python's default 1000
    # limit (RecursionError → CLI crash in --table --tree, empty tree + toast in
    # the picker). Raise the limit for the build (gated at N<=1000, so bounded).
    _old_limit = sys.getrecursionlimit()
    sys.setrecursionlimit(max(_old_limit, len(sessions) + 200))
    try:
        roots.sort(key=newest_in_tree, reverse=True)
        for i, root in enumerate(roots):
            walk(root, "", i == len(roots) - 1)
    finally:
        sys.setrecursionlimit(_old_limit)
    return out


# ── Claude Desktop session-list sync ─────────────────────────────────────────
# Claude Desktop builds its session picker from
#   %APPDATA%/Claude/claude-code-sessions/<org>/<user>/local_<uuid>.json
# entries, each linking to a ~/.claude/projects JSONL via `cliSessionId`. Sessions
# started in the terminal / VS Code after Desktop's one-time import have no such
# entry, so Desktop doesn't list them. cmd_sync_desktop ADDITIVELY creates the
# missing entries — it never touches ~/.claude/projects canonical history.
DESKTOP_SESSIONS_ROOT = Path.home() / "AppData" / "Roaming" / "Claude" / "claude-code-sessions"


def _desktop_index_dir() -> Path | None:
    """The <org>/<user> dir holding Desktop's local_*.json session entries."""
    if not DESKTOP_SESSIONS_ROOT.exists():
        return None
    locs = list(DESKTOP_SESSIONS_ROOT.rglob("local_*.json"))
    if not locs:
        return None
    # the dir with the most entries is the active org/user
    return Counter(p.parent for p in locs).most_common(1)[0][0]


def _iso_to_ms(s) -> int:
    try:
        return int(datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp() * 1000)
    except Exception:
        return 0


def _session_surface_model(jsonl: Path) -> tuple:
    """Read entrypoint (surface) and last assistant model from a session JSONL."""
    ep = model = None
    try:
        with open(jsonl, "rb") as f:
            for line in f:
                try:
                    o = json.loads(line)
                except Exception:
                    continue
                if ep is None and "entrypoint" in o:
                    ep = o["entrypoint"]
                if o.get("type") == "assistant":
                    m = (o.get("message") or {}).get("model")
                    if m:
                        model = m
    except Exception:
        pass
    return ep, model


def cmd_sync_desktop() -> None:
    """Surface Terminal/VS Code sessions in Claude Desktop's session list.

    Additive (writes only new local_<uuid>.json entries into Desktop's own store)
    and idempotent (sessions already linked by cliSessionId are skipped).
    """
    idx = _desktop_index_dir()
    if idx is None:
        print(_c("  Claude Desktop session store not found "
                 "(is Desktop installed and has it run once?)", YELLOW), file=sys.stderr)
        return
    if sys.platform == "win32":
        try:
            # bytes (no text decode): tasklist emits the console OEM codepage
            # (e.g. CP932 on JP Windows), which is not valid UTF-8.
            r = subprocess.run(["tasklist", "/FI", "IMAGENAME eq Claude.exe"],
                               capture_output=True, creationflags=NO_WINDOW)
            if b"Claude.exe" in (r.stdout or b""):
                print(_c("  note: Claude Desktop appears to be running — restart it "
                         "afterwards to see the new sessions.", YELLOW), file=sys.stderr)
        except Exception:
            pass
    known = set()
    for p in idx.glob("local_*.json"):
        c = _read_json(p, {}).get("cliSessionId")
        if c:
            known.add(c)
    created = skipped = 0
    for d in _project_dirs(PROJECTS_ROOT):
        for s in load_sessions_in_dir(d, None):
            sid = s["id"]
            if sid in known:
                skipped += 1
                continue
            jsonl = s.get("jsonl_path")
            if not jsonl:
                continue
            surface, model = _session_surface_model(jsonl)
            if surface not in ("cli", "claude-vscode"):
                continue   # only Terminal / VS Code sessions
            cwd = s.get("cwd") or s.get("origin_cwd") or str(Path.home())
            title = (s.get("ai_title")
                     or (s["real_msgs"][0] if s.get("real_msgs") else "")
                     or f"({sid[:8]})")[:80]
            created_ms = _iso_to_ms(s.get("first_ts"))
            # last activity = the later of last-message ts and file mtime, matching
            # saikai's Recency column (untimed ai-title/permission-mode appends bump
            # mtime but not last_ts) so Desktop orders these sessions the same way.
            active_ms = (max(_iso_to_ms(s.get("last_ts")),
                             int((s.get("mtime") or 0) * 1000))
                         or created_ms)
            entry = {
                "sessionId": "local_" + str(uuid.uuid4()),
                "cliSessionId": sid,
                "cwd": cwd,
                "originCwd": s.get("origin_cwd") or cwd,
                "createdAt": created_ms,
                "lastActivityAt": active_ms,
                "lastFocusedAt": active_ms,
                "model": model or "claude-opus-4-8",
                "effort": "max",
                "isArchived": False,
                "title": title,
                "titleSource": "user",
                "permissionMode": "default",
                "enabledMcpTools": {},
                "remoteMcpServersConfig": [],
                "chromePermissionMode": "skip_all_permission_checks",
                "completedTurns": 0,
                "alwaysAllowedReasons": [],
                "sessionPermissionUpdates": [],
                "classifierSummaryEnabled": True,
            }
            try:
                _write_json(idx / (entry["sessionId"] + ".json"), entry)
                created += 1
            except Exception as e:
                print(_c(f"  failed writing entry for {sid[:8]}: {e}", RED), file=sys.stderr)
    print(_c(f"  Claude Desktop sync: +{created} new, {skipped} already present.",
             GREEN), file=sys.stderr)
    if created:
        print(_c("  Restart Claude Desktop to see the new sessions.", DIM), file=sys.stderr)


# ── Main ─────────────────────────────────────────────────────────────────────
def main():
    p = argparse.ArgumentParser(
        description="Claude Code session history viewer. Shows all history by factory default; "
                    "use --days N to limit. Filters (--days/--here/--all) are one-shot "
                    "unless --save-defaults is also passed.",
        epilog="Environment variables:\n"
               "  SAIKAI_RESUME=1            set on the resumed `claude` child so your own\n"
               "                            hooks can tell saikai-resumed sessions apart\n"
               "                            (e.g. suppress idle notifications).\n"
               "  SAIKAI_AUTO_PERMISSION=1   opt in to adding --permission-mode auto when\n"
               "                            the target cwd is frequent.\n"
               "  SAIKAI_NO_AUTO_PERMISSION  hard-disable auto-permission even if enabled.\n"
               "  SAIKAI_FREQ_CWD_MIN=N      minimum session count to flag a cwd as\n"
               "                            \"frequent\" for auto-permission (default 5).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--version", action="version", version=f"saikai {__version__}")
    p.add_argument("--init-config", action="store_true",
                   help="Write a commented config.toml template to the config path, then exit.")
    p.add_argument("--print-config", action="store_true",
                   help="Print the resolved settings + their source (default/config/env), then exit.")
    p.add_argument("--force", action="store_true",
                   help="With --init-config: overwrite an existing config file.")
    # default=None on persisted flags so we can detect "not provided" and use
    # the last saved value instead.
    def _nonneg_int(v: str) -> int:
        n = int(v)
        if n < 0:
            raise argparse.ArgumentTypeError(f"--days must be >= 0 (got {n})")
        return n
    p.add_argument("--days", type=_nonneg_int, default=None, metavar="N",
                   help="Limit to the last N days. Omit (or pass 0) for full history. "
                        "One-shot unless --save-defaults is also passed.")
    p.add_argument("--here", "--this-project-only", action="store_true",
                   default=None, dest="here",
                   help="Show only sessions for the current project")
    p.add_argument("--all", "--all-projects", action="store_true",
                   default=None, dest="all_scope",
                   help="Show sessions across all projects")
    p.add_argument("--reset-options", action="store_true",
                   help="Forget saved --days/--here/--all defaults. Preserves "
                        "split ratio and filter-bar visibility. Does NOT clear "
                        "hidden/favorite/view-mode/tree-mode/sort — "
                        "toggle those via F7 / F6 / Shift-F5 in the "
                        "picker, ':hidden' in search for hidden rows, a column-header "
                        "click to sort (or the matching "
                        "--toggle-* / --cycle-sort / --reset-sort flags).")
    p.add_argument("--save-defaults", action="store_true",
                   help="Persist the current --days/--here/--all values as new defaults. "
                        "Without this flag, CLI args are one-shot and saved options stay untouched.")
    p.add_argument("--pick", action="store_true",
                   help="Open the interactive picker. This is the default when "
                        "no other action flag is given; --pick is kept as an explicit "
                        "no-op for clarity in shell aliases.")
    p.add_argument("--table", action="store_true",
                   help="Show static table instead of the interactive picker")
    p.add_argument("--project", metavar="PATH")
    p.add_argument("--no-summary", action="store_true",
                   help="Skip Haiku summarization (use AI title or first user msg)")
    p.add_argument("--refresh-summary", action="store_true",
                   help="Discard cached Haiku summaries and regenerate. Does NOT touch "
                        "parsed/topic caches; delete ~/.cache/saikai/parsed/ for that.")
    p.add_argument("--preview", metavar="SESSION_ID",
                   help="Print a session's content preview")
    p.add_argument("--preview-full", metavar="SESSION_ID",
                   help="Print a session's full conversation preview")
    p.add_argument("--hide", metavar="SESSION_ID",
                   help="Toggle hidden state for a session")
    p.add_argument("--favorite", metavar="SESSION_ID",
                   help="Toggle favorite (★) state for a session")
    p.add_argument("--fav-current", action="store_true",
                   help="Mark the current Claude Code session as favorite. "
                        "Resolves the session ID from $CLAUDE_SESSION_ID, "
                        "falling back to the most-recently-modified JSONL "
                        "in this project's encoded directory.")
    p.add_argument("--toggle-view", action="store_true",
                   help="Toggle saved default/show-hidden view mode (persistent).")
    p.add_argument("--toggle-tree", action="store_true",
                   help="Toggle saved flat/nested tree-display mode (persistent). "
                        "Same effect as Shift-F5 inside the picker.")
    p.add_argument("--cycle-sort", type=int, metavar="N", choices=[1, 2, 3],
                   help="Advance the Nth sort priority to the next column. Persistent. "
                        "In the picker, click a column header instead.")
    p.add_argument("--toggle-sort-dir", type=int, metavar="N", choices=[1, 2, 3],
                   help="Toggle the Nth sort priority's direction (asc/desc). Persistent. "
                        "In the picker, click a sorted column header again to reverse.")
    p.add_argument("--reset-sort", action="store_true",
                   help="Reset all sort priorities to defaults (recency desc, then none).")
    p.add_argument("--sync-desktop", action="store_true",
                   help="Create Claude Desktop session-list entries for Terminal/VS Code "
                        "sessions missing from it. Additive + idempotent; never modifies "
                        "~/.claude/projects. Restart Desktop afterwards to see them.")
    p.add_argument("--related", metavar="SESSION_ID",
                   help="Show sessions related to SESSION_ID with confidence scores and reasons")
    p.add_argument("--tree", action="store_true",
                   help="Group sessions into an inferred parent/child forest (heuristic, "
                        "scores cwd / branch / title / topic + time decay).")
    p.add_argument("--sidechain", metavar="SESSION_ID",
                   help="Show the in-session sidechain (subagent) tree for SESSION_ID "
                        "using isSidechain+parentUuid metadata (confirmed, not heuristic).")
    args = p.parse_args()

    if args.init_config:
        sys.exit(_init_config(force=args.force))
    if args.print_config:
        sys.exit(_print_config())

    if args.preview:
        preview_session(args.preview)
        return
    if args.preview_full:
        preview_session_full(args.preview_full)
        return
    if args.hide:
        sid = _trim_sid(args.hide)
        now_hidden = _toggle_in_set(HIDDEN_FILE, sid)
        state = _c("HIDDEN", GRAY) if now_hidden else _c("visible", GREEN)
        print(f"  {sid[:8]}: {state}", file=sys.stderr)
        return
    if args.favorite:
        sid = _trim_sid(args.favorite)
        now_fav = _toggle_in_set(FAVORITE_FILE, sid)
        state = _c("* favorite", YELLOW) if now_fav else _c("not favorite", GRAY)
        print(f"  {sid[:8]}: {state}", file=sys.stderr)
        return
    if args.fav_current:
        sid = (os.environ.get("CLAUDE_SESSION_ID") or "").strip()
        if not sid:
            # Fall back: most-recently-modified JSONL in this project's
            # encoded dir. That's the session being written to *now* — i.e.
            # the one the user is in. Stable across slash-command and shell
            # invocations where the env var isn't propagated.
            proj = find_project_dir(Path.cwd())
            if proj and proj.exists():
                def _mt(p):   # claude may rotate/remove a transcript between glob+stat
                    try:
                        return p.stat().st_mtime
                    except OSError:
                        return 0.0
                jsonls = sorted(proj.glob("*.jsonl"), key=_mt, reverse=True)
                if jsonls:
                    sid = jsonls[0].stem
        if not sid:
            print(_c("  no current session found "
                     "(set CLAUDE_SESSION_ID or run inside a project)", RED),
                  file=sys.stderr)
            sys.exit(1)
        now_fav = _toggle_in_set(FAVORITE_FILE, sid)
        state = _c("* favorite", YELLOW) if now_fav else _c("not favorite", GRAY)
        print(f"  {sid[:8]}: {state}", file=sys.stderr)
        return
    if args.toggle_view:
        new_mode = _toggle_view_mode()
        color = YELLOW if new_mode == "show-hidden" else GREEN
        print(f"  view-mode: {_c(new_mode, color)}", file=sys.stderr)
        return
    if args.toggle_tree:
        new_on = _toggle_tree_mode()
        label = "nested (tree)" if new_on else "flat"
        print(f"  tree-mode: {_c(label, YELLOW if new_on else GREEN)}", file=sys.stderr)
        return
    if args.cycle_sort is not None:
        entry = _cycle_sort_col(args.cycle_sort)
        print(f"  sort[{args.cycle_sort}]: {_c(entry['col'], CYAN)} {entry['dir']}",
              file=sys.stderr)
        return
    if args.toggle_sort_dir is not None:
        entry = _toggle_sort_dir(args.toggle_sort_dir)
        print(f"  sort[{args.toggle_sort_dir}]: {entry['col']} "
              f"{_c(entry['dir'], CYAN)}", file=sys.stderr)
        return
    if args.reset_sort:
        _reset_sort()
        print(_c("  sort: reset to defaults (recency desc)", GREEN), file=sys.stderr)
        return
    if args.sync_desktop:
        cmd_sync_desktop()
        return
    # --sidechain SID: in-session subagent tree. Handled here (no need to load
    # all sessions across the project — we open the target JSONL directly).
    if args.sidechain:
        cmd_sidechain_tree(args.sidechain)
        return

    if args.reset_options:
        _reset_saved_cli_options()
        print("Saved options cleared.", file=sys.stderr)
        return

    # --related needs cross-project scope so the target can be found wherever it lives.
    # Warn if the user explicitly asked for here/project — those would be silently
    # overridden otherwise and the result wouldn't match expectations.
    if args.related:
        if args.here:
            print(_c("  note: --related forces cross-project scope; --here ignored",
                    YELLOW), file=sys.stderr)
        if args.project:
            print(_c("  note: --related searches across all projects; --project ignored",
                    YELLOW), file=sys.stderr)
        args.all_scope = True
        args.here = False

    # Resolve --days / --here / --all from CLI vs saved defaults.
    # CLI args are ONE-SHOT: they only become the new default if the user passes
    # --save-defaults. This prevents test/exploratory invocations from silently
    # overwriting the user's preferred filter (e.g. running `saikai --days 7`
    # once would otherwise pin every future `saikai` to 7 days).
    saved_opts = _load_options()
    if args.days is None:
        args.days = saved_opts.get("days", 0)   # 0 = all history
    if args.here:
        scope = "here"
    elif args.all_scope:
        scope = "all"
    else:
        scope = saved_opts.get("scope", "all")
    args.here = (scope == "here")
    if args.save_defaults and not args.related:
        _save_options({"days": args.days, "scope": scope})
        print(_c(f"  saved defaults: days={args.days}, scope={scope}", GREEN),
              file=sys.stderr)

    # --project always wins for scope; otherwise scope follows --here/--all.
    # Warn on the silent override so the user isn't surprised when "show current
    # project's sessions" was actually "show /some/other/path's sessions".
    if args.here and args.project:
        print(_c(f"  note: --project {args.project} overrides --here; using that path",
                YELLOW), file=sys.stderr)
    args.all_projects = not (args.here or args.project)

    if args.refresh_summary and CACHE_DIR.exists():
        # Delete ONLY the per-session summary caches (named <session-uuid>.json).
        # Match UUID cache names instead of maintaining an allowlist so every
        # settings file — current or future — is safe.
        for f in CACHE_DIR.glob("*.json"):
            if _UUID_RE.fullmatch(f.stem):
                f.unlink()

    since = None if args.days == 0 else datetime.now(tz=timezone.utc) - timedelta(days=args.days)
    projects_root = PROJECTS_ROOT
    cwd = Path.cwd()

    try:
        r = subprocess.run(["git", "rev-parse", "--show-toplevel"],
                          capture_output=True, text=True, cwd=cwd, timeout=3,
                          creationflags=NO_WINDOW)
        repo = Path(r.stdout.strip()) if r.returncode == 0 else None
    except Exception:
        repo = None

    if not args.table:
        # The interactive picker shows nothing until Textual paints, and a cold
        # cache makes the scan below take seconds. Print a transient breadcrumb so
        # a cold start isn't indistinguishable from a hung shell (Textual clears
        # the screen on run(), so this only shows during the pre-UI gap).
        print(_c(f"  scanning {projects_root} …", DIM), file=sys.stderr, flush=True)
    sessions = []
    if args.all_projects:
        for d in _project_dirs(projects_root):
            sessions.extend(load_sessions_in_dir(d, since))
    else:
        target = Path(args.project) if args.project else find_project_dir(cwd)
        if not target or not target.exists():
            print(f"No Claude Code project for {cwd}", file=sys.stderr)
            print("Use --project PATH (or omit --here for all projects)", file=sys.stderr)
            sys.exit(1)
        sessions = load_sessions_in_dir(target, since)
        for s in sessions:
            s["worktree_label"] = ""   # main checkout → blank Wt cell
        # Also include sessions from other git worktrees of the same repo.
        # `git worktree list --porcelain` works from any worktree and enumerates
        # the full tree, so --here from the main repo shows worktree sessions too
        # (and vice versa). Skip if --project was given explicitly.
        if not args.project:
            for wt_dir, wt_label in _worktree_project_dirs(cwd, projects_root,
                                                            exclude=target):
                extra = load_sessions_in_dir(wt_dir, since)
                for s in extra:
                    s["worktree_label"] = wt_label
                if extra:
                    sessions.extend(extra)

    # Initial chronological sort gives _build_forest a deterministic order; the
    # user-configurable sort spec is applied AFTER forest building so it controls
    # only the displayed order.
    sessions.sort(key=lambda s: s["first_ts"], reverse=True)
    _log(f"start: loaded {len(sessions)} sessions "
         f"(all_projects={args.all_projects}, project={args.project}, days={args.days})")

    if not sessions:
        period = "all history" if args.days == 0 else f"last {args.days} days"
        print(f"No sessions in {period}.")
        return

    # Skip Haiku here so the picker starts instantly: rely on whatever cache a
    # previous run filled, falling back to the first user message on a miss.
    # Phase 1 (instant): fill all sessions from cache / ai_title / first_msg.
    # This runs synchronously in <1s so the UI starts immediately.
    _set_summary_forced_off(args.no_summary)   # CLI beats config for this run
    for s in sessions:
        cached = (_load_cache(s["id"], s["mtime"], s.get("last_ts", ""))
                  if not s.get("is_open") else None)
        s["_cache_hit"] = cached      # reused by the Phase-2 needs_llm probe (avoid a 2nd read)
        s["summary"] = (cached if cached and not _looks_like_refusal(cached)
                        else s["ai_title"] or _first_msg(s))

    # Phase 2 (background): LLM-summarize sessions that had no cache hit.
    # Skipped when summaries are OFF (default; opt-in) / --related, or all cached.
    if _summary_enabled() and not args.related:
        import threading as _thr
        needs_llm = [s for s in sessions
                     if not s["ai_title"] and not s.get("is_open")
                     and s.get("_cache_hit") is None]
        if needs_llm:
            _t = _thr.Thread(target=summarize_all_parallel, args=(sessions,),
                             daemon=True)
            _t.start()
            _bg_summarize.update(thread=_t, pending=len(needs_llm))
        else:
            _bg_summarize["thread"] = None

    if args.related:
        cmd_related(args.related, sessions)
        return

    # Always build the cross-session forest so the preview header can surface
    # the top-related session, even when --tree (nested display) is off. The forest
    # is O(N²) but each comparison is a cheap structural score (no Haiku); guard
    # with N <= 1000 to keep startup snappy on very large histories.
    # parent_id feeds tree display + the related-header, NOT first paint. The
    # static --table path needs it now; the interactive picker initialises it
    # cheaply here and builds the real forest in the background after mount (see
    # on_mount / _build_forest_bg), so a large history doesn't gate the first frame.
    if args.table and len(sessions) <= 1000:
        _build_forest(sessions)
    else:
        for s in sessions:
            s["parent_id"] = None
            s["parent_score"] = 0.0
            s["parent_reasons"] = []

    # Display mode (flat / nested-tree). The saved mode is the source of truth
    # so Shift-F5 inside the picker can toggle it in place. CLI --tree is a
    # one-shot override for the initial invocation only.
    use_tree = (args.tree or _get_tree_mode()) and len(sessions) <= 1000
    flat = not use_tree
    # Apply user-configurable sort only in flat mode. Tree mode is structural,
    # so a free-form sort would override its layout.
    if flat:
        _apply_sort(sessions, _load_sort())
    if args.table:
        # Static table display (opt-in with --table)
        hidden = _load_hidden()
        view_mode = _get_view_mode()
        if view_mode != "show-hidden":
            visible = [s for s in sessions if s["id"] not in hidden]
        else:
            visible = sessions
        display_table(visible, repo, args.all_projects, flat=flat)
    else:
        # Default: interactive textual picker. Hand it a reload closure so the
        # in-app refresh (F5) can re-scan ~/.claude/projects for new/updated
        # sessions without restarting.
        def _reload():
            fresh = []
            if args.all_projects:
                for d in _project_dirs(projects_root):
                    fresh.extend(load_sessions_in_dir(d, since))
            else:
                tgt = Path(args.project) if args.project else find_project_dir(cwd)
                if tgt and tgt.exists():
                    fresh = load_sessions_in_dir(tgt, since)
                    for s in fresh:
                        s["worktree_label"] = ""
                    if not args.project:
                        for wt_dir, wt_label in _worktree_project_dirs(
                                cwd, projects_root, exclude=tgt):
                            extra = load_sessions_in_dir(wt_dir, since)
                            for s in extra:
                                s["worktree_label"] = wt_label
                            if extra:
                                fresh.extend(extra)
            fresh.sort(key=lambda s: s["first_ts"], reverse=True)
            for s in fresh:
                cached = (_load_cache(s["id"], s["mtime"], s.get("last_ts", ""))
                          if not s.get("is_open") else None)
                s["summary"] = (cached if cached and not _looks_like_refusal(cached)
                                else s["ai_title"] or _first_msg(s))
            if len(fresh) <= 1000:
                _build_forest(fresh)
            else:
                for s in fresh:
                    s["parent_id"] = None
                    s["parent_score"] = 0.0
                    s["parent_reasons"] = []
            return fresh

        textual_pick(sessions, repo, args.all_projects, flat=flat,
                     reload_fn=_reload)


if __name__ == "__main__":
    main()
