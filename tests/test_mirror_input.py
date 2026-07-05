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


def test_typed_inject_dispatches_by_tag_in_order():
    """One FIFO drain dispatches keyboard/mouse/key items to the matching
    handler in global submission order. Bare-str keyboard items (Phase B) and
    tagged tuples interleave; ordering across handlers is preserved."""
    hub = m.MirrorHub(token="t")
    seen = []
    ev = threading.Event()

    def on_input(d): seen.append(("input", d))
    def on_mouse(col, row, button, kind):
        seen.append(("mouse", col, row, button, kind))
    def on_key(key):
        seen.append(("key", key))
        if len(seen) == 4:
            ev.set()

    hub.set_input_handler(on_input)
    hub.set_mouse_handler(on_mouse)
    hub.set_key_handler(on_key)
    hub._control_enabled = True
    hub.serve()
    try:
        assert hub.inject("a") is True                       # keyboard (bare str)
        assert hub.inject_mouse(5, 9, 0, "down") is True     # mouse
        assert hub.inject_mouse(5, 9, 0, "up") is True       # mouse
        assert hub.inject_key("escape") is True              # key
        assert ev.wait(timeout=3.0), f"drain did not deliver 4 items: {seen}"
        assert seen == [
            ("input", "a"),
            ("mouse", 5, 9, 0, "down"),
            ("mouse", 5, 9, 0, "up"),
            ("key", "escape"),
        ], seen
    finally:
        hub.stop()


def test_mouse_and_key_inject_gate_on_control_and_handler():
    """inject_mouse/inject_key refuse when control is OFF or no matching handler
    is wired, and accept (enqueue) when control is ON with the handler set."""
    hub = m.MirrorHub(token="t")
    # No handlers yet -> refuse even if enabled.
    hub._control_enabled = True
    assert hub.inject_mouse(1, 1, 0, "down") is False, "no mouse handler must refuse"
    assert hub.inject_key("tab") is False, "no key handler must refuse"
    assert hub._inject_q.empty(), "refused input must not be queued"
    hub.set_mouse_handler(lambda *a: None)
    hub.set_key_handler(lambda *a: None)
    # Control OFF -> refuse.
    hub._control_enabled = False
    assert hub.inject_mouse(1, 1, 0, "down") is False, "control OFF must refuse mouse"
    assert hub.inject_key("tab") is False, "control OFF must refuse key"
    assert hub._inject_q.empty()
    # Control ON + handlers -> accept (tagged tuple enqueued).
    hub._control_enabled = True
    assert hub.inject_mouse(2, 3, 0, "up") is True
    assert hub._inject_q.get_nowait() == ("mouse", 2, 3, 0, "up")
    assert hub.inject_key("f12") is True
    assert hub._inject_q.get_nowait() == ("key", "f12")


def test_update_control_target_syncs_without_rearming_idle():
    """update_control_target refreshes ONLY the banner 'typing into' target (focus
    moved while control stays ON): no-op when control is OFF or the target is
    unchanged, and -- unlike set_control_state -- it does NOT re-arm the idle
    auto-disable (so focus churn can't keep control alive forever)."""
    hub = m.MirrorHub(token="secret", host="127.0.0.1", port=0, cols=10, rows=2,
                      idle_secs=999)
    hub.set_input_handler(lambda d: None)
    # Control OFF -> no-op (never stores a target).
    hub.update_control_target("X")
    assert hub._control_target is None, "OFF must not store a target"
    # Enable (this arms the idle timer) then move the target.
    hub.set_control_state(True, "pane-A")
    assert hub._control_target == "pane-A"
    t_before = hub._idle_timer
    hub.update_control_target("pane-B")
    assert hub._control_target == "pane-B"
    assert hub._idle_timer is t_before, "update_control_target must NOT re-arm idle"
    # Unchanged target -> still no re-arm.
    hub.update_control_target("pane-B")
    assert hub._idle_timer is t_before
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
    except (urllib.error.URLError, ConnectionError, OSError) as e:
        # Rejecting a POST before its body is fully drained can reset the
        # connection mid-send on some platforms (seen on macOS), so the client
        # sees a broken pipe instead of the status line. That is still a
        # transport-level rejection; surface it as status 0 so callers can
        # accept it where a clean status is not guaranteed.
        return 0, str(getattr(e, "reason", e))


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
        # Chunked transfer -> rejected (we require a Content-Length). Ideally a
        # clean 411 (Length Required); but a chunked body has no length to
        # drain, so rejecting it can reset the connection on some platforms,
        # surfacing as a transport error (status 0). Both are valid rejections —
        # what matters is the body is never delivered (asserted below).
        ch = {"X-Mirror-Write-Key": key, "Transfer-Encoding": "chunked"}
        st, _ = _post(port, "/input", raw=b"5\r\nhello\r\n0\r\n\r\n", headers=ch)
        assert st in (411, 0), st
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


def _raw_request(port, method, path, headers):
    """Send a raw HTTP/1.1 request with EXACT headers (so we can forge Host or
    omit Origin); return the numeric status from the response line."""
    import socket
    body = b""
    if headers.get("_body") is not None:
        body = headers.pop("_body")
        headers["Content-Length"] = str(len(body))
    lines = [f"{method} {path} HTTP/1.1"]
    lines += [f"{k}: {v}" for k, v in headers.items() if not k.startswith("_")]
    lines += ["Connection: close", "", ""]
    raw = ("\r\n".join(lines)).encode("ascii") + body
    s = socket.create_connection(("127.0.0.1", port), timeout=3.0)
    try:
        s.sendall(raw)
        resp = b""
        while b"\r\n" not in resp:
            chunk = s.recv(256)
            if not chunk:
                break
            resp += chunk
        first = resp.split(b"\r\n", 1)[0].decode("ascii", "replace")
        return int(first.split(" ")[1])     # "HTTP/1.1 <code> <reason>"
    finally:
        s.close()


def test_post_refused_input_returns_429():
    """A refused injection (rate cap / bounded queue full / no handler) must NOT
    read as success: the handler used to discard hub.inject*()'s False and 204
    anyway, so throttled keystrokes vanished with the browser believing they
    landed. Refusal maps to 429. (#audit-codex-inject-429)"""
    hub = m.MirrorHub(token="secret", host="127.0.0.1", port=0)
    delivered = []
    hub.set_input_handler(lambda d: delivered.append(d))
    hub._control_enabled = True
    port = hub.serve()
    key = hub._write_key
    try:
        WK = {"X-Mirror-Write-Key": key}
        # accepted baseline
        st, _ = _post(port, "/input", {"data": "a"}, headers=WK)
        assert st == 204, st
        # force the accepted-input rate cap: everything now inside the gap
        hub._min_accept_gap = 100.0
        st, _ = _post(port, "/input", {"data": "b"}, headers=WK)
        assert st == 429, f"refused input must be 429, got {st}"
        st, _ = _post(port, "/key", {"key": "enter"}, headers=WK)
        assert st == 429, st
        st, _ = _post(port, "/mouse",
                      {"col": 1, "row": 1, "button": 0, "kind": "down"},
                      headers=WK)
        assert st == 429, st
    finally:
        hub.stop()


