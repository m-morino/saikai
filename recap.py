#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "textual>=0.50",
#   "pyte>=0.8",                          # PTY byte-stream -> screen grid (split-live)
#   "pywinpty>=2.0 ; sys_platform == 'win32'",   # Windows ConPTY backend
#   "ptyprocess>=0.7 ; sys_platform != 'win32'", # POSIX PTY backend
# ]
# ///
"""
recap — Claude Code session history viewer with LLM summarization
Usage:
  recap [--days N] [--all-projects] [--pick] [--project PATH]
        [--no-summary] [--refresh-summary]
"""
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
import uuid
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from pathlib import Path

# Force UTF-8 output on Windows
if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

# CREATE_NO_WINDOW prevents a console window flash when launching command-line
# helpers (git, taskkill) from a GUI terminal on Windows. No-op on POSIX.
NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

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


def _read_json(path: Path, default):
    """Read JSON file, returning `default` on any error (missing/corrupt/etc.)."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


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
CACHE_DIR = Path.home() / ".cache" / "recap"
SUMMARY_MODEL = "haiku"
HIDDEN_FILE = CACHE_DIR / "hidden.json"
FAVORITE_FILE = CACHE_DIR / "favorite.json"
VIEW_MODE_FILE = CACHE_DIR / "view-mode.txt"
TREE_MODE_FILE = CACHE_DIR / "tree-mode.txt"
CLUSTER_MODE_FILE = CACHE_DIR / "cluster-mode.txt"
GROUP_BY_FILE = CACHE_DIR / "group-by.txt"
STATUS_FILTER_FILE = CACHE_DIR / "status-filter.txt"
LASTACT_FILTER_FILE = CACHE_DIR / "lastact-filter.txt"
SORT_FILE = CACHE_DIR / "sort.json"
GLOBAL_CLUSTERS_FILE = CACHE_DIR / "global-clusters.json"
OPTIONS_FILE = CACHE_DIR / "options.json"
RESUME_HISTORY_FILE = CACHE_DIR / "resume-history.tsv"
LOG_FILE = CACHE_DIR / "recap.log"

# Sort columns selectable via Ctrl-1/2/3. "-" = inactive (no sort at this priority).
SORT_COLS = ("-", "date", "last", "proj", "title", "turns", "fav", "topic")
SORT_DEFAULT = [
    {"col": "date", "dir": "desc"},
    {"col": "-",    "dir": "desc"},
    {"col": "-",    "dir": "desc"},
]
PARSED_DIR = CACHE_DIR / "parsed"
PREVIEW_DIR = CACHE_DIR / "preview"
PREVIEW_FULL_DIR = CACHE_DIR / "preview-full"


def _log(msg: str) -> None:
    """Append a timestamped line to CACHE_DIR/recap.log. TUI-safe (a FILE, never
    stdout — that would corrupt the Textual display), best-effort, and size-bounded
    (rotates at ~1 MB, one backup) so it can neither fail recap nor grow without
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
    by an older recap version that doesn't know about them."""
    merged = _load_options()
    merged.update(opts)
    _write_json(OPTIONS_FILE, merged)


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


def _get_cluster_mode() -> bool:
    """Saved topic-cluster display preference. False (no cluster) by default."""
    try:
        return CLUSTER_MODE_FILE.read_text(encoding="utf-8").strip() == "on"
    except Exception:
        return False


def _toggle_cluster_mode() -> bool:
    new = not _get_cluster_mode()
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    CLUSTER_MODE_FILE.write_text("on" if new else "off", encoding="utf-8")
    return new


def _get_group_by() -> str:
    """Saved grouping axis: 'none' | 'date' | 'project'. Mirrors Claude
    Desktop's 'Group by' menu (Desktop defaults to Date; recap defaults to
    'none' to keep the plain list unless the user opts in)."""
    try:
        v = GROUP_BY_FILE.read_text(encoding="utf-8").strip()
        return v if v in ("none", "date", "project", "state") else "none"
    except Exception:
        return "none"


def _set_group_by(value: str) -> None:
    if value not in ("none", "date", "project", "state"):
        value = "none"
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    GROUP_BY_FILE.write_text(value, encoding="utf-8")


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
    Today / Yesterday / 'M月D日' (this year) / 'YYYY/M/D' (older)."""
    if d is None:
        return "—"
    today = now.date()
    if d == today:
        return "Today"
    if (today - d).days == 1:
        return "Yesterday"
    if d.year == today.year:
        return f"{d.month}月{d.day}日"
    return f"{d.year}/{d.month}/{d.day}"


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
    pinned = [s for s in rest if s["id"] in favorites]
    if pinned:
        groups.append(("Pinned", pinned))
        rest = [s for s in rest if s["id"] not in favorites]
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
                # Claude Code writes tool_result turns as type:"user" too; those
                # are auto-generated, not a human prompt pending — don't flag.
                content = (rec.get("message") or {}).get("content")
                if isinstance(content, list) and any(
                        isinstance(b, dict) and b.get("type") == "tool_result"
                        for b in content):
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
        "model": SUMMARY_MODEL,
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
# Sessions whose first user message matches these are filtered out of recap.
HOOK_PROMPT_MARKERS = (
    "以下は git commit で **新しく追加される行のみ**",  # personal-names hook
    "実在する個人情報 (実在人名 kanji",                    # personal-names hook variant
    "回答は JSON のみで",                                  # generic JSON-only hook prompt
    "Reply with ONLY",                                     # English JSON-only hook
    "Extract 3-5 short topic keywords",                    # recap's own topic extractor
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
# terminal closes, so recap and any resumed `claude --resume` child die with the
# tab. Windows has no such cascade: closing a wezterm tab kills that tab's shell
# (pwsh) but leaves the orphaned cmd→uv→python(recap)→claude chain running
# forever — recap blocked in subprocess.run(claude), claude idle on a dead pty.
# Confirmed 2026-06-05 via reaper.log: 12 such pairs survived ~24h, and
# reap-orphan-claude.py is structurally blind to them (it excludes python/uv
# parents after the 2026-05-23 live-session false-positive incident). This
# watchdog restores the SIGHUP semantic: find this tab's shell, poll it, and
# when it dies taskkill recap's OWN subtree (the claude child included). It only
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
    — an inner shim (recap.cmd's cmd.exe, the bash wrapper) merely orphans
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
    when no tab shell is found (headless), or when RECAP_NO_TERMINAL_WATCHDOG is
    set. See the module comment above _SHELL_ANCESTOR_NAMES for the why."""
    if sys.platform != "win32" or os.environ.get("RECAP_NO_TERMINAL_WATCHDOG"):
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
    _thr.Thread(target=_watch, daemon=True, name="recap-terminal-watchdog").start()


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
        "first_ts": parsed["first_ts"],
        "last_ts": parsed.get("last_ts") or parsed["first_ts"],
        "ai_title": parsed.get("ai_title", ""),
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
PROJECTS_ROOT = Path.home() / ".claude" / "projects"


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
    orphans that keep running — observed as recap-originated zombies after Haiku
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
    cost (e.g. one-shot clustering across the full session list).

    The prompt is delivered over STDIN, not as a command-line argument —
    Windows' CreateProcess caps argv at 32,767 chars total, which a
    cluster-classification prompt (170+ sessions × ~150 chars) bumps right
    up against. Stdin has no such limit.

    Set RECAP_SUMMARIZE_BACKEND=project-three to use project-three-cli instead of claude -p
    (avoids personal Haiku quota; model param is ignored for project-three)."""
    if os.environ.get("RECAP_SUMMARIZE_BACKEND") == "project-three":
        return call_project-three(prompt, timeout=timeout, raw=raw)
    cmd = ["claude", "-p", "--model", model or SUMMARY_MODEL,
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


def call_project-three(prompt: str, timeout: int = 45, raw: bool = False) -> str:
    """Call project-three-cli chat as a drop-in for call_claude_haiku.

    Enabled by RECAP_SUMMARIZE_BACKEND=project-three. Uses the company project-three
    service instead of claude -p, so it doesn't consume personal Haiku quota.
    JSON format differs: content[0].text instead of result."""
    cmd = ["project-three-cli", "chat", "--json"]
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
        raw_text = raw_out.decode("utf-8", errors="replace")
        try:
            payload = json.loads(raw_text)
            text = "".join(
                c.get("text", "") for c in payload.get("content", [])
                if c.get("type") == "text"
            ).strip()
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
        global _haiku_missing_warned
        if not _haiku_missing_warned:
            _haiku_missing_warned = True
            print(_c("  warn: `project-three-cli` not found — summaries will be raw user msgs",
                     YELLOW), file=sys.stderr)
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


def summarize_session(s: dict) -> str:
    """Get summary for a session: cache → AI title → LLM.

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
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = {ex.submit(summarize_session, s): s for s in pending}
        for fut in as_completed(futures):
            s = futures[fut]
            try:
                s["summary"] = fut.result()
            except Exception:
                s["summary"] = _first_msg(s)
            done += 1
            print(f"\r  [{done}/{len(pending)}] ", end="", file=sys.stderr, flush=True)
    print(file=sys.stderr)

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
    """Strip the home-path prefix ("C--Users-user-") so the column shows
    a recognizable suffix. e.g. C--Users-user-CLI-project-one → CLI-project-one."""
    parts = name.split("-")
    if len(parts) > 4:
        return "-".join(parts[5:])[:14] or name[:14]
    return name[:14]


