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


def test_project_short_strips_prefix_case_insensitively():
    """Claude lowercases the Windows drive letter in the project-dir name, so the
    home-prefix strip must be case-INSENSITIVE — a regression where an exact-case
    startswith left the whole encoded prefix (c--Users-…) in the column."""
    import re as _re
    from pathlib import Path as _P
    home_enc = _re.sub(r"[:/\\.]", "-", str(_P.home()))
    assert recap.project_short(home_enc + "-CLI-myproj") == "CLI-myproj"
    if home_enc[:1].isalpha():
        lowered = home_enc[0].lower() + home_enc[1:]   # Claude-style lowercased drive
        assert recap.project_short(lowered + "-CLI-myproj") == "CLI-myproj"


def test_new_session_stub_has_renderable_fields():
    """A new-session stub carries the fields the list render/sort/group read, so a
    just-launched session shows immediately (before its JSONL is scanned)."""
    s = recap._new_session_stub("sid-123", "/tmp/myproj", "myproj")
    assert s["id"] == "sid-123" and s["is_open"] is True
    assert s["summary"] == "myproj"            # Title column
    assert s["last_active_dt"] is not None     # Last / Recency
    for k in ("first_ts", "last_ts", "mtime", "cwd", "origin_cwd", "real_msgs",
              "n_turns", "parent_id", "topics", "ai_title",
              "project_name", "worktree_label", "primary_topic"):  # render colour-maps + columns
        assert k in s, k
    # the project colour-map / Project column subscript s["project_name"] — must not KeyError
    assert recap.project_short(s["project_name"]) is not None
    # sorts cleanly alongside a real session (keys off last_active_dt)
    recap._apply_sort([s, {"id": "x", "mtime": time.time(), "last_ts": _iso_ago(1)}],
                      [{"col": "last", "dir": "desc"}])


def test_list_title_fallback_no_claude_p():
    """The session-LIST title uses claude's OWN data only (NO claude -p summary):
    native ai-title → first user message → project label → short id. A freshly-
    opened session (stub: no ai_title, no msgs) shows the project — never blank,
    never a claude -p call."""
    from pathlib import Path as _P
    assert recap._list_title({"id": "x" * 16, "ai_title": "Fix auth",
                              "real_msgs": ["hi"]}) == "Fix auth"
    assert recap._list_title({"id": "x" * 16, "ai_title": "",
                              "real_msgs": ["do the thing"]}) == "do the thing"
    stub = recap._new_session_stub("abcd1234-0000-0000-0000-000000000000",
                                   str(_P.home() / "proj"), "proj")
    assert recap._list_title(stub) == recap.project_short(stub["project_name"])  # project, not blank
    assert recap._list_title({"id": "deadbeef-1111"})    # never blank → short id last resort


def test_pane_title_prefers_human_label_over_id():
    """A live pane's tab shows a human label, not a bare session id:
    ai_title → summary → (first user msg) → the term's launch title → short id."""
    sid = "abcd1234-5678-90ab-cdef-1234567890ab"
    assert recap._pane_title({"ai_title": "Fix auth bug"}, sid) == "Fix auth bug"
    assert recap._pane_title({"ai_title": "", "summary": "refactor X"}, sid) == "refactor X"

    class _T:
        title = "my-folder"
    assert recap._pane_title(None, sid, _T()) == "my-folder"   # new session → folder
    assert recap._pane_title(None, sid) == "abcd1234"          # last resort: short id


def test_resolve_resume_cwd_uses_stub_origin_cwd():
    """Shift+F4 restore of an OUT-OF-SCOPE session resumes in the right dir: it
    injects a _new_session_stub with the saved cwd, and _resolve_resume_cwd reads
    that origin_cwd so `claude --resume` targets the correct directory."""
    import tempfile
    import shutil as _sh
    d = tempfile.mkdtemp()
    try:
        stub = recap._new_session_stub("sid-xyz", d, "myproj")
        assert stub["origin_cwd"] == d
        assert recap._resolve_resume_cwd("sid-xyz", [stub]) == d
    finally:
        _sh.rmtree(d, ignore_errors=True)


