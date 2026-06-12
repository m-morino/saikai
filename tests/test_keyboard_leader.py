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
    "cluster": "toggle_cluster", "group": "cycle_group", "new": "new_session",
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
    assert (" ", "mark") in by_fam["Panes"] and ("[", "tab◀") in by_fam["Panes"]
    # unknown action -> last family, not dropped
    g2 = saikai._leader_groups({"q": "made_up_action"})
    assert g2 and g2[-1][0] == saikai.LEADER_FAMILY_ORDER[-1]
    assert ("q", "made_up_action") in g2[-1][1]
    assert saikai._leader_groups({}) == []


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

    assert facts.get("favorited"), f"leader Space→f did not favorite: {facts}"
    assert facts["ratio_after"] > facts["ratio_before"], facts
    assert abs(facts["ratio_saved"] - facts["ratio_after"]) < 1e-6, facts
    assert facts.get("bar_shown"), facts


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


if __name__ == "__main__":
    test_resolve_leader_defaults_on()
    print("PASS test_resolve_leader_defaults_on")
    test_resolve_leader_disable_and_custom_key()
    print("PASS test_resolve_leader_disable_and_custom_key")
    test_resolve_leader_user_letter_wins()
    print("PASS test_resolve_leader_user_letter_wins")
    test_resolve_leader_no_defaults()
    print("PASS test_resolve_leader_no_defaults")
    test_nudge_split_ratio_clamps()
    print("PASS test_nudge_split_ratio_clamps")
    test_leader_label_short_names()
    print("PASS test_leader_label_short_names")
    test_leader_groups_by_family()
    print("PASS test_leader_groups_by_family")
    test_pilot_space_leader_and_divider()
    print("PASS test_pilot_space_leader_and_divider")
    test_pilot_settings_screen()
    print("PASS test_pilot_settings_screen")
    print("ALL PASS")
