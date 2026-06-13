"""Contract tests for the isolated fictional demo workspace."""
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from demo_fixture import build_demo_fixture


def test_fixture_is_fictional_reproducible_and_contains_a_runnable_repo():
    root = Path(tempfile.mkdtemp())
    fixture = build_demo_fixture(root)
    assert fixture.home == root / "home"
    assert fixture.hero_repo == root / "repos" / "webapp"
    assert (fixture.hero_repo / "tests" / "test_auth.py").is_file()
    assert (fixture.hero_repo / ".git").is_dir()
    assert len(fixture.sessions) >= 8

    text = "\n".join(
        p.read_text(encoding="utf-8", errors="ignore")
        for p in root.rglob("*")
        if p.is_file() and ".git" not in p.parts
    )
    assert "masay" not in text.lower()
    assert "C:\\Users" not in text
    assert "/home/demo/work/webapp" in text
    assert "/home/alex" not in text

    result = subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "-s", "tests"],
        cwd=fixture.hero_repo,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    assert "test_refreshes_at_expiry_boundary" in result.stderr


if __name__ == "__main__":
    test_fixture_is_fictional_reproducible_and_contains_a_runnable_repo()
    print("PASS test_fixture_is_fictional_reproducible_and_contains_a_runnable_repo")
