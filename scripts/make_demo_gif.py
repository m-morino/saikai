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
    # The BLOATED session plays the context-lifecycle "checkpoint" scenario: it
    # reads saikai's injected handoff/`/clear` and drives the REAL b2 machine
    # (mints the post-/clear child transcript into its project dir so detect_child
    # binds it honestly). The auth session plays the faithful idle transcript;
    # any other session plays "working -> needs you" (animates ~ -> ?).
    if full_id == fixture.bloated_sid:
        return ([sys.executable, str(MOCK_CLAUDE), "checkpoint", disp,
                 str(fixture.bloated_project_dir), str(fixture.bloated_sid)],
                str(fixture.hero_repo), dict(os.environ))
    # The reseeded CHILD is the session b2 actually RECORDED in the lineage (minted
    # by /clear during the checkpoint arc). Keying off the lineage — not "any
    # session sharing the bloated project dir" — is essential: the api-server
    # project also holds "Fix N+1 queries", which must play needs-you (-> ?), not
    # fresh. The lineage is empty when that session opens (beat 4) and only carries
    # the real child once b2 has run (beat 11).
    if full_id != fixture.bloated_sid and full_id in saikai._load_lineage():
        return ([sys.executable, str(MOCK_CLAUDE), "fresh", disp],
                str(fixture.hero_repo), dict(os.environ))
    sc = "idle" if "auth" in (s.get("ai_title") or "").lower() else "needs-you"
    return ([sys.executable, str(MOCK_CLAUDE), sc, disp],
            str(fixture.hero_repo), dict(os.environ))


saikai._build_resume_invocation = _resume
# Clean demo state: don't let the host's real memory load leak into the recording
# (an "⚠ 82% RAM" warning intruding on the statusbar). Pin a healthy reading so the
# RAM-aware capacity feature still shows, just not alarmingly.
saikai._mem_status = lambda: saikai._MemStatus(44.0, 8600.0, 12000.0, 16384.0, 0.0)

from textual.app import App  # noqa: E402

