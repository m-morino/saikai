import os, re, sys, threading
import urllib.request, urllib.error, base64, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from pathlib import Path
import saikai_mirror as m


def _get(url):
    return urllib.request.urlopen(url, timeout=3.0)


def test_set_size_broadcasts_and_dedups():
    """A host resize must reach live browsers: set_size broadcasts a _Size frame
    (the xterm is fixed-size otherwise, so absolute host ANSI garbles), deduped
    on an unchanged size. Fresh clients read the size from the page's data-*
    attrs on connect. (#mirror-resize)"""
    hub = m.MirrorHub(token="t", cols=100, rows=40)
    import queue as _q, json as _json
    cq = _q.Queue(maxsize=8)
    with hub._clients_lock:
        hub._clients.add(cq)
    hub.set_size(120, 50)
    frame = cq.get_nowait()
    assert type(frame).__name__ == "_Size", frame
    assert _json.loads(frame.json) == {"cols": 120, "rows": 50}
    assert (hub._cols, hub._rows) == (120, 50)
    hub.set_size(120, 50)                 # unchanged -> deduped
    assert cq.empty(), "unchanged size must not rebroadcast"
    with hub._clients_lock:
        hub._clients.discard(cq)


def test_set_regions_dedups_and_reaches_clients():
    """set_regions publishes host scrollable rects as a named SSE frame:
    identical layouts are deduped (it rides hot paths), clients receive a
    _Regions frame, and a FRESH client gets the current layout on connect
    (the initial stream sends _regions_json). (#mirror-regions)"""
    hub = m.MirrorHub(token="t", cols=100, rows=40)
    import queue as _q
    cq = _q.Queue(maxsize=8)
    with hub._clients_lock:
        hub._clients.add(cq)
    regs = [{"x": 40, "y": 4, "w": 60, "h": 26, "k": "pane"}]
    hub.set_regions(regs)
    frame = cq.get_nowait()
    assert type(frame).__name__ == "_Regions", frame
    import json as _json
    assert _json.loads(frame.json) == regs
    # dedup: same layout again -> nothing queued
    hub.set_regions(list(regs))
    assert cq.empty(), "identical layout must be deduped"
    # a change flows again
    hub.set_regions([])
    assert type(cq.get_nowait()).__name__ == "_Regions"
    assert hub._regions_json == "[]"
    with hub._clients_lock:
        hub._clients.discard(cq)


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
    # A wildcard bind is refused unless explicitly opted in: 0.0.0.0 falls back to a
    # concrete address (the LAN IP, or loopback offline), never stays 0.0.0.0. (#audit-mirror-wildcard-bind)
    en, host = _m.mirror_config({"SAIKAI_MIRROR": "1", "SAIKAI_MIRROR_HOST": "0.0.0.0"})
    assert en is True and host != "0.0.0.0"
    # ...but WITH the opt-in, the wildcard is honored verbatim.
    assert _m.mirror_config({"SAIKAI_MIRROR": "1", "SAIKAI_MIRROR_HOST": "0.0.0.0",
                             "SAIKAI_MIRROR_ALLOW_ALL_INTERFACES": "1"}) == (True, "0.0.0.0")


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
    layout. The size rides <body data-cols/data-rows> — NOT inline-script
    substitution, which would change the script's bytes per size and break its
    CSP hash whitelist (#audit-csp-inline) — and the script reads the dataset
    into Terminal()."""
    hub = m.MirrorHub(token="t", cols=137, rows=43)
    port = hub.serve()
    try:
        page = _get(f"http://127.0.0.1:{port}/?token=t").read().decode("utf-8")
        assert 'data-cols="137"' in page and 'data-rows="43"' in page
        assert "dataset.cols" in page and "dataset.rows" in page
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


def test_norm_src_collapses_rotatable_identities():
    """A lockout key must be stable so an attacker can't rotate source identities:
    v4-mapped-v6 collapses to the bare v4, and an IPv6 address collapses to its
    /64 prefix (one host owns the whole prefix). (#audit-mirror-ratecap)"""
    assert m._norm_src("::ffff:1.2.3.4") == "1.2.3.4"
    assert m._norm_src("1.2.3.4") == "1.2.3.4"
    a = m._norm_src("2001:db8:1:2:aaaa:bbbb:cccc:dddd")
    b = m._norm_src("2001:db8:1:2:1111:2222:3333:4444")
    assert a == b, "same /64 must map to one lockout identity"
    assert m._norm_src("2001:db8:1:9::1") != a, "different /64 stays distinct"
    # The SAME address in compressed vs expanded form MUST map to one key (a naive
    # string split gave two, handing back the identity-rotation this prevents).
    assert m._norm_src("2001:db8::1:2:3:4") == m._norm_src("2001:db8:0:0:1:2:3:4")
    # Malformed / hostname / sentinel pass through unchanged (no crash).
    assert m._norm_src("not-an-ip") == "not-an-ip" and m._norm_src("?") == "?"
    # The write-key lockout keys through _norm_src, so two mapped forms share a bucket.
    hub = m.MirrorHub(token="t")
    hub._note_bad_key("::ffff:9.9.9.9")
    assert "9.9.9.9" in hub._bad_key and "::ffff:9.9.9.9" not in hub._bad_key


def test_read_token_has_its_own_lockout():
    """The read token gets a per-source lockout in a SEPARATE budget from the
    write-key, so guessing one can't consume the other's cooldown. (#audit-mirror-ratecap)"""
    hub = m.MirrorHub(token="t")
    src = "10.1.1.1"
    assert hub._token_locked_out(src) is False
    for _ in range(m._BAD_TOKEN_LOCKOUT_THRESHOLD):
        hub._note_bad_token(src)
    assert hub._token_locked_out(src) is True
    # separate budget: write-key lockout for the same src is untouched.
    assert hub._input_locked_out(src) is False
    assert src in hub._bad_token and src not in hub._bad_key


def test_proven_source_is_exempt_from_lockouts():
    """A source that presented a VALID credential is exempt from BOTH lockouts, so
    a hostile peer sharing its IPv6 /64 (or the operator's own stale-token tab)
    can't lock out the real operator's device. An un-proven peer stays throttled.
    (#audit-mirror-lockout-grace)"""
    hub = m.MirrorHub(token="t")
    # Attacker floods bad write-keys from the SAME /64 as the operator → arms lockout.
    for _ in range(m._BAD_KEY_LOCKOUT_THRESHOLD):
        hub._note_bad_key("2001:db8:1:2::99")
    assert hub._input_locked_out("2001:db8:1:2::abc") is True     # un-proven, same /64
    hub._mark_proven("2001:db8:1:2::5")                           # operator authenticates
    assert hub._input_locked_out("2001:db8:1:2::5") is False      # exempt despite shared bucket
    # Read-token lockout honours the same grace (stale-tab self-lockout fix).
    hub2 = m.MirrorHub(token="t")
    for _ in range(m._BAD_TOKEN_LOCKOUT_THRESHOLD):
        hub2._note_bad_token("10.0.0.5")
    assert hub2._token_locked_out("10.0.0.5") is True
    hub2._mark_proven("10.0.0.5")
    assert hub2._token_locked_out("10.0.0.5") is False
    # The grace expires (bounded): a far-past deadline is swept / ignored.
    import time as _t
    hub2._proven["10.0.0.5"] = _t.monotonic() - 1.0
    assert hub2._token_locked_out("10.0.0.5") is True


def test_paste_framing_rejects_embedded_esc():
    """A bracketed-paste region with an interior raw ESC is the injection-smuggling
    pattern; a well-behaved browser never sends it. (#audit-mirror-paste-smuggle)"""
    assert m._paste_framing_ok("\x1b[200~hello world\x1b[201~") is True
    assert m._paste_framing_ok("plain keystrokes \x1b[A") is True   # arrow key, no paste
    assert m._paste_framing_ok("\x1b[200~a\x1b]52;c;AAAA\x07b\x1b[201~") is False
    assert m._paste_framing_ok("\x1b[200~no end marker but \x1b here") is False
    # a nested open starts with ESC, so the body-ESC rule already rejects it
    assert m._paste_framing_ok("\x1b[200~\x1b[200~x\x1b[201~\x1b[201~") is False
    # an early-close (…ESC[201~ then live keys) is deliberately ACCEPTED here:
    # /input needs the write key, so the sender could type those keys anyway —
    # it's not a privilege boundary. The composer prevents the accidental case
    # at the source (marker-strip loop). (#review-paste-earlyclose)
    assert m._paste_framing_ok("\x1b[200~safe\x1b[201~\revil") is True


def test_tls_scheme_and_url():
    """TLS is ON by DEFAULT (opt-out via SAIKAI_MIRROR_TLS=0/false/no/off) so the LAN
    transport is encrypted and the browser gets a secure context; when the hub is
    given a cert/key pair its scheme + url() flip to https so the QR/URL advertise
    the encrypted origin. (#audit-mirror-tls, #mirror-tls-default-on)"""
    assert m.mirror_tls_enabled({}) is True                           # unset → default-on
    assert m.mirror_tls_enabled({"SAIKAI_MIRROR_TLS": "1"}) is True
    assert m.mirror_tls_enabled({"SAIKAI_MIRROR_TLS": "0"}) is False   # explicit opt-out
    assert m.mirror_tls_enabled({"SAIKAI_MIRROR_TLS": "off"}) is False
    assert m.mirror_tls_enabled({"SAIKAI_MIRROR_TLS": ""}) is True     # empty → default-on
    plain = m.MirrorHub(token="tok", host="127.0.0.1", port=9999)
    assert plain._scheme == "http" and plain.url().startswith("http://")
    secure = m.MirrorHub(token="tok", host="127.0.0.1", port=9999,
                         tls=("/x/cert.pem", "/x/key.pem"))
    assert secure._scheme == "https" and secure.url().startswith("https://")


def test_resolve_tls_paths_precedence():
    """User-provided cert+key win when both exist; a named-but-missing pair returns
    None (never silently self-signs); absent env → openssl self-sign (if available). """
    import tempfile
    from pathlib import Path
    d = Path(tempfile.mkdtemp())
    cert, key = d / "c.pem", d / "k.pem"
    cert.write_text("x"); key.write_text("y")
    got = m.resolve_tls_paths(
        {"SAIKAI_MIRROR_TLS_CERT": str(cert), "SAIKAI_MIRROR_TLS_KEY": str(key)}, d)
    assert got == (str(cert), str(key))
    # named but missing → None (don't fall back to self-sign under the user's nose)
    assert m.resolve_tls_paths(
        {"SAIKAI_MIRROR_TLS_CERT": str(d / "nope.pem"),
         "SAIKAI_MIRROR_TLS_KEY": str(key)}, d) is None
    # no cert env → self-sign in-process. This must work with NO openssl binary
    # (the Windows case that used to fall back to plain HTTP). (#review-tls-windows)
    import shutil
    _real_which = shutil.which
    shutil.which = lambda n: None if n == "openssl" else _real_which(n)
    try:
        auto = m.resolve_tls_paths({}, d / "auto", "192.168.1.50")
    finally:
        shutil.which = _real_which
    assert auto is not None, "self-sign must work without the openssl binary"
    assert Path(auto[0]).is_file() and Path(auto[1]).is_file()
    # the minted cert loads into a TLS server, covers the host, and is valid
    import ssl as _ssl
    _ssl.SSLContext(_ssl.PROTOCOL_TLS_SERVER).load_cert_chain(auto[0], auto[1])
    assert m._cert_covers(auto[0], {"127.0.0.1", "192.168.1.50"})
    assert m._cert_valid_for(auto[0], 3600)
    import os as _os
    if _os.name == "posix":
        assert (_os.stat(auto[1]).st_mode & 0o077) == 0, "key must be owner-only"
    # the OUTCOME is always inspectable: success names the minter, and a
    # fallback names the CAUSE (an http-only mirror was undiagnosable — the
    # helpers swallow their exceptions by design). (#review-tls-reason)
    assert m.tls_reason(), "resolve must record an outcome"
    got_named_missing = m.resolve_tls_paths(
        {"SAIKAI_MIRROR_TLS_CERT": "/nope.pem",
         "SAIKAI_MIRROR_TLS_KEY": "/nope.key"}, d)
    assert got_named_missing is None and "missing on disk" in m.tls_reason()
    import builtins
    real_import = builtins.__import__
    def _broken(name, *a, **k):
        if name.startswith("cryptography"):
            raise ImportError("simulated absence")
        return real_import(name, *a, **k)
    builtins.__import__ = _broken
    shutil.which = lambda n: None if n == "openssl" else _real_which(n)
    try:
        got_none = m.resolve_tls_paths({}, d / "none", "10.9.9.9")
    finally:
        builtins.__import__ = real_import
        shutil.which = _real_which
    assert got_none is None
    assert "cryptography unavailable" in m.tls_reason() \
        and "openssl not on PATH" in m.tls_reason(), m.tls_reason()


def test_add_client_caps_concurrent_viewers():
    """The SSE viewer cap bounds a token-holder opening streams in a loop (each
    forces a UI-thread repaint). Over cap → (None, None), no registration. (#audit-mirror-dos)"""
    hub = m.MirrorHub(token="t", cols=4, rows=2)
    held = []
    for _ in range(m._MAX_SSE_CLIENTS):
        cq, snap = hub._add_client()
        assert cq is not None
        held.append(cq)
    cq, snap = hub._add_client()            # one past the cap
    assert cq is None and snap is None
    assert hub.client_count() == m._MAX_SSE_CLIENTS



# ══════════════════════════════════════════════════════════════════════════════
# Pane direct view (#pane-direct)
# ══════════════════════════════════════════════════════════════════════════════
def test_pane_seed_roundtrip_restores_grid_and_modes():
    """_synth_pane_seed must be a FULL state: feeding the seed into a fresh pyte
    screen reproduces the original grid (glyphs + colors), and the tracked
    terminal modes (alt-screen, DECCKM, mouse, bracketed paste, cursor
    visibility) are replayed explicitly — set OR reset — so a browser xterm
    joining mid-session lands in exactly the child's state. (#pane-direct)"""
    import pyte
    src = pyte.Screen(20, 5)
    st = pyte.Stream(src)
    st.feed("\x1b[1;1H\x1b[38;2;255;100;0mHOT\x1b[0m plain "
            "\x1b[48;5;27m\x1b[97mBLU\x1b[0m\x1b[3;4H\x1b[1mBoldY\x1b[0m")
    modes = {"alt": True, "app_cursor": True, "mouse_click": True,
             "mouse_btn_motion": False, "mouse_any_motion": False,
             "mouse_sgr": True, "focus_reporting": False,
             "bracketed_paste": True, "cursor_hidden": False}
    seed = m._synth_pane_seed(src, 20, 5, modes)
    # mode replay: every tracked mode appears explicitly, h or l per the flag
    for want in ("\x1b[?1049h", "\x1b[?1h", "\x1b[?1000h", "\x1b[?1002l",
                 "\x1b[?1003l", "\x1b[?1004l", "\x1b[?1006h",
                 "\x1b[?2004h", "\x1b[?25h"):
        assert want in seed, f"seed must replay {want!r}"
    assert seed.index("\x1b[?1049h") < seed.index("\x1b[2J"), \
        "alt-screen enter must precede the paint (it targets the alt buffer)"
    # xterm.js quirk net: ANY of ?1000l/?1002l/?1003l zeroes the (single) mouse
    # protocol slot regardless of which protocol is active — so the one enable
    # must come AFTER every reset of the family, or tracking ends up OFF.
    assert seed.rindex("\x1b[?1003l") < seed.index("\x1b[?1000h"), \
        "the mouse enable must FOLLOW the protocol-slot resets (xterm.js quirk)"
    stacked = dict(modes, mouse_btn_motion=True, mouse_any_motion=True)
    s2 = m._synth_pane_seed(src, 20, 5, stacked)
    assert "\x1b[?1003h" in s2 and "\x1b[?1002h" not in s2 \
        and "\x1b[?1000h" not in s2, \
        "stacked child enables must replay only the STRONGEST protocol"
    dst = pyte.Screen(20, 5)
    pyte.Stream(dst).feed(seed)
    for y in range(5):
        for x in range(20):
            a, b = src.buffer[y][x], dst.buffer[y][x]
            assert (a.data or " ") == (b.data or " "), f"glyph {y},{x}: {a} vs {b}"
            assert (a.fg, a.bg, a.bold) == (b.fg, b.bg, b.bold), \
                f"attrs {y},{x}: {a} vs {b}"
    assert (dst.cursor.y, dst.cursor.x) == (src.cursor.y, src.cursor.x)
    print("PASS test_pane_seed_roundtrip_restores_grid_and_modes")


def test_pane_channel_routes_by_view_and_reseeds_on_fallbehind():
    """Pane frames reach ONLY pane-view clients (and app frames only app-view
    clients); a _PaneReset REPLACES a pane client's backlog; and a fallen-behind
    pane client triggers the app-reseed callback instead of drop-oldest (a
    spliced raw stream is permanent corruption). (#pane-direct)"""
    import queue as _q, json as _json
    hub = m.MirrorHub(token="t", cols=20, rows=5)
    port = hub.serve()               # starts the ingest drain thread
    try:
        app_q, pane_q = _q.Queue(256), _q.Queue(4)
        with hub._clients_lock:
            hub._clients.add(app_q)
            hub._pane_clients.add(pane_q)
        reseeds = []
        hub.set_pane_reseed_request(lambda: reseeds.append(1))
        hub.pane_feed("\x1b[31mPANE\x1b[0m")
        deadline = time.time() + 3.0
        got = None
        while time.time() < deadline and got is None:
            try:
                got = pane_q.get(timeout=0.1)
            except _q.Empty:
                pass
        assert got is not None and type(got).__name__ == "_PaneData", got
        assert got.data == "\x1b[31mPANE\x1b[0m"
        hub.broadcast("APPFRAME")
        deadline = time.time() + 3.0
        gotapp = None
        while time.time() < deadline and gotapp is None:
            try:
                gotapp = app_q.get(timeout=0.1)
            except _q.Empty:
                pass
        assert gotapp == "APPFRAME"
        assert pane_q.empty(), "app frames must not reach a pane client"
        # pane frames must not reach the app client
        assert app_q.empty(), "pane frames must not reach an app client"
        # meta rides the same ordered path
        hub.set_pane_meta({"open": True, "cols": 20, "rows": 5, "title": "x"})
        deadline = time.time() + 3.0
        meta = None
        while time.time() < deadline and meta is None:
            try:
                meta = pane_q.get(timeout=0.1)
            except _q.Empty:
                pass
        assert type(meta).__name__ == "_PaneMeta" and _json.loads(meta.json)["open"] is True
        hub.set_pane_meta({"open": True, "cols": 20, "rows": 5, "title": "x"})   # dedup
        # a reset REPLACES the backlog
        hub.pane_feed("stale1"); hub.pane_feed("stale2")
        hub.pane_reset("SEED")
        deadline = time.time() + 3.0
        frames = []
        while time.time() < deadline:
            try:
                frames.append(pane_q.get(timeout=0.1))
            except _q.Empty:
                if frames and type(frames[-1]).__name__ == "_PaneReset":
                    break
        assert frames and type(frames[-1]).__name__ == "_PaneReset"
        # the reset must ARRIVE LAST: no pane data may follow it in the backlog
        # (frames before it may legitimately be stale data the collector drained
        # before the flush landed — only data AFTER the reset would be a bug)
        assert type(frames[-1]).__name__ == "_PaneReset" and \
            all(type(f).__name__ != "_PaneReset" for f in frames[:-1])
        # fallen behind: fill the tiny queue -> flushed + reseed requested
        for i in range(10):
            hub.pane_feed(f"burst{i}")
        deadline = time.time() + 3.0
        while time.time() < deadline and not reseeds:
            time.sleep(0.05)
        assert reseeds, "a fallen-behind pane client must trigger an app reseed"
    finally:
        hub.stop()
    print("PASS test_pane_channel_routes_by_view_and_reseeds_on_fallbehind")


def test_raw_endpoint_gates_and_dispatches():
    """/raw: same gate chain as the other input routes (write key -> 403,
    control off -> 409), and an accepted body reaches the raw handler VERBATIM
    (escape sequences intact — it is a terminal byte stream). (#pane-direct)"""
    import json as _json
    hub = m.MirrorHub(token="t", host="127.0.0.1", port=0, cols=10, rows=2)
    port = hub.serve()
    try:
        base = f"http://127.0.0.1:{port}"
        got = []
        hub.set_raw_handler(lambda d: got.append(d))
        def post(key, body):
            req = urllib.request.Request(
                f"{base}/raw", data=_json.dumps(body).encode(),
                headers={"Content-Type": "application/json",
                         "Origin": f"http://127.0.0.1:{port}",
                         **({"X-Mirror-Write-Key": key} if key else {})})
            try:
                return urllib.request.urlopen(req, timeout=3.0).status
            except urllib.error.HTTPError as e:
                return e.code
        assert post(None, {"data": "x"}) == 403, "missing write key must 403"
        assert post(hub._write_key, {"data": "x"}) == 409, "control off must 409"
        hub.set_control_state(True, "pane")
        payload = "\x1b[<64;5;6MHello\x1b[A"
        assert post(hub._write_key, {"data": payload}) == 204
        deadline = time.time() + 3.0
        while time.time() < deadline and not got:
            time.sleep(0.05)
        assert got == [payload], f"raw handler must get the verbatim bytes: {got!r}"
    finally:
        hub.stop()
    print("PASS test_raw_endpoint_gates_and_dispatches")


def test_pane_stream_sends_meta_and_reset_seed():
    """GET /stream?view=pane: the greeting carries writekey + control +
    pane-meta (current geometry), the connect fires the app-reseed request, and
    a pane_reset arrives as a named pane-reset event with the base64 seed —
    while APP output frames never appear on this connection. (#pane-direct)"""
    hub = m.MirrorHub(token="secret", host="127.0.0.1", port=0, cols=10, rows=2)
    port = hub.serve()
    try:
        hub.set_pane_meta({"open": True, "cols": 33, "rows": 7, "title": "T"})
        hub.set_pane_reseed_request(lambda: hub.pane_reset("\x1b[2J\x1b[HSEED"))
        resp = _get(f"http://127.0.0.1:{port}/stream?token=secret&view=pane")
        hub.broadcast("APPONLY")
        deadline = time.time() + 5.0
        seen = b""
        while time.time() < deadline:
            chunk = resp.read1(256)
            if not chunk:
                break
            seen += chunk
            # break only once the reset's DATA line is complete (read1 can stop
            # right after the event: line)
            if b"pane-reset" in seen and seen.rstrip(b" ").endswith(b"\n\n"):
                break
        txt = seen.decode("utf-8", "replace")
        assert "event: writekey" in txt
        assert "event: pane-meta" in txt and '"cols": 33' in txt
        assert "event: pane-reset" in txt, txt
        import re as _re
        mres = _re.search(r'event: pane-reset\ndata: (\{[^\n]+\})', txt)
        assert mres, txt
        import json as _json
        seed = base64.b64decode(_json.loads(mres.group(1))["seed"]).decode()
        assert seed == "\x1b[2J\x1b[HSEED"
        assert "QVBQT05MWQ==" not in txt, "app frames must not reach a pane stream"
    finally:
        hub.stop()
    print("PASS test_pane_stream_sends_meta_and_reset_seed")


def test_pane_flush_preserves_control_meta_and_sentinel():
    """Every pane backlog flush must PRESERVE what a reseed cannot restore: the
    last unconsumed _Control (sent only on state change), the last _PaneMeta
    (deduped at source) and the stop() sentinel — losing any of them left a
    stale banner / stale geometry / an SSE thread looping after shutdown.
    (#review-pane-frame-loss)"""
    import queue as _q
    cq = _q.Queue(16)
    ctrl1 = m._Control('{"on": true}')
    ctrl2 = m._Control('{"on": false}')
    meta = m._PaneMeta('{"cols": 92}')
    for item in (m._PaneData("stale1"), ctrl1, m._PaneData("stale2"),
                 meta, ctrl2, None):
        cq.put_nowait(item)
    m.MirrorHub._flush_pane_backlog(cq)
    kept = []
    try:
        while True:
            kept.append(cq.get_nowait())
    except _q.Empty:
        pass
    assert kept == [ctrl2, meta, None], f"flush must keep last ctrl, meta, sentinel: {kept}"
    print("PASS test_pane_flush_preserves_control_meta_and_sentinel")


def test_pane_reset_carries_meta_and_drain_strips_queries():
    """(1) A reseed must CARRY the current meta (geometry before paint; a meta
    lost to any flush is re-delivered by the next reseed — set_pane_meta dedups
    at source and would never resend it). (2) Child terminal QUERIES are
    stripped on the DRAIN thread via set_pane_strip, so a pane-view browser
    never auto-answers them. (#review-pane-meta-loss #pane-direct)"""
    import queue as _q, re as _re, json as _json
    hub = m.MirrorHub(token="t", cols=20, rows=5)
    port = hub.serve()
    try:
        hub.set_pane_strip(_re.compile("\x1b\\[0?c|\x1b\\[\\??[56]n"))
        pane_q = _q.Queue(64)
        with hub._clients_lock:
            hub._pane_clients.add(pane_q)
        hub.set_pane_meta({"open": True, "cols": 33, "rows": 7, "title": "T"})
        hub.pane_reset("SEED")
        got = []
        deadline = time.time() + 3.0
        while time.time() < deadline and len(got) < 2:
            try:
                got.append(pane_q.get(timeout=0.1))
            except _q.Empty:
                pass
        resets = [f for f in got if type(f).__name__ == "_PaneReset"]
        assert resets, f"no reset delivered: {got}"
        assert _json.loads(resets[0].meta)["cols"] == 33, \
            "the reseed must carry the CURRENT meta"
        # queries stripped in the drain, payload preserved
        hub.pane_feed("plain \x1b[6n text \x1b[0c done")
        deadline = time.time() + 3.0
        data = None
        while time.time() < deadline and data is None:
            try:
                f = pane_q.get(timeout=0.1)
                if type(f).__name__ == "_PaneData":
                    data = f
            except _q.Empty:
                pass
        assert data is not None and data.data == "plain  text  done", \
            f"drain must strip queries: {data!r}"
    finally:
        hub.stop()
    print("PASS test_pane_reset_carries_meta_and_drain_strips_queries")


def test_pane_feed_gates_on_clients_and_caps_inflight():
    """The tee must cost ~nothing in the steady state (mirror on, no pane
    browser): pane_feed drops with no pane clients. And it may not starve app
    frames out of the SHARED ingest queue: beyond the inflight cap, chunks are
    dropped and _pane_lost marks the reseed debt. (#review-pane-flood)"""
    hub = m.MirrorHub(token="t", cols=20, rows=5)   # no serve(): inspect the queue
    hub.pane_feed("dropped — no clients")
    assert hub._ingest.qsize() == 0, "zero pane clients must gate the tee"
    import queue as _q
    with hub._clients_lock:
        hub._pane_clients.add(_q.Queue(4))
    hub.pane_feed("counted")
    assert hub._ingest.qsize() == 1 and hub._pane_inflight == 1
    hub._pane_inflight = m._PANE_INFLIGHT_CAP
    hub.pane_feed("over cap")
    assert hub._ingest.qsize() == 1, "over-cap pane data must be dropped"
    assert hub._pane_lost is True, "the drop must schedule a reseed"
    print("PASS test_pane_feed_gates_on_clients_and_caps_inflight")


def test_pane_strip_holds_split_dcs():
    """A DCS query (DECRQSS/XTGETTCAP) split across chunk boundaries must not
    slip past the drain-side strip — the reader reassembles split CSI/OSC but
    not DCS, so the hub carries the trailing unterminated DCS to the next chunk.
    A long legit DCS (sixel — never a strip target) is released, not held.
    (#review-dcs-split)"""
    import re as _re
    hub = m.MirrorHub(token="t", cols=10, rows=3)
    strip = _re.compile(r"\x1bP\$q[^\x07\x1b]*(?:\x07|\x1b\\)")
    a = hub._strip_pane_chunk(strip, "\x1bP$q")     # opens a DCS, no terminator
    b = hub._strip_pane_chunk(strip, "m\x1b\\OK")    # completes it + tail text
    assert a == "" and b == "OK", (a, b)             # DECRQSS stripped, tail kept
    assert hub._pane_strip_carry == ""
    # single-chunk still works
    assert hub._strip_pane_chunk(strip, "x\x1bP$qm\x1b\\y") == "xy"
    # a long NON-TARGET DCS (sixel) is released once past the small bound
    big = "\x1bP0q" + "d" * 600
    out = hub._strip_pane_chunk(strip, big)
    assert len(out) > 500 and hub._pane_strip_carry == "", "non-target must release"
    # but a long TARGET query ($q/+q) is HELD past that bound, not leaked
    # un-stripped to the browser (#review-dcs-bound)
    hub._pane_strip_carry = ""
    long_q = "\x1bP$q" + "6b6579" * 120     # ~720 chars, DECRQSS query, no ST
    r = hub._strip_pane_chunk(strip, long_q)
    assert r == "" and hub._pane_strip_carry == long_q, "target query must be held"
    assert hub._strip_pane_chunk(strip, "\x1b\\AFTER") == "AFTER"   # completes → stripped
    print("PASS test_pane_strip_holds_split_dcs")


def test_pane_strip_carry_cleared_at_stream_boundary():
    """The DCS carry is per-stream: a reseed (retarget / reopen / overflow) is a
    boundary, so a half-carried DCS from the OLD pane must be dropped — else it
    prefixes the NEW pane's first bytes and swallows them into a bogus query.
    (#review-carry-boundary)"""
    import queue as _q
    hub = m.MirrorHub(token="t", cols=10, rows=3)
    hub.set_pane_strip(re.compile(r"\x1bP\$q[^\x07\x1b]*(?:\x07|\x1b\\)"))
    cq = _q.Queue(64)
    with hub._clients_lock:
        hub._pane_clients.add(cq)
    hub._drain_pane_frame(m._PaneData("\x1bP$q"))          # old pane: unterminated DCS
    assert hub._pane_strip_carry == "\x1bP$q"
    hub._drain_pane_frame(m._PaneReset("SEED", '{"open": true}'))  # boundary
    assert hub._pane_strip_carry == "", "reseed must clear the carry"
    hub._drain_pane_frame(m._PaneData("HELLO"))             # new pane's first bytes
    seen = []
    try:
        while True:
            seen.append(cq.get_nowait())
    except _q.Empty:
        pass
    datas = [f.data for f in seen if type(f).__name__ == "_PaneData"]
    assert datas and datas[-1] == "HELLO", f"new-pane bytes swallowed: {datas}"
    # overflow path also clears it
    hub._pane_strip_carry = "\x1bP$q"
    hub._pane_lost = True
    hub._drain_overflow_recovery()
    assert hub._pane_strip_carry == "", "overflow recovery must clear the carry"
    print("PASS test_pane_strip_carry_cleared_at_stream_boundary")


def test_offer_sentinel_reaches_full_queue():
    """stop() must deliver the shutdown sentinel even to a FULL client queue, or
    that SSE handler loops on keepalives until process death. (#review-stop-sentinel)"""
    import queue as _q
    cq = _q.Queue(2)
    cq.put_nowait(m._PaneData("x"))
    cq.put_nowait(m._PaneData("y"))
    m.MirrorHub._offer_sentinel(cq)
    drained = []
    try:
        while True:
            drained.append(cq.get_nowait())
    except _q.Empty:
        pass
    assert None in drained, f"sentinel must be delivered: {drained}"
    print("PASS test_offer_sentinel_reaches_full_queue")

def test_overflow_repaint_is_gated_on_a_viewer_and_rate_limited():
    """The resync repaint is the app's most expensive frame (every cell re-emitted),
    and during a pane storm the ingest queue overflows on essentially every drain —
    so an ungated request latched saikai into one full repaint per drain cycle and
    the LOCAL ui went heavy in lockstep with a mirror nobody had to be watching.
    Ask at most once per window, and only with a browser attached."""
    import queue as _q
    hub = m.MirrorHub(token="t", cols=20, rows=5)
    calls = []
    hub.set_repaint_request(lambda: calls.append(1))

    # No viewer: overflow after overflow must not touch the app at all.
    for _ in range(5):
        hub._ingest_overflow = True
        hub._drain_overflow_recovery()
    assert calls == [], f"repaint requested with no viewer: {calls}"

    # With a viewer: the first overflow asks, the immediate ones behind it don't.
    cq = _q.Queue()
    with hub._clients_lock:
        hub._clients.add(cq)
    for _ in range(5):
        hub._ingest_overflow = True
        hub._drain_overflow_recovery()
    assert calls == [1], f"expected one request per window, got {len(calls)}"

    # Once the window passes, a fresh overflow asks again (a resync still has to
    # be possible — this is a rate limit, not a one-shot).
    hub._repaint_req_after = 0.0
    hub._ingest_overflow = True
    hub._drain_overflow_recovery()
    assert calls == [1, 1], calls

    with hub._clients_lock:
        hub._clients.discard(cq)

def test_hub_screen_keeps_text_after_a_width_zero_character():
    """The hub models the app's output, so it needs the same corrected `draw` the pane
    has. It used to build a stock `pyte.Screen`, whose draw BREAKS on the first width-0
    character and drops the rest of the run — so a live SSE client (raw bytes) looked
    right while every re-seed built from this screen (a phone connecting, a reconnect
    after a blip, a client falling behind) arrived with rows truncated at the emoji.
    (#saikai-screen-shared)"""
    import pyte

    hub = m.MirrorHub(token="t", cols=20, rows=2)

    # The class is shared with the pane, not re-implemented.
    try:
        import saikai_terminal as rt
        assert type(hub._screen) is rt._ScreenBase, type(hub._screen).__name__
        assert rt._ScreenBase.__mro__[1] is rt._HistoryScreenBase.__mro__[1], \
            "pane and hub screens must share ONE draw implementation"
    except ImportError:
        print("SKIP shared-class assertion (saikai_terminal unavailable)")

    def row0(scr, n=12):
        return "".join(scr.buffer[0][x].data for x in range(n))

    # Stock pyte is the baseline this test exists to beat.
    stock = pyte.Screen(20, 2)
    pyte.Stream(stock).feed("AB⚠️CDEF")
    assert "CDEF" not in row0(stock), \
        "precondition: stock pyte is expected to drop the run (got %r)" % row0(stock)

    for text, keep in (("AB⚠️CDEF", "CDEF"),
                       ("AB\U0001F469‍\U0001F4BBCDEF", "CDEF"),
                       ("ABéCDEF", "CDEF")):
        h = m.MirrorHub(token="t", cols=20, rows=2)
        pyte.Stream(h._screen).feed(text)
        got = row0(h._screen)
        assert keep in got, "hub screen dropped the run after a width-0 char: %r" % got

def test_drain_thread_survives_a_frame_that_raises():
    """The mirror's drain thread is the ONLY consumer of the ingest queue.

    Its body was unguarded, so one raise from the pyte feed, the frame synth or a pane
    frame ended it for good. The local UI kept working, so the mirror looked alive from
    the host side while every attached browser sat frozen on its last frame forever —
    and the overflow recovery that would have asked for a repaint lives inside this
    loop, so it died with it, while broadcast()/pane_feed() kept filling a queue nobody
    read. (#drain-survives-a-frame)"""
    import queue as _queue
    import threading as _threading
    import time as _time

    hub = m.MirrorHub.__new__(m.MirrorHub)
    hub._stopped = _threading.Event()
    hub._ingest = _queue.Queue(64)
    seen: list = []
    logged: list = []

    def _one(data):
        if data == "BOOM":
            raise ValueError("bad frame")
        seen.append(data)

    hub._drain_one = _one
    prev_hook, m.LOG_HOOK = m.LOG_HOOK, logged.append
    try:
        t = _threading.Thread(target=hub._drain_loop, daemon=True)
        t.start()
        for item in ("a", "BOOM", "b", "BOOM", "c"):
            hub._ingest.put(item)
        deadline = _time.monotonic() + 5
        while seen != ["a", "b", "c"] and _time.monotonic() < deadline:
            _time.sleep(0.02)
        hub._stopped.set()
        t.join(timeout=3)
    finally:
        m.LOG_HOOK = prev_hook

    assert seen == ["a", "b", "c"], \
        "the drain thread stopped consuming after a bad frame: %r" % (seen,)
    assert not t.is_alive(), "the drain thread did not stop when asked"
    assert getattr(hub, "_drain_errors", 0) == 2, hub.__dict__.get("_drain_errors")
    assert any("dropped a frame" in m and "ValueError" in m for m in logged), logged
    assert all(m.startswith("[mirror]") for m in logged), logged

def test_read_token_is_short_enough_to_type_and_the_write_key_is_not():
    """The read token is typed by hand on a phone; the write-key never is.

    The URL is the QR's payload AND what someone types when the camera is not
    convenient, so the read token is deliberately short: token_urlsafe(6) = 8 url-safe
    chars = 48 bits. That is bounded by the hub's OWN guessing budget rather than by
    hope — a bad read token arms a per-source 30s lockout after
    _BAD_TOKEN_LOCKOUT_THRESHOLD attempts, and the source is normalised to an IPv6 /64
    so rotation does not dodge it. At the resulting ~1.7 guesses/s, 2^47 is ~2.7 million
    years, on a mirror that also idles itself off.

    The write-key keeps its full length because it is never typed and never appears in
    a URL, file, QR or log — the authority to TYPE into a pane must not be weakened by
    a usability change to the READ link. (#short-token)"""
    import re as _re

    src = (Path(__file__).resolve().parent.parent / "saikai.py").read_text(
        encoding="utf-8")
    tok = _re.search(r"token=_secrets\.token_urlsafe\((\d+)\)", src)
    assert tok, "the hub's read token is no longer generated here"
    nbytes = int(tok.group(1))
    assert nbytes == 6, "read token size changed: %d bytes" % nbytes

    hub = m.MirrorHub(token="A" * 8, host="192.168.11.15", port=5112)
    url = hub.url()
    assert len(url) <= 44, "the URL got long enough to be annoying to type: %r" % url
    assert "?token=AAAAAAAA" in url, url
    # The write-key is long, is NOT the read token, and is nowhere in the link.
    assert len(hub._write_key) >= 40, len(hub._write_key)
    assert hub._write_key not in url
    # …and the guessing budget the short token relies on is actually in force.
    assert m._BAD_TOKEN_LOCKOUT_THRESHOLD <= 50, m._BAD_TOKEN_LOCKOUT_THRESHOLD
    assert m._BAD_KEY_LOCKOUT_SECS >= 10.0, m._BAD_KEY_LOCKOUT_SECS
    # The QR stays sparse enough for another PC's camera (the reason it is short).
    rows = m.qr_matrix(url)
    assert len(rows) <= 37, "QR grew to %dx%d modules" % (len(rows), len(rows[0]))

def test_outbox_serves_only_what_was_dropped_in_it():
    """File hand-off to the phone: the outbox directory, and nothing else.

    "Offering" a file IS putting it in ~/.cache/saikai/outbox — claude does that the
    moment it produces something, so the hand-off works while nobody is at the keyboard,
    and the user can do it from any shell. The phone lists exactly that directory.

    No request ever carries a PATH, only a bare name inside the outbox, so there is no
    traversal to defend — just a name to validate. Asserted end to end over real HTTP,
    because that is the layer an attacker meets: the token gate, the refusals (../ and
    ..\\ and an absolute path and a subdirectory), the TTL, and a Japanese filename
    surviving as RFC 5987 filename*. (#outbox)"""
    import json as _json
    import os
    import tempfile
    import time as _t
    from urllib.parse import quote

    box = tempfile.mkdtemp(prefix="saikai-outbox-test-")
    with open(os.path.join(box, "report.pptx"), "wb") as f:
        f.write(b"PPTX" * 100)
    jp = "日本語 レポート.xlsx"
    with open(os.path.join(box, jp), "wb") as f:
        f.write(b"XLSX" * 50)
    stale = os.path.join(box, "stale.txt")
    with open(stale, "wb") as f:
        f.write(b"old")
    os.utime(stale, (_t.time() - 90000, _t.time() - 90000))   # past the 24h TTL
    os.mkdir(os.path.join(box, "subdir"))
    with open(os.path.join(box, "subdir", "hidden.txt"), "wb") as f:
        f.write(b"nope")

    hub = m.MirrorHub(token="secret", host="127.0.0.1", port=0, outbox=box)
    port = hub.serve()
    base = "http://127.0.0.1:%d" % port
    try:
        # The listing shows the fresh plain files, newest first, and nothing else.
        with _get(base + "/files?token=secret") as r:
            listed = _json.loads(r.read().decode("utf-8"))["files"]
        names = [f["name"] for f in listed]
        assert names == [jp, "report.pptx"] or names == ["report.pptx", jp], names
        assert "stale.txt" not in names, "a file past the TTL was offered"
        assert "subdir" not in names, "a directory was offered"
        assert all(f["size"] > 0 and f["mtime"] > 0 for f in listed), listed

        # A download comes back whole, as an attachment.
        with _get(base + "/file/report.pptx?token=secret") as r:
            body = r.read()
            disp = r.headers.get("Content-Disposition") or ""
        assert body == b"PPTX" * 100, len(body)
        assert disp.startswith("attachment;"), disp

        # A Japanese name survives via filename* (and the ASCII fallback is present).
        with _get(base + "/file/" + quote(jp) + "?token=secret") as r:
            body = r.read()
            disp = r.headers.get("Content-Disposition") or ""
        assert body == b"XLSX" * 50, len(body)
        assert "filename*=UTF-8''" in disp, disp
        assert quote(jp, safe="") in disp, disp

        # Everything else is refused. The token gate first: without it, not even the
        # listing (which would otherwise disclose file names).
        for label, path, token in (
                ("listing without a token", "/files", False),
                ("file without a token", "/file/report.pptx", False),
                ("../ traversal", "/file/" + quote("../saikai.log"), True),
                ("..\\ traversal", "/file/" + quote("..\\saikai.log"), True),
                ("absolute path", "/file/" + quote("C:\\Windows\\win.ini"), True),
                ("a subdirectory", "/file/" + quote("subdir/hidden.txt"), True),
                ("past the TTL", "/file/stale.txt", True),
                ("not there", "/file/nope.bin", True),
                ("no name at all", "/file/", True),
        ):
            url = base + path + ("?token=secret" if token else "")
            try:
                _get(url)
                raise AssertionError("%s was served" % label)
            except urllib.error.HTTPError as e:
                assert e.code in (403, 404), "%s -> %d" % (label, e.code)
                if not token:
                    assert e.code == 403, "%s -> %d (want 403)" % (label, e.code)

        # Download slots are counted apart from the SSE viewers, so a big transfer
        # cannot starve the screen stream.
        assert m._MAX_DOWNLOADS >= 1 and m._MAX_DOWNLOADS < m._MAX_CONNECTIONS
        taken = [hub.take_download_slot() for _ in range(m._MAX_DOWNLOADS)]
        assert all(taken), taken
        assert hub.take_download_slot() is False, "the download cap does not hold"
        for _ in range(m._MAX_DOWNLOADS):
            hub.release_download_slot()
        assert hub.take_download_slot() is True, "slots are not released"
        hub.release_download_slot()

        # An oversize file is not offered (the cap is on the hub, not the browser).
        big = os.path.join(box, "big.bin")
        with open(big, "wb") as f:
            f.write(b"x")
        real_cap = m._OUTBOX_MAX_BYTES
        try:
            m._OUTBOX_MAX_BYTES = 0
            assert hub.outbox_resolve("big.bin") is None, "oversize file was resolved"
            assert "big.bin" not in [f[0] for f in hub.outbox_entries()]
        finally:
            m._OUTBOX_MAX_BYTES = real_cap
    finally:
        hub.stop()

    # With no outbox configured the feature is simply off — every existing caller.
    off = m.MirrorHub(token="t", host="127.0.0.1", port=0)
    assert off.outbox_entries() == [] and off.outbox_resolve("x") is None


if __name__ == "__main__":
    test_set_size_broadcasts_and_dedups()
    test_set_regions_dedups_and_reaches_clients()
    test_norm_src_collapses_rotatable_identities()
    print("PASS test_norm_src_collapses_rotatable_identities")
    test_read_token_has_its_own_lockout()
    print("PASS test_read_token_has_its_own_lockout")
    test_proven_source_is_exempt_from_lockouts()
    print("PASS test_proven_source_is_exempt_from_lockouts")
    test_paste_framing_rejects_embedded_esc()
    print("PASS test_paste_framing_rejects_embedded_esc")
    test_tls_scheme_and_url()
    print("PASS test_tls_scheme_and_url")
    test_resolve_tls_paths_precedence()
    print("PASS test_resolve_tls_paths_precedence")
    test_add_client_caps_concurrent_viewers()
    print("PASS test_add_client_caps_concurrent_viewers")
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
    test_pane_seed_roundtrip_restores_grid_and_modes()
    test_pane_channel_routes_by_view_and_reseeds_on_fallbehind()
    test_raw_endpoint_gates_and_dispatches()
    test_pane_stream_sends_meta_and_reset_seed()
    test_pane_flush_preserves_control_meta_and_sentinel()
    test_pane_reset_carries_meta_and_drain_strips_queries()
    test_pane_feed_gates_on_clients_and_caps_inflight()
    test_pane_strip_holds_split_dcs()
    test_offer_sentinel_reaches_full_queue()
    test_pane_strip_carry_cleared_at_stream_boundary()
    print("OK test_mirror_hub")
    test_overflow_repaint_is_gated_on_a_viewer_and_rate_limited()
    print("PASS test_overflow_repaint_is_gated_on_a_viewer_and_rate_limited")
    test_hub_screen_keeps_text_after_a_width_zero_character()
    print("PASS test_hub_screen_keeps_text_after_a_width_zero_character")
    test_drain_thread_survives_a_frame_that_raises()
    print("PASS test_drain_thread_survives_a_frame_that_raises")
    test_read_token_is_short_enough_to_type_and_the_write_key_is_not()
    print("PASS test_read_token_is_short_enough_to_type_and_the_write_key_is_not")
    test_outbox_serves_only_what_was_dropped_in_it()
    print("PASS test_outbox_serves_only_what_was_dropped_in_it")
