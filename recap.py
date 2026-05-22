#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["textual>=0.50"]
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
    """Strip fzf field padding from a session-id arg."""
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
    readers/writers (worker pool, fzf reload) cannot observe a torn write."""
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
UI_MODE_FILE = CACHE_DIR / "ui-mode.txt"
SORT_FILE = CACHE_DIR / "sort.json"
GLOBAL_CLUSTERS_FILE = CACHE_DIR / "global-clusters.json"
OPTIONS_FILE = CACHE_DIR / "options.json"
RESUME_HISTORY_FILE = CACHE_DIR / "resume-history.tsv"

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
    """Toggle membership of `sid` in the set stored at `path`. Returns new state (True=present)."""
    s = _load_set(path)
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


def _get_ui_mode() -> str:
    """Picker UI: 'fzf' (default) or 'textual'."""
    try:
        v = UI_MODE_FILE.read_text(encoding="utf-8").strip()
        return v if v in ("fzf", "textual") else "fzf"
    except Exception:
        return "fzf"


def _set_ui_mode(mode: str) -> None:
    if mode not in ("fzf", "textual"):
        raise ValueError(f"ui mode must be 'fzf' or 'textual', got {mode!r}")
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    UI_MODE_FILE.write_text(mode, encoding="utf-8")


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
        if col == "last":  return s.get("last_ts") or ""
        if col == "proj":  return (s.get("project_name") or "").lower()
        if col == "title": return (s.get("ai_title") or _first_msg(s) or "").lower()
        if col == "turns": return s.get("n_turns") or 0
        if col == "fav":   return 1 if s["id"] in favs else 0
        if col == "topic": return s.get("primary_topic") or "~"
        return 0

    for k in reversed(active):
        sessions.sort(key=lambda s, c=k["col"]: keyfn(s, c),
                      reverse=(k["dir"] == "desc"))

def _load_cache(sid: str, mtime: float) -> str | None:
    d = _read_json(CACHE_DIR / f"{sid}.json", None)
    if d is None:
        return None
    # Cache is valid if file mtime matches (session not updated)
    if abs(d.get("mtime", 0) - mtime) < 1.0:
        return d.get("summary", "") or None
    return None


def _save_cache(sid: str, mtime: float, summary: str):
    _write_json(CACHE_DIR / f"{sid}.json", {
        "session_id": sid,
        "summary": summary,
        "mtime": mtime,
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
    _active_sessions_cache = out
    return out


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
        "n_turns": parsed.get("n_turns", 0),
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
        if _is_hook_session(cached.get("real_msgs", []), cached.get("n_turns", 0)):
            return None
        return _enrich_session(sid, cached, jsonl_path, mtime)

    # Preserve topics across re-parse (JSONL append shouldn't invalidate Haiku-derived topics)
    prior_topics = (cached or {}).get("topics") or []

    first_ts = last_ts = ai_title = cwd = origin_cwd = git_branch = None
    real_msgs: list[str] = []
    n_user = 0

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
                    n_user += 1
                    text = _extract_text(obj.get("message", {}).get("content", ""))
                    if _is_real_user_msg(text):
                        real_msgs.append(text[:800].replace("\n", " "))
    except Exception:
        return None

    if first_ts is None:
        return None

    if _is_hook_session(real_msgs, n_user):
        return None

    parsed = {
        "mtime": mtime,
        "first_ts": first_ts,
        "last_ts": last_ts or first_ts,
        "ai_title": ai_title or "",
        "real_msgs": real_msgs,
        "n_turns": n_user,
        "cwd": cwd or "",
        "origin_cwd": origin_cwd or cwd or "",
        "git_branch": git_branch or "",
    }
    if prior_topics:
        parsed["topics"] = prior_topics
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
    for jsonl in PROJECTS_ROOT.rglob(f"{session_id}.jsonl"):
        try:
            jsonl.unlink()
        except Exception:
            pass


_haiku_missing_warned = False  # surface "claude not on PATH" at most once per run


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
    up against. Stdin has no such limit."""
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
    if s["ai_title"] and _has_cjk(s["ai_title"]):
        return s["ai_title"]

    # Active sessions: JSONL mtime changes every turn → cache always invalid → skip LLM
    if s.get("is_open"):
        return _first_msg(s)

    mtime = s["mtime"]
    cached = _load_cache(s["id"], mtime)
    if cached is not None and not _looks_like_refusal(cached):
        return cached

    if not s["real_msgs"]:
        # No content to summarize — cache empty so we don't retry next time
        _save_cache(s["id"], mtime, "")
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
        _save_cache(s["id"], mtime, summary)
        return summary
    return _first_msg(s)


def summarize_all_parallel(sessions: list[dict], max_workers: int = 5):
    """Summarize all sessions in parallel, showing progress."""
    pending = [s for s in sessions if not s["ai_title"]
               and not s.get("is_open")   # active JSONL mtime changes → cache always stale
               and _load_cache(s["id"], s["mtime"]) is None]
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
    """Strip the home-path prefix ("C--Users-masayuki-morino-") so the column shows
    a recognizable suffix. e.g. C--Users-masayuki-morino-CLI-work-tools → CLI-work-tools."""
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
    lines.extend([
        f"  project:  {found.parent.name}",
        f"  cwd:      {s.get('cwd','')}",
        f"  start:    {fmt_ts(s['first_ts'])}",
        f"  last:     {fmt_last_active(s)} ago  ({fmt_ts(s['last_ts'])})",
        f"  turns:    {s['n_turns']}",
        "",
    ])
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
    lines.append("\033[2mCtrl-f: full conversation  |  Ctrl-s: this summary view\033[0m")
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
                    text = _extract_text(obj.get("message", {}).get("content", ""))
                    if _is_real_user_msg(text):
                        n += 1
                        lines.append(f"\033[36m▶ user [{n}]:\033[0m {text[:1200]}")
                elif t == "assistant":
                    content = obj.get("message", {}).get("content", [])
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
    lines.append("\033[2mCtrl-s: condensed summary  |  Ctrl-f: this full view\033[0m")
    return "\n".join(lines)


