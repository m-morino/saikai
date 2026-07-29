"""Headless regression tests for ClaudeTerminal threading.

Runs WITHOUT textual/pyte/pywinpty: saikai_terminal soft-imports them (Widget
falls back to object), so ClaudeTerminal can be built via __new__ with just the
fields under test. Run:  python tests/test_terminal_concurrency.py
"""
import os
import sys
import collections
import threading
import time

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
        t._cursor_hidden_since = 0.0
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

        # visible cursor on a repaint -> FOLLOW it (show + anchor), single sync.
        t, writes = _term()
        t._sync_terminal_cursor()
        assert writes == ["\x1b[?25h"], writes
        assert t._app.cursor_position == rt.Offset(43, 7)

        # claude agent mode: alt-screen + cursor VISIBLE -> still follow (track).
        t, writes = _term(in_alt=True)
        t._sync_terminal_cursor()
        assert writes == ["\x1b[?25h"], writes
        assert t._app.cursor_position == rt.Offset(43, 7)

        # cursor HIDDEN on a repaint (mid-redraw / no-cursor) -> FREEZE: touch neither
        # visibility nor position, so the child's ?25l/?25h redraw can't flicker it.
        t, writes = _term(hidden=True)
        t._sync_terminal_cursor()
        assert writes == [], writes
        assert getattr(t._app, "cursor_position", None) is None

        # a definitive focus sync on a hidden cursor DOES hide (real no-cursor state).
        t, writes = _term(hidden=True)
        t._sync_terminal_cursor(reason="focus")
        assert writes == ["\x1b[?25l"], writes

        # tracking: a visible cursor moving cell-to-cell -> the anchor FOLLOWS each
        # repaint (claude moves the terminal cursor to the caret; +2 cols per CJK char).
        t, writes = _term()
        t._sync_terminal_cursor()
        assert t._app.cursor_position == rt.Offset(43, 7)
        t._screen.cursor.x = 9
        t._sync_terminal_cursor()
        assert t._app.cursor_position == rt.Offset(49, 7), "anchor must follow the caret"
    finally:
        rt._IS_WIN, rt._IME_ANCHOR, rt.Offset = old_win, old_anchor, old_offset


def test_marshal_runs_inline_on_the_apps_own_thread():
    """A marshal issued FROM the UI thread must still run the callback.

    Textual's call_from_thread raises when the caller is the app's own thread, and
    _marshal swallows it — so every UI-thread-originated marshal was a silent no-op.
    The busy->idle flip is detected by the 1.5s UI-thread status poll in the common
    'claude finished quietly' case, which dropped the 'settle' anchor sync and left
    the IME anchored at the frozen mid-turn cell. Run it inline instead; a callback
    that raises must still not escape. (#ime-settle)"""
    import threading as _th

    class _SameThreadApp:
        def __init__(self):
            self._thread_id = _th.get_ident()
        def call_from_thread(self, fn):
            raise RuntimeError("must run in a different thread from the app")

    class _ReaderThreadApp:
        _thread_id = -1
        def __init__(self):
            self.posted = []
        def call_from_thread(self, fn):
            self.posted.append(fn)

    class _Shim(rt.AgentTerminal):
        app = property(lambda self: self._app)

    t = _Shim.__new__(_Shim)
    ran = []
    t._app = _SameThreadApp()
    t._marshal(lambda: ran.append("settle"))
    assert ran == ["settle"], "a UI-thread marshal must not be dropped"

    def _boom():
        raise ValueError("callback blew up")
    t._marshal(_boom)                       # must not escape _marshal

    t._app = _ReaderThreadApp()             # reader thread: still posted, not inline
    t._marshal(lambda: ran.append("reader"))
    assert ran == ["settle"], "an off-thread marshal must go through call_from_thread"
    assert len(t._app.posted) == 1


def test_focus_gate_yields_to_a_modal_screen_on_top():
    """A pushed ModalScreen must silence the pane's IME anchoring.

    Textual only SUSPENDS the base screen on push_screen — it never clears its
    .focused — so ``screen.focused is self`` still names this pane while a modal
    owns the keyboard. The anchor then steals app.cursor_position (and forces
    ?25h) away from the modal's Input, and a CJK composition in a rename box
    jumps to the pane's prompt cell. Gate on the app's ACTIVE screen too.
    (#ime-modal)"""
    class _Screen:
        focused = None

    class _App:
        def __init__(self, screen):
            self.screen = screen

    class _Shim(rt.AgentTerminal):
        app = property(lambda self: self._app)
        screen = property(lambda self: self._screen_node)

    t = _Shim.__new__(_Shim)
    base = _Screen()
    base.focused = t
    t._screen_node = base
    t._app = _App(base)
    assert t._is_focused_pane() is True          # pane owns the live focus

    t._app.screen = _Screen()                    # modal pushed; base keeps .focused
    assert t._is_focused_pane() is False, "a modal on top must silence the anchor"

    t._app.screen = base                         # modal popped -> anchoring resumes
    assert t._is_focused_pane() is True


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
    }, is_win=True)
    assert env["PATH"] == "/bin"
    assert env["TERM"] == "xterm-256color"
    assert env["COLORTERM"] == "truecolor"
    # WT_SESSION is PRESERVED so Claude detects Windows Terminal and uses the cursor
    # path that tracks the caret (stripping it parked the cursor and broke IME tracking).
    assert env["WT_SESSION"] == "outer-wt"
    assert "TERM_PROGRAM" not in env
    assert "TERM_PROGRAM_VERSION" not in env
    assert "KITTY_WINDOW_ID" not in env
    assert "CLAUDE_CODE_ALT_SCREEN_FULL_REPAINT" not in env
    # Explicit user/developer override remains explicit; only host identity leaks
    # and Claude's derived WT full-repaint flag are scrubbed.
    assert env["CLAUDE_CODE_FORCE_SYNC_OUTPUT"] == "1"


def test_child_pty_env_presents_one_windows_terminal_identity_per_platform():
    """The pane's WT identity is saikai's OWN presentation, not the outer host's.

    saikai answers Primary DA byte-identically to Windows Terminal, so the env
    must agree on every Windows host — inheriting WT_SESSION only under a real WT
    made the caret-tracking fix a lottery: under WezTerm/conhost the child gets no
    WT identity, falls back to parking its cursor at a fixed base cell, and the
    anchor faithfully pins the IME there. Synthesize it on Windows instead.

    On POSIX the reverse holds: the anchor is _IS_WIN-gated so a WT identity buys
    nothing, while WSL DOES export WT_SESSION into Linux — a pure downside, and the
    exact host-path leak the strip set exists to stop. (#wt-session #wsl)"""
    outer = {"PATH": "/bin", "WT_SESSION": "outer-wt"}

    # Windows under a real WT: the host's session id passes through untouched.
    assert rt._child_pty_env(outer, is_win=True)["WT_SESSION"] == "outer-wt"

    # Windows under WezTerm / conhost: synthesized, stable across panes.
    synth = rt._child_pty_env({"PATH": "/bin"}, is_win=True)["WT_SESSION"]
    assert synth, "a Windows pane must always present a WT identity"
    assert rt._child_pty_env({"PATH": "/bin"}, is_win=True)["WT_SESSION"] == synth

    # POSIX (incl. WSL, where WT exports WT_SESSION into Linux): stripped.
    assert "WT_SESSION" not in rt._child_pty_env(outer, is_win=False)

    # The WezTerm scrub covers the WHOLE family, not two hand-listed keys: a leaked
    # WEZTERM_UNIX_SOCKET lets the child drive the REAL outer window via `wezterm cli`.
    wez = rt._child_pty_env({
        "PATH": "/bin",
        "WEZTERM_EXECUTABLE": "/usr/bin/wezterm",
        "WEZTERM_PANE": "3",
        "WEZTERM_UNIX_SOCKET": "/tmp/wezterm-sock",
        "WEZTERM_CONFIG_FILE": "/home/u/.wezterm.lua",
        "WEZTERM_CONFIG_DIR": "/home/u/.config/wezterm",
        "WEZTERM_EXECUTABLE_DIR": "/usr/bin",
    }, is_win=False)
    assert [k for k in wez if k.startswith("WEZTERM")] == [], wez


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
        t._cursor_hidden_since = 0.0
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

        # 3) repaint at the SAME idle visible cell: anchor unchanged, no extra flush.
        refreshes.clear()
        t._sync_terminal_cursor(reason="repaint")
        assert t._app.cursor_position == rt.Offset(43, 7)
        assert refreshes == [], refreshes

        # 3b) idle VISIBLE cursor moves cell-to-cell (typing) -> anchor FOLLOWS every
        #     repaint (this is the tracking that a cell-stability freeze used to break).
        t._screen.cursor.x = 9
        t._sync_terminal_cursor(reason="repaint")
        assert t._app.cursor_position == rt.Offset(49, 7), "anchor must follow the caret"

        # 3c) cursor HIDDEN on a repaint (mid-redraw) -> FREEZE: anchor stays put.
        t._screen.cursor.x = 20
        t._screen.cursor.hidden = True
        t._sync_terminal_cursor(reason="repaint")
        assert t._app.cursor_position == rt.Offset(49, 7), "hidden cursor must not move the anchor"
        t._screen.cursor.hidden = False

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


