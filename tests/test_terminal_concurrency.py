"""Headless regression tests for ClaudeTerminal threading.

Runs WITHOUT textual/pyte/pywinpty: saikai_terminal soft-imports them (Widget
falls back to object), so ClaudeTerminal can be built via __new__ with just the
fields under test. Run:  python tests/test_terminal_concurrency.py
"""
import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# Headless harness: no terminal to watch, and the watchdog's os._exit on a
# false-positive orphan detection would kill the test process. (production-only)
os.environ["SAIKAI_NO_TERMINAL_WATCHDOG"] = "1"
# Isolate app-launch tests from a developer's ambient SAIKAI_MIRROR (the mirror
# perturbs focus-on-launch in the Pilot harness). (#test-isolation)
os.environ.pop("SAIKAI_MIRROR", None)
import saikai_terminal as rt
import saikai


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
    # Generous timeout: this joins a REAL `taskkill` subprocess, which can take
    # 2-3s even for a nonexistent pid and far longer on a loaded / slow CI runner.
    # The point under test is that the reap is TRACKED + joinable, not its speed.
    rt.join_all_reaps(timeout=30)
    assert not t.is_alive(), "reap not joined by join_all_reaps"


def test_posix_kill_signals_only_and_closes_off_thread():
    """POSIX kill() must NEVER call pty.close()/terminate() on the calling (UI)
    thread. ptyprocess wraps the master fd in io.BufferedRWPair; the reader
    thread blocks in fileobj.read1() HOLDING the buffer's reader lock, and
    fileobj.close() takes that same lock — and ptyprocess.close() signals the
    child only AFTER the fileobj close, so the read never returns: an inline
    close deadlocks the UI forever (the 2026-06 Linux Esc-quit freeze). The UI
    thread may only post signals; the blocking close belongs to the reap thread."""
    sigs = []
    closed_on = []

    class _FakePty:
        def isalive(self):
            return False                      # child died from the signals

        def close(self, force=True):
            closed_on.append(threading.current_thread())

    ct = rt.ClaudeTerminal.__new__(rt.ClaudeTerminal)
    ct._stop = threading.Event()
    ct._pty = _FakePty()
    ct._pid = 4242
    ct.sid = "x"
    caller = threading.current_thread()
    old_win, old_post = rt._IS_WIN, rt._post_signal
    rt._IS_WIN = False
    rt._post_signal = lambda pid, name: sigs.append((pid, name))
    try:
        with rt._REAP_LOCK:
            rt._REAP_THREADS.clear()
        t = ct.kill()
        assert t is not None, "POSIX kill() must return its reap thread"
        assert (4242, "SIGHUP") in sigs and (4242, "SIGTERM") in sigs, sigs
        with rt._REAP_LOCK:
            assert any(x is t for x in rt._REAP_THREADS), "POSIX reap not tracked"
        t.join(timeout=5)
        assert not t.is_alive(), "reap thread hung"
        assert closed_on, "pty.close() never ran"
        assert all(th is not caller for th in closed_on), \
            "DEADLOCK HAZARD: pty.close() ran on the calling (UI) thread"
        assert (4242, "SIGKILL") not in sigs, "dead child must not be SIGKILLed"
        # idempotent: a 2nd kill() must not re-signal a (recycled) PID
        n = len(sigs)
        assert ct.kill() is None and len(sigs) == n
    finally:
        rt._IS_WIN, rt._post_signal = old_win, old_post


def test_posix_reap_escalates_to_sigkill():
    """A child that survives SIGHUP/SIGTERM past the deadline gets SIGKILL from
    the reap thread, and the pty is still closed afterwards."""
    sigs = []
    closed = []

    class _Stubborn:
        def isalive(self):
            return True                       # ignores HUP/TERM

        def close(self, force=True):
            closed.append(True)

    old_post = rt._post_signal
    rt._post_signal = lambda pid, name: sigs.append((pid, name))
    try:
        rt.ClaudeTerminal._reap_posix(_Stubborn(), 99, deadline_s=0.05)
    finally:
        rt._post_signal = old_post
    assert (99, "SIGKILL") in sigs, f"no SIGKILL escalation: {sigs}"
    assert closed, "pty.close() skipped after the escalation"


def test_post_signal_never_raises():
    """_post_signal resolves the signal by NAME (so the POSIX branch stays
    importable/testable on Windows, where SIGHUP doesn't exist) and swallows
    every failure: missing signal, missing pid, nonexistent process."""
    rt._post_signal(None, "SIGHUP")           # no pid → no-op
    rt._post_signal(999999999, "SIGHUP")      # pid > pid_max → ESRCH swallowed
    rt._post_signal(999999999, "NO_SUCH_SIG") # unknown name → no-op


def test_pane_refresh_coalesces():
    """_schedule_pane_refresh queues at most ONE repaint until the UI paints it
    (then re-queues), so a burst of PTY chunks can't flood call_from_thread."""
    ct = rt.ClaudeTerminal.__new__(rt.ClaudeTerminal)
    queued = []
    ct._marshal = lambda fn: queued.append(fn)   # simulate the UI queue (don't run)
    ct.refresh = lambda: None
    ct._sync_terminal_cursor = lambda *a, **k: None  # cursor sync needs a mounted widget (accepts reason=)
    ct._schedule_pane_refresh()
    ct._schedule_pane_refresh()
    ct._schedule_pane_refresh()
    assert len(queued) == 1, f"not coalesced: {len(queued)} marshals"
    queued[0]()                                   # simulate UI running _do_pane_refresh
    ct._schedule_pane_refresh()
    assert len(queued) == 2, "should re-queue a repaint after the UI painted"


class _Cell:
    """Minimal pyte-Char stand-in: _pyte_grid_lines only reads ``.data``."""
    __slots__ = ("data",)

    def __init__(self, data):
        self.data = data


class _FakeScreen:
    """pyte-shaped screen (lines/columns/buffer[y][x].data) for the buffer walk in
    _pyte_grid_lines — keeps this suite pyte-free like the module docstring."""

    def __init__(self, text, title="T"):
        self.title = title
        self.set_text(text)

    def set_text(self, text):
        self.lines = 1
        self.columns = len(text)
        self.buffer = {0: {x: _Cell(ch) for x, ch in enumerate(text)}}


def test_current_screen_caches_by_version():
    """_current_screen reuses the last join until _scr_ver bumps (a feed bumps it),
    so the host poll / render path don't re-join an unchanged screen."""
    ct = rt.ClaudeTerminal.__new__(rt.ClaudeTerminal)
    ct._lock = threading.Lock()
    ct._scr_ver = 5
    ct._cached_ver = -1
    ct._cached_screen = ("", "")

    scr = _FakeScreen("line a")
    ct._screen = scr
    assert ct._current_screen() == ("line a", "T")
    scr.set_text("CHANGED")                               # mutate WITHOUT a version bump
    assert ct._current_screen() == ("line a", "T"), "should serve the cached join"
    ct._scr_ver = 6                                        # a feed bumps the version
    assert ct._current_screen() == ("CHANGED", "T"), "bump → rejoin"


def test_refresh_status_skips_stable_idle_pane():
    """A non-busy pane with no new output (scr_ver unchanged) skips the re-classify;
    a busy pane is always re-checked so it can still flip to idle."""
    ct = rt.ClaudeTerminal.__new__(rt.ClaudeTerminal)
    ct._lock = threading.Lock()
    ct.is_dead = False
    ct._screen = object()
    ct._scr_ver = 3
    ct._last_poll_ver = 3                  # no output since the last poll
    ct._status = "idle"
    calls = []
    ct._current_screen = lambda: (calls.append(1), ("", ""))[1]
    ct._update_status = lambda new: None
    ct.refresh_status()
    assert calls == [], "stable idle pane must skip the screen-join + classify"
    ct._status = "busy"                    # busy must always be re-checked
    ct.refresh_status()
    assert calls == [1], "busy pane must be re-classified to catch the idle flip"


def test_refresh_status_polls_pending_flip_on_static_screen():
    """A non-busy flip mid-debounce must still be re-classified by the poll, so it
    gets its debounce 2nd tick. Regression: the trust-folder gate classifies
    'waiting' once, then claude goes silent (scr_ver stops changing) — a static
    screen used to starve the pending 'waiting' (it never committed, so the pane
    never reached 'Needs input' until something redrew)."""
    ct = rt.ClaudeTerminal.__new__(rt.ClaudeTerminal)
    ct._lock = threading.Lock()
    ct.is_dead = False
    ct._screen = object()
    ct._scr_ver = 3
    ct._last_poll_ver = 3                  # screen unchanged since the last poll
    ct._status = "idle"
    ct._pending_status = "waiting"         # a 'waiting' flip is mid-debounce
    calls = []
    ct._current_screen = lambda: (calls.append(1), ("", ""))[1]
    ct._update_status = lambda new: None
    ct.refresh_status()
    assert calls == [1], "a pending non-busy flip must be re-classified, not skipped"


def test_classify_pty_status_basics():
    """Guard the busy/waiting/idle classifier (and the slice-before-strip tail
    handling) so the per-chunk perf trim didn't change its verdicts."""
    assert rt.classify_pty_status("", "⠀ working") == "busy"      # braille spinner title
    assert rt.classify_pty_status("Do you want to proceed? (y/n)", "") == "waiting"
    assert rt.classify_pty_status("1. one\n2. two\n", "") == "waiting"  # numbered menu
    assert rt.classify_pty_status("just some output", "✳ ready") == "idle"
    # a prompt in the last 2000 chars is still found after slicing the tail first
    assert rt.classify_pty_status("x" * 5000 + "\n(y/n)", "") == "waiting"
    # REGRESSION: a numbered list / prose being STREAMED (title shows the busy
    # spinner) must stay "busy" — the spinner wins over the screen-scrape, else a
    # working pane false-fires "needs input" on essentially every multi-step run.
    assert rt.classify_pty_status("1. one\n2. two\n3. three\n", "⠋ Generating…") == "busy"
    assert rt.classify_pty_status("Would you like to continue?", "⠹ working") == "busy"


def test_alt_screen_suppresses_false_needs_input():
    """claude's alt-screen full-screen UIs (agent switcher, /help) render menu-like
    text that _MENU_RE misfires on — and a user navigating them types into the
    pane. _classify demotes those two cases; the title-spinner 'busy' still wins.
    (#alt-waiting)"""
    import time as _t
    term = rt.AgentTerminal(["agent"], status_classifier=rt.classify_pty_status)
    menu = "1. one\n2. two\n3. three\n"
    term._alt.in_alt = False
    assert term._classify(menu, "") == "waiting"        # normal buffer → menu reads as waiting
    term._alt.in_alt = True
    assert term._classify(menu, "") == "idle"           # bare list on alt → an ANSWER, not a gate
    assert term._classify(menu, "⠋ working") == "busy"  # spinner wins even in alt-screen
    # a non-menu idle screen stays idle regardless of alt-screen
    assert term._classify("just output", "✳ ready") == "idle"
    # the user is DRIVING a TUI (recent keys into the pane): even a ❯-pointed
    # menu must not flag while they navigate it
    term.last_input_ts = _t.monotonic()
    assert term._classify("❯ 1. session-a\n  2. session-b\n", "") == "idle"


def test_real_gates_on_alt_screen_stay_waiting():
    """Current claude (≥2.1) runs its WHOLE session UI on the alt screen, so the
    old blanket alt-demotion silenced every real gate. A ❯-pointed forced choice
    (the resume-from-summary gate — layout captured from a live 2.1.201 probe,
    2026-07-16) and a permission prompt must classify 'waiting' on the alt screen
    when the user has NOT been typing into the pane. (#resume-gate-waiting)"""
    term = rt.AgentTerminal(["agent"], status_classifier=rt.classify_pty_status)
    term._alt.in_alt = True
    term.last_input_ts = 0.0
    resume_gate = (
        "  This session is 13d 1h old and 273k tokens.\n\n"
        "  Resuming the full session will consume a substantial portion of your"
        " usage limits. We recommend\n  resuming from a summary.\n\n"
        "  ❯ 1. Resume from summary (recommended)\n"
        "    2. Resume full session as-is\n"
        "    3. Don't ask me again\n\n"
        "  Enter to confirm · Esc to cancel\n")
    assert term._classify(resume_gate, "✳ title") == "waiting"
    perm = "Do you want to proceed? (y/n)\n"
    assert term._classify(perm, "") == "waiting"


