"""Headless tests for the TOML config layer: location resolution, load (with safe
degradation), and the env > config > default precedence resolver.

Run:  python tests/test_config.py
"""
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# Headless harness: no terminal to watch, and the watchdog's os._exit on a
# false-positive orphan detection would kill the test process. (production-only)
os.environ["SAIKAI_NO_TERMINAL_WATCHDOG"] = "1"
import saikai


def test_config_path_honors_env():
    p = Path(tempfile.gettempdir()) / "saikai-cfg-test.toml"
    os.environ["SAIKAI_CONFIG"] = str(p)
    try:
        assert saikai._config_path() == p
    finally:
        os.environ.pop("SAIKAI_CONFIG", None)


def test_load_config_parses_and_degrades():
    d = Path(tempfile.mkdtemp())
    good = d / "config.toml"
    good.write_text("[summary]\nenabled = true\n[limits]\nmax_live = 9\n", encoding="utf-8")
    os.environ["SAIKAI_CONFIG"] = str(good)
    try:
        saikai._reset_config_cache()
        c = saikai._load_config()
        assert c["summary"]["enabled"] is True and c["limits"]["max_live"] == 9
    finally:
        os.environ.pop("SAIKAI_CONFIG", None)
        saikai._reset_config_cache()
    # corrupt → {} (no raise); missing → {} too
    bad = d / "bad.toml"
    bad.write_text("this is not toml = = =", encoding="utf-8")
    os.environ["SAIKAI_CONFIG"] = str(bad)
    try:
        saikai._reset_config_cache()
        assert saikai._load_config() == {}
        os.environ["SAIKAI_CONFIG"] = str(d / "nope.toml")
        saikai._reset_config_cache()
        assert saikai._load_config() == {}
    finally:
        os.environ.pop("SAIKAI_CONFIG", None)
        saikai._reset_config_cache()


def test_cfg_precedence_env_over_config_over_default():
    d = Path(tempfile.mkdtemp())
    f = d / "config.toml"
    f.write_text("[limits]\nmax_live = 30\nclaude_mb = 700\n", encoding="utf-8")
    os.environ["SAIKAI_CONFIG"] = str(f)
    saikai._reset_config_cache()
    try:
        os.environ["SAIKAI_MAX_LIVE"] = "12"                                  # env wins
        assert saikai._cfg("limits", "max_live", "SAIKAI_MAX_LIVE", 64, int) == 12
        os.environ.pop("SAIKAI_MAX_LIVE", None)                               # → config
        assert saikai._cfg("limits", "max_live", "SAIKAI_MAX_LIVE", 64, int) == 30
        assert saikai._cfg("limits", "claude_mb", "SAIKAI_CLAUDE_MB", 600.0, float) == 700.0
        assert saikai._cfg("limits", "missing", "SAIKAI_NOPE", 5, int) == 5    # default
        os.environ["SAIKAI_MAX_LIVE"] = "bad"                                 # bad cast → default
        assert saikai._cfg("limits", "max_live", "SAIKAI_MAX_LIVE", 64, int) == 64
    finally:
        for k in ("SAIKAI_CONFIG", "SAIKAI_MAX_LIVE", "SAIKAI_CLAUDE_MB"):
            os.environ.pop(k, None)
        saikai._reset_config_cache()


def test_cfg_bool_parses_truthy_falsy():
    assert saikai._cfg_bool(True) is True
    assert saikai._cfg_bool("true") is True and saikai._cfg_bool("on") is True
    assert saikai._cfg_bool("0") is False and saikai._cfg_bool("false") is False
    assert saikai._cfg_bool(None, default=True) is True
    assert saikai._cfg_bool(None) is False


def test_summary_enabled_matrix():
    for k in ("SAIKAI_SUMMARIZE_ENABLED", "SAIKAI_SUMMARIZE_CMD", "SAIKAI_CONFIG"):
        os.environ.pop(k, None)
    saikai._reset_config_cache()
    saikai._set_summary_forced_off(False)
    try:
        assert saikai._summary_enabled() is False                    # default OFF (opt-in)
        os.environ["SAIKAI_SUMMARIZE_ENABLED"] = "1"
        assert saikai._summary_enabled() is True
        os.environ.pop("SAIKAI_SUMMARIZE_ENABLED")
        os.environ["SAIKAI_SUMMARIZE_CMD"] = "mytool --json"
        assert saikai._summary_enabled() is True                     # custom backend → enabled
        saikai._set_summary_forced_off(True)
        assert saikai._summary_enabled() is False                    # --no-summary wins over config
    finally:
        for k in ("SAIKAI_SUMMARIZE_ENABLED", "SAIKAI_SUMMARIZE_CMD"):
            os.environ.pop(k, None)
        saikai._set_summary_forced_off(False)
        saikai._reset_config_cache()


def test_summarize_session_skips_llm_when_disabled():
    saikai._set_summary_forced_off(True)   # deterministic OFF, no claude -p
    try:
        s = {"id": "sid-nollm-test", "ai_title": "", "is_open": False, "mtime": 1.0,
             "last_ts": "", "real_msgs": ["build the thing first"]}
        # returns the first-message heuristic without invoking claude -p
        assert saikai.summarize_session(s) == "build the thing first"
        s["ai_title"] = "Native Title"
        assert saikai.summarize_session(s) == "Native Title"   # ai_title preferred (still no claude -p)
    finally:
        saikai._set_summary_forced_off(False)


def test_validate_keymap():
    ids = {"refresh", "favorite", "close", "tree"}
    applied, errs = saikai._validate_keymap({
        "refresh": "f1",       # ok
        "favorite": "F6",      # ok (lowercased)
        "leader": "ctrl+g",    # skipped (handled by the leader state machine)
        "bogus": "f2",         # unknown action id
        "close": "ctrl+w",     # reserved readline key
        "tree": "f1",          # duplicate (f1 already → refresh)
    }, ids)
    assert applied == {"refresh": "f1", "favorite": "f6"}
    assert any("bogus" in e for e in errs)
    assert any("reserved" in e for e in errs)
    assert any("already bound" in e for e in errs)
    assert saikai._validate_keymap({}, ids) == ({}, [])


