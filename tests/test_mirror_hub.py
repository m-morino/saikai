import os, sys, threading
import urllib.request, urllib.error, base64, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import saikai_mirror as m


def _get(url):
    return urllib.request.urlopen(url, timeout=3.0)


def test_broadcast_is_nonblocking_and_drops_oldest():
    """broadcast() runs on Textual's UI thread; it must NEVER block, even if the
    drain side is stalled. When the bounded ingest queue is full it drops the
    OLDEST item and enqueues the newest, returning immediately."""
    hub = m.MirrorHub(token="t", ingest_cap=4)
    # Do NOT start the drain thread: simulate a fully stalled consumer.
    for i in range(1000):
        start = threading.Event()
        done = threading.Event()

        def call():
            start.set()
            hub.broadcast(f"frame-{i}")
            done.set()

        th = threading.Thread(target=call)
        th.start()
        done_ok = done.wait(timeout=2.0)
        th.join(timeout=2.0)
        assert done_ok, f"broadcast() blocked on iteration {i} (cap full)"
    assert hub._ingest.qsize() <= 4
    drained = []
    while not hub._ingest.empty():
        drained.append(hub._ingest.get_nowait())
    assert drained == ["frame-996", "frame-997", "frame-998", "frame-999"]


def test_server_rejects_bad_token_and_streams_with_good_token():
    hub = m.MirrorHub(token="secret", host="127.0.0.1", port=0, cols=10, rows=2)
    port = hub.serve()
    try:
        base = f"http://127.0.0.1:{port}"
        # Wrong token on the page and the stream -> 403.
        for path in ("/", "/stream"):
            try:
                _get(f"{base}{path}?token=nope")
                assert False, f"{path} accepted a bad token"
            except urllib.error.HTTPError as e:
                assert e.code == 403
        # Good token: a frame fed now must appear (base64) in the SSE stream,
        # and the stream must open with the full-frame snapshot.
        hub.broadcast("\x1b[32mGO\x1b[0m")
        resp = _get(f"{base}/stream?token=secret")
        deadline = time.time() + 3.0
        seen = b""
        while time.time() < deadline and b"\n\n" not in seen[1:]:
            seen += resp.read1(64)   # read1: return buffered bytes, don't block for a full 64
        text = seen.decode("utf-8", "replace")
        assert text.startswith("data: ")
        payloads = [base64.b64decode(ln[6:]).decode("utf-8", "replace")
                    for ln in text.splitlines() if ln.startswith("data: ")]
        joined = "".join(payloads)
        assert "\x1b[2J\x1b[H" in joined   # snapshot first
        assert "GO" in joined
    finally:
        hub.stop()


def test_env_gate_default_off():
    import saikai_mirror as _m
    assert _m.mirror_config({}) == (False, "127.0.0.1")
    assert _m.mirror_config({"SAIKAI_MIRROR": "1"}) == (True, "127.0.0.1")
    assert _m.mirror_config({"SAIKAI_MIRROR": "0", "SAIKAI_MIRROR_HOST": "0.0.0.0"}) == (False, "0.0.0.0")
    assert _m.mirror_config({"SAIKAI_MIRROR": "1", "SAIKAI_MIRROR_HOST": "0.0.0.0"}) == (True, "0.0.0.0")


def test_url_includes_token_and_resolves_wildcard_host():
    h = m.MirrorHub(token="tok", host="127.0.0.1", port=9999)
    assert "127.0.0.1:9999" in h.url() and "token=tok" in h.url()
    # 0.0.0.0 is a bind wildcard, not browsable: url() must resolve it away.
    h2 = m.MirrorHub(token="tok", host="0.0.0.0", port=9999)
    assert "0.0.0.0" not in h2.url() and ":9999" in h2.url() and "token=tok" in h2.url()


if __name__ == "__main__":
    test_broadcast_is_nonblocking_and_drops_oldest()
    test_server_rejects_bad_token_and_streams_with_good_token()
    test_env_gate_default_off()
    test_url_includes_token_and_resolves_wildcard_host()
    print("OK test_mirror_hub")