def test_paste_marker_strip_is_linear_not_quadratic():
    """Reaching the fixed point must not cost a pass per marker.

    Re-running the regex until the text stops changing removes only ONE marker per
    pass for a nested chain (ESC[20 ESC[20 … 0~ 0~), so a 240KB clipboard of them is
    O(n^2) — and the strip runs on the UI thread inside on_paste, freezing the whole
    TUI for minutes. Strip in one scan instead. (#H3)"""
    import time as _time

    k = 40000
    evil = "\x1b[20" * k + "0~" * k
    t0 = _time.monotonic()
    out = rt._strip_paste_markers(evil)
    elapsed = _time.monotonic() - t0
    assert "\x1b[200~" not in out and "\x1b[201~" not in out, "fixed point not reached"
    assert elapsed < 2.0, f"quadratic strip: {len(evil)} chars took {elapsed:.1f}s"


def test_bracketed_paste_strip_is_idempotent_across_seams():
    """Overlapping marker fragments must not re-form a marker at the deletion seam.
    A single sub() pass scans the ORIGINAL string, so '\\x1b[20' + '\\x1b[201~' + '1~'
    loses the inner marker and the surviving halves concatenate into a brand-new
    ESC[201~ — paste mode ends early and the rest runs as typed-and-submitted
    input (the breakout). Strip to a fixed point, as saikai_mirror.py already
    does on the browser side. (#H3)"""
    evil = "\x1b[20" + "\x1b[201~" + "1~rm -rf /\r"
    wrapped = rt._wrap_bracketed_paste(evil)
    assert wrapped.count("\x1b[201~") == 1, wrapped        # only the closing one
    assert wrapped.endswith("\x1b[201~"), wrapped          # and it is LAST
    assert wrapped.startswith("\x1b[200~"), wrapped
    assert "\x1b[200~" not in wrapped[6:], wrapped         # no re-formed opener
    # A seam that re-forms the OPENING marker is equally fatal (the child sees a
    # second paste start and the bytes before it leave paste mode).
    assert rt._wrap_bracketed_paste("\x1b[20" + "\x1b[200~" + "0~x") == "\x1b[200~x\x1b[201~"
    # Non-overlapping content is unchanged (no over-stripping regression).
    assert rt._wrap_bracketed_paste("safe\x1b[201~\rok") == "\x1b[200~safe\rok\x1b[201~"


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


def test_closing_a_failed_open_block_restores_atomicity_and_clears_in_block():
    """A fail-open must not leave the pane permanently mid-frame.

    The ?2026l that closes a block AFTER a fail-open flipped bypass->outside
    without going through _release, so the clean-frame timestamp was never
    stamped: atomic_at stayed False and the IME anchor stopped tracking through a
    busy storm until some later frame closed cleanly. And in_block reported bypass
    as "still inside a block" with no aging, so a child that abandoned a ?2026h
    answered DECRQM ?2026 as SET for the rest of the pane's life. (#2026)"""
    s = rt._SynchronizedOutputStager(max_chars=1024, max_age=0.2)
    s.push("\x1b[?2026hpartial", now=1.0)
    s.push("more", now=2.0)                       # timeout fail-open -> bypass
    assert s.torn_at(2.0) is True and s.in_block_at(2.0) is True

    s.push("rest\x1b[?2026l", now=2.05)           # the real close finally arrives
    assert s.torn_at(2.05) is False
    assert s.in_block_at(2.05) is False, "the block ended; DECRQM must stop saying SET"
    assert s.atomic_at(2.05) is True, "a closed frame is frame-final state"

    # A bypass whose close never arrives stops claiming the block as well.
    s2 = rt._SynchronizedOutputStager(max_chars=8, max_age=5.0)
    s2.push("\x1b[?2026habcdefghij", now=3.0)     # overflow fail-open
    assert s2.in_block_at(3.0) is True
    assert s2.in_block_at(3.0 + rt._SYNC_BYPASS_TTL + 0.1) is False


def test_sync_output_bypass_rearms_and_atomicity_decays():
    """Neither 'this pane delivers atomic frames' nor 'this pane is mid-tear' may be
    a permanent latch — the IME anchor's freeze decision reads both.

    atomic said 'one frame ever closed cleanly', so a pane that ran claude once kept
    the anti-fly freeze disabled for later UNBRACKETED output that pyte really does
    hold mid-frame. torn was cleared only by a literal ?2026l, so an abandoned block
    froze the anchor forever. Age both, and let a fresh ?2026h during a fail-open
    restart staging: if the child really is re-setting an open block the remainder is
    still released at the real close, and if it abandoned the old one the new frame
    is staged properly. (#ime-midframe #2026)"""
    s = rt._SynchronizedOutputStager(max_chars=1024, max_age=0.2)
    assert s.push("\x1b[?2026hA\x1b[?2026l", now=1.0) == [("\x1b[?2026hA\x1b[?2026l", None)]
    assert s.atomic_at(1.0) is True
    assert s.atomic_at(1.0 + rt._SYNC_ATOMIC_TTL + 0.1) is False, "atomicity must age out"

    assert s.push("\x1b[?2026hpartial", now=2.0) == []
    torn_units = s.push("more", now=2.5)                  # > max_age -> fail-open
    assert torn_units and torn_units[0][1] == "timeout", torn_units
    assert s.torn_at(2.5) is True

    units = s.push("\x1b[?2026hnext-frame", now=2.6)      # child starts a fresh frame
    assert units == [], units
    assert s.active is True, "a new BSU during bypass must re-arm staging"
    assert s.torn_at(2.6) is False
    closed = s.push("\x1b[?2026l", now=2.65)
    assert closed and closed[0][1] is None, closed

    s2 = rt._SynchronizedOutputStager(max_chars=8, max_age=5.0)
    s2.push("\x1b[?2026habcdefghij", now=3.0)             # overflow fail-open
    assert s2.torn_at(3.0) is True
    assert s2.torn_at(3.0 + rt._SYNC_BYPASS_TTL + 0.1) is False, "bypass must age out"


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
    assert sent == ["\x1b[?61;4;6;7;14;21;22;23;24;28;32;42;52c"]


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


def test_every_query_is_answered_at_its_own_position_and_in_stream_order():
    """One chunk, one ordered set of replies.

    Three defects met here. (1) DECRQM was answered from mode state _consume had
    already applied for the WHOLE chunk, so a child that asks "is SGR mouse on?"
    and only then enables it was told it was already on — and a save/restore child
    then skips the restore and leaves mouse reporting armed. (2) Replies crossed
    class boundaries out of order: the static answerer ran before the feed, so
    `\\x1b[6n\\x1b[c` — the canonical "query, then CSI c as end-of-replies
    sentinel" probe — answered the sentinel first. (3) Primary/Secondary DA used
    search(), so a second probe in the same chunk got nothing. (#term-queries)"""
    import pyte

    def _mk():
        t = rt.AgentTerminal(["agent"], status_classifier=lambda _t, _title: "idle")
        t._screen = rt._HistoryScreenBase(30, 6, history=20)
        t._stream = pyte.Stream(t._screen)
        t._sync_output = rt._SynchronizedOutputStager()
        t.sent = []
        t._send_to_child = t.sent.append
        t._marshal = lambda fn: fn()
        return t

    # (1) mode state as of the query, not after the chunk.
    t = _mk()
    t._consume("\x1b[?1006$p\x1b[?1006h")
    assert "".join(t.sent) == "\x1b[?1006;2$y", t.sent
    assert t._mouse_sgr is True, "the set still applies for the rest of the chunk"

    t = _mk()
    t._consume("\x1b[?2004h")
    t.sent.clear()
    t._consume("\x1b[?2004$p\x1b[?2004l")
    assert "".join(t.sent) == "\x1b[?2004;1$y", t.sent
    assert t._bracketed_paste is False

    # ...and a query after the change sees the change.
    t = _mk()
    t._consume("\x1b[?1003h\x1b[?1003$p")
    assert "".join(t.sent) == "\x1b[?1003;1$y", t.sent

    # (2) stream order across query classes.
    t = _mk()
    t._consume("\x1b[2;3HP\x1b[6n\x1b[c")
    assert "".join(t.sent) == "\x1b[2;4R\x1b[?61;4;6;7;14;21;22;23;24;28;32;42;52c", t.sent

    t = _mk()
    t._consume("\x1b[c\x1b[6n")          # the other order is preserved too
    assert "".join(t.sent) == "\x1b[?61;4;6;7;14;21;22;23;24;28;32;42;52c\x1b[1;1R", t.sent

    # (3) every probe answered, not just the first.
    t = _mk()
    t._consume("\x1b[c hello \x1b[c")
    assert t.sent.count("\x1b[?61;4;6;7;14;21;22;23;24;28;32;42;52c") == 2 or \
        "".join(t.sent).count("\x1b[?61;") == 2, t.sent
    t = _mk()
    t._consume("\x1b[>c aa \x1b[>c")
    assert "".join(t.sent).count("\x1b[>0;10;1c") == 2, t.sent

    # ?1004 focus reporting is implemented — the pane really sends \x1b[I / \x1b[O
    # — so DECRQM must not answer "not recognised".
    t = _mk()
    t._consume("\x1b[?1004$p")
    assert "".join(t.sent) == "\x1b[?1004;2$y", t.sent
    t.sent.clear()
    t._consume("\x1b[?1004h")
    t._consume("\x1b[?1004$p")
    assert "".join(t.sent) == "\x1b[?1004;1$y", t.sent


