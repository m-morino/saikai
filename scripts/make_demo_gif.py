# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "textual>=0.50", "platformdirs>=3", "pyte>=0.8", "pillow>=10", "segno>=1.6",
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
for _v in ("SAIKAI_MIRROR", "SAIKAI_MIRROR_HOST", "SAIKAI_MIRROR_PORT",
           "SAIKAI_MIRROR_ALLOW_LAN_INPUT"):
    os.environ.pop(_v, None)        # never inherit the caller's mirror config

FRAMES_DIR = Path(tempfile.mkdtemp(prefix="saikai-demo-frames-"))

# ---- 2. drive the app, saving one SVG per story beat -----------------------
sys.path.insert(0, str(REPO))
import saikai  # noqa: E402

def _resume(full_id, sessions):
    s = next((x for x in (sessions or []) if x.get("id") == full_id), {})
    disp = s.get("cwd") or s.get("origin_cwd") or "/home/demo/work/webapp"
    # The auth session plays the faithful idle transcript; any other session
    # plays "working -> needs you", which animates its list marker ~ -> ?.
    sc = "idle" if "auth" in (s.get("ai_title") or "").lower() else "needs-you"
    return ([sys.executable, str(MOCK_CLAUDE), sc, disp],
            str(fixture.hero_repo), dict(os.environ))


saikai._build_resume_invocation = _resume

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
            # Attach a loopback mirror hub so the F12 beat shows a real QR. (The
            # production start path wires the mirror through a custom driver_class
            # that Textual's run_test replaces, so do it directly here.)
            import secrets as _sec
            import saikai_mirror as _m
            _tok = _sec.token_urlsafe(16)
            _hub = _m.MirrorHub(token=_tok, host="127.0.0.1",
                                port=0, cols=SIZE[0], rows=SIZE[1])
            _hub.serve()
            # The real server stays on loopback (the demo never exposes anything),
            # but show a plausible LAN URL in the QR — 127.0.0.1 would read as
            # "a phone could never reach that".
            _hub.url = lambda: f"http://192.168.1.50:8771/?token={_tok}"
            self._mirror_hub = _hub
            # Beat 1 — HOOK: the whole cross-project list (the value shot).
            await snap(pilot, 3000)
            # Beat 2 — RESUME the cursored auth session live (faithful pane).
            await pilot.press("enter")
            for _ in range(22):                  # let the PTY paint
                await pilot.pause(0.1)
            await snap(pilot, 2400)
            # Beat 3 — open a SECOND session and flip between the two panes.
            await pilot.press("ctrl+right_square_bracket")   # back to the list
            await pilot.pause(0.4)
            await pilot.press("down")                        # to the api-server session
            await pilot.press("enter")                       # open it ("working" => ~)
            for _ in range(24):                  # let the 2nd pane paint
                await pilot.pause(0.1)
            await snap(pilot, 1700)              # two live panes
            await pilot.press("f2")              # flip tabs
            await pilot.pause(0.6)
            await snap(pilot, 1500)
            # Beat 4 — back on the list: the second pane is ~ working...
            await pilot.press("ctrl+right_square_bracket")
            await pilot.pause(0.6)
            await snap(pilot, 1600)
            # ...and a moment later its marker has changed to ? (needs you).
            for _ in range(36):                  # mock transitions (5 s) + 1.5 s poll
                await pilot.pause(0.1)
            await snap(pilot, 2600)
            # Beat 5 — jump straight to the pane that needs you.
            await pilot.press("shift+f3")        # next-attention
            for _ in range(8):
                await pilot.pause(0.1)
            await snap(pilot, 2400)
            # Beats 6-7 — organize the list: GROUP then SORT. Set the dropdown
            # values directly (Select.Changed applies them and keeps the dropdown
            # consistent) — the keyboard cyclers either don't sync the Select
            # (sort) or risk a stray key landing in the search box.
            await pilot.press("ctrl+right_square_bracket")    # back to the list
            await pilot.pause(0.5)
            self.query_one("#groupsel").value = "date"        # group by date
            await pilot.pause(0.7)
            await snap(pilot, 2600)
            self.query_one("#sortsel").value = "title"        # sort A->Z
            await pilot.pause(0.7)
            await snap(pilot, 2600)
            # Beat 8 — the closer: mirror to ANOTHER DEVICE via the scannable QR.
            # Call F12's action directly (key routing depends on focus; this does
            # not). We're already on the list after the sort beat.
            self.action_mirror_info()            # F12's action — show the QR
            await pilot.pause(0.8)
            await snap(pilot, 3000)
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

# ---- 6. draw callout bubbles, add an end card, assemble the GIF -------------
from PIL import Image, ImageDraw, ImageFont  # noqa: E402


GIF_OUT_JA = ASSETS / "saikai-demo-ja-headless.gif"


def _font(sz, jp=False):
    en = (r"C:\Windows\Fonts\segoeuib.ttf", r"C:\Windows\Fonts\arialbd.ttf",
          r"C:\Windows\Fonts\segoeui.ttf", r"C:\Windows\Fonts\arial.ttf")
    jp_ = (r"C:\Windows\Fonts\YuGothB.ttc", r"C:\Windows\Fonts\meiryob.ttc",
           r"C:\Windows\Fonts\YuGothM.ttc", r"C:\Windows\Fonts\meiryo.ttc",
           r"C:\Windows\Fonts\msgothic.ttc")
    for p in (jp_ if jp else en):
        try:
            return ImageFont.truetype(p, sz)
        except Exception:
            pass
    return ImageFont.load_default()


# A bright terracotta bubble (the saikai accent) with dark text + a near-white
# border, so callouts POP against the dark terminal instead of blending in.
_BUBBLE, _EDGE, _TXT = (226, 124, 88, 255), (252, 246, 240, 255), (26, 20, 16, 255)