# 144 cols (was 128) so the per-pane context gauge in the status row — which sits
# after the Live/RAM segment — is FULLY visible (it needs ~136 cols; 128 clipped
# "862K/1.0M (86%)"). The callouts are fractional, so they scale with the width.
SIZE = (144, 35)
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
            # Beat 1 — HOOK: the whole cross-project list (the value shot).
            await snap(pilot, 1800)
            # Beat 2 — SEARCH: filter the whole list by title / body / id. Drive the
            # search Input directly (its Changed handler re-filters) — robust to key
            # routing like the dropdown beats, and focus stays on the table so the
            # next Enter still resumes.
            self.query_one("#search").value = "auth"
            await pilot.pause(0.6)
            await snap(pilot, 1900)              # filtered list + the query in the bar
            self.query_one("#search").value = ""   # clear -> the full list again
            await pilot.pause(0.5)
            # Beat 3 — RESUME the cursored session live (faithful pane).
            await pilot.press("enter")
            for _ in range(22):                  # let the PTY paint
                await pilot.pause(0.1)
            await snap(pilot, 1900)
            # Beat 4 — KEYBOARD NAV (the core loop): Ctrl+] hops back to the list,
            # ↑↓ moves the cursor to switch which session is selected — snap the
            # moved cursor, THEN open it as a second live pane (F2 / F3 to flip).
            await pilot.press("ctrl+right_square_bracket")   # back to the list
            await pilot.pause(0.4)
            await pilot.press("down")                        # cursor switches sessions
            await pilot.pause(0.4)
            await snap(pilot, 1700)              # the list, cursor moved — keyboard switch
            await pilot.press("down")                        # to another session
            await pilot.press("enter")                       # open it ("working" => ~)
            for _ in range(24):                  # let the 2nd pane paint
                await pilot.pause(0.1)
            await pilot.press("f2")              # flip to the other pane
            await pilot.pause(0.6)
            await snap(pilot, 1700)              # two panes, just flipped
            # Beat 5 — back on the list, let the mock flip its marker
            # ~ (working) -> ? (needs you): the supervise story in one shot.
            await pilot.press("ctrl+right_square_bracket")   # back to the list
            for _ in range(70):                  # wait out the mock ~ -> ? transition
                await pilot.pause(0.1)            # (+ the 2-tick debounce settling to ?)
            await snap(pilot, 1900)              # the ? (needs-you) marker on the list
            # Beat 6 — jump straight to the pane that needs you.
            await pilot.press("shift+f3")        # next-attention
            for _ in range(8):
                await pilot.pause(0.1)
            await snap(pilot, 1800)
            # ── Context-lifecycle arc (the standout). Drive the REAL b2 machine
            # on the deliberately bloated session: red gauge → Checkpoint → the
            # confirm gate → /clear+reseed → green gauge → Shift+F6 back. The
            # mock pane honours the injected handoff/`/clear` and mints the child
            # transcript, so detect_child binds it honestly (nothing faked).
            from textual.widgets import DataTable as _DT
            _table = self.query_one("#table", _DT)

            async def open_and_focus(sid):
                """Open `sid` as a live pane and put KEYBOARD FOCUS on its
                terminal — the statusbar context gauge only renders for the
                FOCUSED live pane (_focused_terminal()), so an opened-but-unfocused
                pane shows no gauge. Move the cursor by SID (robust to grouping /
                sort), open, focus the terminal, THEN recompute the subtitle so the
                gauge reflects the now-focused pane (nothing else refreshes it on a
                bare focus change)."""
                _table.focus()
                await pilot.pause(0.2)
                _table.move_cursor(row=_table.get_row_index(sid))
                await pilot.pause(0.3)
                self._open_or_attach_live(sid)
                t = None
                for _ in range(40):                # let it mount (python spawn + paint)
                    await pilot.pause(0.1)
                    t = self._live.get(sid) if self._live and self._live.has(sid) else None
                    if t is not None:
                        t.focus()                  # gauge needs the pane focused
                        await pilot.pause(0.1)
                        if self._focused_terminal() is not None:
                            break
                # Wait for the mock to actually PAINT (PTY spawn is slow on Windows)
                # so the pane isn't a blank rectangle at snap time, then hold focus
                # and refresh the statusbar so the ctx gauge for THIS pane renders.
                if t is not None:
                    for _ in range(30):
                        await pilot.pause(0.1)
                        try:
                            _txt, _ = t._current_screen()
                        except Exception:
                            _txt = ""
                        if _txt and "Claude Code" in _txt:
                            break
                    t.focus()
                    await pilot.pause(0.4)
                    try:
                        self._update_subtitle()
                    except Exception:
                        pass
                    await pilot.pause(0.2)

            # Beat 7 — RESUME the bloated session live + FOCUS it; the statusbar
            # context gauge reads its transcript and shows RED (~86%).
            await open_and_focus(fixture.bloated_sid)
            await pilot.pause(0.4)
            await snap(pilot, 2400)              # RED ctx gauge in the statusbar
            # Beat 8 — Space-c Checkpoint: drafts the handoff, then HOLDS on the
            # confirm modal showing the extracted NEW SESSION PROMPT (the trust
            # beat). run_action drives it regardless of key routing.
            await pilot.app.run_action("checkpoint")
            for _ in range(70):                  # ~7s: handoff busy→idle→extract
                await pilot.pause(0.1)
                if type(self.screen).__name__ == "ConfirmRefreshScreen":
                    break
            await pilot.pause(0.4)
            await snap(pilot, 2800)             # the confirm modal — HOLD
            # Beat 9 — Enter: the REAL b2 machine runs /clear, the mock mints the
            # fresh child transcript (low usage), b2 binds it + records lineage +
            # pastes the reseed prompt. Then re-scan (F5) so the new child session
            # is indexed, open + focus it, and snap its GREEN gauge (the child
            # transcript's real low context — nothing faked).
            await pilot.press("ctrl+s")          # Ctrl+S = proceed (the prompt is an
            #                                      editable TextArea now; Enter is a newline)
            for _ in range(90):                  # ~9s: /clear → detect child → reseed
                await pilot.pause(0.1)
                if getattr(self, "_b2", None) is None:
                    break
            await pilot.pause(0.6)
            # The child sid b2 just bound (read it back from the lineage it wrote).
            _lin = saikai._load_lineage()
            _child = next((c for c, r in _lin.items()
                           if r.get("parent") == fixture.bloated_sid), None)
            await pilot.app.run_action("refresh")   # F5 — index the new child
            await pilot.pause(0.7)
            if _child is not None and _child in self._sid_index:
                await open_and_focus(_child)        # open the reseeded child live
            await pilot.pause(0.4)
            await snap(pilot, 2500)             # GREEN ctx gauge on the reseeded pane
            # Beat 10 — Shift+F6: the parent (pre-clear) session is still there. The
            # list cursor is on the child (lineage child→parent), so open_parent
            # jumps the cursor to the parent row. Release focus to the list so the
            # parent row is highlighted under the cursor.
            if _child is not None and _child in self._sid_index:
                _table.move_cursor(row=_table.get_row_index(_child))
            await pilot.press("ctrl+right_square_bracket")   # focus the list
            await pilot.pause(0.4)
            await pilot.app.run_action("open_parent")        # Shift+F6
            await pilot.pause(0.7)
            await snap(pilot, 2200)
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
# shared between languages, only the wording differs. Index matches the snap order
# in go(): 0 list, 1 search (tail → the top search bar), 2 resume, 3 keyboard-nav
# (tail → the moved list cursor), 4 two-panes (tail → the right-pane tabs),
# 5 needs-you list, 6 jump, then the context-lifecycle arc — 7 RED gauge + 9 GREEN
# gauge tail → the ctx segment in the statusbar (it sits mid-bar); 8 confirm modal
# (centre); 10 parent (list). NOTE positions are eyeballed — re-tune against the
# rendered GIF if a tail misses.
_POS = [(0.40, 0.71, 0.17, 0.28), (0.30, 0.44, 0.05, 0.064),
        (0.42, 0.72, 0.72, 0.42), (0.34, 0.24, 0.07, 0.30),
        (0.12, 0.42, 0.42, 0.144),
        (0.04, 0.60, 0.02, 0.198), (0.42, 0.72, 0.72, 0.42),
        (0.30, 0.40, 0.66, 0.117), (0.28, 0.20, 0.50, 0.32),
        (0.30, 0.40, 0.66, 0.117), (0.40, 0.70, 0.18, 0.304)]