def test_classify_trust_folder_dialog_is_waiting():
    """The startup 'trust this folder?' gate blocks the session on the human, but
    it renders at the TOP of the screen (rest blank) so it sits OUTSIDE the tail
    window the other prompt checks use, and its footer ('Enter to confirm · Esc to
    cancel') lacks the 'Press' the _WAITING_RE patterns want. classify must still
    flag it 'waiting' (-> the needs-input toast + list marker). Layout captured
    from a real claude 2.1.178 startup in an untrusted folder."""
    dialog = (
        " Accessing workspace:\n\n C:\\Users\\me\\AppData\\Local\\Temp\\foo\n\n"
        " Quick safety check: Is this a project you created or one you trust?\n\n"
        " Claude Code'll be able to read, edit, and execute files here.\n\n"
        " Security guide\n\n"
        " ❯ 1. Yes, I trust this folder\n   2. No, exit\n\n"
        " Enter to confirm · Esc to cancel\n"
    )
    # ~22 blank 140-col rows below fill the tail window the other checks look at,
    # so the dialog is only reachable by the full-screen trust scan.
    screen = dialog + "\n".join([" " * 140] * 22)
    assert rt.classify_pty_status(screen, "claude") == "waiting"
    # A braille-spinner title still WINS — never flag a streaming pane that merely
    # printed "trust this folder" somewhere in its output.
    assert rt.classify_pty_status(screen, "⠇ working") == "busy"


def test_status_classifier_profiles_and_injection():
    generic = rt.classifier_for_profile("generic")
    assert generic is rt.classify_generic_status
    assert rt.classifier_for_profile("claude") is rt.classify_pty_status
    assert generic("", "⠋ generating") == "idle"  # generic agents cannot trust Claude OSC
    assert generic("Do you want to proceed? (y/n)", "⠋ generating") == "waiting"
    try:
        rt.classifier_for_profile("unknown")
    except ValueError:
        pass
    else:
        raise AssertionError("unknown classifier profile must fail")

    marker = lambda screen, title: "waiting"
    term = rt.AgentTerminal(["agent"], status_classifier=marker)
    assert term._status_classifier is marker
    assert rt.ClaudeTerminal is rt.AgentTerminal  # compatibility alias


def test_show_hw_cursor_native_cursor_dec_bytes():
    """#native-cursor: on Windows the pane shows the terminal's NATIVE cursor via
    \\x1b[?25h on focus / ?25l on blur (instead of saikai's wide reverse block);
    elsewhere it's a no-op. Must never raise headless (no mounted app).
    The native-cursor / anchor machinery is opt-in (SAIKAI_IME_ANCHOR); enable it
    for this test since it verifies that machinery's byte output. (#ime-anchor-optout)"""
    _saved = rt._IME_ANCHOR
    rt._IME_ANCHOR = True
    try:
        bare = rt.AgentTerminal.__new__(rt.AgentTerminal)
        bare.sid = "x"
        bare._show_hw_cursor(True)     # no app context → swallowed, no raise
        bare._show_hw_cursor(False)

        writes = []
        class _Drv:
            def write(self, s): writes.append(s)
        class _Shim(rt.AgentTerminal):
            app = property(lambda self: type("A", (), {"_driver": _Drv()})())
        t = _Shim.__new__(_Shim)
        t.sid = "y"
        t._show_hw_cursor(True)
        t._show_hw_cursor(False)
        if rt._IS_WIN:
            assert writes == ["\x1b[?25h", "\x1b[?25l"]
        else:
            assert writes == []
    finally:
        rt._IME_ANCHOR = _saved


def test_native_cursor_follows_dectcem_regardless_of_screen():
    """The native cursor / IME anchor follows the child's DECTCEM state, screen-
    agnostic: SHOW when the child shows its cursor (?25h), HIDE when it hides it (?25l).

    claude's agent / fullscreen renderer runs on the ALT screen while keeping its
    prompt cursor VISIBLE, and still needs the IME there — so alt+visible must anchor
    (gating on alt-screen alone, the old policy, sent composition to the pane top-left).
    Conversely a main-screen program that hides its cursor for a progress spinner must
    NOT have saikai force a cursor back on. cursor_hidden is the signal for both.
    (#agents-cursor)"""
    import threading as _th

    class _Cursor:
        def __init__(self, hidden=False):
            self.x = 3
            self.y = 2
            self.hidden = hidden

    class _Screen:
        def __init__(self, hidden=False):
            self.cursor = _Cursor(hidden)

    class _Alt:
        def __init__(self, in_alt=False):
            self.in_alt = in_alt

    class _Region:
        x = 40
        y = 5
        width = 80
        height = 24

    class _Drv:
        def __init__(self, writes):
            self._writes = writes
        def write(self, s):
            self._writes.append(s)

    class _App:
        def __init__(self, writes):
            self._driver = _Drv(writes)

    class _Shim(rt.AgentTerminal):
        app = property(lambda self: self._app)
        content_region = property(lambda self: _Region())

    def _term(hidden=False, in_alt=False):
        writes = []
        t = _Shim.__new__(_Shim)
        t.sid = "x"
        t._app = _App(writes)
        t._lock = _th.Lock()
        t._screen = _Screen(hidden)
        t._alt = _Alt(in_alt)
        t._scroll = 0
        t.is_dead = False
        t._hw_cursor_visible = None
        t._is_focused_pane = lambda: True
        t.refresh = lambda *a, **k: None
        return t, writes

    old_win, old_anchor, old_offset = rt._IS_WIN, rt._IME_ANCHOR, rt.Offset
    rt._IS_WIN = True
    rt._IME_ANCHOR = True
    if rt.Offset is None:
        rt.Offset = lambda x, y: (x, y)
    try:
        # (cursor_hidden, in_alt_screen): follow cursor_hidden, ignore the screen.
        assert rt._native_cursor_should_show(False, False) is True   # main, cursor shown
        assert rt._native_cursor_should_show(True, False) is False   # main, cursor hidden (spinner) -> respect ?25l
        assert rt._native_cursor_should_show(False, True) is True    # claude agent mode: alt + cursor shown
        assert rt._native_cursor_should_show(True, True) is False    # fullscreen TUI, no insertion point

        # main-screen program that hid its cursor -> saikai must NOT force it back on.
        t, writes = _term(hidden=True)
        t._sync_terminal_cursor()
        assert writes == ["\x1b[?25l"], writes

        # alt-screen but cursor visible (claude agent mode) -> anchors at the prompt.
        t, writes = _term(in_alt=True)
        t._sync_terminal_cursor()
        assert writes == ["\x1b[?25h"], writes
        assert t._app.cursor_position == rt.Offset(43, 7)

        # alt-screen AND cursor hidden (real fullscreen UI) -> hand the cursor back.
        t, writes = _term(hidden=True, in_alt=True)
        t._sync_terminal_cursor()
        assert writes == ["\x1b[?25l"], writes

        t, writes = _term()
        t._sync_terminal_cursor()
        t._sync_terminal_cursor()
        assert writes == ["\x1b[?25h"], writes
        assert t._app.cursor_position == rt.Offset(43, 7)
    finally:
        rt._IS_WIN, rt._IME_ANCHOR, rt.Offset = old_win, old_anchor, old_offset


def test_child_pty_env_hides_outer_terminal_identity_from_child():
    """The pane child renders into saikai, not directly into Windows Terminal.

    Claude Code enables WT/host-specific redraw paths from WT_SESSION and related
    probes; those are correct for a direct terminal, but wrong behind saikai's
    pyte renderer. The advertised contract is generic xterm truecolor.
    (#agents-cursor)"""
    env = rt._child_pty_env({
        "PATH": "/bin",
        "TERM": "xterm-kitty",
        "COLORTERM": "24bit",
        "WT_SESSION": "outer-wt",
        "TERM_PROGRAM": "WezTerm",
        "TERM_PROGRAM_VERSION": "999",
        "KITTY_WINDOW_ID": "1",
        "CLAUDE_CODE_ALT_SCREEN_FULL_REPAINT": "1",
        "CLAUDE_CODE_FORCE_SYNC_OUTPUT": "1",
    })
    assert env["PATH"] == "/bin"
    assert env["TERM"] == "xterm-256color"
    assert env["COLORTERM"] == "truecolor"
    assert "WT_SESSION" not in env
    assert "TERM_PROGRAM" not in env
    assert "TERM_PROGRAM_VERSION" not in env
    assert "KITTY_WINDOW_ID" not in env
    assert "CLAUDE_CODE_ALT_SCREEN_FULL_REPAINT" not in env
    # Explicit user/developer override remains explicit; only host identity leaks
    # and Claude's derived WT full-repaint flag are scrubbed.
    assert env["CLAUDE_CODE_FORCE_SYNC_OUTPUT"] == "1"


def test_cursor_sync_freezes_while_busy_and_settles_on_transition():
    """Anti-fly WITHOUT a timer: a per-repaint sync FREEZES the anchor while the pane
    is 'busy' (an agent storm moves the pyte cursor every frame, so moving the anchor
    then makes the IME window fly); leaving 'busy' marshals a 'settle' sync that lands
    the anchor on the resting prompt and forces one repaint to flush it. (#agents-cursor)"""
    import threading as _th

    class _Cursor:
        x = 3; y = 2; hidden = False

    class _Screen:
        def __init__(self):
            self.cursor = _Cursor(); self.columns = 80; self.lines = 24

    class _Alt:
        in_alt = False

    class _Region:
        x = 40; y = 5; width = 80; height = 24

    class _Drv:
        def __init__(self, w): self._w = w
        def write(self, s): self._w.append(s)

    class _App:
        def __init__(self, w): self._driver = _Drv(w)

    class _Shim(rt.AgentTerminal):
        app = property(lambda self: self._app)
        content_region = property(lambda self: _Region())

    old_win, old_anchor, old_offset = rt._IS_WIN, rt._IME_ANCHOR, rt.Offset
    rt._IS_WIN = True
    rt._IME_ANCHOR = True
    if rt.Offset is None:
        rt.Offset = lambda x, y: (x, y)
    try:
        writes, refreshes = [], []
        t = _Shim.__new__(_Shim)
        t.sid = "x"
        t._app = _App(writes)
        t._lock = _th.Lock()
        t._screen = _Screen()
        t._alt = _Alt()
        t._scroll = 0
        t.is_dead = False
        t._hw_cursor_visible = None
        t._anchored_xy = None
        t._is_focused_pane = lambda: True
        t.refresh = lambda *a, **k: refreshes.append(k.get("repaint", False))

        # 1) repaint sync while BUSY: anchor frozen (never moved).
        t._status = "busy"
        t._sync_terminal_cursor(reason="repaint")
        assert getattr(t._app, "cursor_position", None) is None
        assert t._anchored_xy is None

        # 2) settle sync (busy just ended): anchors at the prompt + forces a flush.
        t._status = "idle"
        t._sync_terminal_cursor(reason="settle")
        assert t._app.cursor_position == rt.Offset(43, 7)
        assert t._anchored_xy == (43, 7)
        assert True in refreshes, refreshes          # moved -> forced repaint to flush
        assert writes == ["\x1b[?25h"], writes

        # 3) a repaint sync now (still idle, unchanged cell): no extra flush (no loop).
        refreshes.clear()
        t._sync_terminal_cursor(reason="repaint")
        assert refreshes == [], refreshes

        # 4) _update_status leaving 'busy' marshals a 'settle' sync.
        marshalled, reasons = [], []
        t._marshal = lambda fn: marshalled.append(fn)
        t._on_status = lambda sid, st: None
        t._status = "busy"; t._pending_status = None; t._pending_ticks = 0
        t._update_status("idle")   # 1st tick: pending
        t._update_status("idle")   # 2nd tick: flips out of busy -> fire
        t._sync_terminal_cursor = lambda reason="repaint": reasons.append(reason)
        for fn in marshalled:
            try:
                fn()
            except Exception:
                pass
        assert "settle" in reasons, reasons
    finally:
        rt._IS_WIN, rt._IME_ANCHOR, rt.Offset = old_win, old_anchor, old_offset


