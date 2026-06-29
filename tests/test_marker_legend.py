"""Unit tests for _marker_legend — the contextual marker explanation shown at
the top of a session's preview (#marker-legend).

Pure function over a session dict + favorite/hidden sets. Run with:
    uv run python tests/test_marker_legend.py
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import saikai


def test_active():
    assert saikai._marker_legend({"id": "a", "is_active": True}, set(), set()) == \
        ["+ recently active"]


def test_recent_dormant():
    assert saikai._marker_legend({"id": "a", "is_recent": True}, set(), set()) == \
        [". recent (dormant)"]


def test_favorite_appended():
    # activity (+) AND state (*) both present → one entry each, in order.
    out = saikai._marker_legend({"id": "a", "is_active": True}, {"a"}, set())
    assert out == ["+ recently active", "* favorite"]


def test_hidden_appended():
    out = saikai._marker_legend({"id": "a", "is_recent": True}, set(), {"a"})
    assert out == [". recent (dormant)", "x hidden"]


def test_bg_wins_over_everything():
    out = saikai._marker_legend(
        {"id": "a", "is_bg": True, "is_active": True, "is_recent": True}, set(), set())
    assert out == ["& background agent/job"]


def test_open_shell_and_busy_and_idle():
    assert saikai._marker_legend(
        {"id": "a", "is_open": True, "session_status": "shell"}, set(), set()) == \
        ["$ open, running a shell command elsewhere"]
    assert saikai._marker_legend(
        {"id": "a", "is_open": True, "session_status": "busy"}, set(), set()) == \
        ["@ open, responding in another window"]
    assert saikai._marker_legend(
        {"id": "a", "is_open": True}, set(), set()) == ["@ open in another window"]


def test_no_markers_is_empty():
    assert saikai._marker_legend({"id": "a"}, set(), set()) == []


def test_favorite_and_hidden_mutually_exclusive_state():
    # favorite takes precedence over hidden (matches _state_marker).
    out = saikai._marker_legend({"id": "a"}, {"a"}, {"a"})
    assert out == ["* favorite"]


if __name__ == "__main__":
    test_active()
    test_recent_dormant()
    test_favorite_appended()
    test_hidden_appended()
    test_bg_wins_over_everything()
    test_open_shell_and_busy_and_idle()
    test_no_markers_is_empty()
    test_favorite_and_hidden_mutually_exclusive_state()
    print("OK test_marker_legend")
