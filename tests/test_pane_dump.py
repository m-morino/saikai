"""Regression test for the pane-dump / status-classifier grid render (#pane-dump).

pyte's ``Screen.display`` carries ``assert sum(map(wcwidth, char[1:])) == 0``,
which raises ``AssertionError`` on a cell whose combining TAIL has a non-zero
width — real terminal output can produce that. ``snapshot_text`` and
``_current_screen`` fed that through ``except Exception`` and so left the pane
dump BODY empty (the reported "画面が崩れている") and the status classifier blank.
``_pyte_grid_lines`` walks the buffer directly (no wcwidth, no assert) so it can
render the visible grid even when ``.display`` would blow up.

    uv run python tests/test_pane_dump.py
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import saikai_terminal  # noqa: F401 — importing applies the wcwidth patch
import pyte


def _pathological_screen():
    """A HistoryScreen whose row 0 cell 0 has a combining tail with non-zero
    width — exactly what makes pyte's ``Screen.display`` assert fail."""
    scr = pyte.HistoryScreen(20, 2)
    pyte.Stream(scr).feed("hello\r\n")
    c = scr.buffer[0][0]
    scr.buffer[0][0] = c._replace(data="a\U0001F600")   # 'a' + a wide emoji "tail"
    return scr


def test_display_property_raises_on_pathological_cell():
    """Guard: prove the pyte fragility we work around is real (not hypothetical)."""
    scr = _pathological_screen()
    raised = False
    try:
        list(scr.display)
    except Exception:
        raised = True
    assert raised, "expected pyte Screen.display to raise on the pathological cell"


def test_grid_lines_survive_display_failure():
    """The dump body must NOT come back empty when ``.display`` would raise."""
    scr = _pathological_screen()
    lines = saikai_terminal._pyte_grid_lines(scr)
    assert len(lines) == 2, f"expected one string per screen row, got {lines!r}"
    assert lines[0].startswith("a"), f"row 0 lost its content: {lines[0]!r}"
    assert "ello" in lines[0], f"rest of row 0 dropped: {lines[0]!r}"


def test_grid_lines_match_display_on_happy_path():
    """On a screen ``.display`` can handle, the buffer walk matches it exactly."""
    scr = pyte.HistoryScreen(20, 1)
    pyte.Stream(scr).feed("X\U0001F1EF\U0001F1F5Y")   # X + 🇯🇵 + Y
    assert saikai_terminal._pyte_grid_lines(scr) == list(scr.display)


if __name__ == "__main__":
    test_display_property_raises_on_pathological_cell()
    test_grid_lines_survive_display_failure()
    test_grid_lines_match_display_on_happy_path()
    print("OK test_pane_dump")