def test_host_allow_list_and_origin_matrix():
    hub = m.MirrorHub(token="secret", host="127.0.0.1", port=0)
    hub.set_input_handler(lambda d: None)
    hub._control_enabled = True
    port = hub.serve()
    key = hub._write_key
    good_host = f"127.0.0.1:{port}"
    body = json.dumps({"data": "x"}).encode("utf-8")
    try:
        base = {"X-Mirror-Write-Key": key, "Content-Type": "application/json"}

        def H(**extra):
            h = dict(base); h["_body"] = body; h.update(extra); return h

        # Foreign Host on the PAGE route -> 403 (anti DNS-rebinding, all routes).
        assert _raw_request(port, "GET", "/?token=secret",
                            {"Host": "evil.example.com"}) == 403
        # Foreign Host on POST -> 403 even with a valid key + Origin.
        assert _raw_request(port, "POST", "/input",
                            H(Host="evil.example.com",
                              Origin="http://evil.example.com")) == 403
        # Matching Host + matching Origin -> 204.
        assert _raw_request(port, "POST", "/input",
                            H(Host=good_host,
                              Origin=f"http://{good_host}")) == 204
        # Cross-origin (Origin != server origin) -> 403.
        assert _raw_request(port, "POST", "/input",
                            H(Host=good_host,
                              Origin="http://attacker.test")) == 403
        # Absent Origin AND absent Referer -> 403 (fail-closed).
        assert _raw_request(port, "POST", "/input",
                            H(Host=good_host)) == 403
        # Absent Origin but matching Referer host -> 204 (Referer fallback).
        assert _raw_request(port, "POST", "/input",
                            H(Host=good_host,
                              Referer=f"http://{good_host}/")) == 204
        # Origin host matches but PORT differs -> 403 (exact origin equality).
        assert _raw_request(port, "POST", "/input",
                            H(Host=good_host,
                              Origin="http://127.0.0.1:1")) == 403
        # Literal "null" Origin -> 403.
        assert _raw_request(port, "POST", "/input",
                            H(Host=good_host, Origin="null")) == 403
    finally:
        hub.stop()


def test_post_mouse_gate_and_body_matrix():
    hub = m.MirrorHub(token="secret", host="127.0.0.1", port=0)
    got = []
    hub.set_mouse_handler(lambda col, row, button, kind: got.append((col, row, button, kind)))
    hub._control_enabled = True
    port = hub.serve()
    key = hub._write_key
    try:
        WK = {"X-Mirror-Write-Key": key}
        # Good key + good body -> 204; handler gets the exact 0-based coords.
        st, _ = _post(port, "/mouse", {"col": 5, "row": 9, "button": 0, "kind": "down"}, headers=WK)
        assert st == 204, st
        st, _ = _post(port, "/mouse", {"col": 5, "row": 9, "button": 0, "kind": "up"}, headers=WK)
        assert st == 204, st
        st, _ = _post(port, "/mouse", {"col": 2, "row": 3, "button": 0, "kind": "scrollup"}, headers=WK)
        assert st == 204, st
        # Bad key -> 403, not delivered.
        st, _ = _post(port, "/mouse", {"col": 1, "row": 1, "button": 0, "kind": "down"},
                      headers={"X-Mirror-Write-Key": "wrong"})
        assert st == 403, st
        # Missing field -> 400.
        st, _ = _post(port, "/mouse", {"col": 1, "row": 1, "button": 0}, headers=WK)
        assert st == 400, st
        # Non-int coord -> 400.
        st, _ = _post(port, "/mouse", {"col": "x", "row": 1, "button": 0, "kind": "down"}, headers=WK)
        assert st == 400, st
        # Unknown kind -> 400.
        st, _ = _post(port, "/mouse", {"col": 1, "row": 1, "button": 0, "kind": "wiggle"}, headers=WK)
        assert st == 400, st
        # Non-JSON -> 400.
        st, _ = _post(port, "/mouse", raw=b"not json", headers=WK)
        assert st == 400, st
        # Control OFF -> 409.
        hub._control_enabled = False
        st, _ = _post(port, "/mouse", {"col": 1, "row": 1, "button": 0, "kind": "down"}, headers=WK)
        assert st == 409, st
        hub._control_enabled = True
        # Only the three good taps reached the handler, in order.
        assert got == [(5, 9, 0, "down"), (5, 9, 0, "up"), (2, 3, 0, "scrollup")], got
        # Phase B regression: /input still 204 with a wired input handler.
        hub.set_input_handler(lambda d: None)
        st, _ = _post(port, "/input", {"data": "x"}, headers=WK)
        assert st == 204, st
    finally:
        hub.stop()


def test_post_mouse_host_and_origin_matrix():
    hub = m.MirrorHub(token="secret", host="127.0.0.1", port=0)
    hub.set_mouse_handler(lambda *a: None)
    hub._control_enabled = True
    port = hub.serve()
    key = hub._write_key
    good_host = f"127.0.0.1:{port}"
    body = json.dumps({"col": 1, "row": 1, "button": 0, "kind": "down"}).encode("utf-8")
    try:
        base = {"X-Mirror-Write-Key": key, "Content-Type": "application/json"}

        def H(**extra):
            h = dict(base); h["_body"] = body; h.update(extra); return h

        # Foreign Host -> 403.
        assert _raw_request(port, "POST", "/mouse",
                            H(Host="evil.example.com", Origin="http://evil.example.com")) == 403
        # Matching Host + Origin -> 204.
        assert _raw_request(port, "POST", "/mouse",
                            H(Host=good_host, Origin=f"http://{good_host}")) == 204
        # Cross-origin -> 403.
        assert _raw_request(port, "POST", "/mouse",
                            H(Host=good_host, Origin="http://attacker.test")) == 403
        # Absent Origin AND Referer -> 403 (fail-closed).
        assert _raw_request(port, "POST", "/mouse", H(Host=good_host)) == 403
    finally:
        hub.stop()


def test_post_key_gate_and_body_matrix():
    hub = m.MirrorHub(token="secret", host="127.0.0.1", port=0)
    got = []
    hub.set_key_handler(lambda key: got.append(key))
    hub._control_enabled = True
    port = hub.serve()
    key = hub._write_key
    try:
        WK = {"X-Mirror-Write-Key": key}
        st, _ = _post(port, "/key", {"key": "escape"}, headers=WK)
        assert st == 204, st
        st, _ = _post(port, "/key", {"key": "ctrl+c"}, headers=WK)
        assert st == 204, st
        # Bad key header -> 403.
        st, _ = _post(port, "/key", {"key": "tab"}, headers={"X-Mirror-Write-Key": "wrong"})
        assert st == 403, st
        # Missing 'key' -> 400.
        st, _ = _post(port, "/key", {"nope": 1}, headers=WK)
        assert st == 400, st
        # Non-str 'key' -> 400.
        st, _ = _post(port, "/key", {"key": 123}, headers=WK)
        assert st == 400, st
        # Empty 'key' -> 400 (never post a garbage Key).
        st, _ = _post(port, "/key", {"key": ""}, headers=WK)
        assert st == 400, st
        # Over-long 'key' -> 400 (defensive cap).
        st, _ = _post(port, "/key", {"key": "x" * 65}, headers=WK)
        assert st == 400, st
        # Control OFF -> 409.
        hub._control_enabled = False
        st, _ = _post(port, "/key", {"key": "up"}, headers=WK)
        assert st == 409, st
        hub._control_enabled = True
        # Foreign Host -> 403.
        assert _raw_request(port, "POST", "/key",
                            {"X-Mirror-Write-Key": key, "Content-Type": "application/json",
                             "Host": "evil.example.com", "Origin": "http://evil.example.com",
                             "_body": json.dumps({"key": "up"}).encode("utf-8")}) == 403
        # Only the two good keys reached the handler, in order.
        assert got == ["escape", "ctrl+c"], got
        # Unknown path is still 405.
        assert _raw_request(port, "POST", "/nope",
                            {"X-Mirror-Write-Key": key, "Content-Type": "application/json",
                             "Host": f"127.0.0.1:{port}", "Origin": f"http://127.0.0.1:{port}",
                             "_body": b"{}"}) == 405
    finally:
        hub.stop()


