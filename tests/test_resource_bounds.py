"""Headless tests for saikai memory-bound fixes (resource code-review).

Run:  python tests/test_resource_bounds.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import saikai


def test_na_cache_is_bounded():
    """_needs_attention's cache must not grow without bound as distinct session
    ids accumulate over a long-lived picker (resource #8)."""
    cache = {}
    for i in range(5000):
        saikai._needs_attention({"id": f"s{i}", "mtime": 0}, cache)  # no jsonl_path -> False
    assert len(cache) <= 4097, f"cache grew unbounded: {len(cache)}"


def test_load_severity_bands():
    """warn is the precursor band (within 15 points of the gate); crit at/over."""
    assert saikai._load_severity(None, 85) == "ok"
    assert saikai._load_severity(50, 85) == "ok"
    assert saikai._load_severity(69.9, 85) == "ok"
    assert saikai._load_severity(70, 85) == "warn"     # 85 - 15
    assert saikai._load_severity(84, 85) == "warn"
    assert saikai._load_severity(85, 85) == "crit"
    assert saikai._load_severity(97, 95) == "crit"     # posix default gate 95


class _MS:
    """Minimal _MemStatus stand-in for the pure segment formatter."""
    def __init__(self, load, avail_mb):
        self.load = load
        self.avail_phys_mb = avail_mb


def test_live_ram_segment_estimate_and_severity_colour():
    # No memory status -> bare count, no RAM claims.
    assert saikai._live_ram_segment(3, "", None, 2, 600, 85) == "Live: 3"
    # Healthy: green load, green fit, saikai's estimated share shown (8*600/1024).
    s = saikai._live_ram_segment(8, "", _MS(60, 4096), 3, 600, 85)
    assert "Live: 8~4.7G" in s, s
    assert "[green]60% RAM[/green]" in s, s
    assert "[green]~3 fit[/green]" in s and "4.0G free" in s, s
    assert "⚠" not in s, "no warning sign while healthy"
    # Precursor (warn band): yellow + warning sign, BEFORE the gate trips.
    s2 = saikai._live_ram_segment(8, "", _MS(75, 2048), 1, 600, 85)
    assert "[yellow]" in s2 and "⚠" in s2 and "75% RAM" in s2, s2
    # Crit: red load + red ~0 fit.
    s3 = saikai._live_ram_segment(8, "", _MS(90, 512), 0, 600, 85)
    assert "[red]" in s3 and "90% RAM" in s3 and "[red]~0 fit[/red]" in s3, s3


def test_ctx_tokens_reads_last_usage_block(tmp_path=None):
    import json, tempfile, os
    d = tempfile.mkdtemp(prefix="saikai-ctx-")
    p = os.path.join(d, "s.jsonl")
    recs = [
        {"type": "user", "message": {"role": "user", "content": "hi"}},
        {"type": "assistant", "message": {"model": "claude-opus-4-8",
            "usage": {"input_tokens": 100, "cache_read_input_tokens": 5000,
                      "cache_creation_input_tokens": 200, "output_tokens": 50}}},
        {"type": "assistant", "message": {"model": "claude-opus-4-8",
            "usage": {"input_tokens": 131, "cache_read_input_tokens": 715734,
                      "cache_creation_input_tokens": 4017, "output_tokens": 4229}}},
    ]
    with open(p, "w", encoding="utf-8") as f:
        f.write("\n".join(json.dumps(r) for r in recs) + "\n")
    # last usage block: 131 + 715734 + 4017
    assert saikai._ctx_tokens_from_jsonl(p) == 719882
    # no usage anywhere -> None
    p2 = os.path.join(d, "n.jsonl")
    with open(p2, "w", encoding="utf-8") as f:
        f.write(json.dumps({"type": "user", "message": {"content": "x"}}) + "\n")
    assert saikai._ctx_tokens_from_jsonl(p2) is None
    # missing file -> None (never raises)
    assert saikai._ctx_tokens_from_jsonl(os.path.join(d, "nope.jsonl")) is None


if __name__ == "__main__":
    test_na_cache_is_bounded()
    print("PASS test_na_cache_is_bounded")
    test_load_severity_bands()
    print("PASS test_load_severity_bands")
    test_live_ram_segment_estimate_and_severity_colour()
    print("PASS test_live_ram_segment_estimate_and_severity_colour")
    test_ctx_tokens_reads_last_usage_block()
    print("PASS test_ctx_tokens_reads_last_usage_block")