def test_autoscroll_tick_pins_anchor_to_content():
    """#drag-autoscroll: while edge-dragging, _autoscroll_tick scrolls one line and
    shifts the anchor by the SAME delta so it stays pinned to its text (the visible
    row for a fixed line is hist-scroll+y, so scroll+Δ ⇒ row+Δ). The head rides the
    edge, and it's a no-op once the scrollback limit / live bottom is hit."""
    import threading as _th

    class _Hist:
        def __init__(self, n): self.top = list(range(n))

    class _Scr:
        def __init__(self, lines, histn): self.lines = lines; self.history = _Hist(histn)

    t = rt.AgentTerminal.__new__(rt.AgentTerminal)
    t._lock = _th.Lock()
    t._screen = _Scr(lines=30, histn=100)
    t.refresh = lambda *a, **k: None
    t._scroll = 5
    t._sel_anchor, t._sel_head = (10, 2), (20, 8)

    # scroll UP (reveal older lines): scroll 5→6, anchor row +1, head → top row 0
    t._autoscroll_dir = 1
    t._autoscroll_tick()
    assert t._scroll == 6 and t._sel_anchor == (11, 2) and t._sel_head == (0, 8)

    # scroll DOWN (toward live): scroll 6→5, anchor row -1, head → bottom row lines-1
    t._autoscroll_dir = -1
    t._autoscroll_tick()
    assert t._scroll == 5 and t._sel_anchor == (10, 2) and t._sel_head == (29, 8)

    # at the live bottom (scroll 0) scrolling down is a no-op (anchor unchanged)
    t._scroll, t._sel_anchor = 0, (10, 2)
    t._autoscroll_dir = -1
    t._autoscroll_tick()
    assert t._scroll == 0 and t._sel_anchor == (10, 2)

    # dir 0 (pointer not at an edge) does nothing
    t._scroll, t._sel_anchor, t._autoscroll_dir = 4, (10, 2), 0
    t._autoscroll_tick()
    assert t._scroll == 4 and t._sel_anchor == (10, 2)


def test_encode_key_meta_and_release():
    """readline keys reach claude: Ctrl+letters AND Meta/Alt word-ops (ESC prefix).
    The release key must resolve to Textual's real name, not the dead 'ctrl+]'."""
    assert rt.encode_key("alt+b", None) == "\x1bb"          # backward-word
    assert rt.encode_key("alt+f", None) == "\x1bf"          # forward-word
    assert rt.encode_key("alt+d", None) == "\x1bd"          # kill-word
    assert rt.encode_key("alt+backspace", None) == "\x1b\x7f"  # backward-kill-word
    assert rt.encode_key("ctrl+w", None) == "\x17"          # word-delete still forwards
    assert rt.encode_key("ctrl+a", None) == "\x01"
    assert rt.encode_key("alt+left", None) == "\x1b[1;3D"
    assert rt.encode_key("ctrl+right", None) == "\x1b[1;5C"
    assert rt.encode_key("ctrl+shift+up", None) == "\x1b[1;6A"
    assert rt.encode_key("shift+delete", None) == "\x1b[3;2~"
    # Modified Enter (newline-in-prompt gesture) must NOT be silently dropped:
    # emit the CSI-u (kitty) form claude negotiates. mod = 1+shift+2*alt+4*ctrl.
    assert rt.encode_key("shift+enter", None) == "\x1b[13;2u"
    assert rt.encode_key("alt+enter", None) == "\x1b[13;3u"
    assert rt.encode_key("ctrl+enter", None) == "\x1b[13;5u"
    assert rt._normalize_key("ctrl+]") == "ctrl+right_square_bracket"
    if not os.environ.get("SAIKAI_RELEASE_KEY"):
        assert rt.RELEASE_FOCUS_KEY == "ctrl+right_square_bracket"


def test_configure_release_focus_key_restores_old_key():
    old = rt.RELEASE_FOCUS_KEY
    try:
        assert rt.configure_release_focus_key("ctrl+g") == "ctrl+g"
        assert rt.encode_key("ctrl+g", None) is None
        assert rt.encode_key("ctrl+right_square_bracket", None) == "\x1d"
    finally:
        rt.configure_release_focus_key(old)


def test_copy_text_uses_pbcopy_on_macos_before_osc52():
    calls = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))

    old_platform = rt.sys.platform
    old_run = rt.subprocess.run
    # set_clipboard_macos declines over SSH (so OSC-52 can target the client), so
    # this darwin-path test must run as if local — otherwise it fails spuriously
    # when the suite itself is invoked over SSH (the CI/dev-on-Pi case).
    old_ssh = {k: os.environ.pop(k, None) for k in ("SSH_CONNECTION", "SSH_TTY", "SSH_CLIENT")}
    term = rt.AgentTerminal.__new__(rt.AgentTerminal)
    try:
        rt.sys.platform = "darwin"
        rt.subprocess.run = fake_run
        term._copy_text("日本語")
    finally:
        rt.sys.platform = old_platform
        rt.subprocess.run = old_run
        for k, v in old_ssh.items():
            if v is not None:
                os.environ[k] = v
    assert calls and calls[0][0] == ["pbcopy"], calls
    assert calls[0][1]["input"] == "日本語".encode("utf-8")


def test_set_clipboard_macos_skips_remote_sessions():
    calls = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))

    old_run = rt.subprocess.run
    # Clear EVERY SSH marker set_clipboard_macos consults (not just SSH_TTY) so the
    # 'local' leg is genuinely local even when the suite is invoked over SSH, where
    # the ambient SSH_CONNECTION/SSH_CLIENT would otherwise force the remote path.
    old_ssh = {k: os.environ.pop(k, None) for k in ("SSH_CONNECTION", "SSH_TTY", "SSH_CLIENT")}
    try:
        rt.subprocess.run = fake_run
        assert rt.set_clipboard_macos("local") is True
        os.environ["SSH_TTY"] = "/dev/pts/1"
        assert rt.set_clipboard_macos("remote") is False
    finally:
        rt.subprocess.run = old_run
        os.environ.pop("SSH_TTY", None)
        for k, v in old_ssh.items():
            if v is not None:
                os.environ[k] = v
    assert len(calls) == 1 and calls[0][0] == ["pbcopy"], calls


def test_copy_text_skips_pbcopy_on_macos_over_ssh():
    calls = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))

    old_platform = rt.sys.platform
    old_run = rt.subprocess.run
    old_ssh = os.environ.get("SSH_CONNECTION")
    term = rt.AgentTerminal.__new__(rt.AgentTerminal)
    try:
        rt.sys.platform = "darwin"
        rt.subprocess.run = fake_run
        os.environ["SSH_CONNECTION"] = "client 1 server 2"
        term._copy_text("remote")
    finally:
        rt.sys.platform = old_platform
        rt.subprocess.run = old_run
        if old_ssh is None:
            os.environ.pop("SSH_CONNECTION", None)
        else:
            os.environ["SSH_CONNECTION"] = old_ssh
    assert not calls, calls


def test_set_status_ignores_forgotten_sid():
    """A status callback that lands AFTER the pane was closed must not resurrect a
    ghost entry in the manager's status dict (which statuses() reports as a stale
    marker / false 'needs input' toast / phantom Esc-close target)."""
    mgr = rt.LiveSessionManager.__new__(rt.LiveSessionManager)
    mgr._terms = {"sidA": object()}     # a registered (live) pane
    mgr._status = {}
    mgr.set_status("sidA", "busy")
    assert mgr.statuses() == {"sidA": "busy"}
    mgr._terms.pop("sidA")              # mimic forget() popping _terms + _status
    mgr._status.pop("sidA", None)
    mgr.set_status("sidA", "idle")      # a late callback for the forgotten sid
    assert "sidA" not in mgr.statuses(), "forgotten sid resurrected as a ghost"


def test_rekey_moves_term_status_and_pane_id():
    """After /clear the SAME live pane becomes the CHILD session, so the manager
    must re-key it parent->child: the term, the status, AND the TabPane DOM id
    string all move under the new sid. The pane_id must stay the ORIGINAL
    'tab-live-{parent}' (Textual sets a TabPane's DOM id at mount and it cannot
    change at runtime — the pane keeps its id but is now found under the child),
    while an UNREGISTERED sid still falls back to the 'tab-live-{sid}' default."""
    mgr = rt.LiveSessionManager()
    term = object()
    mgr.register("parent", term)
    assert mgr.pane_id("parent") == "tab-live-parent"
    mgr.set_status("parent", "idle")

    mgr.rekey("parent", "child")
    assert mgr.has("child") and not mgr.has("parent"), "term not moved parent->child"
    assert mgr.get("child") is term, "the SAME term must follow the child sid"
    assert mgr.status("child") == "idle" and mgr.status("parent") == "", "status not moved"
    # The TabPane's DOM id can't change at runtime: the child REUSES the parent's
    # existing 'tab-live-parent' id, just looked up under the child sid now.
    assert mgr.pane_id("child") == "tab-live-parent", "pane_id string must follow the re-key"
    # An unregistered sid still derives the deterministic default.
    assert mgr.pane_id("never-seen") == "tab-live-never-seen", "default pane_id broke"
    # No-ops: same sid, or an absent old sid, must not raise or fabricate entries.
    mgr.rekey("child", "child")
    assert mgr.has("child") and mgr.pane_id("child") == "tab-live-parent"
    mgr.rekey("ghost", "ghost2")
    assert not mgr.has("ghost2")


def test_note_reap_prunes_finished_threads():
    """note_reap drops already-finished reaps so _reaps can't grow unbounded over
    open/close pane churn — while still tracking in-flight ones. This does NOT
    weaken reaping: join_reaps only needs to wait on STILL-RUNNING reaps, and the
    module-level _REAP_THREADS (atexit) awaits every reap at process exit."""
    mgr = rt.LiveSessionManager.__new__(rt.LiveSessionManager)
    mgr._reaps = []
    for _ in range(3):                       # three already-finished reaps
        d = threading.Thread(target=lambda: None)
        d.start(); d.join()
        mgr.note_reap(d)
    # each append prunes the prior finished ones -> at most 1 dead thread retained
    assert len([t for t in mgr._reaps if not t.is_alive()]) <= 1, mgr._reaps
    ev = threading.Event()
    live = threading.Thread(target=ev.wait)
    live.start()
    mgr.note_reap(live)                      # prunes the dead, keeps the live one
    assert live in mgr._reaps
    assert all(t is live or not t.is_alive() for t in mgr._reaps)
    ev.set(); live.join()


def test_kitty_keyboard_csi_u_is_scrubbed():
    """pyte leaks the trailing 'u' of the Kitty keyboard protocol's CSI-u
    push/pop into the grid (so a kanji being edited appears to gain a stray 'u').
    The pre-pyte scrub drops CSI >/</=/? … u, but NOT plain CSI u (SCO
    restore-cursor, which carries no private marker)."""
    sub = rt._KITTY_KBD_RE.sub
    assert sub("", "\x1b[>1u漢字\x1b[<u") == "漢字"      # push + pop stripped
    assert sub("", "\x1b[<u") == ""                       # pop alone
    assert sub("", "\x1b[=1;2u") == ""                    # set
    assert sub("", "\x1b[?u") == ""                       # query
    assert sub("", "\x1b[u") == "\x1b[u"                  # SCO restore: PRESERVED
    assert sub("", "\x1b[1u") == "\x1b[1u"                # numeric, no marker: PRESERVED