def _read_sse(resp, deadline_s=3.0, until=b"event: control"):
    """Read SSE bytes until `until` has appeared (after the snapshot), or time out."""
    import time as _t
    end = _t.time() + deadline_s
    seen = b""
    while _t.time() < end and until not in seen:
        seen += resp.read1(128)
    return seen.decode("utf-8", "replace")


def test_sse_emits_writekey_and_control_without_colliding_output():
    import base64
    hub = m.MirrorHub(token="secret", host="127.0.0.1", port=0, cols=10, rows=2)
    hub.set_input_handler(lambda d: None)
    port = hub.serve()
    try:
        resp = urllib.request.urlopen(
            f"http://127.0.0.1:{port}/stream?token=secret", timeout=3.0)
        # Read through the control frame's DATA line, not just its event header:
        # the `"on"/"target"` assertions below live on the data line, which can
        # arrive in a later TCP segment than `event: control` itself.
        text = _read_sse(resp, until=b'"target"')
        # On connect: a writekey event (raw JSON, NOT base64) and a control event
        # reflecting the default OFF state with a null target.
        assert "event: writekey" in text, text
        assert hub._write_key in text, "write-key must arrive over SSE"
        assert "event: control" in text, text
        assert '"on": false' in text or '"on":false' in text, text
        assert '"target": null' in text or '"target":null' in text, text
        # A normal output frame still arrives as a base64 default-event.
        hub.broadcast("\x1b[32mGO\x1b[0m")
        out = _read_sse(resp, until=b"data: ")
        payloads = [ln[6:] for ln in out.splitlines() if ln.startswith("data: ")]
        joined = "".join(base64.b64decode(p).decode("utf-8", "replace")
                         for p in payloads)
        assert "GO" in joined, joined        # output path intact, not collided
    finally:
        hub.stop()


def test_set_control_state_pushes_control_frame():
    hub = m.MirrorHub(token="secret", host="127.0.0.1", port=0, cols=10, rows=2)
    hub.set_input_handler(lambda d: None)
    port = hub.serve()
    try:
        resp = urllib.request.urlopen(
            f"http://127.0.0.1:{port}/stream?token=secret", timeout=3.0)
        _read_sse(resp, until=b"event: control")        # drain the on-connect frames
        hub.set_control_state(True, "Session S")
        text = _read_sse(resp, until=b'"on": true')
        assert "event: control" in text, text
        assert '"on": true' in text or '"on":true' in text, text
        assert "Session S" in text, text
    finally:
        hub.stop()


def test_idle_auto_disable_flips_control_off():
    """With a short idle window and no accepted input, control auto-disables and
    an OFF control frame is broadcast."""
    hub = m.MirrorHub(token="secret", host="127.0.0.1", port=0, idle_secs=0.3)
    hub.set_input_handler(lambda d: None)
    port = hub.serve()
    try:
        hub.set_control_state(True, "S")        # arms the idle timer
        assert hub._control_enabled is True
        time.sleep(0.7)                          # no input within the window
        assert hub._control_enabled is False, "idle window must auto-disable"
    finally:
        hub.stop()


def test_accepted_input_resets_idle_timer():
    hub = m.MirrorHub(token="secret", host="127.0.0.1", port=0, idle_secs=0.4)
    hub.set_input_handler(lambda d: None)
    port = hub.serve()
    try:
        hub.set_control_state(True, "S")
        for _ in range(3):                       # keep poking before the window
            time.sleep(0.2)
            assert hub.inject("x") is True
        assert hub._control_enabled is True, "activity must keep control alive"
    finally:
        hub.stop()


def test_bad_write_key_increments_failure_counter():
    hub = m.MirrorHub(token="secret", host="127.0.0.1", port=0)
    hub.set_input_handler(lambda d: None)
    hub._control_enabled = True
    port = hub.serve()
    try:
        before = hub._bad_key.get("127.0.0.1", (0, 0.0, 0.0))[0]
        _post(port, "/input", {"data": "x"},
              headers={"X-Mirror-Write-Key": "wrong"})
        assert hub._bad_key.get("127.0.0.1", (0, 0.0, 0.0))[0] == before + 1, hub._bad_key
    finally:
        hub.stop()


def test_lan_input_requires_opt_in():
    """A LAN-exposed mirror (non-loopback host) refuses to ENABLE control unless
    allow_lan_input was opted in; loopback always allows it."""
    # Loopback: control may enable freely.
    lo = m.MirrorHub(token="t", host="127.0.0.1", port=0)
    lo.set_input_handler(lambda d: None)
    lo.set_control_state(True, "S")
    assert lo._control_enabled is True, "loopback control must enable"

    # LAN bind, NOT opted in: enabling control is refused (stays OFF).
    lan = m.MirrorHub(token="t", host="192.168.1.50", port=0)
    lan.set_input_handler(lambda d: None)
    lan.allow_lan_input = False
    lan.set_control_state(True, "S")
    assert lan._control_enabled is False, "LAN input must require opt-in"

    # LAN bind, opted in: enabling control is allowed.
    lan2 = m.MirrorHub(token="t", host="192.168.1.50", port=0)
    lan2.set_input_handler(lambda d: None)
    lan2.allow_lan_input = True
    lan2.set_control_state(True, "S")
    assert lan2._control_enabled is True, "opted-in LAN control must enable"


def test_page_contains_input_listeners_and_sender():
    """No browser in CI: assert the served page wires the writekey/control SSE
    listeners, the onData single-flight POST sender (with the write-key header),
    coalescing/flush-on-control-byte, the CONTROL banner, and the 409/403
    reactions. Manual phone verification covers actual keystroke fidelity."""
    hub = m.MirrorHub(token="secret", host="127.0.0.1", port=0, cols=80, rows=24)
    hub.set_input_handler(lambda d: None)
    port = hub.serve()
    try:
        page = urllib.request.urlopen(
            f"http://127.0.0.1:{port}/?token=secret", timeout=3.0
        ).read().decode("utf-8")
        # SSE named-event listeners (NOT onmessage) for writekey + control.
        assert "addEventListener('writekey'" in page, page
        assert "addEventListener('control'" in page, page
        # Input capture + single-flight POST to /input with the write-key header.
        assert "term.onData" in page, page
        assert "/input" in page and "X-Mirror-Write-Key" in page, page
        # Coalescing + flush on control bytes (ESC / CR / <0x20).
        assert "< 32" in page, page
        # CONTROL banner + target + disabled-until-on.
        assert "CONTROL ON" in page and "CONTROL OFF" in page, page
        assert "typing into" in page, page
        # Client reactions to the server gate.
        assert "409" in page and "403" in page, page
        # The output path is untouched (still base64 via onmessage).
        assert "es.onmessage" in page and "atob" in page, page
    finally:
        hub.stop()


