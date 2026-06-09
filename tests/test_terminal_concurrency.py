"""Headless regression tests for ClaudeTerminal threading.

Runs WITHOUT textual/pyte/pywinpty: recap_terminal soft-imports them (Widget
falls back to object), so ClaudeTerminal can be built via __new__ with just the
fields under test. Run:  python tests/test_terminal_concurrency.py
"""
import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import recap_terminal as rt


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
    rt.join_all_reaps(timeout=5)
    assert not t.is_alive(), "reap not joined by join_all_reaps"


def test_pane_refresh_coalesces():
    """_schedule_pane_refresh queues at most ONE repaint until the UI paints it
    (then re-queues), so a burst of PTY chunks can't flood call_from_thread."""
    ct = rt.ClaudeTerminal.__new__(rt.ClaudeTerminal)
    queued = []
    ct._marshal = lambda fn: queued.append(fn)   # simulate the UI queue (don't run)
    ct.refresh = lambda: None
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


def test_encode_key_meta_and_release():
    """readline keys reach claude: Ctrl+letters AND Meta/Alt word-ops (ESC prefix).
    The release key must resolve to Textual's real name, not the dead 'ctrl+]'."""
    assert rt.encode_key("alt+b", None) == "\x1bb"          # backward-word
    assert rt.encode_key("alt+f", None) == "\x1bf"          # forward-word
    assert rt.encode_key("alt+d", None) == "\x1bd"          # kill-word
    assert rt.encode_key("alt+backspace", None) == "\x1b\x7f"  # backward-kill-word
    assert rt.encode_key("ctrl+w", None) == "\x17"          # word-delete still forwards
    assert rt.encode_key("ctrl+a", None) == "\x01"
    assert rt._normalize_key("ctrl+]") == "ctrl+right_square_bracket"
    if not os.environ.get("RECAP_RELEASE_KEY"):
        assert rt.RELEASE_FOCUS_KEY == "ctrl+right_square_bracket"


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
    """recap-owned drag-selection geometry: single row = a column span; multi-row
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
    """recap re-wraps pastes in \\x1b[200~ … \\x1b[201~ only when claude has
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
    """Re-opening an EXITED session must not hit Textual DuplicateIds. recap keeps a
    dead pane mounted (for its final frame) and re-uses the sid's pane id on reopen;
    TabbedContent.remove_pane() is DEFERRED (returns AwaitComplete), so a synchronous
    remove_pane()+add_pane(same id) collides. This proves the mechanism behind recap's
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
                if awaited:                       # recap's _mount_live_pane fix
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


if __name__ == "__main__":
    test_update_status_marshals_outside_lock()
    print("PASS test_update_status_marshals_outside_lock")
    test_ime_anchor_xy_maps_cursor_into_region()
    print("PASS test_ime_anchor_xy_maps_cursor_into_region")
    test_reopen_after_exit_requires_awaited_pane_removal()
    print("PASS test_reopen_after_exit_requires_awaited_pane_removal")
    test_kill_tracks_reap_for_atexit_join()
    print("PASS test_kill_tracks_reap_for_atexit_join")
    test_pane_refresh_coalesces()
    print("PASS test_pane_refresh_coalesces")
    test_current_screen_caches_by_version()
    print("PASS test_current_screen_caches_by_version")
    test_refresh_status_skips_stable_idle_pane()
    print("PASS test_refresh_status_skips_stable_idle_pane")
    test_classify_pty_status_basics()
    print("PASS test_classify_pty_status_basics")
    test_encode_key_meta_and_release()
    print("PASS test_encode_key_meta_and_release")
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
    test_toggle_freeze_flips_and_resumes()
    print("PASS test_toggle_freeze_flips_and_resumes")
    test_bracketed_paste_mode_tracking()
    print("PASS test_bracketed_paste_mode_tracking")
