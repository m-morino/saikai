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


def test_missing_both_is_none_not_crash():
    s = {"id": "empty"}
    assert recap._last_active_dt(s) is None
    # sort must not raise on a None-keyed session mixed with real ones
    sessions = [s, {"id": "real", "mtime": time.time(), "last_ts": _iso_ago(1)}]
    recap._apply_sort(sessions, [{"col": "last", "dir": "desc"}])
    assert sessions[0]["id"] == "real"


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
    test_missing_both_is_none_not_crash()
    print("PASS test_missing_both_is_none_not_crash")
    print("ALL PASS")