def test_query_split_does_not_classify_or_tee_half_drawn_frames():
    """Splitting the pyte feed at a query must stay invisible above pyte.

    _feed_presentation_unit feeds one unit in segments so each DSR-6 reports the
    cursor at its own position, but _consume_ready ends by classifying the screen,
    bumping the cache version and teeing to the browser — so a chunk with two
    probes classified three half-drawn screens, flipped the pane to 'busy' off a
    partially erased frame (busy is reported with no debounce), and sent the
    mirror three frames where one chunk arrived. Do that trailing work once per
    unit. (#term-queries #pane-direct)"""
    import pyte

    t = rt.AgentTerminal(["agent"], status_classifier=lambda _txt, _title: "idle")
    t._screen = rt._HistoryScreenBase(30, 4, history=20)
    t._stream = pyte.Stream(t._screen)
    t._sync_output = rt._SynchronizedOutputStager()
    t._marshal = lambda fn: fn()
    t._send_to_child = lambda data: None

    seen = []
    t._classify = lambda text, title: (seen.append(text), "idle")[1]
    teed = []
    t._mirror_tee = teed.append
    versions = []

    chunk = "\x1b[2J\x1b[HHALF\x1b[6n MORE\x1b[6n DONE"
    before = t._scr_ver
    t._consume(chunk)
    versions.append(t._scr_ver - before)

    assert len(seen) == 1, [s.strip()[:20] for s in seen]
    assert "DONE" in seen[0], seen[0]
    assert teed == [chunk], teed
    assert versions == [1], versions

    # The replies still report each query's own position — the reason for the
    # split in the first place.
    replies = []
    t._send_to_child = replies.append
    t._consume("\x1b[1;1HAB\x1b[6nCD\x1b[6n")
    assert "".join(replies) == "\x1b[1;3R\x1b[1;5R", replies


def test_cursor_report_uses_the_cursor_at_the_querys_stream_position():
    """A DSR-6 is answered with the cursor AS OF THE QUERY, not after the rest of the
    chunk was drawn. One PTY write can carry 'prompt \\x1b[6n' plus the next lines of
    buffered output; replying with the post-chunk position makes a shell-integration
    wrap probe compute the wrong prompt row. Multiple queries in one chunk must each
    get their own position. (#term-queries)"""
    import pyte

    t = rt.AgentTerminal(["agent"], status_classifier=lambda _txt, _title: "idle")
    t._screen = rt._HistoryScreenBase(40, 8, history=20)
    t._stream = pyte.Stream(t._screen)
    t._sync_output = rt._SynchronizedOutputStager()
    sent = []
    t._send_to_child = lambda data: sent.append(data)
    t._marshal = lambda fn: fn()

    t._consume("\x1b[3;7HAB\x1b[6n\r\nnext line spills onto another row")
    assert sent == ["\x1b[3;9R"], sent          # at the probe, not after 'next line'

    sent.clear()
    t._consume("\x1b[1;1HX\x1b[6n\x1b[5;5HY\x1b[6n")
    assert sent == ["\x1b[1;2R\x1b[5;6R"], sent  # each query at its own position


def test_reader_asks_for_large_reads_and_guards_the_eof_flush():
    """Two properties of the reader loop.

    (1) ptyprocess defaults read() to 1024 bytes, so on POSIX a multi-megabyte turn
        woke the reader ~1000 times per MB, each wakeup paying the whole per-chunk
        pipeline (esc carry, query scans, DEC-private scan, stager push, a coalesced
        refresh marshal). Ask for a real buffer.
    (2) The EOF flush runs before _finalize in the same finally. It feeds pyte and
        then the status classifier — a user-supplied callable — so an exception
        there used to skip _finalize entirely and leave the pane never marked dead.
    (#linux-read-size #eof-flush)"""
    import threading as _th

    reads = []

    class _P:
        def __init__(self):
            self.n = 0
        def read(self, *args, **kwargs):
            reads.append((args, kwargs))
            self.n += 1
            if self.n > 1:
                raise EOFError
            return "out"

    t = rt.AgentTerminal.__new__(rt.AgentTerminal)
    t._pty = _P()
    t._stop = _th.Event()
    t._scroll = 0
    t._frozen = False
    t._consume = lambda _c: False
    t._flush_sync_output = lambda _r: False
    finalized = []
    t._finalize = lambda: finalized.append(True)
    t._read_loop()
    assert reads and reads[0][0], "read() must be given an explicit size"
    assert reads[0][0][0] >= 65536, reads[0]
    assert finalized == [True]

    # a raising EOF flush must not cost us _finalize
    finalized.clear()
    t._pty = _P()
    t._stop = _th.Event()
    def _boom(_reason):
        raise ValueError("classifier blew up during teardown")
    t._flush_sync_output = _boom
    t._read_loop()
    assert finalized == [True], "a failing eof flush must still finalize the pane"


def test_poll_sync_respects_a_frozen_pane_and_flushes_what_it_anchors():
    """The 1.5s host poll reconciles the anchor, so it has to play by the rules.

    (1) A FROZEN pane is mid drag-select: repaints are suppressed so the selection
        survives, so the poll must not move the caret or write to the driver
        underneath it.
    (2) app.cursor_position only reaches the terminal on a CompositorUpdate. The
        poll does not ride one, so a sync that stamps a new anchor and does not
        force a repaint leaves it unflushed — and the next sync computes the same
        cell, sees it unchanged and skips the flush too, so it never lands.
    (#ime-midframe #native-cursor)"""
    import threading as _th

    class _Region:
        x, y, width, height = 0, 0, 40, 6

    class _Drv:
        def __init__(self, sink):
            self._sink = sink
        def write(self, data):
            self._sink.append(data)

    class _App:
        def __init__(self, sink):
            self._driver = _Drv(sink)

    class _Shim(rt.AgentTerminal):
        app = property(lambda self: self._app)
        content_region = property(lambda self: _Region())

    def _term():
        writes, refreshes = [], []
        t = _Shim.__new__(_Shim)
        t.sid = "x"
        t._app = _App(writes)
        t._lock = _th.Lock()
        t._scroll = 0
        t._frozen = False
        t.is_dead = False
        t._status = "idle"
        t._hw_cursor_visible = None
        t._hw_cursor_shape = 0
        t._cursor_style = 0
        t._anchored_xy = None
        t._cursor_hidden_since = 0.0
        t._sync_output = rt._SynchronizedOutputStager()
        t._is_focused_pane = lambda: True
        t.refresh = lambda *a, **k: refreshes.append(k.get("repaint", False))
        import pyte
        t._screen = rt._HistoryScreenBase(20, 5, history=10)
        t._stream = pyte.Stream(t._screen)
        t._alt = type("A", (), {"in_alt": False})()
        t._scr_ver = t._last_poll_ver = 3
        t._pending_status = None
        return t, writes, refreshes

    saved = (rt._IS_WIN, rt._IME_ANCHOR, rt.Offset)
    rt._IS_WIN = True
    rt._IME_ANCHOR = True
    if rt.Offset is None:
        rt.Offset = lambda x, y: (x, y)
    try:
        # (1) frozen: the poll must leave the caret exactly as it is.
        t, writes, _refreshes = _term()
        t._sync_terminal_cursor(reason="focus")
        anchored, writes_before = t._anchored_xy, list(writes)
        assert anchored is not None
        writes.clear()
        t._frozen = True
        t._stream.feed("\x1b[3;7H")            # the live cursor moved underneath
        t.refresh_status()
        assert writes == [], writes
        assert t._anchored_xy == anchored, "a frozen pane must keep its anchor"

        # (2) not frozen: an anchor the poll moves is flushed with a repaint.
        t, writes, refreshes = _term()
        t._sync_terminal_cursor(reason="focus")
        refreshes.clear()
        t._stream.feed("\x1b[4;9H")
        t.refresh_status()
        assert t._anchored_xy == (8, 3), t._anchored_xy
        assert True in refreshes, "a moved anchor needs a repaint to reach the host"
    finally:
        (rt._IS_WIN, rt._IME_ANCHOR, rt.Offset) = saved


def test_queue_overflow_refuses_the_write_and_says_so_in_the_log():
    """A refused write must be RECORDED, outside the lock.

    Once the queue hits _PTY_WRITE_QUEUE_MAX (the child has stopped reading stdin for
    good) _write_child refuses further writes rather than buffering without limit —
    correct, but an unconditional `return` inside the lock covered BOTH the queued and
    the dropped branch, so the `pty write dropped` _log was unreachable. A wedged pane
    then ate every keystroke with nothing in saikai.log to distinguish it from any other
    "my keys don't go through" report. The refusal must also not fall through to an
    inline write (that would hand the child bytes out of order). (#paste-block)"""
    import threading as _th

    written = []

    class _Pty:
        def write(self, data):
            written.append(data)

    logged = []
    _real_log = rt._log
    rt._log = lambda m: logged.append(m)
    try:
        t = rt.AgentTerminal.__new__(rt.AgentTerminal)
        t._pty = _Pty()
        t.is_dead = False
        t._write_lock = _th.Lock()
        t._write_q = collections.deque()
        t._writer = object()                       # a writer is alive -> queue path
        t._write_q_chars = rt._PTY_WRITE_QUEUE_MAX  # ...and the queue is already full

        t._write_child("x" * 100)

        assert list(t._write_q) == [], "an over-cap write must not be buffered"
        assert t._write_q_chars == rt._PTY_WRITE_QUEUE_MAX, "accounting must not drift"
        assert written == [], "a refused write must NOT reach the pty inline"
        assert any("pty write dropped" in m and "100" in m for m in logged), logged
    finally:
        rt._log = _real_log


