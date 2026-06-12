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
Output: docs/assets/saikai-demo.gif
"""
import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
ASSETS = REPO / "docs" / "assets"
MOCK_CLAUDE = Path(__file__).resolve().parent / "mock_claude.py"
GIF_OUT = ASSETS / "saikai-demo.gif"

# ---- 1. fake home, BEFORE importing saikai (it derives paths at import) ----
demo_home = Path(tempfile.mkdtemp(prefix="saikai-demo-home-"))
for var in ("USERPROFILE", "HOME", "APPDATA", "LOCALAPPDATA", "XDG_CONFIG_HOME"):
    os.environ[var] = str(demo_home)
os.environ.pop("SAIKAI_CONFIG", None)
os.environ["SAIKAI_SUMMARIZE_ENABLED"] = "0"
os.environ["SAIKAI_AUTO_REFRESH"] = "0"

FRAMES_DIR = Path(tempfile.mkdtemp(prefix="saikai-demo-frames-"))

# ---- 2. fictional sessions (same set as make_screenshots.py) ---------------
now = datetime.now(timezone.utc)


def _enc(p: str) -> str:
    return re.sub(r"[:/\\.]", "-", p)


def demo_session(project: str, title: str, msgs: list[str], age_h: float,
                 branch: str = "main", turns_pad: int = 0) -> None:
    cwd = f"/home/alex/code/{project}"
    pdir = demo_home / ".claude" / "projects" / _enc(cwd)
    pdir.mkdir(parents=True, exist_ok=True)
    t0 = now - timedelta(hours=age_h)
    recs = [{"type": "ai-title", "aiTitle": title,
             "timestamp": t0.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
             "cwd": cwd, "gitBranch": branch}]
    for i, m in enumerate(msgs + ["looks good, thanks!"] * turns_pad):
        ts = (t0 + timedelta(minutes=3 * (i + 1))).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        recs.append({"type": "user", "timestamp": ts, "cwd": cwd,
                     "gitBranch": branch, "message": {"content": m}})
        recs.append({"type": "assistant", "timestamp": ts,
                     "message": {"content": [{"type": "text",
                                              "text": f"(demo reply {i + 1})"}]}})
    out = pdir / f"{uuid.uuid4()}.jsonl"
    out.write_text("\n".join(json.dumps(r) for r in recs) + "\n", encoding="utf-8")
    ts_epoch = (t0 + timedelta(minutes=3 * max(1, len(msgs)))).timestamp()
    os.utime(out, (ts_epoch, ts_epoch))


demo_session("webapp", "Fix flaky auth token refresh test",
             ["The auth token refresh test fails about 1 in 5 runs on CI — "
              "looks like a race between the clock mock and the refresh path.",
              "Can you pin both code paths to the same fake clock?"], 0.4,
             branch="fix/flaky-auth-test", turns_pad=3)
demo_session("webapp", "Add dark mode toggle to settings",
             ["Add a dark mode toggle to the settings page; persist the choice "
              "in localStorage and respect prefers-color-scheme by default."],
             3.1, turns_pad=5)
demo_session("webapp", "Migrate the build from webpack to Vite",
             ["Migrate this app from webpack 5 to Vite — keep the bundle "
              "analyzer and the existing env handling."], 27, turns_pad=11)
demo_session("api-server", "Fix N+1 queries in /orders endpoint",
             ["GET /orders is slow in production. I suspect N+1 queries on the "
              "line-items relation — profile it and fix what you find."], 1.2,
             branch="perf/orders-n-plus-1", turns_pad=7)
demo_session("api-server", "Migrate models to Pydantic v2",
             ["Upgrade the API models to Pydantic v2 and fix every deprecation "
              "warning the test suite prints."], 30, turns_pad=9)
demo_session("api-server", "Add rate limiting middleware",
             ["Add per-API-key rate limiting (sliding window, Redis) and return "
              "proper 429s with Retry-After."], 51, turns_pad=4)
demo_session("data-pipeline", "Backfill 2025 events into warehouse",
             ["Write a backfill job for the 2025 events into the warehouse — "
              "idempotent, resumable, and chunked by day."], 6.5, turns_pad=6)
demo_session("data-pipeline", "Debug Airflow DAG timeout",
             ["The nightly DAG times out on the dedup task since Tuesday. Find "
              "out what changed and fix it."], 73, turns_pad=8)
demo_session("dotfiles", "Set up neovim LSP for Rust",
             ["Set up rust-analyzer with nvim-lspconfig in my dotfiles, "
              "including inlay hints and format-on-save."], 95, turns_pad=2)

# ---- 3. drive the app, saving one SVG per story beat -----------------------
sys.path.insert(0, str(REPO))
import saikai  # noqa: E402

saikai._build_resume_invocation = lambda full_id, sessions: (
    [sys.executable, str(MOCK_CLAUDE)], str(demo_home), dict(os.environ))

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
            # Beat 1 — launch: State grouping + Recency sort + visible bar.
            await snap(pilot, 1800)
            # Beat 2 — type-to-search narrows the list live.
            for i, ch in enumerate("auth"):
                await pilot.press(ch)
                await snap(pilot, 250 if i < 3 else 1300)
            # Beat 3 — Esc returns to the list (filter + bar stay).
            await pilot.press("escape")
            await snap(pilot, 900)
            # Beat 4 — ␣ menu: pause so the which-key hint pops up.
            await pilot.press("space")
            await pilot.pause(0.9)               # > 0.6 s hesitation hint
            await snap(pilot, 2600)
            # Beat 5 — f = favorite (★ appears on the row).
            await pilot.press("f")
            await snap(pilot, 1400)
            # Beat 6 — Enter resumes the session in a live split pane.
            await pilot.press("enter")
            for _ in range(12):                  # let the PTY paint
                await pilot.pause(0.1)
            await snap(pilot, 1500)
            for _ in range(10):                  # streaming continues
                await pilot.pause(0.1)
            await snap(pilot, 1500)
            for _ in range(10):
                await pilot.pause(0.1)
            # Beat 7 — final hero frame, long hold before the loop restarts.
            await snap(pilot, 3000)
    asyncio.run(go())


App.run = fake_run
sys.argv = ["saikai", "--all"]
saikai.main()

# ---- 4. leak guard (same contract as make_screenshots.py) ------------------
suspicious = {demo_home.name.lower(), "temp", "appdata"}
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
    subprocess.run(
        [browser, "--headless=new", "--disable-gpu",
         "--force-device-scale-factor=1", "--hide-scrollbars",
         f"--window-size={W},{H}", f"--screenshot={png}",
         html.as_uri()],
        check=True, capture_output=True, timeout=60)
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
