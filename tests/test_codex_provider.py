"""Codex provider C1: Codex CLI threads (~/.codex/sessions rollout JSONLs)
appear in saikai's one list as provider="codex" rows — searchable, previewable,
and Enter-resumable via `codex resume <root-id>` in a normal live pane.

Facts verified against codex-cli 0.144.1 on real data (2026-07-12):
- rollout line 1 = session_meta {id, cwd, timestamp, source, originator, git};
  a RESUMED rollout adds session_id (= ROOT thread id) + parent_thread_id
- clean user turns ride event_msg {type:"user_message", message} — the
  response_item user messages carry AGENTS.md/instructions preambles
- session_index.jsonl = {id: root thread id, thread_name, updated_at}
- `codex resume <root-id>` loads the thread's LATEST state (chained rollouts),
  and skips its "choose working directory" prompt when cwd matches the record
- source distinguishes "cli"/"vscode"/"exec" (user threads),
  {"subagent":{"thread_spawn":{parent_thread_id,…}}} (codex's own agents) and
  {"subagent":{"other":…}} (internal assessors — noise, excluded)

Run:  python tests/test_codex_provider.py
"""
import json
import os
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone, timedelta

os.environ.pop("SAIKAI_MIRROR", None)
from pathlib import Path

_FAKE_HOME = Path(tempfile.mkdtemp(prefix="saikai-codex-test-"))
for _var in ("USERPROFILE", "HOME", "APPDATA", "LOCALAPPDATA", "XDG_CONFIG_HOME"):
    os.environ[_var] = str(_FAKE_HOME)
os.environ.pop("SAIKAI_CONFIG", None)
os.environ["SAIKAI_SUMMARIZE_ENABLED"] = "0"
os.environ["SAIKAI_NO_TERMINAL_WATCHDOG"] = "1"
_CODEX_HOME = _FAKE_HOME / "codex-home"
os.environ["CODEX_HOME"] = str(_CODEX_HOME)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import saikai


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _mk_rollout(dt, rid, cwd, source, msgs, session_id=None, parent=None):
    """Write one rollout file the way codex-cli 0.144.1 does; returns its path.
    File mtime is pinned to dt so recency assertions are deterministic."""
    day = _CODEX_HOME / "sessions" / f"{dt.year:04d}" / f"{dt.month:02d}" / f"{dt.day:02d}"
    day.mkdir(parents=True, exist_ok=True)
    meta = {"id": rid, "timestamp": _iso(dt), "cwd": cwd, "source": source,
            "originator": "codex_cli_rs", "cli_version": "0.144.1", "git": None,
            "instructions": "…"}
    if session_id:
        meta["session_id"] = session_id
        meta["parent_thread_id"] = parent or session_id
    recs = [{"timestamp": _iso(dt), "type": "session_meta", "payload": meta}]
    t = dt
    for m in msgs:
        t = t + timedelta(minutes=1)
        recs.append({"timestamp": _iso(t), "type": "event_msg",
                     "payload": {"type": "user_message", "message": m}})
        recs.append({"timestamp": _iso(t), "type": "event_msg",
                     "payload": {"type": "agent_message", "message": f"re: {m}"}})
    # the instruction-preamble user record that must NOT become a real_msg
    recs.append({"timestamp": _iso(t), "type": "response_item",
                 "payload": {"type": "message", "role": "user",
                             "content": [{"type": "input_text",
                                          "text": "# AGENTS.md instructions …"}]}})
    p = day / f"rollout-{dt.strftime('%Y-%m-%dT%H-%M-%S')}-{rid}.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in recs) + "\n", encoding="utf-8")
    ts = dt.replace(tzinfo=timezone.utc).timestamp()
    os.utime(p, (ts, ts))
    return p


def _uuid():
    return str(uuid.uuid4())


def _reset_codex_fixture():
    import shutil
    shutil.rmtree(_CODEX_HOME, ignore_errors=True)
    (_CODEX_HOME / "sessions").mkdir(parents=True)


D = datetime(2026, 7, 10, 9, 0, 0)
A = _uuid()          # fresh cli thread
B = _uuid()          # chained thread: root …
B2 = _uuid()         # … + resumed rollout (session_id=B)
C = _uuid()          # codex subagent (thread_spawn, parent B)
G = _uuid()          # internal guardian — excluded


