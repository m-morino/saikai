import os, sys, threading
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import saikai_mirror as m


def test_broadcast_is_nonblocking_and_drops_oldest():
    """broadcast() runs on Textual's UI thread; it must NEVER block, even if the
    drain side is stalled. When the bounded ingest queue is full it drops the
    OLDEST item and enqueues the newest, returning immediately."""
    hub = m.MirrorHub(token="t", ingest_cap=4)
    # Do NOT start the drain thread: simulate a fully stalled consumer.
    for i in range(1000):
        start = threading.Event()
        done = threading.Event()

        def call():
            start.set()
            hub.broadcast(f"frame-{i}")
            done.set()

        th = threading.Thread(target=call)
        th.start()
        done_ok = done.wait(timeout=2.0)
        th.join(timeout=2.0)
        assert done_ok, f"broadcast() blocked on iteration {i} (cap full)"
    assert hub._ingest.qsize() <= 4
    drained = []
    while not hub._ingest.empty():
        drained.append(hub._ingest.get_nowait())
    assert drained == ["frame-996", "frame-997", "frame-998", "frame-999"]


if __name__ == "__main__":
    test_broadcast_is_nonblocking_and_drops_oldest()
    print("OK test_mirror_hub")
