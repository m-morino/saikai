"""Keyboard-first regression tests: the default Space leader, its letter map
resolution (_resolve_leader), the keyboard divider nudge, and — when textual is
installed (CI) — a Pilot test driving the real PickerApp: Space→f toggles the
favorite and Alt+→ grows the persisted split ratio.

Run:  python tests/test_keyboard_leader.py   (headless parts need no deps)
"""
import json
import os
import sys
import tempfile
import uuid

# Isolate the Pilot app-launch tests from a developer's ambient SAIKAI_MIRROR: with
# it set, the launched app starts the web mirror, which perturbs focus-on-launch and
# makes the "table owns focus" assertion flake. Tests must not depend on that env.
os.environ.pop("SAIKAI_MIRROR", None)
from pathlib import Path

# Point saikai at a throwaway home BEFORE importing it (it derives CACHE_DIR /
# state files from Path.home() at import time). Each test file runs in its own
# process, so this cannot leak into the other suites.
_FAKE_HOME = Path(tempfile.mkdtemp(prefix="saikai-kbd-test-"))
for _var in ("USERPROFILE", "HOME", "APPDATA", "LOCALAPPDATA", "XDG_CONFIG_HOME"):
    os.environ[_var] = str(_FAKE_HOME)
os.environ.pop("SAIKAI_CONFIG", None)
os.environ["SAIKAI_SUMMARIZE_ENABLED"] = "0"
# Disable the Windows terminal-death watchdog: it has no real terminal to watch
# in a headless harness, and its os._exit on a (false-positive) orphan detection
# would kill the test process mid-suite — silently, losing buffered output. The
# watchdog is a production-only safety net; the env var is its designed off-switch.
os.environ["SAIKAI_NO_TERMINAL_WATCHDOG"] = "1"

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import saikai

# Realistic id→action fixture (mirrors the PickerApp BINDINGS ids the defaults
# rely on; the Pilot test below exercises the REAL bindings when textual exists).
ID2ACT = {
    "refresh": "refresh", "favorite": "toggle_fav", "hide": "toggle_hide",
    "diff": "preview_changes", "copy": "copy_prompt", "tree": "toggle_tree",
    "group": "cycle_group", "new": "new_session",
    "freeze": "freeze_pane", "restore": "restore_panes", "close": "close_live",
    "prev_tab": "prev_tab", "next_tab": "next_tab", "attention": "next_attention",
    "toggle_list": "toggle_list", "rename": "rename", "notifs": "notifications",
}


def test_resolve_leader_defaults_on():
    lk, m, errs = saikai._resolve_leader({}, ID2ACT)
    assert lk == "space" and not errs
    # every shipped default letter resolves to a real action
    assert len(m) == len(saikai.DEFAULT_LEADER_LETTERS)
    assert m["f"] == "toggle_fav" and m["h"] == "toggle_hide"
    assert m["s"] == "sort" and m["o"] == "order"      # leader-only actions
    assert m[" "] == "toggle_mark"                     # double-Space = mark
    assert m["["] == "prev_tab" and m["]"] == "next_tab"
    assert m[","] == "settings"                        # ␣, = Settings modal
    assert m["/"] == "toggle_search_bar"               # ␣/ = filter bar toggle


def test_resolve_leader_disable_and_custom_key():
    assert saikai._resolve_leader({"leader": "none"}, ID2ACT) == ("", {}, [])
    assert saikai._resolve_leader({"leader": "off"}, ID2ACT) == ("", {}, [])
    lk, m, _ = saikai._resolve_leader({"leader": "ctrl+g"}, ID2ACT)
    assert lk == "ctrl+g" and m["f"] == "toggle_fav"   # defaults still apply


def test_resolve_leader_user_letter_wins():
    # remap favorite to v: old letter gone, new letter set
    _, m, _ = saikai._resolve_leader({"favorite": "v"}, ID2ACT)
    assert m["v"] == "toggle_fav" and "f" not in m
    # stealing a default letter evicts the default action that sat on it
    _, m, _ = saikai._resolve_leader({"refresh": "f"}, ID2ACT)
    assert m["f"] == "refresh" and "r" not in m
    assert "toggle_fav" not in m.values()


def test_resolve_leader_no_defaults():
    _, m, _ = saikai._resolve_leader(
        {"leader_defaults": False, "favorite": "f"}, ID2ACT)
    assert m == {"f": "toggle_fav"}
    _, m2, _ = saikai._resolve_leader({"leader_defaults": False}, ID2ACT)
    assert m2 == {}


def test_resolve_leader_ignores_release_key():
    """The live-pane release key is not a leader action, even when one character."""
    leader, actions, errors = saikai._resolve_leader(
        {"release": "g"}, ID2ACT,
    )
    assert leader == "space"
    assert actions["g"] == "cycle_group"
    assert errors == []


def test_nudge_split_ratio_clamps():
    lo, hi = saikai._SPLIT_RATIO_LO, saikai._SPLIT_RATIO_HI
    assert saikai._nudge_split_ratio(0.34, +0.04) == 0.38
    assert saikai._nudge_split_ratio(hi - 0.01, +0.04) == hi
    assert saikai._nudge_split_ratio(lo + 0.01, -0.04) == lo


def test_leader_label_short_names():
    assert saikai._leader_label("toggle_fav") == "favorite"
    assert saikai._leader_label("preview_changes") == "diff"
    assert saikai._leader_label("cycle_group") == "group"
    assert saikai._leader_label("sort") == "sort"


def test_leader_groups_by_family():
    """The which-key hint / ? help render the leader map grouped Session →
    View → Panes (not an alphabetical soup). Every default letter must appear
    in exactly one family; unknown actions land in the last family."""
    _, m, _ = saikai._resolve_leader({}, ID2ACT)
    groups = saikai._leader_groups(m)
    fams = [f for f, _ in groups]
    assert fams == list(saikai.LEADER_FAMILY_ORDER), fams
    flat = [(k, lbl) for _, pairs in groups for k, lbl in pairs]
    # 1-9 (goto tab N) collapse into ONE "1-9" entry; every other letter shows
    digits = [k for k in m if k.isdigit()]
    assert len(flat) == len(m) - len(digits) + 1, "a letter vanished from the grouped view"
    by_fam = dict(groups)
    assert ("f", "favorite") in by_fam["Session"] and (" ", "mark") in by_fam["Session"]
    assert ("s", "sort") in by_fam["View"] and ("g", "group") in by_fam["View"]
    assert (",", "settings") in by_fam["View"]
    assert ("/", "search bar") in by_fam["View"]
    assert ("[", "prev tab") in by_fam["Move"] and ("1-9", "tab N") in by_fam["Move"]
    assert ("x", "close") in by_fam["Panes"] and ("X", "close all") in by_fam["Panes"]
    # unknown action -> last family, not dropped
    g2 = saikai._leader_groups({"q": "made_up_action"})
    assert g2 and g2[-1][0] == saikai.LEADER_FAMILY_ORDER[-1]
    assert ("q", "made_up_action") in g2[-1][1]
    assert saikai._leader_groups({}) == []


def test_leader_hint_item_separates_key_from_action():
    """A menu choice must not look like one misspelled command (e.g. ffav)."""
    assert saikai._leader_hint_item("f", "fav") == (
        "[yellow]f[/yellow] [dim]→[/dim] fav"
    )
    assert saikai._leader_hint_item(" ", "mark") == (
        "[yellow]␣[/yellow] [dim]→[/dim] mark"
    )
    assert saikai._leader_hint_item("[", "tab◀") == (
        r"[yellow]\[[/yellow] [dim]→[/dim] tab◀"
    )


def _write_demo_session() -> str:
    sid = str(uuid.uuid4())
    pdir = _FAKE_HOME / ".claude" / "projects" / "-home-alex-code-demo"
    pdir.mkdir(parents=True, exist_ok=True)
    recs = [
        {"type": "ai-title", "aiTitle": "Demo session",
         "timestamp": "2026-06-12T00:00:00.000Z", "cwd": "/home/alex/code/demo"},
        {"type": "user", "timestamp": "2026-06-12T00:01:00.000Z",
         "cwd": "/home/alex/code/demo",
         "message": {"content": "demo prompt long enough to count"}},
    ]
    (pdir / f"{sid}.jsonl").write_text(
        "\n".join(json.dumps(r) for r in recs) + "\n", encoding="utf-8")
    return sid


def test_pilot_space_leader_and_divider():
    """Real-app flow (needs textual; CI runs it): Space→f favorites the row,
    Alt+→ grows + persists the split ratio, / reaches the filter dropdowns."""
    try:
        from textual.app import App  # noqa: F401
    except Exception:
        print("SKIP test_pilot_space_leader_and_divider (textual unavailable)")
        return

    import asyncio
    from textual.app import App

    sid = _write_demo_session()
    facts: dict = {}

    def fake_run(self, *a, **kw):
        async def go():
            async with self.run_test(size=(110, 30)) as pilot:
                await pilot.pause(0.4)
                # The search/filter bar (and its Group/Sort/Status/Age
                # dropdowns) is VISIBLE by default — the dropdowns are the
                # features' discoverability. The table still owns focus, so
                # the leader and search-as-you-type work unchanged.
                facts["bar_default"] = bool(self.query_one("#searchrow").display)
                facts["table_focused"] = self.focused is self.query_one("#table")
                await pilot.press("space")          # arm the leader…
                await pilot.press("f")              # …then f = favorite
                await pilot.pause(0.2)
                favs = saikai._read_json(saikai.FAVORITE_FILE, [])
                facts["favorited"] = sid in (favs or [])
                before = getattr(self, "_split_ratio", None)
                await pilot.press("space", "greater_than_sign")  # menu > widens the list
                await pilot.pause(0.2)
                facts["ratio_before"] = before
                facts["ratio_after"] = getattr(self, "_split_ratio", None)
                facts["ratio_saved"] = (saikai._read_json(
                    saikai.OPTIONS_FILE, {}) or {}).get("split_ratio")
                await pilot.press("slash")          # bar shows the dropdowns
                await pilot.pause(0.2)
                facts["bar_shown"] = bool(
                    self.query_one("#searchrow").display)
        asyncio.run(go())

    orig_run, App.run = App.run, fake_run
    orig_argv = sys.argv
    try:
        sys.argv = ["saikai", "--all"]
        saikai.main()
    finally:
        App.run = orig_run
        sys.argv = orig_argv

    assert facts.get("bar_default"), f"search bar must be visible on launch: {facts}"
    assert facts.get("table_focused"), f"table must own focus on launch: {facts}"
    assert facts.get("favorited"), f"leader Space→f did not favorite: {facts}"
    assert facts["ratio_after"] > facts["ratio_before"], facts
    assert abs(facts["ratio_saved"] - facts["ratio_after"]) < 1e-6, facts
    assert facts.get("bar_shown"), facts


def test_pilot_synchronized_output_brackets_frames():
    """Frames must be wrapped in DEC 2026 once the terminal reports ?2026 support.

    saikai sends the ?2026 DECRQM probe on Windows (Textual's Windows driver omits the
    probe its linux/web drivers send); the terminal's positive reply posts
    TerminalSupportsSynchronizedOutput, Textual sets _sync_available, and _begin_update
    then brackets every frame in SYNC_START/END. Without that the native cursor sweeps /
    flickers on every repaint. This verifies the END of that chain — that the supports
    message actually makes a frame emit ?2026h/?2026l — rather than reading back a flag
    saikai set itself (which a Textual rename would leave as a dead attribute)."""
    try:
        from textual.app import App  # noqa: F401
    except Exception:
        print("SKIP test_pilot_synchronized_output_brackets_frames (textual unavailable)")
        return

    import asyncio
    from textual.app import App
    from textual import messages

    _write_demo_session()
    facts: dict = {}

    def fake_run(self, *a, **kw):
        async def go():
            async with self.run_test(size=(110, 30)) as pilot:
                await pilot.pause(0.4)
                # Simulate the terminal answering our probe: it supports ?2026.
                self.post_message(messages.TerminalSupportsSynchronizedOutput())
                await pilot.pause(0.1)
                facts["flag"] = bool(getattr(self, "_sync_available", False))
                # With support acknowledged, opening/closing a frame must emit the
                # DEC 2026 brackets to the driver.
                writes: list = []
                drv = self._driver
                _orig = drv.write
                try:
                    drv.write = lambda d, _o=_orig: (writes.append(d), _o(d))[1]
                    self._begin_update()
                    self._end_update()
                finally:
                    drv.write = _orig
                joined = "".join(writes)
                facts["start"] = "\x1b[?2026h" in joined
                facts["end"] = "\x1b[?2026l" in joined
        asyncio.run(go())

    orig_run, App.run = App.run, fake_run
    orig_argv = sys.argv
    try:
        sys.argv = ["saikai", "--all"]
        saikai.main()
    finally:
        App.run = orig_run
        sys.argv = orig_argv

    assert facts.get("flag") is True, \
        f"the supports message must set _sync_available (Textual path intact): {facts}"
    assert facts.get("start") and facts.get("end"), \
        f"a frame must be bracketed in ?2026h/?2026l once sync is available: {facts}"


def test_pilot_session_list_disables_mouse_hover_repaint():
    """The session list must NOT repaint on mouse hover (#wt-hover).

    Textual's DataTable drives a hover cursor on every mouse move (_on_mouse_move ->
    _set_hover_cursor(True) + hover_coordinate -> refresh). On Windows Terminal each
    such repaint punches cells in the adjacent split-live pane and re-emits/flickers
    the pane's native cursor. saikai's list is keyboard-first, so ListTable swallows
    mouse-move: hovering must leave _show_hover_cursor False (no repaint), while the
    keyboard row cursor still moves."""
    try:
        from textual.app import App  # noqa: F401
    except Exception:
        print("SKIP test_pilot_session_list_disables_mouse_hover_repaint (textual unavailable)")
        return

    import asyncio
    from textual.app import App

    # Write several rows so the keyboard cursor has somewhere to move to; depending on
    # leaked state from earlier tests would make this red in isolation. (#wt-hover)
    for _ in range(3):
        _write_demo_session()
    facts: dict = {}

    def fake_run(self, *a, **kw):
        async def go():
            async with self.run_test(size=(110, 30)) as pilot:
                await pilot.pause(0.4)
                table = self.query_one("#table")
                facts["is_listtable"] = type(table).__name__ == "ListTable"
                facts["hover_off_initial"] = table._show_hover_cursor is False
                await pilot.hover("#table")               # move the mouse onto the list
                await pilot.pause(0.2)
                # ListTable swallows the move -> the hover highlight never activates.
                facts["hover_off_after_hover"] = table._show_hover_cursor is False
                row_before = table.cursor_coordinate.row
                await pilot.press("down")                 # keyboard nav must still work
                await pilot.pause(0.1)
                facts["kbd_moved"] = table.cursor_coordinate.row != row_before
        asyncio.run(go())

    orig_run, App.run = App.run, fake_run
    orig_argv = sys.argv
    try:
        sys.argv = ["saikai", "--all"]
        saikai.main()
    finally:
        App.run = orig_run
        sys.argv = orig_argv

    assert facts.get("is_listtable"), f"#table must be the ListTable subclass: {facts}"
    assert facts.get("hover_off_initial"), facts
    assert facts.get("hover_off_after_hover"), \
        f"mouse hover must not activate the hover cursor (it repaints -> WT punch): {facts}"
    assert facts.get("kbd_moved"), f"keyboard row cursor must still move: {facts}"


def test_pilot_search_clear_button():
    """The clear (X) button: hidden when the search box is empty, shown once it
    has text, and clicking it clears the box. Verifies on_click fires on the
    custom Static subclass and the on_input_changed display toggle."""
    try:
        from textual.app import App  # noqa: F401
    except Exception:
        print("SKIP test_pilot_search_clear_button (textual unavailable)")
        return

    import asyncio
    from textual.app import App

    _write_demo_session()
    facts: dict = {}

    def fake_run(self, *a, **kw):
        async def go():
            async with self.run_test(size=(110, 30)) as pilot:
                await pilot.pause(0.4)
                facts["hidden_empty"] = self.query_one("#search-clear").display
                self.query_one("#search").focus()
                await pilot.pause(0.1)
                await pilot.press("a", "b", "c")
                await pilot.pause(0.3)
                facts["typed"] = self.query_one("#search").value
                facts["shown"] = self.query_one("#search-clear").display
                await pilot.click("#search-clear")
                await pilot.pause(0.3)
                facts["cleared"] = self.query_one("#search").value
                facts["hidden_after"] = self.query_one("#search-clear").display
        asyncio.run(go())

    orig_run, App.run = App.run, fake_run
    orig_argv = sys.argv
    try:
        sys.argv = ["saikai", "--all"]
        saikai.main()
    finally:
        App.run = orig_run
        sys.argv = orig_argv

    assert facts.get("hidden_empty") is False, f"clear X hidden when empty: {facts}"
    assert facts.get("typed") == "abc", f"typing fills the search box: {facts}"
    assert facts.get("shown") is True, f"clear X shown once text present: {facts}"
    assert facts.get("cleared") == "", f"clicking X clears the search: {facts}"
    assert facts.get("hidden_after") is False, f"clear X hidden after clear: {facts}"


def test_pilot_custom_leader_does_not_leave_space_as_menu():
    """When leader moves to Ctrl+G, Space must retain its normal table action."""
    try:
        from textual.app import App  # noqa: F401
    except Exception:
        print("SKIP test_pilot_custom_leader_does_not_leave_space_as_menu (textual unavailable)")
        return

    import asyncio
    from textual.app import App

    sid = _write_demo_session()
    cfg = _FAKE_HOME / "custom-leader.toml"
    cfg.write_text('[keys]\nleader = "ctrl+g"\n', encoding="utf-8")
    facts: dict = {}

    def fake_run(self, *a, **kw):
        async def go():
            async with self.run_test(size=(110, 30)) as pilot:
                await pilot.pause(0.4)
                facts["leader"] = self._leader_key
                await pilot.press("space")
                await pilot.press("f")
                await pilot.pause(0.2)
                facts["favorited"] = sid in (saikai._read_json(
                    saikai.FAVORITE_FILE, []) or [])
        asyncio.run(go())

    orig_run, App.run = App.run, fake_run
    orig_argv = sys.argv
    os.environ["SAIKAI_CONFIG"] = str(cfg)
    saikai._reset_config_cache()
    try:
        sys.argv = ["saikai", "--all"]
        saikai.main()
    finally:
        App.run = orig_run
        sys.argv = orig_argv
        os.environ.pop("SAIKAI_CONFIG", None)
        saikai._reset_config_cache()

    assert facts.get("leader") == "ctrl+g", facts
    assert not facts.get("favorited"), f"Space incorrectly armed custom leader: {facts}"


def test_pilot_settings_screen():
    """␣, opens the Settings modal; changing its Group select forwards into the
    top-bar dropdown (the one true apply path) and persists; Esc closes."""
    try:
        from textual.app import App  # noqa: F401
    except Exception:
        print("SKIP test_pilot_settings_screen (textual unavailable)")
        return

    import asyncio
    from textual.app import App
    from textual.widgets import Select

    _write_demo_session()
    facts: dict = {}

    def fake_run(self, *a, **kw):
        async def go():
            async with self.run_test(size=(110, 30)) as pilot:
                await pilot.pause(0.4)
                await pilot.press("space")              # leader…
                await pilot.press("comma")              # …then , = settings
                await pilot.pause(0.3)
                facts["modal"] = type(self.screen).__name__
                try:
                    sel = self.screen.query_one("#set-group", Select)
                    sel.value = "project"               # edit inside the modal
                    await pilot.pause(0.3)
                    facts["persisted"] = saikai._get_group_by()
                    facts["topbar"] = self.query_one("#groupsel", Select).value
                except Exception as e:                  # noqa: BLE001
                    facts["error"] = repr(e)
                await pilot.press("escape")             # close the modal
                await pilot.pause(0.2)
                facts["closed"] = type(self.screen).__name__
        asyncio.run(go())

    orig_run, App.run = App.run, fake_run
    orig_argv = sys.argv
    try:
        sys.argv = ["saikai", "--all"]
        saikai.main()
    finally:
        App.run = orig_run
        sys.argv = orig_argv

    assert facts.get("modal") == "SettingsScreen", facts
    assert facts.get("persisted") == "project", facts
    assert facts.get("topbar") == "project", facts
    assert facts.get("closed") != "SettingsScreen", facts


