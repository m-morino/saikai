import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import saikai_mirror as m


class _FakeBaseDriver:
    """Stand-in for WindowsDriver/LinuxDriver: records what reaches super().write."""
    def __init__(self, *a, **k):
        self.written = []

    def write(self, data):
        self.written.append(data)


def test_mirror_driver_tees_then_delegates():
    sent = []
    hub = type("H", (), {"broadcast": lambda self, d: sent.append(d)})()
    Drv = m.make_mirror_driver(_FakeBaseDriver, hub)
    d = Drv()                     # base __init__ takes *a, **k
    d.write("\x1b[31mX")
    # Tee'd to the hub AND delegated to the real console writer, in that order.
    assert sent == ["\x1b[31mX"]
    assert d.written == ["\x1b[31mX"]


def test_mirror_driver_never_lets_broadcast_break_console():
    """If broadcast raises, the console write MUST still happen (mirror is best
    effort and must never degrade the local UI)."""
    class _Boom:
        def broadcast(self, d):
            raise RuntimeError("drain exploded")
    Drv = m.make_mirror_driver(_FakeBaseDriver, _Boom())
    d = Drv()
    d.write("data")               # must not raise
    assert d.written == ["data"]


if __name__ == "__main__":
    test_mirror_driver_tees_then_delegates()
    test_mirror_driver_never_lets_broadcast_break_console()
    print("OK test_mirror_driver")