def _seed_standard():
    _reset_codex_fixture()
    _mk_rollout(D, A, "/tmp/work-a", "cli", ["first question", "second question"])
    _mk_rollout(D + timedelta(hours=1), B, "/tmp/work-b", "vscode", ["root msg"])
    _mk_rollout(D + timedelta(hours=3), B2, "/tmp/work-b2", "cli",
                ["resumed msg"], session_id=B, parent=B)
    _mk_rollout(D + timedelta(hours=2), C, "/tmp/work-c",
                {"subagent": {"thread_spawn": {"parent_thread_id": B, "depth": 1,
                                               "agent_nickname": "Euler",
                                               "agent_role": "awaiter"}}},
                ["spawned job"])
    _mk_rollout(D + timedelta(hours=2), G, "/tmp/work-g",
                {"subagent": {"other": "guardian"}}, ["assessor noise"])
    (_CODEX_HOME / "session_index.jsonl").write_text(
        json.dumps({"id": B, "thread_name": "Thread B named",
                    "updated_at": _iso(D + timedelta(hours=3))}) + "\n",
        encoding="utf-8")


def test_codex_enabled_gate():
    import shutil
    shutil.rmtree(_CODEX_HOME, ignore_errors=True)
    assert saikai._codex_enabled() is False          # no sessions dir
    (_CODEX_HOME / "sessions").mkdir(parents=True)
    assert saikai._codex_enabled() is True
    os.environ["SAIKAI_CODEX"] = "0"
    try:
        assert saikai._codex_enabled() is False      # explicit opt-out
    finally:
        os.environ.pop("SAIKAI_CODEX", None)


def test_load_codex_sessions_one_row_per_thread():
    _seed_standard()
    rows = {s["id"]: s for s in saikai.load_codex_sessions(None)}
    assert set(rows) == {A, B, C}, sorted(rows)      # G excluded, B2 folded into B
    assert all(s["provider"] == "codex" for s in rows.values())
    assert all(s["remote_origin"] is False for s in rows.values())
    b = rows[B]
    assert b["jsonl_path"].name.endswith(f"{B2}.jsonl")   # newest rollout = the file
    assert b["ai_title"] == "Thread B named"              # from session_index
    assert b["real_msgs"] == ["root msg", "resumed msg"]  # merged across the chain
    assert b["cwd"] == "/tmp/work-b2" and b["origin_cwd"] == "/tmp/work-b"
    a = rows[A]
    assert a["ai_title"] == "" and a["real_msgs"][0] == "first question"
    assert a["n_turns"] == 2
    c = rows[C]
    assert c["parent_session_id"] == B and c["agent_id"] == "Euler"
    # instruction preambles must not leak into search/preview text
    assert not any("AGENTS.md" in m for s in rows.values() for m in s["real_msgs"])


def test_codex_since_filter_uses_thread_latest_mtime():
    _seed_standard()
    since = (D + timedelta(hours=1, minutes=30)).replace(tzinfo=timezone.utc)
    rows = {s["id"] for s in saikai.load_codex_sessions(since)}
    # A (09:00) is older; B's LATEST rollout (12:00) and C (11:00) survive
    assert rows == {B, C}, rows


def test_build_resume_invocation_codex_argv_and_cwd():
    _seed_standard()
    live_dir = str(_FAKE_HOME)                        # an existing dir
    sessions = [{"id": B, "provider": "codex", "cwd": live_dir,
                 "origin_cwd": "/gone/away"}]
    argv, cwd, env = saikai._build_resume_invocation(B, sessions)
    assert os.path.basename(argv[0]).startswith("codex"), argv
    assert argv[1:3] == ["resume", B], argv
    assert cwd == live_dir            # latest cwd first: codex skips its dir prompt
    assert "TEXTUAL_LOG" not in env
    # vanished cwd → None (inherit; codex prompts inside the pane — survivable)
    sessions = [{"id": B, "provider": "codex", "cwd": "/gone/away"}]
    _argv, cwd2, _env = saikai._build_resume_invocation(B, sessions)
    assert cwd2 is None


def test_codex_rows_in_scope_filters_here_mode():
    rows = [{"id": "1", "cwd": "/repo/x/sub"}, {"id": "2", "cwd": "/elsewhere"},
            {"id": "3", "cwd": "/repo/x"}]
    keep = saikai._codex_rows_in_scope(rows, False, Path("/repo/x"), Path("/repo/x/sub"))
    assert [s["id"] for s in keep] == ["1", "3"]
    assert saikai._codex_rows_in_scope(rows, True, None, Path("/")) == rows


def test_codex_dirs_mtime_bumps_on_new_rollout():
    _seed_standard()
    m1 = saikai._codex_dirs_mtime()
    time.sleep(0.05)
    _mk_rollout(datetime(2026, 7, 11, 8, 0, 0), _uuid(), "/tmp/new", "cli", ["hi"])
    # the new DAY dir is newer than anything the first call saw; pin its mtime
    # to now so the signal moves even on coarse filesystems
    assert saikai._codex_dirs_mtime() > m1


