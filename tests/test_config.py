"""Headless tests for the TOML config layer: location resolution, load (with safe
degradation), and the env > config > default precedence resolver.

Run:  python tests/test_config.py
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
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
    test_dedup_sessions_by_id_keeps_newest()
    print("PASS test_dedup_sessions_by_id_keeps_newest")
    test_ctx_usage_skips_synthetic_and_zero_records()
    print("PASS test_ctx_usage_skips_synthetic_and_zero_records")
    test_save_options_preserves_others_on_unreadable_and_nondict()
    print("PASS test_save_options_preserves_others_on_unreadable_and_nondict")
    print("ALL PASS")