def _write_if_stale(path: Path, mtime: float, render) -> None:
    """Write `render()` to path only if path is missing or its mtime drifts from `mtime`."""
    if path.exists():
        try:
            if abs(path.stat().st_mtime - mtime) < 1.0:
                return
        except Exception:
            pass
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(render(), encoding="utf-8")
        os.utime(path, (mtime, mtime))
    except Exception:
        pass


def _write_preview_cache(s: dict) -> None:
    # Pre-render so fzf preview can `cat` instead of cold-starting Python (~150ms → ~5ms per cursor move).
    # Both files are mtime-gated; reloads (Ctrl-x/p/r) skip rewrites for unchanged sessions.
    sid = s["id"]
    mtime = s.get("mtime", 0.0)
    _write_if_stale(PREVIEW_DIR / f"{sid}.txt", mtime, lambda: _render_preview(s))
    _write_if_stale(PREVIEW_FULL_DIR / f"{sid}.txt", mtime, lambda: _render_preview_full(s))


def _preview_impl(session_id: str, cache_dir: Path, render) -> None:
    sid = _trim_sid(session_id)
    if not sid:
        # Cluster-mode group-header / separator rows carry an empty SID column.
        # Returning silently avoids fzf spinning a "loading" indicator while
        # it waits for the preview command to do nothing useful.
        return
    # Exact cache hit (fzf passes the full UUID — fast path)
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
    """Human-friendly 'last activity' column: '5m', '2h', '3d', '04/22'."""
    age = time.time() - s.get("mtime", 0.0)
    if age < 60:
        return "now"
    if age < 3600:
        return f"{int(age/60)}m"
    if age < 86400:
        return f"{int(age/3600)}h"
    if age < 86400 * 7:
        return f"{int(age/86400)}d"
    try:
        dt = datetime.fromisoformat(s["last_ts"].replace("Z", "+00:00"))
        return dt.astimezone().strftime("%m/%d")
    except Exception:
        return ""


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


# ── fzf pick mode ────────────────────────────────────────────────────────────
def build_fzf_lines(sessions: list[dict], repo: Path | None, show_project: bool,
                    flat: bool = False) -> list[str]:
    """Build tab-separated lines for fzf input.
    Format:  display\tsession_id\tsearchable_text
    --with-nth=1 displays only the first field; --nth=1,3 makes fzf search
    against display + searchable_text. Hidden rows are wrapped in dim+gray
    ANSI so the user can see at a glance which entries are hidden."""
    hidden = _load_hidden()
    favorites = _load_favorites()
    view_mode = _get_view_mode()
    walked: list[tuple[dict, str]] = (
        [(s, "") for s in sessions] if flat else _tree_walk(sessions)
    )
    lines = []
    for s, tree_prefix in walked:
        is_hidden = s["id"] in hidden
        if is_hidden and view_mode != "show-hidden":
            continue
        marker = f"{_activity_marker(s)}{_state_marker(s, hidden, favorites)}"
        start = fmt_ts(s["first_ts"])
        last = fmt_last_active(s)
        sid8 = short_id(s["id"])
        proj = project_short(s["project_name"]) if show_project else ""
        prefix_w = visible_len(tree_prefix)
        lbl = truncate_visual(label_for(s), max(20, 65 - prefix_w))
        commits = ""
        if repo:
            cc = git_commits_in_range(s["first_ts"], s["last_ts"], repo)
            if cc:
                commits = "  " + truncate_visual(cc[0], 38)
        if show_project:
            body = f"{start}  [{last:>4}]  [{proj:<14}]  {sid8}  {tree_prefix}{lbl}{commits}"
        else:
            body = f"{start}  [{last:>4}]  {sid8}  {tree_prefix}{lbl}{commits}"
        if is_hidden:
            disp = f"{marker} {HIDDEN_DIM}{body}  (hidden){RESET}"
        else:
            disp = f"{marker} {body}"
        # Searchable content: AI title + all user messages (capped per-session)
        searchable = (s["ai_title"] + "  " + "  ".join(s["real_msgs"]))[:3000]
        searchable = searchable.replace("\t", " ").replace("\n", " ").replace("\r", " ")
        lines.append(f"{disp}\t{s['id']}\t{searchable}")
        _write_preview_cache(s)
    return lines