def test_pilot_esc_quits_and_bar_toggle():
    """The Esc contract with the default-visible bar: a single Esc from the
    LIST quits (the bar no longer swallows the first Esc); ␣/ is the deliberate
    bar toggle and persists; Esc from the search box returns to the list
    WITHOUT hiding the bar (it's a fixture, not chrome)."""
    try:
        from textual.app import App  # noqa: F401
    except Exception:
        print("SKIP test_pilot_esc_quits_and_bar_toggle (textual unavailable)")
        return

    import asyncio
    from textual.app import App

    _write_demo_session()
    facts: dict = {}

    def fake_run(self, *a, **kw):
        async def go():
            async with self.run_test(size=(110, 30)) as pilot:
                await pilot.pause(0.4)
                bar = self.query_one("#searchrow")
                facts["visible_at_start"] = bool(bar.display)
                await pilot.press("slash")            # jump into the search box
                await pilot.pause(0.1)
                await pilot.press("escape")           # Esc: search → list…
                await pilot.pause(0.1)
                facts["bar_kept_after_esc"] = bool(bar.display)   # …bar STAYS
                facts["table_refocused"] = self.focused is self.query_one("#table")
                await pilot.press("space")            # ␣/ hides the bar…
                await pilot.press("slash")
                await pilot.pause(0.2)
                facts["bar_after_toggle"] = bool(bar.display)
                facts["persisted"] = (saikai._read_json(
                    saikai.OPTIONS_FILE, {}) or {}).get("search_bar")
                # A single Esc from the list now ARMS quit (does not exit) — a
                # reflex Esc must not kill saikai. A deliberate SECOND Esc quits.
                await pilot.press("escape")           # 1st Esc: arm, stay running
                await pilot.pause(0.2)
                facts["running_after_one_esc"] = self.is_running
                await pilot.press("escape")           # 2nd Esc: quit
                await pilot.pause(0.3)
                facts["running_after_two_esc"] = self.is_running
        asyncio.run(go())

    orig_run, App.run = App.run, fake_run
    orig_argv = sys.argv
    try:
        sys.argv = ["saikai", "--all"]
        saikai.main()
    finally:
        App.run = orig_run
        sys.argv = orig_argv

    assert facts.get("visible_at_start"), facts
    assert facts.get("bar_kept_after_esc"), f"Esc from search must NOT hide the bar: {facts}"
    assert facts.get("table_refocused"), facts
    assert facts.get("bar_after_toggle") is False, f"leader / must hide the bar: {facts}"
    assert facts.get("persisted") is False, facts
    assert facts.get("running_after_one_esc") is True, \
        f"a single Esc from the list must NOT quit (double-press guard): {facts}"
    assert facts.get("running_after_two_esc") is False, \
        f"a deliberate second Esc must quit: {facts}"


def test_ctrlc_double_press_and_disarm():
    """Ctrl+C also requires a deliberate SECOND press to quit (claude treats a
    single Ctrl+C as interrupt, exiting only on a second), and any other key
    between presses disarms — so only two CONSECUTIVE quit presses exit. The
    companion to the Esc guard above."""
    try:
        from textual.app import App  # noqa: F401
    except Exception:
        print("SKIP test_ctrlc_double_press_and_disarm (textual unavailable)")
        return

    import asyncio
    from textual.app import App

    _write_demo_session()
    facts: dict = {}

    def fake_run(self, *a, **kw):
        async def go():
            async with self.run_test(size=(110, 30)) as pilot:
                await pilot.pause(0.4)
                self.query_one("#table").focus()
                await pilot.press("ctrl+c")           # 1st Ctrl+C: arm, no quit
                await pilot.pause(0.2)
                facts["after_one_cc"] = self.is_running
                await pilot.press("down")             # any other key disarms…
                await pilot.pause(0.1)
                await pilot.press("ctrl+c")           # …so this only re-arms
                await pilot.pause(0.2)
                facts["after_disarm_then_cc"] = self.is_running
                await pilot.press("ctrl+c")           # consecutive 2nd: quit
                await pilot.pause(0.3)
                facts["after_two_cc"] = self.is_running
        asyncio.run(go())

    orig_run, App.run = App.run, fake_run
    orig_argv = sys.argv
    try:
        sys.argv = ["saikai", "--all"]
        saikai.main()
    finally:
        App.run = orig_run
        sys.argv = orig_argv

    assert facts.get("after_one_cc") is True, \
        f"a single Ctrl+C must NOT quit (double-press guard): {facts}"
    assert facts.get("after_disarm_then_cc") is True, \
        f"a key between presses disarms, so the next single Ctrl+C must not quit: {facts}"
    assert facts.get("after_two_cc") is False, \
        f"two consecutive Ctrl+C must quit: {facts}"


def test_ctrlq_is_double_press_guarded():
    """Ctrl+Q must NOT quit on a single press either. Textual's built-in
    priority ctrl+q->quit resolves to saikai's overridden action_quit, so a
    single Ctrl+Q should ARM (not exit) and a second should quit — a reflex
    Ctrl+Q must not kill saikai (nor orphan its live panes)."""
    try:
        from textual.app import App  # noqa: F401
    except Exception:
        print("SKIP test_ctrlq_is_double_press_guarded (textual unavailable)")
        return

    import asyncio
    from textual.app import App

    _write_demo_session()
    facts: dict = {}

    def fake_run(self, *a, **kw):
        async def go():
            async with self.run_test(size=(110, 30)) as pilot:
                await pilot.pause(0.4)
                self.query_one("#table").focus()
                await pilot.press("ctrl+q")           # 1st Ctrl+Q: must NOT quit
                await pilot.pause(0.2)
                facts["after_one"] = self.is_running
                await pilot.press("ctrl+q")           # 2nd: quit
                await pilot.pause(0.3)
                facts["after_two"] = self.is_running
        asyncio.run(go())

    orig_run, App.run = App.run, fake_run
    orig_argv = sys.argv
    try:
        sys.argv = ["saikai", "--all"]
        saikai.main()
    finally:
        App.run = orig_run
        sys.argv = orig_argv

    assert facts.get("after_one") is True, \
        f"a single Ctrl+Q must NOT quit (must be guarded like Ctrl+C): {facts}"
    assert facts.get("after_two") is False, \
        f"a second Ctrl+Q must quit: {facts}"


def test_focus_moves_are_logged():
    """An always-on focus trail: on_descendant_focus appends a '[focus] a -> b'
    line to saikai.log on each focus move, so an unexpected 'focus changed on its
    own' is captured next to the pane/refresh events that caused it."""
    try:
        from textual.app import App  # noqa: F401
    except Exception:
        print("SKIP test_focus_moves_are_logged (textual unavailable)")
        return

    import asyncio
    import tempfile
    import shutil
    from pathlib import Path
    from textual.app import App

    _write_demo_session()
    d = Path(tempfile.mkdtemp())
    saved_log = saikai.LOG_FILE
    saikai.LOG_FILE = d / "saikai.log"
    facts: dict = {}

    def fake_run(self, *a, **kw):
        async def go():
            async with self.run_test(size=(110, 30)) as pilot:
                await pilot.pause(0.4)
                await pilot.press("slash")            # focus the search box
                await pilot.pause(0.15)
                await pilot.press("escape")           # search -> table (focus move)
                await pilot.pause(0.2)
        asyncio.run(go())

    orig_run, App.run = App.run, fake_run
    orig_argv = sys.argv
    try:
        sys.argv = ["saikai", "--all"]
        saikai.main()
    finally:
        App.run = orig_run
        sys.argv = orig_argv
        facts["log"] = (saikai.LOG_FILE.read_text(encoding="utf-8")
                        if saikai.LOG_FILE.exists() else "")
        saikai.LOG_FILE = saved_log
        shutil.rmtree(d, ignore_errors=True)

    assert "[focus]" in facts["log"], \
        f"focus moves must be logged ([focus] ... -> ...): {facts['log']!r}"


def test_status_refresh_deferred_while_pane_focused():
    """The 1.5s status poll must not rebuild the list while a live pane is focused
    (the rebuild disrupts typing into claude — keystrokes leak to the list/search).
    on_descendant_focus catches the deferred rebuild up when focus returns to the
    list, and must NOT rebuild while a pane is still focused."""
    try:
        from textual.app import App  # noqa: F401
    except Exception:
        print("SKIP test_status_refresh_deferred_while_pane_focused (textual unavailable)")
        return

    import asyncio
    import tempfile
    import shutil
    from types import SimpleNamespace
    from pathlib import Path
    from textual.app import App

    _write_demo_session()
    d = Path(tempfile.mkdtemp())
    saved_log = saikai.LOG_FILE
    saikai.LOG_FILE = d / "saikai.log"
    facts: dict = {}

    def fake_run(self, *a, **kw):
        async def go():
            async with self.run_test(size=(110, 30)) as pilot:
                await pilot.pause(0.4)
                calls = []
                self._request_refresh = lambda: calls.append(1)
                ev = SimpleNamespace(widget=self.query_one("#table"))
                # A live pane is focused + a deferred refresh is pending: the
                # focus handler must NOT rebuild the list yet.
                self._focused_terminal = lambda: object()
                self._status_refresh_pending = True
                self.on_descendant_focus(ev)
                facts["calls_while_pane"] = len(calls)
                facts["pending_while_pane"] = self._status_refresh_pending
                # Focus returns to the list: the deferred rebuild fires once.
                self._focused_terminal = lambda: None
                self.on_descendant_focus(ev)
                facts["calls_after_return"] = len(calls)
                facts["pending_after_return"] = self._status_refresh_pending
        asyncio.run(go())

    orig_run, App.run = App.run, fake_run
    orig_argv = sys.argv
    try:
        sys.argv = ["saikai", "--all"]
        saikai.main()
    finally:
        App.run = orig_run
        sys.argv = orig_argv
        saikai.LOG_FILE = saved_log
        shutil.rmtree(d, ignore_errors=True)

    assert facts.get("calls_while_pane") == 0, \
        f"must NOT rebuild the list while a pane is focused: {facts}"
    assert facts.get("pending_while_pane") is True, facts
    assert facts.get("calls_after_return") == 1, \
        f"must catch the deferred rebuild up on focus return: {facts}"
    assert facts.get("pending_after_return") is False, facts


def test_launch_qr_dismiss_reshows_restore_hint():
    """The launch QR screen is pushed over the 'Shift+F4 to reopen' toast, hiding
    it; _after_launch_qr (the QR's on-dismiss callback) re-shows the hint — but
    only when there are restore candidates."""
    try:
        from textual.app import App  # noqa: F401
    except Exception:
        print("SKIP test_launch_qr_dismiss_reshows_restore_hint (textual unavailable)")
        return

    import asyncio
    from textual.app import App

    _write_demo_session()
    facts: dict = {}

    def fake_run(self, *a, **kw):
        async def go():
            async with self.run_test(size=(110, 30)) as pilot:
                await pilot.pause(0.4)
                notes = []
                self.notify = lambda msg, **k: notes.append(msg)
                self._restore_candidates = []            # nothing to restore
                self._after_launch_qr()
                facts["notes_no_cand"] = len(notes)
                self._restore_candidates = [{"id": "a", "cwd": "x"},
                                            {"id": "b", "cwd": "y"}]
                self._after_launch_qr()
                facts["hint"] = notes[-1] if notes else ""
        asyncio.run(go())

    orig_run, App.run = App.run, fake_run
    orig_argv = sys.argv
    try:
        sys.argv = ["saikai", "--all"]
        saikai.main()
    finally:
        App.run = orig_run
        sys.argv = orig_argv

    assert facts.get("notes_no_cand") == 0, f"no candidates -> no hint: {facts}"
    assert "␣u" in facts.get("hint", "") and "2 pane" in facts.get("hint", ""), \
        f"the deferred hint must re-show with the count: {facts}"


def test_pilot_mirror_control_toggle():
    """A focus-independent priority binding toggles _control_enabled and pushes
    the new state into the hub EVEN WHILE A PANE IS FOCUSED. This catches the
    'leader letter is unreachable over a focused pane' bug: the toggle must be a
    priority Binding, not a leader letter."""
    try:
        from textual.app import App  # noqa: F401
    except Exception:
        print("SKIP test_pilot_mirror_control_toggle (textual unavailable)")
        return

    import asyncio
    from textual.app import App

    _write_demo_session()
    facts: dict = {}

    class _StubHub:
        def __init__(self):
            self.calls = []
        def set_control_state(self, enabled, target=None):
            self.calls.append((enabled, target))
            # Mirror the real hub's contract: return the effective state so the
            # app keeps its own copy in sync. (Loopback stub → no clamp.)
            return enabled
        # on_mount also wires these; provide no-op stand-ins.
        def set_size(self, *a):
            pass
        def set_repaint_request(self, *a):
            pass
        def set_input_handler(self, *a):
            pass
        def set_mouse_handler(self, *a):
            pass
        def set_key_handler(self, *a):
            pass
        def set_client_change_handler(self, *a):
            pass
        def set_control_change_handler(self, *a):
            pass
        def set_raw_handler(self, *a):              # pane-direct wiring (#pane-direct)
            pass
        def set_pane_reseed_request(self, *a):
            pass
        def set_pane_meta(self, *a):
            pass
        def url(self):
            return "http://127.0.0.1:0/?token=x"
        def client_count(self):
            return 0

    def fake_run(self, *a, **kw):
        async def go():
            self._mirror_hub = _StubHub()
            async with self.run_test(size=(110, 30)) as pilot:
                await pilot.pause(0.4)
                facts["start_enabled"] = self._control_enabled
                # Simulate a focused live pane: a stub the binding will read for
                # its target title. (_focused_terminal is overridden so we don't
                # need a real PTY.)
                class _T:
                    title = "Demo session"
                self._focused_terminal = lambda: _T()
                if type(self.screen).__name__ == "MirrorScreen":
                    await pilot.press("escape")       # the launch QR auto-shows
                    await pilot.pause(0.2)
                await pilot.press("f12")              # mirror QR …
                await pilot.pause(0.3)
                await pilot.press("c")                # … c = control (local only)
                await pilot.pause(0.2)
                facts["after_enabled"] = self._control_enabled
                facts["hub_calls"] = list(self._mirror_hub.calls)
                await pilot.press("f12")              # toggle back off
                await pilot.pause(0.3)
                await pilot.press("c")
                await pilot.pause(0.2)
                facts["after_off"] = self._control_enabled
                facts["hub_calls2"] = list(self._mirror_hub.calls)
        asyncio.run(go())

    orig_run, App.run = App.run, fake_run
    orig_argv = sys.argv
    try:
        sys.argv = ["saikai", "--all"]
        saikai.main()
    finally:
        App.run = orig_run
        sys.argv = orig_argv

    assert facts.get("start_enabled") is False, facts
    assert facts.get("after_enabled") is True, f"toggle did not enable: {facts}"
    assert facts.get("hub_calls") == [(True, "Demo session")], facts
    assert facts.get("after_off") is False, f"toggle did not disable: {facts}"
    assert facts.get("hub_calls2")[-1] == (False, None), facts


def test_pilot_mirror_tap_and_key_drive_ui():
    """End-to-end: with control ON, a synthesized events.Key fires a priority
    binding (F6 favorite) and a synthesized click is dispatched by App.on_event
    (mouse_position updates to the clicked cell) — proving on_mount wired the
    handlers and the App routes injected Key + Mouse events natively. Drives the
    REAL PickerApp."""
    try:
        from textual.app import App  # noqa: F401
    except Exception:
        print("SKIP test_pilot_mirror_tap_and_key_drive_ui (textual unavailable)")
        return

    import asyncio
    from textual.app import App

    _write_demo_session()
    facts: dict = {}

    def fake_run(self, *a, **kw):
        async def go():
            async with self.run_test(size=(110, 30)) as pilot:
                await pilot.pause(0.4)
                # Turn control ON directly (the toggle is local-only; we exercise
                # the injection path, not the keybinding).
                self._control_enabled = True
                # 1) A priority binding via a synthesized Key: F6 = favorite.
                # action_toggle_fav favorites THE SELECTED ROW, so read the live
                # cursor sid (not the just-written one — sibling demo sessions from
                # earlier Pilot tests in this process share the fake home, and the
                # cursor may sit on any of them). The proof is that the synthesized
                # F6 flipped that row's favorite state via the priority binding.
                target_sid = self._cursor_sid()
                before = target_sid in (saikai._read_json(saikai.FAVORITE_FILE, []) or [])
                self._mirror_inject_key("action:toggle_fav")
                await pilot.pause(0.3)
                after = target_sid in (saikai._read_json(saikai.FAVORITE_FILE, []) or [])
                facts["cursor_sid"] = target_sid
                facts["fav_before"] = before
                facts["fav_after"] = after
                # 2) A synthesized click is DISPATCHED by App.on_event, which sets
                # self.mouse_position = Offset(event.x, event.y) on MouseDown — a
                # side-effect-free, widget-agnostic proof the click routed.
                table = self.query_one("#table")
                region = table.region                 # screen region of the table
                col_x = region.x + 2                  # a real on-screen cell
                row_y = region.y + 2                  # a row inside the table
                self._mirror_inject_mouse(col_x, row_y, 0, "down")
                self._mirror_inject_mouse(col_x, row_y, 0, "up")
                await pilot.pause(0.3)
                mp = self.mouse_position
                facts["mouse_xy"] = (mp.x, mp.y)
                facts["click_target"] = (col_x, row_y)
                facts["still_running"] = self.is_running
        asyncio.run(go())

    orig_run, App.run = App.run, fake_run
    orig_argv = sys.argv
    try:
        sys.argv = ["saikai", "--all"]
        saikai.main()
    finally:
        App.run = orig_run
        sys.argv = orig_argv

    # The Key reached the favorite priority binding (focus-independent): the
    # synthesized F6 TOGGLED the selected row's favorite state. Asserting the
    # flip (not a fixed direction) keeps the proof robust to sibling demo
    # sessions a prior Pilot test may have already favorited in the shared home.
    assert facts.get("cursor_sid"), f"no row under the cursor to favorite: {facts}"
    assert facts.get("fav_after") != facts.get("fav_before"), \
        f"synthesized F6 did not toggle the favorite: {facts}"
    # The synthesized click was dispatched by App.on_event: it set mouse_position
    # to the clicked cell (proves routing, widget-agnostic, no side-effect), and
    # the app survived (no crash).
    assert facts.get("still_running") is True, f"app crashed on injected click: {facts}"
    assert facts.get("mouse_xy") == facts.get("click_target"), \
        f"injected click did not reach App.on_event (mouse_position): {facts}"


def test_pilot_mirror_text_drives_search():
    """End-to-end: with control ON and NO pane focused, browser text routed through
    _mirror_inject_input replays as Key events that App.on_event forwards to the
    focused widget + App.on_key, driving search-as-you-type — the #search box opens
    and fills. Proves typed text reaches saikai's OWN widgets, not just a live pane
    (the bug: 'can't input search text into the search box'). Drives the REAL app."""
    try:
        from textual.app import App  # noqa: F401
    except Exception:
        print("SKIP test_pilot_mirror_text_drives_search (textual unavailable)")
        return

    import asyncio
    from textual.app import App
    from textual.widgets import Input, DataTable

    _write_demo_session()
    facts: dict = {}

    def fake_run(self, *a, **kw):
        async def go():
            async with self.run_test(size=(110, 30)) as pilot:
                await pilot.pause(0.4)
                self.query_one("#table", DataTable).focus()   # list focused, no pane
                await pilot.pause(0.1)
                self._control_enabled = True
                facts["focused_terminal"] = self._focused_terminal()
                self._mirror_inject_input("hi")               # browser-typed text
                await pilot.pause(0.3)
                facts["search_value"] = self.query_one("#search", Input).value
            facts["ran"] = True
        asyncio.run(go())

    orig_run, App.run = App.run, fake_run
    orig_argv = sys.argv
    try:
        sys.argv = ["saikai", "--all"]
        saikai.main()
    finally:
        App.run = orig_run
        sys.argv = orig_argv

    assert facts.get("ran"), facts
    assert facts.get("focused_terminal") is None, \
        f"precondition: no pane should be focused (text must route as keys): {facts}"
    assert facts.get("search_value") == "hi", \
        f"typed text did not drive search-as-you-type: {facts}"


