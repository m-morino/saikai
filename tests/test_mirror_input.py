# tests/test_mirror_input.py
import os, sys, threading, time, json
import urllib.request, urllib.error
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import saikai_mirror as m


def test_inject_gate_off_by_default_and_requires_handler():
    """inject() returns False when no handler is wired (nothing to deliver to)
    and when control is OFF; only an enabled hub WITH a handler accepts input.

    Delivery is now asynchronous (enqueue onto the FIFO drain, see
    test_inject_is_fifo_via_single_drain), so this gate test asserts the
    accept/refuse decision via the return value and the queued item rather than
    a synchronous handler side-effect."""
    hub = m.MirrorHub(token="t")
    # No handler yet -> refuse, even if somehow enabled.
    hub._control_enabled = True
    assert hub.inject("x") is False, "no handler must refuse"
    assert hub._inject_q.empty(), "refused input must not be queued"
    hub.set_input_handler(lambda d: None)
    # Handler present but control OFF (default) -> refuse.
    hub._control_enabled = False
    assert hub.inject("a") is False, "control OFF must refuse"
    assert hub._inject_q.empty(), "refused input must not be queued"
    # Control ON + handler -> accept (enqueued for the single drain worker).
    hub._control_enabled = True
    assert hub.inject("b") is True
    assert hub._inject_q.get_nowait() == "b"


def test_inject_is_fifo_via_single_drain():
    """Three rapid injects are delivered to the handler in submission order by a
    single drain worker (independent POST threads otherwise have no ordering)."""
    hub = m.MirrorHub(token="t")
    seen = []
    ev = threading.Event()

    def handler(d):
        seen.append(d)
        if len(seen) == 3:
            ev.set()

    hub.set_input_handler(handler)
    hub._control_enabled = True
    hub.serve()                       # starts the input-drain worker
    try:
        assert hub.inject("a") is True
        assert hub.inject("b") is True
        assert hub.inject("c") is True
        assert ev.wait(timeout=3.0), f"drain did not deliver 3 items: {seen}"
        assert seen == ["a", "b", "c"], seen
        assert hasattr(hub, "_inject_q"), "inject must route through a FIFO queue"
    finally:
        hub.stop()


if __name__ == "__main__":
    test_inject_gate_off_by_default_and_requires_handler()
    test_inject_is_fifo_via_single_drain()
    print("OK test_mirror_input")
