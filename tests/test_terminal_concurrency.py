"""Headless regression tests for ClaudeTerminal threading.

Runs WITHOUT textual/pyte/pywinpty: recap_terminal soft-imports them (Widget
falls back to object), so ClaudeTerminal can be built via __new__ with just the
fields under test. Run:  python tests/test_terminal_concurrency.py
"""
import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import recap_terminal as rt


def test_update_status_marshals_outside_lock():
    """_update_status must NOT hold self._lock while marshalling the status
    callback. call_from_thread blocks until the UI thread runs the callback, and
    the UI thread (render_line / _current_screen) takes self._lock — holding the
    lock across the marshal deadlocks reader vs UI (the freeze-on-busy bug)."""
    ct = rt.ClaudeTerminal.__new__(rt.ClaudeTerminal)
    ct._lock = threading.Lock()
    ct._status = "idle"
    ct._pending_status = None
    ct._pending_ticks = 0
    ct.sid = "x"
    ct._on_status = lambda _sid, _st: None

    def fake_marshal(fn):
        # Mimic Textual call_from_thread: block the caller until a UI thread runs
        # fn, and have that UI thread take the SAME lock first (like render_line).
        t = threading.Thread(target=lambda: (ct._lock.acquire(), fn(), ct._lock.release()))
        t.start()
        t.join(timeout=4)
        if t.is_alive():
            raise TimeoutError("UI thread could not acquire ct._lock -> DEADLOCK")

    ct._marshal = fake_marshal

    done = threading.Event()
    err = []

    def reader():
        try:
            ct._update_status("busy")     # idle -> busy fires the callback
        except Exception as e:            # noqa: BLE001
            err.append(repr(e))
        finally:
            done.set()

    r = threading.Thread(target=reader)
    r.start()
    r.join(timeout=6)

    assert done.is_set() and not r.is_alive(), "DEADLOCK: _update_status hung"
    assert not err, f"_update_status raised: {err}"
    assert ct._status == "busy"


def test_kill_tracks_reap_for_atexit_join():
    """kill() must register its taskkill reap in the module registry so
    join_all_reaps (wired to atexit) can wait on it on EVERY exit path — not
    just the App's two quit actions. Otherwise on_unmount-driven teardown leaks
    the reap and orphans claude's node workers (the 0fd9fcf hazard)."""
    if sys.platform != "win32":
        return  # the reap thread is win32-only
    ct = rt.ClaudeTerminal.__new__(rt.ClaudeTerminal)
    ct._stop = threading.Event()
    ct._pty = None              # skip the pty.close() branch
    ct._pid = 999999999         # nonexistent pid -> taskkill returns fast
    with rt._REAP_LOCK:
        rt._REAP_THREADS.clear()
    t = ct.kill()
    assert t is not None, "kill() should return a reap thread on win32"
    with rt._REAP_LOCK:
        assert any(x is t for x in rt._REAP_THREADS), "reap not tracked in registry"
    rt.join_all_reaps(timeout=5)
    assert not t.is_alive(), "reap not joined by join_all_reaps"


if __name__ == "__main__":
    test_update_status_marshals_outside_lock()
    print("PASS test_update_status_marshals_outside_lock")
    test_kill_tracks_reap_for_atexit_join()
    print("PASS test_kill_tracks_reap_for_atexit_join")