def test_no_internal_identifiers_in_source():
    """Public-release hygiene: shipped source + docs must carry no author PII or
    org-internal codenames. PII is computed GENERICALLY (the build machine's OS
    username / home-dir name) so this guard file embeds no name itself; the
    codenames are split-concatenated for the same reason. Also flags any e-mail
    address (allowing example.* / noreply). Scans every shipped .py plus the docs."""
    import re as _re
    from pathlib import Path as _P
    root = _P(__file__).resolve().parent.parent
    # build-machine identity, derived at runtime — no literal name lives in this file
    ids = set()
    for tok in (_P.home().name, os.environ.get("USERNAME", ""), os.environ.get("USER", "")):
        tok = (tok or "").strip().lower()
        if len(tok) >= 4:                       # skip trivially short logins
            ids.add(tok)
    # known internal codenames, split so they don't appear verbatim in this file
    codenames = ("chat" + "agc", "work" + "-tools", "edge" + "-auth")
    email_re = _re.compile(r"[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}", _re.I)
    self_name = _P(__file__).name
    targets = (list(root.glob("*.py")) + list((root / "tests").glob("*.py"))
               + list((root / "docs").rglob("*.md"))     # docs ship publicly too
               + [root / "README.md", root / "CLAUDE.md", root / "THIRD-PARTY-NOTICES.md"])
    for f in targets:
        if not f.exists() or f.name == self_name:
            continue
        src = f.read_text(encoding="utf-8")
        low = src.lower()
        bad = [n for n in ids if n in low] + [c for c in codenames if c in low]
        assert not bad, f"{f.name} contains internal identifier(s): {bad}"
        emails = [e for e in email_re.findall(src)
                  if not (e.endswith("example.com") or e.endswith("example.org")
                          or "noreply" in e.lower())]
        assert not emails, f"{f.name} contains e-mail address(es): {emails}"


def test_ram_gate_windows_principled():
    """The live-pane gate is derived from Windows resource management: gate on
    COMMIT headroom (the documented system-freeze cause) + dwMemoryLoad + a
    RELATIVE physical floor, NOT raw available-physical (which counts reclaimable
    standby cache). _ram_fit counts how many ~per_pane panes fit; the binding
    constraint wins; a None field skips its check; None status (macOS) → unbounded."""
    MS = recap._MemStatus
    fit, gate = recap._ram_fit, recap._ram_gate_decision
    kw = dict(max_load=85, min_commit_mb=2048, min_free_phys_pct=8)
    plenty = MS(40, 16000, 20000, 32000)            # everything ample
    assert fit(plenty, 600, **kw)[0] >= 5 and gate(plenty, 600, **kw)[0] is True
    hot = MS(92, 8000, 8000, 32000)                 # high load → blocked despite free RAM
    assert fit(hot, 600, **kw)[0] == 0 and gate(hot, 600, **kw)[0] is False
    low_commit = MS(50, 16000, 2200, 32000)         # commit is the binding (freeze) limit
    f, why = fit(low_commit, 600, **kw)
    assert f == 0 and "commit" in why and gate(low_commit, 600, **kw)[0] is False
    low_phys = MS(50, 2900, 20000, 32000)           # 8% of 32000 = 2560 floor → blocked
    assert gate(low_phys, 600, **kw)[0] is False
    assert gate(None, 600, **kw) == (True, "")      # macOS / unknown → never blocks
    no_commit = MS(None, 16000, None, 32000)        # missing fields skip; phys still applies
    assert gate(no_commit, 600, **kw)[0] is True


def test_parse_macos_vm_stat():
    """macOS RAM probe: vm_stat + hw.memsize → _MemStatus so the gate works on macOS
    too (it was disabled there). Available = reclaimable pages (free + inactive +
    speculative + purgeable) × page size; load = used/total; commit is None (macOS
    has no fixed commit limit → that check skips); bad input → safe degradation."""
    sample = (
        "Mach Virtual Memory Statistics: (page size of 16384 bytes)\n"
        "Pages free:                               50000.\n"
        "Pages active:                            200000.\n"
        "Pages inactive:                          100000.\n"
        "Pages speculative:                        10000.\n"
        "Pages wired down:                        150000.\n"
        "Pages purgeable:                           5000.\n"
        "Pages stored in compressor:              555555.\n"
    )
    total = 16 * 1024 * 1024 * 1024                 # 16 GiB
    st = recap._parse_macos_vm_stat(sample, total)
    # reclaimable = 50000+100000+10000+5000 = 165000 pages × 16384 B
    assert abs(st.avail_phys_mb - 165000 * 16384 / (1024 * 1024)) < 1
    assert abs(st.total_phys_mb - 16384) < 1
    assert st.avail_commit_mb is None               # macOS: no commit limit
    assert 80 < st.load < 90                         # ~84% used
    assert recap._parse_macos_vm_stat(sample, 0) is None         # bad total → None
    assert recap._parse_macos_vm_stat("garbage", total).avail_phys_mb == 0.0  # no pages → 0 (blocks, safe)


def test_color_key_for_modes():
    """[display] color_by selects the Title hue dimension: project (default) /
    worktree / topic / none."""
    s = {"project_name": "proj-enc", "worktree_label": "feat-x", "primary_topic": "auth"}
    assert recap._color_key_for(s, "worktree") == "feat-x"
    assert recap._color_key_for(s, "topic") == "auth"
    assert recap._color_key_for(s, "none") == ""
    assert recap._color_key_for(s, "project") == recap.project_short("proj-enc")
    assert recap._color_key_for(s, "bogus") == recap.project_short("proj-enc")  # → project default
    assert recap._color_key_for({}, "topic") == "(none)"   # empty topic → its own bucket


