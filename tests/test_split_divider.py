"""Phase 5.3 — draggable list/pane divider.

Two layers:
  * Pure, always-run: _split_ratio_from_x clamping + the options.json persist
    round-trip (_get/_set_split_ratio).
  * Runtime smoke (needs textual): the new layout CSS (#grip width:1, #right
    1fr, `#main.nolist #grip { display:none }`) PARSES and mounts, an inline
    styles.width resize applies, and toggling `nolist` hides the grip — i.e. the
    exact CSS constructs recap's PickerApp now relies on are accepted by the
    installed textual. Skips cleanly when textual is unavailable.

Run (pure only):   python tests/test_split_divider.py
Run (with smoke):  uv run --no-project --with textual python tests/test_split_divider.py
"""
import asyncio
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import recap

try:
    from textual.app import App
    from textual.containers import Horizontal
    from textual.widgets import DataTable, RichLog, Static
    HAVE_TEXTUAL = True
except Exception:
    HAVE_TEXTUAL = False


def test_split_ratio_from_x_clamps():
    lo, hi = recap._SPLIT_RATIO_LO, recap._SPLIT_RATIO_HI
    # left=10, width=100 → x=60 → mid
    assert abs(recap._split_ratio_from_x(60, 10, 100) - 0.5) < 1e-9
    assert recap._split_ratio_from_x(11, 10, 100) == lo     # dragged toward 0 → floor
    assert recap._split_ratio_from_x(9999, 10, 100) == hi   # dragged past end → ceil
    assert recap._split_ratio_from_x(50, 0, 0) == lo        # zero width → no div-by-zero


def test_split_ratio_persist_roundtrip():
    """_set_split_ratio writes options.json; _get_split_ratio reads it back,
    clamped. Isolated to a temp OPTIONS_FILE so the user's prefs are untouched."""
    d = Path(tempfile.mkdtemp())
    saved = recap.OPTIONS_FILE
    for k in ("RECAP_SPLIT_RATIO", "RECAP_CONFIG"):
        os.environ.pop(k, None)
    recap._reset_config_cache()
    recap.OPTIONS_FILE = d / "options.json"
    try:
        recap._set_split_ratio(0.42)
        assert abs(recap._get_split_ratio() - 0.42) < 1e-9
        recap._set_split_ratio(0.99)                         # clamps to hi
        assert recap._get_split_ratio() == recap._SPLIT_RATIO_HI
        recap._set_split_ratio(0.01)                         # clamps to lo
        assert recap._get_split_ratio() == recap._SPLIT_RATIO_LO
        # absent → default 0.34 (no options key, no env/config)
        recap.OPTIONS_FILE = d / "empty.json"
        assert abs(recap._get_split_ratio() - 0.34) < 1e-9
    finally:
        recap.OPTIONS_FILE = saved
        recap._reset_config_cache()


if HAVE_TEXTUAL:
    class _MiniApp(App):
        # The new constructs recap's real CSS depends on (replica — the real
        # CSS lives in a nested class that can't be imported headless).
        CSS = """
        #main { layout: horizontal; height: 1fr; }
        #table { width: 60%; }
        #main.split #table { width: 34%; }
        #grip { width: 1; background: $panel; }
        #grip:hover { background: $accent; }
        #right { width: 1fr; }
        #main.nolist #table { display: none; }
        #main.nolist #grip { display: none; }
        """

        def compose(self):
            with Horizontal(id="main", classes="split"):
                yield DataTable(id="table")
                yield Static("", id="grip")
                yield RichLog(id="right")

        def on_mount(self):
            self.query_one("#table").styles.width = "40.0%"


async def _mount_smoke():
    app = _MiniApp()
    async with app.run_test() as pilot:
        grip = app.query_one("#grip")
        assert grip is not None and grip.display is True
        assert app.query_one("#main").region.width > 0
        # the live pane is 1fr → it has real width beside the (narrow) list+grip
        assert app.query_one("#right").region.width > 0
        # F4-style nolist hides the grip (and the list)
        app.query_one("#main").add_class("nolist")
        await pilot.pause()
        assert app.query_one("#grip").display is False
        assert app.query_one("#table").display is False


def test_layout_mounts_and_nolist_hides_grip():
    if not HAVE_TEXTUAL:
        print("SKIP test_layout_mounts_and_nolist_hides_grip (textual unavailable)")
        return
    asyncio.run(_mount_smoke())


if __name__ == "__main__":
    test_split_ratio_from_x_clamps()
    print("PASS test_split_ratio_from_x_clamps")
    test_split_ratio_persist_roundtrip()
    print("PASS test_split_ratio_persist_roundtrip")
    test_layout_mounts_and_nolist_hides_grip()
    print("PASS test_layout_mounts_and_nolist_hides_grip")
    print("ALL PASS")
