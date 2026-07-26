import os, re, sys, threading
import urllib.request, urllib.error, base64, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
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
        for asset in (
                "/xterm.min.js", "/addon-canvas.js",
                "/addon-saikai-rich-graphemes.js", "/xterm.min.css"):
            r = _get(f"http://127.0.0.1:{port}{asset}")     # no token needed
            assert r.status == 200 and len(r.read(64)) > 0
            assert "no-cache" in r.headers.get("Cache-Control", "")
        page = _get(f"http://127.0.0.1:{port}/?token=secret").read().decode("utf-8")
        assert "/xterm.min.js" in page and "cdn.jsdelivr" not in page
        assert "/addon-canvas.js" in page and "loadAddon" in page   # crisp borders
        assert "/addon-unicode-graphemes.js" not in page
        versioned = re.search(
            r'/addon-saikai-rich-graphemes\.js\?v=([0-9a-f]{12})', page)
        assert versioned is not None
        assert versioned.group(1) == m._static_asset_version(
            "addon-saikai-rich-graphemes.js")
        assert "allowProposedApi:true" in page
        assert "new _RG(richWidthData)" in page
        assert "__RICH_WIDTH_DATA__" not in page
        import html as _html
        import json as _json
        import re as _re
        encoded = _re.search(r"data-rich-width='([^']+)'", page)
        assert encoded is not None
        width_data = _json.loads(_html.unescape(encoded.group(1)))
        assert width_data == m._rich_width_payload()
    finally:
        hub.stop()


def test_rich_width_payload_scans_unicode_in_bounded_blocks():
    """Building browser Unicode tables must not materialize one 1.1M-char
    subject (which peaked above 80 MiB and duplicated under concurrent misses)."""
    import regex

    seen_lengths = []
    original_finditer = regex.finditer

    def recording_finditer(pattern, subject, *args, **kwargs):
        seen_lengths.append(len(subject))
        return original_finditer(pattern, subject, *args, **kwargs)

    m._rich_width_payload.cache_clear()
    regex.finditer = recording_finditer
    try:
        payload = m._rich_width_payload()
    finally:
        regex.finditer = original_finditer
        m._rich_width_payload.cache_clear()

    assert payload["graphemeBreak"]
    for key in (
            "graphemeBreak", "indicConjunctBreak",
            "extendedPictographic"):
        ranges = payload[key]
        assert all(
            left[1] < right[0] for left, right in zip(ranges, ranges[1:])), key
    assert seen_lengths
    assert max(seen_lengths) <= 65536, max(seen_lengths)


def test_rich_width_payload_cold_build_is_single_flight():
    """Concurrent first pages must share one Unicode-table construction."""
    import regex
    import threading
    import time as _time

    callers = set()
    callers_lock = threading.Lock()
    original_finditer = regex.finditer

    def recording_finditer(pattern, subject, *args, **kwargs):
        with callers_lock:
            callers.add(threading.get_ident())
        _time.sleep(0.0005)
        return original_finditer(pattern, subject, *args, **kwargs)

    m._rich_width_payload.cache_clear()
    regex.finditer = recording_finditer
    start = threading.Barrier(5)
    failures = []

    def build():
        try:
            start.wait()
            m._rich_width_payload()
        except Exception as exc:
            failures.append(exc)

    threads = [threading.Thread(target=build) for _ in range(4)]
    for thread in threads:
        thread.start()
    start.wait()
    for thread in threads:
        thread.join(30)
    regex.finditer = original_finditer
    m._rich_width_payload.cache_clear()

    assert not failures
    assert all(not thread.is_alive() for thread in threads)
    assert len(callers) == 1, callers


def test_page_resets_pane_sync_state_at_every_stream_boundary():
    """A partial old-generation DEC 2026 frame must not flush after a reseed."""
    page = m._PAGE_HTML
    assert "function resetPaneSyncState()" in page
    pane_reset = page.split(
        "es.addEventListener('pane-reset'", 1)[1].split("});", 1)[0]
    assert pane_reset.index("resetPaneSyncState();") < pane_reset.index(
        "term.reset();")
    generation = page.split(
        "if (m.gen !== undefined && m.gen !== paneGen)", 1)[1].split(
            "}", 1)[0]
    assert "resetPaneSyncState();" in generation