def test_pilot_mirror_arrow_byte_drives_app():
    """Terminal-equivalence: an ARROW-KEY byte sequence from the browser ("\\x1b[B")
    is parsed (Textual's XTermParser) into Key('down') and reaches App.on_key --
    here, with the search box focused, Down moves focus to the list (the documented
    behavior). Proves physical-arrow navigation works from the mirror, not just
    printables / the key bar. Drives the REAL PickerApp."""
    try:
        from textual.app import App  # noqa: F401
    except Exception:
        print("SKIP test_pilot_mirror_arrow_byte_drives_app (textual unavailable)")
        return

    import asyncio
    from textual.app import App

    _write_demo_session()
    facts: dict = {}

    def fake_run(self, *a, **kw):
        async def go():
            async with self.run_test(size=(110, 30)) as pilot:
                await pilot.pause(0.4)
                self._control_enabled = True
                # Type a letter -> opens + focuses the search box.
                self._mirror_inject_input("a")
                await pilot.pause(0.3)
                facts["focus_after_type"] = getattr(self.focused, "id", None)
                # A down-arrow BYTE sequence -> Key('down') -> on_key moves focus
                # from the search box to the list.
                self._mirror_inject_input("\x1b[B")
                await pilot.pause(0.3)
                facts["focus_after_down"] = getattr(self.focused, "id", None)
            facts["ran"] = True
        asyncio.run(go())

    orig_run, App.run = App.run, fake_run
    orig_argv = sys.argv
    try:
        sys.argv = ["saikai", "--all"]
        saikai.main()
    finally:
        App.run = orig_run
        sys.argv = orig_argv

    assert facts.get("ran"), facts
    assert facts.get("focus_after_type") == "search", \
        f"typing did not focus the search box: {facts}"
    assert facts.get("focus_after_down") == "table", \
        f"down-arrow byte did not reach on_key to move focus to the list: {facts}"


def _write_ssh_remote_session() -> str:
    """A Desktop-SSH mirror fixture: projects/ssh-<uuid>/ with a foreign (Linux)
    cwd and a queue-operation FIRST line, per the schema observed on a real
    Windows Claude Desktop install. (#remote-origin)"""
    sid = str(uuid.uuid4())
    pdir = _FAKE_HOME / ".claude" / "projects" / f"ssh-{uuid.uuid4()}"
    pdir.mkdir(parents=True, exist_ok=True)
    recs = [
        {"type": "queue-operation", "operation": "enqueue",
         "timestamp": "2026-07-01T00:00:00.000Z"},
        {"type": "user", "timestamp": "2026-07-01T00:01:00.000Z",
         "cwd": "/home/remoteuser/projects/demo",
         "message": {"role": "user", "content": "remote task over Desktop SSH"}},
        {"type": "queue-operation", "operation": "dequeue",
         "timestamp": "2026-07-01T00:02:00.000Z"},
    ]
    (pdir / f"{sid}.jsonl").write_text(
        "\n".join(json.dumps(r) for r in recs) + "\n", encoding="utf-8")
    return sid


def test_pilot_kill_agent_gated_and_verified():
    """Shift+K terminates a live AGENT process, but ONLY after a confirm and an
    identity check, and NEVER on a non-agent row. Verifies: (1) a non-bg row
    refuses with an info toast and pushes no modal; (2) a kind=agent row pushes
    the KillAgentScreen with the right pid; (3) confirming calls the kill helper
    with the registry (pid, procStart). The kill helper itself is stubbed so the
    test signals nothing real. (#agent-kill)"""
    try:
        from textual.app import App  # noqa: F401
    except Exception:
        print("SKIP test_pilot_kill_agent_gated_and_verified (textual unavailable)")
        return
    import asyncio, os
    from textual.app import App

    local_sid = _write_demo_session()
    agent_sid = _write_demo_session()
    sdir = _FAKE_HOME / ".claude" / "sessions"
    sdir.mkdir(parents=True, exist_ok=True)
    (sdir / f"{os.getpid()}.json").write_text(json.dumps(
        {"pid": os.getpid(), "sessionId": agent_sid, "status": "running",
         "kind": "agent", "name": "runaway", "procStart": "12345"}), encoding="utf-8")
    saikai._invalidate_active_sessions()
    saikai._set_group_by("state")
    _orig_live = saikai._is_session_pid_live
    saikai._is_session_pid_live = lambda pid, idx=None: pid == os.getpid()
    killed = []
    _orig_kill = saikai._kill_agent_process
    saikai._kill_agent_process = lambda pid, ps: (killed.append((pid, ps)) or "signalled")
    facts = {}

    def fake_run(self, *a, **kw):
        async def go():
            async with self.run_test(size=(110, 30), notifications=True) as pilot:
                await pilot.pause(0.4)
                # (1) non-agent row: focus the local demo, Ctrl+K refuses
                self._cursor_sid = lambda _s=local_sid: _s
                self.action_kill_agent()
                await pilot.pause(0.2)
                facts["nonbg_no_modal"] = not any(
                    type(s).__name__ == "KillAgentScreen" for s in self.screen_stack)
                facts["nonbg_toast"] = any("is neither" in str(n.message)
                                           for n in self._notifications)
                # (2) agent row: Ctrl+K opens the confirm
                self._cursor_sid = lambda _s=agent_sid: _s
                self.action_kill_agent()
                await pilot.pause(0.2)
                modal = next((s for s in self.screen_stack
                              if type(s).__name__ == "KillAgentScreen"), None)
                facts["agent_modal"] = modal is not None
                # (3) confirm → kill helper called with the registry identity
                if modal is not None:
                    modal.dismiss(True)
                    await pilot.pause(0.2)
                facts["killed"] = list(killed)
        asyncio.run(go())

    orig, App.run = App.run, fake_run
    orig_argv = sys.argv
    try:
        sys.argv = ["saikai", "--all"]
        saikai.main()
    finally:
        App.run = orig
        sys.argv = orig_argv
        saikai._is_session_pid_live = _orig_live
        saikai._kill_agent_process = _orig_kill
        saikai._invalidate_active_sessions()
        try:
            (sdir / f"{os.getpid()}.json").unlink()
        except OSError:
            pass
    assert facts.get("nonbg_no_modal") is True and facts.get("nonbg_toast") is True, facts
    assert facts.get("agent_modal") is True, f"agent row must open the confirm: {facts}"
    assert facts.get("killed") == [(os.getpid(), "12345")], \
        f"confirm must kill with the registry (pid, procStart): {facts}"


def test_pilot_agents_kind_groups_and_refusal():
    """claude's agents feature registers live sessions with kind="agent": they
    must (1) group under their own "Agents" section — not pollute "Open", which
    means windows you can act on; (2) refuse resume with a message that points
    at the parent claude's agents view (the old text talked about bg jobs);
    (3) carry live_kind for kind-aware UX. (#agents-kind)"""
    try:
        from textual.app import App  # noqa: F401
    except Exception:
        print("SKIP test_pilot_agents_kind_groups_and_refusal (textual unavailable)")
        return
    import asyncio, os
    from textual.app import App

    sid = _write_demo_session()
    sdir = _FAKE_HOME / ".claude" / "sessions"
    sdir.mkdir(parents=True, exist_ok=True)
    (sdir / f"{os.getpid()}.json").write_text(json.dumps(
        {"pid": os.getpid(), "sessionId": sid, "status": "running",
         "kind": "agent", "name": "teammate-1"}), encoding="utf-8")
    saikai._invalidate_active_sessions()   # earlier pilots memoised an empty registry
    saikai._set_group_by("state")          # the Agents section is a State-lens concept
    # the registry guard requires the pid to BE a claude process (PID-reuse
    # defense) — the test process is python, so stub liveness for this test
    _orig_live = saikai._is_session_pid_live
    saikai._is_session_pid_live = lambda pid, idx=None: pid == os.getpid()
    facts: dict = {}

    def fake_run(self, *a, **kw):
        async def go():
            async with self.run_test(size=(110, 30), notifications=True) as pilot:
                await pilot.pause(0.4)
                s = self._sid_index.get(sid)
                facts["is_bg"] = bool(s and s.get("is_bg"))
                facts["kind"] = s.get("live_kind") if s else None
                # the row must land under an "Agents" group header (state lens
                # is the default) — read the actual table rows
                tbl = self.query_one("#table")
                labels = []
                for i in range(tbl.row_count):
                    try:
                        labels.append(" ".join(str(c) for c in tbl.get_row_at(i)))
                    except Exception:
                        pass
                facts["has_agents_hdr"] = any("Agents" in l for l in labels)
                facts["open_hdr_rows"] = [l for l in labels if "Open (" in l]
                self._open_or_attach_live(sid)
                await pilot.pause(0.2)
                facts["no_pane"] = not (self._live is not None and self._live.has(sid))
                facts["toast"] = any("agents view" in str(n.message)
                                     for n in self._notifications)
        asyncio.run(go())

    orig, App.run = App.run, fake_run
    orig_argv = sys.argv
    try:
        sys.argv = ["saikai", "--all"]
        saikai.main()
    finally:
        App.run = orig
        sys.argv = orig_argv
        saikai._is_session_pid_live = _orig_live
        saikai._invalidate_active_sessions()
        try:
            (sdir / f"{os.getpid()}.json").unlink()
        except OSError:
            pass
    assert facts.get("is_bg") is True, facts
    assert facts.get("kind") == "agent", facts
    assert facts.get("has_agents_hdr") is True, \
        f"agents must group under their own header, not Open: {facts}"
    assert facts.get("no_pane") is True and facts.get("toast") is True, facts


def test_pilot_remote_origin_badge_and_resume_block():
    """Claude Desktop's SSH integration mirrors remotely-executed sessions into
    local projects/ssh-<uuid>/ (foreign cwd, queue-operation records). saikai
    must (1) LIST them without choking on the schema, (2) flag remote_origin,
    (3) REFUSE resume with an explanatory toast — a local `claude --resume`
    against a foreign-cwd transcript cannot work. (#remote-origin)"""
    try:
        from textual.app import App  # noqa: F401
    except Exception:
        print("SKIP test_pilot_remote_origin_badge_and_resume_block (textual unavailable)")
        return
    import asyncio
    from textual.app import App

    local_sid = _write_demo_session()
    ssh_sid = _write_ssh_remote_session()
    facts: dict = {}

    def fake_run(self, *a, **kw):
        async def go():
            async with self.run_test(size=(110, 30), notifications=True) as pilot:
                await pilot.pause(0.4)
                s = self._sid_index.get(ssh_sid)
                facts["listed"] = s is not None
                facts["remote_origin"] = bool(s and s.get("remote_origin"))
                facts["cwd"] = s.get("cwd") if s else None
                ls = self._sid_index.get(local_sid)
                facts["local_flag"] = bool(ls and ls.get("remote_origin"))
                facts["blocked"] = self._remote_origin_block(ssh_sid)
                facts["local_not_blocked"] = not self._remote_origin_block(local_sid)
                # the split-pane opener must return WITHOUT registering an open
                self._open_or_attach_live(ssh_sid)
                await pilot.pause(0.2)
                facts["no_pane"] = not (self._live is not None and self._live.has(ssh_sid))
                facts["toast"] = any("another host" in str(n.message)
                                     for n in self._notifications)
        asyncio.run(go())

    orig, App.run = App.run, fake_run
    orig_argv = sys.argv
    try:
        sys.argv = ["saikai", "--all"]
        saikai.main()
    finally:
        App.run = orig
        sys.argv = orig_argv
    assert facts.get("listed") is True, f"ssh-* session must be listed: {facts}"
    assert facts.get("remote_origin") is True, facts
    assert facts.get("cwd") == "/home/remoteuser/projects/demo", facts
    assert facts.get("local_flag") is False, "a normal session must NOT be flagged"
    assert facts.get("blocked") is True and facts.get("local_not_blocked") is True, facts
    assert facts.get("no_pane") is True, f"resume must not open a pane: {facts}"
    assert facts.get("toast") is True, f"the block must explain itself: {facts}"


def test_pilot_autorefresh_gate_catches_transcript_growth():
    """The default-on auto-refresh change signal must flip when a LISTED
    session's transcript GROWS (a new turn = a flip to 'needs input') and when a
    NEW session file lands in an EXISTING project dir. A directory mtime bumps
    only on entry add/remove, so the old dirs-only gate left the '!' attention
    marker frozen until F5 — the core value silently stale.
    (#audit-attention-freshness)"""
    try:
        from textual.app import App  # noqa: F401
    except Exception:
        print("SKIP test_pilot_autorefresh_gate_catches_transcript_growth (textual unavailable)")
        return
    import asyncio, json, time, uuid
    from textual.app import App

    sid = _write_demo_session()
    # find the demo session's transcript path via the loaded index
    facts: dict = {}

    def fake_run(self, *a, **kw):
        async def go():
            async with self.run_test(size=(110, 24)) as pilot:
                await pilot.pause(0.4)
                s = self._sid_index.get(sid) or next(iter(self._sid_index.values()), None)
                path = s and s.get("jsonl_path")
                if not path:
                    facts["skip"] = "no jsonl path"; return
                from pathlib import Path as _P
                p = _P(path)
                g0 = self._sessions_dirs_mtime()
                time.sleep(1.1)
                with open(p, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps({"type": "user", "cwd": "/w",
                        "timestamp": "2026-07-01T00:09:00.000Z",
                        "message": {"role": "user", "content": "waiting?"}}) + "\n")
                facts["grew"] = self._sessions_dirs_mtime() > g0
                time.sleep(1.1)
                g1 = self._sessions_dirs_mtime()
                (p.parent / f"{uuid.uuid4()}.jsonl").write_text("x\n", encoding="utf-8")
                facts["new_file"] = self._sessions_dirs_mtime() > g1
        asyncio.run(go())

    orig, App.run = App.run, fake_run
    orig_argv = sys.argv
    try:
        sys.argv = ["saikai", "--all"]
        saikai.main()
    finally:
        App.run = orig
        sys.argv = orig_argv
    if facts.get("skip"):
        print("SKIP:", facts["skip"]); return
    assert facts.get("grew") is True, f"gate must flip on transcript growth: {facts}"
    assert facts.get("new_file") is True, \
        f"gate must flip on a new session in an existing project: {facts}"


def test_pilot_mirror_resize_syncs_size():
    """A terminal resize must reach the mirror hub — it models the host at a
    fixed grid, so a frozen size garbles every absolute-positioned frame.
    on_resize → _mirror_sync_geometry → hub.set_size(new). (#mirror-resize)"""
    try:
        from textual.app import App  # noqa: F401
    except Exception:
        print("SKIP test_pilot_mirror_resize_syncs_size (textual unavailable)")
        return
    import asyncio
    from textual.app import App

    _write_demo_session()
    facts: dict = {}

    def fake_run(self, *a, **kw):
        async def go():
            async with self.run_test(size=(100, 30)) as pilot:
                await pilot.pause(0.3)
                sizes = []
                class _FakeHub:
                    _cols = 100; _rows = 30
                    def set_size(self, c, r): sizes.append((c, r)); self._cols=c; self._rows=r
                    def set_regions(self, regs): pass
                self._mirror_hub = _FakeHub()
                await pilot.resize_terminal(140, 45)
                await pilot.pause(0.3)
                facts["app"] = (self.size.width, self.size.height)
                facts["synced"] = sizes
        asyncio.run(go())

    orig, App.run = App.run, fake_run
    orig_argv = sys.argv
    try:
        sys.argv = ["saikai", "--all"]
        saikai.main()
    finally:
        App.run = orig
        sys.argv = orig_argv
    assert facts.get("app") == (140, 45), f"terminal did not resize: {facts}"
    assert (140, 45) in facts.get("synced", []), \
        f"resize must push the new size to the hub: {facts}"


def test_pilot_mirror_push_regions():
    """_mirror_push_regions publishes the session list's content rect (and any
    visible live pane) to the hub in CELL coords — the browser's select-mode
    edge auto-scroll needs the pane's own edges (#mirror-regions). Hub dedup is
    the hub's own test; here we assert the app-side collector's shape."""
    try:
        from textual.app import App  # noqa: F401
    except Exception:
        print("SKIP test_pilot_mirror_push_regions (textual unavailable)")
        return
    import asyncio
    from textual.app import App

    _write_demo_session()
    facts: dict = {}

    def fake_run(self, *a, **kw):
        async def go():
            async with self.run_test(size=(110, 30)) as pilot:
                await pilot.pause(0.4)
                pushed = []
                class _FakeHub:
                    def set_regions(self, regs):
                        pushed.append(regs)
                self._mirror_hub = _FakeHub()
                self._mirror_push_regions()
                facts["pushed"] = pushed
        asyncio.run(go())

    orig, App.run = App.run, fake_run
    orig_argv = sys.argv
    try:
        sys.argv = ["saikai", "--all"]
        saikai.main()
    finally:
        App.run = orig
        sys.argv = orig_argv
    assert facts.get("pushed"), f"collector must push to the hub: {facts}"
    regs = facts["pushed"][-1]
    lists = [r for r in regs if r.get("k") == "list"]
    assert lists and lists[0]["w"] > 0 and lists[0]["h"] > 0, \
        f"the session list rect must be published: {regs}"
    for r in regs:
        assert set(r) == {"x", "y", "w", "h", "k"}, f"region shape drifted: {r}"


def test_pilot_mirror_checkpoint_pseudo_key():
    """The mirror More row exposes checkpoint as the pseudo-key "checkpoint":
    it is a LEADER gesture (␣ c) in the TUI with no single key to synthesize,
    so _mirror_inject_key dispatches action_checkpoint directly — gated on
    control being ON, and never posted as a garbage Key event. (#mirror-checkpoint)"""
    try:
        from textual.app import App  # noqa: F401
    except Exception:
        print("SKIP test_pilot_mirror_checkpoint_pseudo_key (textual unavailable)")
        return
    import asyncio
    from textual.app import App

    _write_demo_session()
    facts: dict = {}

    def fake_run(self, *a, **kw):
        async def go():
            async with self.run_test(size=(120, 24)) as pilot:
                await pilot.pause(0.3)
                calls = []
                self.action_checkpoint = lambda: calls.append(True)
                self._control_enabled = False
                self._mirror_inject_key("checkpoint")
                facts["gated_off"] = len(calls) == 0
                self._control_enabled = True
                self._mirror_inject_key("checkpoint")
                facts["dispatched"] = len(calls) == 1
                await pilot.pause(0.05)
        asyncio.run(go())

    orig, App.run = App.run, fake_run
    orig_argv = sys.argv
    try:
        sys.argv = ["saikai", "--all"]
        saikai.main()
    finally:
        App.run = orig
        sys.argv = orig_argv
    assert facts.get("gated_off") is True, f"control OFF must not dispatch: {facts}"
    assert facts.get("dispatched") is True, f"pseudo-key must run action_checkpoint: {facts}"


def test_pilot_mirror_space_leader_runs_mnemonic():
    """Browser Space leader: a Space byte then a mnemonic byte ('f' = favorite),
    injected via the mirror input path with the list focused, must arm the Space
    leader on the App and run the mapped action -- i.e. Space-then-key works from
    the browser keyboard. Drives the REAL PickerApp."""
    try:
        from textual.app import App  # noqa: F401
    except Exception:
        print("SKIP test_pilot_mirror_space_leader_runs_mnemonic (textual unavailable)")
        return

    import asyncio
    from textual.app import App
    from textual.widgets import DataTable

    _write_demo_session()
    facts: dict = {}

    def fake_run(self, *a, **kw):
        async def go():
            async with self.run_test(size=(110, 30)) as pilot:
                await pilot.pause(0.4)
                self.query_one("#table", DataTable).focus()
                await pilot.pause(0.1)
                self._control_enabled = True
                facts["leader_key"] = getattr(self, "_leader_key", None)
                sid = self._cursor_sid()
                fav = lambda: sid in (saikai._read_json(saikai.FAVORITE_FILE, []) or [])
                facts["sid"] = sid
                facts["before"] = fav()
                self._mirror_inject_input(" ")        # Space -> arm the leader
                await pilot.pause(0.2)
                facts["pending_after_space"] = getattr(self, "_leader_pending", None)
                self._mirror_inject_input("f")        # mnemonic -> favorite
                await pilot.pause(0.3)
                facts["after"] = fav()
            facts["ran"] = True
        asyncio.run(go())

    orig_run, App.run = App.run, fake_run
    orig_argv = sys.argv
    try:
        sys.argv = ["saikai", "--all"]
        saikai.main()
    finally:
        App.run = orig_run
        sys.argv = orig_argv

    assert facts.get("ran"), facts
    assert facts.get("sid"), f"no row under the cursor: {facts}"
    assert facts.get("pending_after_space") is True, \
        f"Space byte did not arm the leader: {facts}"
    assert facts.get("after") != facts.get("before"), \
        f"Space+f did not run the favorite mnemonic: {facts}"