def test_leader_map():
    id2act = {"refresh": "refresh", "favorite": "toggle_fav", "close": "close_live"}
    m, errs = saikai._leader_map(
        {"refresh": "r", "favorite": "f", "close": "r", "bad": "x", "diff": "f8"}, id2act)
    assert m == {"r": "refresh", "f": "toggle_fav"}   # 'close'→r dup; 'diff'→f8 multi-char skipped
    assert any("already used" in e for e in errs)     # duplicate letter
    assert any("bad" in e for e in errs)              # unknown action id


def test_init_config_writes_parseable_template():
    import tomllib
    d = Path(tempfile.mkdtemp())
    f = d / "config.toml"
    os.environ["SAIKAI_CONFIG"] = str(f)
    try:
        saikai._reset_config_cache()
        assert saikai._init_config(force=False) == 0 and f.is_file()
        with open(f, "rb") as fh:
            cfg = tomllib.load(fh)                       # template is valid TOML
        assert cfg["summary"]["enabled"] is False        # documented defaults
        assert cfg["launch"]["auto_permission"] is False
        assert cfg["limits"]["max_live"] == 64
        assert cfg["limits"]["scrollback_lines"] == 2000  # the memory lever ships in the template
        assert saikai._init_config(force=False) == 1      # refuse overwrite
        assert saikai._init_config(force=True) == 0       # --force overwrites
    finally:
        os.environ.pop("SAIKAI_CONFIG", None)
        saikai._reset_config_cache()


def test_resolved_settings_covers_and_applies_runtime_knobs():
    """Settings/--print-config must list the same knobs the runtime consumes."""
    d = Path(tempfile.mkdtemp())
    f = d / "config.toml"
    f.write_text(
        '[summary]\nmodel = "sonnet"\n'
        '[display]\nsplit_ratio = 0.5\n'
        '[limits]\nscrollback_lines = 1234\n'
        '[keys]\nrelease = "ctrl+g"\n',
        encoding="utf-8",
    )
    os.environ["SAIKAI_CONFIG"] = str(f)
    try:
        saikai._reset_config_cache()
        shown = {(sec, key): val for sec, key, val, _src in saikai._resolved_settings()}
        assert shown[("summary", "model")] == "sonnet"
        assert shown[("display", "split_ratio")] == 0.5
        assert shown[("limits", "scrollback_lines")] == 1234
        assert shown[("keys", "release")] == "ctrl+g"
        assert saikai._summary_model() == "sonnet"
        assert saikai._release_focus_key() == "ctrl+g"
    finally:
        os.environ.pop("SAIKAI_CONFIG", None)
        saikai._reset_config_cache()


def test_color_legend_explains_context_without_false_last_color_claim():
    project = saikai._color_legend("project")
    assert "same project" in project.lower()
    assert "symbols show state" in project.lower()
    assert "last column" not in project.lower()

    assert "same worktree" in saikai._color_legend("worktree").lower()
    assert "same topic" in saikai._color_legend("topic").lower()
    assert "title colors are disabled" in saikai._color_legend("none").lower()


def test_removed_cluster_mode_has_no_dangling_runtime_references():
    src = Path(saikai.__file__).read_text(encoding="utf-8")
    for stale in (
        "_get_cluster_mode(",
        "_toggle_cluster_mode(",
        "_global_cluster_assign(",
        '"--toggle-cluster"',
        '"--refresh-clusters"',
        '"toggle_cluster"',
    ):
        assert stale not in src, f"removed cluster mode still referenced: {stale}"


def test_reset_terminal_modes_guarded_and_emits():
    """atexit/crash terminal restore: silent on a non-tty stderr (never pollutes
    a redirected stream), emits the mouse/focus disable + show-cursor sequence on
    a tty. Never raises."""
    import io
    import sys as _sys
    saved = _sys.stderr
    # non-tty → writes nothing
    buf = io.StringIO()                       # StringIO.isatty() is False
    _sys.stderr = buf
    try:
        saikai._reset_terminal_modes()
    finally:
        _sys.stderr = saved
    assert buf.getvalue() == ""
    # tty-like → emits the disable sequence, ending with show-cursor (?25h)
    class _Tty(io.StringIO):
        def isatty(self):
            return True
    tbuf = _Tty()
    _sys.stderr = tbuf
    try:
        saikai._reset_terminal_modes()
    finally:
        _sys.stderr = saved
    out = tbuf.getvalue()
    assert "\033[?1003l" in out and "\033[?1006l" in out and "\033[?1004l" in out
    assert out.endswith("\033[?25h")


def test_child_spawn_env_strips_parent_session_markers():
    """A child agent saikai spawns must boot as its OWN session: the parent Claude
    session markers (esp. CLAUDE_NO_SESSION_PERSISTENCE, which otherwise suppresses
    the child's transcript and breaks discovery/checkpoint) are stripped, while a
    user's CLAUDE_CONFIG_DIR override and auth are preserved. base is not mutated."""
    base = {
        "CLAUDE_NO_SESSION_PERSISTENCE": "true",
        "CLAUDECODE": "1",
        "CLAUDE_CODE_SESSION_ID": "parent-sid",
        "CLAUDE_CODE_CHILD_SESSION": "1",
        "CLAUDE_CODE_ENTRYPOINT": "cli",
        "CLAUDE_PROJECT_DIR": "/parent/project",
        "TEXTUAL_LOG": "saikai.log",
        "CLAUDE_CONFIG_DIR": "/custom/.claude",      # user override → preserved
        "CLAUDE_CODE_GIT_BASH_PATH": "C:/git/bash",  # config the child needs → preserved
        "ANTHROPIC_API_KEY": "sk-xxx",               # auth → preserved
        "PATH": "/usr/bin",
    }
    env = saikai._child_spawn_env(base)
    for k in ("CLAUDE_NO_SESSION_PERSISTENCE", "CLAUDECODE", "CLAUDE_CODE_SESSION_ID",
              "CLAUDE_CODE_CHILD_SESSION", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_PROJECT_DIR",
              "TEXTUAL_LOG"):
        assert k not in env, f"{k} must be stripped from the child env"
    assert env.get("CLAUDE_CONFIG_DIR") == "/custom/.claude", "user config-dir override dropped"
    assert env.get("CLAUDE_CODE_GIT_BASH_PATH") == "C:/git/bash", "git-bash config dropped"
    assert env.get("ANTHROPIC_API_KEY") == "sk-xxx", "auth must be preserved"
    assert "CLAUDE_NO_SESSION_PERSISTENCE" in base, "base must NOT be mutated"


