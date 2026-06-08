"""Headless regression: 'last activity' must reflect a freshly-touched session
even when its tail records carry no timestamp.

Bug (session 6019b00c): Claude appends untimed metadata records (ai-title /
permission-mode / last-prompt) that bump the file mtime but NOT last_ts. The
Last column showed 'now' (mtime) yet Recency sort, Age filter and Date grouping
keyed off the stale last_ts and treated the session as old. _last_active_dt now
unifies all of them on max(mtime, last_ts).

Run:  python tests/test_sort_recency.py
"""
import os
import sys
import time
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import recap


def _iso_ago(days: float) -> str:
    dt = datetime.now(timezone.utc) - timedelta(days=days)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def test_last_active_prefers_mtime_over_stale_last_ts():
    """A session touched now but whose last *timestamped* record is 5d old must
    report ~now, not 5d ago."""
    s = {"mtime": time.time(), "last_ts": _iso_ago(5)}
    la = recap._last_active_dt(s)
    assert la is not None
    assert abs((datetime.now() - la).total_seconds()) < 5, la


def test_last_active_uses_last_ts_when_newer_than_mtime():
    """Restored backup: mtime older than the newest message -> trust the message."""
    s = {"mtime": time.time() - 10 * 86400, "last_ts": _iso_ago(1)}
    la = recap._last_active_dt(s)
    assert la is not None
    # ~1 day ago, NOT 10 days ago
    assert 0.5 * 86400 < (datetime.now() - la).total_seconds() < 2 * 86400, la


def test_recency_sort_puts_freshly_touched_first():
    """The reported bug: Recency (col 'last', desc) must rank a now-touched
    session above a genuinely-older one, regardless of stale last_ts."""
    fresh = {"id": "fresh", "mtime": time.time(),             "last_ts": _iso_ago(5)}
    old   = {"id": "old",   "mtime": time.time() - 3 * 86400, "last_ts": _iso_ago(3)}
    sessions = [old, fresh]                       # deliberately wrong order
    recap._apply_sort(sessions, [{"col": "last", "dir": "desc"}])
    assert [s["id"] for s in sessions] == ["fresh", "old"], [s["id"] for s in sessions]


def test_age_filter_keeps_freshly_touched():
    """'Last 24h' must keep a session touched now even if its last message is 5d old."""
    cut = datetime.now() - timedelta(days=1)
    fresh = {"id": "fresh", "mtime": time.time(), "last_ts": _iso_ago(5)}
    assert (recap._last_active_dt(fresh) or datetime.min) >= cut


def test_date_bucket_uses_mtime():
    """Group-by-Date must file a now-touched session under Today, not 5 days ago."""
    now = datetime.now()
    s = {"id": "x", "mtime": time.time(), "last_ts": _iso_ago(5)}
    la = recap._last_active_dt(s)
    assert recap._date_label(la.date() if la else None, now) == "Today"


def test_sort_select_value_reflects_primary_or_none():
    """The Sort dropdown shows the remembered primary; for a primary the dropdown
    can't represent (header-click sort by turns/fav) it returns None so compose
    OMITS value= — passing Select.BLANK (== False in Textual 8.2.7) would crash
    launch with InvalidSelectValueError."""
    orig = recap._load_sort

    def _spec(col):
        return [{"col": col, "dir": "desc"},
                {"col": "-", "dir": "desc"}, {"col": "-", "dir": "desc"}]
    try:
        recap._load_sort = lambda: _spec("last")
        assert recap._sort_select_value() == "last"
        recap._load_sort = lambda: _spec("title")
        assert recap._sort_select_value() == "title"
        recap._load_sort = lambda: _spec("date")
        assert recap._sort_select_value() == "date"
        recap._load_sort = lambda: _spec("turns")     # not a dropdown option
        assert recap._sort_select_value() is None
    finally:
        recap._load_sort = orig


