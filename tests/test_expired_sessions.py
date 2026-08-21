"""Claude Code deletes transcripts after 30 days (`cleanupPeriodDays`, an OPEN
upstream complaint). Everything the session knew — the reasoning trail, not the
code — goes with it, and saikai used to delete its own parsed copy 60 days later
on the assumption that a record "self-heals via re-parse if the session still
exists".

These tests pin the opposite promise: a session saikai has SEEN stays findable
after Claude removes its transcript, and sessions Claude removed before saikai
ever ran come back from `~/.claude/history.jsonl` (the one per-machine file
Claude Code never prunes). Expired rows are searchable and non-resumable.

Run:  python tests/test_expired_sessions.py
"""
import json
import os
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Point saikai at a throwaway home BEFORE importing it — CACHE_DIR,
# PROJECTS_ROOT and CLAUDE_CONFIG_ROOT are all derived at import time.
_FAKE_HOME = Path(tempfile.mkdtemp(prefix="saikai-expired-test-"))
for _var in ("USERPROFILE", "HOME", "APPDATA", "LOCALAPPDATA", "XDG_CONFIG_HOME"):
    os.environ[_var] = str(_FAKE_HOME)
os.environ.pop("SAIKAI_CONFIG", None)
os.environ.pop("CLAUDE_CONFIG_DIR", None)
os.environ["SAIKAI_SUMMARIZE_ENABLED"] = "0"

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import saikai

PROJ = saikai.PROJECTS_ROOT / "-home-me-app"


def _iso_ago(days: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime(
        "%Y-%m-%dT%H:%M:%S.000Z")


def _reset() -> None:
    """Clear every derived directory so each test starts from a known world."""
    import shutil
    for d in (saikai.PARSED_DIR, PROJ):
        shutil.rmtree(d, ignore_errors=True)
    for f in (saikai.HISTORY_FILE, saikai.HISTORY_INDEX_FILE):
        try:
            f.unlink()
        except OSError:
            pass
    PROJ.mkdir(parents=True, exist_ok=True)
    saikai.PARSED_DIR.mkdir(parents=True, exist_ok=True)
    saikai._history_index_cache_clear()


def _write_transcript(sid: str, prompts, *, cwd="/home/me/app",
                      days_ago: float = 40.0, branch="main") -> Path:
    """A minimal but realistic Claude transcript."""
    p = PROJ / f"{sid}.jsonl"
    recs = []
    for i, text in enumerate(prompts):
        recs.append({
            "type": "user", "timestamp": _iso_ago(days_ago - i * 0.01),
            "cwd": cwd, "gitBranch": branch,
            "message": {"role": "user", "content": text},
        })
    p.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in recs),
                 encoding="utf-8")
    old = time.time() - days_ago * 86400
    os.utime(p, (old, old))
    return p


def _write_history(entries) -> None:
    """`~/.claude/history.jsonl`: one record per prompt, the file Claude keeps."""
    saikai.HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    for sid, project, text, days_ago in entries:
        lines.append(json.dumps({
            "display": text, "pastedContents": {},
            "timestamp": int((time.time() - days_ago * 86400) * 1000),
            "project": project, "sessionId": sid,
        }, ensure_ascii=False))
    saikai.HISTORY_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    saikai._history_index_cache_clear()


# ── the core promise ────────────────────────────────────────────────────────

def test_session_survives_claude_deleting_its_transcript():
    """saikai parsed it once; Claude then removed the JSONL. The session — with
    its prompts — must still be findable."""
    _reset()
    sid = "aaaaaaaa-1111-4111-8111-111111111111"
    path = _write_transcript(sid, ["fix the OAuth refresh loop",
                                   "why does the token expire early?"])
    live = saikai.load_sessions_in_dir(PROJ, None)
    assert [s["id"] for s in live] == [sid], live

    path.unlink()                                   # Claude's 30-day cleanup
    assert saikai.load_sessions_in_dir(PROJ, None) == []

    gone = saikai.load_expired_sessions(set())
    assert [s["id"] for s in gone] == [sid], gone
    s = gone[0]
    assert s["is_expired"] is True
    assert s["expired_source"] == "parsed"
    assert "fix the OAuth refresh loop" in " ".join(s["real_msgs"])
    # the metadata that makes a row readable survived too
    assert s["cwd"] == "/home/me/app" and s["git_branch"] == "main"
    assert s["project_name"] == PROJ.name


def test_expired_row_carries_the_full_session_field_set():
    """Render / sort / group / forest all read a session dict positionally-ish;
    a missing key surfaces as a crash three layers away. An expired row must
    carry every key a live one does."""
    _reset()
    sid = "bbbbbbbb-2222-4222-8222-222222222222"
    _write_transcript(sid, ["one", "two"])
    live = saikai.load_sessions_in_dir(PROJ, None)[0]
    (PROJ / f"{sid}.jsonl").unlink()
    exp = saikai.load_expired_sessions(set())[0]

    missing = set(live) - set(exp)
    assert not missing, f"expired row is missing live keys: {sorted(missing)}"
    assert isinstance(exp["jsonl_path"], Path)
    assert exp["is_open"] is False and exp["is_active"] is False
    assert exp["session_status"] == "expired"
    # the row reads as remembered-but-gone, in the calm (dim) tier
    import re as _re
    glyph = _re.sub(r"\x1b\[[0-9;]*m", "", saikai._activity_marker(exp)).strip()
    assert glyph == "-", repr(glyph)
    assert saikai._MARKER_COLOR["-"] == "dim"