def _reset_terminal_modes() -> None:
    """Emit ANSI disable sequences for terminal modes fzf may have enabled.

    Targets focus tracking (?1004), all mouse tracking variants (?1000/1002/1003/
    1006/1015), bracketed paste (?2004), and ensures the cursor is visible (?25).
    These are no-ops if the mode is already off — safe to send unconditionally.

    Why this exists: on Windows, fzf occasionally exits without sending the matching
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

    The user thinks of `feature-x` as a branch of `work-tools`, but recap sees
    `work-tools/.worktrees/feature-x/` as a distinct cwd. Without this, every
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
    interest topics (e.g. 'email', 'work-tools') rather than singleton groups
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
        "   pairs like 'work-tools development' + 'tool development'.\n"
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
        # Map back 8-char SIDs the LLM returned to full SIDs.
        sid_lookup = {s["id"][:8]: s["id"] for s in sessions}
        for short, theme in parsed["assignments"].items():
            full = sid_lookup.get(short)
            if full and isinstance(theme, str):
                new_assigns[full] = theme.lower().strip()

        if force_refresh or not cache.get("assignments"):
            assignments = new_assigns
            cache = {"clusters": [c.lower() for c in new_clusters], "assignments": assignments}
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


def build_cluster_lines(sessions: list[dict], repo: Path | None,
                        show_project: bool) -> list[str]:
    """Build fzf lines as a flat list ordered by primary topic.

    Every row is a normal selectable session row with a colored `[topic]`
    prefix. Sessions sharing the same primary topic land next to each other
    (sort key = -member_count, topic_name, -first_ts), so the visual effect
    is the same as a clustered list — but without inserting non-selectable
    header rows that confused fzf's preview pump (causing the spinner-loop
    bug). The topic prefix is also in the searchable column, so `email` in
    the query box still jumps straight to the email cluster."""
    # Use the global Haiku classification (shared with the textual picker)
    # if it's cached; otherwise fall back to the per-session primary topic.
    if (GLOBAL_CLUSTERS_FILE.exists()
            and _read_json(GLOBAL_CLUSTERS_FILE, {}).get("assignments")):
        _global_cluster_assign(sessions)
    else:
        _assign_primary_topic(sessions)
    topic_count: Counter = Counter(s["primary_topic"] for s in sessions)
    # Stable two-pass sort: first by time desc (intra-cluster ordering), then
    # by (-member_count, topic_name) (inter-cluster ordering). '' (no topic)
    # sorts last because we map it to a high-value placeholder name.
    sessions_sorted = sorted(sessions, key=lambda s: s["first_ts"], reverse=True)
    # Sort key tuple:
    #   1) "no topic" sessions go LAST (regardless of how many there are)
    #   2) within "real topic" sessions, larger clusters first, then name asc
    #   3) (stable from step 1's sort): time desc inside the cluster
    sessions_sorted.sort(key=lambda s: (
        1 if not s["primary_topic"] else 0,
        -topic_count[s["primary_topic"]] if s["primary_topic"] else 0,
        s["primary_topic"],
    ))

    hidden = _load_hidden()
    favorites = _load_favorites()
    view_mode = _get_view_mode()
    lines: list[str] = []
    for s in sessions_sorted:
        is_hidden = s["id"] in hidden
        if is_hidden and view_mode != "show-hidden":
            continue
        topic_label = (s["primary_topic"] or "(no topic)")
        topic_label = topic_label[:_CLUSTER_TOPIC_W - 2].ljust(_CLUSTER_TOPIC_W - 2)
        topic_tag = _c(f"[{topic_label}]", MAGENTA)
        marker = f"{_activity_marker(s)}{_state_marker(s, hidden, favorites)}"
        start = fmt_ts(s["first_ts"])
        last = fmt_last_active(s)
        sid8 = short_id(s["id"])
        proj = project_short(s["project_name"]) if show_project else ""
        lbl = truncate_visual(label_for(s), 55)
        commits = ""
        if repo:
            cc = git_commits_in_range(s["first_ts"], s["last_ts"], repo)
            if cc:
                commits = "  " + truncate_visual(cc[0], 38)
        if show_project:
            body = f"{topic_tag} {start}  [{last:>4}]  [{proj:<14}]  {sid8}  {lbl}{commits}"
        else:
            body = f"{topic_tag} {start}  [{last:>4}]  {sid8}  {lbl}{commits}"
        if is_hidden:
            disp = f"{marker} {HIDDEN_DIM}{body}  (hidden){RESET}"
        else:
            disp = f"{marker} {body}"
        searchable = (s["ai_title"] + "  " + "  ".join(s["real_msgs"]))[:3000]
        searchable = searchable.replace("\t", " ").replace("\n", " ").replace("\r", " ")
        # Topic name in searchable so `email`-typed query selects email rows.
        lines.append(f"{disp}\t{s['id']}\t{s['primary_topic']}  {searchable}")
        _write_preview_cache(s)
    return lines


def fzf_pick(sessions: list[dict], repo: Path | None, show_project: bool,
             flat: bool = False, cluster_mode: bool = False,
             reload_args: list[str] | None = None):
    """Pipe session list to fzf and run claude --resume on selection.
    Uses temp file for stdin/stdout so fzf gets a clean tty for its TUI."""
    lines = (build_cluster_lines(sessions, repo, show_project) if cluster_mode
             else build_fzf_lines(sessions, repo, show_project, flat=flat))

    # Write to temp file (binary mode → no CRLF translation that confuses fzf).
    # fzf needs a non-pipe stdin on Windows to start its TUI properly.
    import tempfile
    with tempfile.NamedTemporaryFile(
        mode="wb", delete=False, suffix=".txt"
    ) as tf:
        tf.write(("\n".join(lines) + "\n").encode("utf-8"))
        tmp_path = tf.name

    out_path = tmp_path + ".out"
    env = os.environ.copy()
    env.setdefault("TERM", "xterm-256color")
    result = None
    try:
        try:
            with open(tmp_path, "rb") as stdin_file, open(out_path, "wb") as stdout_file:
                # Reload re-runs recap with the session-source flags captured by main()
                ra = reload_args or ["--list"]
                reload_cmd = "recap " + " ".join(f'"{a}"' if " " in a else a for a in ra)

                # `recap --preview` reads the pre-rendered cache; portable across cmd / bash / pwsh
                preview_cmd      = "recap --preview {2}"
                preview_full_cmd = "recap --preview-full {2}"
                bindings_list = [
                    f"ctrl-x:execute-silent(recap --hide {{2}})+reload({reload_cmd})",
                    f"ctrl-p:execute-silent(recap --favorite {{2}})+reload({reload_cmd})",
                    f"ctrl-r:execute-silent(recap --toggle-view)+reload({reload_cmd})",
                    f"ctrl-t:execute-silent(recap --toggle-tree)+reload({reload_cmd})",
                    f"ctrl-g:execute-silent(recap --toggle-cluster)+reload({reload_cmd})",
                    f"ctrl-f:change-preview({preview_full_cmd})",
                    f"ctrl-s:change-preview({preview_cmd})",
                ]
                # Sort controls. fzf in many distros doesn't recognise ctrl-1
                # through ctrl-9 (terminal can't disambiguate the modifier), so
                # we use the alt modifier exclusively:
                #   Alt-1 / Alt-2 / Alt-3       cycle column at priority N
                #   Alt-q / Alt-w / Alt-e       toggle priority N's direction
                # Reload re-applies the saved sort spec.
                for n, dkey in zip((1, 2, 3), ("q", "w", "e")):
                    bindings_list.append(
                        f"alt-{n}:execute-silent(recap --cycle-sort {n})+reload({reload_cmd})")
                    bindings_list.append(
                        f"alt-{dkey}:execute-silent(recap --toggle-sort-dir {n})+reload({reload_cmd})")
                bindings = ",".join(bindings_list)

                view_tag = "show-hidden" if _get_view_mode() == "show-hidden" else "default"
                # tree/cluster are mutually exclusive in the *display*; show the
                # active one so the user knows which keystroke does what.
                if cluster_mode:
                    layout_tag = "Ctrl-g:cluster(ON)"
                else:
                    tree_tag = "flat" if flat else "nested"
                    layout_tag = f"Ctrl-t:tree(now:{tree_tag})  Ctrl-g:cluster(off)"
                # Sort indicator: `[1↓date] [2↑proj] [3 -]` style. Down arrow
                # for desc, up for asc, `-` placeholder for inactive priority.
                # Only meaningful in flat-non-cluster mode; muted otherwise.
                sort_active = (not cluster_mode) and flat
                sort_tag = "  Sort:"
                for i, k in enumerate(_load_sort(), 1):
                    if k["col"] == "-":
                        sort_tag += f" [{i} -]"
                    else:
                        arrow = "v" if k["dir"] == "desc" else "^"
                        sort_tag += f" [{i}{arrow}{k['col']}]"
                if not sort_active:
                    sort_tag = _c(sort_tag + "(N/A)", DIM)
                header = (f"Enter:resume  Ctrl-p:*fav  Ctrl-x:hide  "
                          f"Ctrl-r:hidden(now:{view_tag})  "
                          f"{layout_tag}  "
                          f"Ctrl-f/s:full/summary  Ctrl-C:cancel"
                          f"\nAlt-1/2/3:cycle sort  Alt-q/w/e:toggle dir{sort_tag}")
                result = subprocess.run(
                    # Layout (default fzf, preview pane below):
                    #   top:    list pane (the session / topic selector)
                    #   middle: prompt + header (= keybinding hints), full width
                    #   bottom: preview pane
                    # `--layout=default` (the fzf default) keeps the prompt at
                    # the bottom of the list pane, which puts it just above the
                    # preview pane → header sits in the screen middle, list
                    # above it, preview below it.
                    ["fzf", "--ansi", "--no-sort",
                     "--delimiter=\t", "--with-nth=1", "--nth=1,3",
                     "--preview", preview_cmd,
                     "--preview-window", "down:55%:wrap",
                     "--bind", bindings,
                     "--header", header,
                     "--prompt", "Search> "],
                    stdin=stdin_file,
                    stdout=stdout_file,
                    stderr=None,
                    env=env,
                )
        except FileNotFoundError:
            if sys.platform == "win32":
                hint = "winget install junegunn.fzf"
            elif sys.platform == "darwin":
                hint = "brew install fzf"
            else:
                hint = "sudo apt install fzf  # or: brew install fzf"
            print(f"fzf not found. Install: {hint}", file=sys.stderr)
            sys.exit(1)

        try:
            with open(out_path, "rb") as f:
                chosen = f.read().decode("utf-8", errors="replace").strip()
        except Exception:
            chosen = ""
    finally:
        # Defensively reset terminal modes fzf may have left enabled. Observed on
        # Windows: fzf exit (esp. via Ctrl-C / non-zero or any exception path)
        # sometimes fails to disable focus tracking (\e[?1004h) and SGR mouse
        # (\e[?1006h), so the shell prompt then receives stray '[I' / 'm' from
        # focus and mouse events. Sending the disable sequences here is idempotent
        # and safe on any VT100-compatible terminal. In `finally` so even an
        # unexpected subprocess error doesn't leak modes back to the shell.
        _reset_terminal_modes()
        for p in (tmp_path, out_path):
            try:
                os.unlink(p)
            except Exception:
                pass

    if result is None or result.returncode != 0 or not chosen:
        return
    full_id = chosen.split("\t")[1].strip()   # field 1 = UUID (0=display, 2=searchable)
    if not full_id:
        return
    _resume_claude(full_id, sessions)


def textual_pick(sessions: list[dict], repo: Path | None, show_project: bool,
                 flat: bool = False, cluster_mode: bool = False,
                 reload_args: list[str] | None = None) -> None:
    """Textual-based picker (Phase 3 — adds mouse-click column sort).

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
      Ctrl-r       toggle hidden vis     Ctrl-t        toggle tree display
      Ctrl-g       toggle cluster        Ctrl-f / s    full / summary preview
      Alt-1/2/3    cycle sort col N      Alt-q/w/e     toggle priority N dir

    Mouse — click a column header to promote it to priority 1; click the
    same header again to flip its direction. The previous priority 1
    becomes priority 2, and so on (priority 3 drops off). The column
    label shows the current state, e.g. "Start 1v" = priority 1, desc.
    """
    try:
        from textual.app import App, ComposeResult
        from textual.binding import Binding
        from textual.containers import Horizontal
        from textual.widgets import DataTable, Footer, Input, RichLog
        from rich.text import Text
    except ImportError as e:
        print(_c(f"  textual not available ({e}) — falling back to fzf", YELLOW),
              file=sys.stderr)
        fzf_pick(sessions, repo, show_project, flat=flat,
                 cluster_mode=cluster_mode, reload_args=reload_args)
        return

    all_sessions = list(sessions)

    # Send Textual's internal logs to a file so we have a trail to inspect
    # when something goes wrong inside the framework's event loop.
    os.environ.setdefault("TEXTUAL_LOG", str(CACHE_DIR / "textual-debug.log"))

    class PickerApp(App):
        TITLE = "recap"
        BINDINGS = [
            Binding("escape", "quit", "Cancel"),
            Binding("ctrl+c", "quit", show=False),
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
            Binding("ctrl+p", "toggle_fav", "*Fav"),
            Binding("ctrl+r", "toggle_view", "Show hidden"),
            Binding("ctrl+t", "toggle_tree", "Tree"),
            Binding("ctrl+g", "toggle_cluster", "Cluster"),
            Binding("ctrl+f", "preview_full", "Full"),
            Binding("ctrl+s", "preview_summary", "Summary"),
            Binding("alt+1", "cycle_sort('1')", "Sort1"),
            Binding("alt+2", "cycle_sort('2')", "Sort2"),
            Binding("alt+3", "cycle_sort('3')", "Sort3"),
            Binding("alt+q", "toggle_dir('1')", "Dir1"),
            Binding("alt+w", "toggle_dir('2')", "Dir2"),
            Binding("alt+e", "toggle_dir('3')", "Dir3"),
        ]
        CSS = """
        Screen { layout: vertical; }
        #search { dock: top; height: 3; border: tall $accent; }
        #main { layout: horizontal; height: 1fr; }
        #table { width: 60%; }
        #preview { width: 40%; padding: 0 1; border-left: solid $accent; }
        """

        preview_mode = "summary"   # "summary" or "full"

        def compose(self) -> ComposeResult:
            yield Input(placeholder="Search title / msg / SID / proj    "
                                    "•  :fav  :hidden  :open  :active  :recent",
                        id="search")
            with Horizontal(id="main"):
                yield DataTable(cursor_type="row", zebra_stripes=True, id="table")
                yield RichLog(id="preview", wrap=True, highlight=False, markup=False)
            yield Footer()

        def on_mount(self) -> None:
            self._refresh_table()
            self.query_one("#table", DataTable).focus()
            self._update_subtitle()

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
                if ":active" in statuses and not s.get("is_active"):
                    return False
                if ":recent" in statuses and not s.get("is_recent"):
                    return False
                if text:
                    return (text in (s.get("ai_title") or "").lower()
                            or text in " ".join(s.get("real_msgs") or []).lower()
                            or text in sid
                            or text in (s.get("project_name") or "").lower())
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
                import traceback
                self.notify(
                    f"refresh failed: {e!r}\n{traceback.format_exc()[-400:]}",
                    severity="error", title="recap", timeout=15,
                )

        def _do_refresh_table(self) -> None:
            table = self.query_one("#table", DataTable)
            saved_cursor = table.cursor_row
            table.clear(columns=True)

            # Read state first; layout mode decides whether the Topic column
            # is added, so we need to know it before defining columns.
            query = self.query_one("#search", Input).value
            visible = self._filter(query)
            hidden = _load_hidden()
            favorites = _load_favorites()
            view_mode = _get_view_mode()
            tree_mode = _get_tree_mode() and len(all_sessions) <= 1000
            cluster_mode = _get_cluster_mode()
            tree_prefixes: dict[str, str] = {}

            if cluster_mode:
                # Apply the user sort first so cluster members keep that order
                # inside each group (stable sort below preserves it).
                _apply_sort(visible, _load_sort())
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
            # else: keep main()'s sort order (date desc + user sort spec)

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
            specs: list[tuple[str, str, int]] = [
                ("", "_marker", 3),
                (col_label("date", "Start"), "date", 13),
                (col_label("last", "Last"), "last", 7),
            ]
            if show_project:
                specs.append((col_label("proj", "Project"), "proj", 17))
            if cluster_mode:
                specs.append((col_label("topic", "Topic"), "topic", 16))
            specs.append((col_label("title", "Title"), "title", 80))
            for label, key, width in specs:
                table.add_column(label, key=key, width=width)

            # Precompute one colour-mapping per column so a given project /
            # topic gets the same colour everywhere it appears, and (when the
            # unique count fits the palette) distinct values get distinct
            # colours.
            project_color: dict[str, str] = {}
            topic_color: dict[str, str] = {}
            if show_project:
                project_color = _build_color_map(
                    (project_short(s["project_name"]) for s in visible),
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
            n = 0
            for s in visible:
                is_hidden = s["id"] in hidden
                if is_hidden and not show_hidden:
                    continue
                marker_a = ("@" if s.get("is_open") else "+" if s.get("is_active")
                            else "." if s.get("is_recent") else " ")
                marker_s = ("*" if s["id"] in favorites
                            else "x" if is_hidden else " ")
                marker = f"{marker_a}{marker_s}"
                row = [marker, fmt_ts(s["first_ts"]), fmt_last_active(s)]
                if show_project:
                    proj_txt = project_short(s["project_name"])
                    row.append(Text(proj_txt, style=project_color.get(proj_txt, "")))
                if cluster_mode:
                    topic_full = s.get("primary_topic") or "(none)"
                    row.append(Text(topic_full[:14], style=topic_color.get(topic_full, "")))
                # Plain title cell; collapse any newline/tab so a multi-line
                # ai_title doesn't push the row to multiple terminal lines.
                raw_title = (s.get("ai_title") or _first_msg(s) or "")[:80]
                raw_title = (raw_title.replace("\n", " ")
                                       .replace("\r", " ")
                                       .replace("\t", " "))
                if tree_mode and tree_prefixes.get(s["id"]):
                    # Strip ANSI from the tree prefix so the cell stays a plain
                    # str of consistent width.
                    prefix = _ANSI_RE.sub("", tree_prefixes[s["id"]])
                    title_cell = f"{prefix}{raw_title}"
                else:
                    title_cell = raw_title
                row.append(title_cell)
                table.add_row(*row, key=s["id"])
                n += 1
            if n and 0 <= saved_cursor < n:
                try:
                    table.move_cursor(row=saved_cursor)
                except Exception:
                    pass
            self._update_subtitle()

        def _update_subtitle(self) -> None:
            table = self.query_one("#table", DataTable)
            sort_keys = _load_sort()
            parts = []
            for i, k in enumerate(sort_keys, 1):
                if k["col"] == "-":
                    parts.append(f"[{i} -]")
                else:
                    arrow = "v" if k["dir"] == "desc" else "^"
                    parts.append(f"[{i}{arrow}{k['col']}]")
            view = _get_view_mode()

            # Layout indicator: plain flat / tree / cluster. For cluster mode
            # also surface the top groups + sizes so the user can see at a
            # glance that the grouping is actually applied (the "I toggled
            # cluster but nothing visibly changed" symptom).
            if _get_tree_mode():
                layout = "tree"
            elif _get_cluster_mode():
                # Read assignments from the global classification cache so
                # the subtitle reflects exactly what's on screen.
                cache = _read_json(GLOBAL_CLUSTERS_FILE, {})
                assigns = cache.get("assignments") or {}
                if assigns:
                    cluster_counts = Counter(assigns.values())
                    top = cluster_counts.most_common(3)
                    bits = ", ".join(f"{t}({n})" for t, n in top)
                    layout = f"cluster {bits}"
                else:
                    layout = "cluster (run --refresh-clusters)"
            else:
                layout = "flat"

            self.sub_title = (f"{table.row_count} sessions  "
                              f"view:{view}  layout:{layout}  "
                              f"sort:{' '.join(parts)}")

        def _cursor_sid(self) -> str | None:
            table = self.query_one("#table", DataTable)
            if table.row_count == 0:
                return None
            try:
                row_key, _ = table.coordinate_to_cell_key((table.cursor_row, 0))
                return str(row_key.value) if row_key else None
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
            if cache_file.exists():
                preview.write(Text.from_ansi(cache_file.read_text(encoding="utf-8")))
            else:
                preview.write(f"(no cached preview for {sid[:8]} — open the session once to populate the cache)")

        # ── events ──────────────────────────────────────────────────────────

        def on_key(self, event) -> None:
            # fzf-like: typing while the table is focused redirects into the
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
            self._update_preview(sid)

        def action_resume(self) -> None:
            sid = self._cursor_sid()
            if sid:
                self.exit(sid)

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

        def on_input_changed(self, event) -> None:
            self._refresh_table()

        # ── actions ─────────────────────────────────────────────────────────

        def action_toggle_hide(self) -> None:
            sid = self._cursor_sid()
            if sid:
                _toggle_in_set(HIDDEN_FILE, sid)
                self._refresh_table()

        def action_toggle_fav(self) -> None:
            sid = self._cursor_sid()
            if sid:
                _toggle_in_set(FAVORITE_FILE, sid)
                self._refresh_table()

        def action_toggle_view(self) -> None:
            _toggle_view_mode()
            self._refresh_table()

        def action_toggle_tree(self) -> None:
            new_on = _toggle_tree_mode()
            # Tree and cluster are mutually exclusive in display — turn off the
            # other to keep the saved state consistent.
            if new_on and _get_cluster_mode():
                _toggle_cluster_mode()
            self._refresh_table()

        def action_toggle_cluster(self) -> None:
            new_on = _toggle_cluster_mode()
            if new_on and _get_tree_mode():
                _toggle_tree_mode()
            self._refresh_table()

        def action_preview_full(self) -> None:
            self.preview_mode = "full"
            self._update_preview(self._cursor_sid())

        def action_preview_summary(self) -> None:
            self.preview_mode = "summary"
            self._update_preview(self._cursor_sid())

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
        print(_c("  fall back to fzf with: recap --ui fzf", YELLOW), file=sys.stderr)
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


def _resume_claude(full_id: str, sessions: list[dict]) -> None:
    """Resume `claude --resume <full_id>` from the right cwd. Self-terminating:
    `sys.exit`s with claude's return code. Shared by every picker frontend so
    cwd resolution / auto-permission / venv strip / terminal reset stay in
    exactly one place."""
    # Try origin_cwd first (where Claude originally indexed the session — required
    # for --resume to find it). Fall back to last cwd, then to an existing user
    # directory whose path-key matches the JSONL's project dir name (handles
    # sessions whose original cwd was deleted but a sibling/parent still exists).
    selected = next((s for s in sessions if s["id"] == full_id), None)
    candidates = []
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
    target_cwd = candidates[0] if candidates else None
    if not target_cwd:
        print(_c(f"  warn: session's recorded cwd no longer exists — running from current dir", YELLOW),
              file=sys.stderr)

    # Auto --permission-mode auto for frequent (= trusted) workspaces.
    extra_args: list[str] = []
    auto_perm_note = ""
    if (target_cwd
            and not os.environ.get("RECAP_NO_AUTO_PERMISSION")
            and _canonical_workspace(target_cwd) in _frequent_cwds(sessions)):
        extra_args = ["--permission-mode", "auto"]
        auto_perm_note = _c("  [--permission-mode auto: frequent cwd]", DIM)

    hist_path = _persist_resume_id(full_id, target_cwd)
    print(f"\nResuming {full_id}"
          + (f"  (cwd: {target_cwd})" if target_cwd else "")
          + f"\n  resume ID logged → {hist_path}"
          + (f"\n{auto_perm_note}" if auto_perm_note else ""))
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
    claude_bin = shutil.which("claude", path=env.get("PATH")) or "claude"
    claude_argv = [claude_bin, "--resume", full_id, *extra_args]

    # Pause-on-exit wrapper: wraps claude in `cmd.exe /c "... & pause"` (or
    # `/bin/sh -c "...; read"` on POSIX) so the terminal window stays open
    # after claude exits. wezterm shortcut-launched windows close the moment
    # the foreground process dies — without pause the user loses the chance
    # to scroll back and grab the resume ID after an unstable session.
    # The shell is still process-replaced via execvpe so there is no python
    # parent blocking on subprocess.run (leak prevention from the prior fix
    # remains intact). Opt out with RECAP_NO_PAUSE_ON_EXIT=1.
    no_pause = os.environ.get("RECAP_NO_PAUSE_ON_EXIT") == "1"
    if no_pause:
        exec_bin = claude_bin
        exec_argv = claude_argv
    elif sys.platform == "win32":
        inner = subprocess.list2cmdline(claude_argv)
        wrapped = (f'{inner} & echo. & echo --- claude exited '
                   f'(scroll up to copy resume ID) --- & pause')
        exec_bin = "cmd.exe"
        exec_argv = ["cmd.exe", "/c", wrapped]
    else:
        import shlex
        inner = " ".join(shlex.quote(a) for a in claude_argv)
        wrapped = (f"{inner}; printf '\\n--- claude exited "
                   f"(scroll up to copy resume ID) ---\\n'; read -r _")
        exec_bin = "/bin/sh"
        exec_argv = ["/bin/sh", "-c", wrapped]

    try:
        os.execvpe(exec_bin, exec_argv, env)
    except FileNotFoundError:
        print(_c(f"  error: {exec_bin} not on PATH", RED), file=sys.stderr)
        _reset_terminal_modes()
        sys.exit(127)
    except OSError as e:
        print(_c(f"  error: failed to launch claude ({e})", RED), file=sys.stderr)
        _reset_terminal_modes()
        sys.exit(1)


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
                text = _extract_text(obj.get("message", {}).get("content", "")) or ""
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
    for i, s in enumerate(by_time):
        best_score, best_parent, best_reasons = floor, None, []
        for j in range(i):
            p = by_time[j]
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

    roots.sort(key=newest_in_tree, reverse=True)
    for i, root in enumerate(roots):
        walk(root, "", i == len(roots) - 1)
    return out


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
               "  RECAP_CLUSTER_MIN_SIZE=N  (legacy, no longer used by --ui textual —\n"
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
                        "toggle those via Ctrl-x / Ctrl-p / Ctrl-r / Ctrl-t / Ctrl-g "
                        "/ Alt-1..3 / Alt-q..e in the picker (or the matching "
                        "--toggle-* / --cycle-sort / --reset-sort flags).")
    p.add_argument("--save-defaults", action="store_true",
                   help="Persist the current --days/--here/--all values as new defaults. "
                        "Without this flag, CLI args are one-shot and saved options stay untouched.")
    p.add_argument("--pick", action="store_true",
                   help="Open the interactive fzf picker. This is the default when "
                        "no other action flag is given; --pick is kept as an explicit "
                        "no-op for clarity in shell aliases.")
    p.add_argument("--table", action="store_true",
                   help="Show static table instead of fzf picker")
    p.add_argument("--project", metavar="PATH")
    p.add_argument("--no-summary", action="store_true",
                   help="Skip Haiku summarization (use AI title or first user msg)")
    p.add_argument("--refresh-summary", action="store_true",
                   help="Discard cached Haiku summaries and regenerate. Does NOT touch "
                        "parsed/topic caches; delete ~/.cache/recap/parsed/ for that.")
    p.add_argument("--preview", metavar="SESSION_ID",
                   help="Print session content preview (used internally by --pick)")
    p.add_argument("--preview-full", metavar="SESSION_ID",
                   help="Print full conversation preview (used internally by --pick)")
    p.add_argument("--list", action="store_true",
                   help="Emit fzf-formatted lines (used internally by --pick reload). "
                        "Implies --no-summary (skips Haiku).")
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
                        "Same effect as Alt-N inside the picker.")
    p.add_argument("--toggle-sort-dir", type=int, metavar="N", choices=[1, 2, 3],
                   help="Toggle the Nth sort priority's direction (asc/desc). Persistent. "
                        "Same effect as Alt-q/w/e (for priority 1/2/3) inside the picker.")
    p.add_argument("--reset-sort", action="store_true",
                   help="Reset all sort priorities to defaults (date desc, then none).")
    p.add_argument("--refresh-clusters", action="store_true",
                   help="Re-run the global Haiku classification used by cluster mode. "
                        "One LLM call buckets every session into 6-10 coherent themes; "
                        "result is cached. Run after a flurry of new sessions or when "
                        "the existing themes feel stale.")
    p.add_argument("--ui", choices=["fzf", "textual"], default=None,
                   help="Picker UI: 'fzf' (default, fast, external binary) or "
                        "'textual' (Python TUI with mouse support, in development). "
                        "Persistent — sets the new default. Use without --ui to "
                        "fall back to fzf.")
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
        # Only delete summary cache files (named after session UUIDs).
        # Must NOT touch hidden.json/favorite.json/options.json.
        protected = {HIDDEN_FILE.name, FAVORITE_FILE.name, OPTIONS_FILE.name}
        for f in CACHE_DIR.glob("*.json"):
            if f.name in protected:
                continue
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

    # Initial chronological sort gives _build_forest a deterministic order; the
    # user-configurable sort spec is applied AFTER forest building / clustering
    # so it controls only the displayed order.
    sessions.sort(key=lambda s: s["first_ts"], reverse=True)

    if not sessions:
        period = "all history" if args.days == 0 else f"last {args.days} days"
        print(f"No sessions in {period}.")
        return

    # --list is invoked by fzf reload bindings (Ctrl-x/p/r). Reloads should be
    # instant; skip Haiku entirely and rely on whatever cache the initial run
    # already filled. Cache misses fall back to the first user message.
    if args.no_summary or args.related or args.list:
        for s in sessions:
            cached = _load_cache(s["id"], s["mtime"]) if not s.get("is_open") else None
            if cached and not _looks_like_refusal(cached):
                s["summary"] = cached
            else:
                s["summary"] = s["ai_title"] or _first_msg(s)
    else:
        summarize_all_parallel(sessions)

    if args.related:
        cmd_related(args.related, sessions)
        return

    # Always build the cross-session forest so the fzf preview header can surface
    # the top-related session, even when --tree (nested display) is off. The forest
    # is O(N²) but each comparison is a cheap structural score (no Haiku); guard
    # with N <= 1000 to keep startup snappy on very large histories.
    if len(sessions) <= 1000:
        _build_forest(sessions)
    else:
        for s in sessions:
            s["parent_id"] = None
            s["parent_score"] = 0.0
            s["parent_reasons"] = []

    # Display mode (flat / nested-tree / topic-cluster). Saved modes are the
    # source of truth so Ctrl-t / Ctrl-g inside the picker can toggle between
    # them via reload. CLI --tree is a one-shot override for the initial
    # invocation only — it is NOT carried into reload_args, so a Ctrl-* toggle
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
    if args.list:
        # Emit fzf-formatted lines without launching fzf (used by reload)
        emitter = (build_cluster_lines(sessions, repo, args.all_projects) if cluster_mode
                   else build_fzf_lines(sessions, repo, args.all_projects, flat=flat))
        for line in emitter:
            sys.stdout.write(line + "\n")
        return

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
        # Default: interactive picker. UI choice = CLI --ui (one-shot, also
        # persists) > saved ui-mode > 'fzf' fallback.
        if args.ui:
            _set_ui_mode(args.ui)
            ui_mode = args.ui
        else:
            ui_mode = _get_ui_mode()
        reload_args = ["--list", "--days", str(args.days)]
        if args.here:
            reload_args.append("--here")
        if args.project:
            reload_args += ["--project", args.project]
        # Intentionally NOT propagating --tree into reload_args: the saved
        # tree-mode (toggled via Ctrl-t inside the picker) is the source of
        # truth on reload. Otherwise an initial `recap --tree` would override
        # subsequent toggles forever.
        if ui_mode == "textual":
            textual_pick(sessions, repo, args.all_projects, flat=flat,
                         cluster_mode=cluster_mode, reload_args=reload_args)
        else:
            fzf_pick(sessions, repo, args.all_projects, flat=flat,
                     cluster_mode=cluster_mode, reload_args=reload_args)


if __name__ == "__main__":
    main()