def test_wt_column_is_sortable():
    """The Wt (worktree) header must sort: SORT_COLS gates _promote_sort_col and it
    omitted 'wt', so the header was a silent no-op while every other column sorted
    (the keyfn already supported col=='wt' via worktree_label)."""
    assert "wt" in recap.SORT_COLS
    sess = [{"id": "a", "worktree_label": "zeta", "mtime": 1.0},
            {"id": "b", "worktree_label": "alpha", "mtime": 2.0}]
    recap._apply_sort(sess, [{"col": "wt", "dir": "asc"}])
    assert [s["id"] for s in sess] == ["b", "a"]   # alpha before zeta


def test_at_live_capacity_counts_inflight_opens():
    """Regression: the live-pane cap must count BOTH registered panes and in-flight
    opens (register is deferred to the async mount worker). Without counting the
    in-flight ones, a Space-batch / Shift+F4-restore loop reads a stale count and
    overruns RECAP_MAX_LIVE (and races into DuplicateIds)."""
    f = recap._at_live_capacity
    assert f(0, 0, 4) is False
    assert f(3, 0, 4) is False
    assert f(4, 0, 4) is True            # full on registered alone
    assert f(2, 2, 4) is True            # 2 registered + 2 in-flight = at cap
    assert f(0, 4, 4) is True            # in-flight alone reaches the cap
    assert f(1, 2, 4) is False           # 3 < 4 → room for one more


def test_live_pane_mount_awaits_pane_removal():
    """Regression (sessions 30540a39 / 0b01b23a 'won't open'): re-opening a session
    whose claude EXITED must AWAIT the deferred remove_pane of the kept dead pane
    BEFORE add_pane — else Textual raises DuplicateIds and the reopen fails silently.
    Guards that _spawn_live_pane delegates to the async _mount_live_pane worker which
    awaits both. Source scan so it runs without textual."""
    from pathlib import Path as _P
    src = _P(__file__).resolve().parent.parent.joinpath("recap.py").read_text(encoding="utf-8")
    assert "async def _mount_live_pane" in src, "awaited mount worker missing"
    assert "await tabs.remove_pane(" in src, "remove_pane must be awaited before add_pane"
    assert "await tabs.add_pane(" in src, "add_pane must be awaited in the mount worker"
    assert "self.run_worker(" in src, "_spawn_live_pane must schedule the mount worker"


def test_split_live_default_on_with_env_opt_out():
    """Split-live is the DEFAULT now; RECAP_SPLIT_LIVE is a tri-state OPT-OUT.
    Only an explicit falsy token (0/false/no/off, case-insensitive, trimmed)
    disables it → legacy full-takeover. Unset / empty / any other value = on
    (a typo'd value fails safe to the default, split-live)."""
    f = recap._split_live_disabled_by_env
    for keep_on in (None, "", "1", "true", "yes", "on", "flase"):
        assert f(keep_on) is False, keep_on
    for opt_out in ("0", "false", "no", "off", "FALSE", " Off ", "NO"):
        assert f(opt_out) is True, opt_out


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
    test_project_short_strips_prefix_case_insensitively()
    print("PASS test_project_short_strips_prefix_case_insensitively")
    test_new_session_stub_has_renderable_fields()
    print("PASS test_new_session_stub_has_renderable_fields")
    test_list_title_fallback_no_claude_p()
    print("PASS test_list_title_fallback_no_claude_p")
    test_pane_title_prefers_human_label_over_id()
    print("PASS test_pane_title_prefers_human_label_over_id")
    test_resolve_resume_cwd_uses_stub_origin_cwd()
    print("PASS test_resolve_resume_cwd_uses_stub_origin_cwd")
    test_no_internal_identifiers_in_source()
    print("PASS test_no_internal_identifiers_in_source")
    test_ram_gate_windows_principled()
    print("PASS test_ram_gate_windows_principled")
    test_parse_macos_vm_stat()
    print("PASS test_parse_macos_vm_stat")
    test_color_key_for_modes()
    print("PASS test_color_key_for_modes")
    test_wt_column_is_sortable()
    print("PASS test_wt_column_is_sortable")
    test_at_live_capacity_counts_inflight_opens()
    print("PASS test_at_live_capacity_counts_inflight_opens")
    test_live_pane_mount_awaits_pane_removal()
    print("PASS test_live_pane_mount_awaits_pane_removal")
    test_split_live_default_on_with_env_opt_out()
    print("PASS test_split_live_default_on_with_env_opt_out")
    print("ALL PASS")