def test_child_spawn_env_strips_virtualenv_from_var_and_path():
    """uv's ephemeral VIRTUAL_ENV is removed from both the var and PATH so the
    child's `uv` doesn't warn about a stale venv."""
    sep = os.pathsep
    venv = "/proj/.venv"
    bindir = "Scripts" if sys.platform == "win32" else "bin"
    base = {
        "VIRTUAL_ENV": venv,
        "VIRTUAL_ENV_PROMPT": "(.venv)",
        "PATH": sep.join([str(Path(venv) / bindir), "/usr/bin"]),
    }
    env = saikai._child_spawn_env(base)
    assert "VIRTUAL_ENV" not in env and "VIRTUAL_ENV_PROMPT" not in env
    assert str(Path(venv) / bindir) not in env["PATH"].split(sep)
    assert "/usr/bin" in env["PATH"].split(sep)


def test_activity_marker_bg_agent_distinct_from_open():
    """A running background agent (registry kind=bg) gets the '&' marker, distinct
    from the '@' of an interactive session open elsewhere — so saikai doesn't mistake
    a headless, non-resumable bg job for an attachable window. is_bg wins even though
    a bg session is also is_open. (#bg)"""
    assert "&" in saikai._activity_marker({"is_bg": True})
    assert "&" in saikai._activity_marker({"is_bg": True, "is_open": True})
    assert "@" in saikai._activity_marker({"is_open": True})
    assert "&" not in saikai._activity_marker({"is_open": True})


def test_desktop_index_dir_prefers_recent_over_most_entries():
    """The sync target is the account Desktop is CURRENTLY writing to (newest
    local_*.json), not the one with the most history — else sync lands in a
    signed-out account and the logged-in Desktop shows nothing. (#H8)"""
    root = Path(tempfile.mkdtemp()) / "claude-code-sessions"
    big_old = root / "orgA" / "userA"
    small_new = root / "orgB" / "userB"
    big_old.mkdir(parents=True)
    small_new.mkdir(parents=True)
    for i in range(3):                                   # more entries, OLDER
        f = big_old / f"local_{i}.json"
        f.write_text("{}", encoding="utf-8")
        os.utime(f, (1_000_000, 1_000_000))
    f = small_new / "local_x.json"                       # fewer entries, NEWER
    f.write_text("{}", encoding="utf-8")
    os.utime(f, (2_000_000, 2_000_000))
    old_root = saikai.DESKTOP_SESSIONS_ROOT
    try:
        saikai.DESKTOP_SESSIONS_ROOT = root
        assert saikai._desktop_index_dir() == small_new, "should pick the recently-written account"
    finally:
        saikai.DESKTOP_SESSIONS_ROOT = old_root


def test_desktop_index_dir_prefers_authoritative_account_over_recency():
    """When Desktop's config resolves an existing <org>/<user> account dir, sync
    targets THAT — even if a different (signed-out) account dir was written more
    recently. Falls back to recency only when config can't resolve. (#recon-desktop-acct)"""
    base = Path(tempfile.mkdtemp())
    appdata = base / "appdata"
    (appdata / "Claude").mkdir(parents=True)
    (appdata / "Claude" / "cowork-enabled-cli-ops.json").write_text(
        json.dumps({"ownerAccountId": "ORG"}), encoding="utf-8")
    (appdata / "Claude" / "config.json").write_text(json.dumps({
        "dxt:allowlistEnabled:USER": True,
        "dxt:allowlistLastUpdated:USER": "2026-06-18T00:00:00Z"}), encoding="utf-8")
    root = appdata / "Claude" / "claude-code-sessions"
    auth = root / "ORG" / "USER"
    auth.mkdir(parents=True)
    (auth / "local_old.json").write_text("{}", encoding="utf-8")
    os.utime(auth / "local_old.json", (1_000, 1_000))                 # OLD
    stale = root / "ORGX" / "USERX"
    stale.mkdir(parents=True)
    (stale / "local_new.json").write_text("{}", encoding="utf-8")
    os.utime(stale / "local_new.json", (9_000_000, 9_000_000))        # NEWEST mtime
    saved = (saikai._DESKTOP_APPDATA, saikai.DESKTOP_SESSIONS_ROOT)
    try:
        saikai._DESKTOP_APPDATA = appdata
        saikai.DESKTOP_SESSIONS_ROOT = root
        assert saikai._desktop_index_dir() == auth, "authoritative account must beat recency"
    finally:
        saikai._DESKTOP_APPDATA, saikai.DESKTOP_SESSIONS_ROOT = saved


def test_dedup_sessions_by_id_keeps_newest():
    """Two case-variant project dirs holding the same sid must collapse to one row
    (newest mtime), so the sid-keyed table can't raise DuplicateKey. (#H2)"""
    a = {"id": "sid1", "mtime": 100.0, "via": "C--dir"}
    b = {"id": "sid1", "mtime": 200.0, "via": "c--dir"}   # same sid, newer
    c = {"id": "sid2", "mtime": 50.0}
    out = saikai._dedup_sessions_by_id([a, b, c])
    ids = sorted(s["id"] for s in out)
    assert ids == ["sid1", "sid2"], ids
    kept = next(s for s in out if s["id"] == "sid1")
    assert kept["via"] == "c--dir", "newest-mtime variant should win"
    # No duplicates → same list object back (no needless copy)
    uniq = [{"id": "x", "mtime": 1.0}, {"id": "y", "mtime": 2.0}]
    assert saikai._dedup_sessions_by_id(uniq) is uniq