def test_page_encodes_kitty_flag1_pane_keyboard_input():
    """Pane input must honor the child's active-buffer Kitty keyboard mode."""
    import json
    import shutil
    import subprocess

    page = m._PAGE_HTML
    assert "m.kitty_keyboard" in page
    assert "term.attachCustomKeyEventHandler" in page
    assert "sendRaw(encodeKittyFlag1Data(d, paneKittyKeyboard));" in page

    begin = "// kitty input helpers begin"
    end = "// kitty input helpers end"
    assert begin in page and end in page
    helpers = page.split(begin, 1)[1].split(end, 1)[0]

    node = shutil.which("node")
    if node is None:
        return
    script = r"""
const fs = require('fs');
const ESC = String.fromCharCode(27);
eval(fs.readFileSync(0, 'utf8'));
function ev(key, code, mods = {}) {
  return Object.assign({
    type: 'keydown', key, code, shiftKey: false, altKey: false,
    ctrlKey: false, metaKey: false, isComposing: false, keyCode: 0,
    getModifierState: () => false
  }, mods);
}
const out = [
  kittySequenceForKeyboardEvent(ev('Escape', 'Escape'), 1),
  kittySequenceForKeyboardEvent(
    ev('Escape', 'Escape', {ctrlKey: true}), 1),
  kittySequenceForKeyboardEvent(ev('ArrowUp', 'ArrowUp'), 1),
  kittySequenceForKeyboardEvent(
    ev('ArrowUp', 'ArrowUp', {ctrlKey: true}), 1),
  kittySequenceForKeyboardEvent(
    ev('Home', 'Home', {shiftKey: true}), 1),
  kittySequenceForKeyboardEvent(ev('Delete', 'Delete'), 1),
  kittySequenceForKeyboardEvent(
    ev('PageDown', 'PageDown', {altKey: true}), 1),
  kittySequenceForKeyboardEvent(ev('F1', 'F1'), 1),
  kittySequenceForKeyboardEvent(
    ev('F2', 'F2', {shiftKey: true}), 1),
  kittySequenceForKeyboardEvent(ev('F3', 'F3'), 1),
  kittySequenceForKeyboardEvent(
    ev('F5', 'F5', {ctrlKey: true}), 1),
  kittySequenceForKeyboardEvent(ev('F12', 'F12'), 1),
  kittySequenceForKeyboardEvent(ev('c', 'KeyC', {ctrlKey: true}), 1),
  kittySequenceForKeyboardEvent(
    ev('C', 'KeyC', {ctrlKey: true, shiftKey: true}), 1),
  kittySequenceForKeyboardEvent(ev('[', 'BracketLeft', {altKey: true}), 1),
  kittySequenceForKeyboardEvent(
    ev('Enter', 'Enter', {ctrlKey: true}), 1),
  kittySequenceForKeyboardEvent(ev('A', 'KeyA', {shiftKey: true}), 1),
  kittySequenceForKeyboardEvent(
    ev('c', 'KeyC', {ctrlKey: true, isComposing: true}), 1),
  kittySequenceForKeyboardEvent(ev('@', 'KeyQ', {
    ctrlKey: true, altKey: true,
    getModifierState: name => name === 'AltGraph'
  }), 1),
  kittySequenceForKeyboardEvent(ev('c', 'KeyC', {ctrlKey: true}), 0),
  kittySequenceForKeyboardEvent(ev('ArrowUp', 'ArrowUp'), 0),
  encodeKittyFlag1Data(String.fromCharCode(3), 1),
  encodeKittyFlag1Data(String.fromCharCode(3), 0)
];
process.stdout.write(JSON.stringify(out));
"""
    result = subprocess.run(
        [node, "-e", script],
        input=helpers, check=True, capture_output=True, text=True,
        encoding="utf-8",
    )
    observed = json.loads(result.stdout)
    assert observed == [
        "\x1b[27u",
        "\x1b[27;5u",
        "\x1b[A",
        "\x1b[1;5A",
        "\x1b[1;2H",
        "\x1b[3~",
        "\x1b[6;3~",
        "\x1b[P",
        "\x1b[1;2Q",
        "\x1b[13~",
        "\x1b[15;5~",
        "\x1b[24~",
        "\x1b[99;5u",
        "\x1b[99;6u",
        "\x1b[91;3u",
        "\x1b[13;5u",
        None,
        None,
        None,
        None,
        None,
        "\x1b[99;5u",
        "\x03",
    ]
    import saikai_terminal as terminal
    local_functional = [
        terminal.encode_key(key, None, kitty_flags=1)
        for key in (
            "escape", "ctrl+escape", "up", "ctrl+up", "shift+home",
            "delete", "alt+pagedown", "f1", "shift+f2", "f3",
            "ctrl+f5", "f12",
        )
    ]
    assert observed[:len(local_functional)] == local_functional