def test_page_has_no_js_breaking_control_bytes():
    """Regression: the served page (HTML + inline JS) must contain no raw C0
    control byte except TAB/LF. A literal CR baked into the JS by a non-raw
    Python string once ended a // comment early (CR is a JS line terminator),
    turning the rest of the line into code -> 'Unexpected token' -> blank page.
    A string-only content check missed it; this catches the byte itself."""
    hub = m.MirrorHub(token="secret", host="127.0.0.1", port=0, cols=80, rows=24)
    hub.set_input_handler(lambda d: None)
    port = hub.serve()
    try:
        page = urllib.request.urlopen(
            f"http://127.0.0.1:{port}/?token=secret", timeout=3.0
        ).read().decode("utf-8")
        norm = page.replace("\r\n", "\n")           # legit CRLF line endings are fine
        stray = sorted({ord(c) for c in norm if ord(c) < 32 and c not in "\t\n"})
        assert stray == [], f"stray control bytes in served page (lone CR=13): {stray}"
    finally:
        hub.stop()


def test_wildcard_bind_allows_lan_ip_host():
    """Regression: a 0.0.0.0 (wildcard) bind must accept the LAN IP that url()
    advertises as a Host header -- otherwise a phone using that IP gets 403 on
    every request. _allowed_hosts must include _lan_ip() for a wildcard bind,
    while still rejecting a foreign host (anti-rebinding intact)."""
    hub = m.MirrorHub(token="secret", host="0.0.0.0", port=0)
    hub.set_input_handler(lambda d: None)
    port = hub.serve()
    lan = m._lan_ip()
    try:
        assert _raw_request(port, "GET", "/?token=secret",
                            {"Host": f"{lan}:{port}"}) == 200, \
            "wildcard bind rejected its own advertised LAN IP host (403)"
        assert _raw_request(port, "GET", "/?token=secret",
                            {"Host": f"127.0.0.1:{port}"}) == 200
        assert _raw_request(port, "GET", "/?token=secret",
                            {"Host": "evil.example.com"}) == 403
    finally:
        hub.stop()


def test_local_ipv4s_time_bounds_slow_hostname_and_memoises():
    """Regression (macOS-CI hang): a slow/hanging hostname resolver must NOT block
    the per-request host check. _allowed_hosts calls _local_ipv4s on every request;
    getaddrinfo(gethostname()) can hang for a long time on a macOS '.local' runner,
    which timed the mirror requests out. _local_ipv4s now time-bounds the lookup and
    memoises, so it returns fast even when getaddrinfo sleeps forever."""
    import socket, time
    saved_cache, saved_gai = m._LOCAL_IPV4S_CACHE, socket.getaddrinfo
    m._LOCAL_IPV4S_CACHE = None

    def _slow(*a, **k):
        time.sleep(5)                        # simulate a macOS .local mDNS hang
        return saved_gai(*a, **k)
    socket.getaddrinfo = _slow
    try:
        t0 = time.monotonic()
        first = m._local_ipv4s()             # cold: bounded hostname cost, paid once
        assert time.monotonic() - t0 < 3.0, "_local_ipv4s blocked on a hanging resolver"
        t1 = time.monotonic()
        second = m._local_ipv4s()            # memoised: doesn't touch the resolver
        assert time.monotonic() - t1 < 0.5 and second == first
    finally:
        socket.getaddrinfo = saved_gai
        m._LOCAL_IPV4S_CACHE = saved_cache


def test_mirror_inject_mouse_double_gate_and_events():
    """_mirror_inject_mouse no-ops when control is OFF (UI-thread re-check), and
    when ON posts MouseDown+MouseUp for a click / MouseScroll* for a scroll, at
    the given 0-based coords. Built via __new__ + a post_message recorder (no
    textual run loop)."""
    try:
        from textual import events
    except Exception:
        print("SKIP test_mirror_inject_mouse_double_gate_and_events (textual unavailable)")
        return
    import saikai
    app = saikai._MirrorControl.__new__(saikai._MirrorControl)
    posted = []
    app.post_message = lambda ev: posted.append(ev)

    # Control OFF -> no-op (double-gate).
    app._control_enabled = False
    app._mirror_inject_mouse(5, 9, 0, "down")
    assert posted == [], "control OFF must post nothing"

    # Control ON, click down+up -> MouseDown then MouseUp at (5,9), button 0.
    app._control_enabled = True
    app._mirror_inject_mouse(5, 9, 0, "down")
    app._mirror_inject_mouse(5, 9, 0, "up")
    assert len(posted) == 2, posted
    assert isinstance(posted[0], events.MouseDown), posted
    assert isinstance(posted[1], events.MouseUp), posted
    for ev in posted:
        assert ev.x == 5 and ev.y == 9, (ev.x, ev.y)
        assert ev.screen_x == 5 and ev.screen_y == 9, (ev.screen_x, ev.screen_y)
        assert ev.button == 0, ev.button

    # Scroll -> MouseScrollUp / MouseScrollDown (button 0).
    posted.clear()
    app._mirror_inject_mouse(2, 3, 0, "scrollup")
    app._mirror_inject_mouse(2, 3, 0, "scrolldown")
    assert isinstance(posted[0], events.MouseScrollUp), posted
    assert isinstance(posted[1], events.MouseScrollDown), posted
    assert posted[0].x == 2 and posted[0].y == 3, (posted[0].x, posted[0].y)

    # Negative coord -> ignored (no event).
    posted.clear()
    app._mirror_inject_mouse(-1, 4, 0, "down")
    app._mirror_inject_mouse(4, -1, 0, "down")
    assert posted == [], "out-of-range cell must be ignored"

    # Over-range coord (beyond the live screen) -> ignored too. size is the App's
    # at runtime; set a stand-in on this headless mixin instance to exercise the
    # upper clamp.
    posted.clear()
    app.size = type("Sz", (), {"width": 80, "height": 24})()
    app._mirror_inject_mouse(999, 5, 0, "down")
    app._mirror_inject_mouse(5, 999, 0, "down")
    assert posted == [], "out-of-range (over screen) cell must be ignored"
    app._mirror_inject_mouse(10, 10, 0, "down")     # in range -> still posts
    assert len(posted) == 1 and isinstance(posted[0], events.MouseDown), posted
    posted.clear()

    # Unknown kind -> ignored.
    app._mirror_inject_mouse(1, 1, 0, "wiggle")
    assert posted == [], "unknown kind must post nothing"


def test_mirror_inject_key_double_gate_and_event():
    """_mirror_inject_key no-ops when control is OFF; when ON posts events.Key
    with the right key/character (printable char carries itself; a named key
    carries character=None). Built via __new__ + a post_message recorder."""
    try:
        from textual import events
    except Exception:
        print("SKIP test_mirror_inject_key_double_gate_and_event (textual unavailable)")
        return
    import saikai
    app = saikai._MirrorControl.__new__(saikai._MirrorControl)
    posted = []
    app.post_message = lambda ev: posted.append(ev)

    # Control OFF -> no-op.
    app._control_enabled = False
    app._mirror_inject_key("escape")
    assert posted == [], "control OFF must post nothing"

    # Control ON: a named key -> Key(key="escape"), non-printable (character None).
    app._control_enabled = True
    app._mirror_inject_key("escape")
    assert len(posted) == 1 and isinstance(posted[0], events.Key), posted
    assert posted[0].key == "escape", posted[0].key
    assert posted[0].is_printable is False, "named key must be non-printable"

    # A single printable char -> Key carries itself as character.
    posted.clear()
    app._mirror_inject_key(" ")               # space (the leader)
    assert posted[0].key == " " and posted[0].character == " ", posted[0]

    # A modified key -> Key(key="ctrl+c"), non-printable.
    posted.clear()
    app._mirror_inject_key("ctrl+c")
    assert posted[0].key == "ctrl+c" and posted[0].is_printable is False, posted[0]

    # Empty / non-str -> ignored (never post a garbage Key).
    posted.clear()
    app._mirror_inject_key("")
    app._mirror_inject_key(None)
    assert posted == [], "empty/None key must post nothing"


