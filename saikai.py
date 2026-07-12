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

__version__ = "0.5.2"

import argparse
import io
import json
import math
import os
import re
import shutil
import signal
import stat as _stat
import subprocess
import sys
import threading
import time
import tomllib
import unicodedata
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
    (project_short / _first_msg resolved at call time.)

    `claude_name` (Claude's OWN session name, from the live registry) is only a
    FALLBACK below ai_title / first message: Claude auto-names sessions after the
    project (e.g. "saikai-d1"), which is less informative than the ai_title, so it
    must not override it — it fills in only when there's no ai_title/first message
    (and still below a user's Shift+F2 override)."""
    return (s.get("custom_title") or s.get("ai_title") or _first_msg(s)
            or s.get("claude_name")
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


def _esc_markup(s: object) -> str:
    """Escape USER content (session titles, folder names, search queries) before
    it goes into a Textual CONTENT-markup string — a Static/Label with markup=True,
    a TabPane title, or a markup `.update()`. A raw '[' is parsed as a style tag:
    '[wip]' is silently swallowed, and '[/x]' / '[/]' raise MarkupError inside
    Content.from_markup, corrupting or crashing the render. Uses Textual's own
    escaper (textual.markup.escape); rich.markup.escape happens to work because
    both honor '\\[', but this is the API for Textual content markup. For a whole
    widget prefer Content(literal) or Content.from_markup(tmpl, var=...) (its
    $variables substitute as literals). NOTE: a RichLog(markup=True) renders RICH
    markup, not Textual content — escape those with rich.markup.escape instead."""
    from textual.markup import escape
    return escape(str(s))


def _color_legend(color_by: str) -> str:
    """Plain-language explanation shared by help and Settings."""
    # Phrased as the guaranteed direction (same project → one stable colour);
    # with a fixed palette two projects can share a hue, so we don't promise the
    # reverse ("same colour = same project"). (#stable-hue)
    labels = {
        "project": "Each project keeps one stable color.",
        "worktree": "Each worktree keeps one stable color.",
        "topic": "Each topic keeps one stable color.",
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


_PREF_CACHE: dict = {}    # path-str -> (mtime, parsed value)


def _pref_cached(path: Path, parse, default):
    """Serve a small pref file's parsed value from an mtime-keyed cache. The list
    rebuild (_do_refresh_table) reads ~half a dozen of these single-value config
    files (hidden / favorites / view-mode / tree / group-by / sort / age / status)
    EVERY time, and the live poll rebuilds ~continuously while a session is busy —
    so uncached this cost ~8-10 stat+open+read syscalls per rebuild on the UI
    thread. Re-read only when the mtime changes; the _save_*/_set_*/_toggle_*
    writers also _invalidate_pref() so a write + an immediate re-read can't be
    masked by a coarse filesystem mtime resolution. (#14)"""
    try:
        _st = path.stat()
        m = (_st.st_mtime_ns, _st.st_size)   # ns+size: same class as custom-titles/
        #                                      lineage — mtime alone misses a
        #                                      same-mtime rewrite (#audit-self-prefcache)
    except OSError:
        _PREF_CACHE.pop(str(path), None)
        return default
    key = str(path)
    hit = _PREF_CACHE.get(key)
    if hit is not None and hit[0] == m:
        return hit[1]
    try:
        v = parse()
    except Exception:
        return default
    _PREF_CACHE[key] = (m, v)
    return v


def _invalidate_pref(path: Path) -> None:
    """Drop a pref's cache entry right after writing it (see _pref_cached)."""
    _PREF_CACHE.pop(str(path), None)


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
        _sec = _load_config().get(section)
        v = _sec.get(key, None) if isinstance(_sec, dict) else None  # non-table section (#audit-codex-cfgshape)
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
    "[limits]                       # live-pane memory safety\n"
    'memory_safety          = "on"  # ONE knob: on (balanced) | off (only refuse at true\n'
    "#                                exhaustion + max_live) | strict (refuse earlier, hard stop)\n"
    "max_live               = 64    # hard cap on concurrent live panes\n"
    "scrollback_lines       = 2000  # per-pane scrollback kept in memory (biggest RAM lever)\n"
    "per_pane_mb            = 600   # estimated RAM per live pane (used by the gate + 'fit')\n"
    "# ── advanced: fine-grained gate thresholds. You rarely need these — memory_safety\n"
    "#    sets sensible values for all of them; anything set here OVERRIDES that preset.\n"
    "# max_memory_load      = 85    # refuse/warn above this % memory load (default 85 Win / 95 POSIX)\n"
    "# max_memory_pressure  = 10    # Linux PSI some-avg10 % / macOS critical -> refuse (no effect on Win)\n"
    "# min_commit_headroom_mb = 2048# keep this much commit headroom free (Win; Linux only if strict overcommit)\n"
    "# min_free_phys_pct    = 8     # keep >= this % of physical RAM free/available\n"
    "# hard_ram_gate        = false # true = refuse (vs warn) when crossed\n\n"
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
    '# refresh         = "f5"      # multi-char    = direct key rebind\n\n'
    "[checkpoint]                  # b2 checkpoint flow (leader Space-c)\n"
    '# handoff_prompt_file = ""    # path to a custom handoff prompt ("" = built-in).\n'
    "#   It MUST instruct the model to END with a fenced block whose first line is\n"
    "#   'NEW SESSION PROMPT'; a file dropping that is rejected (falls back to the\n"
    "#   built-in). Seed an editable copy with:  saikai --dump-handoff-prompt\n"
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
    "diff": "d", "copy": "y", "copy_summary": "i", "sort": "s", "order": "o",
    "group": "g", "tree": "t", "new": "n",
    "restore": "p", "freeze": "z", "attention": "a", "toggle_list": "l",
    "close": "x", "prev_tab": "[", "next_tab": "]", "mark": " ",
    "settings": ",", "search_bar": "/",
    # 'notifs' (recall dismissed toasts) also has an F11 Binding, but Windows
    # Terminal binds F11 to full-screen and eats it — so give it a leader letter
    # (Space m) that no host terminal intercepts. (#wt-f11)
    "notifs": "m",
    # b2 (Task 11): the human-gated checkpoint→/clear→rehydrate flow. A LEADER
    # action (not an F-key) because the F-key / Shift+F-key space is full AND a
    # deliberate, discoverable two-keystroke gesture (which-key hinted) suits a
    # flow that ends in a destructive /clear. b1's plain Shift+F11 stays
    # /compact-only; b2 is its own entry-point sharing b1's idle-gate helpers.
    "checkpoint": "c",
}
# Leader-only action ids (no Binding / F-key behind them): id -> action name.
LEADER_VIRTUAL_ACTIONS = {"sort": "sort", "order": "order", "mark": "toggle_mark",
                          "settings": "settings",
                          "search_bar": "toggle_search_bar",
                          "checkpoint": "checkpoint",
                          "copy_summary": "copy_summary"}

# Leader families: action name -> family, in display order. The which-key hint
# and the ? help render the map grouped this way (Session / View / Panes)
# instead of an alphabetical soup — the LETTERS stay flat (two keystrokes),
# only the presentation is systematic. Unknown actions (user remaps of new ids)
# fall into the last family rather than vanishing from the hint.
LEADER_FAMILY_ORDER = ("Session", "View", "Panes")
LEADER_FAMILY_OF = {
    "toggle_fav": "Session", "toggle_hide": "Session", "rename": "Session",
    "copy_prompt": "Session", "copy_summary": "Session",
    "preview_changes": "Session", "refresh": "Session",
    "sort": "View", "order": "View", "cycle_group": "View",
    "toggle_tree": "View", "toggle_list": "View", "notifications": "View",
    "settings": "View", "toggle_search_bar": "View",
    "new_session": "Panes", "restore_panes": "Panes", "freeze_pane": "Panes",
    "next_attention": "Panes", "close_live": "Panes", "prev_tab": "Panes",
    "next_tab": "Panes", "toggle_mark": "Panes", "checkpoint": "Panes",
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
               "copy_prompt": "copy", "copy_summary": "copy text",
               "next_attention": "next!",
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
    ("limits", "memory_safety", "SAIKAI_MEM_SAFETY", "on"),   # on | off | strict (one knob)
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
    ("checkpoint", "handoff_prompt_file", "SAIKAI_HANDOFF_PROMPT_FILE", ""),
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
        elif (isinstance(cfg.get(sec), dict)
              and cfg[sec].get(key) is not None):   # non-table section (#audit-codex-cfgshape)
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


def _write_text_atomic(path: Path, text: str, mode: "int | None" = None) -> None:
    """Atomically write `text` to path via a unique tempfile + os.replace, so a
    concurrent reader (another saikai tab polling the pref) can never observe the
    truncate-then-write window that a plain path.write_text opens — during which
    the read returns an empty string and the caller silently reverts to a wrong
    default mode/filter until the next distinct-mtime read.

    `mode` (e.g. 0o600 for token-bearing files) is applied at tmp CREATION, not
    chmod-after-write — a chmod would leave a default-umask window; os.replace
    then carries the tmp's permissions onto the destination."""
    path.parent.mkdir(parents=True, exist_ok=True)
    # PID + thread id keep the tmp name unique across concurrent writers of the
    # same path (the docstring's worker-pool safety claim needs the thread id;
    # PID alone collides between two threads in one process).
    tmp = path.with_suffix(path.suffix + f".tmp.{os.getpid()}.{threading.get_ident()}")
    try:
        if mode is not None:
            fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(text)
        else:
            tmp.write_text(text, encoding="utf-8")
        # On Windows os.replace fails with PermissionError if another process holds
        # the DESTINATION open without FILE_SHARE_DELETE (another saikai tab, a
        # PowerShell `type`, an editor). The lock is transient, so retry briefly
        # before giving up. No-op on POSIX, where replace-over-open succeeds.
        for _attempt in range(4):
            try:
                os.replace(tmp, path)
                break
            except PermissionError:
                if _attempt == 3:
                    raise
                time.sleep(0.05)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except Exception:
                pass


def _write_json(path: Path, obj) -> None:
    """Atomically write JSON to path. Uses tempfile + os.replace so concurrent
    readers/writers (worker pool, concurrent reads) cannot observe a torn write."""
    _write_text_atomic(path, json.dumps(obj, indent=2, ensure_ascii=False))


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

# Single source for the LAN-input opt-in env var name: it appears in the env
# read, the startup banner, and the Shift+F12 refusal toast — a rename or typo
# in any one copy would tell users to set a variable that does nothing.
_MIRROR_LAN_INPUT_ENV = "SAIKAI_MIRROR_ALLOW_LAN_INPUT"
# Where the (token-bearing) mirror URL is persisted while the picker runs;
# written at mirror startup, removed at exit AND before a resume handoff.
_MIRROR_URL_FILE = CACHE_DIR / "mirror-url.txt"


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
    raw = _read_json(OPTIONS_FILE, {})
    return raw if isinstance(raw, dict) else {}   # corrupt shape (#audit-codex-prefshape)


def _save_options(opts: dict) -> None:
    """Merge `opts` into the persisted options so future fields aren't dropped
    by an older saikai version that doesn't know about them.

    Erase-guarded: if the existing file is PRESENT but unreadable (corrupt, locked,
    or mid-write by another instance), _load_options would return {} and the merge
    would persist ONLY the one field just set — silently wiping every other option
    (search_bar/split_ratio/days/scope). So distinguish absent (legit empty) from
    unreadable (skip the write, don't erase). A non-dict-but-valid-JSON file (e.g.
    `[]`) is treated as empty rather than crashing on .update. (#H7 + the non-dict
    AttributeError sibling)."""
    if OPTIONS_FILE.exists():
        try:
            existing = json.loads(OPTIONS_FILE.read_text(encoding="utf-8"))
        except Exception:
            _log("save options skipped: existing options file present but unreadable "
                 "(not erasing the other saved options)")
            return
        merged = existing if isinstance(existing, dict) else {}
    else:
        merged = {}
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
        _st = CUSTOM_TITLES_FILE.stat()
        m = (_st.st_mtime_ns, _st.st_size)   # ns+size: an external same-mtime
        #                                      rewrite must not serve the old map (#audit-codex-cachekey)
    except OSError:
        _CUSTOM_TITLES_CACHE, _CUSTOM_TITLES_MTIME = {}, None
        return {}
    if _CUSTOM_TITLES_CACHE is not None and m == _CUSTOM_TITLES_MTIME:
        return _CUSTOM_TITLES_CACHE
    raw = _read_json(CUSTOM_TITLES_FILE, {})
    # keep only str->str entries: a hand-edited/corrupt value (dict, int) would
    # otherwise flow into the title pipeline and TypeError at render slicing,
    # breaking every list rebuild until the file is fixed. (#audit-hostile-files)
    _CUSTOM_TITLES_CACHE = ({k: v for k, v in raw.items()
                             if isinstance(k, str) and isinstance(v, str)}
                            if isinstance(raw, dict) else {})
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


# Lineage recovery pointer (Task 5): child_sid -> {parent, parent_jsonl, ts}.
# Clones the mtime-cache + atomic-write pattern of the custom-titles sidecar.
LINEAGE_FILE = CACHE_DIR / "lineage.json"     # child_sid -> {parent, parent_jsonl, ts}
_LINEAGE_CACHE: "dict | None" = None
_LINEAGE_MTIME: "float | None" = None


def _load_lineage() -> dict:
    """child_sid -> {parent, parent_jsonl, ts}. Re-read only when the file
    mtime changes (or after a write)."""
    global _LINEAGE_CACHE, _LINEAGE_MTIME
    try:
        _st = LINEAGE_FILE.stat()
        m = (_st.st_mtime_ns, _st.st_size)   # ns+size (#audit-codex-cachekey)
    except OSError:
        _LINEAGE_CACHE, _LINEAGE_MTIME = {}, None
        return {}
    if _LINEAGE_CACHE is not None and m == _LINEAGE_MTIME:
        return _LINEAGE_CACHE
    raw = _read_json(LINEAGE_FILE, {})
    _LINEAGE_CACHE = raw if isinstance(raw, dict) else {}
    _LINEAGE_MTIME = m
    return _LINEAGE_CACHE


def _set_lineage(child: str, parent: str, parent_jsonl: str) -> None:
    """Record that `child` was forked/cleared from `parent`. Atomic write;
    cache invalidated so the next _load_lineage re-reads from disk.

    Strict read of the existing file (raise on present-but-unreadable) so a
    transient read failure / locked file doesn't collapse the whole lineage map
    to this one entry — mirrors _set_custom_title / _toggle_in_set. (#audit-lineage)"""
    global _LINEAGE_CACHE, _LINEAGE_MTIME
    import time
    if LINEAGE_FILE.exists():
        try:
            raw = json.loads(LINEAGE_FILE.read_text(encoding="utf-8"))
        except Exception as e:
            raise RuntimeError(
                f"{LINEAGE_FILE.name} exists but is unreadable ({e!r}); "
                f"not writing (won't risk erasing recovery lineage)") from e
        d = dict(raw) if isinstance(raw, dict) else {}
    else:
        d = {}
    d[child] = {"parent": parent, "parent_jsonl": parent_jsonl,
                "ts": time.strftime("%Y-%m-%dT%H:%M:%S")}
    _write_json(LINEAGE_FILE, d)
    _LINEAGE_CACHE, _LINEAGE_MTIME = None, None     # force reload next read


def _load_set(path: Path) -> set[str]:
    raw = _pref_cached(path, lambda: _read_json(path, []), [])
    if not isinstance(raw, list):
        return set()                               # corrupt shape (#audit-codex-prefshape)
    return {x for x in raw if isinstance(x, str)}


def _save_set(path: Path, ids: set[str]) -> None:
    _write_json(path, sorted(ids))
    _invalidate_pref(path)


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


def _bulk_toggle_in_set(path: Path, sids, force: "bool | None" = None) -> bool:
    """Toggle many `sids` in the set at `path` with ONE read + ONE write; return
    the resulting membership (True = the sids are now present).

    Direction: force=True adds all, force=False removes all, force=None auto-
    decides by the *converging* rule "any-off → all-on, else all-off". A mixed
    selection becomes uniformly on (a second press turns it uniformly off) — it
    never flips each row independently, which on a mixed set would un-favorite the
    rows you can see while favoriting the ones scrolled off.

    Shares _toggle_in_set's anti-erase guard (refuses to write when the file
    EXISTS but won't parse) AND, by reading/writing once for the whole batch,
    closes the lost-update window a per-sid loop would open between calls."""
    ids = [s for s in sids if s]
    if path.exists():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            raise RuntimeError(f"{path.name} exists but could not be read "
                               f"({e!r}); not toggling (won't risk erasing it)") from e
        s = set(raw) if isinstance(raw, list) else set()
    else:
        s = set()
    if not ids:
        return False
    target = (any(i not in s for i in ids) if force is None else bool(force))
    if target:
        s.update(ids)
    else:
        s.difference_update(ids)
    _save_set(path, s)
    return target


def _write_pref_atomic(path: Path, value: str) -> None:
    """Best-effort atomic pref write + cache invalidation. A UI pref (view/tree/
    group/status/lastact) is non-critical: a transient os.replace failure — e.g.
    Windows PermissionError when another tab/editor holds the file open past the
    retry window — must not crash the key-binding action that triggered it. The
    setting simply isn't persisted this time and is retried on the next toggle."""
    try:
        _write_text_atomic(path, value)
    except Exception:
        pass
    _invalidate_pref(path)


def _load_hidden() -> set[str]:
    return _load_set(HIDDEN_FILE)


def _load_favorites() -> set[str]:
    return _load_set(FAVORITE_FILE)


def _get_view_mode() -> str:
    return _pref_cached(VIEW_MODE_FILE,
                        lambda: VIEW_MODE_FILE.read_text(encoding="utf-8").strip(),
                        "default") or "default"


def _toggle_view_mode() -> str:
    new_mode = "show-hidden" if _get_view_mode() == "default" else "default"
    _write_pref_atomic(VIEW_MODE_FILE, new_mode)
    return new_mode


def _get_tree_mode() -> bool:
    """Saved nested-tree display preference. False (flat) by default."""
    return _pref_cached(TREE_MODE_FILE,
                        lambda: TREE_MODE_FILE.read_text(encoding="utf-8").strip(),
                        "") == "on"


def _toggle_tree_mode() -> bool:
    new = not _get_tree_mode()
    _write_pref_atomic(TREE_MODE_FILE, "on" if new else "off")
    return new


def _get_group_by() -> str:
    """Saved grouping axis: 'none' | 'date' | 'project' | 'state'. Default is
    State: with split-live the question is "who needs me / what's running",
    and the State sections (Needs input / Running / Open / Recent / Idle /
    Archived) answer it at a glance — Date is one ␣g away. An explicit choice —
    including 'none' — is persisted by _set_group_by and wins from then on."""
    v = _pref_cached(GROUP_BY_FILE,
                     lambda: GROUP_BY_FILE.read_text(encoding="utf-8").strip(), "")
    return v if v in ("none", "date", "project", "state") else "state"


def _set_group_by(value: str) -> None:
    if value not in ("none", "date", "project", "state"):
        value = "none"
    _write_pref_atomic(GROUP_BY_FILE, value)


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
    v = _pref_cached(STATUS_FILTER_FILE,
                     lambda: STATUS_FILTER_FILE.read_text(encoding="utf-8").strip(), "")
    return v if v in ("active", "archived", "all") else "active"


def _set_status_filter(value: str) -> None:
    if value not in ("active", "archived", "all"):
        value = "active"
    _write_pref_atomic(STATUS_FILTER_FILE, value)


def _get_lastact_days() -> int:
    """Claude-Desktop 'Last activity' window in days (0 = All time, default).
    Clamped to the dropdown option set — a stray/negative persisted value (e.g.
    -3 makes the cutoff a FUTURE time that hides EVERY row) must not silently
    empty the list, and the box must not show a value it can't represent."""
    v = _pref_cached(LASTACT_FILTER_FILE,
                     lambda: int(LASTACT_FILTER_FILE.read_text(encoding="utf-8").strip()), 0)
    return v if v in (0, 1, 3, 7, 30) else 0


def _set_lastact_days(days: int) -> None:
    _write_pref_atomic(LASTACT_FILTER_FILE, str(int(days)))


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
    return -30.0 <= (now_ts - (s.get("mtime") or 0.0)) < 1800   # future ≠ recent; ±30s clock-jitter slack (#audit-codex-futuremtime)


def _is_active_now(s: dict, now_ts: float) -> bool:
    """True if running (live-registry snapshot) or touched < 5 min ago, evaluated
    against the current time (see _is_recent_now re: staleness)."""
    return bool(s.get("is_open")) or -30.0 <= (now_ts - (s.get("mtime") or 0.0)) < 300  # (#audit-codex-futuremtime)


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
    _LIVE_STATES = ("Needs input", "Running", "Open", "Agents")
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
        if buckets.get("Agents"):
            # cluster the agents by PARENT session (stable sort keeps the
            # recency order within one parent's brood) so one parent's agents
            # read as a block (#agent-lineage)
            buckets["Agents"].sort(key=lambda x: x.get("parent_session_id") or x["id"])
        for l in ("Needs input", "Running", "Open", "Agents", "Idle", "Archived"):
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
                rec = json.loads(line)
                # Contract: a RECORD (dict) or None. A trailing `[]` / `"x"` is
                # valid JSON but not a record — returning it made _needs_attention
                # AttributeError on .get(), which killed --table outright and
                # broke every TUI refresh. (#audit-codex-lastrec)
                return rec if isinstance(rec, dict) else None
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
    raw = _pref_cached(SORT_FILE, lambda: _read_json(SORT_FILE, None), None)
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
    _invalidate_pref(SORT_FILE)


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
        if col == "date":  return _iso_sort_key(s.get("first_ts"))  # tz-aware (#audit-codex-tsort)
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


_CLAUDE_PROC_NAMES = frozenset({"claude.exe", "node.exe"})
# POSIX /proc/<pid>/comm equivalents (no ".exe"; comm is truncated to 15 chars).
_CLAUDE_PROC_COMMS = frozenset({"claude", "node"})
_PROC_ROOT = Path("/proc")   # Linux procfs root; module-level so tests can point it at a fixture


def _linux_pid_is_claude(pid: int) -> bool:
    """Linux PID-reuse guard: verify /proc/<pid>/comm names a Claude runtime, the
    POSIX analogue of the Windows image-name check. A missing /proc/<pid> means the
    process is gone (dead); an unreadable comm for any other reason falls back to a
    bare liveness check so we never FALSE-kill a live session. (#audit-pidreuse)"""
    if pid <= 0:
        return False
    try:
        comm = (_PROC_ROOT / str(pid) / "comm").read_text(
            encoding="utf-8", errors="replace").strip()
    except FileNotFoundError:
        return False          # /proc/<pid> absent → process no longer exists
    except OSError:
        return _is_pid_alive(pid)   # unreadable for another reason → don't false-kill
    return comm in _CLAUDE_PROC_COMMS or comm.startswith("claude")


def _is_session_pid_live(pid: int, pid_index: "dict | None") -> bool:
    """A registered session PID counts as live only if it's alive AND actually a
    Claude process. Guards against PID reuse: the `~/.claude/sessions/<pid>.json`
    registry has no TTL, so a stale entry whose PID the OS has recycled to an
    unrelated process would otherwise read as a live (is_open) session. Windows uses
    the CreateToolhelp32 image name; Linux uses /proc/<pid>/comm; other POSIX (macOS,
    no /proc) falls back to a bare liveness check. (#audit-pidreuse)"""
    if pid_index is None:
        if sys.platform.startswith("linux"):
            return _linux_pid_is_claude(pid)
        return _is_pid_alive(pid)
    info = pid_index.get(pid)
    if info is None:
        return False
    name = info[0]
    # Exact image-name match (szExeFile is always "<name>.exe" here), plus a
    # "claude*" prefix for wrapper variants (claude.exe, claude-code.exe). The old
    # bare `"node" in name` / `"claude" in name` substring test matched ANY image
    # containing those tokens (e.g. an unrelated "node-red.exe", "vscode-node.exe"),
    # widening the PID-reuse false-positive window this guard exists to close.
    # (A recycled PID landing on a real node.exe still can't be told apart by name
    # alone — that residue needs ppid ancestry, out of scope here.) (#audit-pidreuse)
    return name in _CLAUDE_PROC_NAMES or name.startswith("claude")


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
    # modern shells: absent, a tab launched from them anchored nothing and the
    # terminal-close watchdog silently never armed (#audit-codex-shells)
    "nu.exe", "fish.exe", "xonsh.exe", "elvish.exe", "powershell_ise.exe",
})
# Terminal emulators sit ABOVE the tab shell and survive a single-tab close, so
# the ancestor walk stops here — the tab shell is the last shell seen before the
# emulator (anchoring on the emulator would only fire on whole-window close).
_TERM_EMULATOR_NAMES = frozenset({
    "wezterm-gui.exe", "wezterm.exe", "windowsterminal.exe",
    "windowsterminalpreview.exe", "wt.exe",          # WT + its app-alias launcher
    "openconsole.exe", "conhost.exe",                # classic/legacy console host
    "alacritty.exe", "kitty.exe", "mintty.exe",      # git-bash/msys
    "conemu64.exe", "conemu.exe", "hyper.exe", "tabby.exe",
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
        # ctypes.pointer (not byref): with argtypes = POINTER(PROCESSENTRY32),
        # strict ctypes rejects byref's lightweight ref ("expected LP_… instance
        # instead of pointer to …"). pointer() yields a real LP_PROCESSENTRY32.
        ok = k32.Process32First(snap, ctypes.pointer(entry))
        while ok:
            name = entry.szExeFile.decode("ascii", "replace").lower()
            out[int(entry.th32ProcessID)] = (name, int(entry.th32ParentProcessID))
            ok = k32.Process32Next(snap, ctypes.pointer(entry))
    except Exception:
        # Honour the documented "{} on any failure" contract — the snapshot-walk
        # must never raise into callers (the watchdog AND the live-session reader
        # both treat {} as "no info"). Previously only snapshot CREATION was
        # guarded, so a ctypes mismatch escaped. (#audit-pidreuse)
        return {}
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


def _start_terminal_watchdog(poll_sec: float = 8.0) -> None:
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
        misses = 0
        while True:
            time.sleep(poll_sec)
            # AUTHORITATIVE liveness check: re-walk the process tree from our OWN
            # pid for ANY live shell ancestor below the terminal emulator. Doing the
            # full re-walk every poll — rather than a cheap _is_pid_alive(anchor)
            # fast-path — is deliberate: Windows recycles PIDs, so if the original
            # anchor PID gets reused by an unrelated process, _is_pid_alive(anchor)
            # stays True forever and a genuinely orphaned tab is never reaped. The
            # old fast-path also short-circuited (misses=0; continue) BEFORE this
            # re-walk, so on PID reuse the re-walk never ran at all. A live anchor
            # here → the tab/window is still open.
            try:
                alive = bool(_find_terminal_anchor(_win_pid_index(), self_pid))
            except Exception:
                # A transient enumeration failure is inconclusive — neither alive
                # nor dead. RESET the miss streak: otherwise a failure sitting
                # BETWEEN two real misses (miss→1, fail→continue@1, miss→2) lets
                # two NON-consecutive misses reach the kill, defeating the "2
                # consecutive confirmations" contract and os._exit-ing a healthy
                # saikai. Erring toward a delayed reap is far safer than a false kill.
                misses = 0
                continue
            if alive:
                misses = 0
                continue
            # No live terminal ancestor → likely orphaned. os._exit (below) bypasses
            # atexit AND Textual teardown, so a false positive would silently kill a
            # healthy saikai and leave the terminal stuck in mouse/paste mode (the
            # "sudden crash + stray chars on scroll" report). Require 2 consecutive
            # confirmations so a one-off snapshot glitch (the shell momentarily
            # absent during heavy process churn) can't trigger the kill — ~16s at
            # the 8s cadence, a middle ground between the old trigger-happy single
            # poll and the over-cautious 3-miss (~36s) debounce.
            misses += 1
            if misses < 2:
                continue
            # Genuinely orphaned (tab/window closed) → emulate SIGHUP. Restore the
            # terminal modes FIRST so even a residual false positive can't leave
            # mouse tracking on, THEN kill our OWN subtree (the resumed claude child
            # included) and exit hard so a daemon thread blocked elsewhere can't
            # keep the interpreter alive.
            try:
                _reset_terminal_modes()
            except Exception:
                pass
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
_active_kinds_cache: "dict[str, str] | None" = None   # sid -> kind ("interactive" | "bg" | …)
_active_procinfo_cache: "dict[str, tuple] | None" = None   # sid -> (pid, procStart) for a verified kill (#agent-kill)
_active_jobids_cache: "dict[str, str] | None" = None   # sid -> jobId (bg sessions → ~/.claude/jobs/<jobId>/)
_active_remote_cache: set[str] | None = None   # live sids with bridgeSessionId (Remote Control)
_active_names_cache: "dict[str, str] | None" = None   # sid -> Claude's own session name (the switcher label)
_JOB_STATE_CACHE: dict = {}    # job_id -> (mtime, parsed state.json | None)


def _load_active_sessions() -> dict[str, str]:
    """Read Claude Code's `~/.claude/sessions/<pid>.json` registry and return
    {sessionId: status} for every PID still alive. Claude Code writes one file per
    running session with `status` = "busy" | "idle" AND `kind` = "interactive" |
    "bg" (a headless background agent/job) | … — the kind is exposed separately via
    _active_session_kinds (a `bg` session is live but NOT resumable / attachable)."""
    global _active_sessions_cache, _active_kinds_cache, _active_jobids_cache
    global _active_remote_cache, _active_names_cache, _active_procinfo_cache
    if _active_sessions_cache is not None:
        return _active_sessions_cache
    out: dict[str, str] = {}
    kinds: dict[str, str] = {}
    procinfo: dict[str, tuple] = {}
    jobids: dict[str, str] = {}
    remote: set[str] = set()
    names: dict[str, str] = {}
    sessions_dir = CLAUDE_CONFIG_ROOT / "sessions"
    scanned_ok = False
    # One fast process snapshot (no subprocess) to validate registered PIDs are
    # still Claude processes — defends against Windows PID reuse. Empty snapshot =
    # failure → None so _is_session_pid_live falls back to a bare liveness check
    # rather than marking every session dead. (#audit-pidreuse)
    pid_index = _win_pid_index() if sys.platform == "win32" else None
    if not pid_index:
        pid_index = None
    try:
        if sessions_dir.exists():
            for f in sessions_dir.glob("*.json"):
                try:
                    d = json.loads(f.read_text(encoding="utf-8"))
                    pid = d.get("pid")
                    sid = d.get("sessionId")
                    status = d.get("status", "")
                    if pid and sid and _is_session_pid_live(int(pid), pid_index):
                        out[sid] = status
                        kinds[sid] = d.get("kind", "")
                        # (pid, procStart) identifies the process for a verified
                        # kill — procStart is the pid-reuse guard. (#agent-kill)
                        procinfo[sid] = (int(pid), str(d.get("procStart", "")))
                        _jid = d.get("jobId")
                        if _jid:
                            jobids[sid] = str(_jid)
                        # In-session `/remote-control` adds bridgeSessionId to
                        # this live registry entry. Only accept it after the PID
                        # liveness check above: stale registry files have no TTL.
                        if isinstance(d.get("bridgeSessionId"), str) and d["bridgeSessionId"]:
                            remote.add(sid)
                        # Claude's OWN session name (the label shown in its
                        # session/agent switcher) — so saikai's list matches what
                        # the user named/sees there, not a divergent local title.
                        _nm = d.get("name")
                        if isinstance(_nm, str) and _nm.strip():
                            names[sid] = _nm.strip()
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
        _active_kinds_cache = kinds
        _active_procinfo_cache = procinfo
        _active_jobids_cache = jobids
        _active_remote_cache = remote
        _active_names_cache = names
    return out


def _active_session_names() -> dict:
    """sid -> Claude's own session name (the switcher label) for live registry
    entries; {} until _load_active_sessions has run cleanly."""
    _load_active_sessions()
    return _active_names_cache or {}


def _job_state_for(sid: str) -> "dict | None":
    """The bg job state for a live session, joined via its registry jobId →
    CLAUDE_CONFIG_ROOT/jobs/<jobId>/state.json. (mtime,size)-cached; tolerant of a
    job dir that has only tmp/ (no state.json yet). Read-only; never touches the
    daemon control/auth keys. (#recon-bg-jobs)"""
    _load_active_sessions()
    job_id = (_active_jobids_cache or {}).get(sid)
    if not job_id:
        return None
    p = CLAUDE_CONFIG_ROOT / "jobs" / job_id / "state.json"
    try:
        st = p.stat()
        key = (st.st_mtime, st.st_size)   # size too: a coarse-mtime FS (FAT/SMB/NFS
    except OSError:                       # roaming profile) can rewrite state.json
        return None                       # within one mtime tick — size disambiguates
    hit = _JOB_STATE_CACHE.get(job_id)
    if hit is not None and hit[0] == key:
        return hit[1]
    d = _read_json(p, None)
    val = d if isinstance(d, dict) else None
    _JOB_STATE_CACHE[job_id] = (key, val)
    return val


def _active_session_kinds() -> dict:
    """sid -> kind ('interactive' | 'bg' | …) for the live registry entries,
    populated alongside _load_active_sessions; {} until that has run cleanly."""
    _load_active_sessions()
    return _active_kinds_cache or {}


def _active_procinfo() -> dict:
    """sid -> (pid, procStart) for the live registry entries. (#agent-kill)"""
    _load_active_sessions()
    return _active_procinfo_cache or {}


def _proc_start_matches(pid: int, procstart: str) -> bool:
    """True if `pid` is STILL the process the registry recorded — the pid-reuse
    guard for a kill. claude stores the OS process-start identity in `procStart`;
    on Linux that's /proc/<pid>/stat field 22 (start time in clock ticks since
    boot), compared exactly. Without a recorded procStart, or off Linux where we
    can't cheaply read it, fall back to the image-name check (weaker but the only
    cross-platform signal). Never raises. (#agent-kill)"""
    if pid <= 0:
        return False
    ps = (procstart or "").strip()
    if sys.platform.startswith("linux") and ps.isdigit():
        try:
            fields = (_PROC_ROOT / str(pid) / "stat").read_text(
                encoding="utf-8", errors="replace").rsplit(")", 1)[-1].split()
            # after "comm)" the fields are state ppid … starttime = overall field
            # 22 = index 19 in this post-")" slice.
            return fields[19] == ps
        except (FileNotFoundError, IndexError):
            return False
        except OSError:
            pass
    if sys.platform == "win32":
        idx = _win_pid_index()
        info = idx.get(pid) if idx else None
        nm = (info[0] if info else "")
        return bool(nm) and (nm in _CLAUDE_PROC_NAMES or nm.startswith("claude"))
    return _linux_pid_is_claude(pid) if sys.platform.startswith("linux") else _is_pid_alive(pid)


def _kill_agent_process(pid: int, procstart: str) -> str:
    """Terminate a live AGENT/bg session's process by pid, off the UI thread.
    Verifies the (pid, procStart) identity FIRST so a recycled pid is never
    signalled. POSIX: SIGTERM the PID only (never the group — an external fork
    may share the parent claude's group; the bare PID can't take the parent
    down), then SIGKILL after a grace period if it survives. Windows: taskkill
    /T /F by pid (the agent + its own workers, not its ancestors). Returns a
    short outcome for the toast. (#agent-kill)"""
    if not _proc_start_matches(pid, procstart):
        return "stale"
    if sys.platform == "win32":
        import subprocess
        try:
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=10,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except Exception:
            return "error"
        return "signalled"
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return "gone"
    except Exception:
        return "error"

    def _escalate():
        import time as _t
        for _ in range(15):                 # ~1.5s grace for a clean SIGTERM exit
            _t.sleep(0.1)
            if not _is_pid_alive(pid):
                return
        if _proc_start_matches(pid, procstart):   # re-verify before the hard kill
            try:
                os.kill(pid, signal.SIGKILL)
            except Exception:
                pass
    import threading as _thr
    _thr.Thread(target=_escalate, daemon=True, name="saikai-agent-reap").start()
    return "signalled"


def _active_remote_sessions() -> set[str]:
    """Live session ids whose Claude registry entry has Remote Control enabled."""
    _load_active_sessions()
    return _active_remote_cache or set()


def _invalidate_active_sessions() -> None:
    """Drop the memoised live-session registry so the next _load_active_sessions
    re-reads ~/.claude/sessions. Called on reload — otherwise is_open / is_active
    stay frozen at the launch-time snapshot for the whole picker lifetime (a
    session that exited elsewhere keeps showing Open/Running)."""
    global _active_sessions_cache, _active_kinds_cache, _active_jobids_cache
    global _active_remote_cache, _active_names_cache, _active_procinfo_cache
    _active_sessions_cache = None
    _active_kinds_cache = None
    _active_jobids_cache = None
    _active_remote_cache = None
    _active_names_cache = None


def _enrich_session(sid: str, parsed: dict, jsonl_path: Path, mtime: float) -> dict:
    """Wrap parsed session data with runtime state (active/recent/status)."""
    # Clock skew (NTP correction, restored backup) can put mtime in the FUTURE.
    # The old `max(0.0, …)` floor was meant to stop the false is_active but did
    # the OPPOSITE: age 0 reads as "touched just now". A future mtime now maps
    # to +inf age, so is_active/is_recent read False for it. (#audit-codex-futuremtime)
    _raw_age = time.time() - mtime
    age_sec = max(0.0, _raw_age) if _raw_age >= -30.0 else float("inf")
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
        "worktree_origin_cwd": parsed.get("worktree_origin_cwd", ""),
        "git_branch": parsed.get("git_branch", ""),
        "claude_name": _active_session_names().get(sid, ""),   # Claude's own switcher label (live sessions)
        "is_open": sid in active,
        "is_remote_control": sid in _active_remote_sessions(),
        # Claude Desktop's SSH integration mirrors REMOTELY-executed sessions
        # into local projects/ssh-<uuid>/ (verified on a real Desktop install:
        # the remote agent is %APPDATA%/Claude/claude-ssh-remote/*, the cwd is a
        # path on the REMOTE host). They are listable but not resumable here —
        # the cwd doesn't exist on this machine and the schema differs.
        # Detection is by directory name: a real cwd can never slug to "ssh-"
        # (a Linux cwd slugs to "-…", a Windows one to "C--…"). (#remote-origin)
        "remote_origin": jsonl_path.parent.name.startswith("ssh-"),
        # agent lineage (#agent-lineage): who spawned this session (empty for a
        # user-started one), its agent id, and whether it's a sidechain
        "parent_session_id": parsed.get("parent_session_id", ""),
        "agent_id": parsed.get("agent_id", ""),
        "is_sidechain": bool(parsed.get("is_sidechain")),
        # Default-DENY unknown live kinds: a session is non-attachable (is_bg) when
        # it is LIVE with a non-empty kind other than "interactive". Only bg + an
        # interactive kind exist today, but a future kind defaults to NOT-resumable
        # (refusing resume is recoverable; resuming a live session corrupts it).
        # A dormant session (absent from the registry) → kind None → not is_bg. (#recon-unknown-kind)
        "is_bg": (lambda _k: bool(_k) and _k != "interactive")(_active_session_kinds().get(sid)),
        # the raw registry kind ("agent" = claude's agents/teammates feature,
        # "bg" = a headless bg job, "" = interactive) — drives kind-aware UX
        # for non-attachable live sessions (#agents-kind)
        "live_kind": _active_session_kinds().get(sid) or "",
        "session_status": active.get(sid, ""),
        "is_active": (sid in active) or age_sec < 300,
        "is_recent": age_sec < 1800,
    }
    # Background-job status: bg sessions link to ~/.claude/jobs/<jobId>/state.json
    # (state working|blocked|done|failed|stopped + a `needs` clarify prompt when
    # blocked). Surface it so a bg agent waiting on YOU ("blocked") is visible. (#recon-bg-jobs)
    if result["is_bg"]:
        _js = _job_state_for(sid)
        if _js:
            result["job_state"] = _js.get("state") or ""
            result["job_needs"] = _js.get("needs") or ""
            result["job_detail"] = _js.get("detail") or ""
    result["last_active_dt"] = _compute_last_active_dt(result)
    if "topics" in parsed:
        result["topics"] = parsed["topics"]
    return result


def parse_session(jsonl_path: Path) -> dict | None:
    sid = jsonl_path.stem
    # A session's JSONL can vanish between a caller's glob() snapshot and this
    # stat (another saikai tab deleting/hiding it, or Claude Code pruning an
    # ephemeral transcript). The since=None callers (e.g. the Claude-Desktop
    # import) do NOT wrap parse_session, so an unhandled FileNotFoundError here
    # would abort the whole enumeration — skip the one vanished session instead.
    try:
        _st = jsonl_path.stat()
    except OSError:
        return None
    mtime, size = _st.st_mtime, _st.st_size
    cache_file = PARSED_DIR / f"{sid}.json"

    # Disk cache: skip JSONL re-parsing if mtime is unchanged AND schema is current.
    # `origin_cwd` was added 2026-04-30 to fix `claude --resume` for sessions
    # whose cwd changed mid-flight (e.g. moved into a worktree). Caches predating
    # that field force a re-parse.
    # SIZE must also match: the 0.5s mtime tolerance (kept for coarse-mtime
    # filesystems) otherwise treats an append landing within that window as a
    # HIT, serving stale content — exactly what the summary cache was migrated
    # off. A real append always changes the byte size. (#audit-parsecache)
    cached = _read_json(cache_file, None)
    if (cached and abs(cached.get("mtime", 0) - mtime) < 0.5
            and cached.get("size") == size
            and "origin_cwd" in cached
            and "parent_session_id" in cached):   # lineage added 2026-07-06 (#agent-lineage)
        if _is_hook_session(cached.get("real_msgs", []), len(cached.get("real_msgs") or [])):
            return None
        return _enrich_session(sid, cached, jsonl_path, mtime)

    # Preserve topics across re-parse (JSONL append shouldn't invalidate Haiku-derived topics)
    prior_topics = (cached or {}).get("topics") or []

    first_ts = last_ts = ai_title = cwd = origin_cwd = git_branch = None
    worktree_origin_cwd = None    # worktree-state.worktreeSession.originalCwd (resume cwd)
    parent_session_id = agent_id = None   # agent/subagent lineage (#agent-lineage)
    is_sidechain = False
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
                if not isinstance(obj, dict):
                    continue   # a bare JSON array/string/number line carries no fields
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
                # Agent lineage: a session claude's agents feature spawned records
                # who spawned it — the anchor for grouping/bulk ops on the parent.
                # First value wins (it's constant per session). (#agent-lineage)
                if parent_session_id is None \
                        and isinstance(obj.get("parentSessionId"), str) \
                        and obj["parentSessionId"]:
                    parent_session_id = obj["parentSessionId"]
                if agent_id is None and isinstance(obj.get("agentId"), str) \
                        and obj["agentId"]:
                    agent_id = obj["agentId"]
                if obj.get("isSidechain") is True:
                    is_sidechain = True
                if t == "ai-title":
                    ai_title = obj.get("aiTitle", "") or ai_title
                elif t == "worktree-state":
                    # A worktree session's AUTHORITATIVE origin cwd for `claude --resume`
                    # (the repo root the user was in before entering the isolated worktree).
                    # The plain `cwd` records may point at the .claude/worktrees/ dir. (#recon-worktree-cwd)
                    _ws = obj.get("worktreeSession")
                    if isinstance(_ws, dict) and isinstance(_ws.get("originalCwd"), str):
                        worktree_origin_cwd = _ws["originalCwd"]
                if t == "user":
                    # message can be a truthy non-dict (a JSON string, or a list) on
                    # some records; guard so .get() can't raise and abort the whole
                    # `for line in f` loop, which would lose every later record's
                    # ts / cwd / ai_title / turns. (#audit-parse-msg)
                    msg = obj.get("message")
                    text = _extract_text(msg.get("content", "")) if isinstance(msg, dict) else ""
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
        "size": size,
        "first_ts": first_ts,
        "last_ts": last_ts or first_ts,
        "ai_title": ai_title or "",
        "real_msgs": real_msgs,
        "n_turns": len(real_msgs),
        "cwd": cwd or "",
        "origin_cwd": origin_cwd or cwd or "",
        "worktree_origin_cwd": worktree_origin_cwd or "",
        "git_branch": git_branch or "",
        "parent_session_id": parent_session_id or "",
        "agent_id": agent_id or "",
        "is_sidechain": is_sidechain,
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
        # Contain per-file failures at the loop level too: whatever parse_session
        # may raise (beyond the stat race it guards itself), one bad/vanished
        # JSONL must not abort the enumeration of every other session in the dir.
        try:
            s = parse_session(jsonl)
        except Exception:
            continue
        if s:
            s["project_name"] = project_dir.name
            sessions.append(s)
    return sessions


# ── LLM summarization via claude -p ──────────────────────────────────────────
PROJECTS_ROOT = _ACTIVE_PROVIDER.history_roots()[0]
# The Claude config root (CLAUDE_CONFIG_DIR or ~/.claude), derived from the SAME
# provider resolution as the transcripts root. The live-session registry lives
# under it; reading the registry from a hard-coded ~/.claude while transcripts come
# from a CLAUDE_CONFIG_DIR-relocated root split-brained discovery — every session
# read as dead/closed, contradicting the documented CLAUDE_CONFIG_DIR support
# (README / CHANGELOG). (#recon-configdir)
CLAUDE_CONFIG_ROOT = PROJECTS_ROOT.parent


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
    # Pre-assign the session id so a run KILLED on timeout (before we can parse the
    # id from stdout) still leaves a DELETABLE orphan. --no-session-persistence
    # cleans up on graceful exit, but a killed claude can leave an in-progress
    # transcript under PROJECTS_ROOT that saikai's own scan would rescan as a
    # phantom session — the old finally ran with session_id='' (a no-op). (#audit-summarizer-leak)
    session_id = str(uuid.uuid4())
    cmd = ["claude", "-p", "--model", model or _summary_model(),
           "--session-id", session_id,
           "--setting-sources", "",
           "--strict-mcp-config",
           "--disable-slash-commands",
           "--no-session-persistence",
           "--output-format", "json"]
    extra: dict = {}
    if sys.platform == "win32":
        extra["creationflags"] = NO_WINDOW  # no inherited console handles

    _payload_sid = ""
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
            _payload_sid = payload.get("session_id", "") or ""
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
        # If claude reported a different id than the one we forced, clean that too.
        if _payload_sid and _payload_sid != session_id:
            _delete_session_files(_payload_sid)


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
    if not isinstance(iso, str):
        return ""          # None/int first_ts must not TypeError inside the except
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
    `★` would be 1 cell, so columns drifted by 1).

    Combining marks / zero-width joiners overlay the previous cell and count 0,
    so accented or ZWJ text doesn't over-count in the plain-text --table path.
    (Multi-codepoint grapheme clusters — flags, VS16 emoji — still can't be
    measured by a per-char function; that needs grapheme segmentation.) (#audit-cellwidth)"""
    o = ord(ch)
    # ZWSP/ZWNJ/ZWJ/BOM and combining marks are zero-width.
    if o in (0x200B, 0x200C, 0x200D, 0xFEFF) or unicodedata.combining(ch):
        return 0
    return 2 if o > 0x2E80 else 1


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
    summary → first user message → Claude's own session name (a project auto-slug
    like "saikai-d1", so only a fallback below the descriptive titles) → the term's
    launch title (e.g. a new session's folder name) → a short id only as a last
    resort, so a tab never shows just a bare session id."""
    if s:
        t = (s.get("custom_title") or s.get("ai_title") or s.get("summary")
             or _first_msg(s) or s.get("claude_name") or "").strip()
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
    # Preserve the cwd's drive-letter casing: Claude transliterates whatever
    # casing the cwd string carries, and real project dirs are predominantly
    # UPPERCASE 'C--…' (Path.cwd() yields 'C:'). The old force-lowercase pointed
    # jsonl_path / project_name at a dir that doesn't exist. Self-heals on the
    # next scan either way, but matching now avoids a transient mismatch. (#audit-drivecase)
    enc = re.sub(r"[:/\\.]", "-", str(cwd))
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


def _complete_dir(val: str, cache: dict | None = None,
                  limit: int = 50) -> list[tuple[str, str]]:
    """Shell-style directory completion for the NewSessionScreen path Input.

    Given a partially-typed path, return up to `limit` child directories of its
    last segment's PARENT whose name matches the partial last segment — e.g.
    "C:/Users/me/CLI/sai" → the dirs under .../CLI starting with "sai". A
    trailing separator means "list this dir's children" (empty partial). Returns
    [(label, full_path)] with label = name + os.sep so the list reads as folders.

    `cache` (optional dict) memoises os.scandir per parent so per-keystroke
    typing within one folder doesn't re-scan. Pure + Textual-free so it unit
    tests without mounting the modal. Best-effort: [] on any error."""
    val = os.path.expanduser((val or "").strip())
    if not val:
        return []
    seps = (os.sep,) if not os.altsep else (os.sep, os.altsep)
    if val[-1] in seps:
        parent, partial = val, ""
    else:
        parent, partial = os.path.dirname(val), os.path.basename(val)
    if not parent:
        parent = "."
    try:
        key = os.path.normcase(os.path.abspath(parent))
    except Exception:
        return []
    names = None if cache is None else cache.get(key)
    if names is None:
        try:
            names = sorted((e.name for e in os.scandir(parent) if e.is_dir()),
                           key=str.lower)
        except Exception:
            names = []
        if cache is not None:
            cache[key] = names
    pl = partial.lower()
    matches = [n for n in names if n.lower().startswith(pl)] if pl else names
    return [(n + os.sep, os.path.join(parent, n)) for n in matches[:limit]]


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
                if not isinstance(obj, dict):
                    continue          # non-dict JSON line must not abort the scan (#audit-codex-nondict)
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
        # Confidence marker reuses --related's legend: ● ≥0.70, dim ● ≥0.40,
        # dim ○ ≥0.20 — so a low-confidence "parent" link is visually
        # distinguishable from a strong one (the forest is heuristic). Encoded in
        # glyph + weight, not hue, so it stays clear of the cyan attention accent.
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


def _preview_detail_rows(s: dict) -> list[str]:
    """The secondary metadata (parent lineage, cwd, worktree, model, edited files,
    full id) — everything NOT in the one-line summary. Shown DIM below the messages
    in the condensed preview (recognition signal first, details on demand), and
    reused nowhere else. Only non-empty rows are emitted."""
    found = s["jsonl_path"]
    rows: list[str] = []
    pid = s.get("parent_id")
    if pid:
        score = s.get("parent_score", 0.0)
        reasons = s.get("parent_reasons", [])
        marker = _confidence_marker(score)
        rs = "  ·  ".join(reasons) if reasons else ""
        rows.append(f"  parent:   {marker} {pid[:8]}  [score {score:.2f}]  {rs}".rstrip())
    # AUTHORITATIVE agent lineage (recorded by claude itself in the transcript,
    # unlike the heuristic `parent:` score above). (#agent-lineage)
    if s.get("parent_session_id"):
        rows.append(f"  spawned by: {s['parent_session_id'][:8]}  (claude agents"
                    + (", sidechain)" if s.get("is_sidechain") else ")"))
    cwd = s.get("cwd", "")
    if cwd:
        rows.append(f"  cwd:      {cwd}")
    wt = s.get("worktree_label") or ""
    if wt:
        rows.append(f"  worktree: {wt}")
    try:
        _ep, _model = _session_surface_model(found)
    except Exception:
        _ep = _model = None
    if _model or _ep:
        _meta = [m for m in (_model, (f"via {_ep}" if _ep else "")) if m]
        rows.append(f"  model:    {'  ·  '.join(_meta)}")
    edited = _extract_edited_files(found)
    if edited:
        rows.append(f"  edited:   {', '.join(edited)}")
    rows.append(f"  id:       {s['id']}")
    # The whole block is secondary — render it dim so the messages above own the eye.
    return [_c(r, DIM) for r in rows]


def _render_preview(s: dict) -> str:
    """Condensed preview: lead with the RECOGNITION signal (title + first/last user
    message) so a glance tells sessions apart, then a single compact context line,
    then the secondary metadata dimmed below. The full labelled header lives one
    Tab away (_render_preview_full)."""
    hidden_tag = "  [HIDDEN]" if s["id"] in _load_hidden() else ""
    title = f"\033[1m{s['ai_title'] or '(no AI title)'}\033[0m{hidden_tag}"
    # One compact context line — start · last · turns · project · branch. No "ago"
    # so it reads right whether fmt_last_active is a relative age (5m) or a date.
    meta = [f"start {fmt_ts(s['first_ts'])}", f"last {fmt_last_active(s)}",
            f"{s['n_turns']} turns", s["jsonl_path"].parent.name]
    branch = s.get("git_branch") or ""
    if branch:
        meta.append(branch)
    lines = [title, _c("  " + "  ·  ".join(meta), DIM), ""]
    # Recognition signal FIRST (was buried under ~10 metadata rows).
    lines.append("\033[36m── First user message ──\033[0m")
    if s["real_msgs"]:
        lines.append(s["real_msgs"][0][:1500])
    else:
        lines.append("(no real user messages)")
    if len(s["real_msgs"]) > 1:
        lines.append("")
        lines.append(f"\033[36m── Last user message  (#{len(s['real_msgs'])}) ──\033[0m")
        lines.append(s["real_msgs"][-1][:1500])
    detail = _preview_detail_rows(s)
    if detail:
        lines.append("")
        lines.append("\033[2m── details ──\033[0m")
        lines.extend(detail)
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
                if not isinstance(obj, dict):
                    continue          # (#audit-codex-nondict)
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
                if not isinstance(obj, dict):
                    continue          # non-dict JSON line must not abort the scan (#audit-codex-nondict)
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
    """Write `render()` to path only if path is missing or its mtime drifts from
    `mtime` (the cache file's own mtime is pinned to the transcript's below).

    The drift tolerance must be TINY: with the old <1.0s window, a transcript
    appended within a second of the cached snapshot read as "fresh" and the
    stale preview persisted until some LATER write moved the mtime by >=1s. A
    float utime->stat round-trip is exact to well under a microsecond, so 1e-6
    keeps FS-precision slack without swallowing real appends. (#audit-codex-cachekey)"""
    if path.exists():
        try:
            if abs(path.stat().st_mtime - mtime) < 1e-6:
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
    # Resolve the transcript FIRST so a cache hit can be freshness-checked:
    # the cache's mtime is pinned to the transcript's by _write_if_stale, and
    # serving a hit blind meant a session that grew since the last TUI warm
    # printed a silently STALE preview forever. (#audit-self-preview-fresh)
    found = _find_session_jsonl(sid)

    def _cache_fresh(cf: Path) -> bool:
        if not cf.exists():
            return False
        if found is None:
            return True          # transcript gone (session deleted) — serve what we have
        try:
            return abs(cf.stat().st_mtime - found.stat().st_mtime) < 1e-6
        except OSError:
            return True
    cache_file = cache_dir / f"{(found.stem if found else sid)}.txt"
    if _cache_fresh(cache_file):
        sys.stdout.write(cache_file.read_text(encoding="utf-8"))
        return
    if not found:
        print(f"(session {sid[:8]} not found)")
        return
    s = parse_session(found)
    if not s:
        print("(unable to parse session)")
        return
    try:
        _write_preview_cache(s)      # re-warm so the next call is fast AND fresh
    except Exception:
        pass
    print(render(s))


def preview_session(session_id: str) -> None:
    _preview_impl(session_id, PREVIEW_DIR, _render_preview)


def preview_session_full(session_id: str) -> None:
    _preview_impl(session_id, PREVIEW_FULL_DIR, _render_preview_full)


_MARKER_BLANK = " "

# Single source for the LIVE-status markers in the session list — glyph + colour
# together, so the activity column and its tint can't drift. The tab labels
# (saikai_terminal.STATUS_GLYPH) now use the SAME glyph vocabulary (? ~ =), so a
# glyph reads the same in the list and on a tab; they can't share one Python
# constant (STATUS_GLYPH lives in the terminal module, which loads after this), so
# keep the two in step when adding or renaming a status. "idle" is intentionally
# absent here: the list resolves it to "!" (reply-due) vs "=" from app state in
# _refresh_table, not from the raw status.
# saikai's SINGLE attention accent. Exactly one saturated colour across the whole
# marker vocabulary means "this needs you right now" — a live session waiting on
# input, a dormant session whose last turn was yours, or a background agent blocked
# on your clarification. Everything else reads in calm greyscale WEIGHT: running
# work at normal weight (visible, not shouting), quiet/open-elsewhere/background
# state dimmed. So the eye lands on exactly what's actionable instead of parsing a
# dozen competing hues. RED is reserved for a genuine failure only; favourite (gold)
# and hidden live in a SEPARATE column (a different axis: user tags, not urgency).
ATTENTION = CYAN
_ATTENTION_STYLE = "bold cyan"   # Rich/Textual equivalent for the TUI table tint

_LIVE_MARKER = {
    "waiting": ("?", _ATTENTION_STYLE),   # needs you NOW — the single attention accent
    "busy":    ("~", ""),                 # working — normal weight (default fg), not accented
}

# Per-glyph Rich style for the TUI list's activity marker. Three tiers only:
# ATTENTION accent (needs you) · default (running now) · dim (quiet / elsewhere /
# background). The live-state styles come from _LIVE_MARKER (single source); the
# rest are file-registry / reply-due markers. bg (&) re-tints by job state in
# _marker_tint so a BLOCKED agent still gets the accent. Glyphs not listed = default.
_MARKER_COLOR = {_g: _c for _g, _c in _LIVE_MARKER.values()}
_MARKER_COLOR.update({
    "!": _ATTENTION_STYLE,   # reply due — needs you (dormant): same accent as waiting
    "=": "dim",              # idle live pane, no reply due
    "R": "dim",              # Remote Control in another session (someone else's)
    "@": "dim",              # open in another window
    "$": "dim",              # open & running a shell command elsewhere
    "&": "dim",              # bg agent/job (job STATE re-tints via _marker_tint)
    "+": "dim",              # recently active
    ".": "dim",              # recent (dormant)
})


def _marker_tint(glyph: str, s: dict) -> str:
    """Rich style for a TUI activity glyph. A background agent (&) re-tints by job
    state so a BLOCKED agent (awaiting your clarification) gets the same attention
    accent as a waiting session, and a failed one goes red — the two bg states you'd
    act on — while a merely-running/done bg stays dim like the rest of the calm tier."""
    if glyph == "&":
        if s.get("job_needs"):
            return _ATTENTION_STYLE                       # blocked → needs you (accent)
        if s.get("job_state") in ("failed", "stopped"):
            return "red"
        return "dim"
    return _MARKER_COLOR.get(glyph, "")


# Markers are intentionally ASCII (1 cell, terminal-width-independent). The
# previous Unicode glyphs (◉●○★✗) were East-Asian-Ambiguous, which made their
# cell count depend on the terminal's CJK-width setting — and saikai can't
# reliably probe that, so columns drifted whenever the user's terminal didn't
# match the static assumption. Letters trade a bit of visual flair for
# reliable column alignment everywhere.
_TABLE_NA_CACHE: dict = {}   # mtime-keyed reply-due cache for the --table activity column


def _activity_marker(s: dict) -> str:
    """Activity column: bg / Remote Control / open / active / reply-due / recent."""
    # Three tiers of colour only: ATTENTION accent (needs you) · default (running
    # now) · DIM (quiet / open-elsewhere / background). RED = genuine failure only.
    if s.get("is_bg"):
        # Same glyph '&' (no new marker — '?' already means live-waiting); the bg
        # JOB state is conveyed by COLOUR so it can't collide with other markers.
        _jst = s.get("job_state")
        if s.get("job_needs"):
            return _c("&", ATTENTION, BOLD)  # bg agent BLOCKED — awaiting your clarification
        if _jst in ("failed", "stopped"):
            return _c("&", RED)              # bg job ended abnormally
        return _c("&", DIM)                  # running / done bg agent — calm tier
    if s.get("remote_origin"):
        return _c("s", DIM)              # Desktop-SSH mirror of a REMOTE host's session
    if s.get("is_remote_control"):
        return _c("R", DIM)              # Remote Control elsewhere — someone else's session
    if s.get("is_open"):
        _ss = s.get("session_status")
        if _ss == "busy":
            return _c("@")               # open & responding — running now (default weight)
        if _ss == "shell":
            return _c("$", DIM)          # open & running a shell command elsewhere
        return _c("@", DIM)              # open & idle in another Claude window
    if s.get("is_active"):
        return _c("+", DIM)
    if _needs_attention(s, _TABLE_NA_CACHE):
        return _c("!", ATTENTION, BOLD)  # dormant: your last turn is unanswered — needs you
    if s.get("is_recent"):
        return _c(".", DIM)
    return _MARKER_BLANK


def _state_marker(s: dict, hidden: set, favorites: set) -> str:
    """State column: favorite or hidden (mutually exclusive)."""
    sid = s["id"]
    if sid in favorites:
        return _c("*", GOLD)
    if sid in hidden:
        return _c("x", RED)
    return _MARKER_BLANK


def _marker_legend(s: dict, favorites: set, hidden: set) -> list:
    """Plain-language meaning of the activity + state markers THIS session
    currently shows — mirrors _activity_marker / _state_marker precedence so the
    preview can explain its own +/./*/@/&/… glyphs in context. At most one
    activity entry + one state entry (the two columns each show one glyph)."""
    out = []
    if s.get("is_bg"):
        out.append("& agents/bg session (owned by another claude — resumable when it ends)"
                   if s.get("live_kind") == "agent" else "& background agent/job")
    elif s.get("remote_origin"):
        out.append("s ssh-remote session (ran on another host via Claude Desktop"
                    " — not resumable here)")
    elif s.get("is_remote_control"):
        out.append("R Remote Control on in another session")
    elif s.get("is_open"):
        ss = s.get("session_status")
        if ss == "shell":
            out.append("$ open, running a shell command elsewhere")
        elif ss == "busy":
            out.append("@ open, responding in another window")
        else:
            out.append("@ open in another window")
    elif s.get("is_active"):
        out.append("+ recently active")
    elif _needs_attention(s, _TABLE_NA_CACHE):
        out.append("! reply due — your last turn is unanswered")
    elif s.get("is_recent"):
        out.append(". recent (dormant)")
    sid = s.get("id")
    if sid in favorites:
        out.append("* favorite")
    elif sid in hidden:
        out.append("x hidden")
    return out


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
    # Lead with the ONE accent (needs-you); the calm tier is listed plainly, the
    # fav/hidden tags keep their own column colours. Short on purpose.
    legend = (f"  {len(sessions)} sessions{mode_tag}  ·  "
              f"{_c('!', ATTENTION, BOLD)}/{_c('?', ATTENTION, BOLD)} needs you  "
              f"~ running  @ open  & bg  ·  "
              f"{_c('*', GOLD)} fav  {_c('x', RED)} hidden  ·  saikai to resume")
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
            # even if the console mode was reset on the way down. If VT can't be
            # enabled (a genuine legacy conhost that never supported it), the mouse/
            # focus/paste modes below were never enabled either — and writing raw
            # ANSI would print visible garbage on every exit path. Bail instead.
            vt_ok = False
            try:
                import ctypes
                k32 = ctypes.windll.kernel32
                h = k32.GetStdHandle(-12)        # STD_ERROR_HANDLE
                mode = ctypes.c_uint32()
                if k32.GetConsoleMode(h, ctypes.byref(mode)):
                    if mode.value & 0x0004:      # already VT-enabled
                        vt_ok = True
                    elif k32.SetConsoleMode(h, mode.value | 0x0004):  # ENABLE_VIRTUAL_TERMINAL_PROCESSING
                        vt_ok = True
            except Exception:
                vt_ok = False
            if not vt_ok:
                return
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
#
# LOW-SATURATION by design (user: the saturated ANSI set read as garish and
# fought the calm baseline): tinting is a GROUPING cue, not an accent, so it
# must sit close to the neutral foreground. CYAN IS BANISHED from both
# palettes — it is the reserved needs-you accent and must never appear on a
# row that doesn't need you. (#palette-muted)
_PROJECT_PALETTE = ("#9aa5ce", "#b8a965", "#8fae8b", "#b48ead", "#7f9fbf",
                    "#bf8f8f", "#a8a8a8", "#87afaf", "#c0a36e",
                    "#95b1a4", "#a48fbf")
_TOPIC_PALETTE = ("#b48ead", "#9aa5ce", "#b8a965", "#8fae8b", "#7f9fbf",
                  "#bf8f8f", "#a8a8a8", "#a48fbf", "#87afaf",
                  "#c0a36e", "#95b1a4")


def _stable_color(value: str, palette) -> str:
    """A value's hue is a PURE function of the value: `palette[hash % len]`, the
    same in every view and across runs, independent of what else is on screen.
    Two different values may collide onto one colour (the cell text + state
    marker still disambiguate) — we deliberately trade guaranteed-distinct hues
    for a STABLE association, so a project doesn't change colour when you filter,
    sort, or a new session appears. (#stable-hue)"""
    if not value:
        return ""
    import hashlib
    h = int(hashlib.md5(value.encode("utf-8")).hexdigest(), 16)
    return palette[h % len(palette)]


def _build_color_map(values, palette) -> dict[str, str]:
    """value → stable hue for a column/title (project | worktree | topic).

    Each unique value maps to `_stable_color(v)` — a pure function of the value,
    so the same project/topic renders in the same colour in every view and never
    shifts when the visible set changes (filter / search / sort / a new session
    appearing). This favours recognition STABILITY over distinctness: with only
    len(palette) hues, two values can collide onto one colour once enough are
    visible (birthday-bound), but the cell text and the state marker still tell
    them apart.

    (Earlier this linear-probed the *visible* set to guarantee distinct hues, but
    that made a value's colour depend on its co-visible neighbours — so a project
    changed colour mid-session on a filter/search/sort. #stable-hue)"""
    return {v: _stable_color(v, palette) for v in {v for v in values if v}}


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


def _mem_safety_mode() -> str:
    """Live-pane memory safety as ONE knob: 'on' (default, balanced) | 'off'
    (minimal — refuse only at true exhaustion, plus the max_live cap) | 'strict'
    (refuse earlier, keep more headroom, hard-refuse instead of warn). The
    granular limits.* thresholds are advanced overrides that still win when set."""
    m = str(_cfg("limits", "memory_safety", "SAIKAI_MEM_SAFETY", "on")).strip().lower()
    return m if m in ("on", "off", "strict") else "on"


def _mem_safety_preset() -> dict:
    """Threshold DEFAULTS for the current safety mode, fed as the defaults to the
    granular _cfg reads below — so an explicitly-set limits.* / SAIKAI_* knob still
    overrides. EVERY mode keeps true-exhaustion protection (_ram_fit never opens a
    pane when the estimated per-pane RAM isn't actually free); the mode only tunes
    how much conservative HEADROOM to hold back on top of that."""
    mode = _mem_safety_mode()
    if mode == "off":
        return dict(max_load=200.0, pressure=200.0, commit_mb=0.0,
                    phys_pct=0.0, phys_mb=0.0, hard=False)
    if mode == "strict":
        return dict(max_load=max(0.0, _DEFAULT_MAX_LOAD - 10.0), pressure=6.0,
                    commit_mb=4096.0, phys_pct=15.0, phys_mb=0.0, hard=True)
    return dict(max_load=_DEFAULT_MAX_LOAD, pressure=10.0, commit_mb=2048.0,
                phys_pct=8.0, phys_mb=0.0, hard=False)


def _ram_gate_kwargs() -> dict:
    """Live-pane gate thresholds resolved env > config > memory_safety preset (spec
    §A.1). Shared by the open-gate and the statusbar 'fit' indicator so they can't
    disagree. The preset supplies the DEFAULT; a granular limits.* knob overrides."""
    _pre = _mem_safety_preset()
    return dict(
        max_load=_cfg("limits", "max_memory_load", "SAIKAI_MAX_MEM_LOAD", _pre["max_load"], float),
        min_commit_mb=_cfg("limits", "min_commit_headroom_mb", "SAIKAI_MIN_COMMIT_MB", _pre["commit_mb"], float),
        min_free_phys_pct=_cfg("limits", "min_free_phys_pct", "SAIKAI_MIN_FREE_PHYS_PCT", _pre["phys_pct"], float),
        min_free_phys_mb=_cfg("limits", "min_free_mb", "SAIKAI_MIN_FREE_MB", _pre["phys_mb"], float),
        max_pressure=_cfg("limits", "max_memory_pressure", "SAIKAI_MAX_MEM_PRESSURE", _pre["pressure"], float),
    )


def _ram_per_pane_mb() -> float:
    """Estimated RAM per live pane (env > config > default)."""
    return _cfg("limits", "per_pane_mb", "SAIKAI_CLAUDE_MB", 600.0, float)


_CTX_USAGE_CACHE: dict = {}    # str(path) -> (mtime, size, (tokens, model))


def _usage_int(v) -> int:
    """Coerce a transcript usage field to int, best-effort. Healthy transcripts
    carry ints, but one corrupt/foreign record ("12k", None, a float string)
    must degrade to 0 — the raw int() here leaked ValueError/TypeError out of
    the gauge on every statusbar rebuild. (#audit-hostile-usage)"""
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def _ctx_usage_from_jsonl(path) -> "tuple[int | None, str | None]":
    """(live context size, model id) from the LAST transcript record that carries a
    usage block: tokens = input + cache_read + cache_creation input tokens (the
    number `/context` shows), model = that turn's message.model. Ground truth, no
    estimation. (None, None) if the file is unreadable or has no usage yet. Reads
    only the tail (transcripts are large).

    Cached on (mtime, size): the statusbar gauge re-reads this on every list rebuild
    AND on every cursor move, so a 400 KB tail re-read + json parse per call would
    jank the UI (the context-lifecycle spec mandates this (mtime,size) cache)."""
    try:
        import os
        st = os.stat(path)
        size, mtime = st.st_size, st.st_mtime
    except (OSError, ValueError, TypeError):
        return None, None
    key = str(path)
    hit = _CTX_USAGE_CACHE.get(key)
    if hit is not None and hit[0] == mtime and hit[1] == size:
        return hit[2]
    try:
        with open(path, "rb") as f:
            f.seek(max(0, size - 400_000))      # tail: the last usage is near the end
            chunk = f.read().decode("utf-8", "replace")
    except (OSError, ValueError):
        return None, None
    last = None
    last_model = None
    # split on '\n' ONLY: str.splitlines() also breaks on U+2028/U+2029/U+0085,
    # which Claude writes verbatim inside message content (ensure_ascii=False),
    # fragmenting a record so json.loads fails and the usage is silently dropped.
    for ln in chunk.split("\n"):
        ln = ln.strip()
        if not ln.startswith("{") or '"usage"' not in ln:
            continue
        try:
            msg = (json.loads(ln).get("message") or {})
            u = msg.get("usage") if isinstance(msg, dict) else None
        except Exception:
            continue
        if isinstance(u, dict) and "input_tokens" in u:
            _model = msg.get("model") if isinstance(msg, dict) else None
            _toks = (_usage_int(u.get("input_tokens"))
                     + _usage_int(u.get("cache_read_input_tokens"))
                     + _usage_int(u.get("cache_creation_input_tokens")))
            # Skip <synthetic> / all-zero interrupt records (written on Esc / abort /
            # API error): they carry a usage block but 0 tokens, so accepting one as
            # the "last usage" would make the gauge read 0K (empty/green) and MASK a
            # nearly-full window — inverting the safety-relevant context-fill reading
            # that informs checkpoint decisions. Keep the last REAL usage + its model
            # (so window inference isn't degraded to the synthetic 200K default). (#H5)
            if _toks <= 0 or _model == "<synthetic>":
                continue
            last = u
            last_model = _model
    if last is None:
        result = (None, None)
    else:
        tokens = (_usage_int(last.get("input_tokens"))
                  + _usage_int(last.get("cache_read_input_tokens"))
                  + _usage_int(last.get("cache_creation_input_tokens")))
        result = (tokens, last_model)
    _CTX_USAGE_CACHE[key] = (mtime, size, result)   # cache the (None,None) too
    return result


def _ctx_tokens_from_jsonl(path) -> "int | None":
    """Live context size only (see _ctx_usage_from_jsonl) — for callers/tests that
    don't need the model id."""
    return _ctx_usage_from_jsonl(path)[0]


# ── b2 (Task 11): human-gated checkpoint → /handoff → confirm → /clear → reseed.
# Pure helpers below; the tick state machine lives on the App (action_checkpoint
# + _b2_tick). The flow is intentionally a tick-driven machine — NEVER a blocking
# wait — because the PTY reader threads marshal onto the UI thread, so a UI-thread
# sleep/poll-loop would freeze every pane (ARCHITECTURE.md concurrency invariant).


# The ordered states the b2 machine advances through (one step per tick). Exposed
# as a function so the safety ORDERING (the destructive /clear sits AFTER the
# human confirm AND after the handoff settles) is unit-testable without any I/O.
_B2_STEPS = (
    "inject_handoff",       # paste saikai's handoff prompt (non-destructive)
    "await_handoff_idle",   # wait for the turn to settle (status back to idle)
    "extract_prompt",       # read the NEW SESSION PROMPT out of the transcript
    "confirm",              # push ConfirmRefreshScreen — the HUMAN GATE
    "inject_clear",         # ONLY after confirm: snapshot sids, paste /clear
    "detect_child",         # bind the fresh child sid (falsifiable, see below)
    "inject_reseed",        # paste the parent's NEW SESSION PROMPT into the child
    "verify_reseed",        # confirm the reseed SUBMITTED (busy) — resend CR if not:
    #                         claude's post-/clear re-init absorbs a too-early CR
    #                         (measured on v2.1.198; the paste survives, the CR dies),
    #                         and without this gate b2 toasted "done" on an empty,
    #                         never-reseeded child. (#audit-b2-reseed-cr)
    "record_lineage",       # _set_lineage(child, parent, parent_jsonl)
)


def _b2_step_sequence() -> tuple:
    """The ordered b2 state names. Pure — unit-tested for the safety invariant
    that inject_clear comes after both `confirm` and `await_handoff_idle`."""
    return _B2_STEPS


# The handoff instructions b2 injects, instead of depending on a personal
# `/handoff` slash-command skill being present in the controlled pane (it isn't a
# Claude Code built-in). Self-contained so b2 works in any session / machine /
# user; deliberately generic (no personal or English-harness sections). It is a
# PLAIN prompt, not a slash command, so it also dodges the slash-palette CR-absorb
# the spike hit. It MUST end with a fenced block whose first line is exactly
# `NEW SESSION PROMPT` — that is what _extract_handoff_prompt slices out, the
# confirm modal shows, and inject_reseed pastes into the fresh session.
_B2_HANDOFF_PROMPT = (
    "Wrap up THIS session so a brand-new session can resume the work. Do not keep "
    "working here. Goal: hand off the least context that is still SUFFICIENT — "
    "short on narration, complete on anything expensive or impossible to rederive.\n"
    "Write it from what you already know in this conversation: do NOT run tools or "
    "commands for this handoff — if something was not actually observed, mark it "
    "UNVERIFIED instead of checking now.\n"
    "\n"
    "DROP: exploration play-by-play, tool output, long quotes, history. (The old "
    "session stays reopenable, so detail is recoverable — don't pad.)\n"
    "KEEP even at the cost of length (never drop these to save space):\n"
    "- WHY behind each key decision, not just the decision — and what it rules out.\n"
    "- What you RULED OUT and why (so the next session doesn't retry a dead end).\n"
    "- Exact state to resume from: branch / dirty files / services up / env vars "
    "(BY NAME) / the precise failing command + its last observed output / "
    "migrations / flags — whatever applies to this kind of work.\n"
    "- The user's standing constraints and preferences stated this session (tech "
    "choices, \"don't touch X\", scope, deadline, tone/audience).\n"
    "- Stable identifiers: file paths, PR/issue/commit/run IDs, URLs, doc paths, "
    "and ANY opaque handle a later command will consume (message/draft/record IDs, "
    "ARNs, container/job IDs). Reproduce these VERBATIM and in FULL — never shorten, "
    "truncate, or \"...\"-elide an identifier value to save space, however long or "
    "random-looking it is; the next session cannot guess the missing part and cannot "
    "run the command without it. When two IDs differ only near the end, keep both "
    "complete.\n"
    "- WHERE to look: the 2-3 files/functions/sources that are the center of "
    "gravity, so the next session reads the right thing instead of re-exploring.\n"
    "\n"
    "Status discipline (guards against false \"done\"): give every work item a "
    "status — DONE / IN-PROGRESS / NOT-STARTED. For anything DONE or \"verified\", "
    "state the exact evidence (command run + result observed); if you did not "
    "actually observe it, mark it UNVERIFIED. Never write \"done\" from assumption.\n"
    "\n"
    "Write a short human summary: 1) current goal  2) decided + why / ruled out  "
    "3) what changed or was produced + how verified (files, OR a published doc / "
    "query result / provisioned resource / drafted section — whatever this session "
    "produced; \"none\" if nothing)  4) state to resume from + where to look  "
    "5) constraints, preferences, gotchas  6) open questions.\n"
    "\n"
    "Write in the language this session has been using.\n"
    "\n"
    "Then END your reply with ONE fenced code block and NOTHING after it. Its FIRST "
    "line must be exactly: NEW SESSION PROMPT\n"
    "The block is the fresh session's ONLY memory of this work — it must stand fully "
    "alone: no \"as discussed / above / earlier\"; restate the goal, key paths, the "
    "resume state, and the immediate next step inline. Never inline secrets, tokens, "
    "credentials, or PII — refer to them by name and location. This carve-out does "
    "NOT cover non-secret resource identifiers (message/draft/record/commit/run IDs, "
    "paths, URLs): when the immediate next step runs a command against one, paste the "
    "FULL identifier inline — an abbreviated or \"...\"-elided ID makes this block "
    "non-actionable. Keep it compact; do "
    "not put triple-backtick fences inside it (indent code or use single backticks)."
)

HANDOFF_PROMPT_FILE = CACHE_DIR / "handoff-prompt.md"


def _handoff_prompt_path() -> Path:
    """Override-file path for the b2 handoff prompt: SAIKAI_HANDOFF_PROMPT_FILE /
    [checkpoint] handoff_prompt_file (registered in _CONFIG_SPECS with a "" default),
    else CACHE_DIR/handoff-prompt.md."""
    p = _cfg("checkpoint", "handoff_prompt_file", "SAIKAI_HANDOFF_PROMPT_FILE", "", str)
    return Path(p).expanduser() if p else HANDOFF_PROMPT_FILE


def _resolve_handoff_prompt() -> "tuple[str, str | None]":
    """The handoff prompt b2 injects: the user's override file if present AND it
    still carries the load-bearing `NEW SESSION PROMPT` contract, else the built-in
    default. Returns (prompt, warning): a non-None warning means an override file
    existed but was REJECTED (the caller should toast + fall back). Pure read."""
    path = _handoff_prompt_path()
    try:
        if path.is_file():
            text = path.read_text(encoding="utf-8", errors="replace").strip()
            if text:
                if "NEW SESSION PROMPT" not in text.upper():
                    return (_B2_HANDOFF_PROMPT,
                            f"{path.name}: missing the required 'NEW SESSION PROMPT' "
                            f"contract — using the built-in handoff prompt")
                return (text, None)
    except OSError:
        pass
    return (_B2_HANDOFF_PROMPT, None)


def _extract_handoff_prompt(text: "str | None") -> "str | None":
    """Slice the `NEW SESSION PROMPT` block out of a /handoff assistant turn.

    Tolerates the two shapes claude's /handoff produces: a fenced ``` block whose
    first line is `NEW SESSION PROMPT`, or a markdown header (`## NEW SESSION
    PROMPT`) followed by the prompt. Returns the inner prompt text (stripped), or
    None when there is no NEW SESSION PROMPT marker (never guess — b2 aborts +
    toasts on None rather than reseeding with garbage)."""
    if not text:
        return None
    lines = text.splitlines()
    n = len(lines)
    marker = "NEW SESSION PROMPT"

    def _fence_char(ln):
        # The fence token of a fence line: '`' for ```, '~' for ~~~, else "".
        s = ln.strip()
        if s.startswith("```"):
            return "`"
        if s.startswith("~~~"):
            return "~"
        return ""

    def _opener_char(idx):
        # Fence char of the opening fence on the previous non-empty line, else "".
        k = idx - 1
        while k >= 0 and not lines[k].strip():
            k -= 1
        return _fence_char(lines[k]) if k >= 0 else ""

    def _is_close_fence(ln, ch):
        # A CLOSING fence is a line of ONLY the fence char (>=3), no info string —
        # so a "```bash" opener or an inner indented "```lang" is NOT mistaken for
        # the close, and a ~~~ block closes on ~~~ (not on a stray ```).
        s = ln.strip()
        return len(s) >= 3 and set(s) == {ch}

    def _body_after(idx, ch):
        body = []
        j = idx + 1
        if not ch:
            # Header/bold marker ("## NEW SESSION PROMPT" / "**…**") followed by a
            # fenced block that does NOT repeat the marker inside — a shape models
            # produce often. The old bare-mode "stop at any fence" returned "" here
            # (the very next line IS the fence), silently aborting the checkpoint.
            # Instead, step INTO that fence and collect its body. (#audit-b2-extract)
            k = j
            while k < n and not lines[k].strip():
                k += 1
            fch = _fence_char(lines[k]) if k < n else ""
            if fch:
                j, ch = k + 1, fch
        while j < n:
            if ch:
                if _is_close_fence(lines[j], ch):
                    break
            elif _fence_char(lines[j]):          # bare/header: stop at any fence line
                break
            body.append(lines[j])
            j += 1
        return "\n".join(body).strip()

    markers = [i for i, ln in enumerate(lines) if marker in ln.upper()]
    # The real block is the LAST one: the prompt says "END with ONE fenced block",
    # and replies often NARRATE the phrase or show an EXAMPLE block first. So scan
    # markers in REVERSE and PREFER a marker that opens a fenced block (pass 1) over
    # a bare/header match (pass 2) — an earlier prose echo or example can't win.
    for prefer_fenced in (True, False):
        for i in reversed(markers):
            ch = _opener_char(i)
            if prefer_fenced and not ch:
                continue
            if not prefer_fenced and ch:
                continue                          # in-fence markers handled in pass 1
            out = _body_after(i, ch)
            if out:
                return out
    return None


def _last_assistant_text_from_jsonl(path) -> "str | None":
    """The text of the LAST assistant turn in a transcript (the /handoff output).

    Tail-read like _ctx_tokens_from_jsonl (transcripts are large); decode the
    assistant message content with the same _extract_text helper the parser uses.
    None if unreadable or there is no assistant text yet."""
    try:
        import os
        size = os.path.getsize(path)
    except (OSError, ValueError):
        return None
    # Tail-read first (transcripts are large). A final assistant turn bigger than
    # the window (e.g. a long /handoff reply) would land the seek INSIDE that line,
    # so its truncated fragment fails the prefilter and the whole turn is lost —
    # fall back to a whole-file read only when the tail found nothing. (#audit-tailseek)
    for window in (400_000, size):
        try:
            with open(path, "rb") as f:
                f.seek(max(0, size - window))
                chunk = f.read().decode("utf-8", "replace")
        except (OSError, ValueError):
            return None
        last = None
        for ln in chunk.split("\n"):       # '\n' only (see _ctx_usage_from_jsonl)
            ln = ln.strip()
            if not ln.startswith("{") or '"assistant"' not in ln:
                continue
            try:
                obj = json.loads(ln)
            except Exception:
                continue
            if not isinstance(obj, dict):
                continue              # (#audit-codex-nondict)
            if obj.get("type") != "assistant":
                continue
            msg = obj.get("message") or {}
            txt = _extract_text(msg.get("content", "")) if isinstance(msg, dict) else ""
            if txt:
                last = txt
        if last is not None or window >= size:
            return last
    return last


def _session_turns(path, byte_window: int = 1_500_000, limit: int = 200) -> list:
    """User/assistant turns from a transcript as (role, text), oldest→newest — the
    backing list for the copy picker (so off-screen messages can be copied from the
    log, which the alt-screen pane can't select). Tail-reads the last `byte_window`
    bytes (transcripts get large) and returns at most the last `limit` non-empty
    turns. Reuses the parser's _extract_text. Best-effort: [] on any error. A
    window that slices mid-line just drops that one oldest partial turn. (#copy-response)"""
    try:
        import os
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            f.seek(max(0, size - byte_window))
            chunk = f.read().decode("utf-8", "replace")
    except (OSError, ValueError, TypeError):
        return []
    out = []
    for ln in chunk.split("\n"):       # '\n' only (see _ctx_usage_from_jsonl)
        ln = ln.strip()
        if not ln.startswith("{") or ('"user"' not in ln and '"assistant"' not in ln):
            continue
        try:
            obj = json.loads(ln)
        except Exception:
            continue
        if not isinstance(obj, dict):
            continue
        role = obj.get("type")
        if role not in ("user", "assistant"):
            continue
        msg = obj.get("message")
        if not isinstance(msg, dict):
            continue
        txt = _extract_text(msg.get("content", ""))
        if txt and txt.strip():
            out.append((role, txt.strip()))
    return out[-limit:]


def _flatten_turns(turns) -> str:
    """Render (role, text) turns into ONE plain-text document for the vi copy-mode
    view, each turn prefixed with a role header so message boundaries are visible.
    (#copy-mode)"""
    parts = []
    for role, text in turns:
        tag = "claude" if role == "assistant" else "you"
        parts.append(f"───── {tag} ─────")
        parts.append(text)
        parts.append("")
    return "\n".join(parts).rstrip("\n")


def _first_cwd_from_jsonl(path) -> "str | None":
    """The first `cwd` in a transcript, scanning the first several records.

    Spike finding #3: a freshly /clear'd child's record 1 is {"type":"mode"} and
    record 2 is file-history-snapshot — `cwd` first appears on the early
    `attachment` records. So scan a window of leading records, not just record 1.
    None if no cwd appears (or the file is unreadable)."""
    try:
        with open(path, "rb") as f:
            scanned = 0
            for line in f:
                if scanned == 0 and line.startswith(b"\xef\xbb\xbf"):
                    line = line[3:]
                scanned += 1
                if scanned > 30:                # generous window; cwd is in the first few
                    break
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                if not isinstance(obj, dict):
                    continue          # a [] line aborted b2 child detection (#audit-codex-nondict)
                if isinstance(obj.get("cwd"), str) and obj["cwd"]:
                    return obj["cwd"]
    except OSError:
        return None
    return None


def _first_ts_from_jsonl(path) -> "str | None":
    """The first ISO8601 `timestamp` in a transcript (scanning leading records),
    used to confirm a candidate child session post-dates the /clear. None if
    absent/unreadable."""
    try:
        with open(path, "rb") as f:
            scanned = 0
            for line in f:
                if scanned == 0 and line.startswith(b"\xef\xbb\xbf"):
                    line = line[3:]
                scanned += 1
                if scanned > 30:
                    break
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                if not isinstance(obj, dict):
                    continue          # (#audit-codex-nondict)
                ts = obj.get("timestamp")
                if isinstance(ts, str) and ts:
                    return ts
    except OSError:
        return None
    return None


def _parse_iso_aware(s):
    """Parse an ISO8601 timestamp to a timezone-AWARE datetime for safe ordering.
    Transcript timestamps are UTC with a trailing 'Z'; a naive value (no offset)
    is interpreted as the host's LOCAL time (what `time.strftime` produced before
    clear_ts was switched to UTC). None if unparseable — callers must treat that
    like a missing timestamp, never as "older than the clear"."""
    if not isinstance(s, str) or not s:
        return None
    t = s.strip()
    if t.endswith("Z"):
        t = t[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(t)
    except ValueError:
        return None
    return dt.astimezone() if dt.tzinfo is None else dt


_TS_EPOCH = datetime.fromtimestamp(0, timezone.utc)


def _iso_sort_key(ts) -> datetime:
    """CHRONOLOGICAL sort key for transcript ISO timestamps. Raw string
    comparison mis-orders mixed offsets — "2026-01-01T00:30:00+09:00" sorts
    lexically AFTER "2025-12-31T16:00:00Z" yet is 30 minutes EARLIER — so every
    first_ts sort parses tz-aware; missing/unparseable sinks to the epoch
    (oldest end). (#audit-codex-tsort)"""
    dt = _parse_iso_aware(ts)
    return dt if dt is not None else _TS_EPOCH


def _cleared_child_candidates(project_dir, pre_existing_sids, pane_cwd, clear_ts) -> list:
    """ALL post-/clear candidate child sids (the falsifiable filter, minus the
    single-survivor collapse) so callers can apply stability/confirmation logic.

    Among the *.jsonl whose stem is NOT in the pre-/clear snapshot, keep only
    those whose first-record cwd matches the pane AND whose first timestamp
    post-dates the clear. Pure (filesystem read only)."""
    try:
        from pathlib import Path as _P
        pd = _P(project_dir)
        present = {p.stem: p for p in pd.glob("*.jsonl")}
    except Exception:
        return []
    pre = set(pre_existing_sids or ())
    candidates = []
    for stem, p in present.items():
        if stem in pre:
            continue
        cwd = _first_cwd_from_jsonl(p)
        if pane_cwd and cwd != pane_cwd:
            continue                            # contamination: different pane
        ts = _first_ts_from_jsonl(p)
        # Post-date check, tz-AWARE: the transcript ts is UTC ('Z') and clear_ts
        # is UTC too, but parse both so a stray naive/offset value still orders
        # correctly across host timezones (a raw string compare wrongly rejected
        # the child on +UTC-offset hosts). Only reject when BOTH parse and the
        # candidate genuinely pre-dates the clear; a missing/unparseable ts on a
        # cwd-matched brand-new child shouldn't reject it.
        if clear_ts and ts:
            _cdt = _parse_iso_aware(clear_ts)
            _tdt = _parse_iso_aware(ts)
            if _cdt and _tdt and _tdt < _cdt:
                continue
        candidates.append(stem)
    return candidates


def _bind_cleared_child(project_dir, pre_existing_sids, pane_cwd, clear_ts):
    """Falsifiably bind the child session minted by /clear (spike finding #6).

    `/clear` mints exactly ONE new `<sid>.jsonl`, but unrelated new transcripts
    also appear in the same project dir from other lifecycle events (a sibling
    pane's first turn, a session flushing on exit). Return the single survivor,
    or None on 0 or ≥2 candidates — record NO lineage and toast rather than
    guess. Pure (filesystem read only); unit-tested. (The b2 detect_child caller
    additionally requires the single survivor to be STABLE across consecutive
    ticks before binding, to defeat a contaminant-lands-first TOCTOU race.)"""
    candidates = _cleared_child_candidates(project_dir, pre_existing_sids, pane_cwd, clear_ts)
    return candidates[0] if len(candidates) == 1 else None


def _project_sids(project_dir) -> set:
    """The set of session sids (jsonl stems) currently present in a project dir.
    Snapshotted BEFORE /clear so the post-clear diff is falsifiable."""
    try:
        from pathlib import Path as _P
        return {p.stem for p in _P(project_dir).glob("*.jsonl")}
    except Exception:
        return set()


_CTX_TIERS = (200_000, 1_000_000)

# Model id families that offer a 1M-token context window. The transcript records
# only the BASE model id (no `[1m]` suffix), so a session's true window can't be
# read back -- but when the model CAN do 1M we default the gauge to the 1M window,
# because 1M is the common mode for these models now. The rare cost: a 200K-mode
# session on one of these models reads as a low % and won't redden until ~700K;
# set SAIKAI_CTX_WINDOW=200000 (or [context] window) to pin it.
_CTX_1M_MODELS = ("opus-4", "sonnet-4")


def _model_supports_1m(model) -> bool:
    """True when `model` (a transcript base id like 'claude-opus-4-8') is from a
    family that offers a 1M context window. Pure."""
    if not model:
        return False
    m = str(model).lower()
    return any(tag in m for tag in _CTX_1M_MODELS)


def _ctx_window_for(tokens, override=None, model=None) -> int:
    """Context window for a session. `override` (env/config) wins. Else, if the
    turn's model is 1M-capable, default to the 1M window -- the base model id can't
    tell a 1M session from a 200K one, and 1M is the common mode. Else infer the
    smallest tier that fits the observed count (a 720K reading can't be 200K)."""
    try:
        _ov = int(override) if override else 0
    except (TypeError, ValueError):
        _ov = 0
    if _ov > 0:                    # a 0/negative SAIKAI_CTX_WINDOW rendered
        return _ov                 # "ctx 0K/0K (-2000%)" garbage (#audit-self-ctxwin)
    if _model_supports_1m(model):
        return 1_000_000
    for t in _CTX_TIERS:
        if tokens <= t:
            return t
    return _CTX_TIERS[-1]


# Statusbar RAM indicator. The headroom ("~N fit") is the deductive precursor —
# how many more panes fit before the gate trips, derived from _ram_fit. These add
# a severity colour + a ⚠ to the system-load reading so the box "getting heavy" is
# visible in the WARN band (approaching the gate) rather than only once it trips.
_LOAD_COL = {"ok": "green", "warn": "yellow", "crit": "red"}


def _load_severity(load, max_load) -> str:
    """ok / warn / crit for the statusbar load reading. warn = the precursor band
    (within 15 points of the gate); crit = at/over the gate. Pure."""
    if load is None:
        return "ok"
    if load >= max_load:
        return "crit"
    if load >= max_load - 15.0:
        return "warn"
    return "ok"


def _live_ram_segment(cnt, att, ms, fit, per_pane_mb, max_load) -> str:
    """The statusbar 'Live' segment: pane count + saikai's ESTIMATED RAM share
    (cnt x per-pane, so 'is saikai the cause of the slowdown?' is answerable at a
    glance), a severity-coloured system-load reading (the heaviness precursor),
    the ~fit headroom, and free RAM. Pure -> unit-testable."""
    if ms is None or ms.avail_phys_mb is None:
        return f"Live: {cnt}{att}"
    est = cnt * per_pane_mb / 1024.0
    fitcol = "green" if fit > 0 else "red"
    load_str = ""
    if ms.load is not None:
        col = _LOAD_COL[_load_severity(ms.load, max_load)]
        warn = "\N{WARNING SIGN} " if col != "green" else ""
        load_str = f"  [{col}]{warn}{ms.load:.0f}% RAM[/{col}]"
    return (f"Live: {cnt}~{est:.1f}G{att}{load_str}"
            f"  [{fitcol}]~{fit} fit[/{fitcol}]  ({ms.avail_phys_mb / 1024:.1f}G free)")


def _ctx_severity(pct) -> str:
    if pct is None:
        return "ok"        # unknown fill reads calm, never TypeErrors (#audit-hostile-usage)
    if pct >= 0.70:
        return "crit"
    if pct >= 0.55:
        return "warn"
    return "ok"


def _fmt_k(n) -> str:
    return f"{n/1_000_000:.1f}M" if n >= 1_000_000 else f"{round(n/1000)}K"


def _ctx_gauge_segment(tokens, window) -> str:
    """Statusbar 'ctx' segment for the focused pane: ground-truth fill, K-rounded,
    severity-coloured (green<55% / yellow / red>=70%). '' when tokens is None."""
    if tokens is None or not window:
        return ""
    pct = tokens / window
    col = _LOAD_COL[_ctx_severity(pct)]
    return f"[{col}]ctx {_fmt_k(tokens)}/{_fmt_k(window)} ({pct*100:.0f}%)[/{col}]"


def _copy_to_host_clipboard(text: str) -> bool:
    """Copy `text` to the HOST OS clipboard via the platform clip tool, so the
    tokened mirror URL pastes cleanly. Returns True only on a clean exit, so the
    QR screen can tell the truth about whether the copy worked (e.g. `xclip` may
    be absent on Linux). Bounded by a timeout: this runs on the Textual UI thread
    (F12 / startup), and `xclip` can otherwise daemonize and block the event loop
    holding the X selection — a timeout caps the worst case and reports False."""
    import subprocess as _sp
    if sys.platform == "win32":
        cmds = [["clip"]]
    elif sys.platform == "darwin":
        cmds = [["pbcopy"]]
    else:
        # Linux/BSD: try Wayland's wl-copy, then X11 xclip, then xsel — the first
        # tool actually installed wins (a missing one raises FileNotFoundError, so
        # fall through). Avoids assuming xclip on a Wayland-only or minimal box.
        cmds = ([["wl-copy"]] if os.environ.get("WAYLAND_DISPLAY") else []) + \
               [["xclip", "-selection", "clipboard"], ["xsel", "-b", "-i"]]
    for clip in cmds:
        try:
            return _sp.run(clip, input=text.encode("utf-8"), timeout=2.0).returncode == 0
        except FileNotFoundError:
            continue              # tool not installed -> try the next
        except Exception:
            return False          # ran but errored/timed out -> honest "not copied"
    return False


def _copy_host_or_osc52(text: str, app) -> bool:
    """Host-clipboard copy with an OSC-52 fallback via the Textual `app`, so a
    headless / SSH terminal with no wl-copy/xclip/xsel (a minimal Pi over SSH) can
    still copy — the same escape hatch the F9 copy-prompt path already uses. OSC-52
    has no acknowledgement, so a clean emit is reported as copied."""
    if _copy_to_host_clipboard(text):
        return True
    try:
        app.copy_to_clipboard(text)
        return True
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
        now = time.monotonic()
        parser = getattr(self, "_mirror_parser", None)
        # Stale-partial guard: the parser reassembles a key sequence split across
        # /input batches, but a real split arrives in ONE rapid burst. If >0.5s
        # elapsed since the last batch, a still-buffered partial is an ABANDONED
        # incomplete escape (a lone Esc, or an Alt+<char> the browser sent as
        # ESC+char), NOT a split about to continue — DROP it so it can't concatenate
        # onto THIS batch and fire a phantom key (a typed 'A' read as 'ctrl+up', the
        # cross-batch poison the audit found). The real driver's escape-timeout does
        # the equivalent flush. (Reassembly within a burst still works: <0.5s.) (#H9)
        if parser and (now - getattr(self, "_mirror_parser_ts", 0.0)) > 0.5:
            parser = None
            self._mirror_parser = None
        if parser is None:
            try:
                from textual._xterm_parser import XTermParser
                parser = XTermParser()
            except Exception:
                parser = False                 # parser API gone: remember + fall back
            self._mirror_parser = parser
        self._mirror_parser_ts = now
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
        # Reject out-of-range cells. The browser clamps to term cols/rows, but a
        # buggy/hostile client could POST anything; the upper bound mirrors the
        # existing <0 guard against the live screen size so a wild coord can't
        # reach hit-testing with a bogus value. (size is absent on the headless
        # mixin used in tests -> the <0 guard still applies, upper clamp skipped.)
        if col < 0 or row < 0:
            return
        _sz = getattr(self, "size", None)
        if _sz is not None and (col >= _sz.width or row >= _sz.height):
            return
        from textual import events
        if kind == "down":
            cls = events.MouseDown
        elif kind == "up":
            cls = events.MouseUp
        elif kind == "move":
            # A held-button drag: routes to the pane under the press (Textual
            # mouse capture after the first move), whose on_mouse_move forwards
            # motion to a child that asked for ?1002/?1003 — the child's OWN
            # selection runs from the browser. (#app-native-select)
            cls = events.MouseMove
        elif kind == "scrollup":
            cls = events.MouseScrollUp
        elif kind == "scrolldown":
            cls = events.MouseScrollDown
        else:
            return                             # unknown kind: never post garbage
        # Scroll has no pressed button (0); a click/drag carries the SGR button.
        btn = button if kind in ("down", "up", "move") else 0
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
        if key == "checkpoint":
            # The mirror's More row exposes checkpoint directly: in the TUI it is
            # a LEADER gesture (␣ c), so there is no single key to synthesize —
            # dispatch the action itself. All of b2's own gates (already-running,
            # no live target, mid-turn) still apply inside. (#mirror-checkpoint)
            try:
                self.action_checkpoint()
            except Exception:
                pass
            return
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
        try:                                   # refresh the always-on statusbar count
            self._update_subtitle()
        except Exception:
            pass
        if n > prev:
            try:
                self.notify(f"\N{GLOBE WITH MERIDIANS} mirror: a browser connected "
                            f"— {n} now viewing", title="saikai", timeout=6)
            except Exception:
                pass

    def _mirror_control_changed(self, on: bool) -> None:
        """HUB-initiated control-state change (idle auto-off). Runs on the UI
        thread (the hub's change handler marshals here). Sync the app's
        authoritative _control_enabled so the next Shift+F12 toggles from the
        REAL state instead of a stale True (which made the first press a dead
        'OFF an already-off control'), and tell the user the mirror went
        read-only."""
        if self._control_enabled == bool(on):
            return
        self._control_enabled = bool(on)
        try:
            if not on:
                self.notify("Mirror control auto-OFF (idle) — read-only",
                            title="saikai mirror", timeout=5)
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
      Enter        resume                Esc (saikai controls) leave / quit (×2)
      Ctrl-]       pane → list           Ctrl-C        pane interrupt / quit (×2)
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
                                     TabPane, Tabs, TextArea)
        from textual.widgets.option_list import Option
        from rich.text import Text
        from textual.content import Content  # markup-safe title/label type (TabPane
        #   rejects rich Text: render_str→_strip_control_codes calls str.translate)
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

    class SearchClear(Static):
        """A clickable X at the right of the search box that clears it. Hidden
        (display:none) unless the box has text — toggled in on_input_changed.
        can_focus stays False so it never steals keyboard focus from the list."""
        can_focus = False

        def on_click(self, event) -> None:
            event.stop()
            try:
                inp = self.app.query_one("#search")
                inp.value = ""   # fires Input.Changed -> on_input_changed -> hides me + unfilters
                inp.focus()
            except Exception:
                pass

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
                "  [yellow]Esc[/yellow]         Leave the current context: search/dropdown → list · list → quit (Esc twice)\n"
                "  [yellow]?[/yellow]           Help (this screen)\n\n"
                "[bold cyan]Session ops[/bold cyan]  [dim](␣x = Space then x; F-keys are the aliases)[/dim]\n"
                "  [yellow]␣f[/yellow] [dim]F6[/dim]     Toggle ★ favorite   ([dim]:fav[/dim] in search to filter)\n"
                "  [yellow]␣h[/yellow] [dim]F7[/dim]     Toggle hide/unhide  ([dim]:hidden[/dim] in search to find them)\n"
                "  [yellow]␣␣[/yellow]        Mark rows (▣) — then ␣f / ␣h / Enter act on ALL marked\n"
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
                "  [yellow]Esc[/yellow]        from the list: quit (press twice) + snapshot panes (␣p reopens)\n"
                "  [yellow]Ctrl-C[/yellow]     interrupt claude in a focused pane; from saikai controls, quit-all (press twice)\n"
                "  [yellow]␣z[/yellow] [dim]⇧F9[/dim]    Freeze the pane in place (copy mode): Shift+drag selects while\n"
                "             claude streams · scroll up also freezes · ␣z / typing resumes\n\n"
                "[bold cyan]Filter / Group / Sort (top-right dropdowns, Desktop-style)[/bold cyan]\n"
                "  Group by  Date / Project / State / None   (␣g cycles)\n"
                "  Sort by   Recency / Created time / Alphabetically\n"
                "  Status    Active / Archived / All\n"
                "  Age       last 1d / 3d / 7d / 30d / All time\n"
                "  Search    [yellow]/[/yellow] or type to open the bar; tokens AND with text + each other —\n"
                "            :fav  :hidden  :open  :active  :recent   (Esc clears)\n"
                "  Markers   [bold cyan]needs you[/bold cyan] (cyan): [bold cyan]?[/bold cyan] waiting · [bold cyan]![/bold cyan] reply due · [bold cyan]&[/bold cyan] bg blocked\n"
                "            running (normal): ~ busy · @ responding elsewhere\n"
                "            quiet (dim): = idle · @ open · $ shell · R remote · + active · . recent · & bg\n"
                "            tags: [#e0af68]*[/#e0af68] favorite · [red]x[/red] hidden · [red]&[/red] bg failed\n"
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
                    body += (f"[bold cyan]Menu key[/bold cyan]  [yellow]{_esc_markup(_lk)}[/yellow] "
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
                    body += (f"  [dim]\\[{sec}][/dim] {key:<22} = {_esc_markup(repr(val)):<14} "  # val is env/config user content
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
        Type a path (Tab completes child folders shell-style, ↓ picks from the
        list), or select a recent dir; Enter launches, Esc cancels. Returns the
        chosen path (or None) via dismiss()."""
        CSS = """
        NewSessionScreen { align: center middle; }
        #new-box { background: $panel; border: solid $accent; padding: 1 2;
                   width: 84; height: auto; max-height: 28; }
        #new-path { margin: 1 0; border: tall $accent; }
        #new-hint { height: 1; }
        #new-dirs { height: auto; max-height: 16; }
        """
        BINDINGS = [Binding("escape", "cancel", show=False),
                    # Tab = shell-style path completion; intercept before the
                    # default focus-next so typing a folder feels like a shell.
                    Binding("tab", "complete", show=False, priority=True),
                    Binding("down", "focus_list", show=False)]

        def __init__(self, base_dir, candidates):
            super().__init__()
            self._base_dir = base_dir
            self._candidates = candidates           # list[(label, path)] recent dirs
            self._scan_cache: dict = {}             # parent → child names (per-keystroke reuse)
            self._opts: list = []                   # backs #new-dirs: (kind, path, label)

        def compose(self) -> ComposeResult:
            with Vertical(id="new-box"):
                yield Static("[bold cyan]New claude session[/bold cyan]   "
                             "[dim]type a folder · Tab completes · ↓ picks · "
                             "Enter launches · Esc cancels[/dim]")
                yield Input(value=self._base_dir, placeholder="folder path",
                            id="new-path")
                yield Static("", id="new-hint")
                # Always present (even with no recent dirs) so completions can fill it.
                yield OptionList(id="new-dirs")

        def on_mount(self) -> None:
            self._populate(self._base_dir)
            try:
                inp = self.query_one("#new-path", Input)
                inp.focus()
                inp.cursor_position = len(inp.value)
            except Exception:
                pass

        def _populate(self, val: str) -> None:
            """Rebuild #new-dirs: recent dirs when the path is empty/unchanged,
            else live shell-style directory completions for what's typed."""
            v = (val or "").strip()
            if not v or v == (self._base_dir or "").strip():
                self._opts = [("recent", p, lbl) for lbl, p in self._candidates]
                hint = "recent dirs" if self._candidates else ""
            else:
                self._opts = [("complete", full, lbl)
                              for lbl, full in _complete_dir(v, self._scan_cache)]
                hint = (f"{len(self._opts)} match{'' if len(self._opts) == 1 else 'es'}"
                        if self._opts else "no matching folder")
            try:
                self.query_one("#new-hint", Static).update(f"[dim]{hint}[/dim]")
                ol = self.query_one("#new-dirs", OptionList)
                ol.clear_options()
                if self._opts:
                    # Text(): labels are DIRECTORY NAMES (user content) — a
                    # str prompt renders as markup, so "[red]x" loses text and a
                    # stray "[/x]" raises MarkupError at LAYOUT time and crashes
                    # the whole app. (#audit-self-option-markup)
                    ol.add_options([Option(Text(lbl)) for _k, _p, lbl in self._opts])
            except Exception:
                pass

        def on_input_changed(self, event) -> None:
            if getattr(event.input, "id", None) == "new-path":
                self._populate(event.value)

        def action_cancel(self) -> None:
            self.dismiss(None)

        def action_complete(self) -> None:
            """Tab: extend the path to the longest common prefix of the matches
            (or fully, when there's a single match). No-op when nothing extends."""
            try:
                inp = self.query_one("#new-path", Input)
            except Exception:
                return
            comps = _complete_dir(inp.value, self._scan_cache)
            if not comps:
                return
            new_val = (comps[0][1] + os.sep if len(comps) == 1
                       else os.path.commonprefix([full for _lbl, full in comps]))
            cur = os.path.expanduser((inp.value or "").strip())
            if new_val and new_val != cur and len(new_val) >= len(cur):
                inp.value = new_val
                inp.cursor_position = len(new_val)
                self._populate(new_val)

        def action_focus_list(self) -> None:
            try:
                ol = self.query_one("#new-dirs", OptionList)
                if ol.option_count:
                    ol.focus()
                    ol.highlighted = 0
            except Exception:
                pass

        def on_input_submitted(self, event) -> None:
            self.dismiss((event.value or "").strip() or None)

        def on_option_list_option_selected(self, event) -> None:
            try:
                kind, path, _lbl = self._opts[event.option_index]
            except Exception:
                self.dismiss(None)
                return
            if kind == "recent":
                self.dismiss(path)                  # recent dir → launch immediately
                return
            # completion → drill into that folder, keeping the modal open to go deeper
            try:
                inp = self.query_one("#new-path", Input)
                inp.value = path + os.sep
                inp.cursor_position = len(inp.value)
                inp.focus()
                self._populate(inp.value)
            except Exception:
                pass

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

    class ConfirmRefreshScreen(ModalScreen):
        """The HUMAN GATE in the b2 checkpoint flow (leader ␣c). Shows the
        NEW SESSION PROMPT extracted from the /handoff so the user can vet it
        before the destructive /clear runs. Enter proceeds (dismiss True), Esc
        cancels (dismiss False). The /clear is injected ONLY on a True dismiss —
        every other path (Esc, closing the screen) leaves the live session
        untouched."""
        CSS = """
        ConfirmRefreshScreen { align: center middle; }
        #confref-box { background: $panel; border: solid $warning; padding: 1 2;
                       width: 96; max-width: 96%; height: auto; max-height: 80%; }
        #confref-prompt { height: auto; min-height: 6; max-height: 24; margin: 1 0; }
        """
        # ctrl+s save is safe here: this modal is up only when NO live claude pane
        # is focused, so it can't steal the readline key — see the
        # test_no_app_binding_steals_a_readline_ctrl_key guard.
        BINDINGS = [Binding("ctrl+s", "proceed", show=False, priority=True),  # readline-exempt
                    Binding("escape", "cancel", show=False, priority=True)]

        def __init__(self, prompt: str):
            super().__init__()
            self._prompt = prompt or ""
            # The modal is pushed ASYNCHRONOUSLY (up to minutes after ␣c, whenever
            # the handoff settles) and steals focus mid-whatever — so a Ctrl+S the
            # user was about to type elsewhere must not fire the DESTRUCTIVE
            # proceed in its first instants. Esc (cancel) stays instant. (#audit-b2-modal-arm)
            self._armed_at = time.monotonic()

        def prompt_text(self) -> str:
            """The NEW SESSION PROMPT this modal is gating on — the live (possibly
            edited) text once mounted, else the originally extracted prompt."""
            try:
                return self.query_one("#confref-prompt", TextArea).text
            except Exception:
                return self._prompt

        def compose(self) -> ComposeResult:
            with Vertical(id="confref-box"):
                yield Static("[bold yellow]Checkpoint → refresh this session?[/bold yellow]   "
                             "[dim]edit if needed · Ctrl+S reseeds · Esc cancels[/dim]")
                yield Static("[dim]/handoff is done. Ctrl+S sends /clear (destructive) "
                             "and reseeds a fresh session with this prompt:[/dim]")
                yield TextArea(self._prompt, id="confref-prompt")

        def action_proceed(self) -> None:
            if time.monotonic() - self._armed_at < 0.4:
                return                         # swallow a mid-typing Ctrl+S (see __init__)
            self.dismiss(self.prompt_text())   # the (possibly edited) reseed prompt

        def action_cancel(self) -> None:
            self.dismiss(None)

    class OpenElsewhereScreen(ModalScreen):
        """Guard for resuming a session already open in ANOTHER Claude
        window/instance (the @ marker). A second `claude --resume` on the same
        transcript can interleave and corrupt it, so gate the spawn: Enter resumes
        anyway, Esc cancels."""
        CSS = """
        OpenElsewhereScreen { align: center middle; }
        #oe-box { background: $panel; border: solid $warning; padding: 1 2;
                  width: 78; max-width: 96%; height: auto; }
        """
        BINDINGS = [Binding("enter", "proceed", show=False),
                    Binding("escape", "cancel", show=False)]

        def __init__(self, title: str):
            super().__init__()
            self._title = title or "this session"

        def compose(self) -> ComposeResult:
            with Vertical(id="oe-box"):
                yield Static("[bold yellow]Already open elsewhere[/bold yellow]")
                # the title is USER content — unescaped, "[/x]" in it raised
                # MarkupError and crashed the whole UI when this modal opened.
                # _esc_markup = textual.markup.escape: a Static(markup=True) renders
                # Textual content markup, so use its escaper. (#audit-codex-oe-markup)
                yield Static(f"[dim]'{_esc_markup(self._title)}' is open in another Claude "
                             "window. Resuming starts a SECOND Claude on the same "
                             "conversation — they can clobber each other's writes.[/dim]")
                yield Static("[dim]Enter resumes anyway · Esc cancels[/dim]")

        def action_proceed(self) -> None:
            self.dismiss(True)

        def action_cancel(self) -> None:
            self.dismiss(False)

    class KillAgentScreen(ModalScreen):
        """Confirm terminating a live AGENT/bg process (kind=agent/bg, the &
        marker). This signals a process saikai did NOT spawn, so it's gated by an
        explicit confirm; the parent interactive claude is never affected (only
        the agent fork's own pid is signalled). Enter kills, Esc cancels.
        (#agent-kill)"""
        CSS = """
        KillAgentScreen { align: center middle; }
        #ka-box { background: $panel; border: solid $error; padding: 1 2;
                  width: 78; max-width: 96%; height: auto; }
        """
        BINDINGS = [Binding("enter", "proceed", show=False),
                    Binding("escape", "cancel", show=False)]

        def __init__(self, title: str, pid: int):
            super().__init__()
            self._title = title or "this agent"
            self._pid = pid

        def compose(self) -> ComposeResult:
            with Vertical(id="ka-box"):
                yield Static("[bold red]Kill agent process[/bold red]")
                yield Static(f"[dim]Terminate '{_esc_markup(self._title)}' "
                             f"(pid {self._pid})? This is a background agent owned "
                             "by its parent claude — the parent and your interactive "
                             "session keep running; only this agent stops. Unsaved "
                             "agent work is lost.[/dim]")
                yield Static("[dim]Enter kills · Esc cancels[/dim]")

        def action_proceed(self) -> None:
            self.dismiss(True)

        def action_cancel(self) -> None:
            self.dismiss(False)

    class NotificationsScreen(ModalScreen):
        """Recent-notifications recall (F11). Toasts auto-dismiss; this lists the
        ones that already vanished — newest first, with time + severity colour —
        so a missed 'needs input' / 'done' / error / memory-pressure warning can
        be reviewed after the fact. Esc / F11 close. Drawn in the TUI, so it is
        mirrored to the browser too."""
        CSS = """
        NotificationsScreen { align: center middle; }
        #notif-box { background: $panel; border: solid $accent; padding: 1 2;
                     width: 92; max-width: 96%; height: 80%; max-height: 40; }
        #notif-log { height: 1fr; }
        """
        BINDINGS = [Binding("escape", "dismiss", show=False),
                    Binding("f11", "dismiss", show=False)]

        def __init__(self, entries):
            super().__init__()
            self._entries = list(entries)

        def compose(self) -> ComposeResult:
            with Vertical(id="notif-box"):
                yield Static(f"[bold cyan]Recent notifications[/bold cyan]   "
                             f"[dim]{len(self._entries)} kept · newest first · "
                             f"Esc closes[/dim]")
                yield RichLog(id="notif-log", wrap=True, highlight=False, markup=True)

        def on_mount(self) -> None:
            log = self.query_one("#notif-log", RichLog)
            if not self._entries:
                log.write("[dim](no notifications yet)[/dim]")
                return
            from rich.markup import escape as _rich_escape
            from rich.text import Text
            _col = {"information": "cyan", "warning": "yellow", "error": "red"}
            for ts, sev, title, msg in reversed(self._entries):   # newest first
                c = _col.get(sev, "white")
                head = f"[dim]{ts}[/dim] [{c}]{sev[:4].upper()}[/{c}]"
                if title:
                    head += f" [b]{_rich_escape(title)}[/b]"
                log.write(head)
                # The message is USER content (titles, exception text, paths) —
                # write it as a Text object so this markup=True log renders it
                # VERBATIM: "[WIP] fix" must not lose its tag, and a stray
                # "[/x]" must not raise MarkupError. (#audit-toast-markup)
                log.write(Text(f"  {msg}"))

    class _CopyModeArea(TextArea):
        """A read-only TextArea with a tmux-copy-mode-vi key table, used to select
        & yank transcript text with the keyboard. read_only TextArea passes
        printable keys straight through (its _on_key early-returns under read_only
        WITHOUT consuming), so these BINDINGS fire. h/j/k/l/w/b/0/$ + g/G +
        ctrl+d/u navigate; v starts a selection (subsequent motions extend it);
        y/Enter yanks to the clipboard; Esc/q closes. (#copy-mode)"""
        BINDINGS = [
            Binding("h", "cursor_left", show=False),
            Binding("l", "cursor_right", show=False),
            Binding("k", "cursor_up", show=False),
            Binding("j", "cursor_down", show=False),
            Binding("w", "cursor_word_right", show=False),
            Binding("b", "cursor_word_left", show=False),
            Binding("0", "cursor_line_start", show=False),
            Binding("dollar_sign", "cursor_line_end", show=False),
            Binding("ctrl+d", "half_down", show=False),   # readline-exempt: copy-mode modal only (no live pane focused)
            Binding("ctrl+u", "half_up", show=False),      # readline-exempt: copy-mode modal only (no live pane focused)
            Binding("g", "goto_top", show=False),
            Binding("G", "goto_bottom", show=False),
            Binding("v", "toggle_select", "Select"),
            Binding("y", "yank", "Yank"),
            Binding("enter", "yank", show=False),
            Binding("ctrl+c", "yank", show=False),   # modern copy; coexists with y/Enter
            Binding("Y", "yank_all", "Yank all"),
            Binding("escape", "quit_copy", "Close"),
            Binding("q", "quit_copy", show=False),
        ]
        selecting = False

        # Motions honour the selecting flag (vi visual mode); arrows route here too.
        def action_cursor_left(self, select: bool = False) -> None:
            super().action_cursor_left(self.selecting)
        def action_cursor_right(self, select: bool = False) -> None:
            super().action_cursor_right(self.selecting)
        def action_cursor_up(self, select: bool = False) -> None:
            super().action_cursor_up(self.selecting)
        def action_cursor_down(self, select: bool = False) -> None:
            super().action_cursor_down(self.selecting)
        def action_cursor_word_left(self, select: bool = False) -> None:
            super().action_cursor_word_left(self.selecting)
        def action_cursor_word_right(self, select: bool = False) -> None:
            super().action_cursor_word_right(self.selecting)
        def action_cursor_line_start(self, select: bool = False) -> None:
            super().action_cursor_line_start(self.selecting)
        def action_cursor_line_end(self, select: bool = False) -> None:
            super().action_cursor_line_end(self.selecting)
        def action_half_down(self) -> None:
            self.move_cursor_relative(rows=12, select=self.selecting)
        def action_half_up(self) -> None:
            self.move_cursor_relative(rows=-12, select=self.selecting)
        def action_goto_top(self) -> None:
            self.move_cursor((0, 0), select=self.selecting)
        def action_goto_bottom(self) -> None:
            self.move_cursor(self.document.end, select=self.selecting)

        def action_toggle_select(self) -> None:
            self.selecting = not self.selecting
            if self.selecting:                       # anchor a fresh selection here
                self.move_cursor(self.cursor_location, select=False)

        def action_yank(self) -> None:
            # Yank the visual selection (v + motion); with no selection, yank the
            # CURRENT line so a bare `y` always copies something (vim 'yy' feel).
            text = self.selected_text or ""
            if not text.strip():
                try:
                    text = self.document[self.cursor_location[0]]
                except Exception:
                    text = ""
            self._yank(text, "line")

        def action_yank_all(self) -> None:
            self._yank(self.text, "all")        # Y = copy the whole transcript

        def _yank(self, text: str, what: str) -> None:
            if not (text or "").strip():
                self.app.notify("nothing to copy here", timeout=3)
                return
            if _copy_host_or_osc52(text, self.app):
                self.app.notify(f"copied {what} ({len(text)} chars) to clipboard",
                                timeout=4)
                self.action_quit_copy()
            else:
                self.app.notify("could not copy to clipboard",
                                severity="warning", timeout=4)

        def action_quit_copy(self) -> None:
            try:
                self.app.pop_screen()
            except Exception:
                pass

    class CopyModeScreen(ModalScreen):
        """vi-style copy mode over the session transcript. saikai owns the full
        text (read from the JSONL), so arbitrary keyboard selection works where the
        live alt-screen pane can't be selected past its visible edge. j/k/↑↓ move,
        v selects, y yanks, g/G top/bottom, Esc/q closes. Opens at the most recent
        turn. (#copy-mode)"""
        CSS = """
        CopyModeScreen { align: center middle; }
        #cm-box { background: $panel; border: solid $accent; padding: 0 1;
                  width: 104; max-width: 96%; height: 84%; max-height: 44; }
        #cm-hint { height: 1; }
        #cm-area { height: 1fr; border: none; }
        """
        BINDINGS = [Binding("escape", "dismiss", show=False)]

        def __init__(self, text):
            super().__init__()
            self._text = text

        def compose(self) -> ComposeResult:
            with Vertical(id="cm-box"):
                yield Static("[bold cyan]Copy mode[/bold cyan]   [dim]drag to select · "
                             "Ctrl+C / y copy · [b]Y copy all[/b] · "
                             "j/k·v vi-select · g/G top·bottom · Esc close[/dim]",
                             id="cm-hint")
                yield _CopyModeArea(self._text, read_only=True, soft_wrap=True,
                                    language=None, id="cm-area")

        def on_mount(self) -> None:
            try:
                area = self.query_one("#cm-area", _CopyModeArea)
                area.focus()
                area.move_cursor(area.document.end)   # start at the most recent turn
            except Exception:
                pass

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
            # Ctrl+C is guarded in on_key, NOT via a Binding: a saikai ctrl+c
            # binding would fire the UNGUARDED action_quit_all on the first press.
            # Textual's built-in ctrl+c is `system` (non-priority), so on_key +
            # event.stop() shadow it — the on_key double-press guard wins. Ctrl+Q
            # needs no entry here: Textual's built-in ctrl+q binding IS priority,
            # but its "quit" action resolves to our OVERRIDDEN action_quit (the
            # guarded Esc path), so Ctrl+Q is double-press-guarded too (locked by
            # test_ctrlq_is_double_press_guarded).
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
            # F1: vi-style copy mode over the transcript — select & yank any of
            # claude's output with the keyboard, including replies that scrolled
            # off the alt-screen pane (can't be selected there, but the JSONL has
            # the full text). priority so it fires inside a focused claude pane
            # (Space-leader is eaten by claude there). (#copy-mode)
            Binding("f1", "copy_response", "Copy mode", id="copy_response", show=False, priority=True),
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
            Binding("f11", "notifications", "Notifs", id="notifs", show=False, priority=True),
            Binding("shift+f10", "close_all_live", "Close all", id="close_all", show=False, priority=True),
            # Shift+K: terminate the focused row's AGENT/bg process (kind=agent/bg —
            # the & marker). NOT a Ctrl+letter (those are readline keys); confirmed,
            # identity-verified; never touches an interactive claude. (#agent-kill)
            Binding("K", "kill_agent", "Kill agent", id="kill_agent", show=False, priority=True),
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
            # Ctrl+F12: dump the focused/active pane's visible pyte screen to
            # ~/.cache/saikai/pane-dump.txt so a garbled render can be inspected
            # off the live UI. priority so it fires inside a focused pane. (#pane-dump)
            Binding("ctrl+f12", "dump_pane", "Dump pane", id="dump_pane", show=False, priority=True),
            # Phase B: toggle web-mirror INTERACTIVE control. priority=True so it
            # fires even while a live pane is focused (a leader letter would be
            # swallowed by the focused pane — unreachable exactly when control is
            # used). Default OFF; Shift+F12 because F12 is the QR. Local only —
            # never a browser button.
            Binding("shift+f12", "toggle_mirror_control", "Mirror control",
                    id="mirror_control", show=False, priority=True),
            # Phase C: jump to the session this one was forked/cleared from.
            # priority=True so it fires even while a live pane is focused.
            Binding("shift+f6", "open_parent", "Parent", id="open_parent",
                    show=False, priority=True),
            # Phase B1: inject /compact into the focused live pane (non-destructive).
            # priority=True so it fires even while a live pane is focused.
            Binding("shift+f11", "context_refresh", "Refresh ctx", id="ctx_refresh",
                    show=False, priority=True),
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
        /* 1fr so it still shrinks on narrow terminals, but capped — without the
           cap it swallows the whole bar and dwarfs the filter dropdowns. */
        #search { width: 1fr; max-width: 42; border: tall $panel; }
        #search:focus { border: tall $accent; }   /* focus visible: dim panel -> accent */
        /* Clear (X) button: shown only while the search box has text (toggled in
           on_input_changed); can_focus False so it never steals list focus. */
        #search-clear { display: none; width: 3; height: 3; content-align: center middle; color: $text-muted; }
        #search-clear:hover { color: $text; background: $panel; }
        /* widths sized for the LONGEST option + Select border/chevron overhead
           (~6 cols): "Alphabetically"=14, "Archived"/"All time"=8, "Project"=7 */
        #groupsel { width: 16; }
        #sortsel { width: 22; }
        #statussel { width: 16; }
        #lastsel { width: 16; }
        #statusbar { height: 1; background: $surface; color: $warning; }
        /* which-key panel (Space-leader): a dedicated bottom-docked, ALIGNED
           Static — not a toast — so the family columns line up and it never sits
           over the live pane's bottom-right. Hidden until armed + hesitating. */
        /* margin-bottom lifts the panel clear of the 1-row Footer: two
           dock:bottom siblings don't stack — both pin to the screen edge and the
           hint would otherwise paint over the "tab Preview" footer row. */
        #leaderhint { dock: bottom; height: auto; max-height: 12; margin-bottom: 1;
                      background: $panel; color: $text; border-top: solid $accent;
                      padding: 0 1; display: none; }
        /* Transient toasts move to the TOP-RIGHT — Textual defaults to bottom-right,
           which covers the live pane's input line. KEEP Textual's native
           `_toastrack` layer (auto-appended as the top-most layer in screen.py):
           overriding it with a custom layer lost the top-most + hit-test handling,
           so toasts clipped on hover, fell behind other widgets, and didn't dismiss
           on click. Override the dock/align only — never the layer. Click-to-dismiss
           is then Textual's built-in Toast `@on(Click)` behaviour. */
        ToastRack { dock: top; align: right top; }
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
        #preview-scroll { height: 1fr; width: 1fr; }   /* scroll viewport (fills the pane) */
        #preview { padding: 0 1; height: auto; width: 1fr; }   /* Static grows with content → #preview-scroll scrolls */
        /* No border on the live pane: a box around it makes the embedded claude
           look unlike a real terminal session, and the pane already signals focus
           by SHOWING its cursor only when focused (see AgentTerminal.render_line).
           Focus visibility lives on the search box (#search:focus) + the statusbar
           release-key hint instead. */
        AgentTerminal { width: 1fr; height: 1fr; }
        """

        preview_mode = "summary"   # "summary" or "full"
        _sid_index: dict = {}      # sid -> session; populated in on_mount
        _na_cache: dict = {}       # sid -> (mtime, needs_attention); Group-by-State
        # _last_status is NOT declared here: it is a per-instance dict set in
        # __init__ (the shared class-attr default would leak one PickerApp's
        # statuses into a second instance — see the #8 fix at its init site).
        _b2: "dict | None" = None  # b2 (Task 11) checkpoint state machine; None = idle
        _b1: "dict | None" = None  # b1 /compact inject verifier; None = idle (#audit-b1-verify)
        _b2_timer = None           # the self-cancelling tick interval handle

        def compose(self) -> ComposeResult:
            with Horizontal(id="searchrow"):
                yield Input(placeholder="Search title / msg / SID / proj    "
                                        "•  :fav  :hidden  :open  :active  :recent",
                            id="search")
                yield SearchClear("✕", id="search-clear")
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
                            # Static (not RichLog) so the preview is TEXT-SELECTABLE
                            # while keeping Rich/ANSI formatting — RichLog's selection
                            # yields no text in Textual 8.2.7 (verified). VerticalScroll
                            # supplies the scrolling RichLog used to. (#preview-select)
                            with VerticalScroll(id="preview-scroll"):
                                yield Static(id="preview", markup=False)
                else:
                    # Legacy / graceful-fallback layout: bare preview pane.
                    # The Static keeps id="preview" (so _update_preview's query is
                    # identical in both layouts); the scroll container carries the
                    # .right class so it sizes as the 1fr pane beside the grip.
                    with VerticalScroll(id="preview-scroll", classes="right"):
                        yield Static(id="preview", markup=False)
            yield Footer()
            yield Static("", id="leaderhint")   # which-key panel (dock: bottom)

        # Actions that operate on the list / live panes. They are PRIORITY
        # bindings (so they fire even when a live pane owns focus), but Textual
        # checks priority bindings against the FULL binding chain — it IGNORES
        # the modal boundary that _modal_binding_chain enforces for normal
        # bindings (app.py: _check_bindings uses _binding_chain when priority).
        # Without this gate the App's Enter would resume a session from under the
        # rename box, "?" would open Help instead of typing into an input, F10
        # would close a pane mid-dialog, etc.
        _MODAL_BLOCKED_ACTIONS = frozenset({
            "resume", "refresh", "toggle_fav", "toggle_hide", "preview_changes",
            "copy_prompt", "toggle_tree", "cycle_group", "new_session",
            "freeze_pane", "restore_panes", "toggle_preview", "help",
            "close_live", "close_all_live", "prev_tab", "next_tab",
            "next_attention", "toggle_list", "rename", "shrink_list",
            "grow_list", "notifications", "open_parent", "context_refresh",
            "checkpoint", "copy_response",
        })

        def check_action(self, action: str, parameters):
            # Gate the list/pane priority bindings while a modal / pushed screen
            # is open (screen_stack > 1 means something sits over the main list).
            # Returning False makes run_action skip the action, so the key falls
            # through to the focused widget — e.g. the rename Input gets Enter ->
            # Input.Submitted -> save, instead of the App resuming a session.
            if action in self._MODAL_BLOCKED_ACTIONS and len(self.screen_stack) > 1:
                return False
            return super().check_action(action, parameters)

        def push_screen(self, *args, **kwargs):
            # A pushed screen (rename / new / help / settings / mirror QR) takes
            # over the keyboard. Clear any half-armed leader or quit-guard state:
            # a PRIORITY binding can open a screen, and priority bindings bypass
            # on_key (where these are normally resolved/disarmed), so without this
            # an arm could dangle into the keystrokes after the screen closes (a
            # later single Esc quitting, or a letter running a leader action).
            self._leader_pending = False
            self._set_leader_hint(None)
            if getattr(self, "_quit_armed", False):
                self._disarm_quit()
            return super().push_screen(*args, **kwargs)

        def notify(self, message, **kwargs):
            # Keep a bounded recall log of every toast — they auto-dismiss, so a
            # missed "needs input" / "done" / error / memory-pressure warning is
            # otherwise gone. F11 (action_notifications) surfaces it. Lazy-init so
            # toasts raised during mount/startup are captured too.
            #
            # markup=False: toast messages carry USER content — session titles
            # ("needs input: {title}"), exception reprs, paths — and Textual
            # renders notifications as content markup by default. "[WIP] fix x"
            # displays as " fix x" (tag swallowed) and a stray "[/x]" raises
            # MarkupError inside Toast.render, so the toast never shows at all.
            # No saikai toast uses intentional markup — turn it off wholesale.
            # (#audit-toast-markup)
            kwargs.setdefault("markup", False)
            buf = getattr(self, "_notif_log", None)
            if buf is None:
                from collections import deque
                buf = self._notif_log = deque(maxlen=200)
            try:
                import time as _t
                buf.append((_t.strftime("%H:%M:%S"),
                            str(kwargs.get("severity", "information")),
                            str(kwargs.get("title", "") or ""),
                            str(message)))
            except Exception:
                pass
            return super().notify(message, **kwargs)

        def action_notifications(self) -> None:
            """F11 — recall recent notifications (the toasts that already
            auto-dismissed). Opens a scrollable, mirror-visible panel."""
            self.push_screen(NotificationsScreen(getattr(self, "_notif_log", [])))

        def _heal_toasts(self) -> None:
            """Self-heal for the WT hover artifact: rows of a HOVERED toast
            intermittently vanish on Windows Terminal. Headless probes prove the
            compositor AND the partial-update chops both emit the toast rows
            correctly, so the loss happens in the Windows-driver-ANSI ↔ WT
            rendering layer (out of our reach). While any toast is visible,
            re-emit it on a short tick — a punched row repaints within ~0.4s,
            and the tick is a cheap no-op when no toast is up. (#toast-heal)"""
            try:
                from textual.widgets._toast import Toast
                for t in self.screen.query(Toast):
                    t.refresh()
            except Exception:
                pass

        def on_mount(self) -> None:
            # WT toast-row self-heal (see _heal_toasts).
            try:
                self.set_interval(0.4, self._heal_toasts)
            except Exception:
                pass
            # Gated OUTPUT capture: tee everything saikai writes to the REAL
            # terminal to a file, so the cursor/IME escape stream around focus can
            # be inspected byte-for-byte (does our ?25h survive, or does a later
            # write re-hide it?). Off unless SAIKAI_OUT_CAPTURE=<path>. (#wt-ime)
            _ocap = os.environ.get("SAIKAI_OUT_CAPTURE")
            if _ocap:
                try:
                    _drv = getattr(self, "_driver", None)
                    if _drv is not None and not getattr(_drv, "_saikai_teed", False):
                        _orig_w = _drv.write
                        _cf = open(_ocap, "a", encoding="utf-8", errors="replace")

                        def _tee(data, _o=_orig_w, _f=_cf):
                            try:
                                _f.write(repr(data) + "\n")
                                _f.flush()
                            except Exception:
                                pass
                            return _o(data)
                        _drv.write = _tee
                        _drv._saikai_teed = True
                except Exception:
                    pass
            # sid -> session map so the preview pane can warm its own cache on
            # demand: rendered and cached on a cache miss.
            self._sid_index = {s.get("id"): s for s in all_sessions}
            self._marked: set = set()        # sids selected (Space-Space); bulk-capable verbs (resume/favorite/hide) act on all marked — see _selected_or_cursor
            self._opening_live_sid = None     # sid whose pane should grab focus on open
            self._unread: set = set()         # live panes finished (idle) but not yet responded to → ! marker
            self._busy_seen: set = set()      # sids observed busy since their last "done" toast (catches tasks shorter than the poll)
            self._last_status: dict = {}      # sid -> last poll status snapshot — per INSTANCE: the class-attr default is a shared mutable dict, so a 2nd PickerApp (headless tests, a future multi-window) would otherwise read the first app's statuses on its first poll (#8)
            self._opened_sids: set = set()    # sids opened + kept this session (snapshot source)
            self._quitting = False            # set in action_quit_all so kill-triggered _on_live_exit doesn't erode the saved restore snapshot (#restore-erosion)
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
            self._leader_gen = 0           # bumps each arm; a stale timer no-ops on mismatch
            self._suppress_arm = False     # eat the spurious arm_leader after a leader-key dispatch (#H10)
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
            # Default-on auto-pickup of sessions started elsewhere: a cheap mtime gate
            # (stat the registry + projects dirs) kicks the OFF-thread rescan ONLY when
            # something changed — no constant disk walk, no needless table rebuild.
            # SAIKAI_AUTO_REFRESH above adds a fixed-interval scan on top. (#recon-autorefresh)
            try:
                self._sessions_mtime = self._sessions_dirs_mtime()
            except Exception:
                self._sessions_mtime = 0.0
            self.set_interval(2.0, self._rescan_if_changed)
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

                # Pane direct view (#pane-direct): raw terminal bytes from the
                # browser xterm → the followed pane's child PTY, and the hub's
                # reseed request → a fresh full-state seed. Same _marshal shape.
                def _raw_handler(d, _app=_app_ref):
                    if not getattr(_app, "is_running", False):
                        return
                    try:
                        _app.call_from_thread(_app._mirror_inject_raw, d)
                    except Exception:
                        pass
                _hub.set_raw_handler(_raw_handler)

                def _pane_reseed(_app=_app_ref):
                    if not getattr(_app, "is_running", False):
                        return
                    try:
                        _app.call_from_thread(_app._mirror_pane_reseed)
                    except Exception:
                        pass
                _hub.set_pane_reseed_request(_pane_reseed)
                # Child-query strip for the pane stream — the union regex lives
                # next to the query-answering code in saikai_terminal so the two
                # can't drift; the hub applies it on its drain thread. (#pane-direct)
                if _LIVE_TERM is not None:
                    try:
                        _hub.set_pane_strip(_LIVE_TERM._MIRROR_QUERY_STRIP_RE)
                    except Exception:
                        pass
                    # Relay child OSC 52 copies (claude's copy-selection) to the
                    # browsers: the copy must land on the device driving the
                    # selection, not only on the host. (#app-native-select)
                    try:
                        _LIVE_TERM.MIRROR_CLIP = _hub.send_clip
                    except Exception:
                        pass

                def _client_change(n, _app=_app_ref):
                    if not getattr(_app, "is_running", False):
                        return
                    try:
                        _app.call_from_thread(_app._mirror_clients_changed, n)
                    except Exception:
                        pass
                _hub.set_client_change_handler(_client_change)

                def _control_change(on, _app=_app_ref):
                    if not getattr(_app, "is_running", False):
                        return
                    try:
                        _app.call_from_thread(_app._mirror_control_changed, bool(on))
                    except Exception:
                        pass
                _hub.set_control_change_handler(_control_change)
                # Show the QR so a phone can join without typing the tokened URL
                # (the stderr banner is alt-screen hidden). action_mirror_info also
                # copies the URL to the host clipboard, every time — F12 re-opens it.
                # on_close re-surfaces the "Shift+F4 to reopen" hint the QR covers.
                self.call_after_refresh(
                    lambda: self.action_mirror_info(on_close=self._after_launch_qr))

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
            saved_scroll_y = table.scroll_offset.y   # preserve the user's scroll across the rebuild
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
            _b2_sid = (getattr(self, "_b2", None) or {}).get("sid")  # ↻ checkpoint row (hoisted; constant per rebuild)
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
            # Show the sort PRIORITY number only when more than one column sorts —
            # a lone sort key's "1" (e.g. "Last 1v") is noise; a bare arrow is clear.
            _n_active_sort = sum(1 for k in sort_keys if k.get("col", "-") != "-")
            def col_label(col_key: str, base: str) -> str:
                for i, k in enumerate(sort_keys, 1):
                    if k["col"] == col_key:
                        arrow = "v" if k["dir"] == "desc" else "^"
                        rank = f"{i}" if _n_active_sort > 1 else ""
                        return f"{base} {rank}{arrow}"
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
                    elif _s.get("is_bg"):
                        # claude's agents feature / a bg job: LIVE but owned by
                        # another claude — not attachable from here. Its own
                        # section keeps "Open" meaning "windows you can act on".
                        # (#agents-kind)
                        _s["_state"] = "Agents"
                    elif _s.get("is_open") or _live == "idle":
                        # Running now (live pane / open elsewhere): state is known
                        # and its JSONL is GROWING — skip the needs-attention
                        # tail-read, which would defeat the mtime cache every
                        # refresh (resource #6).
                        _s["_state"] = "Open"
                    else:
                        # Not live → "Idle". The State lens groups by ACTIONABILITY
                        # (Needs input / Running / Open); recency is shown by the
                        # row's +/. marker and is what Date grouping is for, so a
                        # dormant session isn't sub-split by a 30-min window. ("Needs
                        # input" is also reserved for a LIVE pane truly waiting on you
                        # — never a dormant transcript whose last turn was yours.)
                        _s["_state"] = "Idle"
            # Claude-Desktop-style sections: partition the (already sorted) rows
            # into Pinned + date/project/state groups, then remember which row
            # each section header should precede. grouping='none' -> no headers.
            groups = (_build_groups(visible, grouping, set(favorites), datetime.now())
                      if grouping != "none" else [(None, visible)])
            header_before: dict[str, str] = {}
            flat: list[dict] = []
            for _hdr, _members in groups:
                # Pinned (favorites) stay visible regardless of the hidden filter —
                # same "pinned stays regardless" guarantee the Age cut honours. Else
                # a fav+hidden row was hoisted into Pinned, then dropped by the hidden
                # filter, landing in NO group and vanishing entirely. (#audit-fav-hidden)
                _is_pinned = (_hdr == "Pinned")
                _vis = [m for m in _members
                        if (_is_pinned or not (m["id"] in hidden and not show_hidden))
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
            first_attention_row = None # row index of the first session that NEEDS YOU (front-door home)
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
                if live_status in _LIVE_MARKER:      # waiting → ?, busy → ~ (shared glyphs)
                    marker_a = _LIVE_MARKER[live_status][0]
                elif live_status == "idle":
                    # ! = claude finished and the user has not responded yet;
                    # = = idle live pane with no response due. Merely viewing a tab
                    # does not clear !. ASCII keeps the marker column aligned.
                    # _reply_due is the SAME predicate the statusbar !M uses.
                    marker_a = "!" if self._reply_due(s["id"]) else "="
                else:
                    # ! here = a DORMANT session whose last turn was yours and is
                    # still unanswered — the same "reply due" idea as the live ! above,
                    # but for a not-running session (resume it to get the reply). Ranks
                    # below + (just-touched) and above . (merely recent).
                    marker_a = ("&" if s.get("is_bg")
                                else "s" if s.get("remote_origin")
                                else "R" if s.get("is_remote_control")
                                else "$" if (s.get("is_open") and s.get("session_status") == "shell")
                                else "@" if s.get("is_open")
                                else "+" if s.get("is_active")
                                else "!" if _needs_attention(s, self._na_cache)
                                else "." if s.get("is_recent") else " ")
                marker_s = ("*" if s["id"] in favorites
                            else "x" if is_hidden else " ")
                marker = f"{marker_a}{marker_s}"
                # "Needs you" for the front-door home: waiting (?) / reply-due (!) /
                # a background agent blocked on your clarification — the same states
                # the ATTENTION accent tints.
                is_attention = (marker_a in ("?", "!")
                                or (marker_a == "&" and s.get("job_needs")))
                # Tint the marker by its activity state (marker_a); the fav/hidden
                # suffix rides the same colour. Glyph stays the alignment anchor.
                # _marker_tint applies the single ATTENTION accent + bg job-state.
                marker_cell = Text(marker, style=_marker_tint(marker_a, s))
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
                    raw_title = "▣ " + raw_title       # selection (Space-Space)
                if _b2_sid and s["id"] == _b2_sid:
                    raw_title = "↻ " + raw_title       # b2 checkpoint in progress on this session
                if s.get("is_bg") and s.get("parent_session_id"):
                    # agent lineage: name WHO spawned it, so one parent's brood
                    # reads as a block in the Agents section (#agent-lineage)
                    _par = getattr(self, "_sid_index", {}).get(s["parent_session_id"])
                    _pt = _list_title(_par)[:24] if _par else s["parent_session_id"][:8]
                    raw_title = "↳ " + raw_title + " ⟨" + _pt + "⟩"
                _tstyle = _title_color.get(_color_key_for(s, _color_by), "")  # [display] color_by
                if narrow:
                    # marker · relative-Last · title (title tinted per color_by).
                    row = [marker_cell, fmt_last_active(s), Text(raw_title, style=_tstyle)]
                    table.add_row(*row, key=s["id"])
                    if first_session_row is None:
                        first_session_row = n
                    if is_attention and first_attention_row is None:
                        first_attention_row = n
                    n += 1
                    n_sessions += 1
                    continue
                row = [marker_cell, fmt_ts(s["first_ts"]), fmt_last_active(s)]
                if show_proj_col:
                    proj_txt = project_short(s.get("project_name") or "")
                    row.append(Text(proj_txt, style=project_color.get(proj_txt, "")))
                if has_worktrees:
                    wt = s.get("worktree_label") or ""
                    row.append(Text(wt[:11], style=wt_color.get(wt, "") if wt else ""))
                # ALWAYS a Text object: a bare str cell goes through DataTable's
                # default_cell_formatter, which treats str as POSSIBLE MARKUP —
                # a title containing "[wip]"/"[b]" would silently lose text.
                # (#audit-toast-markup)
                row.append(Text(raw_title, style=_tstyle))
                table.add_row(*row, key=s["id"])
                if first_session_row is None:
                    first_session_row = n
                if is_attention and first_attention_row is None:
                    first_attention_row = n
                n += 1
                n_sessions += 1
            self._n_sessions = n_sessions
            # Restore the cursor onto the SAME session (its row index shifts when
            # grouping/filtering/headers change); fall back to the old clamp.
            restored = False
            # Front door (one-shot): the FIRST paint homes the cursor on the first
            # session that needs you — waiting / reply-due / bg-blocked — so the list
            # opens on "who needs me", not the newest row. Fires once; later refreshes
            # restore by session as usual so navigation is never yanked away. Skipped
            # when a filter/search is active (the user is already driving the cursor).
            if (not getattr(self, "_did_attention_home", False)
                    and not self._filter_is_engaged()):
                self._did_attention_home = True
                if first_attention_row is not None:
                    try:
                        table.move_cursor(row=first_attention_row, scroll=True)
                        restored = True
                    except Exception:
                        restored = False
            if not restored and saved_sid:
                try:
                    table.move_cursor(row=table.get_row_index(saved_sid), scroll=False)
                    restored = True
                except Exception:
                    restored = False
            if not restored and n and 0 <= saved_cursor < n:
                try:
                    table.move_cursor(row=saved_cursor, scroll=False)
                except Exception:
                    pass
            if n_sessions and first_session_row is not None and self._cursor_sid() is None:
                # The restore (or the default row 0) landed on a section-header
                # row, which has no session → preview/Enter would act on nothing.
                # Nudge down to the first real session.
                try:
                    table.move_cursor(row=first_session_row, scroll=False)
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
                    self._set_preview(Text(msg, style="dim italic"))
                except Exception:
                    pass
            # Restore the user's scroll position so a BACKGROUND rebuild (the live
            # poll, a filter keystroke, a fav/hide toggle) never yanks the viewport
            # back to the cursor row (the cursor restores above use scroll=False).
            # Set synchronously to avoid a flicker, then again after layout in case
            # the new rows' virtual size wasn't known yet (the sync set would clamp).
            try:
                table.scroll_to(y=saved_scroll_y, animate=False)
                _sy = saved_scroll_y
                self.call_after_refresh(lambda: table.scroll_to(y=_sy, animate=False))
            except Exception:
                pass
            self._update_subtitle()

        def _update_subtitle(self) -> None:
            table = self.query_one("#table", DataTable)
            # Section-header rows inflate row_count; use the tracked session count.
            n = getattr(self, "_n_sessions", table.row_count)

            # Sort + Group ALSO live in the searchrow dropdowns, so only echo them
            # in the statusbar when that row is HIDDEN (mirrors the search query
            # below) — a visible dropdown row makes them redundant here.
            sep = "  [dim]·[/dim]  "
            _bar_hidden = not self._search_visible()
            _COL_LABEL = {
                "date": "Start", "last": "Last", "title": "Title",
                "proj": "Proj", "topic": "Topic", "turns": "Turns", "fav": "Fav",
            }
            sort_keys = _load_sort()
            first = next((k for k in sort_keys if k["col"] != "-"), None)
            if not _bar_hidden:
                sort_str = ""
            elif first:
                arrow = "↓" if first["dir"] == "desc" else "↑"
                col_display = _COL_LABEL.get(first["col"], first["col"].capitalize())
                sort_str = f"{sep}Sort: {col_display}{arrow}"
            else:
                sort_str = f"{sep}Sort: default"

            # Scope: "All projects" when --all-projects, else repo name
            scope = "All projects" if show_project else (repo.name if repo else "All projects")
            scope_str = f"{sep}{_esc_markup(scope)}"   # repo.name is user content: a '[' folder crashes markup

            # Show Tree only when ON (a row of OFFs is noise).
            tree_str = f"{sep}Tree: [green]ON[/green]" if _get_tree_mode() else ""
            _GROUP_LABEL = {"date": "[green]Date[/green]",
                            "project": "[green]Project[/green]",
                            "state": "[green]State[/green]"}
            if not _bar_hidden:
                group_str = ""
            elif _get_group_by() in _GROUP_LABEL:
                group_str = f"{sep}Group: " + _GROUP_LABEL[_get_group_by()]
            else:
                group_str = f"{sep}[dim]Group: off[/dim]"
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
                # !M = live panes that are reply-due, via the SAME _reply_due
                # predicate the per-row "!" uses, so the badge and the markers can
                # never disagree. Iterating _st (live sids only) also keeps a
                # just-closed pane — forgotten from statuses but cleared from
                # _unread only later by the reader's exit callback — from inflating
                # the count (matches action_next_attention).
                _done = sum(1 for sid in _st if self._reply_due(sid))
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
                    _kw = _ram_gate_kwargs()
                    # 'fit' from the SAME gate math (commit/load/phys), not a raw
                    # free-RAM floor, so the indicator matches what the gate allows.
                    fit, _ = _ram_fit(_ms, per, **_kw)
                    fit = min(fit, max(0, self._live.max_live - cnt))   # MAX_LIVE backstop
                    live_str = f"{sep}" + _live_ram_segment(
                        cnt, _att, _ms, fit, per,
                        float(_kw.get("max_load", _DEFAULT_MAX_LOAD)))
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
                    search_str = f"{sep}[yellow]search: {_esc_markup(repr(_qd))}[/yellow]"
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
            # Persistent web-mirror indicator: how many browsers are connected
            # right now, so an unexpected viewer is always visible at the terminal.
            _mc = getattr(self, "_mirror_clients", 0)
            if _mc:
                _kb_parts.insert(0, f"[b]\N{GLOBE WITH MERIDIANS} {_mc}[/b]")
            _kb = " · ".join(_kb_parts)
            # Context-fill gauge (ground-truth tokens): the FOCUSED live pane if any,
            # else the CURSOR session — so it's visible whether you're typing in a
            # pane or just browsing the list. A b2 checkpoint re-points the running
            # pane at its fresh child, so prefer the pane's live jsonl.
            ctx_str = ""
            _cft = self._focused_terminal() if self._live is not None else None
            if _cft is not None:
                _cjp = (getattr(_cft, "_live_jsonl", None)
                        or (self._sid_index.get(getattr(_cft, "sid", None)) or {}).get("jsonl_path"))
            else:
                _cjp = (self._sid_index.get(self._cursor_sid()) or {}).get("jsonl_path")
            if _cjp:
                _ctok, _cmodel = _ctx_usage_from_jsonl(_cjp)
                if _ctok is not None:
                    _cseg = _ctx_gauge_segment(_ctok, _ctx_window_for(
                        _ctok, model=_cmodel,
                        override=_cfg("context", "window", "SAIKAI_CTX_WINDOW", 0, int) or None))
                    if _cseg:
                        ctx_str = f"{sep}{_cseg}"
            text = (f"  {n} sessions{search_str}{sort_str}"
                    f"{scope_str}{group_str}{filt_str}{tree_str}"
                    f"{live_str}{ctx_str}{sep}{_kb}")
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

        def _selected_or_cursor(self) -> list:
            """Bulk-scope resolver: the marked sids (Space-Space selection) in
            display order if any rows are marked, else the single cursored row
            (or []). This is the ONE place the "marks-else-cursor" rule lives, so
            every bulk-capable verb (resume / favorite / hide) scopes identically
            — the gesture is uniform, not a per-action special case. Filtering the
            marks through `all_sessions` also drops a stale mark (a session that
            left the list) here rather than acting on it."""
            if self._marked:
                return [s["id"] for s in all_sessions if s["id"] in self._marked]
            sid = self._cursor_sid()
            return [sid] if sid else []

        def _apply_split_ratio(self, ratio: float) -> None:
            self.call_after_refresh(self._mirror_sync_geometry)  # regions moved (#mirror-regions)
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
            if not sid:
                self._set_preview()
                return
            cache_dir = (PREVIEW_FULL_DIR if self.preview_mode == "full"
                         else PREVIEW_DIR)
            cache_file = cache_dir / f"{sid}.txt"
            # Rendering runs on the UI thread; guard it like the other handlers
            # in this class so one malformed session shows a per-row message
            # instead of tearing down the whole picker. Body renderables are
            # collected into `parts` and pushed in ONE _set_preview (Static shows a
            # single renderable; multiple are stacked in a Group).
            try:
                s = self._sid_index.get(sid)
                parts: list = []
                # Context legend: explain THIS session's activity/state glyphs
                # (the +/./*/@/&/… the user sees in the list) at the top of the
                # preview. Only the markers it actually has, so it's a 1-line crib,
                # not the full key. (#marker-legend)
                if s is not None:
                    _leg = _marker_legend(s, _load_favorites(), _load_hidden())
                    if _leg:
                        parts.append(Text("markers   " + "    ".join(_leg),
                                          style="dim italic"))
                if self.preview_mode == "changes" and s is not None:
                    # Transcript-reconstructed diff; render on demand (no cache).
                    parts.append(Text.from_ansi(_render_preview_changes(s)))
                    self._set_preview(*parts)
                    return
                # Open sessions grow every turn, so a cached preview goes stale.
                # Render them fresh each time (skip the cache entirely).
                if s is not None and s.get("is_open"):
                    render = (_render_preview_full if self.preview_mode == "full"
                              else _render_preview)
                    parts.append(Text.from_ansi(render(s)))
                    self._set_preview(*parts)
                    return
                if not cache_file.exists() and s is not None:
                    # Warm on demand (fallback for rows the background pre-warm
                    # has not reached yet) so the cache stays self-sufficient.
                    _write_preview_cache(s)
                if cache_file.exists():
                    parts.append(Text.from_ansi(cache_file.read_text(encoding="utf-8")))
                else:
                    parts.append(Text(f"(no preview for {sid[:8]})"))
                self._set_preview(*parts)
            except Exception as e:
                self._set_preview(Text(f"(preview failed for {sid[:8]}: {e})"))

        def _set_preview(self, *renderables) -> None:
            """Replace the preview Static's content with one or more Rich
            renderables (Static shows a single renderable, so stack them in a
            Group), then scroll the viewport to the top. Selectable + format-
            preserving, unlike the old RichLog. (#preview-select)"""
            try:
                pv = self.query_one("#preview", Static)
            except Exception:
                return
            if not renderables:
                pv.update("")
            elif len(renderables) == 1:
                pv.update(renderables[0])
            else:
                from rich.console import Group
                pv.update(Group(*renderables))
            try:
                self.query_one("#preview-scroll", VerticalScroll).scroll_home(
                    animate=False)
            except Exception:
                pass

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

        def _filter_is_engaged(self) -> bool:
            """True while the list is being actively FILTERED: the search box is
            focused, OR a filter keystroke landed within the last beat. The
            highlight handler uses this to NOT switch the foreground live pane out
            from under a filter. The time window matters because the post-filter
            RowHighlighted is queued (the rebuild is call_after_refresh'd), so a
            bare `self.focused is #search` check can miss it if focus momentarily
            moved during the rebuild — which silently switched the foreground."""
            try:
                if self.focused is self.query_one("#search", Input):
                    return True
            except Exception:
                pass
            return time.monotonic() < getattr(self, "_filter_active_until", 0.0)

        def on_key(self, event) -> None:
            # Clear the one-shot arm-suppress at the START of every key event: it is
            # set only by a leader-key dispatch below and consumed by the
            # action_arm_leader binding that fires AFTER this handler in the SAME
            # event. Clearing it here (next event) keeps it from lingering on a
            # platform where event.stop() DID block the binding. (#H10)
            self._suppress_arm = False
            # Ctrl+C reaching the App means the LIST or search box is focused (a
            # focused live terminal consumes Ctrl+C first, to interrupt claude).
            # Route it through the double-press guard, then our force-quit
            # (kill-all + join) so it never exits WITHOUT reaping the claude trees.
            # Textual's built-in ctrl+c is `system` (non-priority), so this handler
            # + event.stop() shadow it. (Ctrl+Q normally arrives via Textual's
            # PRIORITY ctrl+q->quit binding -> our guarded action_quit, before
            # on_key; handling it here too is a harmless fallback.)
            if event.key in ("ctrl+c", "ctrl+q"):
                # A modal / pushed screen owns the keyboard. Help / Mirror QR /
                # Settings define no ctrl+c binding, so a reflex Ctrl+C bubbles here
                # — it must NOT arm the app-quit guard (a double press would exit
                # from under a dialog). Esc closes the modal; quit is list-only.
                if len(self.screen_stack) > 1:
                    return
                event.stop()
                # Double-press guard: a single reflex Ctrl+C (claude treats it as
                # interrupt and exits only on a SECOND one) must not kill saikai.
                # Esc shares the same arm (see _confirm_quit / action_quit).
                if self._confirm_quit():
                    self.action_quit_all()
                return
            # Any other key clears a pending quit-arm, so only two CONSECUTIVE
            # quit presses exit (Esc reaches action_quit via its binding, so it
            # must NOT be treated as "another key" here).
            if getattr(self, "_quit_armed", False) and event.key != "escape":
                self._disarm_quit()
            # Leader/prefix (opt-in, [keys] leader). The leader arms a pending state;
            # the next key runs the mapped action. A focused claude pane consumes its
            # own keys, while the App binding may arm Space from other non-input,
            # non-dropdown saikai controls. Handled BEFORE search-as-you-type so
            # the post-leader letter doesn't fall through and open the search box.
            if self._leader_key:
                if self._leader_pending:
                    self._leader_pending = False
                    self._set_leader_hint(None)
                    event.stop()
                    if event.key == self._leader_key:
                        # event.stop() does NOT block the App's OWN non-priority
                        # space→arm_leader binding, which fires AFTER this dispatch
                        # with _leader_pending now False and re-arms — leaving the
                        # leader stuck after a double-Space mark, so the next key is
                        # hijacked. Eat that one spurious arm. (#H10)
                        self._suppress_arm = True
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
                    self._leader_gen += 1
                    _g = self._leader_gen
                    # which-key style: hint only on HESITATION (no second key
                    # within 0.6 s). Fast fingers (Space-f, double-Space mark
                    # sprees) never see a toast; a user who pauses gets the map,
                    # grouped by family — every time, not just the first three.
                    # _g tags both timers so a stale one from an earlier press
                    # (Space-h then Space) can't cancel/redraw THIS session's menu.
                    self.set_timer(0.6, lambda: self._show_leader_hint(_g))
                    self.set_timer(6.0, lambda: self._cancel_leader(_g))
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

        def _cancel_leader(self, gen=None) -> None:
            """Leader timed out / cancelled — drop the pending state (the next key
            types normally again). `gen` guards a STALE timer from an earlier press
            (Space-h then Space) from cancelling a newer, still-open session."""
            if gen is not None and gen != self._leader_gen:
                return
            self._leader_pending = False
            self._set_leader_hint(None)

        def action_arm_leader(self) -> None:
            """The footer's ␣ Menu binding: arm the leader from any non-typing
            context. Fires only when space bubbled UNCONSUMED to the App (an
            Input or terminal keeps its space; the table fast path in on_key
            already stopped the event), so no double-arm and no stolen keys."""
            if self._leader_key != "space":
                raise SkipAction()
            if self._suppress_arm:
                # This arm is the spurious one firing right after a leader-key
                # dispatch (double-Space) — eat it so the leader doesn't re-arm. (#H10)
                return
            if self._leader_pending:
                return
            if (self._focused_terminal() is not None
                    or isinstance(self.focused, (Input, Select))):
                return
            self._leader_pending = True
            self._leader_gen += 1
            _g = self._leader_gen
            self.set_timer(0.6, lambda: self._show_leader_hint(_g))
            self.set_timer(6.0, lambda: self._cancel_leader(_g))

        def _set_leader_hint(self, lines: "list[str] | None") -> None:
            """Show the which-key panel with `lines` (a dedicated bottom-docked
            Static — NOT a toast — so the family columns align and it never sits
            over the live pane's bottom-right), or hide it when `lines` is None.
            Safe to call before mount / after teardown."""
            try:
                panel = self.query_one("#leaderhint", Static)
            except Exception:
                return
            if lines:
                panel.update("\n".join(lines))
                panel.display = True
            else:
                panel.display = False

        def _show_leader_hint(self, gen=None) -> None:
            """Deferred which-key panel: fires 0.6 s after the leader press, and
            only if the sequence is STILL pending — the user hesitated, so show the
            map grouped by family. Completed / cancelled sequences (and a
            double-Space mark spree) never see it. `gen` ignores a stale timer from
            an earlier press so it can't redraw over a newer session's menu."""
            if gen is not None and gen != self._leader_gen:
                return
            if not self._leader_pending or not self._leader_actions:
                self._set_leader_hint(None)
                return
            lines = ["[bold cyan]Command menu[/bold cyan]  "
                     "[dim]press one key · Esc cancels[/dim]"]
            for fam, pairs in _leader_groups(self._leader_actions):
                seq = "  ".join(_leader_hint_item(k, lbl) for k, lbl in pairs)
                lines.append(f"[bold cyan]{fam:<7}[/bold cyan] {seq}")
            self._set_leader_hint(lines)

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
            # While the WT WINDOW is blurred (alt-tabbed away), do nothing. A
            # background poll/rebuild fires RowHighlighted, and the focus switch
            # below would move focus onto the DataTable *during the blur*. Textual's
            # _watch_app_focus restores focus on window-return ONLY when
            # screen.focused is None; a blur-time table focus leaves it non-None, so
            # the restore is SKIPPED and focus is stranded on the list — the live
            # pane never regains focus and WT shows the IME × (log: APP_FOCUS
            # restored=DataTable, alternating with =AgentTerminal → the ×/ON flicker).
            # Skipping while blurred keeps screen.focused=None so Textual restores the
            # pane correctly on return. (#ime-focus-pingpong — deduced from the dumps)
            if not getattr(self, "app_focus", True):
                return
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
                    self._set_preview(
                        Text(f"\n  ── {label} ──", style="bold #7aa2f7"),
                        Text("  group header — no session selected", style="dim"))
                except Exception:
                    pass
                return
            # Remember where the cursor is now so the next header-skip knows
            # which way we're traveling.
            try:
                self._last_cursor_row = self.query_one("#table", DataTable).cursor_row
            except Exception:
                pass
            # Keep the context-fill gauge tracking the CURSOR. When no live pane is
            # focused the gauge reads the CURSOR session's transcript, but it was only
            # recomputed on a full rebuild — so arrow-browsing the list left it frozen
            # on the startup row's value, looking unrelated to the highlighted session.
            # Refresh it on every real cursor landing (cheap: _ctx_usage_from_jsonl is
            # now (mtime,size)-cached, so this is a dict hit, not a transcript re-read).
            try:
                self._update_subtitle()
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
                if self._filter_is_engaged():
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
                # A background re-highlight (the 1.5s status poll rebuilds the
                # table and re-fires RowHighlighted for the cursored row) must NOT
                # yank focus off a live pane the user is typing in. Stealing it to
                # the table blurs the pane every poll → on_blur hides the cursor
                # (?25l) + pauses the keepalive → WT drops the IME to ×: the real
                # ×/OK flicker (NOT a missing keepalive). A row highlight can only
                # come from a background rebuild while a terminal is focused (arrow
                # nav needs table focus), so when one is focused, leave it. (#ime-focus-steal)
                if not just_opened and self._focused_terminal() is not None:
                    return
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
                    if self._remote_origin_block(sid):   # Desktop-SSH mirror (#remote-origin)
                        return
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

        def _remote_origin_block(self, sid: str) -> bool:
            """True (+ an explanatory toast) when sid is a Desktop-SSH mirror of a
            session that RAN ON ANOTHER HOST: its cwd doesn't exist here, so a
            local `claude --resume` can't reattach it (and must not try — claude
            would fail against a foreign-cwd transcript in its own projects dir).
            Resume it where it ran, or via Claude Desktop's SSH view. (#remote-origin)"""
            s = self._sid_index.get(sid)
            if not (s and s.get("remote_origin")):
                return False
            try:
                self.notify(
                    "this session ran on another host via Claude Desktop's SSH "
                    f"integration (cwd: {s.get('cwd') or '?'})\n"
                    "resume it there, or from Claude Desktop",
                    title="remote session", severity="warning", timeout=8)
            except Exception:
                pass
            return True

        def action_resume_detached(self) -> None:
            """Legacy full-takeover: exit the picker and run claude in the bare
            terminal (alternate screen handed off). Kept as an escape hatch for
            users who want a full-screen claude instead of the split pane."""
            sid = self._cursor_sid()
            if self._remote_origin_block(sid or ""):    # Desktop-SSH mirror (#remote-origin)
                return
            if sid:
                # Tear down any live panes first so their PTYs don't outlive the
                # picker as orphans once we exit into the foreground claude. WAIT
                # for the reaps: exit() → _resume_claude() immediately execs a new
                # foreground `claude --resume <sid>` in the SAME cwd, so a still-
                # dying old process could race the new one on the transcript/lock
                # (corrupted turns, "session in use") — and atexit won't fire until
                # that foreground child returns. action_quit_all uses wait=True for
                # the same reason; keep this path symmetric.
                if self._live is not None:
                    self._live.kill_all(wait=True)
                self.exit(sid)

        def action_toggle_mark(self) -> None:
            """Toggle the cursor row's selection (Space-Space). The marked set is
            consumed by the bulk-capable verbs via _selected_or_cursor: Enter
            (resume) opens one live pane per marked session; ␣f / ␣h favorite /
            hide all marked. Single-target verbs (rename / copy-prompt / diff /
            resume-detached / close) deliberately ignore marks. Split-live only."""
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
            to relaunch and Esc/F10 close the x tab."""
            if _LIVE_TERM is None:
                return None
            foc = self.focused
            if isinstance(foc, _LIVE_TERM.AgentTerminal) and not getattr(foc, "is_dead", False):
                return foc
            return None

        def _pane_typing_recently(self, within: float = 4.0) -> bool:
            """True while the user is ACTIVELY typing into the focused live pane
            — the only state a background table rebuild can disrupt (keystrokes
            racing the rebuild). Focus alone is NOT typing: deferring on mere
            focus froze the State groups whenever focus was parked in a pane,
            because on a quiet POSIX pty the final busy→idle tick comes from the
            UI-thread poll (the reader's marshalled callback either races pane
            registration or never fires once the child goes silent — ConPTY's
            chatty idle output masked all of this on Windows). Keys, paste and
            mirror-injected bytes all stamp last_input_ts. (#linux-state-regroup)"""
            t = self._focused_terminal()
            if t is None:
                return False
            return (time.monotonic() - getattr(t, "last_input_ts", 0.0)) < within

        def _visible_terminal(self):
            """The AgentTerminal in the active live tab, regardless of keyboard
            focus — so a paste (e.g. a file dragged onto the pane) reaches claude
            even while the session list, not the pane, has focus. None if split-
            live isn't up or the active tab has no live terminal."""
            if _LIVE_TERM is None or self._live is None:
                return None
            from textual.widgets import TabbedContent
            try:
                tabs = self.query_one("#right", TabbedContent)
                pane = tabs.get_pane(tabs.active) if tabs.active else None
                term = pane.query_one(_LIVE_TERM.AgentTerminal) if pane else None
            except Exception:
                return None
            if term is not None and not getattr(term, "is_dead", False):
                return term
            return None

        def on_paste(self, event) -> None:
            """Route a paste that no focused text input consumed to the VISIBLE
            live pane, so a file dragged onto the claude pane pastes its path into
            claude even while the session list has keyboard focus. A focused live
            pane already handled it (AgentTerminal.on_paste stops the event, so
            this never fires for it); a focused Input/TextArea (search box,
            checkpoint editor) keeps its own paste; the list falls through to
            claude, and focus moves to the pane so the user can keep typing."""
            text = getattr(event, "text", "")
            if not text:
                return
            from textual.widgets import Input, TextArea
            if isinstance(self.focused, (Input, TextArea)):
                return
            term = self._visible_terminal()
            if term is not None:
                term.paste_text(text)
                try:
                    term.focus()
                except Exception:
                    pass
                event.stop()

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

        def on_app_focus(self, event=None) -> None:
            """The OS window regained focus (terminal FocusIn, ?1004). Textual
            WIDGET focus did NOT change — the live pane still has it — so the
            pane's own on_focus never fires, the cursor anchor goes stale, and WT
            shows the IME disabled (×) on window switch until the next claude
            redraw happens to re-anchor it (hence the intermittency: idle panes
            stay ×, busy ones self-heal). Re-anchor the focused pane's cursor on
            window focus-in, and again after the refresh Textual runs. (#ime-race)"""
            w = getattr(self, "focused", None)
            sync = getattr(w, "_sync_terminal_cursor", None)   # only AgentTerminal has it
            if sync is None:
                return
            try:
                # If the window blurred mid-drag (button held during alt-tab), the
                # pane never got its MouseUp — drop any stuck forwarded-drag capture
                # on return so it doesn't funnel phantom motion. (#faithful-mouse)
                cancel = getattr(w, "_cancel_forwarded_drag", None)
                if cancel is not None:
                    cancel()
                # The OS window regaining focus doesn't fire the pane's own
                # on_focus, so re-anchor its cursor — otherwise WT leaves the IME
                # disabled until the next redraw. (#ime-race)
                show = getattr(w, "_show_hw_cursor", None)
                if show is not None:
                    show(True)            # re-show the native cursor on window return
                sync(reason="focus")
                self.call_after_refresh(lambda: sync(reason="focus"))
            except Exception:
                pass

        def _open_or_attach_live(self, sid: str, refresh: bool = True) -> None:
            """Resume an existing session as a live pane (or switch to it if it's
            already running)."""
            assert _LIVE_TERM is not None and self._live is not None
            if self._remote_origin_block(sid):       # Desktop-SSH mirror (#remote-origin)
                return
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
            title = _pane_title(s, sid)

            def _spawn() -> None:
                try:
                    argv, cwd, env = _build_resume_invocation(sid, all_sessions)
                except Exception as e:
                    self.notify(f"could not build resume command: {e!r}",
                                severity="error", timeout=8)
                    return
                self._spawn_live_pane(sid, argv, cwd, env, title, refresh=refresh)

            # A running BACKGROUND agent/job (kind=bg, the & marker): it's a headless
            # live session owned by its bg process — there is no interactive window to
            # attach to, and `claude --resume` on a live session conflicts with the
            # owner. Refuse with a clear reason; it becomes resumable once the bg job
            # finishes and its registry entry drops (is_bg → False on the next scan).
            if s and s.get("is_bg"):
                _k = s.get("live_kind") or "bg"
                _log(f"open SKIP {sid[:8]}: is_bg kind={_k} (not attachable)")
                if _k == "agent":
                    self.notify(
                        "claude agents session — it belongs to its parent claude; "
                        "drive it from the agents view there. It becomes resumable "
                        "here once it finishes.",
                        severity="warning", title="saikai", timeout=8)
                else:
                    self.notify("running background agent — can't resume a live session "
                                "(resume it after the bg job finishes)",
                                severity="warning", title="saikai", timeout=8)
                return
            # Already open in another Claude window/instance (the @ marker): a second
            # `claude --resume` on the same JSONL can interleave/corrupt it. Confirm.
            if s and s.get("is_open"):
                _log(f"open GATE {sid[:8]}: is_open (open elsewhere) → confirm prompt")
                self.push_screen(OpenElsewhereScreen(title),
                                 lambda ok: _spawn() if ok else None)
                return
            _spawn()

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
                if _cfg("limits", "hard_ram_gate", "SAIKAI_HARD_RAM_GATE",
                        _mem_safety_preset()["hard"], _cfg_bool):
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
            pane = TabPane(Content(_LIVE_TERM.tab_label(title, "idle")), term, id=pane_id)  # Content: markup-safe title (rich Text crashes render_str) (#audit-toast-markup)
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
            registered = False
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
                    self.notify(f"could not open tab: {e!r}", severity="error", timeout=8)
                    return
                # claude died DURING mount (pyte/ConPTY spawn failed, or an instant
                # EOF marshalled _finalize while we awaited add_pane). Do NOT register
                # it — that would re-add a dead 'idle' pane to the manager AND to the
                # Shift+F4 restore set. Drop the zombie tab and tell the user.
                if getattr(term, "is_dead", False):
                    try:
                        await tabs.remove_pane(pane_id)
                    except Exception:
                        pass
                    self.notify(f"session {sid[:8]} could not start",
                                severity="warning", timeout=6)
                    return
                self._live.register(sid, term)
                registered = True
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
                # Safety net for the un-registered term. The explicit error paths
                # above already `return` without registering; this ALSO catches
                # asyncio.CancelledError (a BaseException, so it bypasses the
                # `except Exception` above) raised when App exit calls
                # workers.cancel_all() at a pending `await` — otherwise a PTY that
                # AgentTerminal.on_mount already spawned would leak, untracked by
                # LiveSessionManager/join_all_reaps, surviving saikai's exit. (#audit-mount-cancel)
                if not registered:
                    try:
                        self._live.note_reap(term.kill())
                    except Exception:
                        pass
                self._opening_sids.discard(sid)

        def _save_open_panes(self) -> None:
            """Persist {id, cwd} for panes open this session so Shift+F4 can reopen
            them after a restart/upgrade (cwd lets an out-of-scope session resume in
            the right dir). Best-effort; never blocks the UI."""
            try:
                # Read-merge instead of clobber: two concurrent saikai instances
                # share ONE OPEN_PANES_FILE, so a blind overwrite drops the other
                # instance's panes (last-writer-wins). Keep existing entries that
                # are STILL LIVE in another instance (so they aren't lost) but not
                # ones this instance closed (killed → absent from active → dropped,
                # so a closed pane doesn't resurrect). (#audit-openpanes-clobber)
                merged: dict[str, dict] = {}
                active = _load_active_sessions()
                for row in (_read_json(OPEN_PANES_FILE, []) or []):
                    if isinstance(row, dict) and row.get("id") in active:
                        merged[row["id"]] = row
                for sid in self._opened_sids:
                    s = self._sid_index.get(sid) or {}
                    merged[sid] = {"id": sid,
                                   "cwd": s.get("origin_cwd") or s.get("cwd") or ""}
                _write_json(OPEN_PANES_FILE, [merged[k] for k in sorted(merged)])
            except Exception:
                pass

        def action_restore_panes(self) -> None:
            """Shift+F4: reopen the PREVIOUS session's panes (snapshot loaded at
            startup) — resume each, skipping ones already open. Available anytime,
            not just at launch. An out-of-scope sid (different project dir) gets a
            stub injected with its saved cwd so resume targets the right dir."""
            if _LIVE_TERM is None or self._live is None:
                return
            cands = list(getattr(self, "_restore_candidates", []) or [])
            opened = 0
            skipped = {"no_id": 0, "already_live": 0, "no_cwd": 0}
            for row in cands:
                sid = row.get("id") if isinstance(row, dict) else row
                if not isinstance(sid, str) or not sid:
                    # a corrupt open-panes entry (int/null/nested) must be skipped,
                    # not TypeError later at sid[:8]. (#audit-hostile-files)
                    skipped["no_id"] += 1
                    continue
                if self._live.has(sid):
                    skipped["already_live"] += 1
                    continue
                cwd = row.get("cwd", "") if isinstance(row, dict) else ""
                if sid not in self._sid_index:
                    if cwd and Path(cwd).is_dir():
                        stub = _new_session_stub(sid, cwd, Path(cwd).name or sid[:8])
                        all_sessions.append(stub)
                        self._sid_index[sid] = stub
                    else:
                        # not scanned and no usable cwd → can't resume. Log it so a
                        # lossy restore is diagnosable instead of silent. (#restore-diag)
                        skipped["no_cwd"] += 1
                        _log(f"restore SKIP {sid[:8]}: not in index, cwd={cwd!r} "
                             f"is_dir={bool(cwd) and Path(cwd).is_dir()}")
                        continue
                self._open_or_attach_live(sid, refresh=False)
                opened += 1
            _log(f"restore: candidates={len(cands)} opened={opened} skipped={skipped}")
            if opened:
                self._refresh_table()
                self.notify(f"reopened {opened} pane(s) from last session", timeout=4)
            elif skipped["no_cwd"]:
                self.notify(f"couldn't restore {skipped['no_cwd']} pane(s) — their "
                            f"folder couldn't be resolved (see saikai.log)",
                            severity="warning", timeout=6)
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
                    capture_output=True, text=True,
                    encoding="utf-8", errors="replace",   # git emits UTF-8; don't decode as cp932
                    timeout=5, creationflags=NO_WINDOW)
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
            # If the user is FILTERING (search box focused), a background rebuild
            # fires a queued RowHighlighted a frame later — by then the rebuild may
            # have momentarily taken focus off the search box, so the foreground
            # guard's `self.focused is #search` check reads False and the 0.5s
            # keystroke window may have lapsed. Re-arm the window on EVERY refresh
            # request while searching, so on_data_table_row_highlighted still treats
            # it as filter-engaged and doesn't switch the foreground pane out.
            try:
                if self.focused is self.query_one("#search"):
                    self._filter_active_until = time.monotonic() + 0.5
            except Exception:
                pass
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
            self._apply_live_status(sid, status)
            # Mirror onto the DataTable marker so a backgrounded waiting session
            # is loud even when its tab isn't focused. Coalesced: a streaming
            # claude flips status many times/sec and each full rebuild is costly.
            self._request_refresh()

        def _apply_live_status(self, sid: str, status: str) -> None:
            """The _unread / _busy_seen bookkeeping + tab-glyph repaint a status
            change implies. Shared by _on_live_status (reader-marshalled, once per
            transition) AND _poll_live_status: the 1.5s poll runs on the app's OWN
            thread, where the reader's call_from_thread marshal is a silent no-op,
            so when the poll is the only thing that noticed a flip (the reader
            stayed blocked in read() with no final chunk) it must replay these
            side-effects itself — otherwise the '!' reply-due marker, the !M badge,
            and the tab glyph never update for a backgrounded pane that finished
            its turn (exactly the case this poll exists to catch)."""
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
            # Update the tab label glyph.
            try:
                tabs = self.query_one("#right", TabbedContent)
                s = self._sid_index.get(sid)
                title = _pane_title(s, sid, self._live.get(sid))
                pane = tabs.get_pane(self._live.pane_id(sid))
                if pane is not None:
                    # Relabel via the DISPLAYED Tab widget: TabPane has no `label`
                    # property, so `pane.label = …` only set a dead attribute and the
                    # glyph never changed. Tab.label's setter calls update(); passing
                    # Content keeps a '[' in the title literal. (#tab-glyph-update)
                    tabs.get_tab(pane).label = Content(_LIVE_TERM.tab_label(title, status))
            except Exception:
                pass

        def action_dump_pane(self) -> None:
            """Ctrl+F12: write the focused (else active-tab) live pane's visible
            pyte screen + geometry to ~/.cache/saikai/pane-dump.txt, so a garbled
            render can be inspected as plain text off the live UI. (#pane-dump)"""
            if _LIVE_TERM is None or self._live is None:
                self.notify("no live panes", timeout=3)
                return
            t = self._focused_terminal()
            if t is None:
                try:
                    active = self.query_one("#right", TabbedContent).active or ""
                except Exception:
                    active = ""
                for _sid in list(self._live.statuses().keys()):
                    if self._live.pane_id(_sid) == active:
                        t = self._live.get(_sid)
                        break
            if t is None:
                self.notify("no live pane to dump (focus one or open its tab)",
                            timeout=4)
                return
            try:
                p = CACHE_DIR / "pane-dump.txt"
                p.write_text(t.snapshot_text(), encoding="utf-8")
                self.notify(f"pane dumped → {p}", timeout=5)
            except Exception as e:
                self.notify(f"dump failed: {e}", severity="error", timeout=6)

        def _reply_due(self, sid: str) -> bool:
            """A LIVE pane is 'reply due' — the row marker '!' and the statusbar !M
            — when it has FINISHED its turn (idle) and you haven't responded since
            (still in _unread). A waiting pane shows '?' (counted in ?N) and a busy
            pane was discarded from _unread; neither is reply-due. Single source of
            truth so the per-row '!' and the !M badge can never disagree."""
            return (self._live is not None
                    and self._live.status(sid) == "idle"
                    and sid in getattr(self, "_unread", ()))

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
            # If the pane that just died was the FOCUSED one, focus is now stranded
            # on the dead terminal (it stays mounted to show the final frame, and
            # _focused_terminal() excludes dead panes), so every later keystroke is
            # silently dropped until the user clicks / Enter / Ctrl+]. Capture that
            # here (before forget()) and return focus to the list at the end — but
            # ONLY when it WAS focused, so a BACKGROUND pane exiting never steals
            # focus from the pane you are typing in.
            _dead_t = self._live.get(sid)
            _was_focused = _dead_t is not None and self.focused is _dead_t
            self._unread.discard(sid)   # a dead pane is no longer a live unread answer
            self._busy_seen.discard(sid)  # …nor owed a "done" toast
            try:
                tabs = self.query_one("#right", TabbedContent)
                s = self._sid_index.get(sid)
                title = _pane_title(s, sid, self._live.get(sid))
                pane = tabs.get_pane(self._live.pane_id(sid))
                if pane is not None:
                    tabs.get_tab(pane).label = Content(_LIVE_TERM.tab_label(title, "dead"))  # Tab.label (not TabPane) updates the display (#tab-glyph-update)
            except Exception:
                pass
            self._live.forget(sid)
            self._mark_not_open(sid)         # exited → no longer Open (drop the @ marker)
            # A session whose claude EXITED on its own shouldn't reappear on the
            # next Shift+F4 restore (matches explicit-close in _close_live_sid).
            # The dead x tab stays visible THIS session; re-launching it with Enter
            # re-adds it via _open_or_attach_live. Persist the trimmed snapshot now.
            # EXCEPTION: during quit teardown (_quitting), kill_all fires this for
            # every pane — eroding + re-saving here empties the restore snapshot we
            # just wrote in action_quit_all (the file ended up []). Skip it so quit
            # preserves the open set for Shift+F4. (#restore-erosion)
            if not getattr(self, "_quitting", False):
                self._opened_sids.discard(sid)
                self._save_open_panes()
            self._prune_dead_panes()   # bound retained x dead-pane memory (#H6)
            self._refresh_table()
            # The mirror's pane channel must learn of the death NOW (meta
            # open:false / follow the next pane) — not at the next poll tick,
            # during which phone keystrokes would drop silently against the
            # nulled PTY. (#review-dead-pane-window)
            self._mirror_sync_pane()
            if _was_focused:
                try:
                    self.query_one("#table", DataTable).focus()
                except Exception:
                    pass

        def _prune_dead_panes(self) -> None:
            """Unmount the OLDEST dead (x) panes so their pyte buffers are freed. A
            dead pane is kept mounted to show its final frame, but it's forgotten
            from self._live, so the MAX_LIVE gate doesn't count it — without a cap,
            letting claude exit repeatedly (without F10) grows memory unbounded (each
            AgentTerminal pins its full pyte HistoryScreen, tens of MB). Keep only the
            most-recent few; never remove the currently-active tab. (#H6)"""
            if self._live is None:
                return
            keep = _cfg("limits", "max_dead_panes", "SAIKAI_MAX_DEAD_PANES", 3, int)
            try:
                tabs = self.query_one("#right", TabbedContent)
                alive = {self._live.pane_id(x) for x in self._live.statuses()}
                active = tabs.active or ""
                dead = [pid for pid in self._live_pane_ids() if pid not in alive]
                if len(dead) <= keep:
                    return
                for pid in dead[:len(dead) - keep]:   # oldest-first (DOM order)
                    if pid != active:
                        try:
                            tabs.remove_pane(pid)
                        except Exception:
                            pass
            except Exception:
                pass

        def on_agent_terminal_focus_released(self, event) -> None:
            """The terminal's Ctrl+] (SAIKAI_RELEASE_KEY) escape hatch: refocus the list."""
            self.query_one("#table", DataTable).focus()
            try:
                event.stop()
            except Exception:
                pass

        def on_resize(self, event=None) -> None:
            # The terminal changed size (window/font resize) — the mirror models
            # the host at a FIXED grid, so it must be told NOW or every frame
            # garbles until the next poll. Read the size from the EVENT: self.size
            # may not have committed the new value yet inside this handler.
            # (#mirror-resize)
            _hub = getattr(self, "_mirror_hub", None)
            if _hub is None:
                return
            try:
                sz = getattr(event, "size", None) if event is not None else None
                w = sz.width if sz is not None else self.size.width
                h = sz.height if sz is not None else self.size.height
                _hub.set_size(w, h)
            except Exception:
                pass
            # regions/pane geometry read content_region, which settles after
            # this reflow — run the FULL sync then, so a host resize reaches the
            # pane channel too: new pane cols/rows + a reseed. (The child's
            # post-SIGWINCH repaint would otherwise land on a browser grid of
            # the old size and stay garbled until an unrelated reseed.)
            # (#review-pane-resize)
            try:
                self.call_after_refresh(self._mirror_sync_geometry)
            except Exception:
                pass

        def _mirror_sync_geometry(self) -> None:
            """Push the host geometry the mirror can't otherwise know: terminal
            SIZE (a resize reflows every absolute-positioned frame — a frozen
            browser grid garbles) AND the scrollable region rects. One sync
            called from every layout-changing event + the poll backstop; the hub
            dedups both, so spamming it is cheap. (#mirror-resize #mirror-regions)"""
            _hub = getattr(self, "_mirror_hub", None)
            if _hub is None:
                return
            try:
                _hub.set_size(self.size.width, self.size.height)
            except Exception:
                pass
            self._mirror_push_regions()
            self._mirror_sync_pane()

        def _mirror_push_regions(self) -> None:
            """Publish the scrollable content rectangles (cell coords) to the
            mirror: the session list and the VISIBLE live pane. The browser's
            select-mode edge auto-scroll needs the PANE's own edges — they sit
            mid-canvas, so the canvas-edge zone never fired for a selection
            inside the claude pane. Hub-side dedup makes this hot-path cheap;
            called from the list rebuild and the status poll. (#mirror-regions)"""
            _hub = getattr(self, "_mirror_hub", None)
            if _hub is None:
                return
            regs = []
            try:
                r = self.query_one("#table", DataTable).content_region
                if r.width > 0 and r.height > 0:
                    regs.append({"x": r.x, "y": r.y, "w": r.width,
                                 "h": r.height, "k": "list"})
            except Exception:
                pass
            try:
                if _LIVE_TERM is not None:
                    for t in self.query(_LIVE_TERM.AgentTerminal):
                        r = t.content_region
                        # only the VISIBLE pane (hidden tabs report zero-size)
                        if r.width > 0 and r.height > 0 and t.display:
                            regs.append({"x": r.x, "y": r.y, "w": r.width,
                                         "h": r.height, "k": "pane"})
            except Exception:
                pass
            try:
                _hub.set_regions(regs)
            except Exception:
                pass

        def _mirror_pane_target(self):
            """The live pane the mirror's pane view should follow: the focused
            AgentTerminal when one has focus, else the VISIBLE one in the split
            (the phone watches claude while the local user browses the list).
            None when no live pane is visible. (#pane-direct)"""
            if _LIVE_TERM is None:
                return None
            t = self._focused_terminal()
            if t is not None and not getattr(t, "is_dead", False):
                return t
            try:
                for t in self.query(_LIVE_TERM.AgentTerminal):
                    r = t.content_region
                    if r.width > 0 and r.height > 0 and t.display \
                            and not getattr(t, "is_dead", False):
                        return t
            except Exception:
                pass
            return None

        def _mirror_sync_pane(self) -> bool:
            """Keep the mirror's pane-direct channel following reality: publish
            the target's geometry/liveness FIRST (the browser must resize before
            any seed paints — FIFO delivery makes meta-then-seed the only safe
            order, #review-seed-meta-order), then attach the tee to the current
            target (detaching the previous one), and reseed when the SAME pane's
            geometry changed (the child's repaint at the new size already
            tee'd against the browser's old grid). Returns True when a seed was
            sent (attach or reseed) so _mirror_pane_reseed doesn't send a second
            identical one. Called from every layout-changing event, focus moves
            and the poll backstop — the app-side meta-key compare keeps the
            no-change hot path to one tuple compare. (#pane-direct)"""
            _hub = getattr(self, "_mirror_hub", None)
            if _hub is None:
                return False
            t = self._mirror_pane_target()
            cur = getattr(self, "_mirror_pane_term", None)
            # A per-target GENERATION rides the meta: the browser gates output
            # until the seed for the CURRENT generation arrives, so a seed lost
            # to a retarget/reopen (not just the very first connect) re-arms the
            # blank-view backstop instead of trusting the stale screen forever.
            # Bumped on every target change, BEFORE the meta is built. (#review-pane-gen)
            gen = getattr(self, "_mirror_pane_gen", 0)
            if t is not cur:
                gen += 1
                self._mirror_pane_gen = gen
            if t is None:
                meta = {"open": False, "gen": gen}
                key = (False, 0, 0, "", gen)
            else:
                scr = getattr(t, "_screen", None)
                meta = {
                    "open": True,
                    "cols": int(getattr(scr, "columns", 80) or 80),
                    "rows": int(getattr(scr, "lines", 24) or 24),
                    "title": str(getattr(t, "title", "") or "")[:120],
                    "gen": gen,
                }
                key = (True, meta["cols"], meta["rows"], meta["title"], gen)
            prev = getattr(self, "_mirror_pane_meta_key", None)
            if key != prev:
                self._mirror_pane_meta_key = key
                try:
                    _hub.set_pane_meta(meta)
                except Exception:
                    pass
            seeded = False
            if t is not cur:
                if cur is not None:
                    try:
                        cur.detach_mirror()
                    except Exception:
                        pass
                if t is not None:
                    synth = getattr(self, "_mirror_seed_synth", None)
                    if synth is None:
                        from saikai_mirror import _synth_pane_seed as synth
                        self._mirror_seed_synth = synth
                    try:
                        t.attach_mirror(_hub.pane_feed, _hub.pane_reset, synth)
                        seeded = True
                    except Exception:
                        t = None
                self._mirror_pane_term = t
            elif t is not None and prev is not None and key[:3] != prev[:3]:
                # same pane, new geometry (host resize / split-ratio change):
                # the reseed carries the meta and repaints the resized browser
                # grid. (#review-pane-resize)
                try:
                    t.mirror_reseed()
                    seeded = True
                except Exception:
                    pass
            return seeded

        def _mirror_pane_reseed(self) -> None:
            """Hub-requested full-state reseed (fresh pane client / fallen-behind
            client / ingest overflow). UI thread (the hub callback marshals)."""
            if self._mirror_sync_pane():
                return         # the sync itself just seeded — don't send a twin
            t = getattr(self, "_mirror_pane_term", None)
            if t is not None:
                try:
                    t.mirror_reseed()
                except Exception:
                    pass

        def _mirror_inject_raw(self, data: str) -> None:
            """Write pane-view browser bytes VERBATIM to the followed pane's
            child PTY — the browser xterm is that child's terminal, so arrows,
            mouse reports and bracketed paste arrive exactly as a local terminal
            would produce them. No Textual key translation, no focus routing:
            the bytes go to the pane the browser is LOOKING AT, regardless of
            local focus. Re-checks the authoritative control gate. (#pane-direct)"""
            if not self._control_enabled or not isinstance(data, str) or not data:
                return
            t = getattr(self, "_mirror_pane_term", None)
            if t is None or getattr(t, "is_dead", False) \
                    or getattr(t, "_pty", None) is None:
                # The followed pane closed/died (kill() nulls _pty immediately;
                # is_dead lags until the reader's _finalize) — refresh the
                # mirror's model NOW so meta flips to closed and a next pane can
                # attach, instead of dropping bytes silently until the poll
                # notices. (#review-dead-pane-window)
                self._mirror_sync_pane()
                return
            t.last_input_ts = time.monotonic()   # remote typing defers rebuilds too (#linux-state-regroup)
            try:
                t._send_to_child(data)
            except Exception:
                pass

        def _resync_mirror_target(self) -> None:
            """Keep the mirror CONTROL banner ('typing into: X') honest when focus
            moves or the focused pane closes/dies while control is ON — otherwise
            it keeps naming a pane that no longer has focus while remote keys land
            on whatever is focused now (the list, search, another pane). The hub
            method is a no-op when control is OFF / target unchanged and never
            re-arms the idle auto-disable."""
            _hub = getattr(self, "_mirror_hub", None)
            if _hub is None or not getattr(self, "_control_enabled", False):
                return
            t = self._focused_terminal()
            target = getattr(t, "title", None) if t is not None else None
            try:
                _hub.update_control_target(target)
            except Exception:
                pass

        def on_descendant_focus(self, event) -> None:
            # NOTE: this is the ONLY on_descendant_focus — a second definition in
            # this class would silently shadow it (a duplicate did exactly that
            # and turned the header-skip baseline below into dead code, so ↑ from
            # the row under a group header pushed the cursor back). (#audit-codex-dupfocus)
            #
            # Refresh the header-skip baseline whenever focus lands on the list.
            # Focus can return to the table WITHOUT a RowHighlighted event (Esc-back
            # / Ctrl+] from a pane, a pane close), which would otherwise leave
            # _last_cursor_row frozen at its pre-pane value and send the next
            # header-skip the wrong way. One central point covers every path. (#audit-header-skip)
            try:
                w = getattr(event, "widget", None)
                if getattr(w, "id", None) == "table":
                    self._last_cursor_row = w.cursor_row
            except Exception:
                pass
            # Always-on focus trail: focus moves aren't otherwise logged, so an
            # unexpected "focus changed on its own" had no record. Log each move
            # to saikai.log next to the pane/refresh events that triggered it
            # (e.g. a "[term] exit" immediately followed by "[focus] -> DataTable"
            # pinpoints a pane-exit stealing focus). Best-effort; deduped.
            try:
                w = getattr(event, "widget", None)
                cur = "?" if w is None else (
                    type(w).__name__ + (f"#{w.id}" if getattr(w, "id", None) else ""))
                prev = getattr(self, "_last_focus_log", "-")
                if cur != prev:
                    _log(f"[focus] {prev} -> {cur}")
                    self._last_focus_log = cur
            except Exception:
                pass
            # A focus change while the leader is half-armed means the next key
            # won't be the table's leader letter (focus moved to search / a pane /
            # a dropdown) — clear it so the key types normally instead of leaking
            # into a leader action. Priority bindings can move focus while
            # bypassing on_key, where the leader is normally resolved.
            try:
                if getattr(self, "_leader_pending", False):
                    if getattr(event, "widget", None) is not self.query_one("#table", DataTable):
                        self._leader_pending = False
                        self._leader_gen += 1          # kill the pending show/cancel timers
                        self._set_leader_hint(None)    # hide the panel NOW, so it can't linger
                                                       # over the footer until the cancel timeout
            except Exception:
                pass
            # Keep the mirror control banner's "typing into" target in sync with
            # the actual focus while control is ON (a stale target was a lie that
            # sent remote keys blind into a different widget).
            self._resync_mirror_target()
            # …and retarget the pane-direct channel IMMEDIATELY: leaving it to
            # the 1.5s poll let a phone's in-flight keystrokes (typed against
            # pane A's screen) land in newly-focused pane B — the reseed a
            # retarget sends also makes the switch visible on the phone at the
            # moment it happens. (#review-pane-target-divergence)
            self._mirror_sync_pane()
            # Catch up a list rebuild that _poll_live_status deferred while a pane
            # was focused (markers weren't refreshed under the user's typing). Now
            # that focus has left every pane, rebuilding is safe.
            try:
                if getattr(self, "_status_refresh_pending", False) and \
                        self._focused_terminal() is None:
                    self._status_refresh_pending = False
                    self._request_refresh()
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
            # If a checkpoint (b2) is mid-flight on THIS pane, tear it down first:
            # kill() below nulls the PTY but the reader sets is_dead only later
            # (async _finalize), so _b2_tick's is_dead guard wouldn't fire yet and
            # the machine would keep advancing against a corpse — injecting nothing
            # but still detecting a "child" + writing lineage for a pane you closed.
            _b2 = getattr(self, "_b2", None)
            if _b2 is not None and _b2.get("sid") == sid:
                self._b2_finish("checkpoint aborted — pane closed", "warning")
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
            # last. _live_pane_ids() is DOM order and includes dead x panes.
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
            # Immediate pane-channel retarget: the closed pane's tee detaches,
            # meta flips (closed / next pane), and remote input can't fall into
            # the pty-None window until the next poll. (#review-dead-pane-window)
            self._mirror_sync_pane()

        def _kill_agents(self, targets: list, label: str) -> None:
            """Confirm, then terminate each (pid, procStart, title) in `targets`.
            One confirm for the whole batch; each kill is identity-verified in
            _kill_agent_process. (#agent-kill #agent-lineage)"""
            if not targets:
                return
            head = targets[0]

            def _do(ok: bool):
                if not ok:
                    return
                done = gone = fail = 0
                for pid, ps, _t in targets:
                    res = _kill_agent_process(pid, ps)
                    _log(f"kill agent pid={pid}: {res}")
                    if res == "signalled":
                        done += 1
                    elif res in ("gone", "stale"):
                        gone += 1
                    else:
                        fail += 1
                if fail:
                    self.notify(f"{done} terminating, {fail} failed",
                                severity="error", title="saikai", timeout=6)
                elif done:
                    self.notify(f"terminating {done} agent(s)"
                                + (f", {gone} already ended" if gone else ""), timeout=5)
                else:
                    self.notify("agent(s) already ended", timeout=4)
                self.set_timer(2.2, self.action_refresh)

            # the modal names the batch (its title line = label, pid of the first)
            self.push_screen(KillAgentScreen(label, head[0]), _do)

        def action_kill_agent(self) -> None:
            """Shift+K: terminate a live agent process — the focused agent row's
            own process, OR (on a PARENT row) all of its live child agents at
            once ("manage them where they belong"). Refuses any row that is
            neither, so a stray K can never signal an interactive claude or a
            dormant transcript. Confirmed + identity-verified. (#agent-kill)"""
            if isinstance(self.focused, Select):
                raise SkipAction()
            # A focused live pane owns Shift+K as a literal capital "K": the K
            # binding is priority=True, so it preempts the pane's on_key; raise
            # SkipAction so Textual forwards the key to the AgentTerminal (its
            # on_key writes it to the PTY) instead of killing an agent while the
            # user is typing — mirrors action_resume's Enter guard. (#agent-kill)
            if self._focused_terminal() is not None:
                raise SkipAction()
            sid = self._cursor_sid()
            s = self._sid_index.get(sid) if sid else None
            if not s:
                return
            if s.get("is_bg"):
                # a single agent/bg row
                info = _active_procinfo().get(sid)
                if not info:
                    self.notify("agent already ended", timeout=4)
                    self.action_refresh()
                    return
                pid, procstart = info
                title = _pane_title(s, sid, None)
                self._kill_agents([(pid, procstart, title)], f"'{title}' (pid {pid})")
                return
            # a non-agent row: bulk-stop its live children, if any (#agent-lineage)
            kids = [(k, v) for k, v in self._sid_index.items()
                    if v.get("is_bg") and v.get("parent_session_id") == sid
                    and _active_procinfo().get(k)]
            if kids:
                targets = [(*_active_procinfo()[k], _pane_title(v, k, None))
                           for k, v in kids]
                self._kill_agents(
                    targets, f"{len(targets)} child agent(s) of "
                             f"'{_pane_title(s, sid, None)}'")
                return
            self.notify("K terminates a running AGENT (the & marker), or a "
                        "parent's child agents; this row is neither",
                        severity="information", title="saikai", timeout=5)

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
            # Abort any in-flight checkpoint FIRST: kill_all() nulls the PTYs but the
            # reader sets is_dead only later (async), so _b2_tick would keep advancing
            # against a corpse — detecting a "child" + writing lineage for a pane you
            # just closed. Mirrors the single-pane _close_live_sid guard. (#audit-b2-closeall)
            if getattr(self, "_b2", None) is not None:
                self._b2_finish("checkpoint aborted — all panes closed", "warning")
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
            # Bulk close must retarget the mirror's pane channel NOW, like the
            # single-close path — otherwise a pane viewer keeps seeing a killed
            # pane (meta open:true) until the next poll. (#review-closeall-sync)
            self._mirror_sync_pane()
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
                            # Never focus a DEAD x pane — it has no PTY, so keys
                            # would vanish into a corpse (and a stray printable
                            # would bubble to the list as type-to-search). Land on
                            # the list instead, same guard as action_toggle_list.
                            if t is not None and not getattr(t, "is_dead", False):
                                t.focus()
                            else:
                                self.query_one("#table", DataTable).focus()
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
                        term.focus()        # never focus a dead x pane (keys would vanish)
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
            # Mark "filtering" for a short window: the rebuild + its RowHighlighted
            # are queued (call_after_refresh), so by the time the highlight handler
            # runs the search box may have momentarily lost focus — _filter_is_engaged
            # reads this window so the foreground live pane isn't switched out under
            # the filter (see on_data_table_row_highlighted).
            if getattr(event.input, "id", None) == "search":
                try:
                    self.query_one("#search-clear").display = bool(event.value)
                except Exception:
                    pass
            self._filter_active_until = time.monotonic() + 0.5
            self._request_refresh()

        # ── actions ─────────────────────────────────────────────────────────

        def _bulk_or_single_toggle(self, path, on_verb, off_verb, what) -> None:
            """Favorite/hide scoped by _selected_or_cursor (marks → all marked,
            else the cursor row). Converging bulk-toggle, ONE read+write, ONE
            repaint; a count toast only when >1 row is affected so an off-screen
            marked row is never silently mutated. Single-row (no marks) keeps the
            original quiet flip. Marks clear only AFTER a successful write, so a
            mid-batch failure (the anti-erase guard raising) leaves the selection
            intact for a retry instead of dropping it with nothing done."""
            sids = self._selected_or_cursor()
            if not sids:
                return
            try:
                now_on = _bulk_toggle_in_set(path, sids)
            except Exception as e:
                self.notify(f"{what} skipped: {e}", severity="error", timeout=6)
                return
            bulk = len(sids) > 1
            if self._marked:
                self._marked.clear()
            if bulk:
                self.notify(f"{on_verb if now_on else off_verb} {len(sids)} sessions",
                            timeout=3)
            self._refresh_table()

        def action_toggle_hide(self) -> None:
            self._bulk_or_single_toggle(HIDDEN_FILE, "hid", "unhid", "hide")

        def action_toggle_fav(self) -> None:
            self._bulk_or_single_toggle(FAVORITE_FILE, "favorited", "unfavorited",
                                        "favorite")

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
                            tabs.get_tab(pane).label = Content(_LIVE_TERM.tab_label(title, self._live.status(sid)))  # Tab.label (not TabPane) updates the display (#tab-glyph-update)
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
            self._mirror_sync_geometry()  # hub dedups; tracks resize + layout (#mirror-resize)
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
                    _ts = getattr(term, "_status", "")
                    # Don't propagate the teardown "dead" sentinel (set by the
                    # reader's _finalize before the marshalled _on_live_exit gets
                    # to forget the sid): the list marker logic has no "dead"
                    # branch and would flash the dormant @/+/! file markers for a
                    # frame. Leave the last live status until the pane is forgotten.
                    if _ts and _ts != "dead":
                        self._live.set_status(term.sid, _ts)
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
                prev_st = prev.get(sid)
                if st != prev_st:
                    # The reader marshals _on_live_status once per transition, but
                    # when this poll is the only thing that noticed the flip (the
                    # reader stayed blocked in read() with no final chunk), that
                    # marshal no-ops on our own thread — so replay the
                    # _unread/_busy_seen/tab-glyph bookkeeping here, else the "!"
                    # reply-due marker, the !M badge, and the tab glyph never
                    # update for a backgrounded pane that just finished its turn.
                    self._apply_live_status(sid, st)
                if self._live.pane_id(sid) == active:
                    # You're looking at this pane — its tab/marker suffice; if it just
                    # settled, drop the "done" debt so switching away later doesn't
                    # toast a finish you already watched.
                    if st != "busy":
                        self._busy_seen.discard(sid)
                    continue
                sess = self._sid_index.get(sid) or {}
                title = (sess.get("ai_title") or _first_msg(sess) or sid[:8])[:50]
                if st == "waiting" and prev_st != "waiting":
                    # Alert only for a pane that actually DID work and now needs you
                    # (it was busy at some point → in _busy_seen). A freshly-opened
                    # pane's "trust this folder?" gate also classifies as waiting,
                    # but you just created that prompt by opening the session — it
                    # was never busy, so it stays silent instead of toasting+belling
                    # a burst when you batch-open several sessions. (#4)
                    if sid in self._busy_seen:
                        self.notify(f"needs input: {title}", title="saikai", timeout=8)
                        # Audible nudge so a backgrounded session needing input is
                        # noticed even when you're not watching the screen. Fires
                        # once per transition (guarded above); silent if the terminal
                        # bell is off. SAIKAI_NO_BELL=1 opts out.
                        if not os.environ.get("SAIKAI_NO_BELL"):
                            try:
                                self.bell()
                            except Exception:
                                pass
                    # You're now engaged with this prompt; drop the busy debt so a
                    # later waiting→idle (you answered) can't fire a spurious "done"
                    # toast for a turn you already saw prompt and resolve. (#7)
                    self._busy_seen.discard(sid)
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
                    # NOT `or 85.0`: a configured max_load of 0 is a real value the
                    # gate honours (blocks every pane); the falsy-fallback would show
                    # green/ok and fire the toast at 85% instead. (#audit-maxload)
                    _maxl = float(_ram_gate_kwargs().get("max_load", _DEFAULT_MAX_LOAD))
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
                # Don't rebuild the list while the user is TYPING into a live pane:
                # _do_refresh_table's table.clear()+rebuild disrupts mid-typing
                # (keystrokes race the rebuild). Typing — not focus — is the test:
                # focus parked in a pane while watching the list must still see
                # finished sessions leave "Running" (#linux-state-regroup). Deferred
                # work is caught up by on_descendant_focus or the next poll tick.
                if self._pane_typing_recently():
                    self._status_refresh_pending = True
                else:
                    self._request_refresh()
            elif getattr(self, "_status_refresh_pending", False) \
                    and not self._pane_typing_recently():
                # typing stopped with a deferred rebuild outstanding and no new
                # transition to re-trigger it — catch up on the poll cadence, not
                # only when focus leaves the pane (#linux-state-regroup)
                self._status_refresh_pending = False
                self._request_refresh()

        def action_copy_summary(self) -> None:
            """Leader ␣i — copy the cursor session's preview/summary text to the
            host clipboard (the preview pane can't be mouse-selected in a TUI)."""
            sid = self._cursor_sid()
            if not sid:
                return
            s = self._sid_index.get(sid)
            text = _render_preview(s) if s else ""
            if not (text or "").strip():
                self.notify("no preview text to copy", timeout=3)
                return
            if _copy_host_or_osc52(text, self):
                self.notify("preview copied to clipboard", timeout=3)
            else:
                self.notify("could not copy the preview", severity="warning", timeout=3)

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

        def action_copy_response(self) -> None:
            """F1: open vi-style copy mode over the session transcript for the
            focused live pane (else the list cursor). Navigate with j/k/g/G, select
            with v, yank with y — so you can copy ANY of claude's output, including
            replies that scrolled off the alt-screen pane (which can't be selected).
            (#copy-mode)"""
            w = getattr(self, "focused", None)
            sid = getattr(w, "sid", None) or self._cursor_sid()
            if not sid:
                return
            s = self._sid_index.get(sid)
            path = (s or {}).get("jsonl_path") or _find_session_jsonl(sid)
            turns = _session_turns(path) if path else []
            if not turns:
                self.notify("no transcript messages to copy yet", timeout=3)
                return
            self.push_screen(CopyModeScreen(_flatten_turns(turns)))

        def _apply_fresh_sessions(self, fresh, force: bool = False) -> None:
            nonlocal all_sessions
            # A re-scan that suddenly finds ZERO sessions while we currently HAVE
            # some is almost always transient (a glob race, a momentarily
            # unreadable projects dir, a project-resolution hiccup) — NOT the user
            # deleting everything. Refuse to clobber a populated list with an empty
            # scan; that was the "all sessions suddenly vanished" bug. `force`
            # (explicit double-F5) overrides for a genuinely-empty store. (#audit-f5-empty)
            if not fresh and all_sessions and not force:
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
            # Drop marks for sessions that fell out of the fresh scan (expiry /
            # delete / window change). A dangling sid left in _marked would
            # otherwise be acted on by the next bulk verb against a row the user
            # can no longer see. Live-pane sids were re-appended above, so this
            # keeps them. (#bulk-marks-prune)
            if getattr(self, "_marked", None):
                self._marked &= set(self._sid_index)

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
            if reload_fn is None or self._pane_typing_recently():
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

        def _sessions_dirs_mtime(self) -> float:
            """Cheap change signal for auto-refresh — the newest mtime across three
            layers, because a DIRECTORY mtime only bumps on entry add/remove, NOT
            on a write to a file inside it:
              1. the live registry dir (a new interactive session registers a pid);
              2. every project SUBDIR (a NEW session file bumps its dir — the root
                 does not, so watching only the root missed new sessions in an
                 existing project); and
              3. the KNOWN transcript files (a new TURN — a session flipping to
                 'needs input' — appends CONTENT, which bumps no directory at all,
                 so watching dirs alone left the '!' attention marker frozen until
                 F5: the core value-prop silently stale). Stats known paths from
                 _sid_index, never a glob/walk. (#audit-attention-freshness)"""
            import os as _os
            m = 0.0
            try:
                m = max(m, (CLAUDE_CONFIG_ROOT / "sessions").stat().st_mtime)
            except OSError:
                pass
            try:
                for d in PROJECTS_ROOT.iterdir():   # one readdir of the root
                    try:
                        st = d.stat()
                        if _stat.S_ISDIR(st.st_mode):
                            m = max(m, st.st_mtime)
                    except OSError:
                        pass
            except OSError:
                pass
            for s in list(getattr(self, "_sid_index", {}).values()):
                p = s.get("jsonl_path")
                if not p:
                    continue
                try:
                    m = max(m, _os.stat(p).st_mtime)   # transcript GROWTH (no glob)
                except OSError:
                    pass
            return m

        def _rescan_if_changed(self) -> None:
            """Default-on auto-refresh tick: kick the OFF-thread rescan only when the
            registry/projects dirs changed since the last scan, so a session started
            elsewhere appears within ~2s without a constant disk walk or needless
            rebuild. _auto_tick guards focus (skip while typing in a pane) + overlap;
            _do_refresh_table preserves the cursor by sid. (#recon-autorefresh)"""
            try:
                m = self._sessions_dirs_mtime()
            except Exception:
                return
            if m > getattr(self, "_sessions_mtime", 0.0):
                self._sessions_mtime = m
                self._auto_tick()

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
            # Escape hatch for a GENUINELY empty store: the guard refuses to clobber
            # a populated list with a (usually transient) empty scan, but a second
            # F5 within the arm window forces it through. (#audit-f5-empty)
            if not fresh and all_sessions:
                if getattr(self, "_empty_reload_armed", False):
                    self._empty_reload_armed = False
                    self._apply_fresh_sessions(fresh, force=True)
                    self._refresh_table()
                    self.notify("refreshed — 0 sessions (forced)", timeout=3)
                    return
                self._empty_reload_armed = True
                self.notify("re-scan returned 0 sessions — kept the list. "
                            "Press F5 again to confirm it's really empty.",
                            severity="warning", timeout=6)
                return
            self._empty_reload_armed = False
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

        def _confirm_quit(self) -> bool:
            """Double-press guard for app exit. The first Esc/Ctrl+C arms this and
            shows a hint; a second press within the window returns True (proceed to
            quit). Any other key disarms it (see on_key). This mirrors Claude
            Code's "press Ctrl+C again to exit": a single reflex Esc/Ctrl+C — the
            muscle memory for interrupting claude in a pane — must not kill saikai."""
            if getattr(self, "_quit_armed", False):
                self._quit_armed = False          # consume the arm
                old = getattr(self, "_quit_disarm_timer", None)
                if old is not None:               # cancel the pending disarm timer
                    try:                          # so it can't fire during shutdown
                        old.stop()
                    except Exception:
                        pass
                return True
            self._quit_armed = True
            try:
                self.notify("Press Esc or Ctrl+C again to quit",
                            severity="warning", timeout=2.5)
            except Exception:
                pass
            old = getattr(self, "_quit_disarm_timer", None)
            if old is not None:
                try:
                    old.stop()
                except Exception:
                    pass
            try:
                self._quit_disarm_timer = self.set_timer(2.5, self._disarm_quit)
            except Exception:
                self._quit_disarm_timer = None
            return False

        def _disarm_quit(self) -> None:
            # Belt-and-suspenders: the timer is cancelled on quit and on re-arm,
            # but if it still fires during teardown, don't touch a stopped app.
            if not self.is_running:
                return
            self._quit_armed = False

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
            # A modal / pushed screen owns the keyboard: Esc is consumed by the
            # modal's own escape binding, but Ctrl+Q (a priority binding) routes
            # here regardless — never quit from under a dialog.
            if len(self.screen_stack) > 1:
                return
            if isinstance(self.focused, Input):
                # Esc in the search box CLEARS an active filter first (a non-empty
                # query — especially with the bar hidden — reads as "sessions
                # missing"), then returns to the list. Empty box -> just focus it.
                inp = self.focused
                if getattr(inp, "value", ""):
                    inp.value = ""    # fires Input.Changed -> on_input_changed -> unfiltered refresh
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
            # A focused terminal → Esc returns to the list. _focused_terminal()
            # only counts LIVE panes; a focused DEAD (x) pane bubbles Esc here too
            # (its on_key lets keys through with no PTY), so match the widget type
            # directly — otherwise Esc on a corpse falls through to the quit prompt
            # instead of releasing to the list.
            if self._focused_terminal() is not None or (
                    _LIVE_TERM is not None
                    and isinstance(self.focused, _LIVE_TERM.AgentTerminal)):
                self.query_one("#table", DataTable).focus()
                return
            # Bare list → quit, but only on a DELIBERATE second Esc. A single Esc
            # is too easy to fire by reflex (it interrupts claude in a focused
            # pane), so the first one only arms + hints (see _confirm_quit).
            if not self._confirm_quit():
                return
            if self._live is not None and self._live.count > 0:
                self.action_quit_all()
                return
            if self._live is not None:
                self._live.join_reaps()   # join any reaps from earlier F10 closes
            _log("quit: Esc (confirmed, no live panes)")
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
            # kill_all below fires _on_live_exit for EVERY pane as it dies; that
            # handler discards the sid from _opened_sids and re-saves — which would
            # erode the snapshot we just wrote down to [] (the "saved nothing on
            # quit" bug). Flag the teardown so those exit callbacks skip the
            # snapshot mutation and the just-saved restore set survives. (#restore-erosion)
            self._quitting = True
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

        def _after_launch_qr(self, _result=None) -> None:
            # The launch QR screen is pushed ON TOP of the "Shift+F4 to reopen"
            # toast, hiding it; re-show that hint once the QR is dismissed so the
            # restore reminder is actually seen.
            if getattr(self, "_restore_candidates", None):
                self.notify(f"{len(self._restore_candidates)} pane(s) from last "
                            f"session — Shift+F4 to reopen", timeout=8)

        def action_mirror_info(self, on_close=None) -> None:
            # F12 — (re)show the web-mirror QR + URL. No-op when the mirror is off.
            # on_close: optional callback run when the QR is dismissed — the launch
            # auto-show passes it to re-surface the Shift+F4 restore hint the QR hid.
            _hub = getattr(self, "_mirror_hub", None)
            if _hub is None:
                return
            _url = _hub.url()
            # Copy on EVERY open (not just at startup) so F12 reliably puts the
            # tokened URL on the clipboard; tell the truth if the copy failed.
            # OSC-52 fallback so a headless/SSH host without xclip can still copy.
            _copied = _copy_host_or_osc52(_url, self)
            try:
                import saikai_mirror as _m
                self.push_screen(MirrorScreen(_url, _m.qr_matrix(_url), _copied,
                                              _hub.client_count()), on_close)
            except Exception as _qr_err:
                # Surface WHY the QR couldn't render (was silently swallowed): a
                # missing `segno` in the running install is the usual cause — the
                # mirror itself runs without it, only the QR needs it. (#mirror-qr)
                self.notify(f"Web mirror: {_url}\n(QR unavailable: {_qr_err!r})",
                            title="saikai mirror", severity="warning", timeout=15)

        def action_toggle_mirror_control(self) -> None:
            """Shift+F12 — flip web-mirror interactive control (default OFF). The
            app's _control_enabled is the authority; push the new state + the
            focused-pane title (read HERE on the UI thread) into the hub, which
            keeps an advisory copy and broadcasts a control frame. No-op when the
            mirror is off."""
            _hub = getattr(self, "_mirror_hub", None)
            if _hub is None:
                return
            desired = not self._control_enabled
            # Reset the input parser on every toggle so a partial escape buffered in a
            # PRIOR control session can't survive an OFF→ON and complete against the
            # first key of the new session as a phantom. (#H9)
            self._mirror_parser = None
            # Designate a control target only while enabling; disabling clears it
            # (matches the hub's own `target if enabled else None` normalisation).
            t = self._focused_terminal() if desired else None
            target = (getattr(t, "title", None) if t is not None else None)
            # The hub may CLAMP enable→off on a LAN bind without SAIKAI_MIRROR_ALLOW_LAN_INPUT.
            # Trust its returned effective state as authority so the TUI never claims
            # control is ON while every /input POST is actually being 409'd.
            try:
                effective = _hub.set_control_state(desired, target)
            except Exception:
                effective = desired
            self._control_enabled = bool(effective)
            if self._control_enabled:
                msg = (f"Mirror control ON — typing into: {target}" if target
                       else "Mirror control ON — no pane focused")
                self.notify(msg, title="saikai mirror", severity="warning",
                            timeout=6)
            elif desired and not self._control_enabled:
                # Requested ON but the hub refused it (LAN bind, input not opted in).
                self.notify("Mirror control stays OFF — LAN input needs "
                            f"{_MIRROR_LAN_INPUT_ENV}=1",
                            title="saikai mirror", severity="warning", timeout=7)
            else:
                self.notify("Mirror control OFF (read-only)",
                            title="saikai mirror", timeout=4)

        def action_open_parent(self) -> None:
            """Shift+F6 — jump to the session this one was forked/cleared from
            (lineage recovery). No-op + toast when there is no recorded parent
            or the parent is not in the current session index."""
            sid = self._cursor_sid()
            rec = _load_lineage().get(sid or "")
            parent = rec.get("parent") if rec else None
            if not parent or parent not in self._sid_index:
                self.notify("no parent session recorded", timeout=3)
                return
            try:
                table = self.query_one("#table", DataTable)
                row = table.get_row_index(parent)
                table.move_cursor(row=row)
            except Exception:
                self.notify("could not open parent", severity="error", timeout=4)

        def _ctx_target_pane(self):
            """The live pane a context action (b1 /compact, b2 checkpoint) targets:
            the FOCUSED pane if one is focused, else — when armed from the LIST via
            the leader — the cursor-selected session if it is live. Returns the
            AgentTerminal or None. Shared by b1 and b2."""
            t = self._focused_terminal()
            if t is not None:
                return t
            # Leader path: the list owns focus, so resolve the cursor row's live pane.
            sid = self._cursor_sid()
            if sid and self._live is not None and self._live.has(sid):
                return self._live.get(sid)
            return None

        def _pane_is_midturn(self, sid) -> bool:
            """True if the pane is busy/waiting (a running turn or a pending
            permission prompt) — context actions must not interrupt it."""
            status = self._live.statuses().get(sid) if self._live else None
            return status in ("busy", "waiting")

        def action_context_refresh(self) -> None:
            """Shift+F11 — inject /compact into the focused live pane to summarise
            the context in place (non-destructive). No-op + toast when no pane is
            focused or the pane is mid-turn (busy/waiting — don't interrupt a
            running turn or a pending permission prompt).

            Injection is HARDENED like b2's (#audit-b2-reseed-cr): paste, settle
            ~0.6s (the leading '/' opens the slash palette; a too-early CR can be
            absorbed), CR, then VERIFY the pane actually went busy — resending
            the CR while it hasn't. The old paste+CR-in-one-tick fired a success
            toast with no evidence the command ran. (#audit-b1-verify)"""
            t = self._focused_terminal()
            if t is None:
                self.notify("focus a live pane to refresh its context", timeout=3)
                return
            sid = getattr(t, "sid", None)
            if self._pane_is_midturn(sid):
                self.notify("pane is busy — refresh when it's idle",
                            severity="warning", timeout=3)
                return
            if getattr(self, "_b1", None) is not None:
                self.notify("a /compact is already in flight", timeout=3)
                return
            b2 = getattr(self, "_b2", None)
            if b2 is not None and b2.get("sid") == sid:
                self.notify("a checkpoint is running on this pane",
                            severity="warning", timeout=3)
                return
            try:
                _kill = getattr(t, "kill_input_line", None)
                if _kill is not None:
                    _kill()                # leftover draft would concatenate (#audit-b2-draft)
                t.paste_text("/compact")
            except Exception:
                self.notify("could not inject /compact", severity="error", timeout=4)
                return
            self._b1 = {"term": t, "sid": sid, "ticks": 0,
                        "submitted_at": None, "crs": 0}
            self._b1_timer = self.set_interval(0.3, self._b1_tick)
            self.notify("sending /compact…", timeout=3)

        def _b1_finish(self, msg=None, severity="information") -> None:
            """Tear down the b1 /compact verifier: stop the interval, drop state,
            optional toast. Idempotent (same contract as _b2_finish)."""
            tm = getattr(self, "_b1_timer", None)
            if tm is not None:
                try:
                    tm.stop()
                except Exception:
                    pass
                self._b1_timer = None
            self._b1 = None
            if msg:
                try:
                    _log(f"[b1] finish [{severity}]: {msg}")
                except Exception:
                    pass
                self.notify(msg, severity=severity, timeout=5)

        def _b1_tick(self) -> None:
            """Advance the b1 /compact injection: settle → CR → verify busy,
            resending the CR while the pane stays idle (absorbed-CR hardening,
            same measurements as b2's verify_reseed)."""
            b1 = getattr(self, "_b1", None)
            if not b1:
                return
            term = b1["term"]
            if term is None or getattr(term, "is_dead", False):
                self._b1_finish("/compact aborted — pane closed", "warning")
                return
            b1["ticks"] += 1
            if b1["submitted_at"] is None:
                if b1["ticks"] >= self._B2_CLEAR_SETTLE_TICKS:
                    try:
                        term.submit()
                    except Exception:
                        self._b1_finish("could not submit /compact", "error")
                        return
                    b1["submitted_at"] = b1["ticks"]
                return
            since = b1["ticks"] - b1["submitted_at"]
            try:
                term.refresh_status()
                _ts = getattr(term, "_status", "")
                self._live.set_status(b1["sid"], _ts)
                self._apply_live_status(b1["sid"], _ts)
            except Exception:
                pass
            status = (self._live.statuses().get(b1["sid"])
                      if self._live else None)
            if status in ("busy", "waiting"):
                self._b1_finish("sent /compact — compacting this session in place")
                return
            if since >= self._B2_RESEED_VERIFY_TICKS:
                self._b1_finish("/compact may not have submitted — check the pane "
                                "(press Enter there if it still shows /compact)",
                                "warning")
                return
            if since % 7 == 0:
                try:
                    term.submit()
                except Exception:
                    pass
                b1["crs"] = b1.get("crs", 0) + 1
                try:
                    _log(f"[b1] compact CR resent (n={b1['crs']})")
                except Exception:
                    pass

        # ── b2 (Task 11): human-gated checkpoint → /handoff → confirm → /clear →
        # reseed → lineage. Implemented as a TICK STATE MACHINE (off a
        # self-cancelling set_interval) — never a blocking UI-thread wait, which
        # would freeze every pane (the reader threads marshal onto this thread).
        # Triggered by leader ␣c (a distinct gesture from b1's plain Shift+F11,
        # which stays /compact-only). The destructive /clear is gated behind the
        # ConfirmRefreshScreen — it is sent ONLY after the user presses Enter.
        _B2_DETECT_TICKS = 34          # ~10s at 0.3s/tick: child sid visible in 2.5-4s
        _B2_CHILD_CONFIRM_TICKS = 3    # a single child candidate must persist this many
                                       # consecutive ticks (~0.9s) before binding, so a
                                       # contaminant landing BEFORE the real child can't
                                       # be mis-bound (the real child arriving later then
                                       # makes it ≥2 → ambiguous → reset). (#audit-b2-toctou)
        _B2_SETTLE_TICKS = 2           # let a just-injected turn register as busy
        _B2_CLEAR_SETTLE_TICKS = 2     # ~0.6s between /clear paste and its CR (spike #5)
        _B2_RESEED_VERIFY_TICKS = 34   # ~10s for the reseed turn to visibly START
                                       # (busy); while idle the CR is resent every
                                       # ~2s — claude's post-/clear re-init absorbs
                                       # a too-early CR (measured on v2.1.198, worse
                                       # the bigger the cleared session). (#audit-b2-reseed-cr)
        _B2_HANDOFF_IDLE_TICKS = 1000  # ~5min ceiling for the handoff turn to finish
                                       # (b2's use case is a GROWN session, where
                                       # summarising into a NEW SESSION PROMPT — with
                                       # thinking — easily exceeds a minute; only a
                                       # true hang should hit this, and a dead pane
                                       # is caught separately)
        _B2_HANDOFF_START_TICKS = 24   # ~7s grace for the turn to FLIP to busy before
                                       # we conclude "idle => done" (a grown session
                                       # can take seconds to spin up; concluding too
                                       # early would extract the PRE-handoff text)

        def action_checkpoint(self) -> None:
            """Leader ␣c — start the human-gated checkpoint flow on the target live
            pane (focused, or the cursor-selected session when armed from the list).
            Injects /handoff, waits for it to settle, shows the extracted NEW
            SESSION PROMPT for confirmation, then (only on confirm) /clear + reseed
            and records child→parent lineage. No-op + toast when there is no live
            target, the pane is mid-turn, or a checkpoint is already running."""
            if getattr(self, "_b2", None) is not None:
                self.notify("a checkpoint is already in progress", timeout=3)
                return
            t = self._ctx_target_pane()
            if t is None:
                self.notify("select or focus a live pane to checkpoint", timeout=3)
                return
            sid = getattr(t, "sid", None)
            if self._pane_is_midturn(sid):
                self.notify("pane is busy — checkpoint when it's idle",
                            severity="warning", timeout=3)
                return
            s = self._sid_index.get(sid) or {}
            jp = s.get("jsonl_path")
            if not jp:
                self.notify("no transcript for this pane yet", timeout=3)
                return
            from pathlib import Path as _P
            jp = _P(jp)
            try:
                _pre_size = jp.stat().st_size
            except OSError:
                _pre_size = 0
            self._b2 = {
                "state": "inject_handoff",
                "sid": sid,                  # parent sid (pre-/clear)
                "term": t,
                "parent_jsonl": str(jp),
                "project_dir": str(jp.parent),
                # Transcript size BEFORE the handoff inject: extract_prompt requires
                # GROWTH past this, so a silently-failed inject (absorbed CR) can
                # never extract a STALE block from a PREVIOUS handoff still sitting
                # at the tail — a wrong-but-plausible prompt in the confirm modal
                # was the worst failure shape of all. (#audit-b2-freshness)
                "pre_size": _pre_size,
                # cwd-LAST (current), not origin_cwd (first): the /clear child is
                # minted in the pane's CURRENT dir, so after a mid-session worktree/
                # branch cd, origin_cwd != current and the child's first cwd would
                # fail _bind_cleared_child's equality filter → real child dropped. (#audit-b2-panecwd)
                "pane_cwd": s.get("cwd") or s.get("origin_cwd") or "",
                "prompt": None,
                "pre_sids": set(),
                "child": None,
                "clear_ts": None,
                "ticks": 0,                  # generic per-state countdown/counter
                "resent_cr": False,
            }
            # Drive the machine off a self-cancelling interval (cancelled in
            # _b2_finish). 0.3s matches the spike's settle granularity.
            self._b2_timer = self.set_interval(0.3, self._b2_tick)
            self._b2_log(f"start sid={sid} jsonl={jp.name} pre_size={_pre_size}")
            self.notify("checkpoint: drafting the handoff…", timeout=4)
            self._b2_mark_dirty()      # show the ↻ checkpoint marker (focus-safe)

        def _b2_log(self, msg: str) -> None:
            """Trace the b2 machine to saikai.log. The audit of 'checkpoint feels
            broken' had ZERO on-disk evidence to work from — every failure was a
            vanished toast — so every transition/abort/resend logs. (#audit-b2-log)"""
            try:
                _log(f"[b2] {msg}")
            except Exception:
                pass

        def _b2_save_prompt(self, prompt) -> "str | None":
            """Persist a confirmed-but-unusable reseed prompt so an aborted
            checkpoint never DISCARDS what the user already vetted (it exists in
            the parent transcript, but digging it out mid-failure is hostile).
            Returns the path (str) for the toast, or None."""
            if not prompt:
                return None
            try:
                p = CACHE_DIR / "checkpoint-reseed-prompt.md"
                _write_text_atomic(p, str(prompt))
                return str(p)
            except Exception:
                return None

        def _b2_mark_dirty(self) -> None:
            """Repaint the list for the ↻ checkpoint marker WITHOUT disrupting a
            focused live pane. _do_refresh_table's clear()+rebuild leaks keystrokes
            into the list/search if a pane is focused — so defer exactly like
            _poll_live_status does (on_descendant_focus catches it up when focus
            leaves the pane); otherwise repaint via the coalesced path."""
            if self._focused_terminal() is not None:
                self._status_refresh_pending = True
            else:
                self._request_refresh()

        def _b2_finish(self, msg=None, severity="information") -> None:
            """Tear down the b2 machine: stop the interval, drop state, optional
            toast. Idempotent."""
            tm = getattr(self, "_b2_timer", None)
            if tm is not None:
                try:
                    tm.stop()
                except Exception:
                    pass
                self._b2_timer = None
            had_state = self._b2 is not None
            self._b2 = None
            # Dismiss the confirm modal if we're tearing down while it's still up
            # (pane died / closed / close-all while parked in awaiting_confirm) —
            # otherwise it orphans: its _resume early-returns on the cleared state,
            # so Ctrl+S/Esc become no-ops and the modal is stuck. (The normal
            # Esc/Ctrl+S path already dismissed it before calling here, so the
            # isinstance check makes this a no-op then — no double-pop.) (#audit-b2-modal)
            try:
                if isinstance(self.screen, ConfirmRefreshScreen):
                    self.pop_screen()
            except Exception:
                pass
            if had_state:
                # Clear the ↻ checkpoint marker. _b2_mark_dirty defers the repaint
                # while a live pane is focused (a CONTINUOUS poll rebuild during
                # typing flickers), and that deferred flag only drains when focus
                # leaves every pane — so when the checkpoint was run from its own
                # pane and focus stays there (the common case), the ↻ lingered in
                # the list. b2-finish is a DISCRETE event, so when the repaint got
                # deferred, force it with focus saved + restored: the refocus is
                # synchronous in this handler (no queued keystroke can land on the
                # list first) and _refresh_table preserves the cursor/scroll.
                foc = self.focused
                self._b2_mark_dirty()
                if getattr(self, "_status_refresh_pending", False):
                    self._status_refresh_pending = False
                    try:
                        self._refresh_table()
                        if foc is not None and foc is not self.focused:
                            foc.focus()
                    except Exception:
                        self._status_refresh_pending = True
            if msg:
                if had_state:
                    self._b2_log(f"finish [{severity}]: {msg}")
                self.notify(msg, severity=severity, timeout=5)

        def _b2_tick(self) -> None:
            """Advance the b2 machine at most one step. UI-thread only (set_interval
            fires here; the test calls it directly). Each branch either advances
            `state`, parks (waiting on the modal), or finishes."""
            b2 = getattr(self, "_b2", None)
            if not b2:
                return
            term = b2.get("term")
            # The pane died under us (claude exited) — abort, never inject into a corpse.
            if term is None or getattr(term, "is_dead", False):
                self._b2_finish("checkpoint aborted — pane closed", "warning")
                return
            st = b2.get("state")

            if st == "inject_handoff":
                # Inject saikai's OWN handoff prompt (a plain prompt, not the
                # personal `/handoff` skill) so the session summarises itself into a
                # paste-ready NEW SESSION PROMPT block — works without any skill.
                # Overridable via SAIKAI_HANDOFF_PROMPT_FILE; a rejected override
                # toasts and falls back to the built-in default. The prompt is
                # MULTI-LINE: paste_text wraps it in bracketed paste so the embedded
                # newlines don't submit line-by-line. That relies on the pane having
                # ?2004h on — guaranteed here because b2 only injects when the pane
                # is idle (action_checkpoint's _pane_is_midturn gate), i.e. claude is
                # at its prompt with bracketed paste enabled.
                _hp, _hp_warn = _resolve_handoff_prompt()
                if _hp_warn:
                    self.notify(_hp_warn, severity="warning", timeout=6)
                try:
                    # Clear any leftover draft first: pasted text CONCATENATES with
                    # whatever sits in claude's input box (idle ≠ empty), and a
                    # "draft<handoff>" submits as one garbled message. (#audit-b2-draft)
                    _kill = getattr(term, "kill_input_line", None)
                    if _kill is not None:
                        _kill()
                    term.paste_text(_hp)
                    term.submit()
                except Exception:
                    self._b2_finish("checkpoint aborted — could not inject the handoff prompt",
                                    "error")
                    return
                self._b2_log("handoff injected")
                b2["ticks"] = self._B2_SETTLE_TICKS
                b2["state"] = "await_handoff_idle"
                return

            if st == "await_handoff_idle":
                # Wait for the handoff turn to run and settle back to idle. idle_wait
                # counts every tick here; the ceiling (_B2_HANDOFF_IDLE_TICKS) bounds
                # the total wait -> abort. A dead pane is caught at the top of _b2_tick.
                # We require actually SEEING the pane go busy/waiting (seen_busy) — or
                # a short start grace — before accepting "idle" as "done" (below).
                if b2["ticks"] > 0:
                    b2["ticks"] -= 1
                    return
                b2["idle_wait"] = b2.get("idle_wait", 0) + 1
                if b2["idle_wait"] > self._B2_HANDOFF_IDLE_TICKS:
                    self._b2_finish("checkpoint aborted — the handoff didn't finish in time",
                                    "warning")
                    return
                # Keep the TARGET's status fresh from ITS OWN screen each tick rather
                # than depending on the 1.5s poll (which is deferred while another pane
                # is focused). This makes the wait focus-INDEPENDENT: switching away
                # from the checkpointed session never stalls or "times out" the
                # machine — it tracks b2["sid"], not whatever is focused now.
                if term is not None:
                    try:
                        term.refresh_status()
                        _ts = getattr(term, "_status", "")
                        self._live.set_status(b2["sid"], _ts)
                        # Route through _apply_live_status (not a raw set_status) so the
                        # _unread / _busy_seen bookkeeping stays consistent while the
                        # handoff turn drives the parent busy→idle — else the parent is
                        # left busy+in_unread, a state the normal flow never produces and
                        # which mis-counts in Shift+F3 / the !M badge. (#audit-b2-setstatus)
                        self._apply_live_status(b2["sid"], _ts)
                    except Exception:
                        pass
                status = (self._live.statuses().get(b2["sid"])
                          if self._live else None)
                if status in ("busy", "waiting"):
                    b2["seen_busy"] = True
                    return                    # still working — keep waiting
                # idle: only conclude the turn is DONE once we've actually seen it
                # run, or after a start grace — otherwise a grown session that
                # hasn't spun up yet looks "done" and we'd read the PRE-handoff text.
                if not b2.get("seen_busy") and b2["idle_wait"] < self._B2_HANDOFF_START_TICKS:
                    return
                b2["state"] = "extract_prompt"
                return

            if st == "extract_prompt":
                # Freshness gate: the handoff turn must have APPENDED to the parent
                # transcript. Without it, an inject whose CR was silently absorbed
                # (pane idle throughout) extracts a STALE block from a PREVIOUS
                # handoff at the tail — and the confirm modal shows a plausible but
                # wrong prompt. Growth may lag the idle flip (writer flush), so
                # retry a few ticks before concluding. (#audit-b2-freshness)
                try:
                    cur_size = Path(b2["parent_jsonl"]).stat().st_size
                except OSError:
                    cur_size = -1
                fresh = cur_size > b2.get("pre_size", 0)
                txt = _last_assistant_text_from_jsonl(b2["parent_jsonl"]) if fresh else None
                prompt = _extract_handoff_prompt(txt) if fresh else None
                if not prompt:
                    # Retry window: covers both flush lag (file about to grow /
                    # final record mid-write) and the extractor briefly seeing a
                    # truncated tail. ~3s at the 0.3s tick. (#audit-b2-extract-retry)
                    b2["extract_wait"] = b2.get("extract_wait", 0) + 1
                    if b2["extract_wait"] <= 10:
                        return
                    self._b2_finish(
                        "checkpoint aborted — no NEW SESSION PROMPT in the handoff"
                        if fresh else
                        "checkpoint aborted — the handoff never reached the "
                        "transcript (nothing new was written)",
                        "warning")
                    return
                self._b2_log(f"prompt extracted ({len(prompt)} chars)")
                b2["prompt"] = prompt
                b2["state"] = "confirm"
                return

            if st == "confirm":
                # Park the machine and push the human gate. The callback resumes
                # it (proceed) or finishes it (cancel). Guard so we push once.
                b2["state"] = "awaiting_confirm"

                def _resume(result, _self=self):
                    cur = getattr(_self, "_b2", None)
                    if not cur or cur.get("state") != "awaiting_confirm":
                        return                # machine torn down meanwhile
                    if result is not None:    # Ctrl+S → proceed with the (edited) prompt
                        if str(result).strip():
                            cur["prompt"] = result   # reseed with the edited text
                        cur["state"] = "inject_clear"
                    else:                     # Esc → cancel, session untouched
                        _self._b2_finish("checkpoint cancelled — session untouched")

                self.push_screen(ConfirmRefreshScreen(b2["prompt"]), _resume)
                return

            if st == "awaiting_confirm":
                return                        # waiting on the modal; ticks no-op

            if st == "inject_clear":
                # Two sub-phases within this one state, gated by b2["_clear_pasted"]:
                #   phase 1 (key absent): snapshot sids + stamp clear_ts + paste
                #                         "/clear", arm a settle countdown, return;
                #   phase 2 (key set):    after the settle ticks, submit the CR and
                #                         advance to detect_child.
                # Snapshot the project dir's sids BEFORE /clear so the post-clear
                # diff is falsifiable (spike #6). Record the clear timestamp in
                # UTC ('Z') so it is directly comparable to the transcript's UTC
                # timestamps (a naive-local value mis-ordered the child on
                # +UTC-offset hosts — see _parse_iso_aware / _bind_cleared_child).
                if not b2.get("_clear_pasted"):
                    # Re-check the pane is still idle: the confirm modal can sit
                    # open for MINUTES, and the pane's state may have moved on
                    # meanwhile (auto-compact kicking in near the ceiling — b2's
                    # exact use case — or a hook/turn). Pasting /clear into a busy
                    # pane queues it as a literal MESSAGE. Wait for idle again,
                    # with a ceiling. (#audit-b2-postconfirm)
                    try:
                        term.refresh_status()
                        self._apply_live_status(b2["sid"], getattr(term, "_status", ""))
                    except Exception:
                        pass
                    if self._pane_is_midturn(b2["sid"]):
                        b2["clear_wait"] = b2.get("clear_wait", 0) + 1
                        if b2["clear_wait"] > 200:      # ~60s of unexpected activity
                            _p = self._b2_save_prompt(b2.get("prompt"))
                            self._b2_finish(
                                "checkpoint aborted — the pane went busy after the "
                                "confirm; /clear was NOT sent"
                                + (f" (reseed prompt saved to {_p})" if _p else ""),
                                "warning")
                            return
                        return
                    b2["pre_sids"] = _project_sids(b2["project_dir"])
                    b2["clear_ts"] = datetime.now(timezone.utc).strftime(
                        "%Y-%m-%dT%H:%M:%S.000Z")
                    try:
                        # Ctrl+U first: a leftover draft would turn the paste into
                        # "draft/clear" — no leading '/', so it would SUBMIT as a
                        # garbage message instead of running /clear. (#audit-b2-draft)
                        _kill = getattr(term, "kill_input_line", None)
                        if _kill is not None:
                            _kill()
                        term.paste_text("/clear")
                    except Exception:
                        self._b2_finish("checkpoint aborted — could not inject /clear",
                                        "error")
                        return
                    self._b2_log("clear pasted")
                    b2["_clear_pasted"] = True
                    b2["ticks"] = self._B2_CLEAR_SETTLE_TICKS
                    return
                # ~0.5s settle between the /clear paste and its CR: the leading
                # '/' opens the slash palette and a CR arriving too soon is
                # absorbed (spike #5).
                if b2["ticks"] > 0:
                    b2["ticks"] -= 1
                    return
                try:
                    term.submit()
                except Exception:
                    self._b2_finish("checkpoint aborted — could not submit /clear",
                                    "error")
                    return
                b2["ticks"] = self._B2_DETECT_TICKS
                b2["state"] = "detect_child"
                return

            if st == "detect_child":
                cands = _cleared_child_candidates(
                    b2["project_dir"], b2["pre_sids"], b2["pane_cwd"], b2["clear_ts"])
                # Bind only a STABLE single candidate. A contaminant can land BEFORE
                # the real child; binding eagerly on the first len==1 would mis-bind
                # it (the real child arriving later never trips a >=2 guard once we've
                # moved on). Require the SAME single sid for _B2_CHILD_CONFIRM_TICKS
                # consecutive ticks; a 2nd candidate appearing resets the streak. (#audit-b2-toctou)
                if len(cands) == 1 and b2.get("_cand") == cands[0]:
                    b2["_cand_n"] = b2.get("_cand_n", 0) + 1
                elif len(cands) == 1:
                    b2["_cand"], b2["_cand_n"] = cands[0], 1
                else:
                    b2["_cand"], b2["_cand_n"] = None, 0
                if len(cands) == 1 and b2.get("_cand_n", 0) >= self._B2_CHILD_CONFIRM_TICKS:
                    b2["child"] = cands[0]
                    self._b2_log(f"child bound sid={cands[0]}")
                    b2["state"] = "inject_reseed"
                    return
                # Defensive re-send: one extra CR partway through the window in
                # case the first CR was absorbed (spike #5). Harmless on a fresh
                # empty session.
                if (not b2["resent_cr"]
                        and b2["ticks"] <= self._B2_DETECT_TICKS - 14):
                    try:
                        term.submit()
                    except Exception:
                        pass
                    b2["resent_cr"] = True
                b2["ticks"] -= 1
                if b2["ticks"] <= 0:
                    minted = bool(_project_sids(b2["project_dir"])
                                  - set(b2["pre_sids"] or ()))
                    if minted:
                        # /clear DID mint new transcript(s) — we just can't
                        # falsifiably pick ONE (contamination / ≥2 candidates).
                        # The pane itself IS a fresh session either way, so reseed
                        # it rather than throwing away the prompt the user just
                        # vetted; only the lineage record is skipped. Reseed needs
                        # no child sid — only _set_lineage does. (#audit-b2-reseed-anyway)
                        self._b2_log("detect_child timeout WITH mint — "
                                     "reseeding without lineage")
                        b2["child"] = None
                        b2["state"] = "inject_reseed"
                        return
                    # No new transcript at all: /clear very likely never executed
                    # (both CRs absorbed). Do NOT paste the prompt into the
                    # un-cleared parent — save it for the user instead.
                    _p = self._b2_save_prompt(b2.get("prompt"))
                    self._b2_finish(
                        "checkpoint: /clear was sent but no new session appeared — "
                        "the pane may not have cleared; nothing was reseeded"
                        + (f" (reseed prompt saved to {_p})" if _p else ""),
                        "warning")
                return

            if st == "inject_reseed":
                # Two-phase like inject_clear (paste → settle → CR), then VERIFY.
                # b2's old paste+CR-in-one-tick worked in the spike era because the
                # child jsonl took 2.5-4s to appear; on v2.1.198 it is minted
                # INSTANTLY (the /clear command record), so binding — and this
                # inject — now lands while claude is still re-initialising after
                # /clear. Measured: the paste survives, the CR is absorbed, and the
                # old code then toasted "done" over a never-reseeded child. (#audit-b2-reseed-cr)
                if not b2.get("_reseed_pasted"):
                    try:
                        term.paste_text(b2["prompt"])
                    except Exception:
                        _p = self._b2_save_prompt(b2.get("prompt"))
                        self._b2_finish(
                            "checkpoint: reseed prompt could not be injected"
                            + (f" (saved to {_p})" if _p else ""), "error")
                        return
                    self._b2_log("reseed pasted")
                    b2["_reseed_pasted"] = True
                    b2["ticks"] = self._B2_CLEAR_SETTLE_TICKS
                    return
                if b2["ticks"] > 0:
                    b2["ticks"] -= 1
                    return
                try:
                    term.submit()
                except Exception:
                    _p = self._b2_save_prompt(b2.get("prompt"))
                    self._b2_finish(
                        "checkpoint: reseed prompt could not be submitted"
                        + (f" (saved to {_p})" if _p else ""), "error")
                    return
                b2["ticks"] = self._B2_RESEED_VERIFY_TICKS
                b2["state"] = "verify_reseed"
                return

            if st == "verify_reseed":
                # The reseed only COUNTS once the pane actually starts the turn
                # (busy — or waiting, e.g. a permission prompt the reseed's first
                # step raised). While it stays idle, the CR was absorbed by the
                # post-/clear re-init: resend it every ~2s (harmless once the turn
                # has started — it lands on an empty input). Track the TARGET's
                # own screen every tick, same as await_handoff_idle, so this is
                # focus-independent. (#audit-b2-reseed-cr)
                try:
                    term.refresh_status()
                    _ts = getattr(term, "_status", "")
                    self._live.set_status(b2["sid"], _ts)
                    self._apply_live_status(b2["sid"], _ts)
                except Exception:
                    pass
                status = (self._live.statuses().get(b2["sid"])
                          if self._live else None)
                if status in ("busy", "waiting"):
                    self._b2_log("reseed submit verified (pane went busy)")
                    b2["state"] = "record_lineage"
                    return
                b2["ticks"] -= 1
                if b2["ticks"] <= 0:
                    # Give up verifying but DON'T abort: lineage/re-key are still
                    # correct, and the pasted prompt sits in the child's input —
                    # the final toast tells the user to press Enter there.
                    b2["reseed_unverified"] = True
                    self._b2_log("reseed NOT verified — pane never went busy")
                    b2["state"] = "record_lineage"
                    return
                if b2["ticks"] % 7 == 0:
                    try:
                        term.submit()
                    except Exception:
                        pass
                    b2["reseed_crs"] = b2.get("reseed_crs", 0) + 1
                    self._b2_log(f"reseed CR resent (n={b2['reseed_crs']})")
                return

            if st == "record_lineage":
                _unv = bool(b2.get("reseed_unverified"))
                _unv_suffix = (" — the reseed did NOT visibly submit: open the pane "
                               "and press Enter (the prompt is in its input box)"
                               if _unv else "")
                if not b2.get("child"):
                    # detect_child timed out WITH a mint: the pane was reseeded
                    # above, but no falsifiable child sid → skip lineage/re-key
                    # (guessing would wire Shift+F6 to the wrong session).
                    try:
                        self.set_timer(0.8, self.action_refresh)   # (#audit-b2-autorefresh)
                    except Exception:
                        pass
                    self._b2_finish(
                        "checkpoint: reseeded, but the new session could not be "
                        "identified — no lineage recorded" + _unv_suffix, "warning")
                    return
                try:
                    _set_lineage(b2["child"], b2["sid"], b2["parent_jsonl"])
                except Exception as e:               # noqa: BLE001
                    self._b2_finish(f"checkpoint reseeded, but lineage write failed: {e}",
                                    "warning")
                    return
                # The running pane IS the child now (same PTY, new sid after
                # /clear) — so re-key the pane's whole identity parent->child, not
                # just its gauge. Without this the pane stays keyed by the PARENT
                # sid and: restore (_save_open_panes) resumes the frozen parent and
                # drops the lean child; Shift+F6 from the pane can't find the parent
                # (it's parent-keyed, so _load_lineage().get(parent) is None);
                # re-opening the child row spawns a 2nd `claude --resume child`; and
                # the list marks the FROZEN parent row live. All of it is pure UI-
                # thread dict work here (no PTY write / lock / marshal / close).
                from pathlib import Path as _P
                parent_sid, child_sid = b2["sid"], b2["child"]
                child_jsonl = _P(b2["project_dir"]) / f"{child_sid}.jsonl"
                try:
                    self._live.rekey(parent_sid, child_sid)
                except Exception:
                    pass
                # rekey moved the live pane parent->child in the manager, but the
                # App-level attention sets are keyed by sid too. The parent is no
                # longer a live pane (it's the pre-/clear session), so drop it from
                # both — otherwise every checkpoint leaves a dead parent sid lodged
                # in _unread/_busy_seen forever. The child is a FRESH post-/clear
                # session (about to go busy on the reseed) and picks up its own
                # bookkeeping via the normal status flow; nothing to migrate. (#5)
                self._unread.discard(parent_sid)
                self._busy_seen.discard(parent_sid)
                try:
                    term.sid = child_sid
                except Exception:
                    pass
                # The gauge override is now redundant (term.sid == child and the
                # child's _sid_index jsonl is the child transcript) but harmless;
                # keep it so the gauge re-points even if the rescan lags.
                try:
                    term._live_jsonl = str(child_jsonl)
                except Exception:
                    pass
                # Inject an interim child session so the list / status / Shift+F6
                # resolve BEFORE the next rescan converges. The parent stays in the
                # index as a real historical session (it now has its own transcript).
                try:
                    s_par = self._sid_index.get(parent_sid) or {}
                    title = (s_par.get("ai_title") or s_par.get("summary")
                             or _P(b2["pane_cwd"]).name or child_sid[:8])
                    stub = _new_session_stub(child_sid, b2["pane_cwd"], title)
                    stub["jsonl_path"] = str(child_jsonl)   # str like every other entry
                    self._sid_index[child_sid] = stub
                except Exception:
                    pass
                # Restore must follow the session: drop the parent, add the child.
                try:
                    self._opened_sids.discard(parent_sid)
                    self._opened_sids.add(child_sid)
                    self._save_open_panes()
                except Exception:
                    pass
                self._b2_mark_dirty()      # focus-safe list repaint for the re-key
                # Materialize the parent (now historical) and the child stub as
                # REAL scanned sessions right away — without this the list only
                # showed them after a manual F5. Delay a beat so the child's
                # first records are on disk. (#audit-b2-autorefresh)
                try:
                    self.set_timer(0.8, self.action_refresh)
                except Exception:
                    pass
                if _unv:
                    self._b2_finish("checkpoint: lineage recorded, but the reseed "
                                    "did NOT visibly submit — open the pane and "
                                    "press Enter (the prompt is in its input box)",
                                    "warning")
                else:
                    self._b2_finish("checkpoint done — fresh session reseeded "
                                    "(Shift+F6 jumps back to the parent)")
                return

            # Unknown state — fail safe.
            self._b2_finish("checkpoint aborted — internal state error", "error")

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
                    # token_urlsafe(12) → 16 url-safe chars (96 bits). Short on
                    # purpose: the URL it lands in is the QR's payload, and a
                    # 43-char token (token_urlsafe(32)) pushed the QR to a dense
                    # version that another PC's camera couldn't resolve. 96 bits
                    # is still infeasible to brute-force over HTTP for an
                    # ephemeral, control-gated, idle-off LAN mirror.
                    # TLS (default-on, opt-out via SAIKAI_MIRROR_TLS=0): encrypt the
                    # LAN transport so a passive sniffer can't harvest the token /
                    # write-key / keystrokes, and give the browser a secure context.
                    # Resolve a cert (user-provided, else openssl self-signed cached
                    # in CACHE_DIR); if no cert is obtainable (no openssl), warn and
                    # stay HTTP rather than failing launch.
                    _tls = None
                    if _mirror.mirror_tls_enabled(os.environ):
                        _tls = _mirror.resolve_tls_paths(os.environ, CACHE_DIR, _mir_host)
                        # The WHY is recorded either way (#review-tls-reason):
                        # the stderr warning scrolls behind the alt-screen, so
                        # saikai.log is the durable place to diagnose an
                        # http-only mirror on some host after the fact.
                        _why = _mirror.tls_reason()
                        _log(f"mirror tls: {'ON — ' if _tls else 'FALLBACK to http — '}{_why}")
                        if _tls is None:
                            print(_c("  ⚠ mirror TLS is on by default but no cert "
                                     f"could be resolved [{_why}] — staying on "
                                     "HTTP. Fix the cause, set "
                                     "SAIKAI_MIRROR_TLS_CERT/_KEY, or silence with "
                                     "SAIKAI_MIRROR_TLS=0 (details in "
                                     f"{CACHE_DIR / 'saikai.log'})", YELLOW),
                                  file=sys.stderr)
                    else:
                        _log("mirror tls: OFF by SAIKAI_MIRROR_TLS opt-out")
                    _hub = _mirror.MirrorHub(
                        token=_secrets.token_urlsafe(12), host=_mir_host,
                        port=_mirror.mirror_port(os.environ),
                        idle_secs=_mirror.mirror_idle_secs(os.environ), tls=_tls)
                    # LAN input is its own opt-in: a LAN-exposed mirror stays
                    # read-only unless SAIKAI_MIRROR_ALLOW_LAN_INPUT=1. Loopback
                    # always permits input.
                    _allow_lan_in = str(os.environ.get(
                        _MIRROR_LAN_INPUT_ENV, "")).strip().lower() in (
                        "1", "true", "yes", "on")
                    _hub.allow_lan_input = _allow_lan_in
                    # LAN input is the flood-risk path: enforce a default accepted-
                    # input rate cap (~50/s) unless the user set an explicit gap via
                    # SAIKAI_MIRROR_MIN_ACCEPT_GAP. (#audit-mirror-ratecap)
                    if _allow_lan_in and _hub._min_accept_gap <= 0:
                        _hub._min_accept_gap = 0.02
                    _hub.serve()
                    atexit.register(_hub.stop)
                    _Drv = _mirror.make_mirror_driver(_mirror._base_driver_class(), _hub)
                    _app_kwargs["driver_class"] = _Drv
                    _mode = "LAN-exposed" if _mir_host != "127.0.0.1" else "loopback only"
                    _mode += ", TLS" if _tls else ", HTTP"
                    _in_mode = ("input ON" if (_mir_host == "127.0.0.1" or _allow_lan_in)
                                else f"input OFF (set {_MIRROR_LAN_INPUT_ENV}=1)")
                    _idle = _mirror.mirror_idle_secs(os.environ)
                    _in_mode += ("; control idle-off DISABLED" if _idle <= 0
                                 else f"; control idle-off {_idle:g}s")
                    # Persist the URL so it's reachable even though the Textual alt
                    # screen hides this banner during the session; cleaned up at exit.
                    _url_file = _MIRROR_URL_FILE
                    try:
                        # The URL carries the access token, so create the file
                        # owner-only (0600) rather than at the default umask.
                        # Atomic: a concurrent reader (a script polling for the
                        # URL) must never observe the truncate window as empty.
                        _write_text_atomic(_url_file, _hub.url() + "\n", mode=0o600)
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
        # Stop the web mirror BEFORE handing off to the foreground claude. Resume
        # runs `claude --resume` via subprocess.run and blocks until it returns, so
        # the Python process (and its atexit-registered _hub.stop) stays alive the
        # whole time. Left running, the mirror keeps serving — with a still-valid
        # token and, if opted in, a live /input endpoint wired to the now-gone UI —
        # a stale attack surface for the entire resumed session. (#audit-mirror-resume)
        if _hub is not None:
            try:
                _hub.stop()
                # Drop the persisted URL with the server: atexit won't fire until
                # the resumed foreground claude RETURNS (hours later), and until
                # then the file would keep pointing readers at a dead endpoint.
                # (The atexit unlink is missing_ok — double-removal is fine.)
                _MIRROR_URL_FILE.unlink(missing_ok=True)
            except Exception:
                pass
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


# Deliberately ~/.claude/state (NOT CLAUDE_CONFIG_ROOT): this is a saikai→hook
# contract file, and the notification hook that CONSUMES it reads a fixed
# ~/.claude/state path (hook state is user-level, not config-dir-relative). Moving
# it to CLAUDE_CONFIG_ROOT would desync writer and reader. (#recon-configdir)
_SAIKAI_SUPPRESS_PATH = Path.home() / ".claude" / "state" / "_saikai_resume_oneshot.json"
_SAIKAI_SUPPRESS_TTL = 3600.0  # 1h. teams-notify.py 側の SAIKAI_SUPPRESS_TTL と同期


def _add_saikai_suppress_session(session_id: str) -> None:
    """teams-notify.py に「次の Notification 1 件だけ silent」 を伝える 1-shot file.

    `SAIKAI_RESUME=1` env だけだと session lifetime 全体で Notification 抑止に
    なる過去の事故 (2026-05-24 検出) を構造的に防ぐ。 session_id ごとに 1 件
    だけ「最初の idle_prompt 抑止」 を予約する設計。

    _write_json (atomic tmp + os.replace) で並行 saikai launch race にも安全。
    古い entry (>1h) は ついでに prune (= claude が即 crash した stale を回収)。
    """
    import json as _json
    import time as _time
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
    try:
        _write_json(_SAIKAI_SUPPRESS_PATH, state)
    except Exception:
        pass                       # best-effort: a lost suppress = one extra ping


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
        # worktree_origin_cwd first: for a worktree session the worktree-state
        # record's originalCwd is the authoritative dir Claude indexed under,
        # whereas the plain origin_cwd may be the .claude/worktrees/ path. (#recon-worktree-cwd)
        for k in ("worktree_origin_cwd", "origin_cwd", "cwd"):
            v = selected.get(k)
            if v and Path(v).is_dir():
                candidates.append(v)
        if not candidates and selected.get("jsonl_path"):
            project_dir = selected["jsonl_path"].parent
            sibs: list[tuple[float, str]] = []
            for other in sessions:
                if other.get("jsonl_path") and other["jsonl_path"].parent == project_dir:
                    v = other.get("origin_cwd") or other.get("cwd")
                    if v and Path(v).is_dir():
                        sibs.append((other.get("mtime") or 0.0, v))
            if sibs:
                # Most-recently-active sibling wins: list order tracks the user's
                # active UI sort column (none of which is cwd-validity), so the old
                # first-in-order pick was arbitrary when a project dir legitimately
                # holds several distinct cwds (worktree moves). (#audit-sibling-cwd)
                sibs.sort(key=lambda t: -t[0])
                candidates.append(sibs[0][1])
    return candidates[0] if candidates else None


# Parent Claude-session markers that MUST be stripped from a child agent's
# environment so it boots as its OWN standalone session. CLAUDE_NO_SESSION_PERSISTENCE
# is the critical one: inherited, the spawned `claude` writes NO transcript JSONL, so
# saikai's discovery (transcript-based) and /clear child-detection silently fail — the
# supervisor's core feature breaks whenever saikai is itself launched from inside a
# Claude session (the lifecycle spec mandates this strip). The CLAUDE_CODE_* /
# CLAUDECODE markers are the "you are nested" signals a fresh claude re-injects on its
# own; CLAUDE_PROJECT_DIR would point the child at the parent's history root. NOT
# stripped: CLAUDE_CONFIG_DIR (a user override saikai itself honors) and
# CLAUDE_CODE_GIT_BASH_PATH (config the child needs); auth (ANTHROPIC_*) is untouched.
_CHILD_ENV_STRIP = frozenset({
    "CLAUDE_NO_SESSION_PERSISTENCE",
    "CLAUDECODE",
    "CLAUDE_CODE_SESSION_ID",
    "CLAUDE_CODE_CHILD_SESSION",
    "CLAUDE_CODE_ENTRYPOINT",
    "CLAUDE_CODE_EFFORT_LEVEL",
    "CLAUDE_PROJECT_DIR",
    "TEXTUAL_LOG",   # saikai's own Textual debug log — must not leak to the child
})


def _child_spawn_env(base: "dict | None" = None) -> dict:
    """Environment for a child agent process saikai spawns: a copy of `base`
    (os.environ by default) with the parent Claude-session markers (_CHILD_ENV_STRIP)
    stripped so the child boots standalone, plus uv's ephemeral VIRTUAL_ENV removed
    from both the var and PATH (so the child's `uv` doesn't warn about a stale venv).
    Single source of "which parent vars are unsafe for a child"; pure + unit-tested,
    does NOT mutate `base`."""
    env = dict(os.environ if base is None else base)
    for _k in _CHILD_ENV_STRIP:
        env.pop(_k, None)
    leaked_venv = env.pop("VIRTUAL_ENV", None)
    env.pop("VIRTUAL_ENV_PROMPT", None)
    if leaked_venv:
        bin_dir = "Scripts" if sys.platform == "win32" else "bin"
        venv_bin = str(Path(leaked_venv) / bin_dir)
        # normcase+normpath: "C:\Tmp\.venv\Scripts\" (trailing separator) or
        # mixed slashes must still match, or the stale venv survives on PATH. (#audit-codex-venvpath)
        def cmp(p):
            return os.path.normcase(os.path.normpath(p)) if p else p
        parts = [p for p in env.get("PATH", "").split(os.pathsep) if cmp(p) != cmp(venv_bin)]
        env["PATH"] = os.pathsep.join(parts)
    return env


def _bg_job_respawn_args(full_id: str) -> list[str]:
    """Model/effort flags to REPLAY when resuming a session that originated as a
    background job, so the resumed session keeps the bg job's tier (the job's
    state.json records the exact respawnFlags Claude used). Scans
    CLAUDE_CONFIG_ROOT/jobs/*/state.json for one whose sessionId/resumeSessionId
    matches; returns ONLY --model / --effort pairs — NOT --permission-mode, whose
    auto value would silently make a saikai-resumed session auto-accept (saikai's
    own SAIKAI_AUTO_PERMISSION opt-in governs that). [] when none. (#recon-respawn)"""
    if not full_id:
        return []
    try:
        job_states = list((CLAUDE_CONFIG_ROOT / "jobs").glob("*/state.json"))
    except OSError:
        return []
    flags = None
    for sp in job_states:
        d = _read_json(sp, None)
        if isinstance(d, dict) and full_id in (d.get("sessionId"), d.get("resumeSessionId")):
            rf = d.get("respawnFlags")
            if isinstance(rf, list):
                flags = rf
            break
    if not flags:
        return []
    out: list[str] = []
    i = 0
    while i < len(flags) - 1:
        if flags[i] in ("--model", "--effort"):
            out += [str(flags[i]), str(flags[i + 1])]
            i += 2
        else:
            i += 1
    return out


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

    # Child env: parent Claude-session markers stripped (so the child boots its OWN
    # session — esp. CLAUDE_NO_SESSION_PERSISTENCE, else it writes no transcript and
    # discovery/checkpoint break) + VIRTUAL_ENV cleaned. See _child_spawn_env.
    env = _child_spawn_env()
    env["SAIKAI_RESUME"] = "1"   # signal to teams-notify.py: suppress the first idle_prompt

    if len(session_args) == 2 and session_args[0] == "--resume":
        # Replay a bg-origin session's model/effort so resuming it from saikai keeps
        # the tier the background job ran at (else it falls back to the CLI default).
        extra_args = _bg_job_respawn_args(session_args[1]) + extra_args
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


def _dedup_sessions_by_id(sessions: list[dict]) -> list[dict]:
    """Collapse sessions sharing an id to one (newest mtime wins). A case-insensitive
    filesystem can hold TWO case-variant encoded project dirs for one repo (e.g. a
    'C--…-repo' dir from PowerShell vs a 'c--…-repo' dir from Git Bash, differing only
    in the drive-letter case), each holding the SAME session files; the cross-project
    scan would then list a sid twice and table.add_row(key=sid) raises DuplicateKey,
    breaking the whole list. Dedup by sid (not dir name) so the fix is correct on
    case-sensitive filesystems too. (#H2)"""
    by_id: dict = {}
    for s in sessions:
        prev = by_id.get(s["id"])
        if prev is None or (s.get("mtime") or 0) >= (prev.get("mtime") or 0):
            by_id[s["id"]] = s
    return list(by_id.values()) if len(by_id) != len(sessions) else sessions


def _path_to_key(p: Path) -> str:
    s = str(p.resolve()).lower()
    return re.sub(r"[\\/:.\-]+", "-", s).strip("-")


def find_project_dir(cwd: Path) -> Path | None:
    projects_root = PROJECTS_ROOT
    cwd_key = _path_to_key(cwd)
    best, best_len = None, 0
    # Match on dash-delimited path-segment boundaries, not a raw substring: a
    # bare ancestor/segment-fragment (e.g. an unrelated dir whose key happens to
    # appear MID-segment) must not match. Wrapping both keys in dashes makes the
    # containment test align on segment edges. (#audit-projdir-substr)
    padded_cwd = "-" + cwd_key + "-"
    for cand in _project_dirs(projects_root):
        cand_key = re.sub(r"[\-\.]+", "-", cand.name.lower()).strip("-")
        if cand_key and ("-" + cand_key + "-") in padded_cwd:
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
            capture_output=True, text=True,
            encoding="utf-8", errors="replace",   # git emits UTF-8; don't decode as cp932
            cwd=cwd, timeout=5, creationflags=NO_WINDOW,
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
    # raw=True: the default raw=False path returns only the FIRST non-fence line
    # truncated to 100 chars, which cuts a comma-joined keyword list mid-word and
    # collapses a newline/bulleted reply to one topic. (#audit-topics-raw)
    raw = call_claude_haiku(prompt, timeout=30, raw=True)
    if not raw:
        return []
    # Accept comma- OR newline/bullet-separated replies.
    parts = re.split(r"[,\n]", raw)
    return [re.sub(r"^[\s\-*•]+", "", t).strip().lower() for t in parts if t.strip()][:5]


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
        # Persist even an EMPTY result: _get_cached_topics treats a missing key as
        # "never extracted" and returns None, so gating the save on `if topics`
        # re-pays the full Haiku call (up to RELATION_TOP_K sessions × 30s) on
        # every --related/forest run for every empty-result session. (#audit-topics-empty)
        _save_topics_to_cache(s["id"], s["topics"])
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
    # Relationship CONFIDENCE is a different axis from urgency, so it stays OUT of
    # the single cyan attention accent: the gradient reads from glyph + weight
    # (solid ● vs hollow ○, full vs dim), not from a competing green/yellow hue.
    if score >= 0.7:
        return "●"                # strong link — solid, full weight
    if score >= 0.4:
        return _c("●", DIM)       # medium — solid, dimmed
    if score >= 0.2:
        return _c("○", DIM)       # weak — hollow, dimmed
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
    if len(prefiltered) > RELATION_TOP_K:
        # Ranked by the optimistic topic=1 ceiling, so a dropped candidate's REAL
        # score could in principle exceed a kept one's — surface the cap rather
        # than silently bound recall. (#audit-relation-topk)
        print(_c(f"  note: scored the top {RELATION_TOP_K} of {len(prefiltered)} "
                 f"candidates (SAIKAI cap); {len(prefiltered) - RELATION_TOP_K} "
                 f"lower-ceiling sessions not topic-scored.", DIM), file=sys.stderr)
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
    print(_c("  ● high (≥0.70)   " + _c("●", DIM) + " med (≥0.40)   " +
            _c("○", DIM) + " low (≥0.20)", DIM))
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
                if not isinstance(obj, dict):
                    continue          # (#audit-codex-nondict)
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
    summaries.sort(key=lambda x: _iso_sort_key(x[1]["first_ts"]))

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
    by_time = sorted(sessions, key=lambda s: _iso_sort_key(s["first_ts"]))
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
            # Confidence via WEIGHT, not hue (kept clear of the cyan attention
            # accent): a strong link is a full-weight dash, weaker ones dim, and a
            # low-confidence link degrades the dash to a dot.
            if score >= 0.7:
                glyph = base                       # strong — full weight
            elif score >= 0.4:
                glyph = _c(base, DIM)              # medium — dimmed dash
            else:
                glyph = _c(base[0] + ".", DIM)     # weak — dimmed dot
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
# The store lives under each OS's standard app-data dir (Desktop ships on Windows
# + macOS; on Linux the path simply won't exist and sync reports "not found").
if sys.platform == "darwin":
    _DESKTOP_APPDATA = Path.home() / "Library" / "Application Support"
elif sys.platform == "win32":
    _DESKTOP_APPDATA = Path.home() / "AppData" / "Roaming"
else:
    _DESKTOP_APPDATA = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
DESKTOP_SESSIONS_ROOT = _DESKTOP_APPDATA / "Claude" / "claude-code-sessions"


def _desktop_account_dir_from_config() -> Path | None:
    """The AUTHORITATIVE <org>/<user> account dir from Desktop's own config files,
    when resolvable: <org> = cowork-enabled-cli-ops.json's ownerAccountId, <user> =
    the guid suffix of config.json's `dxt:allowlist*:<guid>` keys (newest by
    allowlistLastUpdated if several accounts are present). Returns the dir only if
    it actually exists; None otherwise so the caller falls back to recency. This
    beats the mtime heuristic on a multi-account machine where a signed-out account
    was written more recently by an unrelated process. (#recon-desktop-acct)"""
    cfgdir = _DESKTOP_APPDATA / "Claude"
    _org_raw = _read_json(cfgdir / "cowork-enabled-cli-ops.json", {})
    org = _org_raw.get("ownerAccountId") if isinstance(_org_raw, dict) else None  # (#audit-codex-desktopshape)
    if not org:
        return None
    cfg = _read_json(cfgdir / "config.json", {}) or {}
    if not isinstance(cfg, dict):
        return None
    best_user, best_ts = None, ""
    for k, v in cfg.items():
        if not (isinstance(k, str) and k.startswith("dxt:allowlist") and ":" in k):
            continue
        guid = k.rsplit(":", 1)[-1]
        if k.startswith("dxt:allowlistLastUpdated:") and isinstance(v, str) and v > best_ts:
            best_ts, best_user = v, guid     # newest-updated account wins
        elif best_user is None:
            best_user = guid                 # no timestamp seen yet → tentative
    if not best_user:
        return None
    d = DESKTOP_SESSIONS_ROOT / str(org) / str(best_user)
    return d if d.is_dir() else None


def _desktop_index_dir() -> Path | None:
    """The <org>/<user> dir holding Desktop's local_*.json session entries.

    Prefer the AUTHORITATIVE account dir from Desktop's own config (org =
    cowork-enabled-cli-ops.json ownerAccountId, user = config.json dxt: guid) so a
    multi-account machine targets the live account deterministically. Fall back to
    the dir with the MOST-RECENTLY-written entry (recency, NOT most-entries: a
    signed-out former account can have more history). (#recon-desktop-acct / #H8)"""
    auth = _desktop_account_dir_from_config()
    if auth is not None:
        return auth
    if not DESKTOP_SESSIONS_ROOT.exists():
        return None
    locs = list(DESKTOP_SESSIONS_ROOT.rglob("local_*.json"))
    if not locs:
        return None
    try:
        return max(locs, key=lambda p: p.stat().st_mtime).parent
    except OSError:
        # stat race → fall back to the most-entries heuristic rather than fail.
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
                if not isinstance(o, dict):
                    # a valid-but-non-dict line ([] / "x") raised AttributeError
                    # into the OUTER except, aborting the scan and silently
                    # skipping the session in --sync-desktop. (#audit-codex-surface)
                    continue
                if ep is None and "entrypoint" in o:
                    ep = o["entrypoint"]
                if o.get("type") == "assistant":
                    msg = o.get("message")
                    m = msg.get("model") if isinstance(msg, dict) else None
                    if m:
                        model = m
    except Exception:
        pass
    return ep, model


def _desktop_default_model(idx: Path) -> str | None:
    """Model of the account's most-recently-written Desktop entry.

    Used as the fallback when a session's transcript carries no assistant model
    to read (e.g. a session with no assistant turn yet): mirror the account's
    OWN current model rather than fabricate a hardcoded version string that
    would misrepresent which model the session ran on. Returns None when no
    existing entry carries a model, in which case the caller omits the field and
    lets Desktop apply its own default. (#8953)"""
    best_mtime = -1.0
    model = None
    for p in idx.glob("local_*.json"):
        _e = _read_json(p, {})
        m = _e.get("model") if isinstance(_e, dict) else None   # (#audit-codex-desktopshape)
        if not m:
            continue
        try:
            mt = p.stat().st_mtime
        except OSError:
            continue
        if mt > best_mtime:
            best_mtime, model = mt, m
    return model


def _desktop_entry(s: dict, model: str | None) -> dict:
    """Build one Desktop local_* session entry from a saikai session row.

    `model` is the already-resolved model (the transcript's model, else the
    account default); when falsy the `model` key is OMITTED rather than
    fabricated (#8953). titleSource is "auto" because saikai's title is always
    derived (ai-title or first message), never user-typed."""
    sid = s["id"]
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
        "effort": "max",
        "isArchived": False,
        "title": title,
        "titleSource": "auto",
        # "default" (NOT "auto"): a synced row's real permission posture is unknown,
        # so assert least-privilege rather than imply auto-accept. The recon survey
        # found native rows are mostly "auto" because the USER ran them that way —
        # not a reason to fabricate auto on a surfaced CLI session. (#recon-desktop-fab)
        "permissionMode": "default",
        "enabledMcpTools": {},
        "remoteMcpServersConfig": [],
        "completedTurns": 0,
        "alwaysAllowedReasons": [],
        "sessionPermissionUpdates": [],
    }
    if model:
        entry["model"] = model
    return entry


def cmd_sync_desktop() -> None:
    """Surface Terminal/VS Code sessions in Claude Desktop's session list.

    Additive (writes only new local_<uuid>.json entries into Desktop's own store)
    and idempotent (sessions already linked by cliSessionId are skipped). The
    model is read from each transcript and, when absent, mirrors the account's
    current model rather than being fabricated (#8953).
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
        _e = _read_json(p, {})
        c = _e.get("cliSessionId") if isinstance(_e, dict) else None  # (#audit-codex-desktopshape)
        if c:
            known.add(c)
    default_model = _desktop_default_model(idx)
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
            entry = _desktop_entry(s, model or default_model)
            try:
                _write_json(idx / (entry["sessionId"] + ".json"), entry)
                created += 1
                # Mark linked NOW so a sid yielded twice in one run (e.g. the same
                # id living in two case-variant project dirs) can't spawn a second
                # entry — `known` was otherwise only seeded from pre-existing files. (#8916)
                known.add(sid)
            except Exception as e:
                print(_c(f"  failed writing entry for {sid[:8]}: {e}", RED), file=sys.stderr)
    print(_c(f"  Claude Desktop sync: +{created} new, {skipped} already present.",
             GREEN), file=sys.stderr)
    if created:
        print(_c("  Restart Claude Desktop to see the new sessions.", DIM), file=sys.stderr)


# ── Main ─────────────────────────────────────────────────────────────────────
def _sweep_cache_litter() -> None:
    """Startup housekeeping (run in a daemon; best-effort, never raises).

    (1) Remove orphaned ``*.tmp.<pid>.<tid>`` files a killed _write_text_atomic
        left behind between write and os.replace (a crash / power loss / End Task).
        Under ~/.claude/state — Claude Code's own dir — match ONLY our filename
        prefix so nothing of Claude's is touched. Age-gated so an in-flight tmp
        from a concurrent saikai is never removed.
    (2) Prune parsed/preview cache files not rewritten in 90 days (a long-gone
        session); they self-heal via re-parse if the session still exists. Bounds
        unbounded cache growth on a long-lived host (e.g. a Pi's small eMMC).
    (#audit-cache-hygiene)"""
    now = time.time()
    tmp_cutoff = now - 300
    cache_cutoff = now - 90 * 86400
    try:
        for f in CACHE_DIR.rglob("*.tmp.*"):
            try:
                if f.is_file() and f.stat().st_mtime < tmp_cutoff:
                    f.unlink()
            except OSError:
                pass
    except Exception:
        pass
    try:
        for f in _SAIKAI_SUPPRESS_PATH.parent.glob(_SAIKAI_SUPPRESS_PATH.name + ".tmp.*"):
            try:
                if f.stat().st_mtime < tmp_cutoff:
                    f.unlink()
            except OSError:
                pass
    except Exception:
        pass
    for d in (PARSED_DIR, PREVIEW_DIR, PREVIEW_FULL_DIR):
        try:
            for f in d.glob("*"):
                try:
                    if f.is_file() and f.stat().st_mtime < cache_cutoff:
                        f.unlink()
                except OSError:
                    pass
        except Exception:
            pass


def _main():
    try:                       # housekeeping off the launch path (see docstring)
        threading.Thread(target=_sweep_cache_litter, daemon=True,
                         name="saikai-cache-sweep").start()
    except Exception:
        pass
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
    # --help groups the flags by purpose. Everyday scope/view flags sit at the
    # top; the scripting, analysis, and config surfaces are demoted into labelled
    # sections below so `saikai --help` reads as a short, calm list — the flags
    # themselves and their behaviour are UNCHANGED (aliases/hooks keep working).
    p.add_argument("--version", action="version", version=f"saikai {__version__}")

    # ── common: what most invocations use ──
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
    p.add_argument("--project", metavar="PATH")
    p.add_argument("--table", action="store_true",
                   help="Show static table instead of the interactive picker")
    p.add_argument("--pick", action="store_true",
                   help="Open the interactive picker. This is the default when "
                        "no other action flag is given; --pick is kept as an explicit "
                        "no-op for clarity in shell aliases.")

    # ── session actions ──
    g_session = p.add_argument_group(
        "session actions",
        "one-shot operations on a session, for shell aliases and Claude hooks — "
        "the picker has keys for all of these (f favorite, h hide, Tab preview)")
    g_session.add_argument("--favorite", metavar="SESSION_ID",
                   help="Toggle favorite (★) state for a session")
    g_session.add_argument("--fav-current", action="store_true",
                   help="Mark the current Claude Code session as favorite. "
                        "Resolves the session ID from $CLAUDE_SESSION_ID, "
                        "falling back to the most-recently-modified JSONL "
                        "in this project's encoded directory.")
    g_session.add_argument("--hide", metavar="SESSION_ID",
                   help="Toggle hidden state for a session")
    g_session.add_argument("--preview", metavar="SESSION_ID",
                   help="Print a session's content preview")
    g_session.add_argument("--preview-full", metavar="SESSION_ID",
                   help="Print a session's full conversation preview")

    # ── view & sort ──
    g_view = p.add_argument_group(
        "view & sort",
        "persist a display preference from the shell; inside the picker set them "
        "live (F6 favorite-view, Shift-F5 tree, a column-header click to sort)")
    g_view.add_argument("--toggle-view", action="store_true",
                   help="Toggle saved default/show-hidden view mode (persistent).")
    g_view.add_argument("--toggle-tree", action="store_true",
                   help="Toggle saved flat/nested tree-display mode (persistent). "
                        "Same effect as Shift-F5 inside the picker.")
    g_view.add_argument("--cycle-sort", type=int, metavar="N", choices=[1, 2, 3],
                   help="Advance the Nth sort priority to the next column. Persistent. "
                        "In the picker, click a column header instead.")
    g_view.add_argument("--toggle-sort-dir", type=int, metavar="N", choices=[1, 2, 3],
                   help="Toggle the Nth sort priority's direction (asc/desc). Persistent. "
                        "In the picker, click a sorted column header again to reverse.")
    g_view.add_argument("--reset-sort", action="store_true",
                   help="Reset all sort priorities to defaults (recency desc, then none).")

    # ── analysis & export ──
    g_analysis = p.add_argument_group("analysis & export")
    g_analysis.add_argument("--related", metavar="SESSION_ID",
                   help="Show sessions related to SESSION_ID with confidence scores and reasons")
    g_analysis.add_argument("--tree", action="store_true",
                   help="Group sessions into an inferred parent/child forest (heuristic, "
                        "scores cwd / branch / title / topic + time decay).")
    g_analysis.add_argument("--sidechain", metavar="SESSION_ID",
                   help="Show the in-session sidechain (subagent) tree for SESSION_ID "
                        "using isSidechain+parentUuid metadata (confirmed, not heuristic).")
    g_analysis.add_argument("--sync-desktop", action="store_true",
                   help="Create Claude Desktop session-list entries for Terminal/VS Code "
                        "sessions missing from it. NOTE: this WRITES into Claude Desktop's "
                        "own session store, whose format is internal/undocumented; the write "
                        "is additive + idempotent and never touches ~/.claude/projects. "
                        "Restart Desktop afterwards to see them.")

    # ── config & defaults ──
    g_config = p.add_argument_group("config & defaults")
    g_config.add_argument("--init-config", action="store_true",
                   help="Write a commented config.toml template to the config path, then exit.")
    g_config.add_argument("--print-config", action="store_true",
                   help="Print the resolved settings + their source (default/config/env), then exit.")
    g_config.add_argument("--force", action="store_true",
                   help="With --init-config: overwrite an existing config file.")
    g_config.add_argument("--save-defaults", action="store_true",
                   help="Persist the current --days/--here/--all values as new defaults. "
                        "Without this flag, CLI args are one-shot and saved options stay untouched.")
    g_config.add_argument("--reset-options", action="store_true",
                   help="Forget saved --days/--here/--all defaults. Preserves "
                        "split ratio and filter-bar visibility. Does NOT clear "
                        "hidden/favorite/view-mode/tree-mode/sort — "
                        "toggle those via F7 / F6 / Shift-F5 in the "
                        "picker, ':hidden' in search for hidden rows, a column-header "
                        "click to sort (or the matching "
                        "--toggle-* / --cycle-sort / --reset-sort flags).")
    g_config.add_argument("--dump-handoff-prompt", action="store_true",
                   help="Write the built-in b2 handoff prompt to the override file "
                        "(SAIKAI_HANDOFF_PROMPT_FILE, else CACHE_DIR/handoff-prompt.md) "
                        "so you can edit it, then exit.")
    g_config.add_argument("--no-summary", action="store_true",
                   help="Skip Haiku summarization (use AI title or first user msg)")
    g_config.add_argument("--refresh-summary", action="store_true",
                   help="Discard cached Haiku summaries and regenerate. Does NOT touch "
                        "parsed/topic caches; delete ~/.cache/saikai/parsed/ for that.")
    args = p.parse_args()

    if args.init_config:
        sys.exit(_init_config(force=args.force))
    if args.print_config:
        sys.exit(_print_config())
    if args.dump_handoff_prompt:
        _hp_path = _handoff_prompt_path()
        try:
            _hp_path.parent.mkdir(parents=True, exist_ok=True)
            _hp_path.write_text(_B2_HANDOFF_PROMPT + "\n", encoding="utf-8")
        except OSError as e:
            print(f"could not write {_hp_path}: {e}", file=sys.stderr)
            sys.exit(1)
        print(f"wrote the default b2 handoff prompt to:\n  {_hp_path}\n")
        print("Edit it freely — but KEEP an instruction to END with a fenced block "
              "whose first line is exactly 'NEW SESSION PROMPT'. saikai aborts the "
              "checkpoint (no /clear) if that block is missing, and on launch it "
              "falls back to the built-in prompt if your file drops that contract.")
        sys.exit(0)

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
        # Also clear the time window: --related must find the target "wherever it
        # lives", but a saved --days default would drop an older target from the
        # scanned list and the lookup would exit 1. (#audit-related-days)
        args.days = 0

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

    # Gate the wipe on summaries actually being enabled: deleting unconditionally
    # before the gate (summaries are opt-in/off by default, and --no-summary forces
    # them off) wipes the cache but skips Phase-2 regeneration, leaving the flag's
    # "discard and regenerate" promise unfulfilled in the common/default case.
    # _set_summary_forced_off runs later, so check args.no_summary explicitly. (#audit-refresh-summary)
    if (args.refresh_summary and CACHE_DIR.exists()
            and not args.no_summary and _summary_enabled()):
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
                          capture_output=True, text=True,
                          encoding="utf-8", errors="replace",   # git emits UTF-8; don't decode as cp932
                          cwd=cwd, timeout=3, creationflags=NO_WINDOW)
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

    # Collapse any sid that surfaced from >1 project dir (case-variant encoded dirs
    # on a case-insensitive FS) BEFORE the table keys rows by sid — else DuplicateKey. (#H2)
    sessions = _dedup_sessions_by_id(sessions)
    # Initial chronological sort gives _build_forest a deterministic order; the
    # user-configurable sort spec is applied AFTER forest building so it controls
    # only the displayed order.
    sessions.sort(key=lambda s: _iso_sort_key(s["first_ts"]), reverse=True)
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
            # Same sid from >1 project dir (case-variant encoded dirs on a
            # case-insensitive FS) must collapse HERE too, not just at initial
            # load — DataTable.add_row(key=sid) raises on a duplicate key, so a
            # reappearing sid broke the list only AFTER an F5/auto-refresh. (#audit-codex-reload-dedup)
            fresh = _dedup_sessions_by_id(fresh)
            fresh.sort(key=lambda s: _iso_sort_key(s["first_ts"]), reverse=True)
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


def main():
    """Console entry (pyproject: saikai = "saikai:main"): run the CLI/TUI,
    treating a broken stdout pipe as a normal pipeline end. `saikai --table |
    head` closes our stdout after 10 lines; the write then raised
    BrokenPipeError with a full traceback and a non-zero exit. (#audit-codex-pipe)"""
    try:
        _main()
    except BrokenPipeError:
        # The reader went away — nothing more to say. Point stdout at devnull
        # so the interpreter-shutdown flush can't raise a second time.
        try:
            os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        except Exception:
            pass
        sys.exit(0)


if __name__ == "__main__":
    main()