def test_pilot_rename_modal_enter_saves_not_resumes():
    """Reported bug: open the rename box (Shift+F2), type a name, press Enter —
    instead of saving, the LIST's resume fired (focus jumped to the list and it
    resumed). Cause: `enter->resume` is a priority binding, and Textual checks
    priority bindings against the FULL binding chain (App included), IGNORING the
    modal boundary that `_modal_binding_chain` enforces for normal bindings. So
    the App's Enter leaked past the RenameScreen modal. Enter in the modal must
    SAVE the typed name and NOT dispatch resume."""
    try:
        from textual.app import App  # noqa: F401
    except Exception:
        print("SKIP test_pilot_rename_modal_enter_saves_not_resumes (textual unavailable)")
        return
    import asyncio
    from textual.app import App

    sid = _write_demo_session()
    facts: dict = {}

    def fake_run(self, *a, **kw):
        async def go():
            async with self.run_test(size=(110, 30)) as pilot:
                await pilot.pause(0.4)
                # Record (and neutralize) any resume dispatch: the real action
                # would spawn `claude --resume` as a PTY child during the test.
                self.action_resume = lambda: facts.__setitem__("resume_fired", True)
                # The rename targets the cursored row; other suites in this
                # process seed extra sessions, so capture the actual target sid
                # rather than assuming the cursor sits on our demo row.
                facts["target"] = self._cursor_sid()
                await pilot.press("space", "e")          # open the rename modal
                await pilot.pause(0.2)
                facts["modal_open"] = type(self.screen).__name__ == "RenameScreen"
                for ch in "myname":
                    await pilot.press(ch)
                await pilot.press("enter")               # must SAVE, not resume
                await pilot.pause(0.3)
                facts["stack_after"] = len(self.screen_stack)
                facts["saved"] = (saikai._load_custom_titles() or {}).get(facts["target"], "")
        asyncio.run(go())

    orig_run, App.run = App.run, fake_run
    orig_argv = sys.argv
    try:
        sys.argv = ["saikai", "--all"]
        saikai.main()
    finally:
        App.run = orig_run
        sys.argv = orig_argv

    assert facts.get("modal_open"), f"Shift+F2 did not open RenameScreen: {facts}"
    assert not facts.get("resume_fired"), f"Enter in the rename modal fired resume (BUG): {facts}"
    assert facts.get("saved") == "myname", f"Enter did not save the typed name: {facts}"
    assert facts.get("stack_after") == 1, f"rename modal did not close on Enter: {facts}"


def test_pilot_ctrlc_over_modal_does_not_quit():
    """Bug-hunt finding (same root as the rename Enter leak): over a no-Input modal
    (Help / Mirror QR / Settings) a reflex DOUBLE Ctrl+C quit the whole app. on_key's
    quit guard had no modal guard and those modals define no ctrl+c binding, so
    Ctrl+C bubbled to on_key -> _confirm_quit -> action_quit_all. Ctrl+C over a modal
    must NOT quit; the modal stays open (Esc closes it). (Ctrl+Q, a priority binding
    -> action_quit, shares action_quit's new screen_stack guard.)"""
    try:
        from textual.app import App  # noqa: F401
    except Exception:
        print("SKIP test_pilot_ctrlc_over_modal_does_not_quit (textual unavailable)")
        return
    import asyncio
    from textual.app import App

    _write_demo_session()
    facts: dict = {}

    def fake_run(self, *a, **kw):
        async def go():
            async with self.run_test(size=(110, 30)) as pilot:
                await pilot.pause(0.4)
                self.action_quit_all = lambda: facts.__setitem__("quit", True)
                await pilot.press("question_mark")       # open Help (priority binding)
                await pilot.pause(0.2)
                facts["screen"] = type(self.screen).__name__
                await pilot.press("ctrl+c")              # reflex...
                await pilot.press("ctrl+c")              # ...twice
                await pilot.pause(0.2)
                facts["quit_fired"] = facts.get("quit", False)
                facts["screen_after"] = type(self.screen).__name__
        asyncio.run(go())

    orig_run, App.run = App.run, fake_run
    orig_argv = sys.argv
    try:
        sys.argv = ["saikai", "--all"]
        saikai.main()
    finally:
        App.run = orig_run
        sys.argv = orig_argv

    assert facts.get("screen") == "HelpScreen", f"'?' did not open Help: {facts}"
    assert not facts.get("quit_fired"), f"Ctrl+C over a modal quit the app (BUG): {facts}"
    assert facts.get("screen_after") == "HelpScreen", f"Ctrl+C should leave Help open: {facts}"


def test_pilot_notification_center_records_and_opens():
    """Feature 3: toasts auto-dismiss, so a missed 'needs input'/'done'/error was
    gone. notify() now keeps a bounded recall log and F11 opens a
    NotificationsScreen listing them (drawn in the TUI, so mirror-visible)."""
    try:
        from textual.app import App  # noqa: F401
    except Exception:
        print("SKIP test_pilot_notification_center_records_and_opens (textual unavailable)")
        return
    import asyncio
    from textual.app import App

    _write_demo_session()
    facts: dict = {}

    def fake_run(self, *a, **kw):
        async def go():
            async with self.run_test(size=(110, 30)) as pilot:
                await pilot.pause(0.4)
                self.notify("recall-me-xyz", severity="warning", title="T")
                await pilot.pause(0.1)
                facts["logged"] = any("recall-me-xyz" in e[3]
                                      for e in getattr(self, "_notif_log", []))
                await pilot.press("space", "m")        # open the notification center
                await pilot.pause(0.2)
                facts["screen"] = type(self.screen).__name__
                await pilot.press("escape")            # close it
                await pilot.pause(0.1)
                facts["screen_after"] = type(self.screen).__name__
        asyncio.run(go())

    orig_run, App.run = App.run, fake_run
    orig_argv = sys.argv
    try:
        sys.argv = ["saikai", "--all"]
        saikai.main()
    finally:
        App.run = orig_run
        sys.argv = orig_argv

    assert facts.get("logged"), f"notify() must record into the recall log: {facts}"
    assert facts.get("screen") == "NotificationsScreen", f"F11 must open the center: {facts}"
    assert facts.get("screen_after") != "NotificationsScreen", f"Esc must close it: {facts}"


def test_pilot_esc_in_search_clears_filter():
    """Bug-hunt 1A: a non-empty search filter (especially with the bar hidden)
    read as 'sessions missing', and Esc only moved focus — it never cleared the
    query. Esc in the search box now CLEARS an active filter first, then returns
    to the list."""
    try:
        from textual.app import App  # noqa: F401
    except Exception:
        print("SKIP test_pilot_esc_in_search_clears_filter (textual unavailable)")
        return
    import asyncio
    from textual.app import App

    _write_demo_session()
    facts: dict = {}

    def fake_run(self, *a, **kw):
        async def go():
            async with self.run_test(size=(110, 30)) as pilot:
                await pilot.pause(0.4)
                await pilot.press("slash")             # open + focus the search box
                await pilot.pause(0.1)
                for ch in "zzz":
                    await pilot.press(ch)
                await pilot.pause(0.1)
                inp = self.query_one("#search")
                facts["typed"] = inp.value
                facts["focus_was_search"] = self.focused is inp
                await pilot.press("escape")            # clear the filter
                await pilot.pause(0.1)
                facts["after_value"] = self.query_one("#search").value
                facts["focus_table"] = self.focused is self.query_one("#table")
        asyncio.run(go())

    orig_run, App.run = App.run, fake_run
    orig_argv = sys.argv
    try:
        sys.argv = ["saikai", "--all"]
        saikai.main()
    finally:
        App.run = orig_run
        sys.argv = orig_argv

    assert facts.get("typed") == "zzz", f"typing into the search box failed: {facts}"
    assert facts.get("focus_was_search"), f"search box should hold focus: {facts}"
    assert facts.get("after_value") == "", f"Esc should clear the filter: {facts}"
    assert facts.get("focus_table"), f"Esc should return focus to the list: {facts}"


def test_pilot_quit_arm_cleared_when_a_screen_opens():
    """Bug-hunt C2: arm the quit guard (one Esc on the list), then open a screen
    via a PRIORITY binding (? -> Help). The priority binding bypasses on_key's
    disarm, so the arm used to dangle — a single later Esc on the list then quit.
    push_screen now clears the arm, so after the screen closes a single Esc only
    re-arms (does not quit)."""
    try:
        from textual.app import App  # noqa: F401
    except Exception:
        print("SKIP test_pilot_quit_arm_cleared_when_a_screen_opens (textual unavailable)")
        return
    import asyncio
    from textual.app import App

    _write_demo_session()
    facts: dict = {}

    def fake_run(self, *a, **kw):
        async def go():
            async with self.run_test(size=(110, 30)) as pilot:
                await pilot.pause(0.4)
                orig_exit = self.exit
                self.exit = lambda *a, **k: facts.__setitem__("quit", True)
                self.action_quit_all = lambda: facts.__setitem__("quit", True)
                await pilot.press("escape")            # arm the quit guard
                await pilot.pause(0.05)
                facts["armed"] = getattr(self, "_quit_armed", False)
                await pilot.press("question_mark")     # priority binding -> push Help
                await pilot.pause(0.1)
                facts["armed_after_push"] = getattr(self, "_quit_armed", False)
                facts["help"] = type(self.screen).__name__
                await pilot.press("escape")            # dismiss Help
                await pilot.pause(0.1)
                await pilot.press("escape")            # single Esc on list -> must NOT quit
                await pilot.pause(0.1)
                facts["quit_fired"] = facts.get("quit", False)
                self.exit = orig_exit                  # restore for clean teardown
        asyncio.run(go())

    orig_run, App.run = App.run, fake_run
    orig_argv = sys.argv
    try:
        sys.argv = ["saikai", "--all"]
        saikai.main()
    finally:
        App.run = orig_run
        sys.argv = orig_argv

    assert facts.get("armed"), f"first Esc should arm the quit guard: {facts}"
    assert facts.get("help") == "HelpScreen", f"'?' should open Help: {facts}"
    assert not facts.get("armed_after_push"), f"opening a screen must clear the quit arm: {facts}"
    assert not facts.get("quit_fired"), f"single Esc after a screen closed must not quit: {facts}"


def test_pilot_open_parent():
    """Shift+F6 (open_parent) jumps the cursor to the session recorded as the
    parent of the currently-selected child session via _set_lineage."""
    try:
        from textual.app import App  # noqa: F401
    except Exception:
        print("SKIP test_pilot_open_parent (textual unavailable)"); return
    import asyncio
    from textual.app import App

    # Write two demo sessions: child and parent.
    parent_sid = _write_demo_session()
    child_sid = _write_demo_session()
    # Record lineage: child -> parent.
    pdir = _FAKE_HOME / ".claude" / "projects" / "-home-alex-code-demo"
    parent_jsonl = str(pdir / f"{parent_sid}.jsonl")
    saikai._set_lineage(child_sid, parent_sid, parent_jsonl)
    facts: dict = {}

    def fake_run(self, *a, **kw):
        async def go():
            async with self.run_test(size=(110, 30)) as pilot:
                await pilot.pause(0.4)
                # Move cursor to the child row.
                table = self.query_one("#table")
                try:
                    table.move_cursor(row=table.get_row_index(child_sid))
                except Exception as e:
                    facts["setup_error"] = repr(e)
                    return
                await pilot.pause(0.1)
                facts["cursor_before"] = self._cursor_sid()
                # Invoke open_parent action.
                await pilot.app.run_action("open_parent")
                await pilot.pause(0.2)
                facts["cursor_after"] = self._cursor_sid()
        asyncio.run(go())

    orig_run, App.run = App.run, fake_run
    orig_argv = sys.argv
    try:
        sys.argv = ["saikai", "--all"]
        saikai.main()
    finally:
        App.run = orig_run
        sys.argv = orig_argv

    assert facts.get("cursor_before") == child_sid, \
        f"cursor should start on child: {facts}"
    assert facts.get("cursor_after") == parent_sid, \
        f"open_parent should jump to parent sid: {facts}"


def test_pilot_ctx_gauge_in_statusbar():
    """A focused live pane shows a ctx gauge in the statusbar from the transcript's
    usage block. (Stubs a live pane + sid_index entry; no real claude.)"""
    try:
        from textual.app import App  # noqa: F401
    except Exception:
        print("SKIP test_pilot_ctx_gauge_in_statusbar (textual unavailable)"); return
    import asyncio, json, uuid
    from textual.app import App
    sid = str(uuid.uuid4())
    pdir = _FAKE_HOME / ".claude" / "projects" / "-ctx-demo"
    pdir.mkdir(parents=True, exist_ok=True)
    jp = pdir / f"{sid}.jsonl"
    jp.write_text(json.dumps({"type": "assistant", "message": {"model": "claude-opus-4-8",
        "usage": {"input_tokens": 100, "cache_read_input_tokens": 95_000,
                  "cache_creation_input_tokens": 900, "output_tokens": 10}}}) + "\n",
        encoding="utf-8")
    facts = {}
    def fake_run(self, *a, **kw):
        async def go():
            async with self.run_test(size=(140, 30)) as pilot:
                await pilot.pause(0.3)
                facts["seg"] = saikai._ctx_gauge_segment(
                    saikai._ctx_tokens_from_jsonl(str(jp)),
                    saikai._ctx_window_for(saikai._ctx_tokens_from_jsonl(str(jp))))
        asyncio.run(go())
    orig, App.run = App.run, fake_run
    try:
        sys.argv = ["saikai", "--all"]; saikai.main()
    finally:
        App.run = orig
    # 96000/200000 = 48% -> the segment renders the gauge.
    assert "ctx 96K/200K (48%)" in facts.get("seg", ""), facts


def test_pilot_context_refresh_idle_and_busy():
    """action_context_refresh (Shift+F11):
    - idle pane  -> paste_text('/compact') + submit() are called
    - busy pane  -> neither is called (toast warning only)
    - no focused pane -> neither called (notify only)"""
    try:
        from textual.app import App  # noqa: F401
    except Exception:
        print("SKIP test_pilot_context_refresh_idle_and_busy (textual unavailable)"); return
    import asyncio
    from textual.app import App

    facts: dict = {}

    def fake_run(self, *a, **kw):
        async def go():
            async with self.run_test(size=(110, 30)) as pilot:
                await pilot.pause(0.3)

                # Build a minimal fake terminal that records paste_text/submit calls.
                class _FakeTerm:
                    sid = "fake-sid-refresh"
                    is_dead = False
                    def __init__(self):
                        self.paste_calls = []
                        self.submit_calls = []
                    def paste_text(self, text):
                        self.paste_calls.append(text)
                    def submit(self):
                        self.submit_calls.append(True)

                # Build a minimal fake LiveSessionManager whose statuses() is controllable.
                class _FakeLive:
                    def __init__(self, status_val):
                        self._status_val = status_val
                    def statuses(self):
                        return {"fake-sid-refresh": self._status_val}
                    def set_status(self, sid, status):
                        pass          # _b1_tick refreshes the target's status
                    def all_terms(self):
                        return []     # _poll_live_status iterates this each tick
                    def pane_id(self, sid: str) -> str:
                        return f"tab-live-{sid}"

                fake_term = _FakeTerm()

                # --- Test 1: idle pane -> inject /compact, settle, CR, verify ---
                # (#audit-b1-verify: the CR now comes from the tick machine after
                # a palette settle, and success requires the pane to go busy)
                self._live = _FakeLive("idle")
                self._focused_terminal = lambda: fake_term
                # Drive the same poll that the 1.5s Textual timer runs. Fast
                # machines used to finish this test before the timer fired,
                # hiding an incomplete _FakeLive contract that crashed on
                # slower Windows hosts.
                self._poll_live_status()
                await pilot.app.run_action("context_refresh")
                await pilot.pause(0.05)
                facts["idle_paste"] = list(fake_term.paste_calls)
                facts["idle_submit_immediate"] = list(fake_term.submit_calls)
                # drive the machine: 2 settle ticks -> CR; then flip busy -> done
                for _ in range(3):
                    if getattr(self, "_b1", None) is None:
                        break
                    self._b1_tick()
                facts["idle_submit"] = list(fake_term.submit_calls)
                self._live = _FakeLive("busy")   # the compact turn started
                for _ in range(3):
                    if getattr(self, "_b1", None) is None:
                        break
                    self._b1_tick()
                facts["idle_b1_done"] = getattr(self, "_b1", None) is None
                self._live = _FakeLive("idle")   # restore for the next cases

                # --- Test 2: busy pane -> should NOT inject ---
                fake_term.paste_calls.clear()
                fake_term.submit_calls.clear()
                self._live = _FakeLive("busy")
                await pilot.app.run_action("context_refresh")
                await pilot.pause(0.1)
                facts["busy_paste"] = list(fake_term.paste_calls)
                facts["busy_submit"] = list(fake_term.submit_calls)

                # --- Test 3: waiting pane -> should NOT inject ---
                fake_term.paste_calls.clear()
                fake_term.submit_calls.clear()
                self._live = _FakeLive("waiting")
                await pilot.app.run_action("context_refresh")
                await pilot.pause(0.1)
                facts["waiting_paste"] = list(fake_term.paste_calls)

                # --- Test 4: no focused pane -> no inject ---
                fake_term.paste_calls.clear()
                fake_term.submit_calls.clear()
                self._focused_terminal = lambda: None
                await pilot.app.run_action("context_refresh")
                await pilot.pause(0.1)
                facts["none_paste"] = list(fake_term.paste_calls)

        asyncio.run(go())

    orig_run, App.run = App.run, fake_run
    orig_argv = sys.argv
    try:
        sys.argv = ["saikai", "--all"]
        saikai.main()
    finally:
        App.run = orig_run
        sys.argv = orig_argv

    assert facts.get("idle_paste") == ["/compact"], \
        f"idle pane should inject /compact: {facts}"
    # the CR must NOT ride in the same tick as the paste (palette absorb) —
    # it comes from the tick machine after the settle. (#audit-b1-verify)
    assert facts.get("idle_submit_immediate") == [], \
        f"CR must not be sent in the same tick as the paste: {facts}"
    assert facts.get("idle_submit") == [True], \
        f"the settle ticks must send exactly one CR: {facts}"
    assert facts.get("idle_b1_done") is True, \
        f"b1 must finish once the pane goes busy: {facts}"
    assert facts.get("busy_paste") == [], \
        f"busy pane must not inject: {facts}"
    assert facts.get("busy_submit") == [], \
        f"busy pane must not submit: {facts}"
    assert facts.get("waiting_paste") == [], \
        f"waiting pane must not inject: {facts}"
    assert facts.get("none_paste") == [], \
        f"no focused pane must not inject: {facts}"


