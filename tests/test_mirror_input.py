# tests/test_mirror_input.py
import os, sys, threading, time, json
import urllib.request, urllib.error
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import saikai_mirror as m


def test_inject_gate_off_by_default_and_requires_handler():
    """inject() returns False when no handler is wired (nothing to deliver to)
    and when control is OFF; only an enabled hub WITH a handler accepts input."""
    hub = m.MirrorHub(token="t")
    got = []
    # No handler yet -> refuse, even if somehow enabled.
    hub._control_enabled = True
    assert hub.inject("x") is False, "no handler must refuse"
    hub.set_input_handler(lambda d: got.append(d))
    # Handler present but control OFF (default) -> refuse.
    hub._control_enabled = False
    assert hub.inject("a") is False, "control OFF must refuse"
    assert got == []
    # Control ON + handler -> accept and deliver.
    hub._control_enabled = True
    assert hub.inject("b") is True
    assert got == ["b"], got


if __name__ == "__main__":
    test_inject_gate_off_by_default_and_requires_handler()
    print("OK test_mirror_input")
