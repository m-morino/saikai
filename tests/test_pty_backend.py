"""Real platform PTY smoke: spawn, resize, read output, and observe EOF.

Run:  python tests/test_pty_backend.py
"""
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import saikai_terminal as rt


def test_real_pty_spawn_resize_and_eof():
    # The other suites run with plain python and NO deps (textual/pyte/PTY
    # backends are soft-imported) — this one needs the real backend. Keep the
    # no-deps contract locally by SKIPPING when it is absent; CI installs the
    # built package, so a missing backend there is a real failure, not a skip.
    if rt.PtyProcess is None:
        if os.environ.get("CI"):
            raise AssertionError(
                f"PTY backend missing on CI: {rt.unavailable_reason()}")
        print(f"SKIP test_real_pty_spawn_resize_and_eof ({rt.unavailable_reason()})")
        return
    pty = rt.PtyProcess.spawn(
        [sys.executable, "-c",
         "import time; print('SAIKAI_PTY_SMOKE', flush=True); time.sleep(0.2)"],
        dimensions=(10, 40),
        env=os.environ.copy(),
    )
    chunks = []

    def read_all():
        while True:
            try:
                chunk = pty.read()
            except EOFError:
                return
            except Exception:
                return
            if not chunk:
                return
            chunks.append(chunk)

    reader = threading.Thread(target=read_all, daemon=True)
    reader.start()
    pty.setwinsize(12, 50)
    reader.join(timeout=10)
    if reader.is_alive():
        if rt._IS_WIN:
            try:
                pty.terminate(force=True)
            except Exception:
                pass
        else:
            # Match the production invariant: never call ptyprocess close() or
            # terminate() while another thread may be blocked in read().
            rt._post_signal(getattr(pty, "pid", None), "SIGKILL")
        reader.join(timeout=2)
        raise AssertionError("real PTY reader did not observe child EOF within 10s")
    try:
        assert "SAIKAI_PTY_SMOKE" in "".join(chunks), chunks
    finally:
        try:
            pty.close(force=True)
        except Exception:
            pass


def test_real_pty_writer_orders_input_and_retires():
    """The production pane writer preserves key/paste ordering on a real PTY."""
    if rt.PtyProcess is None:
        if os.environ.get("CI"):
            raise AssertionError(
                f"PTY backend missing on CI: {rt.unavailable_reason()}")
        print(
            "SKIP test_real_pty_writer_orders_input_and_retires "
            f"({rt.unavailable_reason()})")
        return

    pty = rt.PtyProcess.spawn(
        [
            sys.executable,
            "-c",
            "import sys; "
            "a=sys.stdin.readline().strip(); "
            "b=sys.stdin.readline().strip(); "
            "print('SAIKAI_PTY_ORDER='+a+'|'+b, flush=True)",
        ],
        dimensions=(10, 80),
        env=os.environ.copy(),
    )
    pane = rt.AgentTerminal(
        ["agent"], status_classifier=lambda _text, _title: "idle")
    pane._pty = pty
    pane.is_dead = False
    pane.sid = "real-writer"
    pane._start_writer()
    chunks = []

    def read_all():
        while True:
            try:
                chunk = pty.read()
            except (EOFError, OSError):
                return
            except Exception:
                return
            if not chunk:
                return
            chunks.append(chunk)

    reader = threading.Thread(target=read_all, daemon=True)
    reader.start()
    assert pane.write("ONE\r") is True
    assert pane.write("TWO\r") is True

    deadline = time.monotonic() + 5.0
    while pane._write_pending_bytes and time.monotonic() < deadline:
        time.sleep(0.005)
    assert pane._write_pending_bytes == 0
    writer = pane._stop_writer()
    if writer is not None:
        writer.join(timeout=3.0)
        assert not writer.is_alive(), "pane writer failed to retire"

    reader.join(timeout=10.0)
    if reader.is_alive():
        if rt._IS_WIN:
            try:
                pty.terminate(force=True)
            except Exception:
                pass
        else:
            rt._post_signal(getattr(pty, "pid", None), "SIGKILL")
        reader.join(timeout=2.0)
        raise AssertionError("real PTY did not consume ordered writer input")
    try:
        output = "".join(chunks)
        assert "SAIKAI_PTY_ORDER=ONE|TWO" in output, repr(output)
    finally:
        try:
            pty.close(force=True)
        except Exception:
            pass


if __name__ == "__main__":
    test_real_pty_spawn_resize_and_eof()
    print("PASS test_real_pty_spawn_resize_and_eof")
    test_real_pty_writer_orders_input_and_retires()
    print("PASS test_real_pty_writer_orders_input_and_retires")
    print("ALL PASS")