def test_ctx_usage_skips_synthetic_and_zero_records():
    """The context gauge must read the last REAL usage, not a <synthetic>/all-zero
    interrupt record (Esc/abort/API error) — accepting one would report 0K/empty and
    mask a near-full window, inverting the checkpoint-relevant fill reading. (#H5)"""
    d = Path(tempfile.mkdtemp())
    p = d / "sess.jsonl"
    real = ('{"message": {"model": "claude-opus-4-8", "usage": '
            '{"input_tokens": 5, "cache_read_input_tokens": 277000, '
            '"cache_creation_input_tokens": 0}}}')
    synth = ('{"message": {"model": "<synthetic>", "usage": '
             '{"input_tokens": 0, "cache_read_input_tokens": 0, '
             '"cache_creation_input_tokens": 0}}}')
    p.write_text(real + "\n" + synth + "\n", encoding="utf-8")
    tokens, model = saikai._ctx_usage_from_jsonl(p)
    assert tokens == 277005, tokens             # the REAL usage, not the synthetic 0
    assert model == "claude-opus-4-8", model    # window inference keeps the real model


def test_ctx_usage_caches_on_mtime_size():
    """The gauge reads this on every cursor move, so it must serve a (mtime,size)
    cache and only re-read when the transcript actually changes. (#H5 / audit I3)"""
    d = Path(tempfile.mkdtemp())
    p = d / "s.jsonl"
    rec = ('{"message": {"model": "claude-opus-4-8", "usage": {"input_tokens": 10, '
           '"cache_read_input_tokens": 100, "cache_creation_input_tokens": 0}}}')
    p.write_text(rec + "\n", encoding="utf-8")
    saikai._CTX_USAGE_CACHE.clear()
    assert saikai._ctx_usage_from_jsonl(p) == (110, "claude-opus-4-8")
    assert str(p) in saikai._CTX_USAGE_CACHE
    # Poison the cached RESULT (same mtime/size) → an unchanged file serves it.
    mtime, size, _ = saikai._CTX_USAGE_CACHE[str(p)]
    saikai._CTX_USAGE_CACHE[str(p)] = (mtime, size, (999, "cached-sentinel"))
    assert saikai._ctx_usage_from_jsonl(p) == (999, "cached-sentinel"), "unchanged file must hit cache"
    # Append + bump mtime → cache invalidated → fresh read of the new last usage.
    rec2 = ('{"message": {"model": "claude-opus-4-8", "usage": {"input_tokens": 20, '
            '"cache_read_input_tokens": 500, "cache_creation_input_tokens": 0}}}')
    with open(p, "a", encoding="utf-8") as f:
        f.write(rec2 + "\n")
    os.utime(p, (3_000_000, 3_000_000))
    assert saikai._ctx_usage_from_jsonl(p) == (520, "claude-opus-4-8"), "changed file must re-read"


def test_save_options_preserves_others_on_unreadable_and_nondict():
    """_save_options must not wipe the other persisted options when the existing
    file is present-but-unreadable, and must not crash on a non-dict JSON file. (#H7)"""
    d = Path(tempfile.mkdtemp())
    opt = d / "options.json"
    old_file = saikai.OPTIONS_FILE
    try:
        saikai.OPTIONS_FILE = opt
        saikai._save_options({"search_bar": True})              # absent → first write
        assert saikai._read_json(opt, {}) == {"search_bar": True}
        saikai._save_options({"split_ratio": 0.4})              # readable dict → merge
        got = saikai._read_json(opt, {})
        assert got.get("search_bar") is True and got.get("split_ratio") == 0.4
        opt.write_text("{ corrupt = =", encoding="utf-8")       # present + UNREADABLE
        saikai._save_options({"days": 7})
        assert opt.read_text(encoding="utf-8").startswith("{ corrupt"), \
            "corrupt options file was overwritten — other options would be wiped"
        opt.write_text("[]", encoding="utf-8")                  # non-dict valid JSON
        saikai._save_options({"scope": "all"})                  # must not crash
        assert saikai._read_json(opt, {}) == {"scope": "all"}
    finally:
        saikai.OPTIONS_FILE = old_file


def test_parse_session_survives_nondict_line_and_message():
    """A bare non-dict JSON line and a truthy non-dict `message` must NOT abort the
    parse loop — later records (ts, cwd, real prompts) must still be collected. (#audit-parse-msg)"""
    d = Path(tempfile.mkdtemp())
    jsonl = d / "sid-parse.jsonl"
    lines = [
        '{"type":"user","timestamp":"2026-06-01T00:00:00Z","cwd":"/c/work",'
        '"message":{"role":"user","content":"first real prompt"}}',
        '["a","bare","array","line"]',                         # non-dict line
        '{"type":"user","message":"a non-dict message string"}',  # truthy non-dict message
        '{"type":"user","timestamp":"2026-06-01T01:00:00Z",'
        '"message":{"role":"user","content":"second real prompt"}}',
    ]
    jsonl.write_text("\n".join(lines) + "\n", encoding="utf-8")
    old_parsed = saikai.PARSED_DIR
    try:
        saikai.PARSED_DIR = d / "parsed"
        saikai.PARSED_DIR.mkdir(parents=True, exist_ok=True)
        s = saikai.parse_session(jsonl)
    finally:
        saikai.PARSED_DIR = old_parsed
    assert s is not None
    assert "first real prompt" in s["real_msgs"]
    assert "second real prompt" in s["real_msgs"], "record after the bad lines was lost"
    assert s["last_ts"] == "2026-06-01T01:00:00Z", "last_ts must advance past the bad lines"