def test_pane_seed_carries_and_strips_ordered_kitty_keyboard_mode():
    """A mode-changing reseed must set browser input flags before it is live."""
    import json
    import shutil
    import subprocess
    import pyte

    screen = pyte.Screen(8, 2)
    seed = m._synth_pane_seed(
        screen, 8, 2,
        {
            "alt": False,
            "cursor_hidden": False,
            "cursor_style": 0,
            "kitty_keyboard": 1,
        },
    )
    marker = "\x1b]777;saikai-kitty-keyboard=1\x1b\\"
    assert seed.startswith(marker)

    page = m._PAGE_HTML
    begin = "// pane seed mode helper begin"
    end = "// pane seed mode helper end"
    assert begin in page and end in page
    helper = page.split(begin, 1)[1].split(end, 1)[0]
    pane_reset = page.split(
        "es.addEventListener('pane-reset'", 1)[1].split("});", 1)[0]
    assert pane_reset.index("decodePaneSeedModes") < pane_reset.index(
        "term.reset();")
    generation = page.split(
        "if (m.gen !== undefined && m.gen !== paneGen)", 1)[1].split(
            "}", 1)[0]
    assert "paneKittyKeyboard = 0;" in generation
    assert "hasOwnProperty.call(m, 'kitty_keyboard')" in page

    node = shutil.which("node")
    if node is None:
        return
    script = r"""
const fs = require('fs');
const ESC = String.fromCharCode(27);
eval(fs.readFileSync(0, 'utf8'));
const prefix = ESC + ']777;saikai-kitty-keyboard=';
const st = ESC + String.fromCharCode(92);
const values = [
  prefix + '1' + st + 'PAYLOAD',
  prefix + '0' + st + 'PAYLOAD',
  prefix + '2' + st + 'PAYLOAD',
  prefix + 'x' + st + 'PAYLOAD',
  'PAYLOAD',
  prefix + '1BROKEN'
].map(decodePaneSeedModes);
process.stdout.write(JSON.stringify(values));
"""
    result = subprocess.run(
        [node, "-e", script],
        input=helper, check=True, capture_output=True, text=True,
        encoding="utf-8",
    )
    assert json.loads(result.stdout) == [
        {"data": "PAYLOAD", "kittyKeyboard": 1},
        {"data": "PAYLOAD", "kittyKeyboard": 0},
        {"data": "PAYLOAD", "kittyKeyboard": 0},
        {"data": "PAYLOAD", "kittyKeyboard": 0},
        {"data": "PAYLOAD", "kittyKeyboard": 0},
        {"data": "", "kittyKeyboard": 0},
    ]