def test_known_sessions_are_not_duplicated_as_expired():
    """A session whose transcript is still on disk must never appear twice."""
    _reset()
    sid = "cccccccc-3333-4333-8333-333333333333"
    _write_transcript(sid, ["still here"])
    live = saikai.load_sessions_in_dir(PROJ, None)
    assert saikai.load_expired_sessions({s["id"] for s in live}) == []


# ── the cache GC that used to destroy the record ────────────────────────────

def test_cache_gc_keeps_a_record_whose_transcript_is_gone():
    """The GC prunes a stale parsed record so it can re-parse. That reasoning
    holds ONLY while the transcript exists; when it doesn't, the record is the
    last copy and pruning it is data loss."""
    _reset()
    kept = "dddddddd-4444-4444-8444-444444444444"
    healable = "eeeeeeee-5555-4555-8555-555555555555"
    _write_transcript(kept, ["expired work"])
    _write_transcript(healable, ["live work"])
    saikai.load_sessions_in_dir(PROJ, None)
    (PROJ / f"{kept}.jsonl").unlink()               # Claude's cleanup

    ancient = time.time() - 400 * 86400
    for sid in (kept, healable):
        f = saikai.PARSED_DIR / f"{sid}.json"
        os.utime(f, (ancient, ancient))

    saikai._sweep_cache_litter()

    assert (saikai.PARSED_DIR / f"{kept}.json").exists(), \
        "pruned the only surviving copy of a session Claude deleted"
    assert not (saikai.PARSED_DIR / f"{healable}.json").exists(), \
        "a record whose transcript still exists should still be prunable"


# ── backfill from the file Claude never prunes ──────────────────────────────

def test_history_jsonl_backfills_sessions_saikai_never_saw():
    """The sessions already destroyed before saikai was installed. Their prompts
    live on in ~/.claude/history.jsonl, which Claude Code does not prune."""
    _reset()
    old = "ffffffff-6666-4666-8666-666666666666"
    oneshot = "99999999-7777-4777-8777-777777777777"
    _write_history([
        (old, "/home/me/app", "design the payment retry ladder", 120),
        (old, "/home/me/app", "what did we decide about idempotency keys?", 119),
        (oneshot, "/home/me/bot", "summarise this diff", 118),   # a `claude -p` run
    ])
    got = {s["id"]: s for s in saikai.load_expired_sessions(set())}
    assert old in got, got
    assert got[old]["expired_source"] == "history"
    assert got[old]["cwd"] == "/home/me/app"
    assert "idempotency keys" in " ".join(got[old]["real_msgs"])
    assert oneshot not in got, \
        "a single-prompt session with no transcript is a -p invocation, not work"


def test_parsed_record_wins_over_history_backfill():
    """When both sources know a session, the parsed record is richer — the
    backfill must not shadow it or produce a duplicate row."""
    _reset()
    sid = "88888888-8888-4888-8888-888888888888"
    _write_transcript(sid, ["real transcript prompt", "second"])
    saikai.load_sessions_in_dir(PROJ, None)
    (PROJ / f"{sid}.jsonl").unlink()
    _write_history([(sid, "/home/me/app", "history copy", 50),
                    (sid, "/home/me/app", "history copy 2", 49)])

    rows = saikai.load_expired_sessions(set())
    assert len(rows) == 1, rows
    assert rows[0]["expired_source"] == "parsed"
    assert "real transcript prompt" in " ".join(rows[0]["real_msgs"])


# ── behaviour in the app ────────────────────────────────────────────────────

def test_expired_sessions_are_searchable_by_their_prompt_text():
    """The whole point: find the session by what was said in it."""
    _reset()
    sid = "77777777-9999-4999-8999-999999999999"
    _write_transcript(sid, ["the flaky websocket reconnect storm"])
    saikai.load_sessions_in_dir(PROJ, None)
    (PROJ / f"{sid}.jsonl").unlink()
    exp = saikai.load_expired_sessions(set())
    assert saikai._session_matches_text(exp[0], "websocket reconnect")
    assert not saikai._session_matches_text(exp[0], "kubernetes")


def test_expired_sessions_refuse_resume_with_a_reason():
    """`claude --resume` on a deleted transcript reports 'No conversation found'.
    saikai must say why instead of launching into that."""
    _reset()
    sid = "66666666-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    _write_transcript(sid, ["gone"])
    saikai.load_sessions_in_dir(PROJ, None)
    (PROJ / f"{sid}.jsonl").unlink()
    exp = saikai.load_expired_sessions(set())[0]
    why = saikai._resume_block_reason(exp)
    assert why and "transcript" in why.lower(), why
    live = {"id": "x", "is_expired": False}
    assert saikai._resume_block_reason(live) == ""