def test_selection_geometry_in_sel():
    """saikai-owned drag-selection geometry: single row = a column span; multi-row
    = anchor-col→end, full middle rows, 0→head-col on the last. Direction-agnostic."""
    ct = rt.ClaudeTerminal.__new__(rt.ClaudeTerminal)
    ct._sel_anchor, ct._sel_head = (2, 3), (2, 7)
    assert ct._in_sel(2, 3) and ct._in_sel(2, 7) and ct._in_sel(2, 5)
    assert not ct._in_sel(2, 2) and not ct._in_sel(2, 8) and not ct._in_sel(1, 5)
    ct._sel_anchor, ct._sel_head = (2, 7), (2, 3)        # reversed = same span
    assert ct._in_sel(2, 5) and not ct._in_sel(2, 2)
    ct._sel_anchor, ct._sel_head = (1, 4), (3, 2)        # multi-row
    assert ct._in_sel(1, 4) and ct._in_sel(1, 99) and not ct._in_sel(1, 3)
    assert ct._in_sel(2, 0) and ct._in_sel(2, 99)        # middle: full
    assert ct._in_sel(3, 0) and ct._in_sel(3, 2) and not ct._in_sel(3, 3)
    assert not ct._in_sel(0, 5) and not ct._in_sel(4, 0)
    ct._sel_anchor = ct._sel_head = None
    assert not ct._in_sel(2, 5)


def test_extract_selection_slices_and_joins():
    """Extraction slices each display row by the selection range, drops wide-char
    stubs ('') and trailing blanks, and joins rows with newlines."""
    ct = rt.ClaudeTerminal.__new__(rt.ClaudeTerminal)
    ct._lock = threading.Lock()
    ct._scroll = 0

    class _C:
        def __init__(self, d):
            self.data = d

    class _Scr:
        columns = 13
        history = type("H", (), {"top": []})()
        buffer = {0: {i: _C(c) for i, c in enumerate("hello world  ")}}

    ct._screen = _Scr()
    ct._sel_anchor, ct._sel_head = (0, 0), (0, 4)
    assert ct._extract_selection() == "hello"
    ct._sel_anchor, ct._sel_head = (0, 6), (0, 12)        # to the line end, blanks stripped
    assert ct._extract_selection() == "world"


def test_frozen_pane_copy_uses_snapshot_not_live_buffer():
    """Regression: copying from a FROZEN streaming pane must return the displayed
    frame, not whatever the reader scrolled into screen.buffer afterwards. Freeze
    pins the visible rows (_snapshot_frozen); the live buffer then mutates; extract
    reads the snapshot. Un-freeze drops it and reads live again."""
    ct = rt.ClaudeTerminal.__new__(rt.ClaudeTerminal)
    ct._lock = threading.Lock()
    ct._scroll = 0
    ct._frozen = False
    ct._frozen_buf = None

    class _C:
        def __init__(self, d):
            self.data = d

    class _Scr:
        columns = 5
        lines = 1
        history = type("H", (), {"top": []})()
        buffer = {0: {i: _C(c) for i, c in enumerate("hello")}}

    ct._screen = _Scr()
    ct._frozen = True
    ct._snapshot_frozen()                                   # pin the displayed "hello"
    ct._screen.buffer[0] = {i: _C(c) for i, c in enumerate("WORLD")}   # reader mutates live
    ct._sel_anchor, ct._sel_head = (0, 0), (0, 4)
    assert ct._extract_selection() == "hello"               # copies the FROZEN frame
    ct._frozen = False
    ct._frozen_buf = None
    assert ct._extract_selection() == "WORLD"               # live again after resume


def test_toggle_freeze_flips_and_resumes():
    """Shift+F9 freeze pauses per-chunk repaints so a streaming pane can be
    Shift+drag-selected; resuming repaints once to catch up to buffered output."""
    ct = rt.ClaudeTerminal.__new__(rt.ClaudeTerminal)
    ct._frozen = False
    refreshed = []
    ct.refresh = lambda: refreshed.append(1)
    assert ct.toggle_freeze() is True and ct._frozen is True    # freeze
    assert refreshed == []                                       # no catch-up on freeze
    assert ct.toggle_freeze() is False and ct._frozen is False   # resume
    assert refreshed == [1]                                      # one catch-up repaint


def test_bracketed_paste_mode_tracking():
    """saikai re-wraps pastes in \\x1b[200~ … \\x1b[201~ only when claude has
    enabled bracketed-paste mode; the mode is tracked from CSI ?2004 h/l in the
    output stream (pyte doesn't expose it). Last h/l in a chunk wins."""
    fa = rt._BRACKETED_RE.findall
    assert fa("\x1b[?2004h") == ["h"]
    assert fa("\x1b[?2004l") == ["l"]
    assert fa("x\x1b[?2004h y \x1b[?2004l") == ["h", "l"]
    assert fa("no paste mode here") == []


def test_ime_anchor_xy_maps_cursor_into_region():
    """The IME/terminal-cursor anchor maps claude's grid cursor to an absolute
    screen cell inside the pane's content region (so WezTerm's composition popup
    lands at the claude prompt, not the search box). Clamps to the region; None for
    an empty region."""
    f = rt._ime_anchor_xy
    assert f(3, 2, 40, 5, 80, 24) == (43, 7)        # region origin + cursor
    assert f(0, 0, 40, 5, 80, 24) == (40, 5)        # top-left of the region
    assert f(100, 50, 40, 5, 80, 24) == (119, 28)   # clamped to last col/row (40+79, 5+23)
    assert f(-1, -1, 40, 5, 80, 24) == (40, 5)      # negative cursor clamped to 0
    assert f(5, 5, 0, 0, 0, 0) is None              # empty region → no anchor


def test_reopen_after_exit_requires_awaited_pane_removal():
    """Re-opening an EXITED session must not hit Textual DuplicateIds. saikai keeps a
    dead pane mounted (for its final frame) and re-uses the sid's pane id on reopen;
    TabbedContent.remove_pane() is DEFERRED (returns AwaitComplete), so a synchronous
    remove_pane()+add_pane(same id) collides. This proves the mechanism behind saikai's
    _mount_live_pane worker: NOT awaiting the removal raises DuplicateIds; awaiting it
    mounts cleanly. Needs textual (skips without it — the bug was the silent
    'won't reopen' for every session whose claude had exited)."""
    try:
        import asyncio
        from textual.app import App
        from textual.widgets import TabbedContent, TabPane, Label
    except Exception:
        print("SKIP test_reopen_after_exit_requires_awaited_pane_removal (no textual)")
        return

    class _A(App):
        def compose(self):
            yield TabbedContent(id="tc")

    async def _run(awaited):
        app = _A()
        async with app.run_test() as pilot:
            tc = app.query_one("#tc", TabbedContent)
            await tc.add_pane(TabPane("first", Label("a"), id="tab-live-x"))
            await pilot.pause()
            raised = None
            try:
                if awaited:                       # saikai's _mount_live_pane fix
                    await tc.remove_pane("tab-live-x")
                    await tc.add_pane(TabPane("second", Label("b"), id="tab-live-x"))
                else:                             # the old buggy synchronous path
                    tc.remove_pane("tab-live-x")
                    tc.add_pane(TabPane("second", Label("b"), id="tab-live-x"))
                await pilot.pause()
            except Exception as e:                # noqa: BLE001
                raised = type(e).__name__
            return raised

    async def _both():
        return (await _run(awaited=False), await _run(awaited=True))

    sync_raise, awaited_raise = asyncio.run(_both())
    assert sync_raise == "DuplicateIds", f"sync remove+add should collide, got {sync_raise}"
    assert awaited_raise is None, f"awaited remove+add must mount cleanly, got {awaited_raise}"


def test_agent_terminal_on_key_release_encode_and_dead():
    """Stage-2 routing TARGET: a Key event the App forwards to a focused live pane
    is handled by AgentTerminal.on_key EXACTLY like the host terminal -- the
    release key (Ctrl+]) hands focus back (FocusReleased) and writes nothing to
    claude; any other key encodes to the child PTY; a dead pane writes nothing
    (keys bubble to the host's bindings). This is what makes the unified browser
    input path terminal-equivalent INSIDE a pane (Ctrl+] to leave, Ctrl+C to
    interrupt)."""
    writes = []
    posted = []

    class _FakePty:
        def write(self, d):
            writes.append(d)

    class _Ev:
        def __init__(self, key, character=None):
            self.key = key
            self.character = character
            self.stopped = False

        def stop(self):
            self.stopped = True

    t = rt.AgentTerminal.__new__(rt.AgentTerminal)
    t._pty = _FakePty()
    t.is_dead = False
    t._frozen = False
    t._lock = threading.Lock()
    t._scroll = 0
    t.post_message = lambda m: posted.append(m)

    # Release key -> hand focus back to the list; nothing written to claude.
    t.on_key(_Ev(rt.RELEASE_FOCUS_KEY))
    assert any(isinstance(m, rt.AgentTerminal.FocusReleased) for m in posted), posted
    assert writes == [], writes

    # A normal printable key -> encoded bytes to the child PTY (claude).
    posted.clear()
    t.on_key(_Ev("a", "a"))
    assert writes == ["a"], writes

    # Ctrl-C -> encoded to the PTY (interrupts claude), NOT bubbled to the host.
    writes.clear()
    ev = _Ev("ctrl+c", "\x03")
    t.on_key(ev)
    assert writes == ["\x03"] and ev.stopped, (writes, ev.stopped)

    # Dead pane -> nothing written; the key bubbles so host bindings still work.
    writes.clear()
    t.is_dead = True
    t.on_key(_Ev("b", "b"))
    assert writes == [], writes


def test_mirror_inject_input_parses_full_terminal_keys():
    """Browser input is parsed (Textual's own XTermParser) into the SAME Key events
    a real terminal delivers, then posted to the App -- giving the focused target
    (list / search / dialogs, or a live pane's AgentTerminal) FULL keyboard
    control: printables, Enter, Backspace, AND arrows / Home / Page keys / Delete /
    Shift+Tab / Ctrl combos -- not just printables. This is what makes browser
    control terminal-equivalent; the App then routes each event natively."""
    posted = []
    app = saikai._MirrorControl.__new__(saikai._MirrorControl)
    app._control_enabled = True
    app.post_message = lambda ev: posted.append(ev)

    # Printable text -> one Key per char, character preserved (drives search).
    app._mirror_inject_input("hi")
    assert [(e.key, e.character) for e in posted] == [("h", "h"), ("i", "i")], posted

    # Escape SEQUENCES now resolve to the right NAMED keys (previously dropped):
    # arrows, Home, Page Up, Delete, Shift+Tab.
    posted.clear()
    app._mirror_inject_input("\x1b[A\x1b[B\x1b[H\x1b[5~\x1b[3~\x1b[Z")
    assert [e.key for e in posted] == ["up", "down", "home", "pageup", "delete", "shift+tab"], posted

    # Control combos + Enter + Backspace map to their terminal keys.
    posted.clear()
    app._mirror_inject_input("\x03")     # Ctrl-C
    app._mirror_inject_input("\r")       # Enter
    app._mirror_inject_input("\x7f")     # Backspace
    assert [e.key for e in posted] == ["ctrl+c", "enter", "backspace"], posted

    # A sequence split across two POST batches is reassembled (stateful parser).
    posted.clear()
    app._mirror_inject_input("\x1b[")
    app._mirror_inject_input("D")        # left-arrow, split across batches
    assert [e.key for e in posted] == ["left"], posted

    # A BARE Esc keypress (its own batch) must emit Escape AND not poison the
    # parser: every following key still arrives (regression -- a buffered lone ESC
    # used to swallow all subsequent keys, killing the Space leader in the browser).
    posted.clear()
    app._mirror_inject_input("\x1b")     # bare Esc
    app._mirror_inject_input(" ")        # then Space (leader)
    app._mirror_inject_input("f")        # then mnemonic
    assert [e.key for e in posted] == ["escape", "space", "f"], posted

    # The app gate is still authoritative.
    posted.clear()
    app._control_enabled = False
    app._mirror_inject_input("z")
    assert posted == [], "gate OFF must not route keys"