def test_page_routes_mouse_and_has_key_bar():
    """No browser in CI: assert the served page (a) routes SGR mouse in onData to
    POST /mouse (parsing b;col;row, 1-based -> 0-based, M/m -> kind) while keeping
    keyboard on /input, (b) has a postKey single-flight to /key with the write-key
    header, and (c) renders the on-screen key bar buttons (Leader/Esc/Tab/arrows/
    Ctrl/F12). Manual phone verification covers real tap/scroll fidelity."""
    hub = m.MirrorHub(token="secret", host="127.0.0.1", port=0, cols=80, rows=24)
    hub.set_input_handler(lambda d: None)
    hub.set_mouse_handler(lambda *a: None)
    hub.set_key_handler(lambda *a: None)
    port = hub.serve()
    try:
        page = urllib.request.urlopen(
            f"http://127.0.0.1:{port}/?token=secret", timeout=3.0
        ).read().decode("utf-8")
        # (a) SGR mouse routing in onData: the parser + the /mouse endpoint.
        assert "/mouse" in page, page
        # SGR prefix detected via a backslash-free char class ([[] = literal '['
        # then '<'); see test_sgr_mouse_regex_is_escaping_safe_and_correct.
        assert "[[]<" in page, "page must detect the SGR mouse prefix ESC[<"
        # 1-based -> 0-based conversion is present (a subtraction by 1).
        assert "- 1" in page, page
        # press/release distinguished (M vs m).
        assert "'M'" in page or '"M"' in page, page
        # keyboard still routes to /input (unchanged Phase B path).
        assert "/input" in page, page
        # The /mouse sender QUEUES reports (never drops a down/up — a dropped
        # 'up' would leave the host pane frozen / divider captured); only
        # consecutive same-direction scroll ticks coalesce.
        assert "mouseQueue" in page, "postMouse must queue (not drop) down/up"
        # (b) postKey single-flight to /key with the write-key header.
        assert "/key" in page and "X-Mirror-Write-Key" in page, page
        # (c) the on-screen key bar buttons (F12 rides kb2 as "Mirror QR").
        for label in ("Leader", "Esc", "Tab", "Enter", "Ctrl", "List", "Mirror QR"):
            assert label in page, f"key bar missing {label}: {page[:200]}"
        assert 'data-k="f12"' in page, page
        # Enter (resume + focus a pane from the list, or submit to a focused
        # pane) and the release key (ctrl+] -> drop pane focus back to the list)
        # MUST be tappable: typed text rides /input to the focused pane, so these
        # app-level keys can only reach the app via the key bar. Without Enter the
        # browser can never focus a pane and typed characters go nowhere.
        assert 'data-k="enter"' in page, page
        # List must send the Textual-normalized release key name (the literal
        # "ctrl+]" does NOT match RELEASE_FOCUS_KEY=="ctrl+right_square_bracket",
        # so it would never release pane focus).
        assert 'data-k="ctrl+right_square_bracket"' in page, page
        # Mobile fit: a viewport meta so phones render at device-width (not a
        # ~980px zoomed-out layout that shrinks every control), and touch-sized
        # key-bar buttons (>=44px target, 16px font to avoid iOS focus-zoom).
        assert "width=device-width" in page, page
        assert "min-height:44px" in page, page
        # arrows present (any of the glyphs or names).
        assert ("↑" in page or "up" in page), page    # up arrow / "up"
        # the gate reactions are still wired for the new senders.
        assert "409" in page and "403" in page, page
        # output path untouched.
        assert "es.onmessage" in page and "atob" in page, page
    finally:
        hub.stop()


def test_page_key_bar_has_saikai_action_keys():
    """The on-screen key bar must expose saikai's OWN actions so a phone can DRIVE
    saikai, not just type into a pane: refresh (f5), next-attention (shift+f3),
    close pane (f10), copy (f9), restore (shift+f4), open search (slash), and fast
    list paging (pageup/pagedown). f5/f9/f10/shift+f3/shift+f4 are priority
    bindings (fire even with a pane focused); slash + paging work when the list is
    focused. They sit behind a 'More' toggle so the default bar stays compact.
    (Manual phone verification covers real action firing.)"""
    hub = m.MirrorHub(token="secret", host="127.0.0.1", port=0, cols=80, rows=24)
    hub.set_key_handler(lambda *a: None)
    port = hub.serve()
    try:
        page = urllib.request.urlopen(
            f"http://127.0.0.1:{port}/?token=secret", timeout=3.0
        ).read().decode("utf-8")
    finally:
        hub.stop()
    for k in ('data-k="f5"', 'data-k="shift+f3"', 'data-k="f10"', 'data-k="f9"',
              'data-k="shift+f2"', 'data-k="shift+f4"', 'data-k="f11"',
              'data-k="shift+f11"',
              'data-k="slash"', 'data-k="pageup"', 'data-k="pagedown"'):
        assert k in page, f"key bar missing {k}"
    # A 'More' toggle reveals a secondary action row (keeps the default compact).
    assert "kb-more" in page, "no More toggle for the secondary action row"
    assert "kb2" in page, "no secondary action row container"
    # The secondary buttons must ride the SAME postKey path (write-key + /key).
    assert "postKey" in page and "/key" in page, page


def test_page_wires_touch_swipe_to_scroll():
    """A phone has no wheel, so a touch-swipe emits no SGR scroll and xterm (mouse
    mode 1000 = no motion) reports nothing — `overflow:auto` only pans the
    rendered image, never the list. The page must translate a single-finger
    VERTICAL drag into the same scrollup/scrolldown the wheel path uses (-> POST
    /mouse), and manage touch-action so the browser yields the vertical drag while
    keeping horizontal pan + pinch-zoom. (Manual phone verification covers real
    swipe fidelity.)"""
    hub = m.MirrorHub(token="secret", host="127.0.0.1", port=0, cols=80, rows=24)
    hub.set_mouse_handler(lambda *a: None)
    port = hub.serve()
    try:
        page = urllib.request.urlopen(
            f"http://127.0.0.1:{port}/?token=secret", timeout=3.0
        ).read().decode("utf-8")
    finally:
        hub.stop()
    # A touch-swipe handler must exist (not just the pointerdown focus listener).
    assert "touchstart" in page, "no touch-swipe handler — a phone cannot scroll"
    assert "touchmove" in page, "no touch-swipe handler — a phone cannot scroll"
    # ...and it must drive the SAME /mouse scroll path the wheel already uses.
    assert "scrolldown" in page and "scrollup" in page, page
    assert "postMouse" in page, page
    # touch-action must be set so the browser yields the vertical drag to us
    # (pan-x keeps horizontal pan; pinch-zoom keeps zoom). Without it overflow:auto
    # just pans the image and the swipe never reaches saikai.
    assert "pan-x" in page, page