def test_sort_select_value_ignores_secondary_column():
    """Only the PRIMARY (priority-0) key maps to the dropdown. A header-click sort
    with a non-representable primary (turns) + representable secondary (date) must
    return None — else the box shows 'Created time' and the on_select_changed
    echo-guard swallows a genuine re-pick of it (the bug a multi-level sort hit)."""
    orig = recap._load_sort
    try:
        recap._load_sort = lambda: [{"col": "turns", "dir": "desc"},
                                    {"col": "date", "dir": "desc"},
                                    {"col": "-", "dir": "desc"}]
        assert recap._sort_select_value() is None
    finally:
        recap._load_sort = orig


def test_n_turns_derived_from_real_msgs_not_inflated():
    """Turns = human prompts (len real_msgs), NOT the raw type:'user' record count
    (tool_result records are also type:'user' and inflated it 10-50x).
    _enrich_session derives it from the already-filtered real_msgs and ignores a
    stale/inflated parsed['n_turns'], so even OLD caches self-heal."""
    from pathlib import Path
    parsed = {
        "first_ts": "2026-01-01T00:00:00.000Z",
        "last_ts": "2026-01-01T01:00:00.000Z",
        "real_msgs": ["prompt one", "prompt two", "prompt three"],
        "n_turns": 999,          # inflated raw count — must be ignored
        "mtime": time.time(),
    }
    r = recap._enrich_session("sid-x", parsed, Path("nonexistent.jsonl"), parsed["mtime"])
    assert r["n_turns"] == 3, r["n_turns"]


def test_recency_flags_use_current_time():
    """:active/:recent must reflect NOW, not the load-time is_active/is_recent
    snapshot (which goes stale as the picker stays open)."""
    now = time.time()
    fresh = {"id": "f", "mtime": now - 60}        # 1 min ago
    old = {"id": "o", "mtime": now - 3600}        # 1 h ago
    assert recap._is_recent_now(fresh, now) is True
    assert recap._is_recent_now(old, now) is False
    assert recap._is_active_now(fresh, now) is True
    assert recap._is_active_now(old, now) is False
    # is_open snapshot still wins even when long-untouched
    opened = {"id": "x", "mtime": now - 99999, "is_open": True}
    assert recap._is_active_now(opened, now) is True


def test_enrich_stamps_last_active_dt():
    """_enrich_session memoises last_active_dt; _last_active_dt then reads it."""
    from pathlib import Path
    parsed = {"first_ts": "2026-01-01T00:00:00.000Z",
              "last_ts": "2026-01-01T00:00:00.000Z",
              "real_msgs": [], "mtime": time.time()}
    r = recap._enrich_session("sid-y", parsed, Path("x.jsonl"), parsed["mtime"])
    assert r.get("last_active_dt") is not None
    assert recap._last_active_dt(r) is r["last_active_dt"]   # reads the stamp


def test_summary_cache_keys_on_last_ts():
    """Summary cache validity keys on last_ts (content), not mtime: metadata-only
    mtime drift keeps the cache (no needless Haiku re-summarise), a last_ts change
    invalidates it. Legacy caches without last_ts fall back to the mtime window."""
    orig = recap._read_json
    try:
        recap._read_json = lambda *a, **k: {"summary": "S", "last_ts": "T1", "mtime": 100.0}
        assert recap._load_cache("sid", 999.0, "T1") == "S"     # mtime drifted, last_ts matches → hit
        assert recap._load_cache("sid", 100.0, "T2") is None    # last_ts changed → miss
        recap._read_json = lambda *a, **k: {"summary": "L", "mtime": 100.0}  # legacy, no last_ts
        assert recap._load_cache("sid", 100.4, "x") == "L"      # within mtime tolerance → hit
        assert recap._load_cache("sid", 200.0, "x") is None     # outside tolerance → miss
    finally:
        recap._read_json = orig


