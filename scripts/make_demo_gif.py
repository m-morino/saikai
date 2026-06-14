# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "textual>=0.50", "platformdirs>=3", "pyte>=0.8", "pillow>=10",
#   "pywinpty>=2; sys_platform == 'win32'",
#   "ptyprocess>=0.7; sys_platform != 'win32'",
# ]
# ///
"""Record the README demo GIF headlessly (no asciinema — works on Windows).

Pipeline: drive the real PickerApp with Textual's Pilot over the same
fictional demo data as make_screenshots.py, snap an SVG frame at each story
beat, render the SVGs to PNG with Edge/Chrome headless, and assemble the GIF
with Pillow (per-frame durations). The same leak guard as the screenshots
aborts if anything private would ship.

Usage:  uv run scripts/make_demo_gif.py
Output: docs/assets/saikai-demo-headless.gif
"""
import asyncio
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from demo_fixture import build_demo_fixture

REPO = Path(__file__).resolve().parent.parent
ASSETS = REPO / "docs" / "assets"
MOCK_CLAUDE = Path(__file__).resolve().parent / "mock_claude.py"
GIF_OUT = ASSETS / "saikai-demo-headless.gif"

# ---- 1. fake home, BEFORE importing saikai (it derives paths at import) ----
fixture = build_demo_fixture(Path(tempfile.mkdtemp(prefix="saikai-demo-")))
demo_home = fixture.home
for var in ("USERPROFILE", "HOME", "APPDATA", "LOCALAPPDATA", "XDG_CONFIG_HOME"):
    os.environ[var] = str(demo_home)
os.environ.pop("SAIKAI_CONFIG", None)
os.environ["SAIKAI_SUMMARIZE_ENABLED"] = "0"
os.environ["SAIKAI_AUTO_REFRESH"] = "0"

FRAMES_DIR = Path(tempfile.mkdtemp(prefix="saikai-demo-frames-"))

# ---- 2. drive the app, saving one SVG per story beat -----------------------
sys.path.insert(0, str(REPO))
import saikai  # noqa: E402

saikai._build_resume_invocation = lambda full_id, sessions: (
    [sys.executable, str(MOCK_CLAUDE)], str(fixture.hero_repo), dict(os.environ))

from textual.app import App  # noqa: E402

SIZE = (128, 35)
FRAMES: list[tuple[str, int]] = []   # (svg filename, duration ms)
_n = 0


def fake_run(self, *a, **kw):
    async def snap(pilot, ms: int, settle: float = 0.15):
        global _n
        await pilot.pause(settle)
        _n += 1
        name = f"frame-{_n:03d}.svg"
        self.save_screenshot(filename=name, path=str(FRAMES_DIR))
        FRAMES.append((name, ms))

    async def go():
        async with self.run_test(size=SIZE) as pilot:
            await pilot.pause(0.8)
            # Beat 1 — HOOK: open cold on the whole cross-project list. This is
            # the value shot — every Claude Code session, every repo, one screen
            # — so give it the longest hold of the loop. No search, no menu yet.
            await snap(pilot, 3200)
            # Beats 2-5 — FIND: search-as-you-type narrows to the remembered hit
            # across projects. Quick keystrokes, then a beat to read the result.
            for i, ch in enumerate("auth"):
                await pilot.press(ch)
                await snap(pilot, 280 if i < 3 else 1600)
            # Beats 6-8 — RESUME: Enter brings real Claude Code up live in a
            # split pane, continuing the session in its own cwd. Hold on the
            # live session, then the loop returns to the cross-project list.
            await pilot.press("enter")
            for _ in range(20):                  # let the PTY start painting
                await pilot.pause(0.1)
            await snap(pilot, 1300)              # the session streaming in
            for _ in range(16):                  # finish painting (~36 cycles total)
                await pilot.pause(0.1)
            await snap(pilot, 2600)              # hold on the fully-painted session
    asyncio.run(go())


