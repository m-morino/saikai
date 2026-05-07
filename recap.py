#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = []
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
import subprocess
import sys
import time
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
OPTIONS_FILE = CACHE_DIR / "options.json"
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
    age_sec = time.time() - mtime
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


def _delete_session_files(session_id: str):
    """Remove any JSONL created by an ephemeral claude -p call."""
    if not session_id:
        return
    for jsonl in PROJECTS_ROOT.rglob(f"{session_id}.jsonl"):
        try:
            jsonl.unlink()
        except Exception:
            pass


def call_claude_haiku(prompt: str, timeout: int = 45) -> str:
    """Call claude -p --model haiku and return stripped output.
    Suppresses all side effects: hooks, MCP, skills, session persistence.
    Uses Popen + communicate(timeout) so a hung claude.exe is always killed
    and pipes drained, even when grandchildren inherit console handles."""
    cmd = ["claude", "-p", "--model", SUMMARY_MODEL,
           "--setting-sources", "",
           "--strict-mcp-config",
           "--disable-slash-commands",
           "--no-session-persistence",
           "--output-format", "json",
           prompt]
    extra: dict = {}
    if sys.platform == "win32":
        extra["creationflags"] = 0x08000000  # CREATE_NO_WINDOW — no inherited console handles

    session_id = ""
    try:
        with subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              **extra) as proc:
            try:
                raw_out, _ = proc.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.communicate()  # drain pipes to unblock
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
        for line in text.split("\n"):
            line = line.strip()
            if line and not line.startswith("```"):
                return line[:100]
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


def summarize_session(s: dict) -> str:
    """Get summary for a session: cache → AI title → LLM."""
    if s["ai_title"]:
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
    return len(_ANSI_RE.sub("", s))


def pad(s: str, width: int) -> str:
    return s + " " * max(0, width - visible_len(s))


def truncate_visual(s: str, width: int) -> str:
    """Truncate to visual width, accounting for wide chars (CJK = 2 cols) and ANSI escapes."""
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
        w = 2 if ord(ch) > 0x2E80 else 1
        if cur + w > width:
            break
        out.append(ch)
        cur += w
        i += 1
    return "".join(out)


def project_short(name: str) -> str:
    """C--Users-masayuki-morino-CLI-work-tools → work-tools"""
    parts = name.split("-")
    # Drop drive prefix (e.g. 'c', '', 'Users', 'masayuki', 'morino')
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
        rs = "  ·  ".join(reasons) if reasons else ""
        lines.append(f"  parent:   {pid[:8]}  [score {score:.2f}]  {_c(rs, GRAY)}")
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
    cache_file = cache_dir / f"{sid}.txt"
    if cache_file.exists():
        sys.stdout.write(cache_file.read_text(encoding="utf-8"))
        return
    found = _find_session_jsonl(sid)
    if not found:
        print(f"(session {sid[:8]} not found)")
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
              f"{_c('★', GOLD)} fav  {_c('●', GREEN)} active(<5m)  "
              f"{_c('○', YELLOW)} recent(<30m)  {_c('✗', RED)} hidden  "
              f"·  recap --pick to resume")
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


def fzf_pick(sessions: list[dict], repo: Path | None, show_project: bool,
             flat: bool = False, reload_args: list[str] | None = None):
    """Pipe session list to fzf and run claude --resume on selection.
    Uses temp file for stdin/stdout so fzf gets a clean tty for its TUI."""
    lines = build_fzf_lines(sessions, repo, show_project, flat=flat)

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
    try:
        with open(tmp_path, "rb") as stdin_file, open(out_path, "wb") as stdout_file:
            try:
                # Reload re-runs recap with the session-source flags captured by main()
                ra = reload_args or ["--list"]
                reload_cmd = "recap " + " ".join(f'"{a}"' if " " in a else a for a in ra)

                # `recap --preview` reads the pre-rendered cache; portable across cmd / bash / pwsh
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

    # Try origin_cwd first (where Claude originally indexed the session — required
    # for --resume to find it). Fall back to last cwd, then jsonl_path's parent.
    selected = next((s for s in sessions if s["id"] == full_id), None)
    candidates = []
    if selected:
        for k in ("origin_cwd", "cwd"):
            v = selected.get(k)
            if v and Path(v).is_dir():
                candidates.append(v)
    target_cwd = candidates[0] if candidates else None

    print(f"\nResuming {full_id[:8]}" + (f"  (cwd: {target_cwd})" if target_cwd else ""))
    env = os.environ.copy()
    env["RECAP_RESUME"] = "1"   # signal to teams-notify.py: suppress idle_prompt
    # The recap wrapper invokes us via `uv run --no-project`, which sets
    # VIRTUAL_ENV to its ephemeral env and prepends the venv's Scripts/bin
    # to PATH. Inherit-as-is would make the resumed session's `uv` warn
    # "VIRTUAL_ENV does not match project environment .venv". Strip both.
    leaked_venv = env.pop("VIRTUAL_ENV", None)
    env.pop("VIRTUAL_ENV_PROMPT", None)
    if leaked_venv:
        bin_dir = "Scripts" if sys.platform == "win32" else "bin"
        venv_bin = str(Path(leaked_venv) / bin_dir)
        cmp = (lambda p: p.lower()) if sys.platform == "win32" else (lambda p: p)
        parts = [p for p in env.get("PATH", "").split(os.pathsep) if cmp(p) != cmp(venv_bin)]
        env["PATH"] = os.pathsep.join(parts)
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