def test_page_wires_mouse_drag_to_scroll():
    """A desktop/laptop viewer drives the mirror with a MOUSE. xterm mouse mode
    1000 reports a press/release but NO motion, so a click-drag produced no
    scroll at all (the user saw a Claude pane ignore mouse-drag where a swipe
    scrolls). The page must translate a held left-button mouse drag into the
    SAME scrollup/scrolldown the touch-swipe + wheel paths use, so drag-to-scroll
    works with a mouse too. (Manual desktop verification covers real fidelity.)"""
    hub = m.MirrorHub(token="secret", host="127.0.0.1", port=0, cols=80, rows=24)
    hub.set_mouse_handler(lambda *a: None)
    port = hub.serve()
    try:
        page = urllib.request.urlopen(
            f"http://127.0.0.1:{port}/?token=secret", timeout=3.0
        ).read().decode("utf-8")
    finally:
        hub.stop()
    # A mouse-drag handler must exist (not only the touch-swipe one).
    assert "mousedown" in page, "no mouse-drag handler — a mouse cannot scroll"
    assert "mousemove" in page, "no mouse-drag handler — a mouse cannot scroll"
    # It must gate on the HELD left button (e.buttons & 1) so a plain hover never
    # scrolls and only a real drag drives the host.
    assert "buttons" in page, "mouse-drag must require the button held (e.buttons)"
    # ...and drive the SAME /mouse scroll path the touch + wheel paths use.
    assert "postMouse" in page and "scrollup" in page and "scrolldown" in page, page


def test_page_wires_select_mode_and_copy():
    """A Select toggle turns drag-to-scroll into drag-to-SELECT-text: the user
    asked for mouse-drag range selection (a drag used to only scroll). While
    selectMode is on, the drag drives a character-precise xterm selection
    (term.select over a reading-order length that wraps rows) instead of scroll,
    SGR taps are suppressed (a tap must not click a row mid-select), and a Copy
    control reads term.getSelection() to the clipboard — with an execCommand
    fallback because a plain-http LAN mirror is not a secure context, so
    navigator.clipboard is unavailable. (Manual verification covers real drag
    fidelity + clipboard.)"""
    hub = m.MirrorHub(token="secret", host="127.0.0.1", port=0, cols=80, rows=24)
    hub.set_mouse_handler(lambda *a: None)
    hub.set_key_handler(lambda *a: None)
    port = hub.serve()
    try:
        page = urllib.request.urlopen(
            f"http://127.0.0.1:{port}/?token=secret", timeout=3.0
        ).read().decode("utf-8")
    finally:
        hub.stop()
    # a Select toggle button + the mode state it flips.
    assert 'id="kb-select"' in page, "no Select toggle in the key bar"
    assert "selectMode" in page, "no select-mode state"
    assert "setSelectMode" in page, "no select-mode toggle handler"
    # the drag drives a BLOCK (rectangle) selection: our own overlay + per-row
    # buffer reads — a linear selection crossed the split divider and picked
    # garbage from both panes. (#mirror-blocksel)
    assert "blockText" in page and "selrect" in page, \
        "select mode must be the block model (overlay + blockText)"
    assert "translateToString" in page, "copy must read rows from the xterm buffer"
    assert "clearSel" in page, "toggling select off must clear the rectangle"
    # near the top/bottom edge the drag auto-scrolls the HOST (Chrome-like).
    # The zone must hug the VISIBLE terminal box (.xterm-screen) — #t spans the
    # viewport behind the fixed key bar, so a #t-based bottom zone sat ~150px
    # under the bar and never fired. (#mirror-edgezone)
    assert "edgeScroll" in page and "scrollup" in page, "no edge auto-scroll"
    _edge = page[page.index("function edgeZone"):page.index("function selectTo")]
    assert ".xterm-screen" in _edge and "paddingTop" in _edge, \
        "edge zone must be the VISIBLE slice (canvas box clamped to #t's padded viewport)"
    # Two-stage: pan the oversized canvas locally FIRST (works read-only; the
    # selection grows under a stationary finger), then drive the HOST at the
    # canvas edge. (#mirror-edgezone)
    assert "scrollTop" in _edge, "stage 1 (local pan) missing"
    assert "lastPY" in _edge, "the tick must re-check the finger position"
    # Region-aware zones: the host publishes scrollable rects ('regions' SSE);
    # a selection anchored INSIDE the claude pane must use the PANE's edges
    # (they sit mid-canvas — the canvas-edge zone never fired there), and the
    # wheel is posted at a cell clamped INTO that region. (#mirror-regions)
    # a host resize must live-resize the browser xterm (fixed-size otherwise).
    assert "'size'" in page and "term.resize(" in page, "no live-resize listener"
    assert "hostRegions" in page and "'regions'" in page, "no regions listener"
    assert "anchorRegion" in page, "zone must follow the anchor's host region"
    _zone = page[page.index("function anchorRegion"):page.index("function edgeScroll")]
    assert "rg.y" in _zone and "rg.h" in _zone, "region rows must bound the zone"
    # read-only must SAY why the host won't scroll instead of silently stalling,
    # and &debug=1 exposes live counters for on-machine diagnosis.
    assert "CONTROL ON" in page, "no read-only edge hint"
    assert "'debug'" in page and "__dbg" in page, "no debug overlay"
    # the select drag must keep flowing over the fixed overlays (window-level
    # mousemove — an el-scoped listener went silent over the key bar).
    assert "window.addEventListener('mousemove'" in page, page
    # a tap while selecting must NOT click a row (SGR forwarding suppressed).
    assert "taps drive selection" in page or "selectMode) return" in page, \
        "SGR mouse must be suppressed while selecting"
    # Copy is LAN-safe: navigator.clipboard needs a secure context (https/
    # localhost) which a plain-http LAN mirror is not — an execCommand fallback
    # must exist so copy works over http.
    assert "execCommand('copy')" in page, "no http-safe clipboard fallback"
    assert 'id="sel-copy"' in page and 'id="sel-done"' in page, \
        "no Copy/Done controls for select mode"
    # touch-action must switch to 'none' so a select drag isn't stolen for pan.
    assert "'none'" in page, "select mode must capture the drag (touch-action:none)"
    # Selection is LOCAL (never touches the host) — it must work for READ-ONLY
    # viewers too, so in drag() the selectMode branch must come BEFORE the
    # controlOn gate. (Fable5 re-check finding: the first cut gated selection
    # behind control, locking read-only viewers out of a local operation.)
    _drag = page[page.index("function drag("):]
    _drag = _drag[:_drag.index("function end(")]
    assert _drag.index("selectMode") < _drag.index("controlOn"), \
        "drag(): selection must not be gated behind controlOn (read-only viewers select too)"
    # Copy must hand the keyboard back to the terminal (the hidden-textarea
    # fallback steals focus).
    assert "term.focus" in page.split('id="sel-copy"')[0] or "term.focus()" in page, page