def test_small_writes_queue_when_the_pty_cannot_take_them():
    """The write path must not block the UI thread for ANY size.

    Routing only >4KiB writes to the writer thread left the docstring's promise
    unmet: a child that stops reading stdin fills the few-KiB POSIX pty buffer
    with ordinary keystrokes, no writer is ever spawned, and the next keystroke
    freezes the Textual event loop — the bug class the queue exists for. Check
    readiness instead of size: when the backend cannot take the bytes now, even a
    single character goes to the writer. (#paste-block)"""
    import threading as _th
    import time as _time

    stuck = _th.Event()
    release = _th.Event()
    order = []

    class _Pty:
        fd = 4242                       # POSIX-shaped: readiness is checkable
        def write(self, data):
            stuck.set()
            release.wait(3.0)
            order.append(data)

    t = rt.AgentTerminal.__new__(rt.AgentTerminal)
    t._pty = _Pty()
    t.is_dead = False
    t._write_lock = _th.Lock()
    t._write_q = collections.deque()
    t._write_q_chars = 0
    t._writer = None

    saved = rt._fd_writable
    rt._fd_writable = lambda fd: False          # the child stopped reading stdin
    try:
        started = _time.monotonic()
        t._write_child("k")                     # ONE character
        assert _time.monotonic() - started < 0.5, "a small write must not block"
        assert stuck.wait(3.0), "it must have gone to the writer thread"
        t._write_child("j")                     # and stay in order behind it
        release.set()
        deadline = _time.monotonic() + 3.0
        while len(order) < 2 and _time.monotonic() < deadline:
            _time.sleep(0.01)
        assert order == ["k", "j"], order
    finally:
        rt._fd_writable = saved
        release.set()

    # When the backend IS ready, a small write still goes out inline — the common
    # path, and what the rest of the suite observes synchronously.
    inline = []
    t2 = rt.AgentTerminal.__new__(rt.AgentTerminal)
    t2._pty = type("P", (), {"write": lambda self, d: inline.append(d)})()
    t2.is_dead = False
    t2._write_lock = _th.Lock()
    t2._write_q = collections.deque()
    t2._write_q_chars = 0
    t2._writer = None
    t2._write_child("x")
    assert inline == ["x"] and t2._writer is None


def test_writer_thread_is_only_latched_once_it_actually_started():
    """A failed Thread.start() must not latch a dead writer.

    _write_child assigned self._writer before starting it, so a start failure
    (resource exhaustion, interpreter shutdown) left a never-started thread as
    the live writer — and the 'queue while a writer is ALIVE' rule then sent every
    later write, including single keystrokes, into a queue nobody drains. The
    writer must also be tracked like every other helper thread so process exit
    bounded-joins it. (#paste-block)"""
    import threading as _th
    import time as _time

    written = []

    class _Pty:
        def write(self, data):
            written.append(data)

    t = rt.AgentTerminal.__new__(rt.AgentTerminal)
    t._pty = _Pty()
    t.is_dead = False
    t._write_lock = _th.Lock()
    t._write_q = collections.deque()
    t._write_q_chars = 0
    t._writer = None

    real_thread = rt.threading.Thread

    class _FailingThread(real_thread):
        def start(self):
            raise RuntimeError("can't start new thread")

    rt.threading.Thread = _FailingThread
    try:
        t._write_child("y" * 8192)          # oversized -> wants the writer
    finally:
        rt.threading.Thread = real_thread
    assert t._writer is None, "a writer that never started must not be latched"
    assert list(t._write_q) == ["y" * 8192], "the data must stay queued, not vanish"

    # ...and the pane recovers: the next write retries the start and drains both,
    # in order.
    t._write_child("z")
    deadline = _time.monotonic() + 3.0
    while len(written) < 2 and _time.monotonic() < deadline:
        _time.sleep(0.01)
    assert written == ["y" * 8192, "z"], [len(x) for x in written]

    # A writer that DOES start is tracked, so join_all_reaps can bound it.
    blocked = _th.Event()
    go = _th.Event()
    t._write_q = collections.deque()
    t._write_q_chars = 0
    t._writer = None
    t._pty = type("P", (), {"write": lambda self, d: (blocked.set(), go.wait(3.0))})()
    t._write_child("w" * 8192)
    assert blocked.wait(3.0)
    with rt._REAP_LOCK:
        tracked = [th for th in rt._REAP_THREADS if th is t._writer]
    go.set()
    assert tracked, "the writer thread must be tracked for the exit join"


def test_large_paste_never_blocks_the_ui_thread_and_keeps_order():
    """A big paste must not freeze the whole TUI.

    on_paste/paste_text write to the PTY synchronously on the UI thread, and a POSIX
    pty input queue is a few KiB — so pasting tens of KiB into a pane whose child is
    momentarily not reading stdin blocks write() and with it the Textual event loop:
    no keys, no repaints, no other pane, until the child drains. Hand oversized
    writes to a daemon writer, and queue everything behind it while it drains so the
    child still receives the bytes in order. Small writes stay inline. (#paste-block)"""
    import threading as _th
    import time as _time

    blocking = _th.Event()
    release = _th.Event()
    order = []

    class _P:
        def write(self, data):
            if len(data) > 100:
                blocking.set()
                release.wait(3.0)
            order.append(data)

    t = rt.AgentTerminal.__new__(rt.AgentTerminal)
    t._pty = _P()
    t.is_dead = False
    t._write_lock = _th.Lock()
    t._write_q = collections.deque()
    t._write_q_chars = 0
    t._writer = None

    # small write: straight out, no thread, observable immediately
    t._write_child("a")
    assert order == ["a"], order

    big = "x" * 20000
    t0 = _time.monotonic()
    t._write_child(big)
    assert _time.monotonic() - t0 < 0.5, "an oversized write must not block the caller"
    assert blocking.wait(3.0), "the writer thread must be draining the queue"

    t._write_child("\r")          # arrives while the big write is stuck
    release.set()
    deadline = _time.monotonic() + 3.0
    while len(order) < 3 and _time.monotonic() < deadline:
        _time.sleep(0.01)
    assert order == ["a", big, "\r"], [len(x) for x in order]

    # queue drained -> the writer retires and small writes are inline again
    deadline = _time.monotonic() + 3.0
    while t._writer is not None and _time.monotonic() < deadline:
        _time.sleep(0.01)
    assert t._writer is None and not t._write_q

    # A child that never drains must not turn the queue into an unbounded buffer:
    # every later keystroke would be swallowed AND retained forever.
    wedge = _th.Event()
    stuck = _th.Event()

    class _Wedged:
        def write(self, data):
            stuck.set()
            wedge.wait(5.0)

    t._pty = _Wedged()
    t._write_child("z" * 5000)
    assert stuck.wait(3.0)
    for _ in range(200):
        t._write_child("k" * 100000)
    queued = sum(len(x) for x in t._write_q)
    assert queued <= rt._PTY_WRITE_QUEUE_MAX, queued
    wedge.set()


def test_dcs_strip_closes_its_own_seam_and_never_dumps_a_payload():
    """Two ways the DCS scrub could hand the grid the bytes it exists to remove.

    (1) Deleting a complete string can leave the characters around the cut
        forming a BRAND NEW ESC P — the same deletion-seam bug the paste-marker
        strip in this module was rewritten to close — and nothing rescans, so the
        counterfeit opener reaches pyte and the browser mirror.
    (2) The runaway cap emitted the rest of the read AND cleared the carried
        'inside' state, so a legitimate sixel frame past the cap (multi-megapixel
        photos exceed 4MiB routinely, and the pane advertises sixel in its DA
        reply) had megabytes drawn into the pane and its scrollback. Keep
        dropping instead; the cap must bound MEMORY, not the strip itself.
    (#dcs-scrub)"""
    seam = "\x1b" + "\x1bPpayload\x1b\\" + "PQR\x1b\\tail"
    clean, inside, _dropped = rt._strip_dcs(seam)
    assert "\x1bP" not in clean, repr(clean)
    assert inside is False

    # Back-to-back strings leave an EMPTY slice between them, so the seam check
    # must look past it to the last text actually emitted.
    adjacent = "A\x1b" + "\x1bPone\x1b\\" + "\x1bPtwo\x1b\\" + "Ptail"
    clean, inside, _dropped = rt._strip_dcs(adjacent)
    assert "\x1bP" not in clean, repr(clean)

    # A seam that re-forms the opener across the carry boundary is caught too.
    first, inside, dropped = rt._strip_dcs("text\x1b")
    second, inside, dropped = rt._strip_dcs("\x1bPhidden\x1b\\after", inside, dropped)
    assert "\x1bP" not in first + second, repr(first + second)

    # Oversized payload: nothing leaks, and the carry survives so the REST of the
    # payload in later reads is dropped too.
    huge = "\x1bPq" + "~" * (rt._DCS_MAX_DROP + 4096)
    clean, inside, dropped = rt._strip_dcs(huge)
    assert clean == "", len(clean)
    assert inside is True, "the cap must not forget that we are inside a DCS"
    clean2, inside, dropped = rt._strip_dcs("MORE-SIXEL~~~", inside, dropped)
    assert clean2 == ""
    assert inside is True
    # ...and the real terminator still ends it, with the tail kept.
    clean3, inside, dropped = rt._strip_dcs("\x1b\\tail", inside, dropped)
    assert clean3 == "tail" and inside is False and dropped == 0

    # Ordinary text is untouched and a complete string still vanishes.
    assert rt._strip_dcs("A\x1bPq#0;2\x1b\\B")[0] == "AB"
    assert rt._strip_dcs("plain text only")[0] == "plain text only"