def label_for(s: dict) -> str:
    summary = s.get("summary", "") or ""
    if summary:
        return summary
    fallback = _first_msg(s, 80)
    return fallback if fallback else _c("(empty)", GRAY)


# ── Display ──────────────────────────────────────────────────────────────────
def _find_session_jsonl(sid_prefix: str) -> Path | None:
    sid_prefix = _trim_sid(sid_prefix)
    projects = Path.home() / ".claude" / "projects"
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
    lines.append("\033[2mTab: full/summary  ·  Ctrl-d: changes (transcript diff)\033[0m")
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
    lines.append("\033[2mTab: full/summary  ·  Ctrl-d: changes (transcript diff)\033[0m")
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
    """Preview mode (Ctrl-d): a diff-like view of what THIS session changed,
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
    lines.append("\033[2mTab: full/summary  ·  Ctrl-d: changes (this view)\033[0m")
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
    # Both files are mtime-gated; reloads (Ctrl-x/p/r) skip rewrites for unchanged sessions.
    sid = s["id"]
    mtime = s.get("mtime", 0.0)
    _write_if_stale(PREVIEW_DIR / f"{sid}.txt", mtime, lambda: _render_preview(s))
    _write_if_stale(PREVIEW_FULL_DIR / f"{sid}.txt", mtime, lambda: _render_preview_full(s))


def _preview_impl(session_id: str, cache_dir: Path, render) -> None:
    sid = _trim_sid(session_id)
    if not sid:
        # Cluster-mode group-header / separator rows carry an empty SID column.
        # Returning silently avoids the caller spinning a "loading" indicator while
        # it waits for the preview command to do nothing useful.
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
# cell count depend on the terminal's CJK-width setting — and recap can't
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
                col_proj = pad(project_short(s["project_name"]), 16)
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
                col_proj = pad(_c(project_short(s["project_name"]), MAGENTA), 16)
                print(f" {marker} {col_start} {col_last} {col_proj} {col_id} {col_turns}  {col_lbl}  {commits}")
            else:
                print(f" {marker} {col_start} {col_last} {col_id} {col_turns}  {col_lbl}  {commits}")
    print()
    view_mode = _get_view_mode()
    mode_tag = _c(" [show-hidden mode]", RED) if view_mode == "show-hidden" else ""
    legend = (f"  {len(sessions)} sessions{mode_tag}  ·  "
              f"{_c('*', GOLD)} fav  {_c('+', GREEN)} active(<5m)  "
              f"{_c('.', YELLOW)} recent(<30m)  {_c('x', RED)} hidden  "
              f"{_c('@', CYAN)} open  ·  recap to resume")
    print(_c(legend, DIM))
    print()


def _reset_terminal_modes() -> None:
    """Emit ANSI disable sequences for terminal modes the picker may have enabled.

    Targets focus tracking (?1004), all mouse tracking variants (?1000/1002/1003/
    1006/1015), bracketed paste (?2004), and ensures the cursor is visible (?25).
    These are no-ops if the mode is already off — safe to send unconditionally.

    Why this exists: on Windows, the picker occasionally exits without sending the matching
    'l' sequence for focus / mouse SGR, so the shell receives literal '[I' (focus
    in) or stray 'm' (SGR mouse release terminator) characters."""
    try:
        sys.stderr.write(
            "\033[?1000l\033[?1002l\033[?1003l\033[?1004l"
            "\033[?1006l\033[?1015l\033[?2004l\033[?25h"
        )
        sys.stderr.flush()
    except Exception:
        pass


# Threshold for "frequent cwd": a directory must have hosted at least this many
# sessions before recap auto-applies --permission-mode auto on resume. Tuned by
# eye on a working history of ~hundreds of sessions; a handful of long-lived
# repos comfortably clear it while one-off cwds (downloaded folders, temp
# experiments) don't. Override with RECAP_FREQ_CWD_MIN env var.
FREQ_CWD_MIN_DEFAULT = 5


def _canonical_workspace(cwd: str) -> str:
    """Collapse a git worktree path back to its parent repo.

    The user thinks of `feature-x` as a branch of `project-one`, but recap sees
    `project-one/.worktrees/feature-x/` as a distinct cwd. Without this, every
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

    Used to identify "trusted" working directories (the user's own repos they
    return to repeatedly) so that resuming a session there can auto-apply
    --permission-mode auto, avoiding the per-tool-use approval prompts that
    interrupt flow in well-known projects. Worktrees are folded into their
    parent repo via `_canonical_workspace` so branch-switching doesn't split
    the count."""
    try:
        min_count = max(2, int(os.environ.get("RECAP_FREQ_CWD_MIN") or FREQ_CWD_MIN_DEFAULT))
    except ValueError:
        min_count = FREQ_CWD_MIN_DEFAULT
    counts = Counter(_canonical_workspace(s.get("cwd") or "") for s in sessions)
    return {cwd for cwd, n in counts.items() if cwd and n >= min_count}


def _assign_primary_topic(sessions: list[dict]) -> None:
    """Pick the most widely-shared topic for each session in place.

    'Primary' = the topic from this session's topics list that the maximum
    number of OTHER sessions also have. This produces clusters around common-
    interest topics (e.g. 'email', 'project-one') rather than singleton groups
    around session-unique topics. Sets s["primary_topic"] (lowercase) or ""
    when the session has no cached topics yet."""
    topic_count: Counter = Counter()
    for s in sessions:
        for t in (s.get("topics") or []):
            topic_count[t.lower()] += 1
    for s in sessions:
        topics = [t.lower() for t in (s.get("topics") or [])]
        s["primary_topic"] = max(topics, key=lambda t: topic_count[t]) if topics else ""


def _global_cluster_assign(sessions: list[dict], force_refresh: bool = False) -> None:
    """One-shot Haiku call to bucket every session into one of ~6-10 themes.

    Unlike _assign_primary_topic (which picks a per-session keyword from each
    session's own cached topics), this asks Haiku to look across the whole
    history and propose a small set of coherent themes, then assign every
    session to exactly one of them. Result is cached in
    ~/.cache/recap/global-clusters.json keyed by SID. Sessions added since
    the last classification fall back to the per-session primary topic until
    the next --refresh-clusters.

    Writes the resulting theme name to s["primary_topic"] in place."""
    cache = _read_json(GLOBAL_CLUSTERS_FILE, {})
    assignments: dict[str, str] = cache.get("assignments") or {}

    # Find sessions still missing a cluster assignment.
    missing = [s for s in sessions if s["id"] not in assignments]

    if force_refresh or (assignments == {} and sessions):
        # Refresh: classify ALL sessions in one call.
        targets = sessions
    elif missing:
        # Incremental: classify only the new sessions, reusing the existing
        # theme list so cache stays coherent. If we don't have a theme list
        # yet (corrupt cache), fall through to a full refresh.
        if cache.get("clusters"):
            targets = missing
        else:
            targets = sessions
    else:
        # Nothing to do — just write the cached assignments back into the
        # session dicts.
        for s in sessions:
            s["primary_topic"] = assignments.get(s["id"], "")
        return

    # Each input line gets the title PLUS the per-session topic keywords
    # the earlier Haiku extraction produced. The classifier sees concrete
    # vocabulary already attached to each session and can group on those
    # keywords directly — gives noticeably more specific themes than title
    # alone (which buries the topic in narrative prose).
    items: list[str] = []
    for s in targets:
        sid = s["id"][:8]
        title = (s.get("ai_title") or _first_msg(s) or "").replace("\n", " ")[:120]
        topics = ", ".join((s.get("topics") or [])[:5])
        items.append(f"[{sid}] {title} | topics: {topics}" if topics
                     else f"[{sid}] {title}")

    existing_themes = cache.get("clusters") or []
    theme_hint = ""
    if existing_themes and not force_refresh:
        theme_hint = (
            "\n\nExisting theme list (assign new sessions to these whenever "
            f"reasonable; only introduce a new theme if none fit):\n"
            f"{', '.join(existing_themes)}"
        )

    # Theme-count bounds inspired by Miller (1956) "Magical Number 7 ± 2": the
    # human limit for at-a-glance absolute judgement across categories. 5 keeps
    # each theme meaningful (avoids 2-3 mega-buckets); 9 keeps the picker
    # scannable. Sweet spot ~7. RECAP_CLUSTER_TARGET_N tunes the suggestion
    # (Haiku still picks the actual count); RECAP_CLUSTER_MAX caps it hard.
    try:
        target_n = max(3, min(12, int(os.environ.get("RECAP_CLUSTER_TARGET_N") or 7)))
    except ValueError:
        target_n = 7
    lo = max(3, target_n - 2)
    hi = min(12, target_n + 2)
    prompt = (
        f"Group {len(items)} Claude Code sessions into about {target_n} "
        f"(between {lo} and {hi}) coherent themes describing the WORK done.\n\n"
        "RULES:\n"
        "1. Theme names are SPECIFIC work areas — pick the most concrete\n"
        "   label that still covers ~10-30 sessions.\n"
        "     GOOD: 'recap CLI development', 'meeting-room booking',\n"
        "           'patent classification (Salesforce)', 'email drafting'.\n"
        "     BAD : 'tool development' (too generic), 'personal\n"
        "           productivity' (vague), 'general work', 'misc'.\n"
        "2. Every session must land in exactly one theme. No 'other' or\n"
        "   catch-all bucket. If a theme would only contain 1-2 sessions,\n"
        "   merge those into the nearest larger theme instead.\n"
        "3. Themes must be semantically distinct from each other — avoid\n"
        "   pairs like 'project-one development' + 'tool development'.\n"
        "4. Use the 'topics:' keywords on each line as your primary signal;\n"
        "   the title is supporting context.\n\n"
        "Reply with ONLY valid JSON, no prose:\n"
        '{"clusters": ["theme1", "theme2", ...], '
        '"assignments": {"<8-char-sid>": "<theme>", ...}}'
        f"{theme_hint}\n\nSessions:\n" + "\n".join(items)
    )

    # Haiku is the default classifier: ~30s for the user's full history,
    # and with the keyword-augmented prompt (each session line carries its
    # extracted topics) the output quality is workable. Sonnet does give
    # cleaner partitions but on 170+ sessions it can take 2-3 minutes —
    # not interactive. Opt into Sonnet with RECAP_CLUSTER_MODEL=sonnet when
    # quality matters more than latency.
    cluster_model = os.environ.get("RECAP_CLUSTER_MODEL") or "haiku"
    # Haiku usually finishes in 30-60s for ~200 sessions, but the API
    # latency tail is long — give it 4 minutes before giving up.
    timeout_s = 600 if cluster_model == "sonnet" else 240
    eta = "1-3 min" if cluster_model == "sonnet" else "30-60 s"
    print(_c(f"  classifying {len(targets)} session(s) via {cluster_model} "
            f"({eta}; cached afterwards)...", DIM), file=sys.stderr)
    raw = call_claude_haiku(prompt, timeout=timeout_s, raw=True, model=cluster_model)
    if not raw:
        print(_c(f"  warn: {cluster_model} returned no output "
                f"(likely timeout after {timeout_s}s or claude error).",
                YELLOW), file=sys.stderr)
    parsed = None
    if raw:
        try:
            # Tolerate ```json fences if Haiku wrapped its output.
            cleaned = raw.strip()
            if cleaned.startswith("```"):
                cleaned = re.sub(r"^```[a-zA-Z]*\n?|\n?```$", "", cleaned).strip()
            parsed = json.loads(cleaned)
        except Exception:
            parsed = None

    valid_parsed = (isinstance(parsed, dict)
                    and isinstance(parsed.get("assignments"), dict))
    if raw and not valid_parsed:
        # Help debug malformed LLM output — dump structure clues so we can
        # see whether the model prefaced the JSON with prose, returned a
        # different shape, hit a length cap, etc.
        snippet = raw.strip()[:400].replace("\n", " ")
        kind = type(parsed).__name__ if parsed is not None else "None"
        keys = list(parsed.keys())[:8] if isinstance(parsed, dict) else "(not a dict)"
        print(_c(f"  debug: parsed type={kind} keys={keys}", DIM), file=sys.stderr)
        print(_c(f"  debug: raw[0:400] = {snippet!r}", DIM), file=sys.stderr)

    if valid_parsed:
        new_clusters = parsed.get("clusters") or list(set(parsed["assignments"].values()))
        new_assigns = {}
        # Map back 8-char SIDs the LLM returned to full SIDs — over `targets` ONLY
        # (the sessions actually classified this round), so an over-eager reply
        # that echoes a sid OUTSIDE its task set can't overwrite a good assignment.
        sid_lookup = {s["id"][:8]: s["id"] for s in targets}
        for short, theme in parsed["assignments"].items():
            full = sid_lookup.get(short)
            if full and isinstance(theme, str):
                new_assigns[full] = theme.lower().strip()

        if force_refresh or not cache.get("assignments"):
            # Merge over any prior assignments even on a full refresh: a partial
            # Haiku reply (it omitted some sids — common at scale) then keeps the
            # clusters it didn't re-mention instead of dropping them to "". new wins.
            assignments = {**(cache.get("assignments") or {}), **new_assigns}
            cluster_set = {c.lower() for c in new_clusters} | set(assignments.values())
            cache = {"clusters": sorted(cluster_set), "assignments": assignments}
        else:
            assignments.update(new_assigns)
            cluster_set = set(cache.get("clusters") or []) | {c.lower() for c in new_clusters}
            cache = {"clusters": sorted(cluster_set), "assignments": assignments}
        _write_json(GLOBAL_CLUSTERS_FILE, cache)
    else:
        print(_c(f"  warn: {cluster_model} did not return parseable JSON — "
                "falling back to per-session primary topic.", YELLOW),
              file=sys.stderr)

    # Apply assignments; sessions still missing fall back to per-session topic.
    # Populate primary_topic first (it was otherwise never set, so the fallback
    # was always "" on a Haiku miss despite the message promising a per-session topic).
    _assign_primary_topic(sessions)
    fallback = {s["id"]: s.get("primary_topic", "") for s in sessions}
    for s in sessions:
        s["primary_topic"] = assignments.get(s["id"], fallback.get(s["id"], ""))


_CLUSTER_TOPIC_W = 14   # fixed width for the `[topic]` prefix column


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


def _avail_ram_mb():
    """Best-effort available physical RAM in MB (None if unknown). Memory — not
    CPU/core count — is what limits how many live `claude` node process trees a
    machine can host, so this gates opening more split-live panes."""
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
                return ms.ullAvailPhys / (1024 * 1024)
        else:
            with open("/proc/meminfo", encoding="utf-8") as f:
                for line in f:
                    if line.startswith("MemAvailable:"):
                        return int(line.split()[1]) / 1024
    except Exception:
        return None
    return None


def textual_pick(sessions: list[dict], repo: Path | None, show_project: bool,
                 flat: bool = False, cluster_mode: bool = False,
                 reload_fn=None) -> None:
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
      Enter        resume                Esc / Ctrl-C  cancel
      Ctrl-x       hide/unhide row       Ctrl-p        favorite toggle
      Ctrl-t       toggle tree display   Ctrl-g        toggle cluster
      Tab          preview full/summary  ?             help overlay
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
        from textual.containers import Horizontal
        from textual.screen import ModalScreen
        from textual.widgets import (DataTable, Footer, Input, RichLog, Select,
                                     Static, TabbedContent, TabPane)
        from rich.text import Text
    except ImportError as e:
        print(_c(f"  textual is required but not installed ({e}). "
                 f"Install it with: uv tool install textual "
                 f"(or: pip install 'textual>=0.50')", RED), file=sys.stderr)
        sys.exit(1)

    # Live split-terminal support is OPTIONAL and degrades gracefully: if
    # recap_terminal or its PTY/pyte deps are missing, _LIVE_TERM stays None and
    # the picker behaves exactly as before (static preview, Enter = full-takeover
    # resume). The import lives beside recap.py (single-file script's sibling).
    _LIVE_TERM = None
    _LIVE_TERM_REASON = "recap_terminal module not found"
    try:
        import recap_terminal as _LIVE_TERM  # type: ignore
        if not _LIVE_TERM.TERMINAL_AVAILABLE:
            _LIVE_TERM_REASON = _LIVE_TERM.unavailable_reason() or "unavailable"
            _LIVE_TERM = None
    except Exception as _lte:  # pragma: no cover - missing sibling / dep
        _LIVE_TERM_REASON = repr(_lte)
        _LIVE_TERM = None
    # Split-live is OPT-IN while it stabilises: the live render + keystroke path
    # into a running claude is unproven without an interactive TTY, and the
    # rendering of claude's full alt-screen UI via pyte is known-imperfect. So
    # the DEFAULT stays the proven legacy path (static preview + Enter =
    # full-takeover resume); set RECAP_SPLIT_LIVE=1 to try the live split pane.
    if _LIVE_TERM is not None and not os.environ.get("RECAP_SPLIT_LIVE"):
        _LIVE_TERM = None
        _LIVE_TERM_REASON = "split-live is opt-in (set RECAP_SPLIT_LIVE=1 to enable)"

    # Emulate POSIX SIGHUP on Windows: if this tab's shell dies (tab closed)
    # while the picker is open or a resumed `claude` is running, take recap and
    # its claude child down instead of orphaning the pair (see
    # _start_terminal_watchdog). No-op on POSIX / headless.
    _start_terminal_watchdog()

    all_sessions = list(sessions)

    # Send Textual's internal logs to a file so we have a trail to inspect
    # when something goes wrong inside the framework's event loop.
    os.environ.setdefault("TEXTUAL_LOG", str(CACHE_DIR / "textual-debug.log"))

    class HelpScreen(ModalScreen):
        CSS = """
        HelpScreen { align: center middle; }
        #help-content {
            background: $panel;
            border: solid $accent;
            padding: 1 2;
            width: 66;
            height: auto;
            max-height: 28;
        }
        """
        BINDINGS = [
            Binding("escape", "dismiss", show=False),
            Binding("question_mark", "dismiss", show=False),
        ]

        def compose(self) -> ComposeResult:
            yield Static(
                "[bold cyan]Navigation[/bold cyan]\n"
                "  [yellow]↑[/yellow] [yellow]↓[/yellow]         Move rows\n"
                "  [yellow]Enter[/yellow]       Resume session\n"
                "  [yellow]Esc[/yellow]         Quit\n"
                "  [yellow]?[/yellow]           Help (this screen)\n\n"
                "[bold cyan]Session ops[/bold cyan]\n"
                "  [yellow]Ctrl-X[/yellow]      Toggle hide/unhide"
                "  ([dim]:hidden[/dim] in search to find them)\n"
                "  [yellow]Ctrl-P[/yellow]      Toggle ★ favorite "
                "  ([dim]:fav[/dim] in search to filter)\n"
                "  [yellow]Ctrl-R[/yellow]      Refresh list  (auto: RECAP_AUTO_REFRESH=secs)\n"
                "  [yellow]Ctrl-Y[/yellow]      Copy this session's opening prompt\n"
                "  [yellow]Ctrl-D[/yellow]      Show what this session changed (transcript diff)\n\n"
                "[bold cyan]Display modes[/bold cyan]\n"
                "  [yellow]Ctrl-G[/yellow]      Cluster (topic) mode\n"
                "  [yellow]Ctrl-T[/yellow]      Tree (parent/child) mode\n"
                "  [yellow]Ctrl-O[/yellow]      Cycle grouping: none / Date / Project\n"
                "  [yellow]Tab[/yellow]         Preview: full ↔ summary\n\n"
                "[bold cyan]Split-live (RECAP_SPLIT_LIVE=1)[/bold cyan]\n"
                "  [yellow]Enter[/yellow]       Open / focus the live claude pane\n"
                "  [yellow]F2/F3[/yellow]       Prev / next live tab\n"
                "  [yellow]F4[/yellow]          Hide / show the session list\n"
                "  [yellow]Ctrl-][/yellow]      Return focus: pane → list  (RECAP_RELEASE_KEY to change)\n"
                "  [yellow]Ctrl-W[/yellow]/[yellow]Ctrl-K[/yellow]  Close tab / all (from the list — in a pane they go to claude)  ·  [yellow]Ctrl-C[/yellow] quit-all\n\n"
                "[bold cyan]Filter / Group / Sort (top-right dropdowns, Desktop-style)[/bold cyan]\n"
                "  Group by  Date / Project / State / None   (Ctrl-O cycles)\n"
                "  Sort by   Recency / Created time / Alphabetically\n"
                "  Status    Active / Archived / All\n"
                "  Age       last 1d / 3d / 7d / 30d / All time\n"
                "  (clicking a column header still sorts too)\n\n"
                "[dim]Press ? or Esc to close[/dim]",
                id="help-content",
            )

    class PickerApp(App):
        TITLE = "recap"
        # Textual's built-in command palette also binds Ctrl+P, which SHADOWS our
        # Ctrl+P = toggle-favorite (the palette opened instead of toggling). recap
        # doesn't use the palette, so disable it to free Ctrl+P.
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
            Binding("ctrl+x", "toggle_hide", "Hide"),
            Binding("ctrl+p", "toggle_fav", "★"),
            Binding("ctrl+g", "toggle_cluster", "Cluster"),
            Binding("ctrl+t", "toggle_tree", "Tree"),
            Binding("ctrl+o", "cycle_group", "Group"),
            Binding("ctrl+r", "refresh", "Refresh"),
            Binding("ctrl+y", "copy_prompt", "Copy prompt"),
            Binding("ctrl+d", "preview_changes", "Changes"),
            Binding("tab", "toggle_preview", "Preview", priority=True),  # priority overrides Textual's default focus-cycling
            Binding("question_mark", "help", "Help", priority=True),
            # Split-live: open/attach a live claude as a tab; navigate tabs; and
            # a context-sensitive Escape (handled in action_quit) that returns
            # focus to the list when a terminal is focused instead of quitting.
            # ctrl+enter keeps the legacy full-takeover resume as a fallback.
            Binding("ctrl+w", "close_live", "Close tab", show=False, priority=True),
            Binding("ctrl+k", "close_all_live", "Close all", show=False, priority=True),
            Binding("f2", "prev_tab", "◀Tab", priority=True),
            Binding("f3", "next_tab", "Tab▶", priority=True),
            Binding("f4", "toggle_list", "Hide list", priority=True),
            Binding("ctrl+l", "focus_list", "List", show=False),
        ]
        # The practical limit on concurrent live claude panes is MEMORY — each
        # is a full node process tree that sits CPU-idle waiting for input — so
        # the real gate is a free-RAM check at spawn time (see
        # _open_or_attach_live), NOT a fixed count or core count. MAX_LIVE is
        # only a runaway backstop; set RECAP_MAX_LIVE for a stricter hard cap.
        MAX_LIVE = int(os.environ.get("RECAP_MAX_LIVE", "64") or "64")
        CSS = """
        Screen { layout: vertical; }
        #searchrow { dock: top; height: 3; }
        #search { width: 1fr; border: tall $accent; }
        #groupsel { width: 15; }
        #sortsel { width: 17; }
        #statussel { width: 14; }
        #lastsel { width: 12; }
        #statusbar { height: 1; background: $surface; color: $warning; }
        #main { layout: horizontal; height: 1fr; }
        #table { width: 60%; }
        #main.split #table { width: 34%; }   /* split-live: give the live pane the room */
        #right { width: 66%; border-left: solid $accent; }
        .right { width: 40%; border-left: solid $accent; }
        /* F4 hides the session list so the live pane (or preview) is full-width */
        #main.nolist #table { display: none; }
        #main.nolist #right { width: 100%; }
        #main.nolist .right { width: 100%; }
        #preview { padding: 0 1; height: 1fr; }
        ClaudeTerminal { width: 1fr; height: 1fr; }
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
                    # #right sizing class so the 40% width rule applies.
                    yield RichLog(id="preview", classes="right", wrap=True,
                                  highlight=False, markup=False)
            yield Footer()

        def on_mount(self) -> None:
            # sid -> session map so the preview pane can warm its own cache on
            # demand: rendered and cached on a cache miss.
            self._sid_index = {s.get("id"): s for s in all_sessions}
            self._marked: set = set()        # sids selected for batch launch (Space)
            self._opening_live_sid = None     # sid whose pane should grab focus on open
            self._unread: set = set()         # live panes answered but not yet viewed
            # Live-terminal bookkeeping (None-safe: only used when _LIVE_TERM is
            # available). Pure data structure; the TabbedContent is the UI.
            self._live = (_LIVE_TERM.LiveSessionManager(max_live=self.MAX_LIVE)
                          if _LIVE_TERM is not None else None)
            self._refresh_table()
            self.query_one("#table", DataTable).focus()
            # Optional auto-refresh: RECAP_AUTO_REFRESH=<seconds> re-scans disk on
            # an interval so sessions started elsewhere appear without Ctrl-R.
            _ar = os.environ.get("RECAP_AUTO_REFRESH")
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
            """Worker thread: waits for bg summarization then refreshes table."""
            thread = _bg_summarize.get("thread")
            if thread:
                thread.join()
            if not getattr(self, "is_running", True):
                return   # app quit while summarizing — don't marshal into a dead App
            try:
                self.call_from_thread(self._refresh_table)
                self.call_from_thread(
                    lambda: self.notify("Summaries ready", severity="information", timeout=3)
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
                    return (text in (s.get("ai_title") or "").lower()
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
                    severity="error", title="recap", timeout=15,
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
            cluster_mode = _get_cluster_mode()
            group_by = _get_group_by()
            # Cluster / tree are their own layouts and take precedence; otherwise
            # apply the Claude-Desktop-style grouping (Pinned + date/project).
            grouping = "none" if (cluster_mode or tree_mode) else group_by
            show_proj_col = show_project or (grouping == "project")
            # The split-live list pane is narrow (~34%): use a Desktop-style
            # minimal column set (status + relative-Last + title) and convey the
            # project by tinting the title instead of a wide Project column.
            narrow = _LIVE_TERM is not None
            tree_prefixes: dict[str, str] = {}

            if not tree_mode:
                # Flat, grouped and cluster layouts all honour the live Sort spec
                # (tree is structural and walks itself). visible is a COPY, so this
                # never mutates all_sessions; _build_groups and the cluster pass
                # below only partition, preserving this order via a stable sort.
                # (Flat previously relied on main()'s one-time sort, so changing the
                # Sort dropdown re-ordered nothing until the next launch.)
                _apply_sort(visible, _load_sort())
            if cluster_mode:
                # Global Haiku-based clustering: every session gets assigned
                # to one of ~5-9 themes by a single one-shot LLM call. Cached
                # in ~/.cache/recap/global-clusters.json so the LLM only runs
                # for new sessions (or on `--refresh-clusters`).
                _global_cluster_assign(visible)
                topic_count = Counter(s["primary_topic"] for s in visible)
                # Sort buckets:
                #   0 = named cluster (sorted by size desc, then name asc)
                #   1 = "" (sessions the classifier couldn't place; rare)
                visible.sort(key=lambda s: (
                    1 if not s["primary_topic"] else 0,
                    -topic_count[s["primary_topic"]] if s["primary_topic"] else 0,
                    s["primary_topic"],
                ))
            elif tree_mode:
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
                if cluster_mode:
                    specs.append((col_label("topic", "Topic"), "topic", 16))
                specs.append((col_label("title", "Title"), "title", 80))
            for label, key, width in specs:
                table.add_column(label, key=key, width=width)

            # Precompute one colour-mapping per column so a given project /
            # topic / worktree gets the same colour everywhere it appears.
            project_color: dict[str, str] = {}
            topic_color: dict[str, str] = {}
            wt_color: dict[str, str] = {}
            if show_proj_col or narrow:
                project_color = _build_color_map(
                    (project_short(s["project_name"]) for s in visible),
                    _PROJECT_PALETTE,
                )
            if has_worktrees:
                wt_color = _build_color_map(
                    (s.get("worktree_label") or "" for s in visible
                     if s.get("worktree_label")),
                    _PROJECT_PALETTE,
                )
            if cluster_mode:
                topic_color = _build_color_map(
                    (s.get("primary_topic") or "(none)" for s in visible),
                    _TOPIC_PALETTE,
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
                # A live (recap-hosted) pane's status takes precedence in the
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
                    # ! = answered but not yet viewed (unread); = = viewed / left
                    # as-is. ASCII so the 2-char marker column stays 1-cell-aligned.
                    marker_a = "!" if s["id"] in getattr(self, "_unread", ()) else "="
                else:
                    marker_a = ("@" if s.get("is_open") else "+" if s.get("is_active")
                                else "." if s.get("is_recent") else " ")
                marker_s = ("*" if s["id"] in favorites
                            else "x" if is_hidden else " ")
                marker = f"{marker_a}{marker_s}"
                # Plain title; collapse any newline/tab so a multi-line ai_title
                # doesn't push the row to multiple terminal lines.
                raw_title = (s.get("ai_title") or _first_msg(s) or "")[:80]
                raw_title = (raw_title.replace("\n", " ")
                                       .replace("\r", " ")
                                       .replace("\t", " "))
                if tree_mode and tree_prefixes.get(s["id"]):
                    # Strip ANSI from the tree prefix so the cell stays a plain
                    # str of consistent width.
                    raw_title = _ANSI_RE.sub("", tree_prefixes[s["id"]]) + raw_title
                if s["id"] in getattr(self, "_marked", ()):
                    raw_title = "▣ " + raw_title       # batch-launch selection (Space)
                if narrow:
                    # marker · relative-Last · title (title tinted by project).
                    proj_txt = project_short(s.get("project_name") or "")
                    row = [marker, fmt_last_active(s),
                           Text(raw_title, style=project_color.get(proj_txt, ""))]
                    table.add_row(*row, key=s["id"])
                    if first_session_row is None:
                        first_session_row = n
                    n += 1
                    n_sessions += 1
                    continue
                row = [marker, fmt_ts(s["first_ts"]), fmt_last_active(s)]
                if show_proj_col:
                    proj_txt = project_short(s["project_name"])
                    row.append(Text(proj_txt, style=project_color.get(proj_txt, "")))
                if has_worktrees:
                    wt = s.get("worktree_label") or ""
                    row.append(Text(wt[:11], style=wt_color.get(wt, "") if wt else ""))
                if cluster_mode:
                    topic_full = s.get("primary_topic") or "(none)"
                    row.append(Text(topic_full[:14], style=topic_color.get(topic_full, "")))
                row.append(raw_title)
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
                # No session rows → no row-highlight fires → _update_preview never
                # runs, so the preview pane would keep the last session's content
                # and imply it matched. Clear it and say so.
                try:
                    pv = self.query_one("#preview", RichLog)
                    pv.clear()
                    pv.write("No sessions match the current search / filters.")
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

            # Mode toggles with Rich markup color
            tree_str = "[green]ON[/green]" if _get_tree_mode() else "[dim]OFF[/dim]"
            cluster_str = "[green]ON[/green]" if _get_cluster_mode() else "[dim]OFF[/dim]"
            _GROUP_LABEL = {"none": "[dim]off[/dim]", "date": "[green]Date[/green]",
                            "project": "[green]Project[/green]",
                            "state": "[green]State[/green]"}
            group_str = _GROUP_LABEL.get(_get_group_by(), "[dim]off[/dim]")

            sep = "  [dim]·[/dim]  "
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
            # RAM is an estimate; tune with RECAP_CLAUDE_MB.)
            live_str = ""
            if self._live is not None:
                cnt = self._live.count
                avail = _avail_ram_mb()
                if avail is None:
                    live_str = f"{sep}Live: {cnt}"
                else:
                    floor = float(os.environ.get("RECAP_MIN_FREE_MB", "1536") or "1536")
                    per = float(os.environ.get("RECAP_CLAUDE_MB", "600") or "600")
                    fit = max(0, int((avail - floor) / per)) if per > 0 else 0
                    fit = min(fit, max(0, self._live.max_live - cnt))   # MAX_LIVE backstop
                    _col = "green" if fit > 0 else "red"
                    live_str = (f"{sep}Live: {cnt}  [{_col}]~{fit} fit[/{_col}]"
                                f"  ({avail / 1024:.1f}GB free)")
            text = (f"  {n} sessions{sep}{sort_str}{sep}"
                    f"{scope}{sep}Group: {group_str}{filt_str}{sep}Tree: {tree_str}{sep}Cluster: {cluster_str}{live_str}")
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

        def _update_preview(self, sid: str | None) -> None:
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
                if event.key == "space" and _LIVE_TERM is not None:
                    # Space toggles a batch-launch mark (split-live only — batch
                    # launch opens one live pane per mark). In the default launcher
                    # mode space falls through to search (there's no batch there).
                    self.action_toggle_mark()
                    event.stop()
                    return
                char = event.character
                if char and len(char) == 1 and char.isprintable():
                    search.focus()
                    search.value = search.value + char
                    search.cursor_position = len(search.value)
                    event.stop()
                elif event.key == "backspace" and search.value:
                    search.focus()
                    search.value = search.value[:-1]
                    search.cursor_position = len(search.value)
                    event.stop()
            elif self.focused is search and event.key == "down":
                table.focus()
                event.stop()

        def on_data_table_row_highlighted(self, event) -> None:
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
                # Section-header row: not a session — clear the preview (don't
                # leave a stale one) and show which group this is.
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
                    self.exit(sid)
                return
            # Split-live: if a terminal is focused, Enter belongs to claude.
            # A plain `return` still counts as "handled", so the priority binding
            # would SWALLOW the key; raise SkipAction so Textual forwards it to
            # the focused ClaudeTerminal (whose on_key writes \r to the PTY).
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
                if self._live.at_capacity() and not self._live.has(sid):
                    self.notify(
                        f"opened {opened}; hit the {self._live.max_live}-pane "
                        f"backstop — close some (Ctrl-W) or raise RECAP_MAX_LIVE",
                        severity="warning", timeout=6)
                    break
                self._open_or_attach_live(sid, refresh=False)   # repaint once below
                opened += 1
            self._refresh_table()

        # ── split-live helpers ────────────────────────────────────────────────
        def _focused_terminal(self):
            """Return the focused ClaudeTerminal, or None."""
            if _LIVE_TERM is None:
                return None
            foc = self.focused
            return foc if isinstance(foc, _LIVE_TERM.ClaudeTerminal) else None

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
            assert _LIVE_TERM is not None and self._live is not None
            tabs = self.query_one("#right", TabbedContent)
            pane_id = self._live.pane_id(sid)
            if self._live.has(sid):                  # already running → switch
                tabs.active = pane_id
                self._opening_live_sid = sid
                self.call_after_refresh(lambda: self._focus_live_pane(sid))
                return
            if self._live.at_capacity():
                self.notify(
                    f"hit the {self._live.max_live}-pane backstop; close one "
                    f"(Ctrl-W) or raise RECAP_MAX_LIVE",
                    severity="warning", timeout=6)
                return
            # The real limit is memory (each live pane is a node process tree);
            # warn — but still open — when physical RAM is running low.
            _avail = _avail_ram_mb()
            _floor = float(os.environ.get("RECAP_MIN_FREE_MB", "1536") or "1536")
            if _avail is not None and _avail < _floor:
                self.notify(
                    f"low memory: {_avail:.0f} MB free — each live claude is "
                    f"RAM-heavy; close panes (Ctrl-W) if it slows down",
                    severity="warning", timeout=7)
            s = self._sid_index.get(sid)
            try:
                argv, cwd, env = _build_resume_invocation(sid, all_sessions)
            except Exception as e:
                self.notify(f"could not build resume command: {e!r}",
                            severity="error", timeout=8)
                return
            title = (s.get("ai_title") if s else None) or sid[:8]
            term = _LIVE_TERM.ClaudeTerminal(
                argv, cwd=cwd, env=env, sid=sid, title=title,
                on_status=self._on_live_status, on_exit=self._on_live_exit,
            )
            pane = TabPane(_LIVE_TERM.tab_label(title, "idle"), term, id=pane_id)
            try:
                # A previously-dead pane for this sid may still be mounted (kept
                # for its final frame, only forgotten from the manager); its id
                # collides with the new pane. Drop it first so add_pane doesn't
                # raise DuplicateIds and block re-launching a session that exited.
                try:
                    tabs.remove_pane(pane_id)
                except Exception:
                    pass
                tabs.add_pane(pane)
            except Exception as e:
                # add_pane may have already mounted the widget (on_mount spawned
                # the pty); kill it so a half-opened claude isn't orphaned
                # untracked (it was never register()ed).
                try:
                    self._live.note_reap(term.kill())
                except Exception:
                    pass
                self.notify(f"could not open tab: {e!r}", severity="error",
                            timeout=8)
                return
            self._live.register(sid, term)
            _log(f"live open: {sid[:8]}  ({self._live.count}/{self._live.max_live})")
            tabs.active = pane_id
            # Focus the new pane so cursor keys go straight to claude. The
            # post-open _refresh_table re-emits a row-highlight that races this
            # deferred focus; mark the sid "just opened" so the highlight handler
            # focuses the PANE too — whichever runs first, focus lands on claude
            # (and the _focused_terminal guard then keeps it there).
            self._opening_live_sid = sid
            self.call_after_refresh(lambda: self._focus_live_pane(sid))
            # Reuse the resume side effects: 1-shot teams-notify suppression so
            # the first idle_prompt after launch doesn't ping (mirrors
            # _resume_claude). Best-effort.
            try:
                _add_recap_suppress_session(sid)
            except Exception:
                pass
            # Refresh the table so the marker column shows this row is now live.
            # Batch launch passes refresh=False and repaints ONCE after all opens —
            # N synchronous full rebuilds here serialised the opens and slowed
            # multi-launch (the claude spawns couldn't start back-to-back).
            if refresh:
                self._refresh_table()

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
            # Mark "unread" when claude finishes / needs input in a pane the user
            # isn't currently viewing, so an answered-but-unchecked session is
            # visually distinct (!) from one already viewed and left as-is (=).
            # Cleared when the user activates the tab (on_tabbed_content_tab_activated).
            if status in ("idle", "waiting"):
                try:
                    _active = self.query_one("#right", TabbedContent).active or ""
                except Exception:
                    _active = ""
                if self._live.pane_id(sid) != _active:
                    self._unread.add(sid)
            # Update the tab label.
            try:
                tabs = self.query_one("#right", TabbedContent)
                s = self._sid_index.get(sid)
                title = (s.get("ai_title") if s else None) or sid[:8]
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
            # Viewing a live pane "reads" it: drop its unread badge so ● (answered,
            # unchecked) becomes = (viewed, left as-is). Fires on every activation
            # path — row-highlight switch, F2/F3, click, Enter-open.
            if self._live is None or not getattr(self, "_unread", None):
                return
            try:
                active = self.query_one("#right", TabbedContent).active or ""
            except Exception:
                return
            cleared = [sid for sid in self._unread if self._live.pane_id(sid) == active]
            for sid in cleared:
                self._unread.discard(sid)
            if cleared:
                self._request_refresh()

        def _on_live_exit(self, sid: str) -> None:
            """Called on the UI thread when a pane's child exits. Keep the tab
            (so the user sees the final frame) but re-title it; drop it from the
            active set so a later Enter re-launches instead of attaching to a
            dead PTY."""
            if self._live is None:
                return
            self._unread.discard(sid)   # a dead pane is no longer a live unread answer
            try:
                tabs = self.query_one("#right", TabbedContent)
                s = self._sid_index.get(sid)
                title = (s.get("ai_title") if s else None) or sid[:8]
                pane = tabs.get_pane(self._live.pane_id(sid))
                if pane is not None:
                    pane.label = _LIVE_TERM.tab_label(title, "dead")
            except Exception:
                pass
            self._live.forget(sid)
            self._refresh_table()

        def on_claude_terminal_focus_released(self, event) -> None:
            """The terminal's Ctrl-F1 escape hatch: return focus to the list."""
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
            remaining live pane (so Esc steps through them) or the preview, and
            return focus to the list."""
            if self._live is None or sid is None:
                return
            _log(f"live close: {sid[:8]}")
            t = self._live.get(sid)
            if t is not None:
                try:
                    self._live.note_reap(t.kill())   # reap off-thread; UI stays snappy
                except Exception:
                    pass
            self._live.forget(sid)
            tabs = self.query_one("#right", TabbedContent)
            try:
                tabs.remove_pane(self._live.pane_id(sid))
            except Exception:
                pass
            remaining = list(self._live.statuses().keys())
            try:
                tabs.active = (self._live.pane_id(remaining[-1])
                               if remaining else "tab-preview")
            except Exception:
                pass
            try:
                self.notify(f"closed live session — {len(remaining)} still running",
                            timeout=2)
            except Exception:
                pass
            self.query_one("#table", DataTable).focus()
            self._refresh_table()

        def action_close_all_live(self) -> None:
            # Ctrl-K: close ALL live panes at once (parallel kill) but STAY in
            # recap — unlike Ctrl-C (also quits) or Esc (one at a time). Removes
            # mounted panes incl. dead/exited ones (not just live statuses).
            if self._live is None:
                return
            if isinstance(self.focused, Input):
                raise SkipAction()   # search box: Ctrl+K = kill-line, let it through
            _ft = self._focused_terminal()
            if _ft is not None and not getattr(_ft, "is_dead", False):
                raise SkipAction()   # live claude: Ctrl+K = kill-line, forward it
            tabs = self.query_one("#right", TabbedContent)
            ids = self._live_pane_ids()
            if not ids:
                return
            n = len(ids)
            for pid in ids:
                try:
                    tabs.remove_pane(pid)
                except Exception:
                    pass
            self._live.kill_all()      # kill any still-live terms (parallel, non-blocking)
            try:
                tabs.active = "tab-preview"
            except Exception:
                pass
            self.query_one("#table", DataTable).focus()
            self._refresh_table()
            self.notify(f"closed {n} live tab(s)", timeout=3)

        def action_close_live(self) -> None:
            """Ctrl-W: close the active live tab — but ONLY from the list. In a
            focused claude pane Ctrl+W is readline word-delete, so forward it to
            claude instead of closing the tab (Esc returns focus to the list,
            where Ctrl+W / Esc then closes)."""
            if _LIVE_TERM is None or self._live is None:
                return
            if isinstance(self.focused, Input):
                raise SkipAction()   # search box: Ctrl+W = word-delete, let it through
            _ft = self._focused_terminal()
            if _ft is not None and not getattr(_ft, "is_dead", False):
                raise SkipAction()   # live claude: Ctrl+W = word-delete, forward it
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

        def action_focus_list(self) -> None:
            """Ctrl-L: jump focus back to the session list from a terminal."""
            self.query_one("#table", DataTable).focus()

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
                if term is not None:
                    try:
                        term.focus()
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
                    severity="error", title="recap", timeout=15,
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
                        if _get_cluster_mode():
                            _toggle_cluster_mode()
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

        def action_toggle_view(self) -> None:
            _toggle_view_mode()
            self._refresh_table()

        def action_toggle_tree(self) -> None:
            new_on = _toggle_tree_mode()
            # Tree / cluster / grouping are mutually exclusive layouts — turn
            # the others off to keep the saved state consistent.
            if new_on:
                if _get_cluster_mode():
                    _toggle_cluster_mode()
                _set_group_by("none")
            self._refresh_table()

        def action_toggle_cluster(self) -> None:
            new_on = _toggle_cluster_mode()
            if new_on:
                if _get_tree_mode():
                    _toggle_tree_mode()
                _set_group_by("none")
            self._refresh_table()

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
                if st == "waiting" and prev.get(sid) != "waiting" \
                        and self._live.pane_id(sid) != active:
                    sess = self._sid_index.get(sid) or {}
                    title = (sess.get("ai_title") or _first_msg(sess)
                             or sid[:8])[:50]
                    self.notify(f"needs input: {title}", title="recap", timeout=8)
            changed = (cur != prev)
            self._last_status = cur
            if changed:
                self._request_refresh()

        def action_copy_prompt(self) -> None:
            # Ctrl-Y: copy the selected session's opening user prompt to the
            # clipboard so it can be reused to start a similar task (Crystal-style
            # prompt reuse). OSC-52 first (works over SSH), `clip` as fallback.
            sid = self._cursor_sid()
            if not sid:
                return
            msgs = (self._sid_index.get(sid) or {}).get("real_msgs") or []
            if not msgs:
                self.notify("no user prompt to copy", timeout=3)
                return
            text = msgs[0]
            try:
                self.copy_to_clipboard(text)
            except Exception:
                try:
                    import subprocess
                    subprocess.run("clip", input=text.encode("utf-16-le"),
                                   shell=True, check=False)
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
                                "list (likely transient; Ctrl-R to retry)",
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
            # Quiet periodic re-scan (RECAP_AUTO_REFRESH). Skips while a live pane
            # is focused so it doesn't disrupt typing into claude.
            if reload_fn is None or self._focused_terminal() is not None:
                return
            _invalidate_active_sessions()   # re-read the live registry, not the launch snapshot
            try:
                fresh = reload_fn()
            except Exception as e:
                _log(f"auto-reload failed: {e!r}")
                return
            _log(f"auto-reload: {len(fresh)} sessions")
            self._apply_fresh_sessions(fresh)
            self._refresh_table()

        def action_refresh(self) -> None:
            # Ctrl-R: re-scan ~/.claude/projects for new / updated sessions
            # (recap loads once at startup; this picks up sessions started
            # elsewhere while the picker is open).
            if reload_fn is None:
                self._refresh_table()
                return
            _invalidate_active_sessions()
            try:
                fresh = reload_fn()
            except Exception as e:
                _log(f"Ctrl-R reload failed: {e!r}")
                self.notify(f"refresh failed: {e!r}", severity="error",
                            title="recap", timeout=6)
                return
            _log(f"Ctrl-R reload: {len(fresh)} sessions")
            self._apply_fresh_sessions(fresh)
            self._refresh_table()
            self.notify(f"refreshed — {len(fresh)} sessions", timeout=3)

        def action_cycle_group(self) -> None:
            # Ctrl-O cycles the Claude-Desktop-style grouping: none -> Date ->
            # Project -> none. (The "Group" dropdown sets it explicitly too.)
            new = {"none": "date", "date": "project", "project": "state",
                   "state": "none"}.get(_get_group_by(), "date")
            if new != "none":
                if _get_tree_mode():
                    _toggle_tree_mode()
                if _get_cluster_mode():
                    _toggle_cluster_mode()
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
            # Ctrl-D: show what this session changed (reconstructed from the
            # transcript's Edit/Write records — no git, works for any age).
            self.preview_mode = "changes"
            self._update_preview(self._cursor_sid())

        def action_quit(self) -> None:
            # Esc, on the list, closes live claude sessions ONE AT A TIME so an
            # accidental Esc can't nuke everything; recap quits only once none
            # remain. (Ctrl-C is the force-quit: kill all + exit.) When a
            # terminal is focused Esc belongs to claude — this branch just
            # returns focus to the list (the terminal usually eats Esc first).
            if self._focused_terminal() is not None:
                self.query_one("#table", DataTable).focus()
                return
            if self._live is not None and self._live.count > 0:
                tabs = self.query_one("#right", TabbedContent)
                active = tabs.active or ""
                sid = None
                for s in list(self._live.statuses().keys()):
                    if self._live.pane_id(s) == active:
                        sid = s
                        break
                if sid is None:
                    live_sids = list(self._live.statuses().keys())
                    sid = live_sids[-1] if live_sids else None
                if sid is not None:
                    self._close_live_sid(sid)
                    return
            if self._live is not None:
                self._live.join_reaps()   # don't orphan the last pane's reap
            _log("quit: Esc (no live panes left)")
            self.exit(None)

        def action_quit_all(self) -> None:
            # Ctrl-C: force quit — kill every live claude pane (in PARALLEL) and
            # exit; wait=True joins the reaps so no node worker is orphaned.
            _log(f"quit: force (Ctrl-C), live={self._live.count if self._live else 0}")
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

    # Wrap the app's run() so a Textual / Rich crash never leaves the user
    # at a frozen alternate screen with no way out. On exception: reset
    # terminal modes, leave alternate screen, dump the traceback so we can
    # actually see what blew up (the prior failure was 'screen disappears
    # and doesn't come back' = unrecoverable terminal state).
    try:
        chosen = PickerApp().run()
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


_RECAP_SUPPRESS_PATH = Path.home() / ".claude" / "state" / "_recap_resume_oneshot.json"
_RECAP_SUPPRESS_TTL = 3600.0  # 1h. teams-notify.py 側の RECAP_SUPPRESS_TTL と同期


def _add_recap_suppress_session(session_id: str) -> None:
    """teams-notify.py に「次の Notification 1 件だけ silent」 を伝える 1-shot file.

    `RECAP_RESUME=1` env だけだと session lifetime 全体で Notification 抑止に
    なる過去の事故 (2026-05-24 検出) を構造的に防ぐ。 session_id ごとに 1 件
    だけ「最初の idle_prompt 抑止」 を予約する設計。

    pid 付き tmp + os.replace で並行 recap launch race にも安全。 古い entry
    (>1h) は ついでに prune (= claude が即 crash した stale を回収)。
    """
    import json as _json
    import time as _time
    _RECAP_SUPPRESS_PATH.parent.mkdir(parents=True, exist_ok=True)
    state: dict[str, float] = {}
    if _RECAP_SUPPRESS_PATH.is_file():
        try:
            data = _json.loads(_RECAP_SUPPRESS_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                state = {k: float(v) for k, v in data.items()
                         if isinstance(v, (int, float))}
        except (OSError, _json.JSONDecodeError, ValueError, TypeError):
            state = {}
    now = _time.time()
    state = {k: v for k, v in state.items() if now - v < _RECAP_SUPPRESS_TTL}
    state[session_id] = now
    tmp = _RECAP_SUPPRESS_PATH.with_suffix(f".{os.getpid()}.tmp")
    try:
        tmp.write_text(_json.dumps(state, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, _RECAP_SUPPRESS_PATH)
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


def _build_resume_invocation(
    full_id: str, sessions: list[dict]
) -> tuple[list[str], str | None, dict]:
    """Single source of truth for HOW to launch a resumed `claude`.

    Returns ``(argv, cwd, env)`` where argv = ``[claude_bin, --resume, <id>,
    *auto_perm]``, cwd = the resolved target dir (or None), and env = a prepared
    copy of os.environ (RECAP_RESUME set, ephemeral VIRTUAL_ENV stripped from
    both the var and PATH). Performs NO side effects beyond reading state — no
    chdir, no terminal reset, no printing, no subprocess. Used by BOTH the
    legacy full-takeover path (_resume_claude) and the embedded split-live pane
    (recap_terminal.ClaudeTerminal) so cwd / auto-permission / venv-strip logic
    can never drift between them.
    """
    target_cwd = _resolve_resume_cwd(full_id, sessions)

    # Auto --permission-mode auto for frequent (= trusted) workspaces.
    extra_args: list[str] = []
    if (target_cwd
            and not os.environ.get("RECAP_NO_AUTO_PERMISSION")
            and _canonical_workspace(target_cwd) in _frequent_cwds(sessions)):
        extra_args = ["--permission-mode", "auto"]

    env = os.environ.copy()
    env["RECAP_RESUME"] = "1"   # signal to teams-notify.py: suppress idle_prompt
    # Strip uv's ephemeral VIRTUAL_ENV so the resumed session's `uv` doesn't
    # warn about a stale venv.
    leaked_venv = env.pop("VIRTUAL_ENV", None)
    env.pop("VIRTUAL_ENV_PROMPT", None)
    if leaked_venv:
        bin_dir = "Scripts" if sys.platform == "win32" else "bin"
        venv_bin = str(Path(leaked_venv) / bin_dir)
        cmp = (lambda p: p.lower()) if sys.platform == "win32" else (lambda p: p)
        parts = [p for p in env.get("PATH", "").split(os.pathsep) if cmp(p) != cmp(venv_bin)]
        env["PATH"] = os.pathsep.join(parts)

    claude_bin = shutil.which("claude", path=env.get("PATH")) or "claude"
    argv = [claude_bin, "--resume", full_id, *extra_args]
    return argv, target_cwd, env


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
    # 以降は通常通知」 を実現する。 (env RECAP_RESUME はゲートのみ — 実際の抑止は
    # この file 登録が必須。split-live 経路 3314 と対。これが無いと復帰直後の
    # idle_prompt が毎回 Teams へ誤通知される。)
    try:
        _add_recap_suppress_session(full_id)
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
def _path_to_key(p: Path) -> str:
    s = str(p.resolve()).lower()
    return re.sub(r"[\\/:.\-]+", "-", s).strip("-")


def find_project_dir(cwd: Path) -> Path | None:
    projects_root = Path.home() / ".claude" / "projects"
    cwd_key = _path_to_key(cwd)
    best, best_len = None, 0
    for cand in projects_root.iterdir():
        if not cand.is_dir() or cand.name == "memory":
            continue
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


def _structural_components(a: dict, b: dict) -> dict:
    """Cheap pairwise similarity components (no Haiku required for topic_s=0).
    Reused by _score_relation and cmd_related's prefilter so the formula lives
    in one place."""
    # Detached HEAD records git_branch == "HEAD" — unrelated detached sessions
    # would falsely match. Treat "HEAD" as no-info so it never contributes 1.0.
    ab, bb = a.get("git_branch", ""), b.get("git_branch", "")
    branch_s = 1.0 if (ab and ab != "HEAD" and ab == bb) else 0.0
    return {
        "cwd_s":    _cwd_similarity(a.get("cwd", ""), b.get("cwd", "")),
        "branch_s": branch_s,
        "title_s":  _title_similarity(a, b),
        "gap_min":  _interval_gap_minutes(a, b),
    }


def _score_relation(target: dict, other: dict) -> tuple[float, list[str]]:
    c = _structural_components(target, other)
    time_s   = math.exp(-c["gap_min"] / _TIME_TAU_MIN) if c["gap_min"] != float("inf") else 0.0
    topic_s  = _topic_similarity(target, other)
    structural = (_W_CWD*c["cwd_s"] + _W_BRANCH*c["branch_s"]
                  + _W_TITLE*c["title_s"] + _W_TOPIC*topic_s)
    # Time factor: small floor keeps far-past matches discoverable but heavily damped
    time_factor = 0.10 + 0.90 * time_s
    score = structural * time_factor

    reasons: list[str] = []
    if c["cwd_s"] == 1.0:
        reasons.append("same cwd")
    elif c["cwd_s"] >= 0.7:
        reasons.append("same project")
    if c["branch_s"] == 1.0:
        reasons.append(f"branch {other.get('git_branch','')}")
    gap_label = _fmt_gap(c["gap_min"])
    if gap_label:
        reasons.append(gap_label)
    if c["title_s"] >= 0.3:
        reasons.append(f"title sim {c['title_s']:.0%}")
    if topic_s >= 0.3:
        reasons.append(f"topic sim {topic_s:.0%}")
    return (score, reasons)


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

    Topics are NOT batch-extracted here — for N up to 1000 sessions that would
    be a 30+ minute Haiku call. _topic_similarity returns 0 for missing topics,
    so the forest still builds on cwd/branch/title. Run --related <sid> to get
    topic-aware scoring on demand."""
    by_time = sorted(sessions, key=lambda s: s["first_ts"])
    max_struct = _W_CWD + _W_BRANCH + _W_TITLE + _W_TOPIC   # ceiling of `structural`
    for i, s in enumerate(by_time):
        best_score, best_parent, best_reasons = floor, None, []
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
            score, reasons = _score_relation(s, p)
            if score > best_score:
                best_score, best_parent, best_reasons = score, p, reasons
        s["parent_id"] = best_parent["id"] if best_parent else None
        s["parent_score"] = best_score if best_parent else 0.0
        s["parent_reasons"] = best_reasons


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
    # or 2 cells depending on `cjk_width` settings — which recap can't probe,
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
    for d in PROJECTS_ROOT.iterdir():
        if not d.is_dir() or d.name == "memory":
            continue
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
            # recap's Recency column (untimed ai-title/permission-mode appends bump
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
        description="Claude Code session history viewer. Shows all history by default; "
                    "use --days N to limit. Filters (--days/--here/--all) are one-shot "
                    "unless --save-defaults is also passed.",
        epilog="Environment variables:\n"
               "  RECAP_RESUME=1            set on the resumed `claude` child so teams-notify\n"
               "                            hook suppresses its idle-prompt nag.\n"
               "  RECAP_NO_AUTO_PERMISSION  if set, do NOT auto-apply --permission-mode auto\n"
               "                            on resume even when the target cwd is frequent.\n"
               "  RECAP_FREQ_CWD_MIN=N      minimum session count to flag a cwd as\n"
               "                            \"frequent\" for auto-permission (default 5).\n"
               "  RECAP_CLUSTER_MIN_SIZE=N  (legacy, no longer used by the textual picker —\n"
               "                            cluster mode is now driven by a one-shot\n"
               "                            Haiku classification cached in\n"
               "                            ~/.cache/recap/global-clusters.json; run\n"
               "                            `recap --refresh-clusters` to regenerate).\n"
               "  RECAP_CLUSTER_TARGET_N=N  target theme count for the global classifier\n"
               "                            (clamped to [3,12]; the LLM is asked for\n"
               "                            N±2). Default 7 — Miller (1956)'s '7 ± 2'\n"
               "                            for at-a-glance absolute judgement.\n"
               "  RECAP_CLUSTER_MODEL=...   model used by --refresh-clusters:\n"
               "                            'haiku' (default, ~30 s on ~200 sessions),\n"
               "                            'sonnet' (cleaner partitions, 1-3 min).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
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
                   help="Forget saved --days/--here/--all defaults. Does NOT clear "
                        "hidden/favorite/view-mode/tree-mode/cluster-mode/sort — "
                        "toggle those via Ctrl-x / Ctrl-p / Ctrl-t / Ctrl-g in the "
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
                        "parsed/topic caches; delete ~/.cache/recap/parsed/ for that.")
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
                        "Same effect as Ctrl-t inside the picker.")
    p.add_argument("--toggle-cluster", action="store_true",
                   help="Toggle saved topic-cluster view (persistent). When on, "
                        "sessions are grouped by their most widely-shared cached "
                        "topic keyword. Same effect as Ctrl-g inside the picker. "
                        "Mutually exclusive with tree display.")
    p.add_argument("--cycle-sort", type=int, metavar="N", choices=[1, 2, 3],
                   help="Advance the Nth sort priority to the next column. Persistent. "
                        "In the picker, click a column header instead.")
    p.add_argument("--toggle-sort-dir", type=int, metavar="N", choices=[1, 2, 3],
                   help="Toggle the Nth sort priority's direction (asc/desc). Persistent. "
                        "In the picker, click a sorted column header again to reverse.")
    p.add_argument("--reset-sort", action="store_true",
                   help="Reset all sort priorities to defaults (date desc, then none).")
    p.add_argument("--sync-desktop", action="store_true",
                   help="Create Claude Desktop session-list entries for Terminal/VS Code "
                        "sessions missing from it. Additive + idempotent; never modifies "
                        "~/.claude/projects. Restart Desktop afterwards to see them.")
    p.add_argument("--refresh-clusters", action="store_true",
                   help="Re-run the global Haiku classification used by cluster mode. "
                        "One LLM call buckets every session into 6-10 coherent themes; "
                        "result is cached. Run after a flurry of new sessions or when "
                        "the existing themes feel stale.")
    p.add_argument("--related", metavar="SESSION_ID",
                   help="Show sessions related to SESSION_ID with confidence scores and reasons")
    p.add_argument("--tree", action="store_true",
                   help="Group sessions into an inferred parent/child forest (heuristic, "
                        "scores cwd / branch / title / topic + time decay).")
    p.add_argument("--sidechain", metavar="SESSION_ID",
                   help="Show the in-session sidechain (subagent) tree for SESSION_ID "
                        "using isSidechain+parentUuid metadata (confirmed, not heuristic).")
    args = p.parse_args()

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
                jsonls = sorted(proj.glob("*.jsonl"),
                                key=lambda p: p.stat().st_mtime, reverse=True)
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
    if args.toggle_cluster:
        new_on = _toggle_cluster_mode()
        label = "on (group by topic)" if new_on else "off"
        print(f"  cluster-mode: {_c(label, YELLOW if new_on else GREEN)}", file=sys.stderr)
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
        print(_c("  sort: reset to defaults (date desc)", GREEN), file=sys.stderr)
        return
    if args.refresh_clusters:
        # Need sessions loaded for the classifier. Reuse the same scope the
        # picker uses (defaults to all projects so the cache covers everything).
        since = None if args.days in (None, 0) else datetime.now(tz=timezone.utc) - timedelta(days=args.days)
        projects_root = Path.home() / ".claude" / "projects"
        sessions = []
        for d in projects_root.iterdir():
            if d.is_dir() and d.name != "memory":
                sessions.extend(load_sessions_in_dir(d, since))
        _global_cluster_assign(sessions, force_refresh=True)
        cache = _read_json(GLOBAL_CLUSTERS_FILE, {})
        clusters = cache.get("clusters") or []
        print(_c(f"  clusters refreshed: {len(clusters)} themes for "
                f"{len(cache.get('assignments') or {})} sessions", GREEN),
              file=sys.stderr)
        if clusters:
            print(_c("  " + ", ".join(clusters), DIM), file=sys.stderr)
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
        if OPTIONS_FILE.exists():
            OPTIONS_FILE.unlink()
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
    # overwriting the user's preferred filter (e.g. running `recap --days 7`
    # once would otherwise pin every future `recap` to 7 days).
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
        # The old allowlist missed sort.json and global-clusters.json (expensive
        # Haiku output) and would erase them; match the UUID instead so EVERY
        # settings file — current or future — is safe.
        for f in CACHE_DIR.glob("*.json"):
            if _UUID_RE.fullmatch(f.stem):
                f.unlink()

    since = None if args.days == 0 else datetime.now(tz=timezone.utc) - timedelta(days=args.days)
    projects_root = Path.home() / ".claude" / "projects"
    cwd = Path.cwd()

    try:
        r = subprocess.run(["git", "rev-parse", "--show-toplevel"],
                          capture_output=True, text=True, cwd=cwd, timeout=3,
                          creationflags=NO_WINDOW)
        repo = Path(r.stdout.strip()) if r.returncode == 0 else None
    except Exception:
        repo = None

    sessions = []
    if args.all_projects:
        for d in projects_root.iterdir():
            if d.is_dir() and d.name != "memory":
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
    # user-configurable sort spec is applied AFTER forest building / clustering
    # so it controls only the displayed order.
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
    for s in sessions:
        cached = (_load_cache(s["id"], s["mtime"], s.get("last_ts", ""))
                  if not s.get("is_open") else None)
        s["_cache_hit"] = cached      # reused by the Phase-2 needs_llm probe (avoid a 2nd read)
        s["summary"] = (cached if cached and not _looks_like_refusal(cached)
                        else s["ai_title"] or _first_msg(s))

    # Phase 2 (background): LLM-summarize sessions that had no cache hit.
    # Skipped when --no-summary / --related, or when all are cached.
    if not (args.no_summary or args.related):
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

    # Display mode (flat / nested-tree / topic-cluster). Saved modes are the
    # source of truth so Ctrl-t / Ctrl-g inside the picker can toggle between
    # them in place. CLI --tree is a one-shot override for the initial
    # invocation only — the saved mode stays the source of truth, so a Ctrl-* toggle
    # wins. tree and cluster are mutually exclusive in display: cluster wins
    # when both happen to be on (e.g. saved cluster + CLI --tree).
    cluster_mode = _get_cluster_mode()
    use_tree = (not cluster_mode) and (args.tree or _get_tree_mode()) and len(sessions) <= 1000
    flat = not use_tree
    # Apply user-configurable sort (Ctrl-1/2/3 / Alt-1/2/3 from the picker)
    # only in flat mode. Tree mode is structural (forest topology) and cluster
    # mode is topic-bucketed, so a free-form sort would override their layout.
    if flat and not cluster_mode:
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
        # in-app refresh (Ctrl-R) can re-scan ~/.claude/projects for new/updated
        # sessions without restarting.
        def _reload():
            fresh = []
            if args.all_projects:
                for d in projects_root.iterdir():
                    if d.is_dir() and d.name != "memory":
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
                     cluster_mode=cluster_mode, reload_fn=_reload)


if __name__ == "__main__":
    main()
