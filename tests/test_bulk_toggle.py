"""Unit tests for _bulk_toggle_in_set — the read-once/write-once converging
bulk-toggle behind Space-Space bulk favorite/hide.

Pure file-system tests (no Textual, no PickerApp): _bulk_toggle_in_set takes an
explicit path, so each test writes to its own temp file. Run with:
    uv run python tests/test_bulk_toggle.py
"""
import os, sys, json, tempfile
from pathlib import Path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import saikai


def _tmp():
    fd, name = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    p = Path(name)
    p.unlink()                      # start absent; let the function create it
    return p


def _read(p):
    return set(json.loads(p.read_text(encoding="utf-8"))) if p.exists() else set()


def test_all_off_turns_all_on():
    """No marked sid is present → converging rule adds ALL, returns True."""
    p = _tmp()
    try:
        assert saikai._bulk_toggle_in_set(p, ["a", "b", "c"]) is True
        assert _read(p) == {"a", "b", "c"}
    finally:
        p.unlink(missing_ok=True)


def test_all_on_turns_all_off():
    """Every marked sid already present → removes ALL, returns False."""
    p = _tmp()
    try:
        saikai._save_set(p, {"a", "b", "c"})
        assert saikai._bulk_toggle_in_set(p, ["a", "b", "c"]) is False
        assert _read(p) == set()
    finally:
        p.unlink(missing_ok=True)


def test_mixed_any_off_turns_all_on():
    """A MIXED selection (some on, some off) must converge to all-on — never
    flip each row (which would un-favorite the ones already on)."""
    p = _tmp()
    try:
        saikai._save_set(p, {"b"})              # b on, a/c off
        assert saikai._bulk_toggle_in_set(p, ["a", "b", "c"]) is True
        assert _read(p) == {"a", "b", "c"}      # b stayed on, not flipped off
    finally:
        p.unlink(missing_ok=True)


def test_force_true_and_false():
    p = _tmp()
    try:
        assert saikai._bulk_toggle_in_set(p, ["a", "b"], force=True) is True
        assert _read(p) == {"a", "b"}
        assert saikai._bulk_toggle_in_set(p, ["a", "b"], force=False) is False
        assert _read(p) == set()
    finally:
        p.unlink(missing_ok=True)


def test_preserves_unrelated_entries():
    """A bulk toggle must NEVER erase sids outside the batch."""
    p = _tmp()
    try:
        saikai._save_set(p, {"keep1", "keep2"})
        saikai._bulk_toggle_in_set(p, ["x", "y"])           # add x,y
        assert _read(p) == {"keep1", "keep2", "x", "y"}
        saikai._bulk_toggle_in_set(p, ["x", "y"])           # remove x,y
        assert _read(p) == {"keep1", "keep2"}               # keeps survived both
    finally:
        p.unlink(missing_ok=True)


def test_empty_sids_is_noop():
    """Empty selection writes nothing and returns False; an existing file is
    left byte-for-byte intact (no spurious rewrite)."""
    p = _tmp()
    try:
        saikai._save_set(p, {"a"})
        before = p.read_text(encoding="utf-8")
        assert saikai._bulk_toggle_in_set(p, []) is False
        assert p.read_text(encoding="utf-8") == before
    finally:
        p.unlink(missing_ok=True)


def test_falsy_sids_filtered_out():
    """None / "" in the batch are dropped, not written as members."""
    p = _tmp()
    try:
        assert saikai._bulk_toggle_in_set(p, ["a", "", None, "b"]) is True
        assert _read(p) == {"a", "b"}
    finally:
        p.unlink(missing_ok=True)


def test_unreadable_populated_file_raises_and_is_not_clobbered():
    """The anti-erase guard: an EXISTING but unparseable file must raise rather
    than be overwritten with a truncated set (the same guarantee _toggle_in_set
    gives for the single-sid path)."""
    p = _tmp()
    try:
        p.write_text("{ this is not valid json", encoding="utf-8")
        before = p.read_text(encoding="utf-8")
        raised = False
        try:
            saikai._bulk_toggle_in_set(p, ["a", "b"])
        except RuntimeError:
            raised = True
        assert raised, "expected RuntimeError on an unparseable populated file"
        assert p.read_text(encoding="utf-8") == before, "file must be untouched"
    finally:
        p.unlink(missing_ok=True)


if __name__ == "__main__":
    test_all_off_turns_all_on()
    test_all_on_turns_all_off()
    test_mixed_any_off_turns_all_on()
    test_force_true_and_false()
    test_preserves_unrelated_entries()
    test_empty_sids_is_noop()
    test_falsy_sids_filtered_out()
    test_unreadable_populated_file_raises_and_is_not_clobbered()
    print("OK test_bulk_toggle")