def test_build_groups_date_order_recent_first():
    """Single-pass bucket-max must keep Today first and older date buckets after
    (the ordering the old double-pass max() produced)."""
    now = datetime.now()
    today = {"id": "t", "mtime": time.time(), "last_ts": _iso_ago(0)}
    old = {"id": "o", "mtime": time.time() - 5 * 86400, "last_ts": _iso_ago(5)}
    groups = recap._build_groups([old, today], "date", set(), now)
    labels = [g[0] for g in groups]
    assert labels[0] == "Today", labels
    assert labels[1] != "Today" and len(labels) == 2, labels


def test_build_groups_project_order_by_recency():
    """Project buckets ordered by most-recent activity (single-pass bucket-max)."""
    now = datetime.now()
    a = {"id": "a", "mtime": time.time() - 5 * 86400, "last_ts": _iso_ago(5),
         "project_name": "old-proj"}
    b = {"id": "b", "mtime": time.time(), "last_ts": _iso_ago(0),
         "project_name": "new-proj"}
    groups = recap._build_groups([a, b], "project", set(), now)
    labels = [g[0] for g in groups]
    new_lbl = recap.project_short("new-proj") or "(none)"
    old_lbl = recap.project_short("old-proj") or "(none)"
    assert labels.index(new_lbl) < labels.index(old_lbl), labels