def test_archive_can_be_turned_off():
    """A user who doesn't want the memory gets exactly the old behaviour."""
    _reset()
    sid = "55555555-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
    _write_transcript(sid, ["one", "two"])
    saikai.load_sessions_in_dir(PROJ, None)
    (PROJ / f"{sid}.jsonl").unlink()
    os.environ["SAIKAI_ARCHIVE"] = "0"
    try:
        assert saikai.load_expired_sessions(set()) == []
    finally:
        os.environ.pop("SAIKAI_ARCHIVE", None)
    assert saikai.load_expired_sessions(set())


def test_expired_list_is_capped_newest_first():
    """An unbounded list would swamp the live sessions it sits beside."""
    _reset()
    for i in range(6):
        sid = f"1111{i:04d}-cccc-4ccc-8ccc-cccccccccccc"
        _write_transcript(sid, [f"session number {i}"], days_ago=100 - i)
    saikai.load_sessions_in_dir(PROJ, None)
    for i in range(6):
        (PROJ / f"1111{i:04d}-cccc-4ccc-8ccc-cccccccccccc.jsonl").unlink()

    os.environ["SAIKAI_ARCHIVE_MAX"] = "3"
    try:
        rows = saikai.load_expired_sessions(set())
    finally:
        os.environ.pop("SAIKAI_ARCHIVE_MAX", None)
    assert len(rows) == 3, rows
    # newest first: sessions 5, 4, 3 (days_ago 95, 96, 97)
    assert [s["id"][4:8] for s in rows] == ["0005", "0004", "0003"], \
        [s["id"] for s in rows]


def test_since_filter_applies_to_expired_rows():
    """--days must mean the same thing for remembered sessions as for live ones."""
    _reset()
    recent = "22220000-dddd-4ddd-8ddd-dddddddddddd"
    ancient = "22221111-dddd-4ddd-8ddd-dddddddddddd"
    _write_transcript(recent, ["recent work"], days_ago=3)
    _write_transcript(ancient, ["ancient work"], days_ago=200)
    saikai.load_sessions_in_dir(PROJ, None)
    for sid in (recent, ancient):
        (PROJ / f"{sid}.jsonl").unlink()
    since = datetime.now(timezone.utc) - timedelta(days=30)
    rows = saikai.load_expired_sessions(set(), since=since)
    assert [s["id"] for s in rows] == [recent], rows


def test_history_index_survives_a_corrupt_line():
    """history.jsonl is appended by another process; a torn last line must cost
    one prompt, not the whole index."""
    _reset()
    sid = "33330000-eeee-4eee-8eee-eeeeeeeeeeee"
    _write_history([(sid, "/home/me/app", "first prompt", 10),
                    (sid, "/home/me/app", "second prompt", 9)])
    with open(saikai.HISTORY_FILE, "a", encoding="utf-8") as f:
        f.write('{"display": "torn half of a rec')
    saikai._history_index_cache_clear()
    idx = saikai._history_prompt_index()
    assert sid in idx and len(idx[sid]["prompts"]) == 2, idx


def test_history_row_is_titled_by_what_the_session_was_about():
    """A history-only row has no AI title, so its first prompt becomes the title —
    and sessions routinely open with `/model` or `/usage`, which name nothing."""
    _reset()
    sid = "44440000-ffff-4fff-8fff-ffffffffffff"
    _write_history([(sid, "/home/me/app", "/model", 40),
                    (sid, "/home/me/app", "/usage", 39),
                    (sid, "/home/me/app", "trace the duplicate webhook delivery", 38)])
    row = saikai.load_expired_sessions(set())[0]
    assert row["ai_title"] == "trace the duplicate webhook delivery", row["ai_title"]
    # the skipped commands are still SEARCHABLE, just not the label
    assert saikai._session_matches_text(row, "/usage")


def test_history_backfill_excludes_automation_sessions():
    """`claude -p` hooks leave the same footprint here as in projects/; the scan
    filters them out and the memory must not reintroduce them."""
    _reset()
    sid = "55550000-ffff-4fff-8fff-ffffffffffff"
    hook = saikai.HOOK_PROMPT_MARKERS[0]
    _write_history([(sid, "/home/me/app", hook + " ...diff...", 30),
                    (sid, "/home/me/app", hook + " ...more...", 29)])
    assert saikai.load_expired_sessions(set()) == []


def test_history_prompts_use_the_same_admission_test_as_transcripts():
    """A prompt must mean the same thing whichever source a row came from."""
    _reset()
    sid = "66660000-ffff-4fff-8fff-ffffffffffff"
    _write_history([(sid, "/home/me/app", "ok", 20),           # too short
                    (sid, "/home/me/app", "rewrite the retry backoff", 19),
                    (sid, "/home/me/app", "and add jitter to it", 18)])
    row = saikai.load_expired_sessions(set())[0]
    assert row["real_msgs"] == ["rewrite the retry backoff", "and add jitter to it"], \
        row["real_msgs"]


if __name__ == "__main__":
    for _name, _fn in sorted(list(globals().items())):
        if _name.startswith("test_") and callable(_fn):
            _fn()
            print("PASS", _name)
    print("OK test_expired_sessions")
