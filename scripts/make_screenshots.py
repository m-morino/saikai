"""Regenerate the README screenshots (docs/assets/*.svg) headlessly.

Builds a throwaway $HOME with fictional demo sessions, points saikai at it,
drives the real PickerApp under Textual's run_test harness, and saves SVG
screenshots. The split-live shot runs scripts/mock_claude.py through the real
PTY/pyte pipeline instead of a real `claude` (nothing private can leak).

Usage:  uv run scripts/make_screenshots.py
"""
import asyncio
import os
import sys
import tempfile
from pathlib import Path

from demo_fixture import build_demo_fixture

REPO = Path(__file__).resolve().parent.parent
ASSETS = REPO / "docs" / "assets"
MOCK_CLAUDE = Path(__file__).resolve().parent / "mock_claude.py"

# ---- 1. fake home, BEFORE importing saikai (it derives paths at import) ----
fixture = build_demo_fixture(Path(tempfile.mkdtemp(prefix="saikai-demo-")))
demo_home = fixture.home
for var in ("USERPROFILE", "HOME", "APPDATA", "LOCALAPPDATA", "XDG_CONFIG_HOME"):
    os.environ[var] = str(demo_home)
os.environ.pop("SAIKAI_CONFIG", None)
os.environ["SAIKAI_SUMMARIZE_ENABLED"] = "0"
os.environ["SAIKAI_AUTO_REFRESH"] = "0"

# ---- 2. import saikai against the fake home, patch, and drive --------------
sys.path.insert(0, str(REPO))
import saikai  # noqa: E402

saikai._build_resume_invocation = lambda full_id, sessions: (
    [sys.executable, str(MOCK_CLAUDE)], str(fixture.hero_repo), dict(os.environ))

from textual.app import App  # noqa: E402

SIZE = (128, 35)


def fake_run(self, *a, **kw):
    async def go():
        async with self.run_test(size=SIZE) as pilot:
            await pilot.pause(0.8)
            ASSETS.mkdir(parents=True, exist_ok=True)
            # Show the browser off properly: Date grouping (the default since
            # 2026-06: section headers appear without any keypress), a ★
            # favorite, and the Sort/Group state visible in the status bar.
            await pilot.press("f6")             # ★ the selected session
            await pilot.press("down")
            await pilot.pause(0.4)
            self.save_screenshot(filename="saikai-browse.svg", path=str(ASSETS))
            await pilot.press("enter")          # open a live pane (mock claude)
            for _ in range(40):                 # wait for the PTY to paint
                await pilot.pause(0.1)
            self.save_screenshot(filename="saikai-split-live.svg", path=str(ASSETS))
    asyncio.run(go())


App.run = fake_run
sys.argv = ["saikai", "--all"]
saikai.main()

# Leak guard: a published screenshot must not contain the real user's name,
# home dir, or the throwaway temp path. Fail loudly rather than ship it.
suspicious = {fixture.root.name.lower(), "temp", "appdata"}
try:
    suspicious.add(os.getlogin().lower())
except OSError:
    pass
for svg in ("saikai-browse.svg", "saikai-split-live.svg"):
    text = (ASSETS / svg).read_text(encoding="utf-8").lower()
    hits = sorted(w for w in suspicious if len(w) >= 4 and w in text)
    if hits:
        raise SystemExit(f"LEAK in {svg}: {hits} — screenshot NOT safe to publish")
    print(f"saved (leak-checked): {ASSETS / svg}")