def test_dcs_payloads_never_reach_the_grid():
    """A DCS string (sixel image, DECRQSS, XTGETTCAP) must not print as text.

    pyte has no DCS handler, so it draws the payload body into the grid — and the
    pane now advertises sixel in its Windows-Terminal Primary DA reply, so any
    auto-detecting image tool (chafa, timg, img2sixel, lsix) will send one and fill
    the pane and its scrollback with garbage. Strip complete strings, and hold the
    'inside a DCS' state across read boundaries so a payload split by the PTY can't
    leak its tail. (#dcs-scrub)"""
    import pyte

    def _mk():
        t = rt.AgentTerminal(["agent"], status_classifier=lambda _txt, _title: "idle")
        t._screen = rt._HistoryScreenBase(30, 4, history=20)
        t._stream = pyte.Stream(t._screen)
        t._sync_output = rt._SynchronizedOutputStager()
        t._marshal = lambda fn: None
        return t

    def _text(t):
        return "".join(rt._pyte_grid_lines(t._screen)).strip()

    t = _mk()
    t._consume("A\x1bPq#0;2;0;0;0#0~~@@vv@@~~\x1b\\B")
    assert _text(t) == "AB", _text(t)

    t = _mk()                                     # split across PTY reads
    t._consume("C\x1bPq#0;2;0;0;0")
    t._consume("~~@@vv@@~~")
    t._consume("\x1b\\D")
    assert _text(t) == "CD", _text(t)

    t = _mk()                                     # BEL-terminated DCS
    t._consume("E\x1bP+q544e\x07F")
    assert _text(t) == "EF", _text(t)

    t = _mk()                                     # a lone ESC P mid-text stays inert
    t._consume("plain text only")
    assert _text(t) == "plain text only", _text(t)


def test_decrqm_reports_the_modes_saikai_actually_implements():
    """A pane that answers DA1 as Windows Terminal must not tell a child 'mode not
    recognised' for modes it honours. A child using the set-then-verify pattern reads
    ?2004;0 and never enables bracketed paste — so a multi-line paste is submitted
    line by line, the exact hazard the paste wrapper exists to prevent — and reads
    ?1006;0 and falls back to legacy mouse encoding. Report the tracked state
    instead, and keep 0 for modes we genuinely do not implement. (#term-queries)"""
    import pyte

    t = rt.AgentTerminal(["agent"], status_classifier=lambda _txt, _title: "idle")
    t._screen = rt._HistoryScreenBase(20, 5, history=20)
    t._stream = pyte.Stream(t._screen)
    t._sync_output = rt._SynchronizedOutputStager()
    sent = []
    t._send_to_child = lambda data: sent.append(data)
    t._marshal = lambda fn: fn()

    def _reply(query):
        sent.clear()
        t._answer_queries(query)          # already-fed-chunk path: mode state included
        return sent[-1] if sent else None

    assert _reply("\x1b[?2004$p") == "\x1b[?2004;2$y"      # implemented, reset
    t._consume("\x1b[?2004h")                              # child enables it
    assert t._bracketed_paste is True
    assert _reply("\x1b[?2004$p") == "\x1b[?2004;1$y"      # implemented, SET
    t._consume("\x1b[?1006h\x1b[?1002h")
    assert _reply("\x1b[?1006$p") == "\x1b[?1006;1$y"
    assert _reply("\x1b[?1002$p") == "\x1b[?1002;1$y"
    assert _reply("\x1b[?1003$p") == "\x1b[?1003;2$y"
    assert _reply("\x1b[?25$p") == "\x1b[?25;1$y"          # DECTCEM: cursor visible
    t._consume("\x1b[?25l")
    assert _reply("\x1b[?25$p") == "\x1b[?25;2$y"
    assert _reply("\x1b[?9999$p") == "\x1b[?9999;0$y"      # genuinely unknown

    # ?2026 is SET while a frame is staged — a child that verifies its BSU took
    # effect must not be told the mode is reset while we are holding its frame.
    assert _reply("\x1b[?2026$p") == "\x1b[?2026;2$y"
    t._consume("\x1b[?2026hmid-frame")
    assert t._sync_output.active is True
    assert _reply("\x1b[?2026$p") == "\x1b[?2026;1$y"
    t._consume("\x1b[?2026l")

    # A mode set and verified in the SAME write must report the state AFTER the set:
    # the query is answered at its own stream position, like DSR-6, not from the
    # state the chunk started with. (#term-queries)
    sent.clear()
    t._consume("\x1b[?1003h\x1b[?1003$p")
    assert sent == ["\x1b[?1003;1$y"], sent
    sent.clear()
    t._consume("\x1b[?2026hframe-body\x1b[?2026$p")
    assert sent == ["\x1b[?2026;1$y"], sent   # a child verifying its own BSU took effect


def test_cursor_report_clamps_pending_wrap_and_honours_origin_mode():
    """Two CPR coordinate bugs a child can act on.

    (1) After text exactly fills a row pyte parks the cursor at column == columns
        (the pending-wrap state), so a raw +1 reports a column that does not exist;
        real terminals report the LAST column until the wrap actually happens.
    (2) With origin mode (DECOM) set the report is margin-relative — that is the
        whole point of DECOM — so a full-screen app with a scroll region gets told
        it is outside its own region and repositions its status line one row off.
    (#term-queries)"""
    import pyte

    t = rt.AgentTerminal(["agent"], status_classifier=lambda _txt, _title: "idle")
    t._screen = rt._HistoryScreenBase(10, 6, history=20)
    t._stream = pyte.Stream(t._screen)
    t._sync_output = rt._SynchronizedOutputStager()
    t._marshal = lambda fn: None

    t._consume("\x1b[1;1H" + "x" * 10)               # row exactly full -> pending wrap
    assert t._screen.cursor.x == 10, "precondition: pyte parks at columns"
    assert t._cursor_rowcol() == (1, 10)

    t._consume("\x1b[3;5r\x1b[?6h\x1b[H")            # region rows 3..5, origin mode
    assert t._cursor_rowcol() == (1, 1), "DECOM makes the report margin-relative"
    t._consume("\x1b[2;3H")
    assert t._cursor_rowcol() == (2, 3)


def test_cursor_query_fail_open_only_for_a_RETAINED_query():
    """The fail-open that releases a staged frame early must fire only when the query
    is inside the RETAINED text. A ?6n that arrives BEFORE the ?2026h in the same PTY
    write is already answerable from the plain prefix, so tearing the just-opened
    frame for it re-exposes the mid-frame state the stager exists to hide — and
    claude's private-?6n redraw probe emits exactly probe-then-frame. (#2026)"""
    import pyte

    t = rt.AgentTerminal(["agent"], status_classifier=lambda _txt, _title: "idle")
    t._screen = rt._HistoryScreenBase(40, 8, history=20)
    t._stream = pyte.Stream(t._screen)
    t._sync_output = rt._SynchronizedOutputStager()
    sent = []
    t._send_to_child = lambda data: sent.append(data)
    t._marshal = lambda fn: fn()

    # probe THEN a frame opens: answer the probe, keep staging (no tear).
    t._consume("\x1b[2;3HP\x1b[6n\x1b[?2026h\x1b[?25l\x1b[Hpartial")
    assert sent == ["\x1b[2;4R"], sent
    assert t._sync_output.active is True, "the new frame must still be staged"
    assert t._sync_output.torn_at(time.monotonic()) is False

    # a query INSIDE the retained frame still fails open (the child may be waiting
    # on the reply before it emits ?2026l).
    sent.clear()
    t._consume("\x1b[6n")
    assert sent and sent[0].endswith("R"), sent
    assert t._sync_output.torn_at(time.monotonic()) is True


def test_sync_output_eof_flushes_retained_frame_once():
    import pyte

    t = rt.AgentTerminal(["agent"], status_classifier=lambda _txt, _title: "idle")
    t._screen = rt._HistoryScreenBase(20, 5, history=20)
    t._stream = pyte.Stream(t._screen)
    t._sync_output = rt._SynchronizedOutputStager()
    t._marshal = lambda fn: None

    assert t._consume("\x1b[?2026hEOF-TEXT") is False
    assert t._flush_sync_output("eof") is True
    assert "EOF-TEXT" in "\n".join(rt._pyte_grid_lines(t._screen))
    assert t._flush_sync_output("eof") is False