def test_needs_attention_skips_codex_rows():
    _seed_standard()
    row = next(s for s in saikai.load_codex_sessions(None) if s["id"] == A)
    assert saikai._needs_attention(row, {}) is False


def test_parse_codex_rollout_cache_roundtrip():
    _seed_standard()
    p = next((_CODEX_HOME / "sessions").rglob(f"*{A}.jsonl"))
    one = saikai._parse_codex_rollout(p)
    two = saikai._parse_codex_rollout(p)          # cache hit must be identical
    assert one == two and one["root_id"] == A and one["kind"] == "cli"


# ── Pilot: codex row visible + Enter builds the codex argv ───────────────────

def _write_claude_session(title):
    sid = str(uuid.uuid4())
    pdir = _FAKE_HOME / ".claude" / "projects" / "-tmp-claude-work"
    pdir.mkdir(parents=True, exist_ok=True)
    recs = [
        {"type": "ai-title", "aiTitle": title,
         "timestamp": "2026-07-10T00:00:00.000Z", "cwd": "/tmp/claude-work"},
        {"type": "user", "timestamp": "2026-07-10T00:01:00.000Z",
         "cwd": "/tmp/claude-work",
         "message": {"content": "claude side prompt long enough"}},
    ]
    (pdir / f"{sid}.jsonl").write_text(
        "\n".join(json.dumps(r) for r in recs) + "\n", encoding="utf-8")
    return sid


def test_pilot_codex_rows_listed_and_enter_resumes():
    try:
        from textual.app import App  # noqa: F401
    except Exception:
        print("SKIP test_pilot_codex_rows_listed_and_enter_resumes (textual unavailable)")
        return

    import asyncio
    from textual.app import App

    _seed_standard()
    claude_sid = _write_claude_session("Claude side work")
    facts: dict = {"argvs": []}
    real_build = saikai._build_resume_invocation

    def spy(sid, sessions):
        facts["argvs"].append(real_build(sid, sessions)[0])
        raise RuntimeError("stop-before-spawn (test)")

    def fake_run(self, *a, **kw):
        async def go():
            async with self.run_test(size=(120, 32)) as pilot:
                await pilot.pause(0.5)
                idx = getattr(self, "_sid_index", {})
                facts["has_codex"] = B in idx and idx[B].get("provider") == "codex"
                facts["has_claude"] = claude_sid in idx
                table = self.query_one("#table")
                titles = []
                for rk in list(getattr(table, "rows", {})):
                    try:
                        row = table.get_row(rk)
                        titles.append(str(row[-1]))
                    except Exception:
                        pass
                facts["codex_badged"] = any("◇" in t for t in titles)
                self._open_or_attach_live(B)
                await pilot.pause(0.2)
        asyncio.run(go())

    orig_run, App.run = App.run, fake_run
    orig_argv = sys.argv
    saikai._build_resume_invocation = spy
    try:
        sys.argv = ["saikai", "--all"]
        saikai.main()
    finally:
        saikai._build_resume_invocation = real_build
        App.run = orig_run
        sys.argv = orig_argv

    assert facts.get("has_codex"), facts
    assert facts.get("has_claude"), facts
    assert facts.get("codex_badged"), f"codex rows must carry the ◇ badge: {facts}"
    assert len(facts["argvs"]) == 1, facts
    argv = facts["argvs"][0]
    assert argv[1:3] == ["resume", B], argv


if __name__ == "__main__":
    test_codex_enabled_gate()
    print("PASS test_codex_enabled_gate")
    test_load_codex_sessions_one_row_per_thread()
    print("PASS test_load_codex_sessions_one_row_per_thread")
    test_codex_since_filter_uses_thread_latest_mtime()
    print("PASS test_codex_since_filter_uses_thread_latest_mtime")
    test_build_resume_invocation_codex_argv_and_cwd()
    print("PASS test_build_resume_invocation_codex_argv_and_cwd")
    test_codex_rows_in_scope_filters_here_mode()
    print("PASS test_codex_rows_in_scope_filters_here_mode")
    test_codex_dirs_mtime_bumps_on_new_rollout()
    print("PASS test_codex_dirs_mtime_bumps_on_new_rollout")
    test_needs_attention_skips_codex_rows()
    print("PASS test_needs_attention_skips_codex_rows")
    test_parse_codex_rollout_cache_roundtrip()
    print("PASS test_parse_codex_rollout_cache_roundtrip")
    test_pilot_codex_rows_listed_and_enter_resumes()
    print("PASS test_pilot_codex_rows_listed_and_enter_resumes")
    print("ALL PASS")
