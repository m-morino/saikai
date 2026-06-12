"""Real platform PTY smoke: spawn, resize, read output, and observe EOF.

Run:  python tests/test_pty_backend.py
"""
import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import saikai_terminal as rt


def test_real_pty_spawn_resize_and_eof():
    assert rt.PtyProcess is not None, rt.unavailable_reason()
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


if __name__ == "__main__":
    test_real_pty_spawn_resize_and_eof()
    print("PASS test_real_pty_spawn_resize_and_eof")
    print("ALL PASS")
