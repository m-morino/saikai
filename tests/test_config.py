"""Headless tests for the TOML config layer: location resolution, load (with safe
degradation), and the env > config > default precedence resolver.

Run:  python tests/test_config.py
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import recap


def test_config_path_honors_env():
    p = Path(tempfile.gettempdir()) / "recap-cfg-test.toml"
    os.environ["RECAP_CONFIG"] = str(p)
    try:
        assert recap._config_path() == p
    finally:
        os.environ.pop("RECAP_CONFIG", None)


def test_load_config_parses_and_degrades():
    d = Path(tempfile.mkdtemp())
    good = d / "config.toml"
    good.write_text("[summary]\nenabled = true\n[limits]\nmax_live = 9\n", encoding="utf-8")
    os.environ["RECAP_CONFIG"] = str(good)
    try:
        recap._reset_config_cache()
        c = recap._load_config()
        assert c["summary"]["enabled"] is True and c["limits"]["max_live"] == 9
    finally:
        os.environ.pop("RECAP_CONFIG", None)
        recap._reset_config_cache()
    # corrupt → {} (no raise); missing → {} too
    bad = d / "bad.toml"
    bad.write_text("this is not toml = = =", encoding="utf-8")
    os.environ["RECAP_CONFIG"] = str(bad)
    try:
        recap._reset_config_cache()
        assert recap._load_config() == {}
        os.environ["RECAP_CONFIG"] = str(d / "nope.toml")
        recap._reset_config_cache()
        assert recap._load_config() == {}
    finally:
        os.environ.pop("RECAP_CONFIG", None)
        recap._reset_config_cache()


def test_cfg_precedence_env_over_config_over_default():
    d = Path(tempfile.mkdtemp())
    f = d / "config.toml"
    f.write_text("[limits]\nmax_live = 30\nclaude_mb = 700\n", encoding="utf-8")
    os.environ["RECAP_CONFIG"] = str(f)
    recap._reset_config_cache()
    try:
        os.environ["RECAP_MAX_LIVE"] = "12"                                  # env wins
        assert recap._cfg("limits", "max_live", "RECAP_MAX_LIVE", 64, int) == 12
        os.environ.pop("RECAP_MAX_LIVE", None)                               # → config
        assert recap._cfg("limits", "max_live", "RECAP_MAX_LIVE", 64, int) == 30
        assert recap._cfg("limits", "claude_mb", "RECAP_CLAUDE_MB", 600.0, float) == 700.0
        assert recap._cfg("limits", "missing", "RECAP_NOPE", 5, int) == 5    # default
        os.environ["RECAP_MAX_LIVE"] = "bad"                                 # bad cast → default
        assert recap._cfg("limits", "max_live", "RECAP_MAX_LIVE", 64, int) == 64
    finally:
        for k in ("RECAP_CONFIG", "RECAP_MAX_LIVE", "RECAP_CLAUDE_MB"):
            os.environ.pop(k, None)
        recap._reset_config_cache()


def test_cfg_bool_parses_truthy_falsy():
    assert recap._cfg_bool(True) is True
    assert recap._cfg_bool("true") is True and recap._cfg_bool("on") is True
    assert recap._cfg_bool("0") is False and recap._cfg_bool("false") is False
    assert recap._cfg_bool(None, default=True) is True
    assert recap._cfg_bool(None) is False


if __name__ == "__main__":
    test_config_path_honors_env()
    print("PASS test_config_path_honors_env")
    test_load_config_parses_and_degrades()
    print("PASS test_load_config_parses_and_degrades")
    test_cfg_precedence_env_over_config_over_default()
    print("PASS test_cfg_precedence_env_over_config_over_default")
    test_cfg_bool_parses_truthy_falsy()
    print("PASS test_cfg_bool_parses_truthy_falsy")
    print("ALL PASS")
