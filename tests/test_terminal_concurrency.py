"""Headless regression tests for ClaudeTerminal threading.

Runs WITHOUT textual/pyte/pywinpty: saikai_terminal soft-imports them (Widget
falls back to object), so ClaudeTerminal can be built via __new__ with just the
fields under test. Run:  python tests/test_terminal_concurrency.py
"""
import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
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
    ct._sync_terminal_cursor = lambda: None      # cursor sync needs a mounted widget
    ct._schedule_pane_refresh()
    ct._schedule_pane_refresh()
    ct._schedule_pane_refresh()
    assert len(queued) == 1, f"not coalesced: {len(queued)} marshals"
    queued[0]()                                   # simulate UI running _do_pane_refresh
    ct._schedule_pane_refresh()
    assert len(queued) == 2, "should re-queue a repaint after the UI painted"


def test_current_screen_caches_by_version():
    """_current_screen reuses the last join until _scr_ver bumps (a feed bumps it),
    so the host poll / render path don't re-join an unchanged screen."""
    ct = rt.ClaudeTerminal.__new__(rt.ClaudeTerminal)
    ct._lock = threading.Lock()
    ct._scr_ver = 5
    ct._cached_ver = -1
    ct._cached_screen = ("", "")

    class _Scr:
        display = ["line a", "line b"]
        title = "T"
    ct._screen = _Scr()
    assert ct._current_screen() == ("line a\nline b", "T")
    ct._screen.display = ["CHANGED"]                       # mutate WITHOUT a version bump
    assert ct._current_screen() == ("line a\nline b", "T"), "should serve the cached join"
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
    term = rt.AgentTerminal.__new__(rt.AgentTerminal)
    try:
        rt.sys.platform = "darwin"
        rt.subprocess.run = fake_run
        term._copy_text("日本語")
    finally:
        rt.sys.platform = old_platform
        rt.subprocess.run = old_run
    assert calls and calls[0][0] == ["pbcopy"], calls
    assert calls[0][1]["input"] == "日本語".encode("utf-8")


def test_set_clipboard_macos_skips_remote_sessions():
    calls = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))

    old_run = rt.subprocess.run
    old_ssh = os.environ.get("SSH_TTY")
    try:
        rt.subprocess.run = fake_run
        os.environ.pop("SSH_TTY", None)
        assert rt.set_clipboard_macos("local") is True
        os.environ["SSH_TTY"] = "/dev/pts/1"
        assert rt.set_clipboard_macos("remote") is False
    finally:
        rt.subprocess.run = old_run
        if old_ssh is None:
            os.environ.pop("SSH_TTY", None)
        else:
            os.environ["SSH_TTY"] = old_ssh
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
    t.paste_text("/handoff")
    assert writes == ["\x1b[200~/handoff\x1b[201~"], writes
    writes.clear(); t._bracketed_paste = False
    t.paste_text("/compact")
    assert writes == ["/compact"], writes
    writes.clear(); t.submit()
    assert writes == ["\r"], writes
    # dead pane: no write
    writes.clear(); t.is_dead = True
    t.paste_text("x"); t.submit()
    assert writes == [], writes


if __name__ == "__main__":
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
    test_pane_refresh_coalesces()
    print("PASS test_pane_refresh_coalesces")
    test_current_screen_caches_by_version()
    print("PASS test_current_screen_caches_by_version")
    test_refresh_status_skips_stable_idle_pane()
    print("PASS test_refresh_status_skips_stable_idle_pane")
    test_classify_pty_status_basics()
    print("PASS test_classify_pty_status_basics")
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
    test_note_reap_prunes_finished_threads()
    print("PASS test_note_reap_prunes_finished_threads")
    test_kitty_keyboard_csi_u_is_scrubbed()
    print("PASS test_kitty_keyboard_csi_u_is_scrubbed")
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
    test_copy_to_host_clipboard_picks_tool_and_reports()
    print("PASS test_copy_to_host_clipboard_picks_tool_and_reports")
    test_paste_text_wraps_and_submits()
    print("PASS test_paste_text_wraps_and_submits")