def test_page_inline_script_passes_csp():
    """The hardened CSP (script-src 'self') BLOCKED the page's own inline
    <script>: the mirror rendered NOTHING in a real browser while the
    string-asserting tests stayed green — the exact mock-boundary bug class.
    The inline script is now fully static (cols/rows ride <body data-*>) and
    its sha256 hash is whitelisted in the CSP header; this test recomputes the
    hash from the SERVED bytes and matches it against the SERVED header, so
    any drift (a second inline script, an edit without a rehash) fails here
    instead of blanking the page. (#audit-csp-inline)"""
    import base64 as _b64
    import hashlib as _hl
    import re as _re
    hub = m.MirrorHub(token="secret", host="127.0.0.1", port=0, cols=61, rows=19)
    port = hub.serve()
    try:
        resp = urllib.request.urlopen(
            f"http://127.0.0.1:{port}/?token=secret", timeout=3.0)
        page = resp.read().decode("utf-8")
        csp = resp.headers.get("Content-Security-Policy", "")
    finally:
        hub.stop()
    # cols/rows must NOT be substituted inside the script (that would change
    # its hash per size) — they ride data attributes on <body>.
    assert 'data-cols="61"' in page and 'data-rows="19"' in page, page[:300]
    assert "__COLS__" not in page and "__ROWS__" not in page, "unsubstituted markers"
    # exactly one inline script, and its hash is whitelisted in the header.
    inline = _re.findall(r"<script>(.*?)</script>", page, _re.S)
    assert len(inline) == 1, f"expected exactly ONE inline script, got {len(inline)}"
    digest = _b64.b64encode(_hl.sha256(inline[0].encode("utf-8")).digest()).decode()
    assert f"'sha256-{digest}'" in csp, \
        f"served inline script hash not whitelisted by CSP: {csp}"
    assert "script-src 'self'" in csp and "unsafe-inline" not in csp.split("style-src")[0], \
        f"script-src must stay strict (self + hash only): {csp}"


def test_page_key_bar_flow_and_labels():
    """Key-bar layout follows the remote workflow and drops the confusing labels:
    F12 (connection QR) is setup, not a during-session key, so it lives in the
    secondary row as 'Mirror QR'; shift+f11 is /compact — labelling it 'Refresh'
    collided with f5 Refresh, so it reads 'Compact' now (one Refresh only)."""
    hub = m.MirrorHub(token="secret", host="127.0.0.1", port=0, cols=80, rows=24)
    hub.set_key_handler(lambda *a: None)
    port = hub.serve()
    try:
        page = urllib.request.urlopen(
            f"http://127.0.0.1:{port}/?token=secret", timeout=3.0
        ).read().decode("utf-8")
    finally:
        hub.stop()
    # F12 present but wired to the secondary row with a descriptive label.
    assert 'data-k="f12"' in page and ">Mirror QR<" in page, page
    # shift+f11 relabelled Compact; exactly one "Refresh" (f5) — the collision is gone.
    assert 'data-k="shift+f11"' in page and ">Compact<" in page, page
    assert page.count("Refresh") == 1, \
        f"'Refresh' must be unambiguous (f5 only), found {page.count('Refresh')}"
    # Ergonomic v3 tiers (reach-ordered, not reading-ordered): row1 rare keys,
    # row2 the LOOP keys (List + promoted !Next), row3 Esc | d-pad | Enter.
    assert 'id="kb-row1"' in page and 'id="kb-row2"' in page \
        and 'id="kb-row3"' in page, "three-tier bar missing"
    r2 = page[page.index('id="kb-row2"'):page.index('id="kb-row3"')]
    assert 'data-k="ctrl+right_square_bracket"' in r2 \
        and 'data-k="shift+f3"' in r2, "loop keys (List/Next) must ride row 2"
    r3 = page[page.index('id="kb-row3"'):page.index('id="kb2"')]
    assert r3.index('data-k="escape"') < r3.index('id="kb-arrows"') \
        < r3.index('data-k="enter"'), \
        "row3 must run Esc (far side) → d-pad → Enter (thumb side)"
    # hold-to-repeat on the d-pad (buttons have no key repeat otherwise).
    assert "pointerdown" in page and "setInterval" in page \
        and "pointerleave" in page, "d-pad hold-to-repeat missing"
    # Phones must not get the soft keyboard on every tap (it hid the key bar):
    # focus follows MOUSE pointers only, and typing rides the ⌨ composer. (#mirror-ime)
    assert 'id="kb-kbd"' in page, "no composer toggle for touch typing"
    # The composer is a VISIBLE textarea (the OS paste bubble works — the xterm
    # helper textarea is 1px/invisible so mobile PASTE was impossible), and Send
    # frames the text as a bracketed paste when the host mirrored ?2004h in.
    # (#mirror-composer)
    assert 'id="kb-comp"' in page and 'id="comp-text"' in page, "no composer tray"
    assert 'id="comp-send-cr"' in page and 'id="comp-send"' in page, \
        "composer needs Send and Send-⏎"
    assert "bracketedPasteMode" in page, \
        "composer must frame sends as a bracketed paste when the host has ?2004h"
    assert "pointer: coarse" in page and "pointerType" in page, \
        "touch taps must not focus the xterm textarea"
    # Checkpoint rides the More row as a pseudo-key the app dispatches.
    assert 'data-k="checkpoint"' in page and ">Checkpoint<" in page, \
        "no Checkpoint button in the More row"
    # Handedness: a ⇄ Hand toggle mirrors the bars (row-reverse) so the d-pad
    # cluster sits under the LEFT thumb for left-handed users, persisted per
    # browser (localStorage). All three bars flip together.
    assert 'id="kb-hand"' in page, "no handedness toggle"
    assert "saikai-hand" in page, "handedness must persist (localStorage key)"
    assert "row-reverse" in page, "handedness must mirror via flex row-reverse"
    assert "applyHand" in page and "selBar.style.flexDirection" in page, \
        "the select bar must mirror with the key bar"


def test_page_wires_long_press_context_menu():
    """Feature 4: a long-press (touch) / right-click (mouse) on a row opens a
    context menu whose buttons post saikai's existing row actions (resume / copy /
    favorite / hide / rename) for the row under the pointer. Pure browser-side
    (selects the row via a tap, then posts /key) -- no MouseMove synthesis."""
    hub = m.MirrorHub(token="secret", host="127.0.0.1", port=0, cols=80, rows=24)
    hub.set_mouse_handler(lambda *a: None)
    hub.set_key_handler(lambda *a: None)
    port = hub.serve()
    try:
        page = urllib.request.urlopen(
            f"http://127.0.0.1:{port}/?token=secret", timeout=3.0
        ).read().decode("utf-8")
    finally:
        hub.stop()
    # The menu builder + both entry points (touch long-press timer, right-click).
    assert "openMenu" in page and "ctxmenu" in page, "no context-menu builder"
    assert "contextmenu" in page, "no right-click entry to the context menu"
    assert "setTimeout" in page, "no long-press timer"
    # The menu rides the existing senders: select the row (postMouse), then the
    # action keys (postKey -> /key). The action labels reference saikai's keys.
    assert "postKey" in page and "postMouse" in page, page
    assert "shift+f2" in page and "f9" in page, "menu must offer rename/copy"