def test_sync_output_mirror_gets_closed_block_once_in_order():
    import pyte

    t = rt.AgentTerminal(["agent"], status_classifier=lambda _txt, _title: "idle")
    t._screen = rt._HistoryScreenBase(20, 5, history=20)
    t._stream = pyte.Stream(t._screen)
    t._sync_output = rt._SynchronizedOutputStager()
    t._marshal = lambda fn: None
    mirrored = []
    t._mirror_tee = lambda chunk: mirrored.append(chunk)

    assert t._consume("pre\x1b[?2026hA") is True
    assert mirrored == ["pre"]
    assert t._consume("B\x1b[?2026lpost") is True
    assert mirrored == ["pre", "\x1b[?2026hAB\x1b[?2026l", "post"]


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
    t._dcs_inside = False
    t._dcs_dropped = 0
    t._mouse_click = False
    t._mouse_btn_motion = False
    t._mouse_any_motion = False
    t._app_cursor = False
    t._focus_reporting = False
    t._bracketed_paste = False
    t._sync_output = rt._SynchronizedOutputStager()
    t._marshal = lambda fn: fn()
    t._send_to_child = lambda data: None
    t._mouse_reporting = False
    t._mouse_sgr = False
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
    # Primary DA: reply byte-identical to the outer Windows Terminal (VT500-class),
    # so Claude Code sees the same terminal it does when run directly and uses its
    # cursor-tracking path instead of parking the cursor. (#agents-cursor #wt-da)
    assert _one("\x1b[c") == "\x1b[?61;4;6;7;14;21;22;23;24;28;32;42;52c"
    assert _one("\x1b[0c") == "\x1b[?61;4;6;7;14;21;22;23;24;28;32;42;52c"
    assert _one("\x1b[?6n") == "\x1b[?3;7R"       # private cursor-position reply
    assert _one("\x1b[6n") == "\x1b[3;7R"         # standard cursor-position reply
    assert _one("\x1b[5n") == "\x1b[0n"           # device status OK
    assert _one("\x1b[?2026$p") == "\x1b[?2026;2$y"    # synchronized output, not in a block
    assert _one("\x1b[?1000$p") == "\x1b[?1000;2$y"    # implemented, currently reset
    assert _one("\x1b[?9999$p") == "\x1b[?9999;0$y"    # genuinely unknown mode
    assert _one("\x1b[>0q") is None               # XTVERSION: silent, exactly like WT
    assert _one("\x1b]11;?\x07") == "\x1b]11;rgb:1e1e/1e1e/1e1e\x07"  # bg (dark)
    assert _one("\x1b]10;?\x07") == "\x1b]10;rgb:c0c0/c0c0/c0c0\x07"  # fg (light)
    # The reply must carry the terminator the child ASKED with: an ST-terminated
    # query answered with BEL leaves a strict parser waiting for its ST (and the
    # stray BEL rings the bell). (#term-queries)
    assert _one("\x1b]11;?\x1b\\") == "\x1b]11;rgb:1e1e/1e1e/1e1e\x1b\\"
    assert _one("\x1b]10;?\x1b\\") == "\x1b]10;rgb:c0c0/c0c0/c0c0\x1b\\"
    # Secondary DA (vim's t_RV, tmux). A pane that claims to BE Windows Terminal must
    # answer it — WT does. Leaving it silent stalls the child's probe until it times
    # out and it then mis-detects version-gated features. (#wt-da2)
    assert _one("\x1b[>c") == "\x1b[>0;10;1c"
    assert _one("\x1b[>0c") == "\x1b[>0;10;1c"
    # Replies follow the order the child ASKED in. Primary DA used to be emitted
    # first unconditionally, so a probe batch that ends with CSI c — the common
    # "answer everything you know, then this sentinel" shape — got the sentinel
    # first and a strict parser mismatched every reply after it. (#term-queries)
    assert _one("\x1b]11;?\x07\x1b[>c\x1b[c") == (
        "\x1b]11;rgb:1e1e/1e1e/1e1e\x07" "\x1b[>0;10;1c" "\x1b[?61;4;6;7;14;21;22;23;24;28;32;42;52c")
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
    # the tee passes the chunk's BYTES verbatim and in order — the child-query strip
    # runs on the mirror hub's drain thread (set_pane_strip), never on the reader
    # thread under the terminal lock (#review-strip-offload). Write boundaries are
    # not part of the contract: a DSR-6 splits the pyte feed so the reply reports the
    # cursor at the query, and the tee rides that feed. (#term-queries)
    assert "".join(tees[1:]) == "plain \x1b[6n text \x1b[0c and \x1b[?2026$p done", \
        f"tee must be verbatim (strip is drain-side): {tees[1:]!r}"
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


def test_cursor_anchor_settles_hidden_and_tracks_atomic_frames():
    """Three anchor-gating rules the flat busy-or-hidden freeze got wrong.

    (1) A hidden cursor may only freeze the anchor TRANSIENTLY. The freeze returns
        before the hide path, so a child that parks in a no-cursor view (htop, a
        picker) while its status stays 'idle' never gets ?25l — the native cursor
        blinks on at the stale anchored cell indefinitely. Settle after a beat.
    (2) 'busy' must not freeze tracking while the ?2026 stager presents ATOMIC
        frames: every repaint then observes frame-final state, i.e. the real
        caret, so CJK typed into a generating pane must still move the anchor.
    (3) A TORN stager (fail-open on timeout/overflow/cursor-query, then bypass
        until the block closes) leaks exactly the mid-frame Home/header cursor
        the stager exists to hide — freeze then, even when NOT busy.
    (#agents-cursor #ime-midframe)"""
    import threading as _th

    class _Cursor:
        def __init__(self):
            self.x = 3; self.y = 2; self.hidden = False

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

    def _term(status="idle"):
        writes = []
        t = _Shim.__new__(_Shim)
        t.sid = "x"
        t._app = _App(writes)
        t._lock = _th.Lock()
        t._screen = _Screen()
        t._alt = _Alt()
        t._scroll = 0
        t.is_dead = False
        t._status = status
        t._hw_cursor_visible = None
        t._cursor_hidden_since = 0.0
        t._anchored_xy = None
        t._cursor_hidden_since = 0.0
        t._sync_output = rt._SynchronizedOutputStager()
        t._is_focused_pane = lambda: True
        t.refresh = lambda *a, **k: None
        return t, writes

    old_win, old_anchor, old_offset = rt._IS_WIN, rt._IME_ANCHOR, rt.Offset
    rt._IS_WIN = True
    rt._IME_ANCHOR = True
    if rt.Offset is None:
        rt.Offset = lambda x, y: (x, y)
    try:
        # (1) hidden: transient freeze, then a settled hide that drops the anchor.
        t, writes = _term()
        t._sync_terminal_cursor(now=100.0)
        assert writes == ["\x1b[?25h"] and t._anchored_xy == (43, 7), writes
        writes.clear()
        t._screen.cursor.hidden = True
        t._sync_terminal_cursor(now=100.05)           # mid-redraw ?25l -> hold
        assert writes == [], writes
        assert t._anchored_xy == (43, 7), "a transient hide must not drop the anchor"
        t._sync_terminal_cursor(now=100.6)            # still hidden -> real no-cursor
        assert writes == ["\x1b[?25l"], writes
        assert t._anchored_xy is None
        # coming back visible re-arms the settle window
        writes.clear()
        t._screen.cursor.hidden = False
        t._sync_terminal_cursor(now=100.7)
        assert writes == ["\x1b[?25h"], writes

        # (2) busy + atomic frames -> the anchor still TRACKS the caret.
        t, writes = _term(status="busy")
        t._sync_output.push("\x1b[?2026hA\x1b[?2026l", now=199.9)  # one complete frame
        assert t._sync_output.atomic_at(200.0) is True
        assert t._sync_output.torn_at(200.0) is False
        t._sync_terminal_cursor(now=200.0)
        assert t._app.cursor_position == rt.Offset(43, 7)
        t._screen.cursor.x = 9
        t._sync_terminal_cursor(now=200.1)
        assert t._app.cursor_position == rt.Offset(49, 7), "atomic frames must track"

        # (2b) busy WITHOUT bracketed frames -> the storm freeze still applies.
        t, writes = _term(status="busy")
        assert t._sync_output.atomic_at(200.0) is False
        t._sync_terminal_cursor(now=200.0)
        assert getattr(t._app, "cursor_position", None) is None

        # (3) torn stager while merely 'waiting' -> freeze (mid-frame cursor).
        t, writes = _term(status="waiting")
        t._sync_output.push("\x1b[?2026hA\x1b[?2026l", now=199.5)
        t._sync_terminal_cursor(now=199.6)            # atomic: anchors normally
        assert t._app.cursor_position == rt.Offset(43, 7)
        t._sync_output.push("\x1b[?2026hpartial", now=199.7)
        t._sync_output.push("more", now=200.0)        # > max_age -> fail-open -> bypass
        assert t._sync_output.torn_at(200.05) is True
        t._screen.cursor.x = 0; t._screen.cursor.y = 0        # mid-frame Home
        t._sync_terminal_cursor(now=200.05)
        assert t._app.cursor_position == rt.Offset(43, 7), "a torn frame must not move it"

        # (4) scrolled back: the anchored cell now sits on unrelated history, so the
        # native cursor must be HIDDEN and the anchor dropped — not left blinking
        # mid-scrollback with the IME opening there. Coming back to live must RESTORE
        # it without waiting for output: the reader suppresses repaints while
        # scrolled, so an idle pane would otherwise stay IME-dead. (#ime-scrollback)
        t, writes = _term()
        t._screen.history = type("H", (), {"top": [0] * 50})()
        t._sync_terminal_cursor(now=300.0)
        assert writes == ["\x1b[?25h"] and t._anchored_xy == (43, 7), writes
        writes.clear()
        t._scroll = 4
        t._sync_terminal_cursor(now=300.1)
        assert writes == ["\x1b[?25l"], writes
        assert t._anchored_xy is None

        writes.clear()
        t._snap_to_live()                             # typing / paste returns to live
        assert writes == ["\x1b[?25h"], writes
        assert t._anchored_xy == (43, 7)

        # (5) a child that hides its cursor and then goes QUIET still settles: the
        # settle can only be reached from a sync, and repaints are driven by output,
        # so the host's periodic poll has to reconcile it. (#ime-midframe)
        t, writes = _term()
        t._sync_terminal_cursor(now=400.0)
        writes.clear()
        t._screen.cursor.hidden = True
        t._sync_terminal_cursor(now=400.05)           # transient hold, then silence
        assert writes == [], writes
        t._cursor_hidden_since = time.monotonic() - (rt._NATIVE_CURSOR_HIDE_SETTLE + 0.5)
        t._scr_ver = t._last_poll_ver = 7             # nothing new to classify
        t._pending_status = None
        t.refresh_status()
        assert writes == ["\x1b[?25l"], writes
        assert t._anchored_xy is None

        t, writes = _term()
        # Leaving scrollback restores the native cursor, but must NOT plant the
        # anchor on a cursor the mid-frame gate would have rejected: the three
        # leave-scrollback paths used reason="focus", which skips that gate
        # entirely, so wheeling back during a torn frame anchored composition on
        # the frame's Home position. (#ime-scrollback #ime-midframe)
        t, writes = _term(status="busy")
        t._screen.history = type("H", (), {"top": [0] * 50})()
        t._sync_output.push("\x1b[?2026hA\x1b[?2026l", now=599.9)   # atomic so far
        t._sync_terminal_cursor(now=600.0)
        anchored = t._app.cursor_position
        writes.clear()
        t._scroll = 5
        t._sync_terminal_cursor(now=600.1)
        assert writes == ["\x1b[?25l"], writes
        writes.clear()
        t._sync_output.push("\x1b[?2026hpartial", now=600.2)
        t._sync_output.push("more", now=600.9)        # fail-open -> torn
        t._screen.cursor.x = 0                         # mid-frame Home
        t._snap_to_live()
        assert writes == ["\x1b[?25h"], "the pane is live again: show the caret"
        assert t._app.cursor_position == anchored, "a torn cursor must not move it"

        # A resize that leaves scrollback must re-anchor like every other path
        # back to live: _snap_to_live and the wheel handler do, and a pane that
        # only got here by resizing was left with the native cursor hidden until
        # the 1.5s poll happened to tick. (#ime-scrollback)
        t, writes = _term()
        t._screen.history = type("H", (), {"top": [0] * 50})()
        t._sync_terminal_cursor(now=500.0)
        writes.clear()
        t._scroll = 6
        t._sync_terminal_cursor(now=500.1)
        assert writes == ["\x1b[?25l"], writes
        writes.clear()
        t._dims = lambda: (6, 40)
        t._ensure_screen_pair_locked = lambda: None
        t._main_screen = t._alt_screen = t._screen
        t._invalidate_screen_grapheme = lambda _s: None
        t._pty = None
        t.on_resize(type("E", (), {})())
        assert t._scroll == 0
        assert writes == ["\x1b[?25h"], writes

        t, writes = _term()
        t._screen.history = type("H", (), {"top": [0] * 50})()
        writes.clear()                                # wheeling back down, no input
        t._scroll = 3
        t._sync_terminal_cursor(now=300.2)
        assert writes == ["\x1b[?25l"], writes
        writes.clear()
        ev = type("E", (), {"stop": lambda self: None})()
        for _ in range(2):
            t.on_mouse_scroll_down(ev)
        assert t._scroll == 0 and writes == ["\x1b[?25h"], (t._scroll, writes)
    finally:
        rt._IS_WIN, rt._IME_ANCHOR, rt.Offset = old_win, old_anchor, old_offset


