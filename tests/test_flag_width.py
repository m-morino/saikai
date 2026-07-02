"""Regression test for the Regional-Indicator flag-emoji width fix (#flag-width).

pyte (via wcwidth) counts each RI symbol as width 2, so a flag like 🇯🇵 would take
4 cells and drift every line carrying it (claude's "🇯🇵 JA" status line) — Rich
and Windows Terminal render a flag pair as width 2. saikai_terminal patches
pyte.screens.wcwidth on import so a flag is 2 cells, matching the render target.

    uv run python tests/test_flag_width.py
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import saikai_terminal  # noqa: F401 — importing applies the wcwidth patch

_JP_FLAG = "\U0001F1EF\U0001F1F5"   # 🇯🇵


def test_regional_indicator_is_width_1():
    import pyte
    assert pyte.screens.wcwidth("\U0001F1EF") == 1
    assert pyte.screens.wcwidth("\U0001F1F5") == 1


def test_flag_occupies_two_cells_in_pyte_grid():
    """X + 🇯🇵 + Y → Y lands at column 3 (flag = cols 1-2), not column 5."""
    import pyte
    s = pyte.Screen(20, 1)
    st = pyte.Stream(s)
    st.feed("X" + _JP_FLAG + "Y")
    row = s.buffer[0]
    got = [row[i].data for i in range(6)]
    assert row[3].data == "Y", f"flag not width-2 (Y should be at col 3): {got}"
    assert row[0].data == "X" and row[1].data == "\U0001F1EF" and row[2].data == "\U0001F1F5"


def test_matches_rich_cell_width():
    """The whole point: pyte's flag width now equals what Rich/WT render (2)."""
    import pyte
    from rich.cells import cell_len
    pyte_cells = pyte.screens.wcwidth("\U0001F1EF") + pyte.screens.wcwidth("\U0001F1F5")
    assert pyte_cells == cell_len(_JP_FLAG) == 2


if __name__ == "__main__":
    test_regional_indicator_is_width_1()
    test_flag_occupies_two_cells_in_pyte_grid()
    test_matches_rich_cell_width()
    print("OK test_flag_width")
