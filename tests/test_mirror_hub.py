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
        deadline = time.time() + 5.0
        seen = b""
        joined = ""
        while time.time() < deadline:
            chunk = resp.read1(64)   # read1: buffered bytes, don't block for a full 64
            if not chunk:
                break
            seen += chunk
            # Decode only COMPLETE "data:" lines. read1 can stop mid-frame, so
            # the trailing line may be a partial base64 chunk — decoding it threw
            # "Incorrect padding" flakily (timing-dependent, esp. on macOS CI).
            parts = []
            for ln in seen.decode("utf-8", "replace").split("\n")[:-1]:
                ln = ln.rstrip("\r")
                if ln.startswith("data: "):
                    try:
                        parts.append(base64.b64decode(ln[6:]).decode("utf-8", "replace"))
                    except Exception:
                        pass   # incomplete chunk — keep reading
            joined = "".join(parts)
            if "\x1b[2J\x1b[H" in joined and "GO" in joined:
                break
        assert seen.startswith(b"data: ")                      # stream opens with a frame
        assert "\x1b[2J\x1b[H" in joined, f"snapshot missing: {joined!r}"   # snapshot first
        assert "GO" in joined, f"GO frame missing: {joined!r}"
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


def test_mirror_port_parsing():
    import saikai_mirror as _m
    assert _m.mirror_port({}) == 0                                  # default ephemeral
    assert _m.mirror_port({"SAIKAI_MIRROR_PORT": "8771"}) == 8771
    assert _m.mirror_port({"SAIKAI_MIRROR_PORT": "bogus"}) == 0
    assert _m.mirror_port({"SAIKAI_MIRROR_PORT": "99999"}) == 0     # out of range


def test_static_assets_served_locally_without_token():
    """xterm.js/css are vendored and served from this origin (no CDN, works on
    locked-down/offline networks); the library asset needs no token, and the
    page must reference the local path, not a CDN."""
    hub = m.MirrorHub(token="secret", host="127.0.0.1", port=0)
    port = hub.serve()
    try:
        for asset in ("/xterm.min.js", "/addon-canvas.js", "/xterm.min.css"):
            r = _get(f"http://127.0.0.1:{port}{asset}")     # no token needed
            assert r.status == 200 and len(r.read(64)) > 0
        page = _get(f"http://127.0.0.1:{port}/?token=secret").read().decode("utf-8")
        assert "/xterm.min.js" in page and "cdn.jsdelivr" not in page
        assert "/addon-canvas.js" in page and "loadAddon" in page   # crisp borders
    finally:
        hub.stop()


def test_page_injects_terminal_size():
    """The browser xterm must be sized to the host terminal's cols/rows; the
    mirror streams absolute-positioned ANSI, so a size mismatch garbles the
    layout. The page bakes the live cols/rows into the Terminal() options."""
    hub = m.MirrorHub(token="t", cols=137, rows=43)
    port = hub.serve()
    try:
        page = _get(f"http://127.0.0.1:{port}/?token=t").read().decode("utf-8")
        assert "cols: 137" in page and "rows: 43" in page
        assert "__COLS__" not in page and "__ROWS__" not in page
    finally:
        hub.stop()


def test_broadcast_overflow_flags_resync():
    """On ingest overflow broadcast() must flag a resync (not splice): the whole
    stale backlog is dropped and _ingest_overflow is set so the drain requests a
    full repaint. (#audit-mirror-broadcast-splice)"""
    hub = m.MirrorHub(token="t", ingest_cap=4)
    assert hub._ingest_overflow is False
    for i in range(10):           # no drain thread → forces overflow
        hub.broadcast(f"f{i}")
    assert hub._ingest_overflow is True
    assert hub._ingest.qsize() <= 4


def test_resync_client_replaces_backlog_with_snapshot_and_control():
    """_resync_client drops a fallen-behind client's stale diffs and leaves it
    exactly [snapshot, control] — one clean repaint, not corruption. (#audit-mirror-sse-drop)"""
    import queue as _q
    cq = _q.Queue(256)
    for k in range(5):
        cq.put_nowait(f"stale-diff-{k}")
    ctrl = m._Control('{"on": true}')
    m.MirrorHub._resync_client(cq, "FULL-SNAPSHOT", ctrl)
    got = []
    while not cq.empty():
        got.append(cq.get_nowait())
    assert got == ["FULL-SNAPSHOT", ctrl], got


def test_bad_key_lockout_enforced_and_resets():
    """The bad-key counter must actually LOCK OUT input at the threshold (was a
    write-only counter) and auto-reset after the cooldown. (#audit-mirror-ratecap)"""
    hub = m.MirrorHub(token="t")
    src = "10.0.0.9"
    assert hub._input_locked_out(src) is False
    for _ in range(m._BAD_KEY_LOCKOUT_THRESHOLD):
        hub._note_bad_key(src)
    assert hub._input_locked_out(src) is True, "threshold of bad keys must lock out input"
    # A DIFFERENT source is unaffected — the lockout is per-peer, not hub-wide.
    assert hub._input_locked_out("10.0.0.42") is False
    # Simulate the cooldown elapsing → auto-reset, input allowed again.
    n, _until, seen = hub._bad_key[src]
    hub._bad_key[src] = (n, 1.0, seen)        # deadline far in the past (monotonic)
    assert hub._input_locked_out(src) is False
    assert src not in hub._bad_key
    # Sub-threshold strays are swept once idle past the TTL — no per-IP leak.
    hub._note_bad_key("10.0.0.7")
    n, until, _seen = hub._bad_key["10.0.0.7"]
    hub._bad_key["10.0.0.7"] = (n, until, -2 * m._BAD_KEY_TTL_SECS)
    hub._note_bad_key("10.0.0.8")             # any note sweeps expired entries
    assert "10.0.0.7" not in hub._bad_key, "idle sub-threshold entry must be swept"


def test_min_accept_gap_reads_env():
    """The accepted-input rate cap must be REACHABLE at runtime (was hardcoded 0.0
    → the documented flood control never engaged). (#audit-mirror-ratecap)"""
    os.environ["SAIKAI_MIRROR_MIN_ACCEPT_GAP"] = "0.05"
    try:
        hub = m.MirrorHub(token="t")
        assert abs(hub._min_accept_gap - 0.05) < 1e-9
    finally:
        os.environ.pop("SAIKAI_MIRROR_MIN_ACCEPT_GAP", None)
    assert m.MirrorHub(token="t")._min_accept_gap == 0.0   # absent → off (no regression)


if __name__ == "__main__":
    test_broadcast_is_nonblocking_and_drops_oldest()
    test_broadcast_overflow_flags_resync()
    test_resync_client_replaces_backlog_with_snapshot_and_control()
    test_bad_key_lockout_enforced_and_resets()
    test_min_accept_gap_reads_env()
    test_server_rejects_bad_token_and_streams_with_good_token()
    test_env_gate_default_off()
    test_url_includes_token_and_resolves_wildcard_host()
    test_mirror_port_parsing()
    test_static_assets_served_locally_without_token()
    test_page_injects_terminal_size()
    print("OK test_mirror_hub")