def test_vendored_xterm_grapheme_width_matches_rich_when_node_available():
    """Exercise the exact browser parser and Rich-width adapter assets."""
    import json
    import pathlib
    import regex
    import shutil
    import subprocess
    from rich.cells import cell_len

    node = shutil.which("node")
    if node is None:
        return
    root = pathlib.Path(__file__).resolve().parent.parent
    xterm = root / "saikai_mirror_static" / "xterm.min.js"
    addon = root / "saikai_mirror_static" / "addon-saikai-rich-graphemes.js"
    samples = (
        "\U0001fae0",            # Rich 2, stock xterm grapheme provider 1
        "\u2764\u200d\U0001f525",  # Rich 1, stock provider 2
        "\U0001f590\U0001f3fd",  # Rich 1, stock provider 2
        "a\u200dY",              # ZWJ + non-EP must break before Y
        "e\u0301", "\u2764\ufe0f", "1\ufe0f\u20e3",
        "\U0001f468\u200d\U0001f469\u200d\U0001f467\u200d\U0001f466",
        "\U0001f1ef\U0001f1f5", "\U0001f44d\U0001f3fd",
        "\u2764\u200d\u200d\ufe0f",  # skipped ZWJ must not skip following VS16
        "\u200d\u0e33", "\u200d\u200d\u0e33",  # leading-ZWJ skip parity
        "\u0915\u094d\u0924\u094d\u092f",  # one regex EGC, three Rich cells
        "\u1100\u1100",          # one regex EGC, four Rich cells
    )
    width_data = m._rich_width_payload()
    script = r"""
const fs = require('fs');
const { Terminal } = require(process.argv[1]);
const { SaikaiRichGraphemesAddon } = require(process.argv[2]);
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const samples = input.samples;
const widthData = input.widthData;
(async () => {
  const out = [];
  for (const sample of samples) {
    async function observe(writes) {
      const term = new Terminal({
        cols: 40, rows: 2, allowProposedApi: true
      });
      term.loadAddon(new SaikaiRichGraphemesAddon(widthData));
      for (const write of writes) {
        await new Promise(resolve => term.write(write, resolve));
      }
      const line = term.buffer.active.getLine(0);
      let text = '';
      for (let x = 0; x < term.buffer.active.cursorX; ++x) {
        const cell = line.getCell(x);
        if (cell) text += cell.getChars();
      }
      return {
        cursorX: term.buffer.active.cursorX,
        text,
        version: term.unicode.activeVersion
      };
    }
    const input = 'A' + sample + 'B';
    const whole = await observe([input]);
    // SSE frames originate as already-decoded Python str values and each is
    // UTF-8 encoded/base64'd whole, so a frame cannot split inside a Unicode
    // codepoint. One-codepoint writes cover every reachable frame boundary.
    const chunked = await observe(Array.from(input));
    const probe = new Terminal({allowProposedApi: true});
    probe.loadAddon(new SaikaiRichGraphemesAddon(widthData));
    out.push({
      cursorX: whole.cursorX,
      text: whole.text,
      chunkedCursorX: chunked.cursorX,
      chunkedText: chunked.text,
      isolatedWidth: probe._core.unicodeService.getStringCellWidth(sample),
      version: whole.version
    });
  }
  process.stdout.write(JSON.stringify(out));
})().catch(error => {
  console.error(error);
  process.exit(1);
});
"""
    result = subprocess.run(
        [node, "-e", script, str(xterm), str(addon)],
        input=json.dumps(
            {"samples": samples, "widthData": width_data},
            ensure_ascii=True,
            separators=(",", ":"),
        ),
        check=True, capture_output=True, text=True, encoding="utf-8",
    )
    observed = json.loads(result.stdout)
    for sample, row in zip(samples, observed):
        width = sum(cell_len(cluster) for cluster in regex.findall(r"\X", sample))
        assert row == {
            "cursorX": 1 + width + 1,
            "text": "A" + sample + "B",
            "chunkedCursorX": 1 + width + 1,
            "chunkedText": "A" + sample + "B",
            "isolatedWidth": width,
            "version": "saikai-rich-" + width_data["version"],
        }


def test_seed_sgr_preserves_blink_and_strikethrough():
    """A late joiner must receive pyte blink/strike rendition bits as SGR."""
    import pyte

    screen = pyte.Screen(4, 1)
    pyte.Stream(screen).feed("\x1b[5;9mX")
    attrs = m._cell_attrs(screen.buffer[0][0])
    sgr = m._attrs_to_sgr(attrs)
    assert "\x1b[5m" in sgr
    assert "\x1b[9m" in sgr