def test_parse_cache_misses_on_append_within_mtime_tolerance():
    """An append that lands inside the 0.5s mtime tolerance must still re-parse
    because the byte size changed — no stale cache HIT. (#audit-parsecache)"""
    d = Path(tempfile.mkdtemp())
    jsonl = d / "sid-cache.jsonl"
    jsonl.write_text('{"type":"user","timestamp":"2026-06-01T00:00:00Z",'
                     '"message":{"content":"first prompt alpha"}}\n', encoding="utf-8")
    old_parsed = saikai.PARSED_DIR
    try:
        saikai.PARSED_DIR = d / "parsed"
        saikai.PARSED_DIR.mkdir(parents=True, exist_ok=True)
        s1 = saikai.parse_session(jsonl)
        assert "first prompt alpha" in s1["real_msgs"]
        with open(jsonl, "a", encoding="utf-8") as f:
            f.write('{"type":"user","timestamp":"2026-06-01T00:00:00Z",'
                    '"message":{"content":"second prompt beta"}}\n')
        os.utime(jsonl, (s1["mtime"], s1["mtime"]))   # SAME mtime → only size differs
        s2 = saikai.parse_session(jsonl)
        assert "second prompt beta" in s2["real_msgs"], \
            "append within mtime tolerance was served from stale cache"
    finally:
        saikai.PARSED_DIR = old_parsed


def test_ctx_usage_splits_on_newline_only():
    """A usage record whose neighbour line contains U+2028 must still parse — the
    tail reader splits on '\\n' only, not str.splitlines(). (#audit-splitlines)"""
    d = Path(tempfile.mkdtemp())
    p = d / "u.jsonl"
    rec = ('{"message": {"model": "claude-opus-4-8", "content": "para break", '
           '"usage": {"input_tokens": 7, "cache_read_input_tokens": 100, '
           '"cache_creation_input_tokens": 0}}}')
    p.write_text(rec + "\n", encoding="utf-8")
    saikai._CTX_USAGE_CACHE.clear()
    assert saikai._ctx_usage_from_jsonl(p) == (107, "claude-opus-4-8")


def test_last_assistant_falls_back_to_whole_file_for_huge_final_turn():
    """A final assistant turn larger than the 400 KB tail window must still be
    returned via the whole-file fallback. (#audit-tailseek)"""
    d = Path(tempfile.mkdtemp())
    p = d / "h.jsonl"
    big = "X" * 500_000
    rec = json.dumps({"type": "assistant",
                      "message": {"role": "assistant",
                                  "content": [{"type": "text", "text": big}]}})
    p.write_text(rec + "\n", encoding="utf-8")
    out = saikai._last_assistant_text_from_jsonl(p)
    assert out == big, "huge final assistant turn was dropped by the tail seek"


def test_find_project_dir_requires_segment_boundary():
    """A candidate key matching only MID-segment must not win; an exact / boundary
    match must. (#audit-projdir-substr)"""
    fake = [Path("c--users-me-aidas"), Path("c--users-me-cli-saikai")]
    saved = (saikai._project_dirs, saikai.PROJECTS_ROOT)
    try:
        saikai._project_dirs = lambda _root: fake
        saikai.PROJECTS_ROOT = Path(tempfile.mkdtemp())
        # cwd key 'c-users-me-cli-saikai' contains 'aidas' nowhere on a boundary,
        # but the substring 'aida' appears inside no segment → only saikai matches.
        got = saikai.find_project_dir(Path("/c/users/me/cli/saikai"))
        assert got == Path("c--users-me-cli-saikai")
        # A genuinely unrelated cwd whose key embeds 'aidas' mid a longer segment
        # ('aidaszone') must NOT match the 'aidas' project dir.
        none_or_other = saikai.find_project_dir(Path("/c/users/me/aidaszone"))
        assert none_or_other != Path("c--users-me-aidas"), "mid-segment substring wrongly matched"
    finally:
        saikai._project_dirs, saikai.PROJECTS_ROOT = saved


def test_cell_width_zero_for_combining_and_zwj():
    """Combining marks and zero-width joiners overlay the previous cell → width 0,
    so accented/ZWJ text doesn't over-count in the --table path. (#audit-cellwidth)"""
    assert saikai._cell_width("a") == 1
    assert saikai._cell_width("あ") == 2          # hiragana あ — wide
    assert saikai._cell_width("́") == 0          # combining acute accent
    assert saikai._cell_width("‍") == 0          # ZWJ
    assert saikai._cell_width("﻿") == 0          # BOM / ZWNBSP


def test_extract_topics_parses_newline_and_bullets():
    """raw=True replies that are newline- or bullet-separated must yield multiple
    topics, not collapse to one truncated line. (#audit-topics-raw)"""
    saved = saikai.call_claude_haiku
    try:
        saikai.call_claude_haiku = lambda *a, **k: "- alpha\n- beta\n* gamma"
        out = saikai._extract_topics_haiku({"ai_title": "x", "real_msgs": ["hello world"]})
    finally:
        saikai.call_claude_haiku = saved
    assert out == ["alpha", "beta", "gamma"], out


def test_empty_topics_are_persisted():
    """An empty topic result must be cached (key present) so _get_cached_topics
    returns [] not None and the Haiku call isn't re-paid every run. (#audit-topics-empty)"""
    d = Path(tempfile.mkdtemp())
    old_parsed = saikai.PARSED_DIR
    try:
        saikai.PARSED_DIR = d
        (d / "sid-t.json").write_text(json.dumps({"mtime": 1.0, "origin_cwd": "/c"}),
                                      encoding="utf-8")
        saikai._save_topics_to_cache("sid-t", [])
        assert saikai._get_cached_topics("sid-t") == []   # present, not None
    finally:
        saikai.PARSED_DIR = old_parsed


def test_set_lineage_refuses_to_wipe_on_unreadable_file():
    """A present-but-unreadable lineage file must make _set_lineage RAISE rather
    than collapse the whole map to one entry. (#audit-lineage)"""
    d = Path(tempfile.mkdtemp())
    lf = d / "lineage.json"
    saved = (saikai.LINEAGE_FILE, saikai._LINEAGE_CACHE, saikai._LINEAGE_MTIME)
    try:
        saikai.LINEAGE_FILE = lf
        saikai._LINEAGE_CACHE, saikai._LINEAGE_MTIME = None, None
        saikai._set_lineage("child1", "parent1", "/c/p1.jsonl")     # first write OK
        assert "child1" in json.loads(lf.read_text(encoding="utf-8"))
        lf.write_text("{ corrupt = =", encoding="utf-8")            # now unreadable
        try:
            saikai._set_lineage("child2", "parent2", "/c/p2.jsonl")
            assert False, "expected RuntimeError on unreadable lineage file"
        except RuntimeError:
            pass
        assert lf.read_text(encoding="utf-8").startswith("{ corrupt"), \
            "unreadable lineage file was overwritten — child1 would be wiped"
    finally:
        saikai.LINEAGE_FILE, saikai._LINEAGE_CACHE, saikai._LINEAGE_MTIME = saved