def test_mirror_inject_stale_partial_discarded_no_phantom():
    """A buffered incomplete escape from an earlier batch must NOT concatenate onto a
    later, unrelated key and fire a phantom (the cross-batch poison the audit found).
    After a >0.5s gap the stale partial is dropped and a fresh parser handles the new
    key cleanly; a within-burst split (<0.5s) still reassembles. (#H9)"""
    posted = []
    app = saikai._MirrorControl.__new__(saikai._MirrorControl)
    app._control_enabled = True
    app.post_message = lambda ev: posted.append(ev)
    app._mirror_inject_input("\x1b[1;5")          # incomplete CSI → buffers, no token yet
    assert posted == [], posted
    app._mirror_parser_ts -= 1.0                   # simulate a >0.5s pause (abandoned)
    app._mirror_inject_input("A")                  # later key must be ITSELF, not ctrl+up
    keys = [getattr(e, "key", None) for e in posted]
    assert keys == ["A"], f"stale CSI poisoned the next key: {keys}"


def test_copy_to_host_clipboard_picks_tool_and_reports():
    """_copy_to_host_clipboard runs the platform clip tool with the text on stdin
    and reports success by exit code, so the QR screen (F12) can copy the URL
    every time and tell the truth about whether it worked."""
    import os
    import subprocess
    calls = []

    class _R:
        def __init__(self, rc):
            self.returncode = rc

    orig = subprocess.run
    # On Linux the tool order is wl-copy (Wayland) -> xclip -> xsel; unset
    # WAYLAND_DISPLAY so this deterministically asserts the X11 path (xclip)
    # regardless of the CI runner's session type.
    orig_wl = os.environ.pop("WAYLAND_DISPLAY", None)
    try:
        subprocess.run = lambda cmd, input=None, **kw: (calls.append((cmd, input)) or _R(0))
        ok = saikai._copy_to_host_clipboard("http://x/?token=abc")
        assert ok is True, calls
        assert calls and calls[0][1] == b"http://x/?token=abc", calls
        expected = ("clip" if sys.platform == "win32"
                    else "pbcopy" if sys.platform == "darwin" else "xclip")
        assert calls[0][0][0] == expected, (calls[0][0], expected)
        # A non-zero exit (or a missing tool) -> False = honest "not copied".
        subprocess.run = lambda *a, **kw: _R(1)
        assert saikai._copy_to_host_clipboard("x") is False
        subprocess.run = lambda *a, **kw: (_ for _ in ()).throw(FileNotFoundError())
        assert saikai._copy_to_host_clipboard("x") is False
    finally:
        subprocess.run = orig
        if orig_wl is not None:
            os.environ["WAYLAND_DISPLAY"] = orig_wl


def test_paste_text_wraps_and_submits():
    """paste_text wraps in bracketed-paste markers when _bracketed_paste is True,
    sends raw when False; submit writes \\r; dead pane never writes."""
    t = rt.AgentTerminal.__new__(rt.AgentTerminal)
    writes = []
    t._pty = type("P", (), {"write": lambda self, d: writes.append(d)})()
    t.is_dead = False
    t._bracketed_paste = True
    t._lock = threading.Lock()
    t._scroll = 0
    t.paste_text("/handoff")
    assert writes == ["\x1b[200~/handoff\x1b[201~"], writes
    writes.clear(); t._bracketed_paste = False
    t.paste_text("/compact")
    assert writes == ["/compact"], writes
    # Bracketed-paste breakout: an embedded ESC[201~ in the pasted text must be
    # STRIPPED before wrapping, else it ends paste mode early and the bytes after
    # it run as typed-and-submitted input. (#H3)
    writes.clear(); t._bracketed_paste = True
    t.paste_text("safe\x1b[201~\rmalicious")
    assert writes == ["\x1b[200~safe\rmalicious\x1b[201~"], writes
    writes.clear(); t.submit()
    assert writes == ["\r"], writes
    # dead pane: no write
    writes.clear(); t.is_dead = True
    t.paste_text("x"); t.submit()
    assert writes == [], writes


def test_forward_wheel_only_when_mouse_reporting():
    """A full-screen child that enabled mouse reporting receives the WHEEL (scrolls
    its OWN view); otherwise saikai keeps its own scrollback. SGR encoding: 64=up,
    65=down; event x/y → 1-based cell; never writes to a dead pane. (#wheel)"""
    t = rt.AgentTerminal.__new__(rt.AgentTerminal)
    writes = []
    t._pty = type("P", (), {"write": lambda self, d: writes.append(d)})()
    t.is_dead = False
    ev = type("E", (), {"x": 4, "y": 2})()
    t._mouse_reporting = False                         # OFF → not forwarded
    assert t._forward_wheel(ev, up=True) is False and writes == []
    t._mouse_reporting = True; t._mouse_sgr = True     # ON + SGR → forwarded
    assert t._forward_wheel(ev, up=True) is True
    assert writes == ["\x1b[<64;5;3M"], writes
    writes.clear()
    assert t._forward_wheel(ev, up=False) is True
    assert writes == ["\x1b[<65;5;3M"], writes
    writes.clear(); t.is_dead = True                   # dead pane → never writes
    assert t._forward_wheel(ev, up=True) is False and writes == []


def test_sync_output_stager_holds_split_frame_until_close():
    s = rt._SynchronizedOutputStager(max_chars=1024, max_age=0.2)
    assert s.push("plain", now=1.0) == [("plain", None)]
    assert s.push("\x1b[?2026h\x1b[?25l\x1b[Hhalf", now=1.1) == []
    assert s.active is True
    assert s.push("done\x1b[?25h\x1b[?2026l", now=1.15) == [
        ("\x1b[?2026h\x1b[?25l\x1b[Hhalfdone\x1b[?25h\x1b[?2026l", None)
    ]
    assert s.active is False


def test_sync_output_stager_orders_back_to_back_and_combined_markers():
    s = rt._SynchronizedOutputStager(max_chars=1024, max_age=0.2)
    units = s.push(
        "A\x1b[?25;2026hF1\x1b[?2026lB"
        "\x1b[?2026hF2\x1b[?25;2026lC",
        now=2.0,
    )
    assert units == [
        ("A", None),
        ("\x1b[?25;2026hF1\x1b[?2026l", None),
        ("B", None),
        ("\x1b[?2026hF2\x1b[?25;2026l", None),
        ("C", None),
    ]


def test_sync_output_stager_bounds_and_flushes_once():
    s = rt._SynchronizedOutputStager(max_chars=12, max_age=0.2)
    assert s.push("\x1b[?2026hab", now=3.0) == []
    timeout = s.push("c", now=3.3)
    assert timeout == [("\x1b[?2026hab", "timeout"), ("c", None)]
    assert s.flush("eof") == []

    s = rt._SynchronizedOutputStager(max_chars=12, max_age=1.0)
    overflow = s.push("\x1b[?2026habcdef", now=4.0)
    assert overflow == [("\x1b[?2026habcdef", "overflow")]
    assert s.flush("eof") == []

    s = rt._SynchronizedOutputStager(max_chars=1024, max_age=1.0)
    assert s.push("\x1b[?2026hlast", now=5.0) == []
    assert s.flush("eof") == [("\x1b[?2026hlast", "eof")]
    assert s.flush("eof") == []


def test_sync_output_next_open_frame_cannot_mutate_queued_complete_frame():
    import pyte

    t = rt.AgentTerminal(["agent"], status_classifier=lambda _txt, _title: "idle")
    t._screen = rt._HistoryScreenBase(30, 6, history=20)
    t._stream = pyte.Stream(t._screen)
    t._sync_output = rt._SynchronizedOutputStager()
    t._marshal = lambda fn: None

    frame_a = "\x1b[?2026h\x1b[5;10HREADY\x1b[?25h\x1b[?2026l"
    assert t._consume(frame_a) is True
    with t._lock:
        stable = (t._screen.cursor.x, t._screen.cursor.y,
                  bool(t._screen.cursor.hidden))

    frame_b_open = "\x1b[?2026h\x1b[?25l\x1b[Hpartial"
    assert t._consume(frame_b_open) is False
    with t._lock:
        observed = (t._screen.cursor.x, t._screen.cursor.y,
                    bool(t._screen.cursor.hidden))

    assert observed == stable
    assert observed[2] is False
    assert observed[:2] != (0, 0)


def test_static_query_answers_before_sync_block_closes():
    import pyte

    t = rt.AgentTerminal(["agent"], status_classifier=lambda _txt, _title: "idle")
    t._screen = rt._HistoryScreenBase(20, 5, history=20)
    t._stream = pyte.Stream(t._screen)
    t._sync_output = rt._SynchronizedOutputStager()
    sent = []
    t._send_to_child = lambda data: sent.append(data)
    t._marshal = lambda fn: fn()

    assert t._consume("\x1b[?2026h\x1b[c") is False
    assert sent == ["\x1b[?6c"]


def test_cursor_query_fail_opens_sync_block_then_reports_new_cursor():
    import pyte

    t = rt.AgentTerminal(["agent"], status_classifier=lambda _txt, _title: "idle")
    t._screen = rt._HistoryScreenBase(20, 5, history=20)
    t._stream = pyte.Stream(t._screen)
    t._sync_output = rt._SynchronizedOutputStager()
    sent = []
    t._send_to_child = lambda data: sent.append(data)
    t._marshal = lambda fn: fn()

    assert t._consume("\x1b[?2026h\x1b[3;7H\x1b[6n") is True
    assert sent == ["\x1b[3;7R"]
    assert t._sync_output.active is False


def test_input_snaps_scrolled_back_pane_to_live():
    """A scrolled-back pane (_scroll > 0) pins its view to history, and the reader
    repaints ONLY at _scroll == 0 (bumping _scroll to keep the pin as output streams
    in). So typing into a scrolled-back pane left the agent's reply invisible until
    the user wheeled all the way back down. Like every terminal, INPUT must snap the
    view to the live bottom: on_key / paste_text / submit reset _scroll to 0. The
    release key (Ctrl+]) is NOT input — it hands focus to the host and must leave
    scrollback untouched."""
    writes = []

    class _Ev:
        def __init__(self, key, character=None):
            self.key = key
            self.character = character
            self.stopped = False

        def stop(self):
            self.stopped = True

    def _mk():
        t = rt.AgentTerminal.__new__(rt.AgentTerminal)
        t._pty = type("P", (), {"write": lambda self, d: writes.append(d)})()
        t.is_dead = False
        t._frozen = False
        t._bracketed_paste = False
        t._lock = threading.Lock()
        t._scroll = 7                 # user wheeled back 7 lines
        t.post_message = lambda m: None
        return t

    # Typing snaps to the live bottom AND still sends the key to the agent.
    t = _mk()
    t.on_key(_Ev("a", "a"))
    assert writes == ["a"], writes
    assert t._scroll == 0, f"typing must snap to live, got _scroll={t._scroll}"

    # Ctrl+] (release focus) is not input: scrollback preserved, nothing written.
    writes.clear()
    t = _mk()
    t.on_key(_Ev(rt.RELEASE_FOCUS_KEY))
    assert writes == [], writes
    assert t._scroll == 7, f"Ctrl+] must not disturb scrollback, got {t._scroll}"

    # paste_text and submit are input too -> snap.
    writes.clear()
    t = _mk()
    t.paste_text("hi")
    assert t._scroll == 0 and writes == ["hi"], (t._scroll, writes)
    writes.clear()
    t = _mk()
    t.submit()
    assert t._scroll == 0 and writes == ["\r"], (t._scroll, writes)