def test_seed_serializes_osc8_cell_links_and_active_link_state():
    """Late join must preserve both painted OSC8 cells and a still-open link."""
    import json
    import pathlib
    import shutil
    import subprocess
    import pyte

    screen = pyte.Screen(10, 2)
    pyte.Stream(screen).feed("LINK")
    screen._saikai_hyperlinks = {
        (0, column): ("id=painted", "https://example.test/painted")
        for column in range(4)
    }
    screen._saikai_active_hyperlink = (
        "id=active", "https://example.test/active")
    seed = m._synth_pane_seed(
        screen, 10, 2,
        {"alt": False, "cursor_hidden": False, "cursor_style": 0},
    )

    assert "\x1b]8;id=painted;https://example.test/painted\x1b\\" in seed
    assert seed.endswith(
        "\x1b]8;id=active;https://example.test/active\x1b\\")

    node = shutil.which("node")
    if node is None:
        return
    root = pathlib.Path(__file__).resolve().parent.parent
    script = r"""
const fs = require('fs');
const { Terminal } = require(process.argv[1]);
const { SaikaiRichGraphemesAddon } = require(process.argv[2]);
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const term = new Terminal({cols: 10, rows: 2, allowProposedApi: true});
term.loadAddon(new SaikaiRichGraphemesAddon(input.widthData));
term.write(input.seed, () => term.write('Z', () => {
  const line = term.buffer.active.getLine(0);
  process.stdout.write(JSON.stringify(
    [0, 1, 2, 3, 4].map(x => line.getCell(x).extended._urlId)));
}));
"""
    result = subprocess.run(
        [
            node, "-e", script,
            str(root / "saikai_mirror_static" / "xterm.min.js"),
            str(root / "saikai_mirror_static"
                / "addon-saikai-rich-graphemes.js"),
        ],
        input=json.dumps(
            {"seed": seed, "widthData": m._rich_width_payload()},
            ensure_ascii=True, separators=(",", ":"),
        ),
        check=True, capture_output=True, text=True, encoding="utf-8",
    )
    url_ids = json.loads(result.stdout)
    assert url_ids[0] and len(set(url_ids[:4])) == 1
    assert url_ids[4] and url_ids[4] != url_ids[0]


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
             "bracketed_paste": True, "cursor_hidden": False,
             "cursor_style": 5}
    seed = m._synth_pane_seed(src, 20, 5, modes)
    # mode replay: every tracked mode appears explicitly, h or l per the flag
    for want in ("\x1b[?1049h", "\x1b[?1h", "\x1b[?1000h", "\x1b[?1002l",
                 "\x1b[?1003l", "\x1b[?1004l", "\x1b[?1006h",
                 "\x1b[?2004h", "\x1b[5 q", "\x1b[?25h"):
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


def test_pane_seed_preserves_pending_autowrap_and_current_rendition():
    """A reseed at x==columns must not overwrite the edge on the next byte."""
    import pyte

    for edge_text in ("ABCD", "AB\u754c"):
        src = pyte.Screen(4, 2)
        src_stream = pyte.Stream(src)
        src_stream.feed("\x1b[31m" + edge_text)
        assert src.cursor.x == 4

        modes = {
            "alt": False,
            "cursor_hidden": False,
            "cursor_style": 0,
        }
        seed = m._synth_pane_seed(src, 4, 2, modes)
        dst = pyte.Screen(4, 2)
        dst_stream = pyte.Stream(dst)
        dst_stream.feed(seed)
        assert dst.cursor.x == 4, (edge_text, dst.cursor.x)
        assert dst.cursor.attrs.fg == src.cursor.attrs.fg

        src_stream.feed("Z")
        dst_stream.feed("Z")
        assert dst.display == src.display
        assert (dst.cursor.y, dst.cursor.x) == (src.cursor.y, src.cursor.x)
        assert dst.buffer[1][0].fg == src.buffer[1][0].fg == "red"