def test_build_forest_windowed_parent_assignment():
    """The O(1) gap-prune in _build_forest must not change parent assignment:
    a recent same-cwd session still wins over a closer-in-time different-cwd one,
    and the oldest is a root."""
    from datetime import timedelta
    base = datetime(2026, 1, 10, 12, 0, 0)

    def _s(sid, cwd, minutes_ago):
        t = (base - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        return {"id": sid, "cwd": cwd, "git_branch": "", "first_ts": t, "last_ts": t,
                "ai_title": "", "real_msgs": []}

    a = _s("A", "/proj/x", 60)      # oldest, same cwd as C
    b = _s("B", "/other", 30)       # closer in time, different cwd
    c = _s("C", "/proj/x", 0)       # newest
    sessions = [c, a, b]
    recap._build_forest(sessions)
    by = {s["id"]: s for s in sessions}
    assert by["C"]["parent_id"] == "A", by["C"]   # cwd weight beats time proximity
    assert by["A"]["parent_id"] is None           # oldest → root


def test_render_header_includes_worktree_and_model():
    """Preview header surfaces worktree + model (+ entry surface), read from the
    transcript like a statusline. branch was already there."""
    import tempfile
    import shutil
    from pathlib import Path
    d = Path(tempfile.mkdtemp())
    try:
        proj = d / "myproj"
        proj.mkdir()
        f = proj / "sess.jsonl"
        f.write_text(
            '{"type":"user","timestamp":"2026-01-01T00:00:00.000Z",'
            '"message":{"content":"hi there please help me build a thing"}}\n'
            '{"entrypoint":"vscode"}\n'
            '{"type":"assistant","message":{"model":"claude-opus-4-8",'
            '"content":[{"type":"text","text":"ok"}]}}\n',
            encoding="utf-8")
        s = {"id": "sid123", "ai_title": "T", "first_ts": "2026-01-01T00:00:00.000Z",
             "last_ts": "2026-01-01T00:00:00.000Z", "n_turns": 1, "mtime": time.time(),
             "cwd": "/x", "git_branch": "main", "worktree_label": "wt-feature",
             "jsonl_path": f, "real_msgs": ["hi there please help me build a thing"]}
        out = "\n".join(recap._render_header(s))
        assert "worktree:" in out and "wt-feature" in out, out
        assert "model:" in out and "claude-opus-4-8" in out, out
        assert "via vscode" in out, out
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_toggle_in_set_refuses_to_clobber_unreadable_file():
    """The production-class bug: a transient read error must NOT let a toggle save
    a 1-element set over a populated favorites/hidden file (erasing the rest)."""
    import tempfile
    import shutil
    import json as _json
    from pathlib import Path
    d = Path(tempfile.mkdtemp())
    try:
        p = d / "favorite.json"
        p.write_text('["favA", "favB"]', encoding="utf-8")
        recap._toggle_in_set(p, "favC")                       # happy path
        assert set(_json.loads(p.read_text())) == {"favA", "favB", "favC"}
        p.write_text("{ not valid json", encoding="utf-8")    # exists but unreadable
        raised = False
        try:
            recap._toggle_in_set(p, "favD")
        except Exception:
            raised = True
        assert raised, "must refuse to toggle an unreadable existing file"
        assert "favD" not in p.read_text(), "must NOT clobber the unreadable file"
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_invalidate_active_sessions_drops_cache():
    """Reload must re-read the live registry, not the frozen launch snapshot."""
    recap._active_sessions_cache = {"sid": "open"}
    recap._invalidate_active_sessions()
    assert recap._active_sessions_cache is None


def test_refresh_summary_only_matches_uuid_caches():
    """--refresh-summary deletes only <uuid>.json summary caches — settings files
    (sort / clusters / favorites / options) must NOT match the UUID filter."""
    assert recap._UUID_RE.fullmatch("6019b00c-734f-4e73-932d-b6453956a8fd")
    for safe in ("sort", "global-clusters", "favorite", "hidden", "options"):
        assert not recap._UUID_RE.fullmatch(safe), safe


def test_missing_both_is_none_not_crash():
    s = {"id": "empty"}
    assert recap._last_active_dt(s) is None
    # sort must not raise on a None-keyed session mixed with real ones
    sessions = [s, {"id": "real", "mtime": time.time(), "last_ts": _iso_ago(1)}]
    recap._apply_sort(sessions, [{"col": "last", "dir": "desc"}])
    assert sessions[0]["id"] == "real"


def test_no_app_binding_steals_a_readline_ctrl_key():
    """recap must never bind an app action to a bare Ctrl+<letter>: those are
    readline editing keys the user types in the search box and inside live claude
    panes (and claude itself binds Ctrl+R/T/L). App shortcuts live on FUNCTION
    keys instead. Ctrl+C (quit) is the sole allowed bare-Ctrl binding. Regression
    for the Ctrl+K close-all that wiped every live pane (2026-06). Scans the
    source so it runs without textual (the App/Binding class needs textual)."""
    import re
    from pathlib import Path
    src = Path(__file__).resolve().parent.parent.joinpath("recap.py").read_text(encoding="utf-8")
    keys = re.findall(r'Binding\(\s*"([^"]+)"', src)
    assert keys, "no Binding(...) entries found — regex/structure changed?"
    offenders = [k for k in keys if re.fullmatch(r"ctrl\+[a-z]", k) and k != "ctrl+c"]
    assert not offenders, f"app bindings on readline Ctrl+letter keys: {offenders}"
    # the function keys we relocated them onto must actually be bound
    for must in ("f5", "f6", "f7", "f8", "f9", "f10", "shift+f10"):
        assert must in keys, f"expected F-key binding {must!r} missing: {keys}"


def test_build_new_invocation_starts_fresh_session_with_id():
    """New-session launch must pass --session-id (NOT --resume) so claude starts a
    fresh session keyed to that uuid, with the chosen cwd and RECAP_RESUME env."""
    import tempfile
    import shutil as _sh
    d = tempfile.mkdtemp()
    try:
        argv, cwd, env = recap._build_new_invocation(
            d, "11111111-2222-3333-4444-555555555555", [])
        assert "--session-id" in argv, argv
        assert "11111111-2222-3333-4444-555555555555" in argv, argv
        assert "--resume" not in argv, argv
        assert cwd == d
        assert env.get("RECAP_RESUME") == "1"
    finally:
        _sh.rmtree(d, ignore_errors=True)


def test_build_groups_state_keeps_pinned_live_in_state_group():
    """案B: in STATE grouping a pinned session that is live (Running / Needs input
    / Open) stays in its state group (pin = ★ badge), NOT hoisted to Pinned; only
    non-live pinned (Recent / Idle) go to Pinned. Date grouping still hoists every
    favorite to Pinned (unchanged)."""
    now = datetime.now()
    sess = [
        {"id": "run",  "_state": "Running",     "mtime": time.time(),             "last_ts": _iso_ago(0)},
        {"id": "wait", "_state": "Needs input", "mtime": time.time(),             "last_ts": _iso_ago(0)},
        {"id": "idle", "_state": "Idle",         "mtime": time.time() - 9 * 86400, "last_ts": _iso_ago(9)},
    ]
    favs = {"run", "wait", "idle"}
    g = {lbl: [s["id"] for s in members]
         for lbl, members in recap._build_groups(sess, "state", favs, now)}
    assert "run" in g.get("Running", []), g            # live pinned stays in its state group
    assert "wait" in g.get("Needs input", []), g
    assert "idle" in g.get("Pinned", []), g            # non-live pinned -> Pinned shortcut
    assert "run" not in g.get("Pinned", []), g         # live pinned NOT hoisted
    assert "wait" not in g.get("Pinned", []), g
    # Date grouping unchanged: every favorite hoisted to Pinned.
    gd = {lbl: [s["id"] for s in members]
          for lbl, members in recap._build_groups(sess, "date", favs, now)}
    assert set(gd.get("Pinned", [])) == {"run", "wait", "idle"}, gd


if __name__ == "__main__":
    test_last_active_prefers_mtime_over_stale_last_ts()
    print("PASS test_last_active_prefers_mtime_over_stale_last_ts")
    test_last_active_uses_last_ts_when_newer_than_mtime()
    print("PASS test_last_active_uses_last_ts_when_newer_than_mtime")
    test_recency_sort_puts_freshly_touched_first()
    print("PASS test_recency_sort_puts_freshly_touched_first")
    test_age_filter_keeps_freshly_touched()
    print("PASS test_age_filter_keeps_freshly_touched")
    test_date_bucket_uses_mtime()
    print("PASS test_date_bucket_uses_mtime")
    test_sort_select_value_reflects_primary_or_none()
    print("PASS test_sort_select_value_reflects_primary_or_none")
    test_sort_select_value_ignores_secondary_column()
    print("PASS test_sort_select_value_ignores_secondary_column")
    test_n_turns_derived_from_real_msgs_not_inflated()
    print("PASS test_n_turns_derived_from_real_msgs_not_inflated")
    test_recency_flags_use_current_time()
    print("PASS test_recency_flags_use_current_time")
    test_enrich_stamps_last_active_dt()
    print("PASS test_enrich_stamps_last_active_dt")
    test_summary_cache_keys_on_last_ts()
    print("PASS test_summary_cache_keys_on_last_ts")
    test_build_groups_date_order_recent_first()
    print("PASS test_build_groups_date_order_recent_first")
    test_build_groups_project_order_by_recency()
    print("PASS test_build_groups_project_order_by_recency")
    test_build_forest_windowed_parent_assignment()
    print("PASS test_build_forest_windowed_parent_assignment")
    test_render_header_includes_worktree_and_model()
    print("PASS test_render_header_includes_worktree_and_model")
    test_toggle_in_set_refuses_to_clobber_unreadable_file()
    print("PASS test_toggle_in_set_refuses_to_clobber_unreadable_file")
    test_invalidate_active_sessions_drops_cache()
    print("PASS test_invalidate_active_sessions_drops_cache")
    test_refresh_summary_only_matches_uuid_caches()
    print("PASS test_refresh_summary_only_matches_uuid_caches")
    test_missing_both_is_none_not_crash()
    print("PASS test_missing_both_is_none_not_crash")
    test_no_app_binding_steals_a_readline_ctrl_key()
    print("PASS test_no_app_binding_steals_a_readline_ctrl_key")
    test_build_new_invocation_starts_fresh_session_with_id()
    print("PASS test_build_new_invocation_starts_fresh_session_with_id")
    test_build_groups_state_keeps_pinned_live_in_state_group()
    print("PASS test_build_groups_state_keeps_pinned_live_in_state_group")
    print("ALL PASS")