def test_consume_collapses_alt_screen_reset_amplification():
    """A chunk that ALTERNATES alt-screen enter/leave must end on the LAST
    context's content with the correct in_alt — identical to the per-transition
    reset loop, but without N buffer reallocations under the lock. (#audit-altscreen-reset)"""
    import pyte
    t = rt.AgentTerminal.__new__(rt.AgentTerminal)
    t._lock = threading.Lock()
    t._screen = pyte.HistoryScreen(20, 4, history=50)
    t._stream = pyte.Stream(t._screen)
    t._alt = rt.AltScreenTracker()
    t._scroll = 0
    t._scr_ver = 0
    t._esc_carry = ""
    t._bracketed_paste = False
    t._mouse_reporting = False
    t._mouse_sgr = False
    t._in_sync_update = False
    t._sync_started = 0.0
    t._current_screen = lambda: ("", "")
    t._update_status = lambda s: None
    t._status_classifier = lambda txt, title: "idle"
    # AAA(normal) → [enter]BBB → [leave]CCC → [enter]DDD : 3 transitions in one chunk.
    t._consume("AAA\x1b[?1049hBBB\x1b[?1049lCCC\x1b[?1049hDDD")
    line0 = "".join(t._screen.buffer[0][x].data for x in range(20)).rstrip()
    assert t._alt.in_alt is True, t._alt.in_alt        # last toggle entered alt
    assert line0 == "DDD", repr(line0)                 # only the final context is visible
    # A single transition still works (the unchanged common path).
    t._consume("\x1b[?1049lZZZ")
    line0b = "".join(t._screen.buffer[0][x].data for x in range(20)).rstrip()
    assert t._alt.in_alt is False and line0b == "ZZZ", (t._alt.in_alt, repr(line0b))


def test_finalize_preserves_active_drag_snapshot():
    """A child exiting mid-drag must NOT drop the pinned selection snapshot —
    on_mouse_up still needs _frozen_buf to extract the selection. With no drag,
    freeze is cleared so the final live frame shows. (#audit-finalize-race)"""
    def _mk():
        t = rt.AgentTerminal.__new__(rt.AgentTerminal)
        t.is_dead = False
        t._status = "busy"
        t._on_status = None
        t._on_exit = None
        t.sid = "s"
        t._marshal = lambda fn: None
        t.refresh = lambda: None
        t._frozen = True
        t._frozen_buf = {0: ["pinned"]}
        return t
    t = _mk(); t._sel_anchor = (0, 0)          # drag in progress
    t._finalize()
    assert t._frozen is True and t._frozen_buf is not None, "mid-drag snapshot was dropped"
    t = _mk(); t._sel_anchor = None            # no drag
    t._finalize()
    assert t._frozen is False and t._frozen_buf is None


class _FakePtyWrites:
    """Records what saikai writes to the child PTY."""
    def __init__(self):
        self.writes = []
    def write(self, s):
        self.writes.append(s)


class _MouseEv:
    def __init__(self, x, y, button=1, shift=False, meta=False, ctrl=False):
        self.x = x
        self.y = y
        self.button = button
        self.shift = shift
        self.meta = meta
        self.ctrl = ctrl
        self.stopped = False
    def stop(self):
        self.stopped = True


def _mk_mouse_term(sgr=True):
    t = rt.AgentTerminal.__new__(rt.AgentTerminal)
    t._pty = _FakePtyWrites()
    t.is_dead = False
    t._screen = object()
    t._mouse_sgr = sgr
    t._mouse_reporting = True
    t._mouse_click = True
    t._mouse_btn_motion = True
    t._mouse_any_motion = False
    t._fwd_buttons = set()
    t._fwd_captured = False
    t._fwd_last = (1, 1)
    t._pending_anchor = None
    t._sel_anchor = None
    t.focus = lambda: None
    t.capture_mouse = lambda: None
    t.release_mouse = lambda: None
    return t


def test_forward_mouse_sgr_encoding():
    """_forward_mouse inverts Textual's SGR decode (button=(cb+1)&3): L/M/R press,
    release ('m'), drag motion (+32), and shift/ctrl modifiers (+4/+16). 1-based cells."""
    t = _mk_mouse_term(sgr=True)
    w = t._pty.writes
    t._forward_mouse("down", _MouseEv(4, 2, button=1))     # left @ x4,y2 -> col5,row3
    assert w[-1] == "\x1b[<0;5;3M", w[-1]
    t._forward_mouse("down", _MouseEv(0, 0, button=3))     # right -> base (3-1)&3 = 2
    assert w[-1] == "\x1b[<2;1;1M", w[-1]
    t._forward_mouse("up", _MouseEv(4, 2, button=1))       # release terminates 'm'
    assert w[-1] == "\x1b[<0;5;3m", w[-1]
    # motion during a left drag: Textual carries button=1 on the MouseMove
    t._forward_mouse("move", _MouseEv(9, 9, button=1))     # base 0 + motion 32
    assert w[-1] == "\x1b[<32;10;10M", w[-1]
    t._forward_mouse("down", _MouseEv(0, 0, button=1, shift=True, ctrl=True))  # +4+16
    assert w[-1] == "\x1b[<20;1;1M", w[-1]


def test_forward_mouse_legacy_x10():
    """Without SGR (?1006), fall back to X10: \\x1b[M + chr(32+cb/col/row); release
    button code is 3."""
    t = _mk_mouse_term(sgr=False)
    w = t._pty.writes
    t._forward_mouse("down", _MouseEv(4, 2, button=1))     # cb 0, col5, row3
    assert w[-1] == "\x1b[M" + chr(32) + chr(37) + chr(35), repr(w[-1])
    t._forward_mouse("up", _MouseEv(4, 2, button=1))       # release -> cb 3
    assert w[-1] == "\x1b[M" + chr(35) + chr(37) + chr(35), repr(w[-1])
    # col/row past 95 CAP at 95 (chr(127)) — never emit chr(>=128), which pty.write
    # would expand to multi-byte UTF-8 and corrupt the fixed 6-byte X10 packet.
    t._forward_mouse("down", _MouseEv(120, 200, button=1))
    assert w[-1] == "\x1b[M" + chr(32) + chr(127) + chr(127), repr(w[-1])


def test_dec_private_re_parses_combined_params():
    """The DEC-private regex captures the WHOLE param list + h/l, so COMBINED
    params (\\x1b[?1002;1006h) are seen — a per-mode regex would miss that form."""
    assert rt._DEC_PRIVATE_RE.findall("\x1b[?1002;1006h") == [("1002;1006", "h")]
    assert rt._DEC_PRIVATE_RE.findall(
        "\x1b[?1000h\x1b[?1006h\x1b[?1002l") == [("1000", "h"), ("1006", "h"), ("1002", "l")]


def test_on_mouse_down_forwards_all_when_child_tracks_else_selects():
    """When the child tracks the mouse (fullscreen), EVERY press forwards to it —
    incl. Shift (saikai keeps no in-pane selection there; the child's is smarter and
    OSC-52-copies). When the child does NOT track (classic renderer / plain shell), a
    bare press starts saikai's own grid selection instead."""
    t = _mk_mouse_term(sgr=True)    # (reading self.has_focus raises on a __new__ inst;
                                    #  on_mouse_down's guard try/except swallows it)
    t.on_mouse_down(_MouseEv(3, 1, button=1, shift=False))
    assert t._pty.writes and t._pty.writes[-1].startswith("\x1b[<0;4;2"), t._pty.writes
    assert 1 in t._fwd_buttons
    # Shift+press ALSO forwards now (shift modifier bit +4 → cb 4)
    t._fwd_buttons = set()
    t._pty.writes.clear()
    t.on_mouse_down(_MouseEv(3, 1, button=1, shift=True))
    assert t._pty.writes and t._pty.writes[-1] == "\x1b[<4;4;2M", t._pty.writes
    assert 1 in t._fwd_buttons
    # classic child (no mouse tracking): bare press → saikai's OWN selection anchor
    t._fwd_buttons = set()
    t._pty.writes.clear()
    t._mouse_reporting = False
    t._mouse_click = t._mouse_btn_motion = t._mouse_any_motion = False
    t.on_mouse_down(_MouseEv(3, 1, button=1, shift=False))
    assert t._pty.writes == [] and t._pending_anchor == (1, 3)


def test_on_mouse_move_forwards_motion_only_when_tracked():
    """A forwarded drag relays motion only if the child asked for it (?1002/?1003)."""
    t = _mk_mouse_term(sgr=True)
    t._fwd_buttons = {1}
    t._fwd_captured = True                       # already capturing (skip capture_mouse)
    t._mouse_btn_motion = True
    t.on_mouse_move(_MouseEv(9, 9, button=1))
    assert t._pty.writes and t._pty.writes[-1] == "\x1b[<32;10;10M"
    # click-only child (no motion modes): a forwarded drag must NOT relay motion
    t._pty.writes.clear()
    t._mouse_btn_motion = False
    t._mouse_any_motion = False
    t.on_mouse_move(_MouseEv(5, 5, button=1))
    assert t._pty.writes == []


def test_on_mouse_move_forwards_hover_when_any_motion():
    """A child with ?1003 (any-motion) gets hover reports even with NO button held."""
    t = _mk_mouse_term(sgr=True)
    t._mouse_any_motion = True                 # ?1003 hover tracking on (no button held)
    t.on_mouse_move(_MouseEv(2, 2, button=0))  # no button
    assert t._pty.writes and t._pty.writes[-1] == "\x1b[<35;3;3M"   # no-button motion: base 3 + 32
    # without any-motion, a hover (no held button) is NOT forwarded
    t._pty.writes.clear()
    t._mouse_any_motion = False
    t.on_mouse_move(_MouseEv(2, 2, button=0))
    assert t._pty.writes == []


def test_on_mouse_up_skips_release_when_child_stopped_tracking():
    """If the child turned mouse tracking OFF mid-drag, on_mouse_up must NOT write a
    stray release — but must still drop the capture / _fwd_buttons state."""
    t = _mk_mouse_term(sgr=True)
    t._fwd_buttons = {1}
    t._mouse_reporting = False                 # child disabled tracking mid-drag
    t._mouse_click = t._mouse_btn_motion = t._mouse_any_motion = False
    t.on_mouse_up(_MouseEv(4, 2, button=1))
    assert t._pty.writes == [] and not t._fwd_buttons


def test_on_mouse_up_multi_button_releases_correct_button():
    """A second button pressed during a held drag must release with ITS OWN button;
    the first button's release must not be mis-attributed, and the capture is held
    until ALL buttons are up. (regression: a single _fwd_drag overwrote the button)"""
    t = _mk_mouse_term(sgr=True)
    t.on_mouse_down(_MouseEv(0, 0, button=1))   # left down
    t.on_mouse_down(_MouseEv(0, 0, button=3))   # right down (left still held)
    assert t._fwd_buttons == {1, 3}
    t._pty.writes.clear()
    t.on_mouse_up(_MouseEv(0, 0, button=1))     # left up → left release, right still held
    assert t._pty.writes[-1] == "\x1b[<0;1;1m", t._pty.writes
    assert t._fwd_buttons == {3}
    t.on_mouse_up(_MouseEv(0, 0, button=3))     # right up → right release, gesture ends
    assert t._pty.writes[-1] == "\x1b[<2;1;1m", t._pty.writes
    assert t._fwd_buttons == set()


def test_cancel_forwarded_drag_sends_release():
    """A stuck forwarded drag (lost MouseUp on blur/alt-tab) must send the child a
    release so it doesn't believe the button is still held, then clear state."""
    t = _mk_mouse_term(sgr=True)
    t._fwd_buttons = {1}
    t._fwd_last = (3, 2)
    t._cancel_forwarded_drag()
    assert t._pty.writes and t._pty.writes[-1] == "\x1b[<0;3;2m", t._pty.writes
    assert not t._fwd_buttons and t._fwd_captured is False


def test_honor_osc52_decodes_and_copies():
    """A child's OSC 52 clipboard write (e.g. claude's fullscreen copy) is base64-
    decoded onto the HOST clipboard; a "?"/empty (read query) is ignored."""
    import base64
    t = rt.AgentTerminal.__new__(rt.AgentTerminal)
    copied = []
    t._copy_text = lambda s: copied.append(s)
    t._marshal = lambda fn: fn()                 # run the marshalled copy inline
    t._honor_osc52(base64.b64encode("hello ぺ".encode()).decode())
    assert copied == ["hello ぺ"], copied
    t._honor_osc52("?"); t._honor_osc52("")      # read query / empty → no copy
    assert copied == ["hello ぺ"], copied