def test_new_session_stub_preserves_drive_letter_case():
    """The placeholder project key must preserve the cwd drive-letter casing
    (real dirs are uppercase 'C--…'), not force-lowercase it. (#audit-drivecase)"""
    s = saikai._new_session_stub("sid-x", "C:/work/repo", "title")
    assert s["project_name"].startswith("C-"), s["project_name"]
    assert not s["project_name"].startswith("c-")


def test_session_pid_live_rejects_reused_pid():
    """A registered PID counts as live only if the snapshot shows it's a Claude
    process; a recycled PID owned by something else is rejected. (#audit-pidreuse)"""
    assert saikai._is_session_pid_live(os.getpid(), None) is True   # no snapshot → bare liveness
    idx = {111: ("claude.exe", 1), 222: ("explorer.exe", 1), 444: ("node.exe", 1)}
    assert saikai._is_session_pid_live(111, idx) is True
    assert saikai._is_session_pid_live(444, idx) is True
    assert saikai._is_session_pid_live(222, idx) is False           # reused → unrelated proc
    assert saikai._is_session_pid_live(333, idx) is False           # not in snapshot at all


def test_resolve_resume_cwd_prefers_recent_sibling():
    """When the selected session's own cwds are gone, the sibling fallback picks the
    MOST-RECENT sibling cwd, not the first in (sort-dependent) list order. (#audit-sibling-cwd)"""
    d1 = Path(tempfile.mkdtemp())
    d2 = Path(tempfile.mkdtemp())
    proj = Path("/fake/projects/key")
    selected = {"id": "sel", "origin_cwd": "/no/such/dir1", "cwd": "/no/such/dir2",
                "jsonl_path": proj / "sel.jsonl"}
    older = {"id": "o1", "origin_cwd": str(d1), "jsonl_path": proj / "o1.jsonl", "mtime": 100.0}
    newer = {"id": "o2", "origin_cwd": str(d2), "jsonl_path": proj / "o2.jsonl", "mtime": 200.0}
    out = saikai._resolve_resume_cwd("sel", [selected, older, newer])  # older first in list
    assert out == str(d2), "should pick the most-recent sibling cwd, not list-order first"


def test_is_bg_default_denies_unknown_live_kind():
    """A LIVE session whose kind is non-empty and != 'interactive' (bg OR a future
    kind) is non-attachable (is_bg); a dormant session (absent from the registry)
    and an interactive one are not. Default-deny: refusing resume is recoverable,
    resuming a live session corrupts it. (#recon-unknown-kind)"""
    saved = (saikai._active_sessions_cache, saikai._active_kinds_cache, saikai._active_jobids_cache)
    try:
        saikai._active_sessions_cache = {"i": "idle", "b": "busy", "u": "busy"}
        saikai._active_kinds_cache = {"i": "interactive", "b": "bg", "u": "future-kind"}
        saikai._active_jobids_cache = {}
        def _bg(sid):
            r = saikai._enrich_session(sid, {"first_ts": "t", "origin_cwd": "/c", "real_msgs": []},
                                       Path("/c/x.jsonl"), 0.0)
            return r["is_bg"]
        assert _bg("i") is False, "interactive must be attachable"
        assert _bg("b") is True, "bg must be non-attachable"
        assert _bg("u") is True, "unknown live kind must default-deny"
        assert _bg("dormant") is False, "dormant (not in registry) must not be is_bg"
    finally:
        (saikai._active_sessions_cache, saikai._active_kinds_cache, saikai._active_jobids_cache) = saved
        saikai._invalidate_active_sessions()


def test_bg_job_state_join_and_marker():
    """A bg session joins jobs/<jobId>/state.json; a 'blocked' job (needs your
    clarification) yields a distinct activity marker. (#recon-bg-jobs)"""
    d = Path(tempfile.mkdtemp())
    (d / "jobs" / "job1").mkdir(parents=True)
    (d / "jobs" / "job1" / "state.json").write_text(
        json.dumps({"state": "blocked", "needs": "clarify: continue or stop?",
                    "detail": "awaiting"}), encoding="utf-8")
    saved = (saikai.CLAUDE_CONFIG_ROOT, saikai._active_sessions_cache,
             saikai._active_kinds_cache, saikai._active_jobids_cache)
    try:
        saikai.CLAUDE_CONFIG_ROOT = d
        saikai._active_sessions_cache = {"bgsid": "busy"}
        saikai._active_kinds_cache = {"bgsid": "bg"}
        saikai._active_jobids_cache = {"bgsid": "job1"}
        saikai._JOB_STATE_CACHE.clear()
        js = saikai._job_state_for("bgsid")
        assert js and js["state"] == "blocked" and js["needs"].startswith("clarify")
        assert saikai._job_state_for("not-a-bg") is None    # no jobId → no join
        # blocked marker reachable + glyph stays '&' (no new colliding marker)
        m = saikai._activity_marker({"is_bg": True, "job_state": "blocked", "job_needs": "clarify: X?"})
        assert "&" in m
        assert "&" in saikai._activity_marker({"is_bg": True, "job_state": "done"})
    finally:
        (saikai.CLAUDE_CONFIG_ROOT, saikai._active_sessions_cache,
         saikai._active_kinds_cache, saikai._active_jobids_cache) = saved
        saikai._invalidate_active_sessions()


def test_resolve_resume_cwd_prefers_worktree_origin():
    """A worktree session's worktree-state.originalCwd outranks the plain origin_cwd
    for resume (the latter may be the isolated .claude/worktrees/ path). (#recon-worktree-cwd)"""
    real = Path(tempfile.mkdtemp())
    sel = {"id": "s", "worktree_origin_cwd": str(real),
           "origin_cwd": "/no/such/iso", "cwd": "/no/such/iso2",
           "jsonl_path": Path("/p/s.jsonl")}
    assert saikai._resolve_resume_cwd("s", [sel]) == str(real)


