#!/usr/bin/env python3
"""
recap — Claude Code session history viewer with LLM summarization
Usage:
  recap [--days N] [--all-projects] [--pick] [--project PATH]
        [--no-summary] [--refresh-summary]
"""
import argparse
import io
import json
import os
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from pathlib import Path

# Force UTF-8 output on Windows
if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

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

def _c(text, *codes):
    return "".join(codes) + str(text) + RESET


# ── Cache ───────────────────────────────────────────────────────────────────
CACHE_DIR = Path.home() / ".cache" / "recap"
SUMMARY_MODEL = "haiku"
HIDDEN_FILE = CACHE_DIR / "hidden.json"
FAVORITE_FILE = CACHE_DIR / "favorite.json"
VIEW_MODE_FILE = CACHE_DIR / "view-mode.txt"
OPTIONS_FILE = CACHE_DIR / "options.json"
PARSED_DIR = CACHE_DIR / "parsed"


def _load_options() -> dict:
    try:
        return json.loads(OPTIONS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_options(opts: dict) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    OPTIONS_FILE.write_text(json.dumps(opts, indent=2), encoding="utf-8")


def _load_hidden() -> set[str]:
    try:
        return set(json.loads(HIDDEN_FILE.read_text(encoding="utf-8")))
    except Exception:
        return set()


def _save_hidden(ids: set[str]) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    HIDDEN_FILE.write_text(json.dumps(sorted(ids), indent=2), encoding="utf-8")


def _toggle_hide(sid: str) -> str:
    h = _load_hidden()
    if sid in h:
        h.remove(sid)
        action = "unhidden"
    else:
        h.add(sid)
        action = "hidden"
    _save_hidden(h)
    return action


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


def _load_favorites() -> set[str]:
    try:
        return set(json.loads(FAVORITE_FILE.read_text(encoding="utf-8")))
    except Exception:
        return set()


def _save_favorites(ids: set[str]) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    FAVORITE_FILE.write_text(json.dumps(sorted(ids), indent=2), encoding="utf-8")


def _toggle_favorite(sid: str) -> str:
    favs = _load_favorites()
    if sid in favs:
        favs.remove(sid)
        action = "unstarred"
    else:
        favs.add(sid)
        action = "starred"
    _save_favorites(favs)
    return action

def _load_cache(sid: str, mtime: float) -> str | None:
    cache_file = CACHE_DIR / f"{sid}.json"
    if not cache_file.exists():
        return None
    try:
        d = json.loads(cache_file.read_text(encoding="utf-8"))
        # Cache is valid if file mtime matches (session not updated)
        if abs(d.get("mtime", 0) - mtime) < 1.0:
            return d.get("summary", "") or None
    except Exception:
        pass
    return None

def _save_cache(sid: str, mtime: float, summary: str):
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_file = CACHE_DIR / f"{sid}.json"
    cache_file.write_text(json.dumps({
        "session_id": sid,
        "summary": summary,
        "mtime": mtime,
        "model": SUMMARY_MODEL,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }, ensure_ascii=False), encoding="utf-8")


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
        for b in content:
            if isinstance(b, dict) and b.get("type") == "text":
                return b.get("text", "")
    return ""


def _is_real_user_msg(text: str) -> bool:
    if not text or len(text) < 15:
        return False
    if any(m in text for m in SKIP_MARKERS):
        return False
    return True


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


def parse_session(jsonl_path: Path) -> dict | None:
    sid = jsonl_path.stem
    mtime = jsonl_path.stat().st_mtime

    # Disk cache: skip JSONL re-parsing if mtime is unchanged
    cache_file = PARSED_DIR / f"{sid}.json"
    if cache_file.exists():
        try:
            c = json.loads(cache_file.read_text(encoding="utf-8"))
            if abs(c.get("mtime", 0) - mtime) < 0.5:
                import time as _time
                age_sec = _time.time() - mtime
                active = _load_active_sessions()
                status = active.get(sid, "")
                return {
                    "id": sid,
                    "first_ts": c["first_ts"],
                    "last_ts": c["last_ts"],
                    "ai_title": c.get("ai_title", ""),
                    "real_msgs": c.get("real_msgs", []),
                    "n_turns": c.get("n_turns", 0),
                    "jsonl_path": jsonl_path,
                    "cwd": c.get("cwd", ""),
                    "is_open": sid in active,
                    "session_status": status,           # "busy" / "idle" / ""
                    "is_active": (sid in active) or age_sec < 300,
                    "is_recent": age_sec < 1800,
                }
        except Exception:
            pass

    first_ts = last_ts = ai_title = cwd = None
    real_msgs: list[str] = []
    n_user = 0

    try:
        with open(jsonl_path, "rb") as f:
            for line in f:
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
                if cwd is None and isinstance(obj.get("cwd"), str):
                    cwd = obj["cwd"]
                if t == "ai-title" and ai_title is None:
                    ai_title = obj.get("aiTitle", "")
                if t == "user":
                    n_user += 1
                    text = _extract_text(obj.get("message", {}).get("content", ""))
                    if _is_real_user_msg(text):
                        # Limit per-message length to keep prompts small
                        real_msgs.append(text[:800].replace("\n", " "))
    except Exception:
        return None

    if first_ts is None:
        return None

    import time as _time
    age_sec = _time.time() - mtime

    # Persist parse result for next run
    try:
        PARSED_DIR.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(json.dumps({
            "mtime": mtime,
            "first_ts": first_ts,
            "last_ts": last_ts or first_ts,
            "ai_title": ai_title or "",
            "real_msgs": real_msgs,
            "n_turns": n_user,
            "cwd": cwd or "",
        }, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass

    active = _load_active_sessions()
    status = active.get(sid, "")
    return {
        "id": sid,
        "first_ts": first_ts,
        "last_ts": last_ts or first_ts,
        "ai_title": ai_title or "",
        "real_msgs": real_msgs,
        "n_turns": n_user,
        "jsonl_path": jsonl_path,
        "cwd": cwd or "",
        "is_open": sid in active,
        "session_status": status,
        "is_active": (sid in active) or age_sec < 300,
        "is_recent": age_sec < 1800,
    }


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


def _delete_session_files(session_id: str):
    """Remove any JSONL/dir created by an ephemeral claude -p call."""
    if not session_id:
        return
    for jsonl in PROJECTS_ROOT.rglob(f"{session_id}.jsonl"):
        try:
            jsonl.unlink()
        except Exception:
            pass
    # Also remove the per-session subagents dir if it exists
    for d in PROJECTS_ROOT.rglob(session_id):
        if d.is_dir():
            try:
                import shutil
                shutil.rmtree(d, ignore_errors=True)
            except Exception:
                pass


def call_claude_haiku(prompt: str, timeout: int = 45) -> str:
    """Call claude -p --model haiku and return stripped output.
    Suppresses all side effects: hooks, MCP, skills, session persistence.
    Even the residual ai-title JSONL is deleted after the call."""
    session_id = ""
    try:
        result = subprocess.run(
            ["claude", "-p", "--model", SUMMARY_MODEL,
             "--setting-sources", "",     # skip user/project/local settings → no hooks
             "--strict-mcp-config",        # ignore all MCP server configs
             "--disable-slash-commands",   # skip skills
             "--no-session-persistence",   # disable resumability persistence
             "--output-format", "json",    # need JSON to capture session_id
             prompt],
            capture_output=True, encoding="utf-8", errors="replace",
            timeout=timeout,
        )
        if result.returncode != 0:
            return ""
        try:
            payload = json.loads(result.stdout)
            session_id = payload.get("session_id", "") or ""
            text = (payload.get("result") or "").strip()
        except Exception:
            text = result.stdout.strip()
        for line in text.split("\n"):
            line = line.strip()
            if line and not line.startswith("```"):
                return line[:100]
        return ""
    except Exception:
        return ""
    finally:
        _delete_session_files(session_id)


def summarize_session(s: dict) -> str:
    """Get summary for a session: cache → AI title → LLM."""
    if s["ai_title"]:
        return s["ai_title"]

    # Active sessions: JSONL mtime changes every turn → cache always invalid → skip LLM
    if s.get("is_open"):
        return s["real_msgs"][0][:60] if s["real_msgs"] else ""

    mtime = s["jsonl_path"].stat().st_mtime
    cached = _load_cache(s["id"], mtime)
    if cached is not None:
        return cached

    if not s["real_msgs"]:
        # No content to summarize — cache empty so we don't retry next time
        _save_cache(s["id"], mtime, "")
        return ""

    # Build prompt
    sample = "\n---\n".join(s["real_msgs"][:5])
    sample = sample[:3000]  # cap input
    prompt = (
        "以下はClaude Codeセッションでのユーザー発言の冒頭です。"
        "このセッションで何をしようとしていたかを、日本語の体言止め1フレーズ"
        "(40字以内)で要約してください。前置きや「要約:」等は不要、"
        "要約フレーズのみを1行で出力してください。\n\n"
        f"{sample}"
    )

    summary = call_claude_haiku(prompt)
    if summary:
        _save_cache(s["id"], mtime, summary)
        return summary
    # LLM unavailable (quota/rate limit) — fallback to first message, don't cache
    return s["real_msgs"][0][:60] if s["real_msgs"] else ""


def summarize_all_parallel(sessions: list[dict], max_workers: int = 5):
    """Summarize all sessions in parallel, showing progress."""
    pending = [s for s in sessions if not s["ai_title"]
               and not s.get("is_open")   # active JSONL mtime changes → cache always stale
               and _load_cache(s["id"], s["jsonl_path"].stat().st_mtime) is None]
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
            done += 1
            print(f"\r  [{done}/{len(pending)}] ", end="", file=sys.stderr, flush=True)
    print(file=sys.stderr)

    # Now fill in summary for all sessions (cached)
    for s in sessions:
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
            cwd=repo, timeout=15,
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


def visible_len(s: str) -> int:
    return len(re.sub(r"\033\[[0-9;]*m", "", s))


def pad(s: str, width: int) -> str:
    return s + " " * max(0, width - visible_len(s))


def truncate_visual(s: str, width: int) -> str:
    """Truncate to visual width, accounting for wide chars (CJK = 2 cols)."""
    out = []
    cur = 0
    for ch in s:
        w = 2 if ord(ch) > 0x2E80 else 1
        if cur + w > width:
            break
        out.append(ch)
        cur += w
    return "".join(out)


def project_short(name: str) -> str:
    """C--Users-user-CLI-project-one → project-one"""
    parts = name.split("-")
    # Drop drive prefix (e.g. 'c', '', 'Users', 'user', 'name')
    if len(parts) > 4:
        return "-".join(parts[5:])[:14] or name[:14]
    return name[:14]


def label_for(s: dict) -> str:
    summary = s.get("summary", "") or ""
    if summary:
        return summary
    if s["real_msgs"]:
        return s["real_msgs"][0][:80]
    return _c("(empty)", GRAY)


# ── Display ──────────────────────────────────────────────────────────────────
def _find_session_jsonl(sid_prefix: str) -> Path | None:
    sid_prefix = sid_prefix.strip().split()[0]  # tolerate fzf field padding
    projects = Path.home() / ".claude" / "projects"
    for p in projects.rglob(f"{sid_prefix}*.jsonl"):
        if "subagents" not in str(p):
            return p
    return None


def _preview_header(s: dict, found: Path) -> None:
    hidden_tag = "  [HIDDEN]" if s["id"] in _load_hidden() else ""
    print(f"\033[1m{s['ai_title'] or '(no AI title)'}\033[0m{hidden_tag}")
    print(f"  id:       {s['id']}")
    print(f"  project:  {found.parent.name}")
    print(f"  cwd:      {s.get('cwd','')}")
    print(f"  start:    {fmt_ts(s['first_ts'])}")
    print(f"  last:     {fmt_last_active(s)} ago  ({fmt_ts(s['last_ts'])})")
    print(f"  turns:    {s['n_turns']}")
    print()


def preview_session(session_id: str) -> None:
    """Condensed preview: header + first/last user msgs."""
    found = _find_session_jsonl(session_id)
    if not found:
        print(f"(session {session_id[:8]} not found)")
        return
    s = parse_session(found)
    if not s:
        print("(unable to parse session)")
        return

    _preview_header(s, found)
    print("\033[36m── First user message ──\033[0m")
    if s["real_msgs"]:
        print(s["real_msgs"][0][:1500])
    else:
        print("(no real user messages)")

    if len(s["real_msgs"]) > 1:
        print()
        print(f"\033[36m── Last user message  (#{len(s['real_msgs'])}) ──\033[0m")
        print(s["real_msgs"][-1][:1500])

    print()
    print("\033[2mCtrl-f: full conversation  |  Ctrl-s: this summary view\033[0m")


def preview_session_full(session_id: str) -> None:
    """Full conversation: every user message + first ~300 chars of each assistant reply."""
    found = _find_session_jsonl(session_id)
    if not found:
        print(f"(session {session_id[:8]} not found)")
        return
    s = parse_session(found)
    if not s:
        print("(unable to parse session)")
        return

    _preview_header(s, found)
    print("\033[36m── Full conversation ──\033[0m")

    n = 0
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
                    print(f"\033[36m▶ user [{n}]:\033[0m {text[:1200]}")
            elif t == "assistant":
                content = obj.get("message", {}).get("content", [])
                if isinstance(content, list):
                    for b in content:
                        if isinstance(b, dict) and b.get("type") == "text":
                            txt = b.get("text", "").strip()
                            if txt:
                                print(f"\033[33m◀ assistant:\033[0m {txt[:400]}")
                                break

    print()
    print("\033[2mCtrl-s: condensed summary  |  Ctrl-f: this full view\033[0m")


def _activity_marker(s: dict) -> str:
    """Activity column: open-busy / open-idle / active / recent."""
    if s.get("is_open"):
        if s.get("session_status") == "busy":
            return _c("◉", CYAN, BOLD)   # open & currently responding
        return _c("◉", GREEN, BOLD)       # open & idle in another Claude window
    if s.get("is_active"):
        return _c("●", GREEN)
    if s.get("is_recent"):
        return _c("○", YELLOW)
    return " "


def _state_marker(s: dict, hidden: set, favorites: set) -> str:
    """State column: favorite or hidden (mutually exclusive)."""
    sid = s["id"]
    if sid in favorites:
        return _c("★", GOLD)
    if sid in hidden:
        return _c("✗", RED)
    return " "


def fmt_last_active(s: dict) -> str:
    """Human-friendly 'last activity' column: '5m', '2h', '3d', '04/22'."""
    import time as _time
    age = _time.time() - s["jsonl_path"].stat().st_mtime
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


def display_table(sessions: list[dict], repo: Path | None, show_project: bool):
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
    for s in sessions:
        is_hidden = s["id"] in hidden
        # Two-column marker: activity + favorite/hidden state
        act = _activity_marker(s)
        st  = _state_marker(s, hidden, favorites)
        marker = f"{act}{st}"
        start = fmt_ts(s["first_ts"])
        last = fmt_last_active(s)
        sid8  = short_id(s["id"])
        turns = str(s["n_turns"]) if s["n_turns"] > 0 else "?"
        lbl_raw = label_for(s)
        lbl = truncate_visual(lbl_raw, title_col_width)

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
              f"{_c('★', GOLD)} fav  {_c('●', GREEN)} active(<5m)  "
              f"{_c('○', YELLOW)} recent(<30m)  {_c('✗', RED)} hidden  "
              f"·  recap --pick to resume")
    print(_c(legend, DIM))
    print()


# ── fzf pick mode ────────────────────────────────────────────────────────────
def build_fzf_lines(sessions: list[dict], repo: Path | None, show_project: bool) -> list[str]:
    """Build tab-separated lines for fzf input.
    Format:  display\tsession_id\tsearchable_text
    --with-nth=1 displays only the first field; --nth=1,3 makes fzf search
    against display + searchable_text. Hidden rows are wrapped in dim+gray
    ANSI so the user can see at a glance which entries are hidden."""
    hidden = _load_hidden()
    favorites = _load_favorites()
    view_mode = _get_view_mode()
    lines = []
    for s in sessions:
        is_hidden = s["id"] in hidden
        if is_hidden and view_mode != "show-hidden":
            continue
        # Activity column
        if s.get("is_open"):
            if s.get("session_status") == "busy":
                act = f"{BOLD}{CYAN}◉{RESET}"
            else:
                act = f"{BOLD}{GREEN}◉{RESET}"
        elif s.get("is_active"):
            act = f"{GREEN}●{RESET}"
        elif s.get("is_recent"):
            act = f"{YELLOW}○{RESET}"
        else:
            act = " "
        # State column
        if s["id"] in favorites:
            st = f"{GOLD}★{RESET}"
        elif is_hidden:
            st = f"{RED}✗{RESET}"
        else:
            st = " "
        marker = f"{act}{st}"
        start = fmt_ts(s["first_ts"])
        last = fmt_last_active(s)
        sid8 = short_id(s["id"])
        proj = project_short(s["project_name"]) if show_project else ""
        lbl = truncate_visual(label_for(s), 65)
        commits = ""
        if repo:
            cc = git_commits_in_range(s["first_ts"], s["last_ts"], repo)
            if cc:
                commits = "  " + truncate_visual(cc[0], 38)
        if show_project:
            body = f"{start}  [{last:>4}]  [{proj:<14}]  {sid8}  {lbl}{commits}"
        else:
            body = f"{start}  [{last:>4}]  {sid8}  {lbl}{commits}"
        if is_hidden:
            disp = f"{marker} {HIDDEN_DIM}{body}  (hidden){RESET}"
        else:
            disp = f"{marker} {body}"
        # Searchable content: AI title + all user messages (capped per-session)
        searchable = (s["ai_title"] + "  " + "  ".join(s["real_msgs"]))[:3000]
        searchable = searchable.replace("\t", " ").replace("\n", " ").replace("\r", " ")
        lines.append(f"{disp}\t{s['id']}\t{searchable}")
    return lines


# Module-level state captured by main() so reload bindings can rebuild the
# same session view. Set in main() before calling fzf_pick.
_pick_days: int = 0
_pick_here: bool = False
_pick_project: str | None = None


def fzf_pick(sessions: list[dict], repo: Path | None, show_project: bool):
    """Pipe session list to fzf and run claude --resume on selection.
    Uses temp file for stdin/stdout so fzf gets a clean tty for its TUI."""
    lines = build_fzf_lines(sessions, repo, show_project)

    # Write to temp file (binary mode → no CRLF translation that confuses fzf).
    # fzf needs a non-pipe stdin on Windows to start its TUI properly.
    import tempfile
    with tempfile.NamedTemporaryFile(
        mode="wb", delete=False, suffix=".txt"
    ) as tf:
        tf.write(("\n".join(lines) + "\n").encode("utf-8"))
        tmp_path = tf.name

    # Selection is written to a temp file via fzf's `--expect=enter` and shell
    # redirect from a file so we don't intercept stdout (which on Windows
    # PowerShell can break fzf's full-screen TUI when combined with subprocess).
    out_path = tmp_path + ".out"
    env = os.environ.copy()
    env.setdefault("TERM", "xterm-256color")
    try:
        with open(tmp_path, "rb") as stdin_file, open(out_path, "wb") as stdout_file:
            try:
                # Build reload command: re-emit list via stdin to fzf.
                # We need to call the same recap with the same session-source flags
                # that produced the current view (here / project / all-projects).
                reload_args = ["--list", "--days", str(_pick_days)]
                if _pick_here:
                    reload_args.append("--here")
                if _pick_project:
                    reload_args += ["--project", _pick_project]
                reload_cmd = "recap " + " ".join(f'"{a}"' if " " in a else a for a in reload_args)

                preview_cmd      = "recap --preview {2}"
                preview_full_cmd = "recap --preview-full {2}"
                bindings = ",".join([
                    f"ctrl-x:execute-silent(recap --hide {{2}})+reload({reload_cmd})",
                    f"ctrl-p:execute-silent(recap --favorite {{2}})+reload({reload_cmd})",
                    f"ctrl-r:execute-silent(recap --toggle-view)+reload({reload_cmd})",
                    f"ctrl-f:change-preview({preview_full_cmd})",
                    f"ctrl-s:change-preview({preview_cmd})",
                ])
                header = ("Enter:resume  Ctrl-p:★fav  Ctrl-x:hide  "
                          "Ctrl-r:toggle-hidden  Ctrl-f/s:full/summary  Ctrl-C:cancel")
                result = subprocess.run(
                    ["fzf", "--ansi", "--no-sort", "--reverse",
                     "--delimiter=\t", "--with-nth=1", "--nth=1,3",
                     "--preview", preview_cmd,
                     "--preview-window", "right:55%:wrap",
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
            chosen = open(out_path, "rb").read().decode("utf-8", errors="replace").strip()
        except Exception:
            chosen = ""
    finally:
        for p in (tmp_path, out_path):
            try:
                os.unlink(p)
            except Exception:
                pass

    if result.returncode != 0 or not chosen:
        return
    full_id = chosen.split("\t")[1].strip()   # field 1 = UUID (0=display, 2=searchable)
    if not full_id:
        return

    # Find the session's working directory so claude --resume runs in the right
    # project context (tools/CLAUDE.md/git status all match the original session)
    selected = next((s for s in sessions if s["id"] == full_id), None)
    target_cwd = (selected.get("cwd") if selected else "") or None
    if target_cwd and not Path(target_cwd).is_dir():
        target_cwd = None

    print(f"\nResuming {full_id[:8]}" + (f"  (cwd: {target_cwd})" if target_cwd else ""))
    env = os.environ.copy()
    env["RECAP_RESUME"] = "1"   # signal to teams-notify.py: suppress idle_prompt
    subprocess.run(["claude", "--resume", full_id], cwd=target_cwd, env=env)


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


# ── Main ─────────────────────────────────────────────────────────────────────
def main():
    p = argparse.ArgumentParser(
        description="Claude Code session history viewer  "
                    "(flags --days/--here/--all are remembered between runs)"
    )
    # default=None on persisted flags so we can detect "not provided" and use
    # the last saved value instead.
    p.add_argument("--days", type=int, default=None, metavar="N",
                   help="Show sessions from the last N days (saved across runs)")
    p.add_argument("--here", "--this-project-only", action="store_true",
                   default=None, dest="here",
                   help="Show only sessions for the current project")
    p.add_argument("--all", "--all-projects", action="store_true",
                   default=None, dest="all_scope",
                   help="Show sessions across all projects")
    p.add_argument("--reset-options", action="store_true",
                   help="Forget saved --days/--here/--all defaults")
    p.add_argument("--pick", action="store_true",
                   help="Open interactive fzf picker (default behavior)")
    p.add_argument("--table", action="store_true",
                   help="Show static table instead of fzf picker")
    p.add_argument("--project", metavar="PATH")
    p.add_argument("--no-summary", action="store_true",
                   help="Skip Haiku summarization (use AI title or first user msg)")
    p.add_argument("--refresh-summary", action="store_true",
                   help="Discard cached summaries and regenerate")
    p.add_argument("--preview", metavar="SESSION_ID",
                   help="Print session content preview (used internally by --pick)")
    p.add_argument("--preview-full", metavar="SESSION_ID",
                   help="Print full conversation preview (used internally by --pick)")
    p.add_argument("--list", action="store_true",
                   help="Emit fzf-formatted lines (used internally by --pick reload)")
    p.add_argument("--hide", metavar="SESSION_ID",
                   help="Toggle hidden state for a session")
    p.add_argument("--favorite", metavar="SESSION_ID",
                   help="Toggle favorite (★) state for a session")
    p.add_argument("--toggle-view", action="store_true",
                   help="Toggle default/show-hidden view mode")
    args = p.parse_args()

    if args.preview:
        preview_session(args.preview)
        return
    if args.preview_full:
        preview_session_full(args.preview_full)
        return
    if args.hide:
        _toggle_hide(args.hide.strip().split()[0])
        return
    if args.favorite:
        _toggle_favorite(args.favorite.strip().split()[0])
        return
    if args.toggle_view:
        _toggle_view_mode()
        return

    if args.reset_options:
        if OPTIONS_FILE.exists():
            OPTIONS_FILE.unlink()
        print("Saved options cleared.", file=sys.stderr)
        return

    # Resolve --days / --here / --all from CLI vs saved defaults
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
    _save_options({"days": args.days, "scope": scope})

    # --project always wins for scope; otherwise scope follows --here/--all
    args.all_projects = not (args.here or args.project)

    if args.refresh_summary and CACHE_DIR.exists():
        for f in CACHE_DIR.glob("*.json"):
            f.unlink()

    since = None if args.days == 0 else datetime.now(tz=timezone.utc) - timedelta(days=args.days)
    projects_root = Path.home() / ".claude" / "projects"
    cwd = Path.cwd()

    try:
        r = subprocess.run(["git", "rev-parse", "--show-toplevel"],
                          capture_output=True, text=True, cwd=cwd, timeout=3)
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

    sessions.sort(key=lambda s: s["first_ts"], reverse=True)

    if not sessions:
        period = "all history" if args.days == 0 else f"last {args.days} days"
        print(f"No sessions in {period}.")
        return

    if args.no_summary:
        for s in sessions:
            s["summary"] = s["ai_title"] or (s["real_msgs"][0][:60] if s["real_msgs"] else "")
    else:
        summarize_all_parallel(sessions)

    if args.list:
        # Emit fzf-formatted lines without launching fzf (used by reload)
        for line in build_fzf_lines(sessions, repo, args.all_projects):
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
        display_table(visible, repo, args.all_projects)
    else:
        # Default: interactive fzf picker
        global _pick_days, _pick_here, _pick_project
        _pick_days    = args.days
        _pick_here    = args.here
        _pick_project = args.project
        fzf_pick(sessions, repo, args.all_projects)


if __name__ == "__main__":
    main()
