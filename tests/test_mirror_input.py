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
        before = hub._bad_key_count
        _post(port, "/input", {"data": "x"},
              headers={"X-Mirror-Write-Key": "wrong"})
        assert hub._bad_key_count == before + 1, hub._bad_key_count
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


if __name__ == "__main__":
    test_inject_gate_off_by_default_and_requires_handler()
    test_inject_is_fifo_via_single_drain()
    test_typed_inject_dispatches_by_tag_in_order()
    test_mouse_and_key_inject_gate_on_control_and_handler()
    test_post_input_write_key_and_body_matrix()
    test_host_allow_list_and_origin_matrix()
    test_post_mouse_gate_and_body_matrix()
    test_post_mouse_host_and_origin_matrix()
    test_sse_emits_writekey_and_control_without_colliding_output()
    test_set_control_state_pushes_control_frame()
    test_idle_auto_disable_flips_control_off()
    test_accepted_input_resets_idle_timer()
    test_bad_write_key_increments_failure_counter()
    test_lan_input_requires_opt_in()
    test_page_contains_input_listeners_and_sender()
    test_page_has_no_js_breaking_control_bytes()
    test_wildcard_bind_allows_lan_ip_host()
    print("OK test_mirror_input")