def test_parse_session_captures_worktree_origin_cwd():
    """parse_session records worktreeSession.originalCwd from a worktree-state line. (#recon-worktree-cwd)"""
    d = Path(tempfile.mkdtemp())
    jsonl = d / "sid-wt.jsonl"
    jsonl.write_text("\n".join([
        json.dumps({"type": "user", "timestamp": "2026-06-01T00:00:00Z", "cwd": "/c/wt/iso",
                    "message": {"content": "hello prompt here"}}),
        json.dumps({"type": "worktree-state",
                    "worktreeSession": {"originalCwd": "/c/repo/root", "worktreePath": "/c/wt/iso"}}),
    ]) + "\n", encoding="utf-8")
    old = saikai.PARSED_DIR
    try:
        saikai.PARSED_DIR = d / "parsed"
        saikai.PARSED_DIR.mkdir(parents=True, exist_ok=True)
        s = saikai.parse_session(jsonl)
        assert s["worktree_origin_cwd"] == "/c/repo/root", s.get("worktree_origin_cwd")
    finally:
        saikai.PARSED_DIR = old


def test_load_active_sessions_honors_config_root():
    """The live-session registry must be read from the SAME root the provider uses
    for transcripts (CLAUDE_CONFIG_DIR or ~/.claude), not a hard-coded ~/.claude —
    else every session reads dead when CLAUDE_CONFIG_DIR relocates the store, which
    the README/CHANGELOG already claim is supported. (#recon-configdir)"""
    d = Path(tempfile.mkdtemp())
    (d / "sessions").mkdir()
    (d / "sessions" / "4242.json").write_text(
        json.dumps({"pid": 4242, "sessionId": "cfgroot-sid",
                    "status": "busy", "kind": "interactive"}),
        encoding="utf-8")
    saved_root = saikai.CLAUDE_CONFIG_ROOT
    saved_live = saikai._is_session_pid_live
    try:
        saikai.CLAUDE_CONFIG_ROOT = d
        saikai._is_session_pid_live = lambda pid, idx: True   # bypass real liveness
        saikai._invalidate_active_sessions()
        active = saikai._load_active_sessions()
        assert active.get("cfgroot-sid") == "busy", active   # read from the relocated root
    finally:
        saikai.CLAUDE_CONFIG_ROOT = saved_root
        saikai._is_session_pid_live = saved_live
        saikai._invalidate_active_sessions()


def test_desktop_entry_omits_unknown_model_and_marks_title_auto():
    """A Desktop entry must NOT fabricate a model: when the resolved model is
    None the `model` key is omitted (Desktop picks), and titleSource is "auto"
    because saikai's title is always derived, never user-typed. (#8953)"""
    s = {"id": "sid-abc", "real_msgs": ["hello there"], "cwd": "/c/work"}
    e = saikai._desktop_entry(s, None)
    assert "model" not in e, "unknown model must be omitted, not hardcoded"
    assert e["titleSource"] == "auto"
    assert e["cliSessionId"] == "sid-abc"
    assert e["sessionId"].startswith("local_")
    assert e["title"] == "hello there"
    # Security-flavored optional keys must NOT be fabricated onto a synced row. (#recon-desktop-fab)
    assert "chromePermissionMode" not in e
    assert "classifierSummaryEnabled" not in e
    assert e["permissionMode"] == "default"   # least-privilege, not fabricated "auto"
    # A resolved model is carried through verbatim.
    e2 = saikai._desktop_entry(s, "claude-sonnet-4-6")
    assert e2["model"] == "claude-sonnet-4-6"


def test_desktop_default_model_mirrors_newest_account_entry():
    """The fallback model mirrors the account's most-recently-written entry that
    carries a model — not a hardcoded version, and skipping model-less entries. (#8953)"""
    idx = Path(tempfile.mkdtemp())
    older = idx / "local_old.json"
    older.write_text(json.dumps({"model": "claude-opus-4-7[1m]"}), encoding="utf-8")
    os.utime(older, (1_000_000, 1_000_000))
    modelless = idx / "local_none.json"                 # newest, but no model → ignored
    modelless.write_text(json.dumps({"cliSessionId": "x"}), encoding="utf-8")
    os.utime(modelless, (3_000_000, 3_000_000))
    newer = idx / "local_new.json"
    newer.write_text(json.dumps({"model": "claude-sonnet-4-6"}), encoding="utf-8")
    os.utime(newer, (2_000_000, 2_000_000))
    assert saikai._desktop_default_model(idx) == "claude-sonnet-4-6"
    assert saikai._desktop_default_model(Path(tempfile.mkdtemp())) is None  # empty store


def test_sync_desktop_dedups_within_run_and_mirrors_model():
    """One sync run must create at most ONE entry per cliSessionId even when the
    same sid is yielded from two project dirs (#8916), and an unknown transcript
    model must mirror the account's existing model, never the old hardcoded
    opus-4-8 (#8953)."""
    root = Path(tempfile.mkdtemp()) / "claude-code-sessions"
    idx = root / "orgA" / "userA"
    idx.mkdir(parents=True)
    pre = idx / "local_pre.json"                         # account's current model = sonnet
    pre.write_text(json.dumps({"cliSessionId": "old", "model": "claude-sonnet-4-6"}),
                   encoding="utf-8")
    dup = {"id": "dup-sid", "real_msgs": ["hi"], "cwd": "/c/x",
           "first_ts": None, "jsonl_path": "x"}
    saved = (saikai.DESKTOP_SESSIONS_ROOT, saikai.PROJECTS_ROOT,
             saikai._project_dirs, saikai.load_sessions_in_dir, saikai._session_surface_model)
    try:
        saikai.DESKTOP_SESSIONS_ROOT = root
        saikai.PROJECTS_ROOT = Path(tempfile.mkdtemp())
        saikai._project_dirs = lambda _root: ["d1", "d2"]          # two dirs…
        saikai.load_sessions_in_dir = lambda _d, _days: [dict(dup)]  # …both yield the SAME sid
        saikai._session_surface_model = lambda _j: ("cli", None)   # no model in transcript
        saikai.cmd_sync_desktop()
    finally:
        (saikai.DESKTOP_SESSIONS_ROOT, saikai.PROJECTS_ROOT,
         saikai._project_dirs, saikai.load_sessions_in_dir,
         saikai._session_surface_model) = saved
    made = [json.loads(p.read_text(encoding="utf-8")) for p in idx.glob("local_*.json")]
    dups = [m for m in made if m.get("cliSessionId") == "dup-sid"]
    assert len(dups) == 1, f"expected exactly one entry for the dup sid, got {len(dups)}"
    assert dups[0]["model"] == "claude-sonnet-4-6", "must mirror account model, not fabricate"