# ── Related sessions ─────────────────────────────────────────────────────────
def _cwd_similarity(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    # Conservative on prefix matches — siblings under a common parent shouldn't dominate
    sep = os.sep
    if a.startswith(b + sep) or b.startswith(a + sep):
        return 0.5
    return 0.0


def _interval_gap_minutes(a: dict, b: dict) -> float:
    """Minutes between two session intervals; 0.0 if they overlapped."""
    try:
        as_ = datetime.fromisoformat(a["first_ts"].replace("Z", "+00:00"))
        ae  = datetime.fromisoformat(a["last_ts"].replace("Z", "+00:00"))
        bs  = datetime.fromisoformat(b["first_ts"].replace("Z", "+00:00"))
        be  = datetime.fromisoformat(b["last_ts"].replace("Z", "+00:00"))
    except Exception:
        return float("inf")
    if ae < bs:
        return (bs - ae).total_seconds() / 60.0
    if be < as_:
        return (as_ - be).total_seconds() / 60.0
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
    cache_file = PARSED_DIR / f"{sid}.json"
    try:
        c = json.loads(cache_file.read_text(encoding="utf-8"))
        if "topics" in c:
            return c["topics"]
    except Exception:
        pass
    return None


def _save_topics_to_cache(sid: str, topics: list[str]) -> None:
    cache_file = PARSED_DIR / f"{sid}.json"
    try:
        c = json.loads(cache_file.read_text(encoding="utf-8"))
        c["topics"] = topics
        cache_file.write_text(json.dumps(c, ensure_ascii=False), encoding="utf-8")
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


def _score_relation(target: dict, other: dict) -> tuple[float, list[str]]:
    cwd_s    = _cwd_similarity(target.get("cwd", ""), other.get("cwd", ""))
    branch_s = 1.0 if (target.get("git_branch") and target.get("git_branch") == other.get("git_branch")) else 0.0
    gap_min  = _interval_gap_minutes(target, other)
    time_s   = math.exp(-gap_min / _TIME_TAU_MIN) if gap_min != float("inf") else 0.0
    title_s  = _title_similarity(target, other)
    topic_s  = _topic_similarity(target, other)
    structural = _W_CWD*cwd_s + _W_BRANCH*branch_s + _W_TITLE*title_s + _W_TOPIC*topic_s
    # Time factor: small floor keeps far-past matches discoverable but heavily damped
    time_factor = 0.10 + 0.90 * time_s
    score = structural * time_factor

    reasons: list[str] = []
    if cwd_s == 1.0:
        reasons.append("same cwd")
    elif cwd_s >= 0.7:
        reasons.append("same project")
    if branch_s == 1.0:
        reasons.append(f"branch {other.get('git_branch','')}")
    gap_label = _fmt_gap(gap_min)
    if gap_label:
        reasons.append(gap_label)
    if title_s >= 0.3:
        reasons.append(f"title sim {title_s:.0%}")
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

    print(_c("Target:  ", BOLD) + f"{short_id(target['id'])}  {label_for(target)}")
    print(f"  cwd:    {target.get('cwd','') or '(none)'}")
    print(f"  branch: {target.get('git_branch','') or '(none)'}")
    print(f"  time:   {fmt_ts(target['first_ts'])} → {fmt_ts(target['last_ts'])}")
    print()

    # Prefilter: score WITHOUT topics (cheap), keep sessions whose best-case
    # score (assuming perfect topic match = +_W_TOPIC) could clear the floor.
    # This caps Haiku calls at TOP_K instead of N for the topic extraction below.
    floor = 0.20
    TOP_K = 50
    prefiltered: list[tuple[dict, float]] = []
    for s in sessions:
        if s["id"] == target["id"]:
            continue
        cwd_s = _cwd_similarity(target.get("cwd",""), s.get("cwd",""))
        branch_s = 1.0 if (target.get("git_branch") and target.get("git_branch") == s.get("git_branch")) else 0.0
        gap_min = _interval_gap_minutes(target, s)
        time_s = math.exp(-gap_min / _TIME_TAU_MIN) if gap_min != float("inf") else 0.0
        title_s = _title_similarity(target, s)
        struct_no_topic = _W_CWD*cwd_s + _W_BRANCH*branch_s + _W_TITLE*title_s
        time_factor = 0.10 + 0.90 * time_s
        max_possible = (struct_no_topic + _W_TOPIC) * time_factor
        if max_possible >= floor:
            prefiltered.append((s, max_possible))

    prefiltered.sort(key=lambda x: -x[1])
    top_candidates = [s for s, _ in prefiltered[:TOP_K]]
    if top_candidates:
        batch_ensure_topics([target] + top_candidates, show_progress=True)

    candidates: list[tuple[dict, float, list[str]]] = []
    for s in top_candidates:
        score, reasons = _score_relation(target, s)
        if score >= floor:
            candidates.append((s, score, reasons))
    candidates.sort(key=lambda x: -x[1])

    if not candidates:
        print(_c("(no related sessions found above confidence floor 0.20)", GRAY))
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


# ── Forest building ──────────────────────────────────────────────────────────
def _build_forest(sessions: list[dict], floor: float = 0.20) -> None:
    """Mutates sessions in place: assigns each its highest-scoring earlier session as parent.
    Adds keys: parent_id (or None), parent_score (0.0 if root), parent_reasons (list)."""
    batch_ensure_topics(sessions, show_progress=True)
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

    def walk(sid: str, prefix: str, is_last: bool):
        s = by_id.get(sid)
        if not s:
            return
        if prefix:
            base = "└─" if is_last else "├─"
            score = s.get("parent_score", 0.0)
            if score >= 0.7:
                glyph = _c(base, GREEN)
            elif score >= 0.4:
                glyph = _c(base, YELLOW)
            else:
                glyph = _c(base.replace("─", "┄"), GRAY)
            node_prefix = prefix + glyph + " "
        else:
            node_prefix = ""
        out.append((s, node_prefix))
        kids = sorted(children.get(sid, []), key=newest_in_tree, reverse=True)
        for i, kid in enumerate(kids):
            cont = "   " if is_last else "│  "
            walk(kid, prefix + cont, i == len(kids) - 1)

    roots.sort(key=newest_in_tree, reverse=True)
    for i, root in enumerate(roots):
        walk(root, "", i == len(roots) - 1)
    return out


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
    p.add_argument("--related", metavar="SESSION_ID",
                   help="Show sessions related to SESSION_ID with confidence scores and reasons")
    p.add_argument("--tree", action="store_true",
                   help="Group sessions into an inferred parent/child forest (heuristic, may be wrong)")
    args = p.parse_args()

    if args.preview:
        preview_session(args.preview)
        return
    if args.preview_full:
        preview_session_full(args.preview_full)
        return
    if args.hide:
        _toggle_in_set(HIDDEN_FILE, _trim_sid(args.hide))
        return
    if args.favorite:
        _toggle_in_set(FAVORITE_FILE, _trim_sid(args.favorite))
        return
    if args.toggle_view:
        _toggle_view_mode()
        return

    if args.reset_options:
        if OPTIONS_FILE.exists():
            OPTIONS_FILE.unlink()
        print("Saved options cleared.", file=sys.stderr)
        return

    # --related needs cross-project scope so the target can be found wherever it lives
    if args.related:
        args.all_scope = True
        args.here = False

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
    if not args.related:
        _save_options({"days": args.days, "scope": scope})

    # --project always wins for scope; otherwise scope follows --here/--all
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

    # Tree mode is opt-in via --tree (heuristic, may produce wrong edges)
    use_tree = args.tree and len(sessions) <= 1000
    if use_tree:
        _build_forest(sessions)
    else:
        for s in sessions:
            s["parent_id"] = None
            s["parent_score"] = 0.0
            s["parent_reasons"] = []

    flat = not use_tree
    if args.list:
        # Emit fzf-formatted lines without launching fzf (used by reload)
        for line in build_fzf_lines(sessions, repo, args.all_projects, flat=flat):
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
        # Default: interactive fzf picker
        reload_args = ["--list", "--days", str(args.days)]
        if args.here:
            reload_args.append("--here")
        if args.project:
            reload_args += ["--project", args.project]
        if args.tree:
            reload_args.append("--tree")
        fzf_pick(sessions, repo, args.all_projects, flat=flat, reload_args=reload_args)


if __name__ == "__main__":
    main()