_EN = ["Every Claude Code session — across every repo, one screen",
       "Search every session — by title, body, or id",
       "Resume it live — real Claude Code, in its own directory",
       "Ctrl+] back to the list · ↑↓ to switch sessions — keyboard-first",
       "Run several at once — F2 / F3 to flip between panes",
       "~ → ? : saikai flags the ones waiting on you",
       "Jump straight to the one that needs you",
       "1. This session is full — and a full session's answers degrade",
       "2. Space c = Checkpoint: it writes a handoff, shows it — Ctrl+S runs it",
       "3. Ctrl+S runs /clear -> a fresh, lean session resumes from that handoff (green)",
       "4. The old full session stays too — Shift+F6 hops back anytime"]
_JA = ["全リポジトリの Claude Code を1画面に",
       "タイトル・本文・ID で全セッションを検索",
       "元のディレクトリでそのまま再開",
       "Ctrl+] でリストへ戻り ↑↓ でセッション切替 — キーボードだけで",
       "複数を同時に — F2 / F3 でペイン切替",
       "~ → ? 返信待ちを自動で検知",
       "要対応のセッションへジャンプ",
       "1. 満杯のセッション — コンテキストが限界で回答が劣化",
       "2. Space → c ＝ Checkpoint：引き継ぎを書き出して提示 → Ctrl+S で実行",
       "3. Ctrl+S で /clear → 引き継ぎから新しい軽量セッションを再開（緑）",
       "4. 元の満杯セッションも残る — Shift+F6 で行き来"]


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
        if os.environ.get("SAIKAI_DEMO_DUMP"):
            _dd = ASSETS / "_debug"; _dd.mkdir(parents=True, exist_ok=True)
            im.save(_dd / f"{out_gif.stem}-{idx + 1:02d}.png")
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
          "uv tool install saikai", GIF_OUT)
build_gif(_JA, _font(28, jp=True), _font(44, jp=True),
          "Claude Code のセッションを、まとめて管理",
          "uv tool install saikai", GIF_OUT_JA)