def test_pilot_checkpoint_gated_clear_and_lineage():
    """b2 (Task 11) — human-gated checkpoint → /handoff → confirm → /clear →
    reseed → lineage, driven as a tick state machine:

    - action_checkpoint on an idle pane injects /handoff (NOT /clear).
    - the machine waits for the handoff to settle, extracts the NEW SESSION
      PROMPT from the transcript, and pushes ConfirmRefreshScreen.
    - while the modal is up, NO /clear has been injected (the destructive step
      is gated behind the human confirm).
    - dismissing the modal with Enter resumes the machine: /clear is injected,
      the fresh child sid is detected, the reseed prompt is injected, and
      _set_lineage records child -> parent.
    """
    try:
        from textual.app import App  # noqa: F401
    except Exception:
        print("SKIP test_pilot_checkpoint_gated_clear_and_lineage (textual unavailable)"); return
    import asyncio, json, uuid
    from textual.app import App

    pane_cwd = "/home/alex/code/demo"
    pdir = _FAKE_HOME / ".claude" / "projects" / "-home-alex-code-demo"
    pdir.mkdir(parents=True, exist_ok=True)
    parent_sid = str(uuid.uuid4())
    parent_jsonl = pdir / f"{parent_sid}.jsonl"
    # Parent transcript starts WITHOUT the handoff exchange: b2's freshness gate
    # (#audit-b2-freshness) requires the transcript to GROW after the inject, so
    # the test appends the /handoff exchange AFTER the machine starts — exactly
    # like the real handoff turn writing its records.
    parent_recs = [
        {"type": "ai-title", "aiTitle": "Parent", "timestamp": "2026-06-17T09:00:00.000Z",
         "cwd": pane_cwd},
    ]
    parent_jsonl.write_text("\n".join(json.dumps(r) for r in parent_recs) + "\n",
                            encoding="utf-8")
    handoff_recs = [
        {"type": "user", "timestamp": "2026-06-17T09:05:00.000Z", "cwd": pane_cwd,
         "message": {"role": "user", "content": "/handoff"}},
        {"type": "assistant", "timestamp": "2026-06-17T09:05:30.000Z", "cwd": pane_cwd,
         "message": {"role": "assistant", "content": [{"type": "text", "text":
            "Handoff ready.\n\n```\nNEW SESSION PROMPT\n"
            "Resume saikai Task 11: the parent built the state machine; "
            "continue from the failing detect test.\n```\n"}]}},
    ]
    child_sid = str(uuid.uuid4())          # the sid /clear will "mint"
    facts: dict = {}

    def fake_run(self, *a, **kw):
        async def go():
            async with self.run_test(size=(120, 32)) as pilot:
                await pilot.pause(0.3)

                class _FakeTerm:
                    """Records the ORDER of paste/submit so we can prove /clear
                    is never injected before the modal is confirmed."""
                    sid = parent_sid
                    is_dead = False
                    def __init__(self):
                        self.events = []
                    def paste_text(self, text):
                        self.events.append(("paste", text))
                    def submit(self):
                        self.events.append(("submit",))

                class _FakeLive:
                    # Implements the slice of LiveSessionManager the running app's
                    # background _poll_live_status / _restat_live touch (they fire
                    # on a 1.5s interval while this test spans several pauses).
                    # Backed by REAL dicts (not a hard-coded parent_sid) so the b2
                    # record_lineage re-key is observable: after the checkpoint the
                    # pane must be found under the CHILD sid, not the parent.
                    def __init__(self, status_val):
                        self._terms = {parent_sid: fake_term}
                        self._status = {parent_sid: status_val}
                        self._pane_ids = {parent_sid: f"tab-live-{parent_sid}"}
                    def statuses(self):
                        return dict(self._status)
                    def all_terms(self):
                        return list(self._terms.values())
                    def get(self, sid):
                        return self._terms.get(sid)
                    def has(self, sid):
                        return sid in self._terms
                    def pane_id(self, sid):
                        return self._pane_ids.get(sid) or f"tab-live-{sid}"
                    def set_status(self, sid, status):
                        if sid in self._terms:
                            self._status[sid] = status
                    def status(self, sid):
                        return self._status.get(sid, "")
                    def rekey(self, old_sid, new_sid):
                        if old_sid == new_sid or old_sid not in self._terms:
                            return
                        self._terms[new_sid] = self._terms.pop(old_sid)
                        if old_sid in self._status:
                            self._status[new_sid] = self._status.pop(old_sid)
                        if old_sid in self._pane_ids:
                            self._pane_ids[new_sid] = self._pane_ids.pop(old_sid)
                    @property
                    def count(self):
                        return len(self._terms)

                fake_term = _FakeTerm()
                self._live = _FakeLive("idle")
                self._focused_terminal = lambda: fake_term
                # sid_index entry so the machine can resolve the pane's transcript
                # path + project dir (parent_jsonl) the way the real app does.
                self._sid_index[parent_sid] = {
                    "id": parent_sid, "jsonl_path": parent_jsonl,
                    "cwd": pane_cwd, "origin_cwd": pane_cwd,
                }
                # The pane was opened under the PARENT sid, so restore currently
                # targets the parent. After the checkpoint re-keys the pane to the
                # child, _opened_sids must point at the child instead (harm #1).
                self._opened_sids.add(parent_sid)

                def drive_until(state, cap=40):
                    """Advance the tick machine until it reaches `state` (or the
                    machine ends / cap hit). Returns the state actually reached."""
                    for _ in range(cap):
                        b2 = getattr(self, "_b2", None)
                        if b2 is None:
                            return None
                        if b2.get("state") == state:
                            return state
                        self._b2_tick()
                    return (getattr(self, "_b2", None) or {}).get("state")

                # Kick off the flow.
                await pilot.app.run_action("checkpoint")
                await pilot.pause(0.05)
                facts["started"] = getattr(self, "_b2", None) is not None
                # Simulate the handoff turn writing its records: the transcript
                # must GROW past the pre-inject size or the freshness gate
                # (#audit-b2-freshness) rightly refuses to extract.
                with parent_jsonl.open("a", encoding="utf-8") as _f:
                    for _r in handoff_recs:
                        _f.write(json.dumps(_r) + "\n")
                # The user immediately navigates AWAY from the checkpointed session
                # (focus leaves it). The machine must NOT stall/timeout — it tracks
                # the captured b2["sid"]/term, not the focused pane. Simulate that by
                # dropping the focused-terminal; the rest of the flow must still
                # reach the modal, /clear after confirm, and record lineage.
                self._focused_terminal = lambda: None

                # Drive up to the confirm modal. Handoff is injected; /clear is NOT.
                reached = drive_until("confirm")
                # the confirm step pushes the modal on the tick that enters it;
                # run one more tick to actually push it if needed.
                for _ in range(3):
                    if type(self.screen).__name__ == "ConfirmRefreshScreen":
                        break
                    self._b2_tick()
                    await pilot.pause(0.05)
                facts["reached_confirm"] = reached
                facts["modal"] = type(self.screen).__name__
                facts["events_at_modal"] = list(fake_term.events)
                # the extracted prompt is shown in the modal (so the human can vet it)
                try:
                    facts["modal_shows_prompt"] = "Resume saikai Task 11" in \
                        self.screen.prompt_text()
                except Exception as e:  # noqa: BLE001
                    facts["modal_err"] = repr(e)

                # The destructive /clear must NOT have happened yet.
                facts["clear_before_confirm"] = any(
                    e == ("paste", "/clear") for e in fake_term.events)

                # Confirm with Ctrl+S -> the machine resumes past the gate (Enter now
                # inserts a newline in the editable prompt; Ctrl+S commits the edit).
                # The modal swallows a proceed within 0.4s of mount (the async-push
                # mid-typing guard, #audit-b2-modal-arm) — wait it out first.
                await pilot.pause(0.5)
                await pilot.press("ctrl+s")
                await pilot.pause(0.1)
                facts["modal_after_enter"] = type(self.screen).__name__

                # Advance to just-after the clear injection, THEN simulate claude
                # minting the fresh child session (its JSONL appears in the project
                # dir). This mirrors the real ~2.5-4s latency: the snapshot is taken
                # before /clear, the child file lands after.
                drive_until("detect_child")
                # Derive the child's transcript ts from the ACTUAL recorded clear
                # instant (+2s, UTC) so the post-date check holds at any wall-clock
                # time / host timezone — a hardcoded date made this clock-flaky.
                from datetime import datetime as _dt, timedelta as _td
                _clear = (getattr(self, "_b2", None) or {}).get("clear_ts") or ""
                assert _clear, "clear_ts must be recorded before detect_child"
                _child_ts = (_dt.fromisoformat(_clear.replace("Z", "+00:00"))
                             + _td(seconds=2)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
                child_jsonl = pdir / f"{child_sid}.jsonl"
                child_jsonl.write_text("\n".join(json.dumps(r) for r in [
                    {"type": "mode", "sessionId": child_sid},
                    {"type": "file-history-snapshot"},
                    {"type": "attachment", "cwd": pane_cwd,
                     "timestamp": _child_ts},
                ]) + "\n", encoding="utf-8")

                # Finish the machine (detect -> reseed -> verify -> record ->
                # done). verify_reseed only advances once the pane visibly starts
                # the reseed turn — flip the status to busy as soon as the reseed
                # paste is observed, exactly like the real turn spinning up.
                for _ in range(60):
                    if getattr(self, "_b2", None) is None:
                        break
                    if any(e[0] == "paste" and "Resume saikai Task 11" in e[1]
                           for e in fake_term.events if len(e) >= 2):
                        self._live.set_status(parent_sid, "busy")
                    self._b2_tick()
                    await pilot.pause(0.02)

                facts["events_final"] = list(fake_term.events)
                facts["lineage"] = saikai._load_lineage().get(child_sid)
                facts["live_jsonl"] = getattr(fake_term, "_live_jsonl", None)
                # --- re-key facts: after the checkpoint the SAME pane IS the child.
                facts["opened_has_child"] = child_sid in self._opened_sids
                facts["opened_has_parent"] = parent_sid in self._opened_sids
                facts["live_has_child"] = self._live.has(child_sid)
                facts["live_has_parent"] = self._live.has(parent_sid)
                facts["term_sid"] = getattr(fake_term, "sid", None)
                facts["child_in_sid_index"] = child_sid in self._sid_index
                facts["parent_in_sid_index"] = parent_sid in self._sid_index
                facts["child_lineage_parent"] = (saikai._load_lineage().get(child_sid) or {}).get("parent")

                # --- Esc (cancel) path: a SECOND checkpoint, dismissed at the
                # confirm modal with Esc, must leave the session UNTOUCHED (no
                # /clear injected) and tear the machine down — the other half of
                # the human-gate safety contract (Enter=proceed was asserted above).
                # Re-focus the pane so this second checkpoint can RESOLVE its target
                # (starting needs a focused/cursor live pane; the de-focus above was
                # to prove the RUNNING machine doesn't stall, which it didn't).
                # The pane is now the CHILD (re-keyed above), so the 2nd checkpoint
                # targets the child sid + its transcript — give that transcript its
                # own /handoff exchange (a reseeded child you checkpoint again has
                # worked + handed off) so extract_prompt can reach the confirm modal.
                # The reseed-verify flip above left the (re-keyed) pane 'busy' —
                # settle it back to idle or the 2nd checkpoint's midturn gate
                # rightly refuses to start.
                self._live.set_status(child_sid, "idle")
                self._focused_terminal = lambda: fake_term
                fake_term.events.clear()
                await pilot.app.run_action("checkpoint")
                await pilot.pause(0.05)
                # append the child's own /handoff exchange AFTER the start, past
                # the freshness gate (same as the first run).
                with child_jsonl.open("a", encoding="utf-8") as _f:
                    for _r in [
                        {"type": "user", "timestamp": "2026-06-17T10:00:00.000Z",
                         "cwd": pane_cwd, "message": {"role": "user", "content": "/handoff"}},
                        {"type": "assistant", "timestamp": "2026-06-17T10:00:30.000Z",
                         "cwd": pane_cwd, "message": {"role": "assistant", "content": [
                            {"type": "text", "text":
                             "Handoff ready.\n\n```\nNEW SESSION PROMPT\n"
                             "Continue the child's work from here.\n```\n"}]}},
                    ]:
                        _f.write(json.dumps(_r) + "\n")
                drive_until("confirm")
                for _ in range(3):
                    if type(self.screen).__name__ == "ConfirmRefreshScreen":
                        break
                    self._b2_tick()
                    await pilot.pause(0.05)
                facts["esc_modal_shown"] = type(self.screen).__name__
                await pilot.press("escape")
                await pilot.pause(0.1)
                facts["esc_modal_after"] = type(self.screen).__name__
                facts["esc_b2_torn_down"] = getattr(self, "_b2", None) is None
                facts["esc_events"] = list(fake_term.events)

        asyncio.run(go())

    orig_run, App.run = App.run, fake_run
    orig_argv = sys.argv
    try:
        sys.argv = ["saikai", "--all"]
        saikai.main()
    finally:
        App.run = orig_run
        sys.argv = orig_argv

    assert facts.get("started"), f"action_checkpoint did not start the machine: {facts}"
    assert facts.get("modal") == "ConfirmRefreshScreen", \
        f"the confirm modal must be shown before /clear: {facts}"
    assert facts.get("modal_shows_prompt"), \
        f"the modal must display the extracted NEW SESSION PROMPT: {facts}"
    # the load-bearing safety assertion: the handoff prompt was injected, /clear
    # was NOT, at the moment the modal is up. (saikai injects its OWN handoff
    # prompt now — not the personal /handoff skill — so match the constant.)
    assert ("paste", saikai._B2_HANDOFF_PROMPT) in facts.get("events_at_modal", []), facts
    assert facts.get("clear_before_confirm") is False, \
        f"/clear must NOT be injected before the human confirms: {facts}"
    # after Enter the modal closes and the machine resumes
    assert facts.get("modal_after_enter") != "ConfirmRefreshScreen", facts
    ev = facts.get("events_final", [])
    assert ("paste", "/clear") in ev, f"/clear must be injected after confirm: {facts}"
    # /clear comes AFTER the handoff prompt in the recorded order
    assert ev.index(("paste", "/clear")) > ev.index(("paste", saikai._B2_HANDOFF_PROMPT)), facts
    # the reseed injects the extracted prompt (references the parent handoff).
    # events are mixed arity — ("paste", text) vs ("submit",) — so guard the unpack.
    assert any(e[0] == "paste" and "Resume saikai Task 11" in e[1]
               for e in ev if len(e) >= 2), f"reseed prompt not injected: {facts}"
    # and lineage records child -> parent with the parent transcript path
    lin = facts.get("lineage")
    assert lin and lin.get("parent") == parent_sid, f"lineage child->parent not recorded: {facts}"
    assert lin.get("parent_jsonl") == str(parent_jsonl), facts
    # After /clear the running pane IS the child — its gauge must re-point at the
    # child transcript (not stay on the frozen parent), so it reads lean in place.
    assert facts.get("live_jsonl") == str(pdir / f"{child_sid}.jsonl"), \
        f"pane gauge not re-pointed at the child transcript: {facts}"
    # --- the pane's IDENTITY must follow the session parent->child (the 4 harms):
    # harm #1 (restore resumes the WRONG session): _opened_sids — which
    # _save_open_panes persists for Shift+F4 — must now hold the child, not parent.
    assert facts.get("opened_has_child") is True and facts.get("opened_has_parent") is False, \
        f"restore set must target the child, not the frozen parent: {facts}"
    # harm #3/#4 (re-opening the child spawns a DUPLICATE / list mislabels the
    # frozen parent as live): the live manager must find the pane under the child.
    assert facts.get("live_has_child") is True and facts.get("live_has_parent") is False, \
        f"live pane must be keyed by the child sid, not the parent: {facts}"
    # the running pane's own sid is the child now (gauge + status resolve via it).
    assert facts.get("term_sid") == child_sid, f"term.sid not re-pointed to the child: {facts}"
    # an interim child stub bridges the list/status until the next rescan; the
    # parent stays a real historical session (both resolvable).
    assert facts.get("child_in_sid_index") is True, f"child not injected into _sid_index: {facts}"
    assert facts.get("parent_in_sid_index") is True, f"parent dropped from _sid_index: {facts}"
    # harm #2 (Shift+F6 from the reseeded pane fails): lineage maps child->parent,
    # so action_open_parent from the child row resolves the parent.
    assert facts.get("child_lineage_parent") == parent_sid, \
        f"Shift+F6 from the child would not find the parent: {facts}"
    # Esc (cancel) path: modal shown, Esc dismissed it, machine torn down, and
    # crucially NO /clear injected — cancelling leaves the session untouched.
    assert facts.get("esc_modal_shown") == "ConfirmRefreshScreen", \
        f"second checkpoint must reach the confirm modal: {facts}"
    assert facts.get("esc_modal_after") != "ConfirmRefreshScreen", \
        f"Esc must dismiss the confirm modal: {facts}"
    assert facts.get("esc_b2_torn_down") is True, \
        f"Esc must tear down the checkpoint machine: {facts}"
    assert ("paste", "/clear") not in facts.get("esc_events", []), \
        f"Esc must NOT inject /clear — the session stays untouched: {facts}"


def test_pilot_refresh_preserves_scroll_position():
    """A background list rebuild (the ~1.5s poll, a filter keystroke, a fav/hide
    toggle) must NOT yank the viewport back to the cursor row — the user's
    mouse-scroll position is kept across _refresh_table. Regression: _do_refresh_
    table cleared + move_cursor'd (default scroll=True) without preserving the
    scroll offset, so every state update snapped the list back to the cursor."""
    try:
        from textual.app import App  # noqa: F401
    except Exception:
        print("SKIP test_pilot_refresh_preserves_scroll_position (textual unavailable)")
        return
    import asyncio
    from textual.app import App
    for _ in range(40):
        _write_demo_session()
    facts: dict = {}

    def fake_run(self, *a, **kw):
        async def go():
            async with self.run_test(size=(120, 18)) as pilot:
                await pilot.pause(0.4)
                table = self.query_one("#table")
                # Scroll DOWN, away from the cursor (which sits near the top after
                # mount) — mimics the user mouse-scrolling through the list.
                table.scroll_to(y=12, animate=False)
                await pilot.pause(0.2)
                facts["before"] = table.scroll_offset.y
                # A background list rebuild (exactly what the live-status poll
                # triggers on a session-state change).
                self._refresh_table()
                await pilot.pause(0.2)
                facts["after"] = table.scroll_offset.y
        asyncio.run(go())

    orig, App.run = App.run, fake_run
    orig_argv = sys.argv
    try:
        sys.argv = ["saikai", "--all"]
        saikai.main()
    finally:
        App.run = orig
        sys.argv = orig_argv
    assert facts.get("before", 0) > 0, f"test setup: table did not scroll: {facts}"
    assert facts["after"] == facts["before"], \
        f"refresh yanked the scroll back to the cursor (before={facts['before']} " \
        f"after={facts['after']})"


def test_pilot_checkpoint_marker_on_row():
    """While a b2 checkpoint is in flight, the target session's list row is
    prefixed with ↻ so you can see WHICH session is being checkpointed; the marker
    clears when the flow ends (driven by _do_refresh_table off self._b2)."""
    try:
        from textual.app import App  # noqa: F401
    except Exception:
        print("SKIP test_pilot_checkpoint_marker_on_row (textual unavailable)")
        return
    import asyncio
    from textual.app import App
    sid = _write_demo_session()
    facts: dict = {}

    def _title(table, key):
        try:
            return str(table.get_row(key)[-1])
        except Exception as e:  # noqa: BLE001
            return f"<err {e!r}>"

    def fake_run(self, *a, **kw):
        async def go():
            async with self.run_test(size=(120, 24)) as pilot:
                await pilot.pause(0.4)
                table = self.query_one("#table")
                facts["before"] = _title(table, sid)
                self._b2 = {"sid": sid}        # simulate a checkpoint in flight
                self._refresh_table()
                await pilot.pause(0.05)
                facts["during"] = _title(table, sid)
                self._b2 = None                # flow ended
                self._refresh_table()
                await pilot.pause(0.05)
                facts["after"] = _title(table, sid)
        asyncio.run(go())

    orig, App.run = App.run, fake_run
    orig_argv = sys.argv
    try:
        sys.argv = ["saikai", "--all"]
        saikai.main()
    finally:
        App.run = orig
        sys.argv = orig_argv
    assert "↻" not in facts.get("before", ""), f"no marker before a checkpoint: {facts}"
    assert "↻" in facts.get("during", ""), f"↻ marker must show during a checkpoint: {facts}"
    assert "↻" not in facts.get("after", ""), f"marker must clear after: {facts}"


def test_pilot_toast_rows_self_heal():
    """WT hover artifact mitigation (#toast-heal): while a toast is visible, a
    0.4s tick re-emits it, so a row punched out by the Windows-driver↔WT layer
    repaints itself. Assert the tick actually re-EMITS partial updates carrying
    the toast rows (headless probes proved the emitted content is correct)."""
    try:
        from textual.app import App  # noqa: F401
    except Exception:
        print("SKIP test_pilot_toast_rows_self_heal (textual unavailable)")
        return
    import asyncio
    from textual.app import App
    from textual._compositor import Compositor

    _write_demo_session()
    facts: dict = {}

    def fake_run(self, *a, **kw):
        async def go():
            async with self.run_test(size=(110, 24), notifications=True) as pilot:
                await pilot.pause(0.4)
                self.notify("HEALMARK toast row", timeout=30)
                await pilot.pause(0.4)
                emitted = []
                _orig = Compositor.render_partial_update

                def _spy(comp):
                    r = _orig(comp)
                    if r is not None:
                        emitted.append(r)
                    return r

                Compositor.render_partial_update = _spy
                try:
                    await pilot.pause(1.0)    # ≥2 heal ticks, no other activity
                finally:
                    Compositor.render_partial_update = _orig
                facts["updates"] = len(emitted)
        asyncio.run(go())

    orig, App.run = App.run, fake_run
    orig_argv = sys.argv
    try:
        sys.argv = ["saikai", "--all"]
        saikai.main()
    finally:
        App.run = orig
        sys.argv = orig_argv
    assert facts.get("updates", 0) >= 2, \
        f"the heal tick must re-emit visible toasts (~0.4s cadence): {facts}"


def test_pilot_toast_user_content_renders_verbatim():
    """Toast messages carry USER content — session titles ('needs input:
    {title}'), exception reprs, paths. Textual renders notifications as content
    markup by default, so '[wip] fix' displayed as ' fix' (tag swallowed) and a
    stray '[/x]' raised MarkupError inside Toast.render — the toast never showed
    at all. saikai's notify wrapper must force markup=False, and the F11 recall
    screen must render the logged message verbatim. (#audit-toast-markup)"""
    try:
        from textual.app import App  # noqa: F401
    except Exception:
        print("SKIP test_pilot_toast_user_content_renders_verbatim (textual unavailable)")
        return
    import asyncio
    from textual.app import App
    _write_demo_session()
    facts: dict = {}

    def fake_run(self, *a, **kw):
        async def go():
            async with self.run_test(size=(120, 24)) as pilot:
                await pilot.pause(0.4)
                import textual.app as _ta
                captured = []
                _orig = _ta.App.notify

                def _spy(app, message, **kwargs):
                    captured.append((message, dict(kwargs)))
                    return _orig(app, message, **kwargs)

                _ta.App.notify = _spy
                try:
                    # the shapes that actually broke: bracketed title + stray
                    # close tag (MarkupError in Toast.render before the fix)
                    self.notify("needs input: [wip] fix [b]auth[/b]", timeout=1)
                    self.notify("rename failed: bad [/x] tag", severity="error",
                                timeout=1)
                finally:
                    _ta.App.notify = _orig
                facts["markup_flags"] = [k.get("markup") for _, k in captured]
                facts["messages"] = [m for m, _ in captured]
                # F11 recall: the logged text must render VERBATIM (no swallowed
                # tags, no MarkupError from the markup=True RichLog)
                self.action_notifications()
                await pilot.pause(0.2)
                try:
                    from textual.widgets import RichLog
                    log = self.screen.query_one("#notif-log", RichLog)
                    facts["recall"] = "\n".join(
                        strip.text for strip in log.lines)
                except Exception as e:  # noqa: BLE001
                    facts["recall_err"] = repr(e)
                await pilot.press("escape")
        asyncio.run(go())

    orig, App.run = App.run, fake_run
    orig_argv = sys.argv
    try:
        sys.argv = ["saikai", "--all"]
        saikai.main()
    finally:
        App.run = orig
        sys.argv = orig_argv
    assert facts.get("markup_flags") == [False, False], \
        f"notify must force markup=False for user content: {facts}"
    rec = facts.get("recall", "")
    assert "[wip] fix [b]auth[/b]" in rec, \
        f"F11 recall must render bracketed content verbatim: {facts}"
    assert "bad [/x] tag" in rec, \
        f"F11 recall must survive stray close tags: {facts}"


def test_tabpane_title_uses_markup_safe_content_not_rich_text():
    """A live pane's TabPane title is USER content (session AI title / first
    message) that can contain '[' — e.g. '[wip] ASCoM 予算'. Textual's TabPane
    title/label type is ContentType = str | textual.content.Content; a rich Text
    is NOT accepted — render_str→_strip_control_codes calls str.translate, which
    rich Text lacks, raising AttributeError and crashing _spawn_live_pane on
    EVERY session open/restore. f0c2c57 wrapped these in rich Text for markup-
    safety (the right goal, wrong type); a plain str is unsafe too (the '[' is
    parsed as markup and swallowed). The fix is textual Content: markup-safe AND
    accepted. The sibling toast test covered notify() but not this TabPane path,
    so the crash shipped. (#audit-toast-markup)"""
    # (1) Source guard — the exact shapes f0c2c57 introduced must stay gone. This
    # is the cheap, deps-free check that WOULD have caught the regression (the
    # behavioral half below needs a real PTY to reach _spawn_live_pane).
    src = Path(saikai.__file__).read_text(encoding="utf-8")
    assert "TabPane(Text(" not in src, \
        "TabPane title must be str|Content, never rich Text (crashes render_str)"
    assert "pane.label = Text(" not in src, \
        "TabPane label must be str|Content, never rich Text"

    # (2) Behavioral — the real production expression, Content(tab_label(...)),
    # must construct without raising and keep brackets literal (not markup).
    try:
        from textual.widgets import TabPane
        from textual.content import Content
        from rich.text import Text
    except Exception:
        print("SKIP test_tabpane_title_uses_markup_safe_content_not_rich_text "
              "(textual unavailable)")
        return
    import saikai_terminal
    hostile = "[wip] plan [x]"          # short: stays under tab_label's 18-char cut
    label = saikai_terminal.tab_label(hostile, "idle")
    assert "[wip]" in label and "[x]" in label, \
        f"tab_label must keep brackets literal (no markup escaping): {label!r}"
    pane = TabPane(Content(label))       # regressed line raised AttributeError here
    plain = pane._title.plain
    assert "[wip]" in plain and "[x]" in plain, \
        f"Content title must render brackets verbatim, not as markup: {plain!r}"
    # Anti-regression witness: confirm the wrapped-in-Text shape really does crash
    # in this textual, so the source guard is guarding a live hazard — but don't
    # hard-fail if a future textual starts accepting Text (Content stays correct).
    try:
        TabPane(Text(label))
    except AttributeError:
        pass
    else:
        print("NOTE: TabPane(Text(...)) no longer crashes in this textual; "
              "source guard still valid — Content is the supported type.")


def test_statusbar_markup_escapes_user_search_and_folder():
    """_update_subtitle echoes two pieces of USER content into a markup=True
    statusbar Static: the search query ('search: {q!r}') and the scope (the repo
    FOLDER NAME). Both were interpolated raw — a '[' folder, or a '[/x]' typed
    into the search box, made Static.update -> Content.from_markup raise
    MarkupError (crashing _update_subtitle) or silently swallowed the text
    ('[bold]' -> gone). User content must be escaped with textual.markup.escape:
    a Static renders Textual CONTENT markup, not rich markup, so the library's
    own escaper is the right tool (rich.markup.escape happens to work only
    because both honor '\\['). Commit 08ef9b7 already proved bracketed folder
    names are a live hazard here. (#audit-toast-markup)"""
    # (1) Source guard — the raw-interpolation shapes must stay gone and the
    # escaper must be wired in. This is the cheap catch for a re-regression.
    src = Path(saikai.__file__).read_text(encoding="utf-8")
    assert "search: {_qd!r}" not in src, \
        "statusbar search echo must escape the query (textual.markup.escape)"
    assert 'scope_str = f"{sep}{scope}"' not in src, \
        "statusbar scope (repo.name) must be markup-escaped"
    assert "_esc_markup(repr(_qd))" in src and "_esc_markup(scope)" in src, \
        "statusbar must route user content through _esc_markup"

    # (2) Behavioral — the exact hazard shapes must survive the SAME sink the
    # statusbar uses (a markup=True Static.update), and the unescaped form must
    # still crash (so the guard protects a live hazard, not a style nit).
    try:
        from textual.widgets import Static
        from textual.markup import MarkupError
    except Exception:
        print("SKIP test_statusbar_markup_escapes_user_search_and_folder "
              "(textual unavailable)")
        return
    esc = saikai._esc_markup
    for hostile in ["[bold]x", "a [/x] b", "'[/]'", "[archive]", "proj [v2] dir"]:
        s = Static()
        # mirrors _update_subtitle: developer markup + escaped user content.
        s.update(f"[yellow]search: {esc(repr(hostile))}[/yellow]  {esc(hostile)}")
        vis = getattr(s, "_Static__visual", None)
        plain = getattr(vis, "plain", "") or ""
        assert hostile in plain, \
            f"escaped user content must render literally: {hostile!r} -> {plain!r}"
    crashed = False
    try:
        Static().update("stray [/x] close")   # the pre-fix statusbar shape
    except MarkupError:
        crashed = True
    assert crashed, \
        "an unescaped stray close tag must MarkupError — the hazard is real"


def test_tab_glyph_updates_via_tab_label_not_pane_label():
    """A live pane's status glyph (idle/busy/dead, and a rename) is refreshed by
    relabelling its tab. TabPane has NO `label` property, so `pane.label = …` only
    set a dead instance attribute — the DISPLAYED glyph never changed. The update
    must go through the Tab widget: tabs.get_tab(pane).label = Content(…), whose
    setter calls update(). Passing Content (not str) keeps a '[' in a session
    title literal, since Tab.label's Content.from_text defaults to markup=True.
    (#tab-glyph-update)"""
    # (1) Source guard — the no-op form must be gone, the Tab.label form wired in
    # at all three glyph-update sites (status poll, dead pane, rename).
    src = Path(saikai.__file__).read_text(encoding="utf-8")
    assert "pane.label = Content(" not in src, \
        "TabPane.label is a no-op — relabel via tabs.get_tab(pane).label"
    assert src.count("get_tab(pane).label = Content(") >= 3, \
        "all three glyph-update sites must relabel the Tab, not the TabPane"

    # (2) Behavioral — prove the contract saikai now relies on, on a minimal app:
    # get_tab().label updates the displayed tab (pane.label does NOT), bracket-safe.
    try:
        from textual.app import App
        from textual.widgets import TabbedContent, TabPane, Static
        from textual.content import Content
    except Exception:
        print("SKIP test_tab_glyph_updates_via_tab_label_not_pane_label "
              "(textual unavailable)")
        return
    import asyncio
    facts: dict = {}

    class _Mini(App):
        def compose(self):
            with TabbedContent(id="tc"):
                with TabPane(Content("~ old"), id="p1"):
                    yield Static("body")

    async def go():
        app = _Mini()
        async with app.run_test() as _pilot:
            tc = app.query_one("#tc", TabbedContent)
            pane = tc.get_pane("p1")
            pane.label = Content("X ignored")                 # the old no-op path
            facts["after_pane_label"] = tc.get_tab(pane).label.plain
            tc.get_tab(pane).label = Content("x [wip] new")   # the fix path
            facts["after_tab_label"] = tc.get_tab(pane).label.plain
    asyncio.run(go())

    assert facts.get("after_pane_label") == "~ old", \
        f"pane.label must NOT change the displayed tab (it is a no-op): {facts}"
    assert facts.get("after_tab_label") == "x [wip] new", \
        f"get_tab().label must update the tab, brackets literal: {facts}"


def test_pilot_filter_engaged_window_survives_focus_move():
    """Filtering must not switch the foreground live pane out from under the user.
    The post-filter RowHighlighted is queued (the rebuild is call_after_refresh'd),
    so by the time it runs focus may have briefly left the search box — a bare
    `self.focused is #search` check then misses it and the foreground gets
    switched. _filter_is_engaged() also honours a short window set on each filter
    keystroke, so it stays True across that queued highlight."""
    try:
        from textual.app import App  # noqa: F401
    except Exception:
        print("SKIP test_pilot_filter_engaged_window_survives_focus_move (textual unavailable)")
        return
    import asyncio, time as _time
    from textual.app import App
    _write_demo_session()
    facts: dict = {}

    def fake_run(self, *a, **kw):
        async def go():
            async with self.run_test(size=(120, 24)) as pilot:
                await pilot.pause(0.3)
                # Focus the TABLE (not the search box) with no recent filter.
                self.query_one("#table").focus()
                self._filter_active_until = 0.0
                await pilot.pause(0.05)
                facts["idle"] = self._filter_is_engaged()
                # A filter keystroke just landed: engaged stays True even though
                # focus is on the table (mimics the queued post-filter highlight).
                self._filter_active_until = _time.monotonic() + 5
                facts["windowed"] = self._filter_is_engaged()
        asyncio.run(go())

    orig, App.run = App.run, fake_run
    orig_argv = sys.argv
    try:
        sys.argv = ["saikai", "--all"]
        saikai.main()
    finally:
        App.run = orig
        sys.argv = orig_argv
    assert facts.get("idle") is False, \
        f"table focused + no recent filter must NOT read as engaged: {facts}"
    assert facts.get("windowed") is True, \
        f"a filter keystroke in the last beat must read as engaged: {facts}"


def test_kill_agent_yields_key_to_a_focused_live_pane():
    """Shift+K (kill-agent) is a priority=True binding, so it preempts a focused
    live pane's on_key — verified separately: priority bindings fire before the
    focused widget's on_key even when that on_key calls event.stop(). Without a
    guard, pressing Shift+K while typing into claude ran action_kill_agent (a
    notify) instead of entering a capital 'K'. action_kill_agent must raise
    SkipAction when a live terminal is focused — the SAME guard action_resume /
    action_toggle_preview / action_help already use — so Textual forwards the key
    to the AgentTerminal, whose on_key writes it to the PTY. (#agent-kill)"""
    src = Path(saikai.__file__).read_text(encoding="utf-8")
    i = src.find("def action_kill_agent(self)")
    assert i != -1, "action_kill_agent not found"
    j = src.find("\n        def ", i + 1)   # next method at the class-body indent
    body = src[i:j] if j != -1 else src[i:i + 2000]
    assert ("self._keys_go_to_pane()" in body
            and "SkipAction" in body), \
        "action_kill_agent must yield Shift+K to a focused live pane (raise SkipAction)"


def test_pilot_cycle_tab_skips_dead_pane():
    """F2/F3 (cycle_tab) must NEVER focus a DEAD ✓ pane. A corpse has no PTY, so
    keys would vanish into it (and a stray printable would bubble to the list as
    type-to-search) — same guard as action_toggle_list. Cycling onto a dead pane
    lands focus on the session list instead. The test starts with the SEARCH box
    focused so "focus moved to the list" is a real signal, then cycles onto a
    mounted dead pane and asserts the dead terminal's focus() was never called."""
    try:
        from textual.app import App  # noqa: F401
    except Exception:
        print("SKIP test_pilot_cycle_tab_skips_dead_pane (textual unavailable)"); return
    import asyncio
    from textual.app import App
    from textual.widgets import TabbedContent, TabPane, Static, DataTable, Input

    facts: dict = {}

    def fake_run(self, *a, **kw):
        async def go():
            async with self.run_test(size=(110, 30)) as pilot:
                await pilot.pause(0.3)
                dead_sid = "dead-cycle-sid"
                pane_id = "tab-live-" + dead_sid
                tabs = self.query_one("#right", TabbedContent)
                await tabs.add_pane(TabPane("✓ dead", Static("corpse"),
                                            id=pane_id))
                await pilot.pause(0.05)

                class _DeadTerm:
                    is_dead = True
                    def focus(self):
                        facts["dead_focus_called"] = True   # the bug would call this

                dead_term = _DeadTerm()

                class _FakeLive:
                    count = 1
                    max_live = 64
                    def statuses(self):
                        return {dead_sid: "dead"}
                    def pane_id(self, sid):
                        return pane_id
                    def get(self, sid):
                        return dead_term if sid == dead_sid else None
                    def all_terms(self):
                        return []
                    def has(self, sid):
                        return sid == dead_sid
                self._live = _FakeLive()

                # Start OFF the list (search focused) so "focus went to the list"
                # is a genuine signal, then cycle onto the dead pane's tab.
                tabs.active = "tab-preview"
                self.query_one("#search", Input).focus()
                await pilot.pause(0.05)
                await pilot.app.run_action("next_tab")
                await pilot.pause(0.1)
                facts["active"] = tabs.active
                facts["dead_focus_called"] = facts.get("dead_focus_called", False)
                facts["focus_is_table"] = (
                    self.focused is self.query_one("#table", DataTable))
        asyncio.run(go())

    orig, App.run = App.run, fake_run
    orig_argv = sys.argv
    try:
        sys.argv = ["saikai", "--all"]
        saikai.main()
    finally:
        App.run = orig
        sys.argv = orig_argv
    assert facts.get("active") == "tab-live-dead-cycle-sid", \
        f"cycle should land on the dead pane's tab: {facts}"
    assert facts.get("dead_focus_called") is False, \
        f"a DEAD pane must never be focused by cycle_tab: {facts}"
    assert facts.get("focus_is_table") is True, \
        f"cycling onto a dead pane must focus the list: {facts}"


def test_pilot_double_space_does_not_leave_leader_armed():
    """Double-Space (the mark gesture) must dispatch and NOT leave the leader armed:
    event.stop() doesn't block the App's own space→arm_leader binding, which would
    re-arm with _leader_pending already reset and hijack the next key. (#H10)"""
    try:
        from textual.app import App  # noqa: F401
    except Exception:
        print("SKIP test_pilot_double_space_does_not_leave_leader_armed (textual unavailable)")
        return

    import asyncio
    from textual.app import App
    from textual.widgets import Input

    _write_demo_session()
    facts: dict = {}

    def fake_run(self, *a, **kw):
        async def go():
            async with self.run_test(size=(110, 30)) as pilot:
                await pilot.pause(0.4)
                self.query_one("#table").focus()
                await pilot.pause(0.1)
                await pilot.press("space")          # arm the leader…
                await pilot.pause(0.1)
                await pilot.press("space")          # …double-Space = mark; must NOT re-arm
                await pilot.pause(0.2)
                facts["pending_after_double"] = getattr(self, "_leader_pending", None)
                await pilot.press("a")              # next key must reach search, not be hijacked
                await pilot.pause(0.2)
                try:
                    facts["search"] = self.query_one("#search", Input).value
                except Exception as e:              # noqa: BLE001
                    facts["error"] = repr(e)
        asyncio.run(go())

    orig_run, App.run = App.run, fake_run
    orig_argv = sys.argv
    try:
        sys.argv = ["saikai", "--all"]
        saikai.main()
    finally:
        App.run = orig_run
        sys.argv = orig_argv

    assert facts.get("pending_after_double") is False, \
        f"leader left armed after double-Space: {facts}"
    assert facts.get("search") == "a", \
        f"key after double-Space was hijacked instead of reaching search: {facts}"


def _write_session_ex(last_role: str, ts: str, title: str):
    """A demo session whose LAST transcript record is a user prompt (reply-due →
    'needs you') or an assistant turn (answered → not). Returns (sid, path)."""
    sid = str(uuid.uuid4())
    pdir = _FAKE_HOME / ".claude" / "projects" / "-home-alex-code-demo"
    pdir.mkdir(parents=True, exist_ok=True)
    recs = [
        {"type": "ai-title", "aiTitle": title, "timestamp": ts,
         "cwd": "/home/alex/code/demo"},
        {"type": "user", "timestamp": ts, "cwd": "/home/alex/code/demo",
         "message": {"content": "a demo prompt long enough to count as real"}},
    ]
    if last_role == "assistant":
        recs.append({"type": "assistant", "timestamp": ts,
                     "cwd": "/home/alex/code/demo",
                     "message": {"role": "assistant",
                                 "content": [{"type": "text", "text": "answered."}]}})
    p = pdir / f"{sid}.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in recs) + "\n", encoding="utf-8")
    return sid, p


def test_pilot_front_door_homes_on_needs_you():
    """The FIRST paint lands the cursor on the first session that NEEDS YOU
    (reply-due), not the newest row — even under flat (non-state) grouping where
    the newest, already-answered session sorts to the top. (#front-door)"""
    try:
        from textual.app import App  # noqa: F401
    except Exception:
        print("SKIP test_pilot_front_door_homes_on_needs_you (textual unavailable)")
        return

    import asyncio
    from textual.app import App

    # Flat list so recency alone orders rows: the front-door home is then the ONLY
    # thing that could move the cursor off the newest (answered) top row.
    saikai._write_text_atomic(saikai.GROUP_BY_FILE, "none")
    saikai._invalidate_pref(saikai.GROUP_BY_FILE)
    # Newer session is already ANSWERED (not attention); older one is reply-due.
    sid_new, p_new = _write_session_ex("assistant", "2026-06-20T00:00:00.000Z", "Answered newest")
    sid_att, p_att = _write_session_ex("user", "2026-06-10T00:00:00.000Z", "Waiting on you")
    os.utime(p_new, (1_750_000_000, 1_750_000_000))   # newer mtime → sorts first
    os.utime(p_att, (1_749_000_000, 1_749_000_000))   # older mtime → below it
    facts: dict = {}

    def fake_run(self, *a, **kw):
        async def go():
            async with self.run_test(size=(110, 30)) as pilot:
                await pilot.pause(0.5)
                facts["cursor_sid"] = self._cursor_sid()
                facts["homed"] = getattr(self, "_did_attention_home", None)
        asyncio.run(go())

    orig_run, App.run = App.run, fake_run
    orig_argv = sys.argv
    try:
        sys.argv = ["saikai", "--all"]
        saikai.main()
    finally:
        App.run = orig_run
        sys.argv = orig_argv
        saikai._write_text_atomic(saikai.GROUP_BY_FILE, "state")   # restore default
        saikai._invalidate_pref(saikai.GROUP_BY_FILE)

    assert facts.get("homed") is True, f"front-door home did not run: {facts}"
    assert facts.get("cursor_sid") == sid_att, \
        f"cursor should home on the reply-due session, not the newest: {facts}"

def test_pilot_cursor_restore_with_scroll_false_does_not_scroll():
    """move_cursor(scroll=False) must actually not scroll. DataTable's own
    watch_cursor_coordinate calls _scroll_cursor_into_view() on any coordinate
    change, so the flag never reached the scroll: a background rebuild restoring the
    cursor still yanked the viewport toward the cursor row, and mid-wheel that reads
    as the list jumping back and forth. MEASURED in a Textual harness: this path
    alone produced 3 A->B->A jumps during a scroll. (#scroll-fight)"""
    try:
        from textual.app import App  # noqa: F401
    except Exception:
        print("SKIP test_pilot_cursor_restore_with_scroll_false_does_not_scroll")
        return
    import asyncio
    from textual.app import App
    for _ in range(40):
        _write_demo_session()
    facts: dict = {}

    def fake_run(self, *a, **kw):
        async def go():
            async with self.run_test(size=(120, 18)) as pilot:
                await pilot.pause(0.4)
                table = self.query_one("#table")
                table.scroll_to(y=12, animate=False)
                await pilot.pause(0.2)
                facts["before"] = table.scroll_offset.y
                table.move_cursor(row=0, scroll=False)      # the rebuild's restore
                await pilot.pause(0.2)
                facts["after"] = table.scroll_offset.y
        asyncio.run(go())

    orig, App.run = App.run, fake_run
    orig_argv = sys.argv
    try:
        sys.argv = ["saikai", "--all"]
        saikai.main()
    finally:
        App.run = orig
        sys.argv = orig_argv
    assert facts.get("before", 0) > 0, f"test setup: table did not scroll: {facts}"
    assert facts["after"] == facts["before"], \
        ("move_cursor(scroll=False) scrolled anyway "
         f"(before={facts['before']} after={facts['after']})")


def test_pilot_rebuild_is_deferred_while_the_list_is_scrolling():
    """A rebuild landing mid-scroll is the oscillation: clear() zeroes scroll_y, so
    the rebuild restores a position captured before the user's notch. During a storm
    the live poll rebuilds about once a frame, so nearly every notch hit one —
    MEASURED: 14 jumps in a 1.6 s scroll and 31 of 32 notches of travel lost. Skip
    the rebuild while the table is scrolling and pick it up after. (#scroll-fight)"""
    try:
        from textual.app import App  # noqa: F401
    except Exception:
        print("SKIP test_pilot_rebuild_is_deferred_while_the_list_is_scrolling")
        return
    import asyncio
    from textual.app import App
    for _ in range(40):
        _write_demo_session()
    facts: dict = {}

    def fake_run(self, *a, **kw):
        async def go():
            async with self.run_test(size=(120, 18)) as pilot:
                await pilot.pause(0.4)
                table = self.query_one("#table")
                table.scroll_to(y=12, animate=False)
                await pilot.pause(0.2)
                facts["before"] = table.scroll_offset.y
                # Pin is_scrolling True for the duration, as a real wheel does.
                cls = type(table)
                saved = cls.is_scrolling
                cls.is_scrolling = property(lambda self: True)
                try:
                    self._refresh_table()
                    await pilot.pause(0.2)
                    facts["during"] = table.scroll_offset.y
                finally:
                    cls.is_scrolling = saved
        asyncio.run(go())

    orig, App.run = App.run, fake_run
    orig_argv = sys.argv
    try:
        sys.argv = ["saikai", "--all"]
        saikai.main()
    finally:
        App.run = orig
        sys.argv = orig_argv
    assert facts.get("before", 0) > 0, f"test setup: table did not scroll: {facts}"
    assert facts["during"] == facts["before"], \
        ("a rebuild ran mid-scroll and moved the viewport "
         f"(before={facts['before']} during={facts['during']})")

def test_pilot_wheel_scroll_is_monotonic_under_constant_rebuilds():
    """The whole scroll-fight, measured end to end on the real app: turn the wheel
    while the list rebuilds on a timer (what the live-status poll does during a storm)
    and the viewport must only ever move DOWN.

    Before the fixes this measured 1-3 backwards jumps per run with most of the travel
    lost; the backwards jump that survived the first three fixes came from clear()
    resetting cursor_coordinate, which fires watch_cursor_coordinate outside
    move_cursor and queues a deferred _scroll_cursor_into_view (traced: 28 -> 1).
    (#scroll-fight)"""
    try:
        from textual.app import App  # noqa: F401
    except Exception:
        print("SKIP test_pilot_wheel_scroll_is_monotonic_under_constant_rebuilds")
        return
    import asyncio
    from textual.app import App
    from textual import events as _ev
    for _ in range(60):
        _write_demo_session()
    facts: dict = {}
    NOTCHES = 16

    def fake_run(self, *a, **kw):
        async def go():
            async with self.run_test(size=(120, 18)) as pilot:
                await pilot.pause(0.5)
                table = self.query_one("#table")
                timer = self.set_interval(0.05, self._refresh_table)
                offs = [table.scroll_offset.y]
                try:
                    for _ in range(NOTCHES):
                        await pilot.hover("#table")
                        table.post_message(_ev.MouseScrollDown(
                            table, 5, 5, 0, 1, 0, False, False, False))
                        await pilot.pause(0.08)
                        offs.append(table.scroll_offset.y)
                finally:
                    timer.stop()
                facts["offsets"] = offs
                facts["max"] = table.max_scroll_y
        asyncio.run(go())

    orig, App.run = App.run, fake_run
    orig_argv = sys.argv
    try:
        sys.argv = ["saikai", "--all"]
        saikai.main()
    finally:
        App.run = orig
        sys.argv = orig_argv
    offs = facts.get("offsets") or []
    assert len(offs) > NOTCHES, f"test setup: no samples: {facts}"
    assert facts.get("max", 0) > 0, f"test setup: table cannot scroll: {facts}"
    back = [(a, b) for a, b in zip(offs, offs[1:]) if b < a]
    assert not back, f"wheel scroll went backwards {back} — offsets={offs}"
    assert offs[-1] > offs[0], f"wheel scroll made no progress: {offs}"

def test_pilot_cursor_scroll_suppression_nests_across_overlapping_rebuilds():
    """The cursor-scroll suppression must COUNT, because its windows overlap.

    Each window closes on a queued callback (only FIFO puts it behind the
    watcher's queued _scroll_cursor_into_view), so a rebuild that starts while the
    previous one's callbacks are still draining opens a second window. With a
    boolean, the FIRST window's end re-armed the second one's pending scroll:

      rebuild1: clear -> queue A1 | move_cursor -> flag=True, queue B1, end1
      rebuild2: clear -> queue A2 | move_cursor -> flag=True, queue B2, end2
      drain:    A1 ok  B1 ok  end1 -> flag=False  A2 SCROLLED to the cursor row

    ATTRIBUTED by recording every viewport move with its caller on the real app:
    the surviving jump came from _scroll_cursor_into_view running OUTSIDE
    _do_refresh_table with force=True (8 -> 1, the cursor's row). It needs the
    overlap, so it only reproduced under load — the monotonic-wheel harness caught
    it in 2 of 4 runs on a busy machine and 0 of 4 when idle. This asserts the
    nesting directly instead of waiting for that race. (#suppression-depth)"""
    try:
        from textual.app import App  # noqa: F401
    except Exception:
        print("SKIP test_pilot_cursor_scroll_suppression_nests_across_overlapping_rebuilds")
        return
    import asyncio
    from textual.app import App
    for _ in range(40):
        _write_demo_session()
    facts: dict = {}

    def fake_run(self, *a, **kw):
        async def go():
            async with self.run_test(size=(120, 18)) as pilot:
                await pilot.pause(0.4)
                table = self.query_one("#table")
                facts["ran"] = True            # so a skip cannot stand in for "never ran"
                table.move_cursor(row=1, scroll=True)     # cursor near the top
                # SETTLE the cursor move before positioning the viewport. move_cursor
                # QUEUES a _scroll_cursor_into_view, and MEASURED here: it lands ~0.3s
                # later and pulls the offset back to the cursor row (12 -> 1). On this
                # machine it happened to land before the scroll_to below and the test
                # passed; on the ubuntu runner it landed after, so the setup silently
                # produced start=1 with 267 rows of room available and the assertion
                # blamed the table. Wait for it, THEN scroll away. (#test-setup-race)
                await pilot.pause(0.3)
                # The assertion below is "a real navigation scrolls BACK to the
                # cursor", which only means anything if the viewport starts far
                # enough away. CI (ubuntu py3.12) once had a table that could only
                # scroll ONE row, so scroll_to(12) clamped to 1, the cursor row was
                # already visible, and "did not scroll back" was indistinguishable
                # from "could not scroll at all". Take the room the table actually
                # has, and say so rather than assert on a table that cannot move.
                facts["room"] = int(table.max_scroll_y or 0)
                if facts["room"] < 5:
                    return
                table.scroll_to(y=min(12, facts["room"]), animate=False)
                await pilot.pause(0.2)
                facts["start"] = table.scroll_offset.y
                table._begin_cursor_scroll_suppression()  # rebuild 1's window
                table._begin_cursor_scroll_suppression()  # rebuild 2 starts, 1 draining
                table._end_cursor_scroll_suppression()    # rebuild 1's queued end
                table._scroll_cursor_into_view()          # rebuild 2's queued scroll
                facts["still_open"] = table.scroll_offset.y
                table._end_cursor_scroll_suppression()    # rebuild 2's queued end
                table._scroll_cursor_into_view()          # a real navigation
                await pilot.pause(0.2)
                facts["closed"] = table.scroll_offset.y
                # An unpaired end must not push the depth negative: that would
                # suppress cursor scrolling for the rest of the session.
                table._end_cursor_scroll_suppression()
                table._end_cursor_scroll_suppression()
                facts["depth"] = table._cursor_scroll_depth
        asyncio.run(go())

    orig, App.run = App.run, fake_run
    orig_argv = sys.argv
    try:
        sys.argv = ["saikai", "--all"]
        saikai.main()
    finally:
        App.run = orig
        sys.argv = orig_argv
    # "The table cannot scroll" and "the pilot never ran" both left room == 0, and the
    # skip swallowed each of them: a Textual upgrade, a renamed #table or a raise in
    # on_mount would have printed SKIP and gone green, shipping the nesting invariant
    # completely unexercised. Assert the body ran FIRST; only then may a small table
    # excuse the assertions. (#skip-not-a-pass)
    assert facts.get("ran"), f"the pilot body never ran: {facts}"
    if facts.get("room", 0) < 5:
        print("SKIP test_pilot_cursor_scroll_suppression_nests_across_overlapping"
              "_rebuilds (table can only scroll %d rows — nothing to observe)"
              % facts.get("room", 0))
        return
    assert facts.get("start", 0) >= 5, \
        f"test setup: the viewport did not move away from the cursor: {facts}"
    assert facts["still_open"] == facts["start"], \
        ("the second window's scroll ran after the first window closed "
         f"({facts['start']} -> {facts['still_open']})")
    assert facts["closed"] < facts["start"], \
        f"suppression never lifted — a real navigation cannot scroll: {facts}"
    assert facts["depth"] == 0, f"depth went negative: {facts}"


# What a legacy (non-kitty-protocol) POSIX terminal actually SENDS for an F-key:
# F1-F4 ride a final letter (SS3 unmodified, CSI 1;<mod> modified), F5-F12 the
# CSI <code>;<mod>~ form. This is xterm's modifyFunctionKeys encoding — what
# PuTTY ("Xterm 216+"), xterm, VTE, tmux and zellij all speak.
_FKEY_FINAL = {"f1": "P", "f2": "Q", "f3": "R", "f4": "S"}
_FKEY_CODE = {"f5": 15, "f6": 17, "f7": 18, "f8": 19, "f9": 20,
              "f10": 21, "f11": 23, "f12": 24}
_FKEY_MODS = {"": 1, "shift+": 2, "alt+": 3, "ctrl+": 5, "ctrl+shift+": 6}


def _posix_fkey_sequence(key: str):
    """The bytes a legacy POSIX terminal sends for the Textual key name `key`,
    or None when `key` is not a plain/modified F-key (arrows, letters, Esc… are
    out of scope here)."""
    import re as _re
    m = _re.fullmatch(r"((?:ctrl\+)?(?:alt\+)?(?:shift\+)?)(f\d{1,2})", key)
    if not m:
        return None
    mod, fkey = m.group(1), m.group(2)
    if mod not in _FKEY_MODS:
        return None
    n = _FKEY_MODS[mod]
    if fkey in _FKEY_FINAL:
        final = _FKEY_FINAL[fkey]
        return f"\x1bO{final}" if n == 1 else f"\x1b[1;{n}{final}"
    if fkey in _FKEY_CODE:
        code = _FKEY_CODE[fkey]
        return f"\x1b[{code}~" if n == 1 else f"\x1b[{code};{n}~"
    return None


def test_pilot_fkey_bindings_survive_posix_terminal_encoding():
    """Every F-key saikai binds must SURVIVE the round trip through a real POSIX
    terminal: encode the key the way the terminal sends it, parse it back with
    Textual's own XTermParser, and demand the SAME key name out.

    Shift+F3 failed this. xterm spells it CSI 1;2R, whose final `R` is the
    cursor-position report — Textual's parser eats it as a DSR reply, so the
    binding could never fire on Linux/macOS. It only ever worked on Windows
    (native key events) and on kitty-protocol terminals (which spell F3 as
    CSI 13;<mod>~ precisely to dodge this collision), which is why it looked
    fine for a year. F3 with ANY modifier is unreachable, so the fix is to not
    bind one — hence next_attention lives on Shift+F1. (#f3-cpr)

    This guards the whole CLASS: any future F-key binding the terminal cannot
    deliver fails here instead of silently doing nothing in the user's hands."""
    try:
        from textual.app import App  # noqa: F401
        from textual._xterm_parser import XTermParser
        from textual.events import Key
    except Exception:
        print("SKIP test_pilot_fkey_bindings_survive_posix_terminal_encoding "
              "(textual unavailable)")
        return

    import asyncio
    from textual.app import App

    _write_demo_session()          # the app exits early with an empty history
    facts: dict = {}

    def fake_run(self, *a, **kw):
        async def go():
            async with self.run_test(size=(110, 30)) as pilot:
                await pilot.pause(0.2)
                facts["bindings"] = [
                    (b.key, b.action) for b in type(self).BINDINGS
                ]
        asyncio.run(go())

    orig, App.run = App.run, fake_run
    orig_argv = sys.argv
    try:
        sys.argv = ["saikai", "--all"]
        saikai.main()
    finally:
        App.run = orig
        sys.argv = orig_argv

    # The pilot body must have RUN — an empty binding list would sail through
    # every assertion below and ship the invariant unexercised. (#skip-not-a-pass)
    assert facts.get("bindings"), f"the pilot body never ran: {facts}"

    checked, dead = [], []
    for key, action in facts["bindings"]:
        seq = _posix_fkey_sequence(key)
        if seq is None:
            continue
        parser = XTermParser()
        # The trailing 'a' flushes any sequence the parser is still buffering,
        # so a swallowed key shows up as [] rather than "needs more input".
        got = [e.key for e in parser.feed(seq + "a") if isinstance(e, Key)]
        checked.append(key)
        if got[:1] != [key]:
            dead.append((key, action, seq, got))

    assert len(checked) >= 11, \
        f"expected saikai's full F-key set, only saw {checked}"
    assert not dead, (
        "these bindings can never fire on a POSIX terminal — the terminal's own "
        "encoding does not survive Textual's parser:\n  " +
        "\n  ".join(f"{k} ({a}): sent {s!r} -> parsed {g}" for k, a, s, g in dead))


# ── keymap v2 (#keymap-v2) ──────────────────────────────────────────────────

def _default_bindings():
    """(key, action, id) of the shipped PickerApp bindings, read from source so
    the headless tests need no textual."""
    import re
    src = Path(saikai.__file__).read_text(encoding="utf-8")
    i = src.index("class PickerApp(App, _MirrorControl):")
    j = src.index("        ]\n", i)
    return re.findall(r'Binding\("([^"]+)",\s*"([^"]+)"[^)]*?(?:id="([^"]+)")?[^)]*\)',
                      src[i:j])


def test_no_default_binding_uses_a_modified_fkey_or_alt():
    """Shift+F-keys don't reach saikai over PuTTY (default mode) / Termius,
    zellij drops F13-F24 and takes Alt+arrows, and any modified F3 is eaten as a
    cursor-position report — so the shipped map uses none of them."""
    import re
    bad = [k for k, _a, _i in _default_bindings()
           if re.match(r"(shift|ctrl|alt)\+", k)]
    assert not bad, f"modified F-key / Alt default bindings: {bad}"
    keys = [k for k, _a, _i in _default_bindings()]
    assert "f11" not in keys, "F11 belongs to the host terminal (full screen)"


def test_every_action_is_reachable_from_the_menu():
    """The menu is the one path that works on every terminal: each id-bearing
    binding (the F-key accelerators) must also have a menu letter."""
    _lk, m, errs = saikai._resolve_leader({}, {})
    assert not errs, errs
    reachable = set(m.values())
    missing = [(k, a) for k, a, i in _default_bindings()
               if i and i not in ("menu", "help") and a not in reachable]
    assert not missing, f"F-key actions with no menu letter: {missing}"
    for vid, act in saikai.LEADER_VIRTUAL_ACTIONS.items():
        if vid == "focus_swap":
            continue                     # the prefix pressed twice
        assert act in reachable, f"menu action {vid} ({act}) has no letter"


def test_menu_letters_are_case_sensitive():
    _lk, m, _e = saikai._resolve_leader({}, {})
    assert m["x"] == "close_live" and m["X"] == "close_all_live"
    assert m["n"] == "new_session" and m["N"] == "new_session_alt"
    assert m["c"] == "context_refresh" and m["C"] == "checkpoint"
    assert m["3"] == "goto_tab:3"
    # a [keys] uppercase letter stays uppercase
    _lk, m2, _e = saikai._resolve_leader({"favorite": "F"}, {})
    assert m2.get("F") == "toggle_fav" and m2.get("f") is None


def test_mirror_buttons_name_actions_not_keys():
    """Mirror buttons send `action:<name>` (or a plain navigation key), so the
    keymap can change without breaking the phone; every action is allow-listed
    and enabling control never is (local-only)."""
    import re
    src = Path(saikai.__file__).with_name("saikai_mirror.py").read_text(encoding="utf-8")
    plain = {"tab", "escape", "enter", "up", "down", "left", "right", "slash",
             "pageup", "pagedown", "home", "end", ""}
    vals = set(re.findall(r'data-k="([^"]*)"', src)) | set(
        re.findall(r"\['[^']+','([^']*)'\]", src))
    for v in vals:
        if v.startswith("action:"):
            assert v[len("action:"):] in saikai._MIRROR_ACTIONS, v
        else:
            assert v in plain, f"mirror button sends a raw key name: {v!r}"
    assert "toggle_mirror_control" not in saikai._MIRROR_ACTIONS


def test_fmt_key_is_human():
    assert saikai._fmt_key("f1") == "F1"
    assert saikai._fmt_key("ctrl+right_square_bracket") == "Ctrl+]"
    assert saikai._fmt_key("space") == "␣"


def test_pane_lets_menu_keys_bubble_and_writes_nothing():
    """With the host menu open, a focused pane must NOT write the key to the
    PTY (not even Esc — it would interrupt the agent); it bubbles to the host.
    In the repeat window after < > [ ] only those keys are the menu's."""
    try:
        import saikai_terminal as st
        if not hasattr(st, "AgentTerminal") or st.Widget is object:
            raise ImportError
    except Exception:
        print("SKIP test_pane_lets_menu_keys_bubble_and_writes_nothing")
        return

    class FakeApp:
        _leader_pending = True
        _leader_repeat_only = False
        _leader_repeat_keys = frozenset("<>[]")
        cancelled = 0
        def _cancel_leader(self):
            FakeApp.cancelled += 1
            self._leader_pending = False

    fake = FakeApp()

    class T(st.AgentTerminal):
        app = fake

    t = T.__new__(T)
    t._pty = object(); t.is_dead = False; t._frozen = False
    written = []
    t._write_child = lambda d: written.append(d) or True
    t._note_input = lambda: None; t._snap_to_live = lambda: None
    t._kitty_flags = lambda: 0; t._app_cursor = False

    class Ev:
        def __init__(self, key, ch):
            self.key, self.character, self.stopped = key, ch, False
        def stop(self):
            self.stopped = True

    for key, ch in (("f", "f"), ("escape", None)):
        ev = Ev(key, ch)
        t.on_key(ev)
        assert not written and not ev.stopped, (key, written)
    # repeat window: '>' is the menu's, 'h' is typed (and ends the window)
    fake._leader_repeat_only = True
    ev = Ev("greater_than_sign", ">")
    t.on_key(ev)
    assert not written and not ev.stopped
    ev = Ev("h", "h")
    t.on_key(ev)
    assert written == ["h"] and ev.stopped and FakeApp.cancelled == 1


def test_pilot_prefix_from_a_pane_opens_the_menu_for_that_pane():
    """Ctrl+] in a pane: the menu opens at once for THAT pane — f favorites the
    pane's session, not the row under the list cursor — and the prefix pressed
    twice swaps focus (no action runs)."""
    try:
        from textual.app import App  # noqa: F401
    except Exception:
        print("SKIP test_pilot_prefix_from_a_pane_opens_the_menu_for_that_pane")
        return
    import asyncio
    from types import SimpleNamespace
    from textual.app import App

    pane_sid = _write_demo_session()
    _write_demo_session()
    facts: dict = {}

    def fake_run(self, *a, **kw):
        async def go():
            async with self.run_test(size=(110, 30)) as pilot:
                await pilot.pause(0.4)
                if type(self.screen).__name__ == "MirrorScreen":
                    await pilot.press("escape")
                cursor = self._cursor_sid()
                facts["cursor_is_other"] = cursor != pane_sid
                term = SimpleNamespace(sid=pane_sid)
                ev = SimpleNamespace(stop=lambda: None, terminal=term)
                self.on_agent_terminal_focus_released(ev)
                await pilot.pause(0.05)
                facts["pending"] = self._leader_pending
                facts["hint_shown"] = bool(self.query_one("#leaderhint").display)
                await pilot.press("f")
                await pilot.pause(0.2)
                favs = saikai._read_json(saikai.FAVORITE_FILE, []) or []
                facts["pane_faved"] = pane_sid in favs
                facts["cursor_faved"] = cursor in favs
                saikai._write_json(saikai.FAVORITE_FILE,
                                   [x for x in favs if x != pane_sid])   # leave no trace
                # twice = swap, nothing else runs
                self.on_agent_terminal_focus_released(ev)
                self.on_agent_terminal_focus_released(ev)
                await pilot.pause(0.05)
                facts["pending_after_twice"] = self._leader_pending
                facts["keys_for_restore"] = self._keys_for("restore")
                # the generated help must render: a "␣[" key once opened a tag
                # and turned every following row into literal markup
                self.action_help()
                await pilot.pause(0.3)
                from textual.widgets import Static as _S
                _txt = " ".join(str(w.render()) for w in self.screen.query(_S))
                facts["help_has_prev_tab"] = "previous tab" in _txt
                facts["help_leaks_markup"] = "[/yellow]" in _txt
                await pilot.press("escape")
                await pilot.pause(0.2)
                self._applied_keymap = {"attention": "f11"}
                facts["keys_for_remapped"] = self._keys_for("attention")
        asyncio.run(go())

    orig_run, App.run = App.run, fake_run
    orig_argv = sys.argv
    try:
        sys.argv = ["saikai", "--all"]
        saikai.main()
    finally:
        App.run = orig_run
        sys.argv = orig_argv
    assert facts.get("cursor_is_other"), facts
    assert facts.get("pending") and facts.get("hint_shown"), facts
    assert facts.get("pane_faved") and not facts.get("cursor_faved"), facts
    assert facts.get("pending_after_twice") is False, facts
    assert facts.get("keys_for_restore") == "␣u", facts
    assert facts.get("help_has_prev_tab") and not facts.get("help_leaks_markup"), facts
    assert facts.get("keys_for_remapped") == "F11 / ␣a", facts


def test_pilot_new_session_agent_row_switches_by_key_arrows_and_click():
    """The Agent row: the key that opened the dialog (F5) switches it, ↑ then ←
    switches back, and a click picks an agent — no Shift+F needed anywhere."""
    try:
        from textual.app import App  # noqa: F401
    except Exception:
        print("SKIP test_pilot_new_session_agent_row_switches_by_key_arrows_and_click")
        return
    import asyncio
    import shutil as _sh
    from textual.app import App

    _write_demo_session()
    facts: dict = {}
    calls = []
    codex_home = _FAKE_HOME / ".codex" / "sessions"
    codex_home.mkdir(parents=True, exist_ok=True)

    def fake_run(self, *a, **kw):
        async def go():
            async with self.run_test(size=(120, 32)) as pilot:
                await pilot.pause(0.4)
                if type(self.screen).__name__ == "MirrorScreen":
                    await pilot.press("escape")
                self._open_new_live = lambda path, agent="claude": calls.append((path, agent))
                await pilot.press("f5")
                for _ in range(20):
                    await pilot.pause(0.2)
                    if type(self.screen).__name__ == "NewSessionScreen":
                        break
                scr = self.screen
                facts["opened"] = type(scr).__name__
                facts["start"] = scr._agent
                await pilot.press("f5")
                await pilot.pause(0.1)
                facts["after_f5"] = scr._agent
                await pilot.press("up")
                await pilot.press("left")
                await pilot.pause(0.1)
                facts["after_left"] = scr._agent
                await pilot.click("#new-agent",
                                  offset=(len("Agent  ") + len(" ✻ claude ") + 3, 0))
                await pilot.pause(0.1)
                facts["after_click"] = scr._agent
                await pilot.press("down")
                scr.query_one("#new-path").value = str(_FAKE_HOME)
                await pilot.press("enter")
                await pilot.pause(0.3)
        asyncio.run(go())

    orig_run, App.run = App.run, fake_run
    orig_which = _sh.which
    _sh.which = lambda name, *a, **k: f"/usr/bin/{name}"
    orig_argv = sys.argv
    try:
        sys.argv = ["saikai", "--all"]
        saikai.main()
    finally:
        App.run = orig_run
        _sh.which = orig_which
        sys.argv = orig_argv
    assert facts.get("opened") == "NewSessionScreen", facts
    assert facts["start"] == "claude" and facts["after_f5"] == "codex", facts
    assert facts["after_left"] == "claude", facts
    assert facts["after_click"] == "codex", facts       # click on the codex cell
    assert calls == [(str(_FAKE_HOME), "codex")], calls
    assert saikai._load_options().get("new_agent") == "codex"   # remembered


def test_mirror_action_injection_is_allow_listed():
    """`action:<name>` from a mirror button runs only allow-listed actions —
    enabling control stays local-only — and only while control is on."""
    ran = []

    class Fake(saikai._MirrorControl):
        _control_enabled = True
        def action_toggle_fav(self):
            ran.append("fav")
        def action_toggle_mirror_control(self):
            ran.append("CONTROL")
        def action_goto_tab(self, n):
            ran.append(f"tab{n}")

    f = Fake()
    f._mirror_inject_key("action:toggle_fav")
    f._mirror_inject_key("action:toggle_mirror_control")
    f._mirror_inject_key("action:goto_tab:2")
    f._mirror_inject_key("action:quit_all")
    f.screen_stack = [1, 2]                      # a host dialog is open
    f._mirror_inject_key("action:toggle_fav")
    f.screen_stack = [1]
    f._mirror_inject_key("action:close_all_live")  # destructive: not from a phone
    f._control_enabled = False
    f._mirror_inject_key("action:toggle_fav")
    assert ran == ["fav", "tab2"], ran


def test_leader_hint_lines_align_and_pair():
    """The menu panel: every row starts its entries at the same column, the
    family header carries its icon, pairs sit together (x next to X), and a
    narrow terminal wraps into more rows instead of overflowing."""
    from rich.text import Text
    from rich.cells import cell_len
    _lk, m, _e = saikai._resolve_leader({}, {})
    for width in (148, 80):
        lines = saikai._leader_hint_lines(m, width)
        plain = [Text.from_markup(l).plain for l in lines]
        assert all(cell_len(p) <= width for p in plain), (width, plain)
        body = plain[1:]
        assert body[0].startswith(saikai.LEADER_FAMILY_ICON["Move"])
        # entries begin at the same column on every row
        starts = {cell_len(p[:len(p) - len(p[11:].lstrip())]) for p in body}
        assert len(starts) <= 2, (width, starts)    # key width right-aligns 1-9
        flat = " ".join(body)
        assert flat.index("x close") < flat.index("X close all") < flat.index("u restore")
    assert len(saikai._leader_hint_lines(m, 80)) > len(saikai._leader_hint_lines(m, 148))
    assert saikai._leader_hint_lines({}, 100) == []
    # [display] menu_icons = false: no emoji at all (PuTTY draws them at the wrong width)
    no_icons = " ".join(saikai._leader_hint_lines(m, 148, icons=False))
    assert not any(i in no_icons for i in saikai.LEADER_FAMILY_ICON.values())


def test_review_fixes_keymap_v2_units():
    """Review findings (#keymap-v2 review): every menu action is modal-blocked
    (covers [keys] direct remaps), key names escape '[' AND backslash, and the
    prefix honours [keys] release even without split-live."""
    from rich.text import Text
    acts = {a.split(":")[0] for a in saikai.LEADER_VIRTUAL_ACTIONS.values()}
    src = Path(saikai.__file__).read_text(encoding="utf-8")
    assert "| {a.split(\":\")[0] for a in LEADER_VIRTUAL_ACTIONS.values()}" in src
    assert acts                                      # (the set union above covers them)
    for key in ("␣[", "\\", "a\\"):
        shown = Text.from_markup(f"[b]{saikai._esc_key(key)}[/b] after").plain
        assert shown == f"{key} after", (key, shown)
    old = os.environ.get("SAIKAI_RELEASE_KEY")
    os.environ["SAIKAI_RELEASE_KEY"] = "ctrl+\\"
    try:
        if hasattr(saikai, "_reset_config_cache"):
            saikai._reset_config_cache()
        assert saikai._prefix_key() == "ctrl+backslash"
        assert saikai._prefix_bytes() == "\x1c"
    finally:
        if old is None:
            os.environ.pop("SAIKAI_RELEASE_KEY", None)
        else:
            os.environ["SAIKAI_RELEASE_KEY"] = old
        if hasattr(saikai, "_reset_config_cache"):
            saikai._reset_config_cache()
    assert saikai._prefix_key() == "ctrl+right_square_bracket"


def test_pilot_menu_state_never_dangles():
    """A pane-opened menu that times out must not scope the NEXT list menu to
    that pane, double-Space after it must not leave the leader armed (#H10), a
    priority binding that runs closes the menu, and close-all asks first."""
    try:
        from textual.app import App  # noqa: F401
    except Exception:
        print("SKIP test_pilot_menu_state_never_dangles")
        return
    import asyncio
    from types import SimpleNamespace
    from textual.app import App

    pane_sid = _write_demo_session()
    _write_demo_session()
    facts: dict = {}

    def fake_run(self, *a, **kw):
        async def go():
            async with self.run_test(size=(110, 30)) as pilot:
                await pilot.pause(0.4)
                if type(self.screen).__name__ == "MirrorScreen":
                    await pilot.press("escape")
                cursor = self._cursor_sid()
                ev = SimpleNamespace(stop=lambda: None, terminal=SimpleNamespace(sid=pane_sid))
                self.on_agent_terminal_focus_released(ev)
                self._cancel_leader()                    # the 6 s timeout
                facts["src_cleared"] = self._leader_src is None
                await pilot.press("space", "h")          # list menu: hide the CURSOR row
                await pilot.pause(0.2)
                hidden = saikai._read_json(saikai.HIDDEN_FILE, []) or []
                facts["cursor_hidden"] = cursor in hidden
                facts["pane_hidden"] = pane_sid in hidden
                await pilot.press("space", "h")          # undo
                await pilot.pause(0.1)
                self.on_agent_terminal_focus_released(ev)
                self._cancel_leader()
                await pilot.press("space", "space")      # mark (double Space)
                await pilot.pause(0.2)
                facts["armed_after_double"] = self._leader_pending
                if self._leader_pending:
                    self._cancel_leader()
                await pilot.press("space")
                await pilot.press("f8")                  # a priority binding RUNS
                await pilot.pause(0.2)
                facts["armed_after_f8"] = self._leader_pending
                if type(self.screen).__name__ != "Screen":
                    await pilot.press("escape")
                # close-all is confirmed
                closed = []
                self._live_pane_ids = lambda: ["p1", "p2"]
                self._close_all_live_now = lambda: closed.append(1)
                self.action_close_all_live()
                await pilot.pause(0.2)
                facts["confirm_screen"] = type(self.screen).__name__
                await pilot.press("escape")
                await pilot.pause(0.1)
                facts["closed_after_esc"] = list(closed)
                self.action_close_all_live()
                await pilot.pause(0.2)
                await pilot.press("enter")
                await pilot.pause(0.1)
                facts["closed_after_enter"] = list(closed)
        asyncio.run(go())

    orig_run, App.run = App.run, fake_run
    orig_argv = sys.argv
    try:
        sys.argv = ["saikai", "--all"]
        saikai.main()
    finally:
        App.run = orig_run
        sys.argv = orig_argv
    assert facts.get("src_cleared"), facts
    assert facts.get("cursor_hidden") and not facts.get("pane_hidden"), facts
    assert facts.get("armed_after_double") is False, facts
    assert facts.get("armed_after_f8") is False, facts
    assert facts.get("confirm_screen") == "CloseAllScreen", facts
    assert facts.get("closed_after_esc") == [] and facts.get("closed_after_enter") == [1], facts


if __name__ == "__main__":
    test_review_fixes_keymap_v2_units()
    print("PASS test_review_fixes_keymap_v2_units")
    test_pilot_menu_state_never_dangles()
    print("PASS test_pilot_menu_state_never_dangles")
    test_leader_hint_lines_align_and_pair()
    print("PASS test_leader_hint_lines_align_and_pair")
    test_mirror_action_injection_is_allow_listed()
    print("PASS test_mirror_action_injection_is_allow_listed")
    test_no_default_binding_uses_a_modified_fkey_or_alt()
    print("PASS test_no_default_binding_uses_a_modified_fkey_or_alt")
    test_every_action_is_reachable_from_the_menu()
    print("PASS test_every_action_is_reachable_from_the_menu")
    test_menu_letters_are_case_sensitive()
    print("PASS test_menu_letters_are_case_sensitive")
    test_mirror_buttons_name_actions_not_keys()
    print("PASS test_mirror_buttons_name_actions_not_keys")
    test_fmt_key_is_human()
    print("PASS test_fmt_key_is_human")
    test_pane_lets_menu_keys_bubble_and_writes_nothing()
    print("PASS test_pane_lets_menu_keys_bubble_and_writes_nothing")
    test_pilot_prefix_from_a_pane_opens_the_menu_for_that_pane()
    print("PASS test_pilot_prefix_from_a_pane_opens_the_menu_for_that_pane")
    test_pilot_new_session_agent_row_switches_by_key_arrows_and_click()
    print("PASS test_pilot_new_session_agent_row_switches_by_key_arrows_and_click")
    test_pilot_fkey_bindings_survive_posix_terminal_encoding()
    print("PASS test_pilot_fkey_bindings_survive_posix_terminal_encoding")
    test_resolve_leader_defaults_on()
    print("PASS test_resolve_leader_defaults_on")
    test_resolve_leader_disable_and_custom_key()
    print("PASS test_resolve_leader_disable_and_custom_key")
    test_resolve_leader_user_letter_wins()
    print("PASS test_resolve_leader_user_letter_wins")
    test_resolve_leader_no_defaults()
    print("PASS test_resolve_leader_no_defaults")
    test_resolve_leader_ignores_release_key()
    print("PASS test_resolve_leader_ignores_release_key")
    test_nudge_split_ratio_clamps()
    print("PASS test_nudge_split_ratio_clamps")
    test_leader_label_short_names()
    print("PASS test_leader_label_short_names")
    test_leader_groups_by_family()
    print("PASS test_leader_groups_by_family")
    test_leader_hint_item_separates_key_from_action()
    print("PASS test_leader_hint_item_separates_key_from_action")
    test_pilot_space_leader_and_divider()
    print("PASS test_pilot_space_leader_and_divider")
    test_pilot_synchronized_output_brackets_frames()
    print("PASS test_pilot_synchronized_output_brackets_frames")
    test_pilot_session_list_disables_mouse_hover_repaint()
    print("PASS test_pilot_session_list_disables_mouse_hover_repaint")
    test_pilot_search_clear_button()
    print("PASS test_pilot_search_clear_button")
    test_pilot_custom_leader_does_not_leave_space_as_menu()
    print("PASS test_pilot_custom_leader_does_not_leave_space_as_menu")
    test_pilot_settings_screen()
    print("PASS test_pilot_settings_screen")
    test_pilot_esc_quits_and_bar_toggle()
    print("PASS test_pilot_esc_quits_and_bar_toggle")
    test_ctrlc_double_press_and_disarm()
    print("PASS test_ctrlc_double_press_and_disarm")
    test_ctrlq_is_double_press_guarded()
    print("PASS test_ctrlq_is_double_press_guarded")
    test_focus_moves_are_logged()
    print("PASS test_focus_moves_are_logged")
    test_status_refresh_deferred_while_pane_focused()
    print("PASS test_status_refresh_deferred_while_pane_focused")
    test_launch_qr_dismiss_reshows_restore_hint()
    print("PASS test_launch_qr_dismiss_reshows_restore_hint")
    test_pilot_mirror_control_toggle()
    print("PASS test_pilot_mirror_control_toggle")
    test_pilot_mirror_tap_and_key_drive_ui()
    print("PASS test_pilot_mirror_tap_and_key_drive_ui")
    test_pilot_mirror_text_drives_search()
    print("PASS test_pilot_mirror_text_drives_search")
    test_pilot_mirror_arrow_byte_drives_app()
    print("PASS test_pilot_mirror_arrow_byte_drives_app")
    test_pilot_agents_kind_groups_and_refusal()
    test_pilot_kill_agent_gated_and_verified()
    test_pilot_remote_origin_badge_and_resume_block()
    test_pilot_autorefresh_gate_catches_transcript_growth()
    test_pilot_mirror_resize_syncs_size()
    test_pilot_mirror_push_regions()
    test_pilot_mirror_checkpoint_pseudo_key()
    test_pilot_mirror_space_leader_runs_mnemonic()
    test_pilot_rename_modal_enter_saves_not_resumes()
    test_pilot_ctrlc_over_modal_does_not_quit()
    test_pilot_quit_arm_cleared_when_a_screen_opens()
    test_pilot_esc_in_search_clears_filter()
    test_pilot_notification_center_records_and_opens()
    test_pilot_ctx_gauge_in_statusbar()
    test_pilot_open_parent()
    test_pilot_context_refresh_idle_and_busy()
    test_pilot_checkpoint_gated_clear_and_lineage()
    test_pilot_refresh_preserves_scroll_position()
    test_pilot_filter_engaged_window_survives_focus_move()
    test_pilot_checkpoint_marker_on_row()
    test_pilot_toast_rows_self_heal()
    test_pilot_toast_user_content_renders_verbatim()
    test_tabpane_title_uses_markup_safe_content_not_rich_text()
    print("PASS test_tabpane_title_uses_markup_safe_content_not_rich_text")
    test_statusbar_markup_escapes_user_search_and_folder()
    print("PASS test_statusbar_markup_escapes_user_search_and_folder")
    test_tab_glyph_updates_via_tab_label_not_pane_label()
    print("PASS test_tab_glyph_updates_via_tab_label_not_pane_label")
    test_kill_agent_yields_key_to_a_focused_live_pane()
    print("PASS test_kill_agent_yields_key_to_a_focused_live_pane")
    test_pilot_cycle_tab_skips_dead_pane()
    test_pilot_double_space_does_not_leave_leader_armed()
    print("PASS test_pilot_double_space_does_not_leave_leader_armed")
    test_pilot_front_door_homes_on_needs_you()
    print("PASS test_pilot_front_door_homes_on_needs_you")
    print("PASS test_pilot_mirror_space_leader_runs_mnemonic")
    print("PASS test_pilot_ctx_gauge_in_statusbar")
    print("PASS test_pilot_open_parent")
    print("PASS test_pilot_context_refresh_idle_and_busy")
    print("PASS test_pilot_checkpoint_gated_clear_and_lineage")
    print("PASS test_pilot_refresh_preserves_scroll_position")
    print("PASS test_pilot_filter_engaged_window_survives_focus_move")
    print("PASS test_pilot_checkpoint_marker_on_row")
    print("PASS test_pilot_toast_user_content_renders_verbatim")
    print("PASS test_pilot_cycle_tab_skips_dead_pane")
    print("ALL PASS")
    test_pilot_cursor_restore_with_scroll_false_does_not_scroll()
    print("PASS test_pilot_cursor_restore_with_scroll_false_does_not_scroll")
    test_pilot_rebuild_is_deferred_while_the_list_is_scrolling()
    print("PASS test_pilot_rebuild_is_deferred_while_the_list_is_scrolling")
    test_pilot_wheel_scroll_is_monotonic_under_constant_rebuilds()
    print("PASS test_pilot_wheel_scroll_is_monotonic_under_constant_rebuilds")
    test_pilot_cursor_scroll_suppression_nests_across_overlapping_rebuilds()
    print("PASS test_pilot_cursor_scroll_suppression_nests_across_overlapping_rebuilds")