def test_osc52_re_extracts_payload_and_needs_terminator():
    """_OSC52_RE matches a BEL- or ST-terminated OSC 52 and yields the base64; an
    UNterminated sequence doesn't match (it's carried across reads in _consume)."""
    import base64
    b64 = base64.b64encode(b"xy").decode()
    assert rt._OSC52_RE.findall(f"\x1b]52;c;{b64}\x07") == [b64]
    assert rt._OSC52_RE.findall(f"\x1b]52;c;{b64}\x1b\\") == [b64]
    assert rt._OSC52_RE.findall(f"\x1b]52;c;{b64}") == []


def test_answer_queries_responds_to_terminal_probes():
    """saikai answers the child's terminal queries (it sits between the child and the
    real terminal): Primary DA, DSR status/cursor-position (private ?6n → private
    reply), DECRQM ?2026 (supported), XTVERSION, OSC 10/11 color. No query → silent."""
    t = rt.AgentTerminal.__new__(rt.AgentTerminal)
    sent = []
    t._send_to_child = lambda d: sent.append(d)
    t._marshal = lambda fn: fn()                 # run the marshalled write inline
    t._cursor_rowcol = lambda: (3, 7)
    def _one(q):
        sent.clear(); t._answer_queries(q); return sent[-1] if sent else None
    assert _one("\x1b[c") == "\x1b[?6c"
    assert _one("\x1b[0c") == "\x1b[?6c"
    assert _one("\x1b[?6n") == "\x1b[?3;7R"       # private cursor-position reply
    assert _one("\x1b[6n") == "\x1b[3;7R"         # standard cursor-position reply
    assert _one("\x1b[5n") == "\x1b[0n"           # device status OK
    assert _one("\x1b[?2026$p") == "\x1b[?2026;2$y"    # synchronized output supported
    assert _one("\x1b[?1000$p") == "\x1b[?1000;0$y"    # other mode: not recognised
    assert _one("\x1b[>0q") == "\x1bP>|saikai\x1b\\"   # XTVERSION
    assert _one("\x1b]11;?\x07") == "\x1b]11;rgb:1e1e/1e1e/1e1e\x07"  # bg (dark)
    assert _one("\x1b]10;?\x07") == "\x1b]10;rgb:c0c0/c0c0/c0c0\x07"  # fg (light)
    sent.clear(); t._answer_queries("plain \x1b[1m bold \x1b[0m"); assert sent == []


def test_osc_notification_parsing_and_notify_host():
    """OSC 9/777/99 desktop notifications are parsed (OSC 9;4 progress excluded) and
    surfaced as a stripped, non-empty saikai toast."""
    assert rt._OSC9_NOTIFY_RE.findall("\x1b]9;Task done\x07") == ["Task done"]
    assert rt._OSC9_NOTIFY_RE.findall("\x1b]9;4;1;50\x07") == []       # 9;4 progress, not a notify
    assert rt._OSC777_RE.findall("\x1b]777;notify;Title;Body\x07") == ["Title;Body"]
    assert rt._OSC99_RE.findall("\x1b]99;i=1:d=0:p=title;Hello\x1b\\") == ["Hello"]
    t = rt.AgentTerminal.__new__(rt.AgentTerminal)
    notes = []
    t.notify = lambda m, **k: notes.append(m)
    t._marshal = lambda fn: fn()
    t._notify_host("  hi  "); assert notes == ["hi"]
    t._notify_host("   "); assert notes == ["hi"]                       # empty → no toast



def test_mirror_tee_orders_seed_before_stream_verbatim():
    """Pane-direct tee contract (#pane-direct): (1) attach_mirror computes the
    seed and enqueues it UNDER _lock, and _consume tees under the same lock —
    so a chunk is either inside the seed or ordered after it, never both;
    (2) the tee'd bytes are VERBATIM — the query strip
    happens on the hub's drain thread, not here under the reader lock; (3) DECCKM (?1) is tracked for the seed's mode replay;
    (4) detach stops the tee."""
    import pyte
    term = rt.AgentTerminal(["agent"], status_classifier=rt.classify_pty_status)
    term._screen = pyte.HistoryScreen(20, 5, history=50)
    term._stream = pyte.Stream(term._screen)
    events = []
    def tee(d):
        events.append(("tee", d))
    def reset(seed):
        events.append(("seed", seed))
    def synth(scr, cols, rows, modes):
        return f"SYNTH:{cols}x{rows}:alt={modes['alt']}:app={modes['app_cursor']}"
    term._consume("before-attach")            # pre-attach bytes: seed-only
    assert events == [], "nothing tees before attach"
    term.attach_mirror(tee, reset, synth)
    assert events and events[0][0] == "seed", "attach must emit the seed first"
    assert "20x5" in events[0][1]
    term._consume("\x1b[?1h")                 # DECCKM on — tracked + tee'd
    term._consume("plain \x1b[6n text \x1b[0c and \x1b[?2026$p done")
    tees = [d for k, d in events if k == "tee"]
    assert tees[0] == "\x1b[?1h"
    # the tee passes the chunk VERBATIM — the child-query strip runs on the
    # mirror hub's drain thread (set_pane_strip), never on the reader thread
    # under the terminal lock (#review-strip-offload)
    assert tees[1] == "plain \x1b[6n text \x1b[0c and \x1b[?2026$p done", \
        f"tee must be verbatim (strip is drain-side): {tees[1]!r}"
    assert term._app_cursor is True
    events.clear()
    term.mirror_reseed()                       # hub-requested reseed
    assert len(events) == 1 and events[0][0] == "seed"
    assert "app=True" in events[0][1], "DECCKM must reach the seed's mode replay"
    events.clear()
    term.detach_mirror()
    term._consume("after-detach")
    assert events == [], "detach must stop the tee"


def test_mirror_seed_and_tee_are_lock_consistent():
    """The reader thread feeds pyte + tees under _lock while attach_mirror
    seeds under the same lock from another thread: every byte must land in
    EXACTLY one of (seed-covered screen state, tee'd stream) — the browser
    applying a chunk that is also inside the seed garbles (relative cursor
    moves run twice). Hammer attach/detach against a feeding thread and check
    the invariant via a monotonically-increasing payload counter."""
    import pyte
    term = rt.AgentTerminal(["agent"], status_classifier=rt.classify_pty_status)
    term._screen = pyte.HistoryScreen(40, 5, history=50)
    term._stream = pyte.Stream(term._screen)
    tee_log = []
    seeds = []
    def synth(scr, cols, rows, modes):
        # capture the screen TEXT at seed time — the last counter painted
        # under the lock is inside the seed
        try:
            txt = "".join(ch.data or " " for ch in scr.buffer[0].values()) \
                  if hasattr(scr.buffer[0], "values") else \
                  "".join((scr.buffer[0][x].data or " ") for x in range(scr.columns))
        except Exception:
            txt = ""
        return "SEED[" + txt.strip() + "]"
    stop = threading.Event()
    def feeder():
        i = 0
        while not stop.is_set() and i < 4000:
            i += 1
            term._consume(f"\x1b[1;1Hn={i:06d}")
    ft = threading.Thread(target=feeder)
    ft.start()
    for _ in range(200):
        term.attach_mirror(lambda d: tee_log.append(d),
                           lambda s: seeds.append((s, len(tee_log))),
                           synth)
        term.detach_mirror()
    stop.set()
    ft.join(5.0)
    assert not ft.is_alive(), "feeder must not deadlock against attach/detach"
    # For every seed: the first tee'd chunk AFTER it must carry a counter
    # STRICTLY GREATER than the one captured inside the seed (no replay).
    import re
    for seed, mark in seeds:
        sm = re.search(r"n=(\d+)", seed)
        if sm is None:
            continue                      # seeded before the first feed
        for d in tee_log[mark:]:
            dm = re.search(r"n=(\d+)", d)
            if dm is None:
                continue
            assert int(dm.group(1)) > int(sm.group(1)), \
                f"chunk n={dm.group(1)} tee'd after a seed that already contains n={sm.group(1)}"
            break


def test_mouse_tracking_is_one_exclusive_protocol_slot():
    """DECSET 1000/1002/1003 share ONE protocol slot in real xterm and
    xterm.js: an enable replaces the active protocol, a DECRST of ANY family
    member turns tracking off entirely. Independent booleans left a stale flag
    ("1000h…1003h…1003l" kept click-tracking True) and the mirror seed then
    re-armed mouse reporting on a child that had turned it off.
    (#review-mouse-slot)"""
    import pyte
    term = rt.AgentTerminal(["agent"], status_classifier=rt.classify_pty_status)
    term._screen = pyte.HistoryScreen(20, 5, history=10)
    term._stream = pyte.Stream(term._screen)
    term._consume("\x1b[?1000h")
    assert term._mouse_click and not term._mouse_any_motion
    term._consume("\x1b[?1003h")             # upgrade replaces the protocol
    assert term._mouse_any_motion and not term._mouse_click, \
        "an enable must REPLACE the slot, not stack"
    term._consume("\x1b[?1003l")             # any family reset → tracking OFF
    assert not (term._mouse_click or term._mouse_btn_motion
                or term._mouse_any_motion), \
        "a family DECRST must clear the whole slot"
    assert term._mouse_reporting is False
    # combined enable ends on the LAST protocol in the sequence (like a real
    # terminal applying them in order)
    term._consume("\x1b[?1000;1002;1006h")
    assert term._mouse_btn_motion and not term._mouse_click
    assert term._mouse_sgr, "encoding (1006) is independent of the slot"

def test_copy_text_relays_to_mirror_clip():
    """claude does NOT track the mouse in its normal prompt, so the terminal owns
    selection AND copy — the pane's own drag-select copy is the ONLY copy a
    mirror viewer gets. _copy_text must relay to MIRROR_CLIP (the hub's send_clip)
    so it reaches the device the viewer is holding, not just the host.
    (#app-native-select)"""
    term = rt.AgentTerminal(["agent"], status_classifier=rt.classify_pty_status)
    got = []
    _prev = rt.MIRROR_CLIP
    rt.MIRROR_CLIP = lambda t: got.append(t)
    try:
        term._copy_text("selected text")
    finally:
        rt.MIRROR_CLIP = _prev
    assert got == ["selected text"], f"copy must relay to the mirror: {got}"
    got.clear()
    rt.MIRROR_CLIP = lambda t: got.append(t)
    try:
        term._copy_text("")
    finally:
        rt.MIRROR_CLIP = _prev
    assert got == [], "empty copy must not relay"


def test_busy_storm_throttles_reclassify():
    """An agent-mode spinner emits ~170k synchronized frames/session; re-classifying
    each (a full pyte-grid render ~0.7ms + the regex ~0.2ms) burned ~150s of CPU only
    to re-confirm 'busy'. While stably busy, _consume throttles the re-classify to
    _CLASSIFY_MIN_INTERVAL; a flip INTO busy is never throttled (busy detection stays
    immediate) and the flip OUT rides the host refresh_status poll. Verified against a
    real capture: 5,993 frames -> 608 classifies. (#agent-storm-throttle)"""
    import pyte, time as _t
    term = rt.AgentTerminal(["agent"], status_classifier=rt.classify_pty_status)
    try:
        term._stop.set()                       # stop any reader; we feed _consume by hand
    except Exception:
        pass
    term._marshal = lambda fn: None            # no Textual app in the harness
    term._screen = rt._HistoryScreenBase(120, 40, history=rt.SCROLLBACK_LINES)
    term._stream = pyte.Stream(term._screen)
    calls = []
    _orig = term._classify
    term._classify = lambda txt, title: (calls.append(1), _orig(txt, title))[1]
    busy = "\x1b]0;⠋ working\x1b\\\x1b[H spinning "   # braille title glyph -> busy

    term._consume(busy)                        # status idle -> NOT throttled -> classifies
    assert term._status == "busy", "a spinner frame must flip the pane to busy"
    assert len(calls) == 1, calls

    term._last_classify_ts = _t.monotonic()    # pretend we just classified
    term._consume(busy)
    assert len(calls) == 1, "a busy frame within the throttle window must be skipped"

    term._last_classify_ts = _t.monotonic() - (rt._CLASSIFY_MIN_INTERVAL + 0.05)
    term._consume(busy)
    assert len(calls) == 2, "a busy frame after the interval must re-classify"


