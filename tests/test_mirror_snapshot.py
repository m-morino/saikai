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


if __name__ == "__main__":
    test_snapshot_reproduces_fed_text_and_color()
    print("OK test_mirror_snapshot")