def test_cursor_anchor_freezes_on_busy_or_hidden_follows_visible():
    """Anti-fly WITHOUT breaking tracking: a per-repaint sync FREEZES while the pane is
    'busy' (an agent-mode storm sweeps the pyte cursor Home->prompt across ~170k frames)
    or while the child HID its cursor (?25l mid-redraw). Otherwise it FOLLOWS the visible
    cursor every repaint, because claude moves the terminal cursor to the real input
    caret and the IME anchor must track it. The sync stays INLINE on the repaint (rides a
    CompositorUpdate = flushes); the debounce timer and the cell-stability gate (which
    also froze legitimate tracking) are gone. (#agents-cursor)"""
    import inspect
    refresh_src = inspect.getsource(rt.AgentTerminal._do_pane_refresh)
    assert "_sync_terminal_cursor" in refresh_src, \
        "_do_pane_refresh must sync the cursor inline (rides the repaint = flushes)"
    assert "_schedule_terminal_cursor_sync" not in refresh_src, \
        "the debounce timer indirection must be gone (it starved + never flushed)"
    sync_src = inspect.getsource(rt.AgentTerminal._sync_terminal_cursor)
    midframe_src = inspect.getsource(rt.AgentTerminal._cursor_may_be_midframe)
    assert "_cursor_may_be_midframe" in sync_src and "cursor_hidden" in sync_src, \
        "the repaint freeze must gate on mid-frame OR a hidden cursor, not cell-stability"
    assert '"busy"' in midframe_src, "the storm freeze must still key on 'busy'"
    assert "_prev_repaint_cell" not in sync_src, \
        "the cell-stability gate (which froze legitimate input tracking) must be gone"
    assert not hasattr(rt.AgentTerminal, "_schedule_terminal_cursor_sync"), \
        "the debounce timer machinery must be removed"


def test_only_a_focus_sync_forces_the_cursor_visibility_write():
    """The FREQUENT syncs (poll / repaint) must NOT re-assert ?25h on every tick.

    force= was `reason != "repaint"`, so the poll — which runs forever — re-wrote ?25h
    on EVERY tick even with nothing changed. Hovering the session list moves focus to the
    list, saikai hides the cursor, and the next poll immediately forced it back on: the
    native cursor visibly blinked between the list and the pane. Now only the INFREQUENT
    event syncs force: 'focus' (window/pane refocus) and 'settle' (agent storm end) — so a
    visibility that drifted while WT hid the cursor still heals — while 'poll'/'repaint'
    are suppressed whenever visibility already matches. (#native-cursor #wt-hover)"""
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
        writes = []
        t = _Shim.__new__(_Shim)
        t.sid = "x"
        t._app = _App(writes)
        t._lock = _th.Lock()
        t._screen = _Screen()
        t._alt = _Alt()
        t._scroll = 0
        t.is_dead = False
        t._status = "idle"
        t._hw_cursor_visible = None
        t._anchored_xy = None
        t._frozen = False
        t._is_focused_pane = lambda: True
        t.refresh = lambda *a, **k: None

        t._sync_terminal_cursor(reason="repaint")      # first show
        assert writes == ["\x1b[?25h"], writes

        # Repeated polls with nothing changed must write NOTHING (this is the blink).
        for _ in range(5):
            t._sync_terminal_cursor(reason="poll")
        assert writes == ["\x1b[?25h"], f"poll must not re-assert ?25h: {writes}"

        # A repaint at the same visible cell is likewise suppressed.
        t._sync_terminal_cursor(reason="repaint")
        assert writes == ["\x1b[?25h"], f"repaint must not re-assert ?25h: {writes}"

        # A settle sync (storm end) DOES force one re-assertion (heals a drifted state).
        t._sync_terminal_cursor(reason="settle")
        assert writes == ["\x1b[?25h", "\x1b[?25h"], f"settle must force: {writes}"

        # A focus sync also forces (WT may have dropped visibility on refocus).
        t._sync_terminal_cursor(reason="focus")
        assert writes == ["\x1b[?25h"] * 3, f"focus must force: {writes}"
    finally:
        rt._IS_WIN, rt._IME_ANCHOR, rt.Offset = old_win, old_anchor, old_offset


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

def _fake_char(d):
    """Minimal stand-in for a pyte Char: _cell_style reads every other attribute
    with a defaulted getattr, so .data is all render_line actually needs."""
    class _C:
        __slots__ = ("data",)
    c = _C()
    c.data = d
    return c