def test_pane_seed_restores_modes_that_change_future_presentation():
    """Late joiners must process the next PTY bytes exactly like the source."""
    import pyte
    import saikai_terminal as terminal

    cases = (
        (2, 5, "\x1b[?7lABCDE", "Z"),          # DECAWM
        (2, 5, "ABCDE\x1b[1;3H\x1b[4h", "Z"), # IRM
        (4, 5, "\x1b[2;3r\x1b[?6h", "A\r\nB\r\nC"),  # margins + DECOM
    )
    for rows, cols, setup, future in cases:
        src = terminal._HistoryScreenBase(cols, rows, history=10)
        src_stream = pyte.Stream(src)
        src_stream.feed(setup)
        modes = {
            "alt": False,
            "cursor_hidden": False,
            "cursor_style": 0,
        }
        seed = m._synth_pane_seed(src, cols, rows, modes)
        dst = terminal._HistoryScreenBase(cols, rows, history=10)
        dst_stream = pyte.Stream(dst)
        dst_stream.feed(seed)

        src_stream.feed(future)
        dst_stream.feed(future)
        assert terminal._pyte_grid_lines(dst) == terminal._pyte_grid_lines(src), \
            (repr(setup), terminal._pyte_grid_lines(src),
             terminal._pyte_grid_lines(dst))
        assert (dst.cursor.y, dst.cursor.x) == (src.cursor.y, src.cursor.x)


def test_pane_seed_restores_decsc_savepoint_for_future_decrc():
    """DECRC after attach must resume the child's saved cursor and rendition."""
    import pyte

    src = pyte.Screen(12, 4)
    src_stream = pyte.Stream(src)
    src_stream.feed(
        "\x1b[?7l\x1b[2;6H\x1b[31m\x1b(0\x0f\x1b7"
        "\x1b[?7h\x1b[32m\x1b(B\x0f\x1b[HNOW")
    modes = {"alt": False, "cursor_hidden": False, "cursor_style": 0}
    seed = m._synth_pane_seed(src, 12, 4, modes)

    dst = pyte.Screen(12, 4)
    dst_stream = pyte.Stream(dst)
    dst_stream.feed(seed)
    # Current parser state must also survive the temporary state used to create
    # the savepoint. Exercise it before restoring the saved slot.
    for stream in (src_stream, dst_stream):
        stream.feed("q")
    assert dst.display == src.display
    assert dst.cursor.attrs.fg == src.cursor.attrs.fg == "green"
    assert pyte.modes.DECAWM in dst.mode

    for stream in (src_stream, dst_stream):
        stream.feed("\x1b8q")

    assert dst.display == src.display
    assert (dst.cursor.y, dst.cursor.x) == (src.cursor.y, src.cursor.x)
    assert dst.buffer[1][5].fg == "red"


def test_pane_seed_restores_buffer_local_tabstops_for_future_tabs():
    """A custom HTS layout must survive attach for later tab navigation."""
    import pyte

    src = pyte.Screen(12, 4)
    src_stream = pyte.Stream(src)
    src_stream.feed("\x1b[3g\x1b[1;4H\x1bH\x1b[2;1H")
    modes = {"alt": False, "cursor_hidden": False, "cursor_style": 0}
    seed = m._synth_pane_seed(src, 12, 4, modes)

    dst = pyte.Screen(12, 4)
    dst_stream = pyte.Stream(dst)
    dst_stream.feed(seed)
    for stream in (src_stream, dst_stream):
        stream.feed("\tX")

    assert dst.display == src.display
    assert (dst.cursor.y, dst.cursor.x) == (src.cursor.y, src.cursor.x)


def test_pane_seed_restores_active_charset_for_future_text():
    """The current G0/G1 designation is terminal state, not painted text."""
    import pyte

    src = pyte.Screen(12, 4)
    src_stream = pyte.Stream(src)
    src_stream.feed("\x1b(0\x0f")
    modes = {"alt": False, "cursor_hidden": False, "cursor_style": 0}
    seed = m._synth_pane_seed(src, 12, 4, modes)

    dst = pyte.Screen(12, 4)
    dst_stream = pyte.Stream(dst)
    dst_stream.feed(seed)
    for stream in (src_stream, dst_stream):
        stream.feed("q")

    assert dst.display == src.display


