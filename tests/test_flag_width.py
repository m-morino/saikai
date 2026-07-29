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

def test_emoji_presentation_cluster_occupies_the_columns_it_renders():
    """A cell must never render wider than the columns it occupies.

    "⚠" is one column, but "⚠"+VS16 is an emoji-presentation sequence that Rich —
    so Textual, so the terminal — draws in two. The zero-width merge used to leave
    that cluster in ONE cell, so the model advanced one column while the render took
    two: every glyph after it on the row landed a column right of where the child put
    it and the row's tail spilled past the pane. MEASURED on a real capture: the two
    rows containing ⚠️ handed Textual a 98-cell Strip for a 97-cell pane, and rows
    looked shifted until the child redrew them without the emoji."""
    import threading
    import pyte
    from rich.cells import cell_len

    COLS, ROWS = 20, 3
    scr = saikai_terminal._HistoryScreenBase(COLS, ROWS, history=10, ratio=0.5)
    pyte.Stream(scr, strict=False).feed("A⚠️B")

    assert scr.buffer[0][0].data == "A"
    assert scr.buffer[0][1].data == "⚠️", scr.buffer[0][1].data
    assert scr.buffer[0][2].data == "", "the cluster must claim its second column"
    assert scr.buffer[0][3].data == "B", "the next glyph goes where the child put it"
    assert cell_len(scr.buffer[0][1].data) == 2

    # And the row the renderer hands Textual is exactly the pane's width.
    class _Size:
        width = COLS
        height = ROWS

    class _Pane(saikai_terminal.ClaudeTerminal):
        size = _Size()
        has_focus = False

    ct = _Pane.__new__(_Pane)
    ct._lock = threading.Lock()
    ct._screen = scr
    ct._scroll = 0
    ct._frozen = False
    ct._frozen_buf = None
    ct._sel_anchor = None
    ct._sel_head = None
    ct.is_dead = False
    ct._spawn_error = None
    ct._frame = None
    assert ct.render_line(0).cell_length == COLS, ct.render_line(0).cell_length

    # A combining mark still merges WITHOUT claiming a column (it renders as one).
    scr2 = saikai_terminal._HistoryScreenBase(COLS, ROWS, history=10, ratio=0.5)
    pyte.Stream(scr2, strict=False).feed("éX")      # e + COMBINING ACUTE
    assert cell_len(scr2.buffer[0][0].data) == 1, scr2.buffer[0][0].data
    assert scr2.buffer[0][1].data == "X", scr2.buffer[0][1].data

def test_a_cell_never_renders_wider_than_the_columns_it_owns():
    """Claiming the second column when the cluster is BUILT is not durable: the
    child sends thousands of ECH/EL, and pyte erases per cell, so an erase can blank
    the stub and leave the 2-column cluster behind. MEASURED on a real capture: a row
    whose stub had been erased handed Textual a 98-cell Strip for a 97-cell pane.
    render_line must enforce the invariant where the columns are known."""
    import threading
    import pyte

    COLS, ROWS = 20, 3
    scr = saikai_terminal._HistoryScreenBase(COLS, ROWS, history=10, ratio=0.5)
    pyte.Stream(scr, strict=False).feed("A⚠️B")
    assert scr.buffer[0][2].data == "", "precondition: the cluster claimed x=2"

    class _Size:
        width = COLS
        height = ROWS

    class _Pane(saikai_terminal.ClaudeTerminal):
        size = _Size()
        has_focus = False

    def _pane(screen):
        ct = _Pane.__new__(_Pane)
        ct._lock = threading.Lock()
        ct._screen = screen
        ct._scroll = 0
        ct._frozen = False
        ct._frozen_buf = None
        ct._sel_anchor = None
        ct._sel_head = None
        ct.is_dead = False
        ct._spawn_error = None
        ct._frame = None
        return ct

    assert _pane(scr).render_line(0).cell_length == COLS

    # Now an erase blanks the stub but not the cluster (pyte erases per cell).
    scr.buffer[0][2] = scr.buffer[0][2]._replace(data=" ")
    strip = _pane(scr).render_line(0)
    assert strip.cell_length == COLS, strip.cell_length
    text = "".join(s.text for s in strip)
    # The cluster keeps its TWO columns and consumes the cell the erase wrote over,
    # so every later glyph stays in the column the child put it in. Shrinking the
    # cluster to its base codepoint instead would keep the row's width but move "B"
    # one column left — and since the child redraws with the stub back, the pane
    # alternated between shifted and unshifted (the scroll oscillation).
    assert text.startswith("A⚠️B"), repr(text[:6])

def test_overwriting_half_a_wide_glyph_erases_the_whole_glyph():
    """A real terminal that overwrites HALF of a double-width glyph erases the WHOLE
    glyph. pyte leaves the other half behind, so the model could hold
    [2-column glyph][real character] — a state no terminal can display. Rendering it
    forces a choice between eating that character and shifting the rest of the row, and
    the child redrawing the row flips between the two: TRACED on a real session as the
    pane alternating between "⚠ 注記" and "⚠注記" while scrolling. (#wide-glyph-halves)"""
    import pyte
    from rich.cells import cell_len

    COLS, ROWS = 12, 2

    def cells(scr, y=0):
        return [scr.buffer[y][x].data for x in range(COLS)]

    # Overwriting the STUB must blank the glyph that owned it.
    scr = saikai_terminal._HistoryScreenBase(COLS, ROWS, history=5, ratio=0.5)
    st = pyte.Stream(scr, strict=False)
    st.feed("A⚠️B")                      # A, cluster(2 cols), B
    assert cells(scr)[1] == "⚠️" and cells(scr)[2] == "", cells(scr)
    st.feed("\x1b[1;3Hx")                # write over the stub at column 3 (index 2)
    got = cells(scr)
    assert got[1] == " ", "the glyph losing its second column must be blanked: %s" % got
    assert got[2] == "x", got

    # Overwriting the FIRST column must blank the orphaned stub.
    scr2 = saikai_terminal._HistoryScreenBase(COLS, ROWS, history=5, ratio=0.5)
    st2 = pyte.Stream(scr2, strict=False)
    st2.feed("A⚠️B")
    st2.feed("\x1b[1;2Hy")               # write over the cluster itself (index 1)
    got2 = cells(scr2)
    assert got2[1] == "y", got2
    assert got2[2] == " ", "the orphaned stub must be blanked: %s" % got2

    # The invariant that follows: no cell renders 2 columns with a real neighbour.
    for scr_ in (scr, scr2):
        for y in range(scr_.lines):
            for x in range(scr_.columns - 1):
                d = scr_.buffer[y][x].data
                if d and cell_len(d) == 2:
                    assert scr_.buffer[y][x + 1].data == "", (y, x, d)


if __name__ == "__main__":
    test_regional_indicator_is_width_1()
    test_flag_occupies_two_cells_in_pyte_grid()
    test_matches_rich_cell_width()
    print("OK test_flag_width")
    test_emoji_presentation_cluster_occupies_the_columns_it_renders()
    print("PASS test_emoji_presentation_cluster_occupies_the_columns_it_renders")
    test_a_cell_never_renders_wider_than_the_columns_it_owns()
    print("PASS test_a_cell_never_renders_wider_than_the_columns_it_owns")
    test_overwriting_half_a_wide_glyph_erases_the_whole_glyph()
    print("PASS test_overwriting_half_a_wide_glyph_erases_the_whole_glyph")