def _grid_pane(rows=4, cols=3, fill="a"):
    """A ClaudeTerminal wired with just what render_line / _build_frame touch.

    size / has_focus are read-only properties on Textual's Widget, so shadow them
    on a throwaway subclass rather than assigning to the instance."""
    class _Size:
        width = cols
        height = rows

    class _Pane(rt.ClaudeTerminal):
        size = _Size()
        has_focus = False

    ct = _Pane.__new__(_Pane)
    ct._lock = threading.Lock()
    ct._scroll = 0
    ct._frozen = False
    ct._frozen_buf = None
    ct._sel_anchor = None
    ct._sel_head = None
    ct.is_dead = False
    ct._spawn_error = None
    ct._frame = None

    class _Cursor:
        x = 0
        y = 0
        hidden = True      # keep saikai's own cursor cell out of the comparison

    class _Screen:
        columns = cols
        lines = rows
        cursor = _Cursor()

        def __init__(self):
            self.buffer = {y: [_fake_char(fill) for _ in range(cols)]
                           for y in range(rows)}

    ct._screen = _Screen()
    return ct


def _row_text(ct, y):
    return "".join(seg.text for seg in ct.render_line(y))


def test_render_frame_pins_one_pyte_generation():
    """A frame must show ONE pyte generation. Textual renders row by row
    (_styles_cache loops render_line per y) and render_line used to take
    self._lock PER ROW, while the reader installs a whole child frame atomically
    under that same lock — so a frame could splice rows from two generations:
    positions right, contents stale, rows visibly out of place until the next
    repaint healed them. render_lines pins the grid once instead."""
    ct = _grid_pane(rows=4, cols=3, fill="a")
    ct._build_frame()                      # frame boundary: generation "a" pinned

    # The reader lands mid-frame and replaces the whole screen with generation
    # "b", holding the lock for the whole feed exactly as _consume does.
    with ct._lock:
        for y in range(4):
            ct._screen.buffer[y] = [_fake_char("b") for _ in range(3)]

    rows = [_row_text(ct, y) for y in range(4)]
    assert rows == ["aaa"] * 4, f"torn frame: {rows}"

    # A _frozen flip mid-frame must not mix pinned and live rows either (this is
    # the drag-select-breaks-the-display case).
    ct._frozen = True
    ct._frozen_buf = {y: [_fake_char("c") for _ in range(3)] for y in range(4)}
    assert [_row_text(ct, y) for y in range(4)] == ["aaa"] * 4

    # The next frame boundary picks up the new state (here: the freeze snapshot).
    # Row 0 is the frozen banner by design — and it is pinned WITH the grid, so a
    # frame can't show the banner over rows built from the un-frozen state.
    ct._build_frame()
    rows = [_row_text(ct, y) for y in range(4)]
    assert "❄" in rows[0], rows[0]      # banner, truncated to the pane's width
    assert rows[1:] == ["ccc"] * 3, rows

    # No frame pinned (a render_line outside a frame) → locked live fallback.
    ct._frame = None
    ct._frozen = False
    ct._frozen_buf = None
    assert [_row_text(ct, y) for y in range(4)] == ["bbb"] * 4


def test_render_takes_the_pane_lock_once_per_frame():
    """The per-row lock was also the cost: N rows meant N acquisitions, each one a
    chance for the reader to slip in. One acquisition per frame, not per row."""
    ct = _grid_pane(rows=10, cols=3)
    real = ct._lock
    acquires = []

    class _CountingLock:
        def __enter__(self):
            acquires.append(1)
            return real.__enter__()

        def __exit__(self, *a):
            return real.__exit__(*a)

    ct._lock = _CountingLock()
    ct._build_frame()
    for y in range(10):
        ct.render_line(y)
    assert acquires == [1], f"expected 1 acquisition per frame, got {len(acquires)}"


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
    test_marshal_runs_inline_on_the_apps_own_thread()
    print("PASS test_marshal_runs_inline_on_the_apps_own_thread")
    test_focus_gate_yields_to_a_modal_screen_on_top()
    print("PASS test_focus_gate_yields_to_a_modal_screen_on_top")
    test_child_pty_env_hides_outer_terminal_identity_from_child()
    print("PASS test_child_pty_env_hides_outer_terminal_identity_from_child")
    test_child_pty_env_presents_one_windows_terminal_identity_per_platform()
    print("PASS test_child_pty_env_presents_one_windows_terminal_identity_per_platform")
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
    test_bracketed_paste_strip_is_idempotent_across_seams()
    print("PASS test_bracketed_paste_strip_is_idempotent_across_seams")
    test_forward_wheel_only_when_mouse_reporting()
    print("PASS test_forward_wheel_only_when_mouse_reporting")
    test_sync_output_stager_holds_split_frame_until_close()
    print("PASS test_sync_output_stager_holds_split_frame_until_close")
    test_sync_output_stager_orders_back_to_back_and_combined_markers()
    print("PASS test_sync_output_stager_orders_back_to_back_and_combined_markers")
    test_closing_a_failed_open_block_restores_atomicity_and_clears_in_block()
    print("PASS test_closing_a_failed_open_block_restores_atomicity_and_clears_in_block")
    test_sync_output_bypass_rearms_and_atomicity_decays()
    print("PASS test_sync_output_bypass_rearms_and_atomicity_decays")
    test_paste_marker_strip_is_linear_not_quadratic()
    print("PASS test_paste_marker_strip_is_linear_not_quadratic")
    test_sync_output_stager_bounds_and_flushes_once()
    print("PASS test_sync_output_stager_bounds_and_flushes_once")
    test_sync_output_next_open_frame_cannot_mutate_queued_complete_frame()
    print("PASS test_sync_output_next_open_frame_cannot_mutate_queued_complete_frame")
    test_static_query_answers_before_sync_block_closes()
    print("PASS test_static_query_answers_before_sync_block_closes")
    test_cursor_query_fail_opens_sync_block_then_reports_new_cursor()
    print("PASS test_cursor_query_fail_opens_sync_block_then_reports_new_cursor")
    test_every_query_is_answered_at_its_own_position_and_in_stream_order()
    print("PASS test_every_query_is_answered_at_its_own_position_and_in_stream_order")
    test_query_split_does_not_classify_or_tee_half_drawn_frames()
    print("PASS test_query_split_does_not_classify_or_tee_half_drawn_frames")
    test_cursor_report_uses_the_cursor_at_the_querys_stream_position()
    print("PASS test_cursor_report_uses_the_cursor_at_the_querys_stream_position")
    test_cursor_report_clamps_pending_wrap_and_honours_origin_mode()
    print("PASS test_cursor_report_clamps_pending_wrap_and_honours_origin_mode")
    test_decrqm_reports_the_modes_saikai_actually_implements()
    print("PASS test_decrqm_reports_the_modes_saikai_actually_implements")
    test_dcs_strip_closes_its_own_seam_and_never_dumps_a_payload()
    print("PASS test_dcs_strip_closes_its_own_seam_and_never_dumps_a_payload")
    test_dcs_payloads_never_reach_the_grid()
    print("PASS test_dcs_payloads_never_reach_the_grid")
    test_poll_sync_respects_a_frozen_pane_and_flushes_what_it_anchors()
    print("PASS test_poll_sync_respects_a_frozen_pane_and_flushes_what_it_anchors")
    test_queue_overflow_refuses_the_write_and_says_so_in_the_log()
    print("PASS test_queue_overflow_refuses_the_write_and_says_so_in_the_log")
    test_small_writes_queue_when_the_pty_cannot_take_them()
    print("PASS test_small_writes_queue_when_the_pty_cannot_take_them")
    test_writer_thread_is_only_latched_once_it_actually_started()
    print("PASS test_writer_thread_is_only_latched_once_it_actually_started")
    test_large_paste_never_blocks_the_ui_thread_and_keeps_order()
    print("PASS test_large_paste_never_blocks_the_ui_thread_and_keeps_order")
    test_reader_asks_for_large_reads_and_guards_the_eof_flush()
    print("PASS test_reader_asks_for_large_reads_and_guards_the_eof_flush")
    test_cursor_query_fail_open_only_for_a_RETAINED_query()
    print("PASS test_cursor_query_fail_open_only_for_a_RETAINED_query")
    test_sync_output_eof_flushes_retained_frame_once()
    print("PASS test_sync_output_eof_flushes_retained_frame_once")
    test_sync_output_mirror_gets_closed_block_once_in_order()
    print("PASS test_sync_output_mirror_gets_closed_block_once_in_order")
    test_input_snaps_scrolled_back_pane_to_live()
    print("PASS test_input_snaps_scrolled_back_pane_to_live")
    test_busy_storm_throttles_reclassify()
    print("PASS test_busy_storm_throttles_reclassify")
    test_cursor_anchor_settles_hidden_and_tracks_atomic_frames()
    print("PASS test_cursor_anchor_settles_hidden_and_tracks_atomic_frames")
    test_cursor_anchor_freezes_on_busy_or_hidden_follows_visible()
    print("PASS test_cursor_anchor_freezes_on_busy_or_hidden_follows_visible")
    test_only_a_focus_sync_forces_the_cursor_visibility_write()
    print("PASS test_only_a_focus_sync_forces_the_cursor_visibility_write")
    test_ime_anchor_default_on_keeps_windows_caret_render_guard()
    print("PASS test_ime_anchor_default_on_keeps_windows_caret_render_guard")
    test_render_frame_pins_one_pyte_generation()
    print("PASS test_render_frame_pins_one_pyte_generation")
    test_render_takes_the_pane_lock_once_per_frame()
    print("PASS test_render_takes_the_pane_lock_once_per_frame")
