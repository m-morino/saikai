"""Headless tests for recap memory-bound fixes (resource code-review).

Run:  python tests/test_resource_bounds.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import recap


def test_na_cache_is_bounded():
    """_needs_attention's cache must not grow without bound as distinct session
    ids accumulate over a long-lived picker (resource #8)."""
    cache = {}
    for i in range(5000):
        recap._needs_attention({"id": f"s{i}", "mtime": 0}, cache)  # no jsonl_path -> False
    assert len(cache) <= 4097, f"cache grew unbounded: {len(cache)}"


if __name__ == "__main__":
    test_na_cache_is_bounded()
    print("PASS test_na_cache_is_bounded")