if __name__ == "__main__":
    test_config_path_honors_env()
    print("PASS test_config_path_honors_env")
    test_load_config_parses_and_degrades()
    print("PASS test_load_config_parses_and_degrades")
    test_cfg_precedence_env_over_config_over_default()
    print("PASS test_cfg_precedence_env_over_config_over_default")
    test_cfg_bool_parses_truthy_falsy()
    print("PASS test_cfg_bool_parses_truthy_falsy")
    test_summary_enabled_matrix()
    print("PASS test_summary_enabled_matrix")
    test_summarize_session_skips_llm_when_disabled()
    print("PASS test_summarize_session_skips_llm_when_disabled")
    test_validate_keymap()
    print("PASS test_validate_keymap")
    test_leader_map()
    print("PASS test_leader_map")
    test_init_config_writes_parseable_template()
    print("PASS test_init_config_writes_parseable_template")
    test_resolved_settings_covers_and_applies_runtime_knobs()
    print("PASS test_resolved_settings_covers_and_applies_runtime_knobs")
    test_color_legend_explains_context_without_false_last_color_claim()
    print("PASS test_color_legend_explains_context_without_false_last_color_claim")
    test_removed_cluster_mode_has_no_dangling_runtime_references()
    print("PASS test_removed_cluster_mode_has_no_dangling_runtime_references")
    test_reset_terminal_modes_guarded_and_emits()
    print("PASS test_reset_terminal_modes_guarded_and_emits")
    test_child_spawn_env_strips_parent_session_markers()
    print("PASS test_child_spawn_env_strips_parent_session_markers")
    test_child_spawn_env_strips_virtualenv_from_var_and_path()
    print("PASS test_child_spawn_env_strips_virtualenv_from_var_and_path")
    test_activity_marker_bg_agent_distinct_from_open()
    print("PASS test_activity_marker_bg_agent_distinct_from_open")
    test_desktop_index_dir_prefers_recent_over_most_entries()
    print("PASS test_desktop_index_dir_prefers_recent_over_most_entries")
    test_parse_session_survives_nondict_line_and_message()
    print("PASS test_parse_session_survives_nondict_line_and_message")
    test_parse_cache_misses_on_append_within_mtime_tolerance()
    print("PASS test_parse_cache_misses_on_append_within_mtime_tolerance")
    test_ctx_usage_splits_on_newline_only()
    print("PASS test_ctx_usage_splits_on_newline_only")
    test_last_assistant_falls_back_to_whole_file_for_huge_final_turn()
    print("PASS test_last_assistant_falls_back_to_whole_file_for_huge_final_turn")
    test_find_project_dir_requires_segment_boundary()
    print("PASS test_find_project_dir_requires_segment_boundary")
    test_cell_width_zero_for_combining_and_zwj()
    print("PASS test_cell_width_zero_for_combining_and_zwj")
    test_extract_topics_parses_newline_and_bullets()
    print("PASS test_extract_topics_parses_newline_and_bullets")
    test_empty_topics_are_persisted()
    print("PASS test_empty_topics_are_persisted")
    test_set_lineage_refuses_to_wipe_on_unreadable_file()
    print("PASS test_set_lineage_refuses_to_wipe_on_unreadable_file")
    test_new_session_stub_preserves_drive_letter_case()
    print("PASS test_new_session_stub_preserves_drive_letter_case")
    test_session_pid_live_rejects_reused_pid()
    print("PASS test_session_pid_live_rejects_reused_pid")
    test_resolve_resume_cwd_prefers_recent_sibling()
    print("PASS test_resolve_resume_cwd_prefers_recent_sibling")
    test_is_bg_default_denies_unknown_live_kind()
    print("PASS test_is_bg_default_denies_unknown_live_kind")
    test_bg_job_state_join_and_marker()
    print("PASS test_bg_job_state_join_and_marker")
    test_resolve_resume_cwd_prefers_worktree_origin()
    print("PASS test_resolve_resume_cwd_prefers_worktree_origin")
    test_parse_session_captures_worktree_origin_cwd()
    print("PASS test_parse_session_captures_worktree_origin_cwd")
    test_load_active_sessions_honors_config_root()
    print("PASS test_load_active_sessions_honors_config_root")
    test_desktop_entry_omits_unknown_model_and_marks_title_auto()
    print("PASS test_desktop_entry_omits_unknown_model_and_marks_title_auto")
    test_desktop_default_model_mirrors_newest_account_entry()
    print("PASS test_desktop_default_model_mirrors_newest_account_entry")
    test_sync_desktop_dedups_within_run_and_mirrors_model()
    print("PASS test_sync_desktop_dedups_within_run_and_mirrors_model")
    test_desktop_index_dir_prefers_authoritative_account_over_recency()
    print("PASS test_desktop_index_dir_prefers_authoritative_account_over_recency")
    test_dedup_sessions_by_id_keeps_newest()
    print("PASS test_dedup_sessions_by_id_keeps_newest")
    test_ctx_usage_skips_synthetic_and_zero_records()
    print("PASS test_ctx_usage_skips_synthetic_and_zero_records")
    test_ctx_usage_caches_on_mtime_size()
    print("PASS test_ctx_usage_caches_on_mtime_size")
    test_save_options_preserves_others_on_unreadable_and_nondict()
    print("PASS test_save_options_preserves_others_on_unreadable_and_nondict")
    print("ALL PASS")
