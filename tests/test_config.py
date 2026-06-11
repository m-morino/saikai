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
        assert cfg["limits"]["max_live"] == 64
        assert cfg["limits"]["scrollback_lines"] == 2000  # the memory lever ships in the template
        assert saikai._init_config(force=False) == 1      # refuse overwrite
        assert saikai._init_config(force=True) == 0       # --force overwrites
    finally:
        os.environ.pop("SAIKAI_CONFIG", None)
        saikai._reset_config_cache()


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
    test_reset_terminal_modes_guarded_and_emits()
    print("PASS test_reset_terminal_modes_guarded_and_emits")
    print("ALL PASS")