def test_cursor_anchor_does_not_chase_every_repaint():
    """The IME/candidate anchor must NOT chase the live pyte cursor on every repaint:
    an agent spinner moves it Home -> prompt on all ~170k frames and coalesced repaints
    caught it mid-frame, flickering the anchor across the screen. The fix keeps the sync
    INLINE on the repaint (so it always rides a CompositorUpdate and flushes) but FREEZES
    the anchor position while status=='busy'; the debounce timer machinery is gone and no
    longer starves. A focus/settle sync anchors immediately. (#agents-cursor)"""
    import inspect
    refresh_src = inspect.getsource(rt.AgentTerminal._do_pane_refresh)
    assert "_sync_terminal_cursor" in refresh_src, \
        "_do_pane_refresh must sync the cursor inline (rides the repaint = flushes)"
    assert "_schedule_terminal_cursor_sync" not in refresh_src, \
        "the debounce timer indirection must be gone (it starved + never flushed)"
    sync_src = inspect.getsource(rt.AgentTerminal._sync_terminal_cursor)
    assert "_prev_sync_cursor" not in sync_src, "the old two-repaint gate must be gone"
    assert 'reason == "repaint"' in sync_src and '"busy"' in sync_src, \
        "the anti-fly must be a status=='busy' freeze on the repaint sync"
    assert not hasattr(rt.AgentTerminal, "_schedule_terminal_cursor_sync"), \
        "the debounce timer machinery must be removed"


def test_ime_anchor_default_on_keeps_windows_caret_render_guard():
    """The IME anchor is default ON (opt-OUT) so CJK composition lands at the pane
    prompt, and the render guard must stay the general form that survives BOTH states.
    (1) render_line draws saikai's OWN cursor unless the native hardware cursor is
    handling it (Windows AND anchor ON): `not (_IS_WIN and _IME_ANCHOR)`. With the
    anchor ON (default) Windows defers to the native cursor; with SAIKAI_IME_ANCHOR=0
    the caret is still drawn (the old `not _IS_WIN` guard left NO caret there once
    _show_hw_cursor went inert). (2) SAIKAI_IME_ANCHOR is parsed opt-OUT: only an
    explicit 0/false/no/off turns it off. (#native-cursor #agents-cursor)"""
    from pathlib import Path
    src = Path(rt.__file__).read_text(encoding="utf-8")
    assert "not (_IS_WIN and _IME_ANCHOR)" in src, \
        "render_line must draw the caret on Windows when the IME anchor is OFF"
    assert "x == cursor_x and not _IS_WIN:" not in src, \
        "the old guard that skipped the caret on ALL Windows panes must be gone"
    assert 'SAIKAI_IME_ANCHOR", "1")).strip().lower() not in (' in src, \
        "the anchor env parse must be opt-OUT (default ON)"
    assert '"0", "false", "no", "off"' in src, "opt-out tokens missing"


if __name__ == "__main__":
    test_osc_notification_parsing_and_notify_host()
    print("PASS test_osc_notification_parsing_and_notify_host")
    test_answer_queries_responds_to_terminal_probes()
    print("PASS test_answer_queries_responds_to_terminal_probes")
    test_honor_osc52_decodes_and_copies()
    print("PASS test_honor_osc52_decodes_and_copies")
    test_osc52_re_extracts_payload_and_needs_terminator()
    print("PASS test_osc52_re_extracts_payload_and_needs_terminator")
    test_consume_collapses_alt_screen_reset_amplification()
    print("PASS test_consume_collapses_alt_screen_reset_amplification")
    test_finalize_preserves_active_drag_snapshot()
    print("PASS test_finalize_preserves_active_drag_snapshot")
    test_update_status_marshals_outside_lock()
    print("PASS test_update_status_marshals_outside_lock")
    test_ime_anchor_xy_maps_cursor_into_region()
    print("PASS test_ime_anchor_xy_maps_cursor_into_region")
    test_reopen_after_exit_requires_awaited_pane_removal()
    print("PASS test_reopen_after_exit_requires_awaited_pane_removal")
    test_kill_tracks_reap_for_atexit_join()
    print("PASS test_kill_tracks_reap_for_atexit_join")
    test_posix_kill_signals_only_and_closes_off_thread()
    print("PASS test_posix_kill_signals_only_and_closes_off_thread")
    test_posix_reap_escalates_to_sigkill()
    print("PASS test_posix_reap_escalates_to_sigkill")
    test_post_signal_never_raises()
    print("PASS test_post_signal_never_raises")
    test_forward_mouse_sgr_encoding()
    print("PASS test_forward_mouse_sgr_encoding")
    test_forward_mouse_legacy_x10()
    print("PASS test_forward_mouse_legacy_x10")
    test_dec_private_re_parses_combined_params()
    print("PASS test_dec_private_re_parses_combined_params")
    test_on_mouse_down_forwards_all_when_child_tracks_else_selects()
    print("PASS test_on_mouse_down_forwards_all_when_child_tracks_else_selects")
    test_on_mouse_move_forwards_motion_only_when_tracked()
    print("PASS test_on_mouse_move_forwards_motion_only_when_tracked")
    test_on_mouse_move_forwards_hover_when_any_motion()
    print("PASS test_on_mouse_move_forwards_hover_when_any_motion")
    test_on_mouse_up_skips_release_when_child_stopped_tracking()
    print("PASS test_on_mouse_up_skips_release_when_child_stopped_tracking")
    test_on_mouse_up_multi_button_releases_correct_button()
    print("PASS test_on_mouse_up_multi_button_releases_correct_button")
    test_cancel_forwarded_drag_sends_release()
    print("PASS test_cancel_forwarded_drag_sends_release")
    test_pane_refresh_coalesces()
    print("PASS test_pane_refresh_coalesces")
    test_current_screen_caches_by_version()
    print("PASS test_current_screen_caches_by_version")
    test_refresh_status_skips_stable_idle_pane()
    print("PASS test_refresh_status_skips_stable_idle_pane")
    test_refresh_status_polls_pending_flip_on_static_screen()
    print("PASS test_refresh_status_polls_pending_flip_on_static_screen")
    test_classify_pty_status_basics()
    print("PASS test_classify_pty_status_basics")
    test_show_hw_cursor_native_cursor_dec_bytes()
    print("PASS test_show_hw_cursor_native_cursor_dec_bytes")
    test_native_cursor_follows_dectcem_regardless_of_screen()
    print("PASS test_native_cursor_follows_dectcem_regardless_of_screen")
    test_child_pty_env_hides_outer_terminal_identity_from_child()
    print("PASS test_child_pty_env_hides_outer_terminal_identity_from_child")
    test_cursor_sync_freezes_while_busy_and_settles_on_transition()
    print("PASS test_cursor_sync_freezes_while_busy_and_settles_on_transition")
    test_autoscroll_tick_pins_anchor_to_content()
    print("PASS test_autoscroll_tick_pins_anchor_to_content")
    test_alt_screen_suppresses_false_needs_input()
    print("PASS test_alt_screen_suppresses_false_needs_input")
    test_real_gates_on_alt_screen_stay_waiting()
    print("PASS test_real_gates_on_alt_screen_stay_waiting")
    test_classify_trust_folder_dialog_is_waiting()
    print("PASS test_classify_trust_folder_dialog_is_waiting")
    test_status_classifier_profiles_and_injection()
    print("PASS test_status_classifier_profiles_and_injection")
    test_encode_key_meta_and_release()
    print("PASS test_encode_key_meta_and_release")
    test_configure_release_focus_key_restores_old_key()
    print("PASS test_configure_release_focus_key_restores_old_key")
    test_copy_text_uses_pbcopy_on_macos_before_osc52()
    print("PASS test_copy_text_uses_pbcopy_on_macos_before_osc52")
    test_set_clipboard_macos_skips_remote_sessions()
    print("PASS test_set_clipboard_macos_skips_remote_sessions")
    test_copy_text_skips_pbcopy_on_macos_over_ssh()
    print("PASS test_copy_text_skips_pbcopy_on_macos_over_ssh")
    test_set_status_ignores_forgotten_sid()
    print("PASS test_set_status_ignores_forgotten_sid")
    test_rekey_moves_term_status_and_pane_id()
    print("PASS test_rekey_moves_term_status_and_pane_id")
    test_note_reap_prunes_finished_threads()
    print("PASS test_note_reap_prunes_finished_threads")
    test_kitty_keyboard_csi_u_is_scrubbed()
    print("PASS test_kitty_keyboard_csi_u_is_scrubbed")
    test_mirror_tee_orders_seed_before_stream_verbatim()
    print("PASS test_mirror_tee_orders_seed_before_stream_verbatim")
    test_mirror_seed_and_tee_are_lock_consistent()
    print("PASS test_mirror_seed_and_tee_are_lock_consistent")
    test_mouse_tracking_is_one_exclusive_protocol_slot()
    print("PASS test_mouse_tracking_is_one_exclusive_protocol_slot")
    test_copy_text_relays_to_mirror_clip()
    print("PASS test_copy_text_relays_to_mirror_clip")
    test_selection_geometry_in_sel()
    print("PASS test_selection_geometry_in_sel")
    test_extract_selection_slices_and_joins()
    print("PASS test_extract_selection_slices_and_joins")
    test_frozen_pane_copy_uses_snapshot_not_live_buffer()
    print("PASS test_frozen_pane_copy_uses_snapshot_not_live_buffer")
    test_toggle_freeze_flips_and_resumes()
    print("PASS test_toggle_freeze_flips_and_resumes")
    test_bracketed_paste_mode_tracking()
    print("PASS test_bracketed_paste_mode_tracking")
    test_agent_terminal_on_key_release_encode_and_dead()
    print("PASS test_agent_terminal_on_key_release_encode_and_dead")
    test_mirror_inject_input_parses_full_terminal_keys()
    print("PASS test_mirror_inject_input_parses_full_terminal_keys")
    test_mirror_inject_stale_partial_discarded_no_phantom()
    print("PASS test_mirror_inject_stale_partial_discarded_no_phantom")
    test_copy_to_host_clipboard_picks_tool_and_reports()
    print("PASS test_copy_to_host_clipboard_picks_tool_and_reports")
    test_paste_text_wraps_and_submits()
    print("PASS test_paste_text_wraps_and_submits")
    test_forward_wheel_only_when_mouse_reporting()
    print("PASS test_forward_wheel_only_when_mouse_reporting")
    test_sync_output_stager_holds_split_frame_until_close()
    print("PASS test_sync_output_stager_holds_split_frame_until_close")
    test_sync_output_stager_orders_back_to_back_and_combined_markers()
    print("PASS test_sync_output_stager_orders_back_to_back_and_combined_markers")
    test_sync_output_stager_bounds_and_flushes_once()
    print("PASS test_sync_output_stager_bounds_and_flushes_once")
    test_sync_output_next_open_frame_cannot_mutate_queued_complete_frame()
    print("PASS test_sync_output_next_open_frame_cannot_mutate_queued_complete_frame")
    test_static_query_answers_before_sync_block_closes()
    print("PASS test_static_query_answers_before_sync_block_closes")
    test_cursor_query_fail_opens_sync_block_then_reports_new_cursor()
    print("PASS test_cursor_query_fail_opens_sync_block_then_reports_new_cursor")
    test_input_snaps_scrolled_back_pane_to_live()
    print("PASS test_input_snaps_scrolled_back_pane_to_live")
    test_busy_storm_throttles_reclassify()
    print("PASS test_busy_storm_throttles_reclassify")
    test_cursor_anchor_does_not_chase_every_repaint()
    print("PASS test_cursor_anchor_does_not_chase_every_repaint")
    test_ime_anchor_default_on_keeps_windows_caret_render_guard()
    print("PASS test_ime_anchor_default_on_keeps_windows_caret_render_guard")