App.run = fake_run
sys.argv = ["saikai", "--all"]
saikai.main()

# ---- 4. leak guard (same contract as make_screenshots.py) ------------------
suspicious = {fixture.root.name.lower(), "temp", "appdata"}
try:
    suspicious.add(os.getlogin().lower())
except OSError:
    pass
for name, _ in FRAMES:
    text = (FRAMES_DIR / name).read_text(encoding="utf-8").lower()
    hits = sorted(w for w in suspicious if len(w) >= 4 and w in text)
    if hits:
        raise SystemExit(f"LEAK in {name}: {hits} — demo NOT safe to publish")
print(f"captured {len(FRAMES)} frames (leak-checked): {FRAMES_DIR}")

# ---- 5. SVG -> PNG via Edge/Chrome headless ---------------------------------


def _find_browser() -> str:
    cands = [
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        "google-chrome", "chromium", "chromium-browser", "msedge",
    ]
    for c in cands:
        p = shutil.which(c) or (c if Path(c).is_file() else None)
        if p:
            return p
    raise SystemExit("no Edge/Chrome found for SVG->PNG rendering")


browser = _find_browser()
first_svg = (FRAMES_DIR / FRAMES[0][0]).read_text(encoding="utf-8")
m = re.search(r'<svg[^>]*\bwidth="([\d.]+)"[^>]*\bheight="([\d.]+)"', first_svg)
if not m:
    # Current Textual/Rich screenshots use a viewBox without width/height.
    m = re.search(r'<svg[^>]*\bviewBox="[\d.]+\s+[\d.]+\s+([\d.]+)\s+([\d.]+)"',
                  first_svg)
if not m:
    raise SystemExit("could not read SVG dimensions")
W, H = int(float(m.group(1))), int(float(m.group(2)))
print(f"frame size: {W}x{H}; rendering with {browser}")

for name, _ in FRAMES:
    svg = FRAMES_DIR / name
    html = svg.with_suffix(".html")
    # Inline the SVG into a zero-margin page so the screenshot is exactly the
    # frame — file:// + <img src=svg> would add body margins and rescaling.
    html.write_text("<!doctype html><html><head><meta charset='utf-8'>"
                    "<style>html,body{margin:0;padding:0}</style></head><body>"
                    + svg.read_text(encoding="utf-8") + "</body></html>",
                    encoding="utf-8")
    png = svg.with_suffix(".png")
    cmd = [browser, "--headless=new", "--disable-gpu",
           "--force-device-scale-factor=1", "--hide-scrollbars",
           f"--window-size={W},{H}", f"--screenshot={png}",
           html.as_uri()]
    # `--headless=new` occasionally stalls on a cold spawn (AV / proxy / first
    # paint). It uses an ephemeral profile, so a retry never lock-conflicts.
    for attempt in range(2):
        try:
            subprocess.run(cmd, check=True, capture_output=True, timeout=90)
            break
        except subprocess.TimeoutExpired:
            if attempt:
                raise
            print(f"  {name}: render stalled, retrying once")
    if not png.is_file():
        raise SystemExit(f"render failed for {name}")
print("rendered all frames to PNG")

# ---- 6. assemble the GIF with Pillow ----------------------------------------
from PIL import Image  # noqa: E402

imgs = []
for name, _ in FRAMES:
    im = Image.open((FRAMES_DIR / name).with_suffix(".png")).convert("RGB")
    # Terminal frames are flat colour — 128-colour palette keeps text crisp
    # and the file small.
    imgs.append(im.quantize(colors=128, dither=Image.Dither.NONE))
durations = [ms for _, ms in FRAMES]
ASSETS.mkdir(parents=True, exist_ok=True)
imgs[0].save(GIF_OUT, save_all=True, append_images=imgs[1:],
             duration=durations, loop=0, optimize=True)
size_kb = GIF_OUT.stat().st_size / 1024
print(f"saved: {GIF_OUT}  ({len(imgs)} frames, {size_kb:.0f} KB)")
