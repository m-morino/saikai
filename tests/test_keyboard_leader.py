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
from pathlib import Path

# Point saikai at a throwaway home BEFORE importing it (it derives CACHE_DIR /
# state files from Path.home() at import time). Each test file runs in its own
# process, so this cannot leak into the other suites.
_FAKE_HOME = Path(tempfile.mkdtemp(prefix="saikai-kbd-test-"))
for _var in ("USERPROFILE", "HOME", "APPDATA", "LOCALAPPDATA", "XDG_CONFIG_HOME"):
    os.environ[_var] = str(_FAKE_HOME)
os.environ.pop("SAIKAI_CONFIG", None)
os.environ["SAIKAI_SUMMARIZE_ENABLED"] = "0"

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
    "toggle_list": "toggle_list", "rename": "rename",
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
    assert saikai._leader_label("toggle_fav") == "fav"
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
    assert len(flat) == len(m), "a letter vanished from the grouped view"
    by_fam = dict(groups)
    assert ("f", "fav") in by_fam["Session"]
    assert ("s", "sort") in by_fam["View"] and ("g", "group") in by_fam["View"]
    assert (",", "settings") in by_fam["View"]
    assert ("/", "bar") in by_fam["View"]
    assert (" ", "mark") in by_fam["Panes"] and ("[", "tab◀") in by_fam["Panes"]
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
                await pilot.press("alt+right")      # keyboard divider nudge
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
        def url(self):
            return "http://127.0.0.1:0/?token=x"

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
                await pilot.press("shift+f12")        # the priority toggle
                await pilot.pause(0.2)
                facts["after_enabled"] = self._control_enabled
                facts["hub_calls"] = list(self._mirror_hub.calls)
                await pilot.press("shift+f12")        # toggle back off
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
                self._mirror_inject_key("f6")
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


if __name__ == "__main__":
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
    test_pilot_custom_leader_does_not_leave_space_as_menu()
    print("PASS test_pilot_custom_leader_does_not_leave_space_as_menu")
    test_pilot_settings_screen()
    print("PASS test_pilot_settings_screen")
    test_pilot_esc_quits_and_bar_toggle()
    print("PASS test_pilot_esc_quits_and_bar_toggle")
    test_ctrlc_double_press_and_disarm()
    print("PASS test_ctrlc_double_press_and_disarm")
    test_pilot_mirror_control_toggle()
    print("PASS test_pilot_mirror_control_toggle")
    test_pilot_mirror_tap_and_key_drive_ui()
    print("PASS test_pilot_mirror_tap_and_key_drive_ui")
    test_pilot_mirror_text_drives_search()
    print("PASS test_pilot_mirror_text_drives_search")
    test_pilot_mirror_arrow_byte_drives_app()
    print("PASS test_pilot_mirror_arrow_byte_drives_app")
    test_pilot_mirror_space_leader_runs_mnemonic()
    print("PASS test_pilot_mirror_space_leader_runs_mnemonic")
    print("ALL PASS")