def test_sgr_mouse_regex_is_escaping_safe_and_correct():
    """Regression (found by a headless-Edge smoke, not by the string-asserts):
    the SGR mouse regex must be built with NO backslash. A backslash inside a
    `new RegExp('...')` string argument does NOT survive the
    Python-string -> served-JS -> JS-string-literal -> RegExp chain: JS string
    parsing collapses the bracket-escape to a bare '[' and the digit-escape to
    the letter 'd', producing an INVALID regex that throws at page load and
    blanks the page on a real browser. Only executing the JS catches this; the
    page string-asserts above (and a static source read) do not. The fix uses
    String.fromCharCode(27) + char classes ([[] and [0-9]) so the pattern body
    carries no backslash."""
    import re as _re
    hub = m.MirrorHub(token="secret", host="127.0.0.1", port=0, cols=80, rows=24)
    hub.set_mouse_handler(lambda *a: None)
    port = hub.serve()
    try:
        page = urllib.request.urlopen(
            f"http://127.0.0.1:{port}/?token=secret", timeout=3.0
        ).read().decode("utf-8")
    finally:
        hub.stop()
    regex_lines = [ln for ln in page.splitlines()
                   if "sgrMouseRe" in ln and "new RegExp" in ln]
    assert regex_lines, "sgrMouseRe construction not found in served page"
    regex_line = regex_lines[0]
    # The bug: ANY backslash in the new RegExp string arg collapses in the
    # browser's JS string parsing and throws -> blank page.
    assert "\\" not in regex_line, (
        "backslash in the SGR regex arg will collapse in JS and blank the page; "
        "use char-class forms instead: " + regex_line)
    # The effective pattern (ESC + the body) must be a valid regex that parses a
    # real SGR mouse report. This proxy checks only the pattern SEMANTICS (the
    # backslash-survival is asserted above); a literal '[' is matched here with
    # \[ to avoid a Python-only "nested set" FutureWarning for the JS form [[].
    rx = _re.compile(chr(27) + r"\[<([0-9]+);([0-9]+);([0-9]+)([Mm])")
    mm = rx.match("\x1b[<0;5;3M")
    assert mm and mm.groups() == ("0", "5", "3", "M"), "must parse a press report"
    assert rx.match("\x1b[<64;10;2M"), "must match a scroll-up report"
    assert rx.match("plain typed text") is None, "must not match plain text"


def test_client_count_and_change_handler():
    """The hub tracks connected SSE clients and fires a change handler with the
    new count on each connect/disconnect, so saikai can show how many browsers
    are viewing and toast a newly-connected one (the user's security ask)."""
    hub = m.MirrorHub(token="secret", host="127.0.0.1", port=0, cols=80, rows=24)
    seen = []
    hub.set_client_change_handler(lambda n: seen.append(n))
    assert hub.client_count() == 0
    cq, _snap = hub._add_client()
    assert hub.client_count() == 1
    cq2, _ = hub._add_client()
    assert hub.client_count() == 2
    hub._remove_client(cq)
    assert hub.client_count() == 1
    hub._remove_client(cq2)
    assert hub.client_count() == 0
    assert seen == [1, 2, 1, 0], seen



def test_page_pane_view_contracts():
    """Pane direct view page contracts (#pane-direct): the view rides the SSE
    URL; output is gated until the first full-state seed; pane-reset resets the
    terminal then applies the seed; pane-meta resizes the follower terminal;
    onData routes VERBATIM to /raw in pane view; the key bar translates
    terminal keys to raw sequences honoring DECCKM; the composer sends raw in
    pane view; and the More row has the view toggle."""
    page = m._PAGE_HTML
    assert "view=pane" in page and "paneView" in page
    assert "'/stream?token=' + encodeURIComponent(token) +" in page
    assert "paneSeeded" in page, "output must be gated until the seed"
    assert "pane-reset" in page and "term.reset()" in page
    assert "pane-meta" in page and "term.resize(m.cols, m.rows)" in page
    assert "fetch('/raw'" in page, "raw pump must post /raw"
    assert "sendRaw(d)" in page, "pane-view onData must go raw"
    assert "applicationCursorKeysMode" in page, "arrows must honor DECCKM"
    assert "function dispatchKey" in page and "paneRawSeq" in page
    assert "kb-view" in page, "the More row needs the Pane/App toggle"
    assert "mouseTrackingMode" in page, \
        "drag-scroll must emit raw wheel reports only when the child tracks the mouse"
    # the app view must NOT force mouse tracking in pane view (the child owns modes)
    assert "if (!paneView) { try { term.write(ESC + '[?1000;1006h'); } catch (e) {} }" in page
    # ── review hardening (#review-*) ─────────────────────────────────────────
    # raw input is DROPPED (not buffered) while control is off — a backlog
    # replayed on control-ON could accept a live confirmation prompt
    assert "if (fatal || !controlOn || writeKey === null) return;" in page.split(
        "function sendRaw(d)")[1].split("}")[1] or \
        "if (fatal || !controlOn || writeKey === null) return;" in page.split(
        "function sendRaw(d)")[1][:400], "sendRaw needs the admission gate"
    assert "try { pendingRaw = ''; } catch (e) {}" in page, \
        "control-off must clear the raw backlog"
    # host-size frames must not resize the pane-view terminal
    assert "if (paneView) return;   // pane view sizes from pane-meta" in page
    # Home/End honor DECCKM like the arrows
    assert "if (k === 'home') return ESC + (app ? 'O' : '[') + 'H';" in page
    # keys with no raw encoding are dropped in pane view (never the invisible app)
    assert "never forwarded to" in page and "postKey(k);" in page
    # app-only buttons hidden in pane view
    assert "const appOnly = ['slash', 'f5', 'f10', 'f9', 'shift+f2', 'shift+f4', 'f11'," in page
    # composer draft survives the view toggle / seed retry
    assert "function stashDraft()" in page and "sessionStorage.getItem('saikai-draft')" in page
    # unseeded-but-open pane view retries (bounded)
    assert "saikai-seed-retry" in page and "location.reload();" in page
    # composer strips embedded paste markers before framing (early-close guard)
    assert "v.split(ESC + '[200~').join('').split(ESC + '[201~').join('')" in page
    # a new pane generation regates output + re-arms the blank-view backstop
    assert "let paneGen = null;" in page and "function armSeedRetry()" in page
    assert "m.gen !== paneGen" in page and "paneSeeded = false;" in page
    # paste-marker removal loops until stable (a single pass can re-form a
    # marker at the deletion seam) — #review-paste-overlap
    assert "do {" in page and "} while (v !== _prev);" in page
    print("PASS test_page_pane_view_contracts")

if __name__ == "__main__":
    test_page_pane_view_contracts()
    test_inject_gate_off_by_default_and_requires_handler()
    test_inject_is_fifo_via_single_drain()
    test_typed_inject_dispatches_by_tag_in_order()
    test_mouse_and_key_inject_gate_on_control_and_handler()
    test_update_control_target_syncs_without_rearming_idle()
    test_post_input_write_key_and_body_matrix()
    test_post_refused_input_returns_429()
    test_host_allow_list_and_origin_matrix()
    test_post_mouse_gate_and_body_matrix()
    test_post_mouse_host_and_origin_matrix()
    test_post_key_gate_and_body_matrix()
    test_sse_emits_writekey_and_control_without_colliding_output()
    test_set_control_state_pushes_control_frame()
    test_idle_auto_disable_flips_control_off()
    test_accepted_input_resets_idle_timer()
    test_bad_write_key_increments_failure_counter()
    test_lan_input_requires_opt_in()
    test_page_contains_input_listeners_and_sender()
    test_page_has_no_js_breaking_control_bytes()
    test_sgr_mouse_regex_is_escaping_safe_and_correct()
    test_client_count_and_change_handler()
    test_wildcard_bind_allows_lan_ip_host()
    test_local_ipv4s_time_bounds_slow_hostname_and_memoises()
    test_mirror_inject_mouse_double_gate_and_events()
    test_mirror_inject_key_double_gate_and_event()
    test_page_routes_mouse_and_has_key_bar()
    test_page_key_bar_has_saikai_action_keys()
    test_page_wires_touch_swipe_to_scroll()
    test_page_wires_mouse_drag_to_scroll()
    test_page_wires_select_mode_and_copy()
    test_page_inline_script_passes_csp()
    test_page_key_bar_flow_and_labels()
    test_page_wires_long_press_context_menu()
    print("OK test_mirror_input")