def _wrap(d, text, maxw, font):
    # Word-wrap on spaces (English); fall back to per-character for Japanese,
    # which has no spaces between words.
    spaced = " " in text.strip()
    units = text.split() if spaced else list(text)
    join = " " if spaced else ""
    out, cur = [], ""
    for u in units:
        t = (cur + join + u) if cur else u
        if d.textlength(t, font=font) <= maxw or not cur:
            cur = t
        else:
            out.append(cur); cur = u
    if cur:
        out.append(cur)
    return out


def _callout(im, text, anchor, tail, font):
    """Draw a rounded terracotta speech bubble (tail pointing at `tail`) over
    `im`. `anchor`/`tail` are (x, y) fractions, so positions are
    resolution-independent across the EN and JA renders."""
    W, H = im.size
    ov = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(ov)
    pad, maxw = 16, 380
    lines = _wrap(d, text, maxw, font)
    asc, desc = font.getmetrics(); lh = asc + desc + 6
    tw = max(d.textlength(ln, font=font) for ln in lines)
    bw, bh = int(tw + 2 * pad), int(len(lines) * lh + 2 * pad)
    bx = max(8, min(int(anchor[0] * W), W - bw - 8))
    by = max(8, min(int(anchor[1] * H), H - bh - 8))
    if tail:
        tx, ty = int(tail[0] * W), int(tail[1] * H)
        cx, cy = bx + bw // 2, by + bh // 2
        if abs(tx - cx) > abs(ty - cy):
            ex = bx if tx < cx else bx + bw
            d.polygon([(ex, cy - 15), (ex, cy + 15), (tx, ty)], fill=_BUBBLE)
        else:
            ey = by if ty < cy else by + bh
            d.polygon([(cx - 15, ey), (cx + 15, ey), (tx, ty)], fill=_BUBBLE)
    d.rounded_rectangle([bx, by, bx + bw, by + bh], radius=13,
                        fill=_BUBBLE, outline=_EDGE, width=3)
    yy = by + pad
    for ln in lines:
        d.text((bx + pad, yy), ln, font=font, fill=_TXT); yy += lh
    return Image.alpha_composite(im.convert("RGBA"), ov).convert("RGB")


# Per-beat (text, bubble top-left, tail target) as W/H fractions; positions are
# shared between languages, only the wording differs.
_POS = [(0.40, 0.71, 0.17, 0.28), (0.42, 0.72, 0.72, 0.42), (0.43, 0.40, 0.56, 0.17),
        None, (0.04, 0.60, 0.02, 0.225), (0.04, 0.60, 0.02, 0.225),
        (0.42, 0.72, 0.72, 0.42),
        (0.30, 0.30, 0.40, 0.09), (0.42, 0.30, 0.55, 0.09),
        (0.63, 0.38, 0.54, 0.42)]
_EN = ["Every Claude Code session — across every repo, one screen",
       "Resume it live — real Claude Code, in its own directory",
       "Run several at once — flip between them", None,
       "Watch the left column — ~ means working",
       "~ → ? — now it needs your reply",
       "Jump straight to the one that needs you",
       "Group by State, Project, or Date",
       "Sort by Recency, Created, or A–Z",
       "Scan to mirror & control it from another device"]
_JA = ["全リポジトリの Claude Code を1画面に",
       "元のディレクトリでそのまま再開",
       "複数を同時に動かして行き来", None,
       "左の ~ は作業中",
       "~ が ? になったら返信待ち",
       "要対応のセッションへジャンプ",
       "State・Project・Date でグループ分け",
       "Recency・Created・A–Z で並べ替え",
       "QR を読むだけで別の端末から操作"]


def _callouts(texts):
    return [None if (t is None or p is None) else (t, (p[0], p[1]), (p[2], p[3]))
            for t, p in zip(texts, _POS)]


def build_gif(texts, font, big_font, tagline, sub, out_gif):
    imgs, cos = [], _callouts(texts)
    for idx, (name, _) in enumerate(FRAMES):
        im = Image.open((FRAMES_DIR / name).with_suffix(".png")).convert("RGB")
        co = cos[idx] if idx < len(cos) else None
        if co:
            im = _callout(im, co[0], co[1], co[2], font)
        imgs.append(im)
    end = Image.new("RGB", imgs[0].size, (16, 16, 20))
    ed = ImageDraw.Draw(end)
    ed.text(((end.width - ed.textlength(tagline, font=big_font)) // 2,
             int(end.height * 0.42)), tagline, font=big_font, fill=(240, 240, 240))
    ed.text(((end.width - ed.textlength(sub, font=font)) // 2,
             int(end.height * 0.42) + 78), sub, font=font, fill=(226, 124, 88))
    imgs.append(end)
    durations = [int(ms * 1.35) for _, ms in FRAMES] + [3400]   # slower switching
    imgs = [im.quantize(colors=128, dither=Image.Dither.NONE) for im in imgs]
    ASSETS.mkdir(parents=True, exist_ok=True)
    imgs[0].save(out_gif, save_all=True, append_images=imgs[1:],
                 duration=durations, loop=0, optimize=True)
    print(f"saved: {out_gif}  ({len(imgs)} frames, {out_gif.stat().st_size/1024:.0f} KB)")


build_gif(_EN, _font(27), _font(48),
          "Find it.  Resume it.  Know what needs you.",
          "saikai — mission control for Claude Code", GIF_OUT)
build_gif(_JA, _font(28, jp=True), _font(44, jp=True),
          "Claude Code のセッションを、まとめて管理",
          "saikai", GIF_OUT_JA)