def test_pane_seed_restores_lnm_for_future_linefeeds():
    """LNM changes the column where the next LF writes after attach."""
    import pyte

    src = pyte.Screen(12, 4)
    src_stream = pyte.Stream(src)
    src_stream.feed("\x1b[20hABC")
    modes = {"alt": False, "cursor_hidden": False, "cursor_style": 0}
    seed = m._synth_pane_seed(src, 12, 4, modes)

    dst = pyte.Screen(12, 4)
    dst_stream = pyte.Stream(dst)
    dst_stream.feed(seed)
    for stream in (src_stream, dst_stream):
        stream.feed("\nZ")

    assert dst.display == src.display
    assert (dst.cursor.y, dst.cursor.x) == (src.cursor.y, src.cursor.x)


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


def test_raw_xterm_divergence_requests_canonical_reseed():
    """Representational gaps are healed from AgentTerminal's canonical screen.

    xterm advances a detached zero-width EGC that the local screen drops, and
    stores a 3+ cell EGC in multiple cells whose later edits are not atomic.
    Detection belongs on the mirror drain thread, never the pane reader lock.
    """
    import queue as _q

    def observed_hub():
        hub = m.MirrorHub(token="t", cols=12, rows=2)
        cq = _q.Queue(32)
        with hub._clients_lock:
            hub._pane_clients.add(cq)
        requests = []
        hub.set_pane_reseed_request(lambda: requests.append("reseed"))
        return hub, requests

    plain, plain_requests = observed_hub()
    plain._drain_pane_frame(m._PaneData("ordinary ASCII"))
    assert plain_requests == []

    detached, detached_requests = observed_hub()
    detached._drain_pane_frame(
        m._PaneData("A\x1b[31m\u0301"))
    assert detached_requests == ["reseed"]

    split, split_requests = observed_hub()
    split._drain_pane_frame(m._PaneData("\u1100"))
    assert split_requests == []
    split._drain_pane_frame(m._PaneData("\u1100"))
    assert split_requests == ["reseed"]

    seeded, seeded_requests = observed_hub()
    seeded._drain_pane_frame(m._PaneReset(
        "\x1b[2J\x1b[H\u1100\u1100", '{"open": true, "gen": 7}'))
    seeded._drain_pane_frame(m._PaneData("next"))
    assert seeded_requests == ["reseed"]


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
    test_rich_width_payload_scans_unicode_in_bounded_blocks()
    test_rich_width_payload_cold_build_is_single_flight()
    test_page_resets_pane_sync_state_at_every_stream_boundary()
    test_page_encodes_kitty_flag1_pane_keyboard_input()
    test_pane_seed_carries_and_strips_ordered_kitty_keyboard_mode()
    test_vendored_xterm_grapheme_width_matches_rich_when_node_available()
    test_seed_sgr_preserves_blink_and_strikethrough()
    test_seed_serializes_osc8_cell_links_and_active_link_state()
    test_page_injects_terminal_size()
    test_pane_seed_roundtrip_restores_grid_and_modes()
    test_pane_seed_preserves_pending_autowrap_and_current_rendition()
    test_pane_seed_restores_modes_that_change_future_presentation()
    test_pane_seed_restores_decsc_savepoint_for_future_decrc()
    test_pane_seed_restores_buffer_local_tabstops_for_future_tabs()
    test_pane_seed_restores_active_charset_for_future_text()
    test_pane_seed_restores_lnm_for_future_linefeeds()
    test_pane_channel_routes_by_view_and_reseeds_on_fallbehind()
    test_raw_xterm_divergence_requests_canonical_reseed()
    test_raw_endpoint_gates_and_dispatches()
    test_pane_stream_sends_meta_and_reset_seed()
    test_pane_flush_preserves_control_meta_and_sentinel()
    test_pane_reset_carries_meta_and_drain_strips_queries()
    test_pane_feed_gates_on_clients_and_caps_inflight()
    test_pane_strip_holds_split_dcs()
    test_offer_sentinel_reaches_full_queue()
    test_pane_strip_carry_cleared_at_stream_boundary()
    print("OK test_mirror_hub")
