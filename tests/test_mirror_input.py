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


def _post(port, path, body=None, headers=None, raw=None):
    """POST helper returning (status, body_text). Uses a same-origin Host+Origin
    so this test isolates the write-key/body checks (Host/Origin get their own
    tests in Tasks 4)."""
    url = f"http://127.0.0.1:{port}{path}"
    data = raw if raw is not None else (
        json.dumps(body).encode("utf-8") if body is not None else b"")
    h = {"Host": f"127.0.0.1:{port}",
         "Origin": f"http://127.0.0.1:{port}",
         "Content-Type": "application/json"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, data=data, headers=h, method="POST")
    try:
        r = urllib.request.urlopen(req, timeout=3.0)
        return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def test_post_input_write_key_and_body_matrix():
    hub = m.MirrorHub(token="secret", host="127.0.0.1", port=0)
    delivered = []
    hub.set_input_handler(lambda d: delivered.append(d))
    hub._control_enabled = True
    port = hub.serve()
    key = hub._write_key
    try:
        WK = {"X-Mirror-Write-Key": key}
        # Good key + good body -> 204 accept, handler gets exact bytes.
        st, _ = _post(port, "/input", {"data": "ls\r"}, headers=WK)
        assert st == 204, st
        # Bad key -> 403, not delivered.
        st, _ = _post(port, "/input", {"data": "rm"},
                      headers={"X-Mirror-Write-Key": "wrong"})
        assert st == 403, st
        # Absent key -> 403.
        st, _ = _post(port, "/input", {"data": "rm"})
        assert st == 403, st
        # Missing 'data' -> 400.
        st, _ = _post(port, "/input", {"nope": 1}, headers=WK)
        assert st == 400, st
        # Non-str 'data' -> 400.
        st, _ = _post(port, "/input", {"data": 123}, headers=WK)
        assert st == 400, st
        # Empty 'data' -> 204 no-op (accepted, nothing delivered).
        st, _ = _post(port, "/input", {"data": ""}, headers=WK)
        assert st == 204, st
        # Non-JSON body -> 400.
        st, _ = _post(port, "/input", raw=b"not json", headers=WK)
        assert st == 400, st
        # Oversized Content-Length -> 413 (declared > 64 KB cap).
        big = {"X-Mirror-Write-Key": key, "Content-Length": str(70000)}
        st, _ = _post(port, "/input", raw=b"x" * 10, headers=big)
        assert st == 413, st
        # Chunked transfer -> 411 (we require a Content-Length).
        ch = {"X-Mirror-Write-Key": key, "Transfer-Encoding": "chunked"}
        st, _ = _post(port, "/input", raw=b"5\r\nhello\r\n0\r\n\r\n", headers=ch)
        assert st == 411, st
        # GET on /input is 405 (only POST).
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/input", timeout=3.0)
            assert False, "GET /input should 405"
        except urllib.error.HTTPError as e:
            assert e.code in (403, 405), e.code   # token-gated GET path -> 403/405
        # Only the one good "ls\r" reached the handler.
        assert delivered == ["ls\r"], delivered
    finally:
        hub.stop()


if __name__ == "__main__":
    test_inject_gate_off_by_default_and_requires_handler()
    test_inject_is_fifo_via_single_drain()
    test_post_input_write_key_and_body_matrix()
    print("OK test_mirror_input")
