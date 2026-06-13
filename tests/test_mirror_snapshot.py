import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import saikai_mirror as m


def test_snapshot_reproduces_fed_text_and_color():
    hub = m.MirrorHub(token="t", cols=20, rows=3)
    # Feed plain text + a red "HI" via SGR 31, into the server-side pyte mirror.
    hub._feed("hello")
    hub._feed("\x1b[31mHI\x1b[0m")
    frame = hub._snapshot()
    # Full repaint clears + homes the cursor, contains the visible text and a
    # red SGR for the colored cells.
    assert frame.startswith("\x1b[2J\x1b[H")
    assert "hello" in frame
    assert "HI" in frame
    assert "\x1b[31m" in frame   # red foreground re-emitted


def test_snapshot_skips_wide_char_continuation():
    """A CJK wide char spans 2 columns; pyte stores an empty continuation cell.
    The snapshot must NOT emit a space for it, or every following column on a
    Japanese line shifts right (the 'partially garbled' layout)."""
    import re
    hub = m.MirrorHub(token="t", cols=12, rows=1)
    hub._feed("あいうA")
    plain = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", hub._snapshot())
    assert plain.startswith("あいうA")   # adjacent — no per-wide-char shift


if __name__ == "__main__":
    test_snapshot_reproduces_fed_text_and_color()
    test_snapshot_skips_wide_char_continuation()
    print("OK test_mirror_snapshot")
