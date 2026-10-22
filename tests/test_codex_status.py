"""Live status for a Codex pane, pinned to screens captured from the real
codex-cli 0.163.0 TUI (tests/fixtures/codex_screens_0163.json).

The generic classifier treated ANY two numbered lines as a forced choice, so a
finished Codex answer containing a numbered list (Codex writes plenty) sat as
"needs you" forever, and it accepted "Would you like" — which Codex also uses
to close an ordinary answer. Codex marks a real choice with its selection cursor
on a numbered option ("› 1. Yes, proceed (y)"); that is the signal.

Run:  python tests/test_codex_status.py
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import saikai_terminal as rt
from saikai_provider import get_provider

with open(os.path.join(os.path.dirname(__file__), "fixtures",
                       "codex_screens_0163.json"), encoding="utf-8") as _f:
    SCREENS = json.load(_f)

classify = rt.classifier_for_profile(get_provider("codex").status_profile)


def test_codex_provider_uses_its_own_profile():
    assert get_provider("codex").status_profile == "codex"
    assert classify is rt.classify_codex_status


def test_real_approval_prompt_is_waiting():
    assert classify(SCREENS["approval"]) == "waiting"


def test_real_trust_gate_is_waiting():
    assert classify(SCREENS["trust"]) == "waiting"


def test_real_working_screen_is_busy():
    assert classify(SCREENS["busy"]) == "busy"


def test_real_finished_screen_is_idle():
    assert classify(SCREENS["done"]) == "idle"


def test_finished_answer_with_a_numbered_list_is_not_waiting():
    """The false positive the generic classifier had."""
    screen = SCREENS["done"].replace(
        "  Worked for", "  1. Check the sensor history\n  2. Re-run the automation\n"
        "  3. Restart Home Assistant\n  Would you like me to apply the fix?\n  Worked for")
    assert rt.classify_generic_status(screen) == "waiting"   # the old behaviour
    assert classify(screen) == "idle"


def test_the_input_prompt_cursor_is_not_a_choice():
    """'› Ask Codex to do anything' uses the same glyph with no number."""
    assert classify("› Ask Codex to do anything\n  ? for shortcuts") == "idle"


def test_a_numbered_prompt_echoed_in_history_is_not_a_choice():
    """A submitted prompt starting "1. …" is echoed as "› 1. …" — without the
    dialog's enter/esc footer it is history, not a pending choice."""
    screen = SCREENS["done"].replace(
        "› Run the shell command", "› 1. Run the shell command")
    assert "› 1. Run" in screen
    assert classify(screen) == "idle"
    busy = SCREENS["busy"].replace("› Run the shell command", "› 1. Run the shell command")
    assert classify(busy) == "busy"


if __name__ == "__main__":
    for _n, _fn in sorted(globals().items()):
        if _n.startswith("test_") and callable(_fn):
            _fn()
            print("PASS", _n)
    print("OK test_codex_status")
