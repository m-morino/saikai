"""Grapheme-width regressions for the pyte/Rich presentation boundary."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pyte

_STOCK_PYTE_WCWIDTH = pyte.screens.wcwidth

import saikai_terminal as rt
from rich.cells import cell_len


_SAMPLES = (
    "e\u0301",                    # decomposed combining mark
    "\u2764\ufe0e",              # heart + VS15 (text presentation)
    "\u2764\ufe0f",              # heart + VS16
    "1\ufe0f\u20e3",             # keycap
    "\U0001f468\u200d\U0001f469\u200d\U0001f467\u200d\U0001f466",
    "\U0001f1ef\U0001f1f5",      # regional-indicator flag
    "\U0001f44d\U0001f3fd",      # emoji modifier
    "\u0915\u094d\u0924\u094d\u092f",  # width-3 Indic conjunct
)


def _screen(columns=20, lines=3):
    screen = rt._HistoryScreenBase(columns, lines, history=20)
    return screen, pyte.Stream(screen)


def _row_text(screen, row=0):
    return "".join(
        screen.buffer[row][x].data
        for x in range(screen.columns)
        if screen.buffer[row][x].data != ""
    )


def _assert_line_cluster_invariants(line, columns):
    """Every wide leader owns its stubs; no continuation cell is orphaned."""
    column = 0
    while column < columns:
        data = line[column].data
        assert data != "", ("orphan stub", column,
                            [line[x].data for x in range(columns)])
        width = max(1, cell_len(data))
        assert column + width <= columns, (
            "clipped leader", column, repr(data), width,
            [line[x].data for x in range(columns)],
        )
        for stub in range(column + 1, column + width):
            assert line[stub].data == "", (
                "missing stub", column, stub, repr(data),
                [line[x].data for x in range(columns)],
            )
        column += width


def _partitions(text):
    """Whole, every two-way split, and one-codepoint-at-a-time delivery."""
    yield (text,)
    for split_at in range(1, len(text)):
        yield (text[:split_at], text[split_at:])
    yield tuple(text)


def test_import_does_not_monkeypatch_process_global_pyte_wcwidth():
    assert pyte.screens.wcwidth is _STOCK_PYTE_WCWIDTH


def test_complete_graphemes_match_rich_width_at_every_read_boundary():
    for grapheme in _SAMPLES:
        for parts in _partitions(grapheme):
            screen, stream = _screen()
            stream.feed("A")
            for part in parts:
                stream.feed(part)
            stream.feed("B")

            expected_x = 1 + cell_len(grapheme) + 1
            assert screen.cursor.x == expected_x, (repr(grapheme), parts, screen.cursor.x)
            assert screen.buffer[0][1].data == grapheme, (
                repr(grapheme), parts, repr(screen.buffer[0][1].data)
            )
            for stub in range(2, 1 + cell_len(grapheme)):
                assert screen.buffer[0][stub].data == "", (
                    repr(grapheme), parts, stub, repr(screen.buffer[0][stub].data)
                )
            assert screen.buffer[0][1 + cell_len(grapheme)].data == "B"


def test_final_printable_is_visible_immediately_without_a_timer():
    screen, stream = _screen()
    stream.feed("A")
    assert screen.buffer[0][0].data == "A"
    assert screen.cursor.x == 1


def test_control_boundary_prevents_retrospective_extension():
    screen, stream = _screen()
    stream.feed("\u2764")
    stream.feed("\x1b[2G")  # explicit cursor operation, even though it lands adjacent
    stream.feed("\ufe0f")
    assert screen.buffer[0][0].data == "\u2764"
    assert "\ufe0f" not in _row_text(screen)


def test_agent_terminal_preserves_read_extensions_but_not_stripped_controls():
    terminal = rt.AgentTerminal(
        ["agent"], status_classifier=lambda _text, _title: "idle")
    terminal._screen, terminal._stream = _screen()
    terminal._marshal = lambda callback: callback()
    terminal._update_status = lambda _status: None

    terminal._consume("\u2764")
    terminal._consume("\ufe0f")
    assert terminal._screen.buffer[0][0].data == "\u2764\ufe0f"

    terminal._consume("\r\u2764")
    terminal._consume("\x1b[?u")  # consumed Kitty query: still an EGC boundary
    terminal._consume("\ufe0f")
    assert terminal._screen.buffer[0][0].data == "\u2764"

    mirrored = []
    terminal._mirror_tee = mirrored.append
    literal = "X\ufdd0Y"
    terminal._consume("\r" + literal)
    assert _row_text(terminal._screen).startswith(literal)
    assert mirrored[-1] == "\r" + literal


def test_retrospective_widening_in_insert_mode_inserts_only_width_delta():
    screen, stream = _screen(columns=8, lines=2)
    stream.feed("XYZ\x1b[1G\x1b[4h")
    stream.feed("\u2764")
    stream.feed("\ufe0f")

    assert [screen.buffer[0][x].data for x in range(5)] == [
        "\u2764\ufe0f", "", "X", "Y", "Z"]
    assert screen.cursor.x == 2


def test_insert_mode_does_not_leave_a_wide_leader_without_its_stub():
    screen, stream = _screen(columns=5, lines=2)
    stream.feed("ABC\u754c")
    stream.feed("\x1b[1G\x1b[4hX")

    row = [screen.buffer[0][x].data for x in range(5)]
    assert row == ["X", "A", "B", "C", " "], row


def test_ich_normalizes_a_clipped_four_cell_grapheme():
    """ICH can clip more than one stub; remove the whole incomplete EGC."""
    grapheme = "\u1100\u1100"
    assert cell_len(grapheme) == 4
    screen, stream = _screen(columns=5, lines=2)
    stream.feed("A" + grapheme)
    stream.feed("\x1b[1;1H\x1b[@")

    row = [screen.buffer[0][x].data for x in range(screen.columns)]
    assert grapheme not in row, row
    _assert_line_cluster_invariants(screen.buffer[0], screen.columns)


def test_widening_cluster_at_right_margin_reflows_before_the_cluster():
    for grapheme in _SAMPLES[1:]:
        if cell_len(grapheme) != 2:
            continue
        screen, stream = _screen(columns=4, lines=3)
        stream.feed("ABC")
        for codepoint in grapheme:
            stream.feed(codepoint)
        stream.feed("Z")

        assert _row_text(screen, 0).rstrip() == "ABC", repr(grapheme)
        assert screen.buffer[1][0].data == grapheme, repr(grapheme)
        assert screen.buffer[1][1].data == "", repr(grapheme)
        assert screen.buffer[1][2].data == "Z", repr(grapheme)
        assert (screen.cursor.y, screen.cursor.x) == (1, 3)


def test_right_margin_split_preserves_displaced_cell_like_whole_cluster():
    def render(parts, *, insert_mode):
        screen, stream = _screen(columns=4, lines=3)
        stream.feed("ABCD\x1b[4G" + ("\x1b[4h" if insert_mode else ""))
        for part in parts:
            stream.feed(part)
        return (
            [[screen.buffer[y][x].data for x in range(screen.columns)]
             for y in range(screen.lines)],
            (screen.cursor.y, screen.cursor.x),
        )

    for grapheme in ("\u2764\ufe0f", "\u2639\u200d\u2640\ufe0f"):
        for insert_mode in (False, True):
            whole = render((grapheme,), insert_mode=insert_mode)
            for parts in _partitions(grapheme):
                split = render(parts, insert_mode=insert_mode)
                assert split == whole, (repr(grapheme), parts, insert_mode)
            assert whole[0][0] == ["A", "B", "C", "D"]
            assert whole[0][1][:2] == [grapheme, ""]


def test_wide_cluster_at_right_margin_is_ignored_without_autowrap():
    def render(parts, *, insert_mode):
        screen, stream = _screen(columns=4, lines=2)
        stream.feed("ABCD\x1b[4G\x1b[?7l"
                    + ("\x1b[4h" if insert_mode else ""))
        for part in parts:
            stream.feed(part)
        return (
            [[screen.buffer[y][x].data for x in range(screen.columns)]
             for y in range(screen.lines)],
            (screen.cursor.y, screen.cursor.x),
        )

    for grapheme in ("\u754c", "\u2764\ufe0f",
                     "\u2639\u200d\u2640\ufe0f"):
        for insert_mode in (False, True):
            expected = render((grapheme,), insert_mode=insert_mode)
            assert expected[0][0] == ["A", "B", "C", "D"]
            assert expected[1] == (0, 3)
            for parts in _partitions(grapheme):
                assert render(parts, insert_mode=insert_mode) == expected, (
                    repr(grapheme), parts, insert_mode)


def test_cluster_wider_than_the_screen_is_dropped_without_orphans():
    """Dropping an overwide EGC is independent of PTY read boundaries."""
    grapheme = "\u1100" * 5
    assert cell_len(grapheme) == 10

    expected = None
    for parts in _partitions(grapheme):
        screen, stream = _screen(columns=8, lines=2)
        for part in parts:
            stream.feed(part)
        stream.feed("Z")
        observed = (
            [[screen.buffer[y][x].data for x in range(screen.columns)]
             for y in range(screen.lines)],
            (screen.cursor.y, screen.cursor.x),
        )
        if expected is None:
            expected = observed
        assert observed == expected, parts

    assert expected[0][0][0] == "Z"
    assert expected[1] == (0, 1)
    for row in range(screen.lines):
        _assert_line_cluster_invariants(screen.buffer[row], screen.columns)


def test_split_width_three_cluster_preserves_every_displaced_cell():
    """Each retrospective width increase retains the pre-prefix row image."""
    grapheme = "\u0915\u094d\u0924\u094d\u092f"
    assert cell_len(grapheme) == 3

    def render(parts, *, autowrap):
        screen, stream = _screen(columns=3, lines=3)
        stream.feed("QQQ\rA")
        if not autowrap:
            stream.feed("\x1b[?7l")
        for part in parts:
            stream.feed(part)
        stream.feed("Z")
        return (
            [[screen.buffer[y][x].data for x in range(screen.columns)]
             for y in range(screen.lines)],
            (screen.cursor.y, screen.cursor.x),
        )

    for autowrap in (False, True):
        expected = render((grapheme,), autowrap=autowrap)
        for parts in _partitions(grapheme):
            assert render(parts, autowrap=autowrap) == expected, (
                parts, autowrap)
        assert expected[0][0] == (
            ["A", "Q", "Q"] if autowrap else ["A", "Z", "Q"])


def test_zero_history_alternate_screen_wrap_never_drops_output():
    """A history=0 ALT buffer still wraps at the bottom without indexing history."""
    screen = rt._HistoryScreenBase(4, 2, history=0)
    stream = pyte.Stream(screen)

    stream.feed("AAAA\r\nBBBB")
    stream.feed("Z")

    assert screen.buffer[1][0].data == "Z"
    assert (screen.cursor.y, screen.cursor.x) == (1, 1)


def test_horizontal_csi_edits_clear_an_intersected_wide_cluster_atomically():
    """Match xterm: a cell edit touching either half first clears the whole EGC."""
    cases = (
        ("ECH leader", "\x1b[1;2H\x1b[X",
         ["A", " ", " ", "B", " "]),
        ("ECH stub", "\x1b[1;3H\x1b[X",
         ["A", " ", " ", "B", " "]),
        ("DCH leader", "\x1b[1;2H\x1b[P",
         ["A", " ", "B", " ", " "]),
        ("DCH stub", "\x1b[1;3H\x1b[P",
         ["A", " ", "B", " ", " "]),
        ("ICH stub", "\x1b[1;3H\x1b[@",
         ["A", " ", " ", " ", "B"]),
        ("EL stub", "\x1b[1;3H\x1b[K",
         ["A", " ", " ", " ", " "]),
    )
    for name, operation, expected in cases:
        screen, stream = _screen(columns=8, lines=2)
        stream.feed("A\u754cB" + operation)
        row = [screen.buffer[0][x].data for x in range(screen.columns)]
        assert row[:5] == expected, (name, row)
        _assert_line_cluster_invariants(screen.buffer[0], screen.columns)
        assert cell_len(_row_text(screen)) == screen.columns, (name, row)


def test_ed_protects_wide_clusters_on_current_and_erased_rows():
    screen, stream = _screen(columns=8, lines=2)
    stream.feed("A\u754cB\r\nC\u754cD")
    stream.feed("\x1b[1;3H\x1b[J")  # ED 0 starts in the first glyph's stub.

    assert [screen.buffer[0][x].data for x in range(4)] == [
        "A", " ", " ", " "]
    assert [screen.buffer[1][x].data for x in range(4)] == [
        " ", " ", " ", " "]
    for row in range(screen.lines):
        _assert_line_cluster_invariants(screen.buffer[row], screen.columns)


def test_resize_removes_clipped_wide_clusters_from_visible_and_history_rows():
    visible, visible_stream = _screen(columns=5, lines=2)
    visible_stream.feed("ABC\u754c")
    visible.resize(2, 4)
    assert [visible.buffer[0][x].data for x in range(4)] == [
        "A", "B", "C", " "]
    _assert_line_cluster_invariants(visible.buffer[0], visible.columns)
    assert cell_len(_row_text(visible)) == visible.columns

    top, top_stream = _screen(columns=5, lines=2)
    top_stream.feed("ABC\u754c\r\n11111\r\n22222")
    assert len(top.history.top) == 1
    top.resize(2, 4)
    top_line = top.history.top[0]
    assert [top_line[x].data for x in range(4)] == ["A", "B", "C", " "]
    _assert_line_cluster_invariants(top_line, top.columns)

    bottom, bottom_stream = _screen(columns=5, lines=2)
    bottom_stream.feed("11111\r\n22222\r\nABC\u754c")
    bottom.prev_page()
    assert len(bottom.history.bottom) == 1
    bottom_line = bottom.history.bottom[0]
    bottom.resize(2, 4)
    assert [bottom_line[x].data for x in range(4)] == [
        "A", "B", "C", " "]
    _assert_line_cluster_invariants(bottom_line, bottom.columns)

    # Explicit cells clipped by a shrink must not reappear after a later grow.
    top.resize(2, 5)
    bottom.resize(2, 5)
    assert top_line[4].data == " "
    assert bottom_line[4].data == " "


def test_resize_removes_clipped_four_cell_graphemes_from_history():
    """History trimming must validate every leader, not only the final cell."""
    grapheme = "\u1100\u1100"
    assert cell_len(grapheme) == 4
    screen, stream = _screen(columns=6, lines=2)
    stream.feed("A" + grapheme + "B\r\n111111\r\n222222")
    assert len(screen.history.top) == 1
    history_line = screen.history.top[0]

    screen.resize(2, 4)

    row = [history_line[x].data for x in range(screen.columns)]
    assert grapheme not in row, row
    _assert_line_cluster_invariants(history_line, screen.columns)


def _presentation_state(screen):
    return (
        screen.columns,
        screen.lines,
        tuple(
            tuple(screen.buffer[y][x] for x in range(screen.columns))
            for y in range(screen.lines)
        ),
        (screen.cursor.y, screen.cursor.x, screen.cursor.attrs),
        screen.margins,
    )


def test_deccolm_and_decscnm_are_side_effect_free_like_embedded_xterm():
    """A child cannot resize the fixed pane or destructively rewrite SGR state."""
    screen, stream = _screen(columns=10, lines=3)
    stream.feed(
        "\x1b[7mR\x1b[27mN"
        "\x1b[2;3r\x1b[2;5H"
    )
    expected = _presentation_state(screen)

    for sequence in ("\x1b[?3h", "\x1b[?3l", "\x1b[?5h", "\x1b[?5l"):
        stream.feed(sequence)
        assert _presentation_state(screen) == expected, repr(sequence)

    assert screen.buffer[0][0].reverse is True
    assert screen.buffer[0][1].reverse is False
    assert pyte.modes.DECCOLM not in screen.mode
    assert pyte.modes.DECSCNM not in screen.mode


def test_ignored_private_modes_do_not_swallow_combined_supported_modes():
    screen, stream = _screen(columns=10, lines=3)
    stream.feed("ABC\x1b[2;3r\x1b[2;5H")
    expected = _presentation_state(screen)

    stream.feed("\x1b[?3;5;7l")
    assert pyte.modes.DECAWM not in screen.mode
    assert pyte.modes.DECCOLM not in screen.mode
    assert pyte.modes.DECSCNM not in screen.mode
    assert _presentation_state(screen) == expected

    stream.feed("\x1b[?3;5;7h")
    assert pyte.modes.DECAWM in screen.mode
    assert _presentation_state(screen) == expected


def test_ignored_private_modes_cannot_mutate_either_real_buffer():
    terminal = rt.AgentTerminal(
        ["agent"], status_classifier=lambda _text, _title: "idle")
    terminal._create_screen_pair(3, 10)
    terminal._marshal = lambda callback: callback()
    terminal._update_status = lambda _status: None

    terminal._consume("\x1b[7mM\x1b[27mAIN\x1b[2;3r\x1b[2;5H")
    main_expected = _presentation_state(terminal._main_screen)
    terminal._consume("\x1b[?1049hALT\x1b[2;3r\x1b[2;5H")
    alt_expected = _presentation_state(terminal._alt_screen)

    terminal._consume("\x1b[?3;5h\x1b[?3;5l")
    assert _presentation_state(terminal._alt_screen) == alt_expected
    terminal._consume("\x1b[?1049l")
    assert _presentation_state(terminal._main_screen) == main_expected
    assert terminal._main_screen.columns == terminal._alt_screen.columns == 10


if __name__ == "__main__":
    test_import_does_not_monkeypatch_process_global_pyte_wcwidth()
    test_complete_graphemes_match_rich_width_at_every_read_boundary()
    test_final_printable_is_visible_immediately_without_a_timer()
    test_control_boundary_prevents_retrospective_extension()
    test_agent_terminal_preserves_read_extensions_but_not_stripped_controls()
    test_retrospective_widening_in_insert_mode_inserts_only_width_delta()
    test_insert_mode_does_not_leave_a_wide_leader_without_its_stub()
    test_ich_normalizes_a_clipped_four_cell_grapheme()
    test_widening_cluster_at_right_margin_reflows_before_the_cluster()
    test_right_margin_split_preserves_displaced_cell_like_whole_cluster()
    test_wide_cluster_at_right_margin_is_ignored_without_autowrap()
    test_cluster_wider_than_the_screen_is_dropped_without_orphans()
    test_split_width_three_cluster_preserves_every_displaced_cell()
    test_zero_history_alternate_screen_wrap_never_drops_output()
    test_horizontal_csi_edits_clear_an_intersected_wide_cluster_atomically()
    test_ed_protects_wide_clusters_on_current_and_erased_rows()
    test_resize_removes_clipped_wide_clusters_from_visible_and_history_rows()
    test_resize_removes_clipped_four_cell_graphemes_from_history()
    test_deccolm_and_decscnm_are_side_effect_free_like_embedded_xterm()
    test_ignored_private_modes_do_not_swallow_combined_supported_modes()
    test_ignored_private_modes_cannot_mutate_either_real_buffer()
    print("OK test_flag_width")
