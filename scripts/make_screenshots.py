"""Regenerate the README screenshots (docs/assets/*.svg) headlessly.

Builds a throwaway $HOME with fictional demo sessions, points saikai at it,
drives the real PickerApp under Textual's run_test harness, and saves SVG
screenshots. The split-live shot runs scripts/mock_claude.py through the real
PTY/pyte pipeline instead of a real `claude` (nothing private can leak).

Usage:  uv run scripts/make_screenshots.py
"""
import asyncio
import json
import os
import re
import sys
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
ASSETS = REPO / "docs" / "assets"
MOCK_CLAUDE = Path(__file__).resolve().parent / "mock_claude.py"

# ---- 1. fake home, BEFORE importing saikai (it derives paths at import) ----
demo_home = Path(tempfile.mkdtemp(prefix="saikai-demo-home-"))
for var in ("USERPROFILE", "HOME", "APPDATA", "LOCALAPPDATA", "XDG_CONFIG_HOME"):
    os.environ[var] = str(demo_home)
os.environ.pop("SAIKAI_CONFIG", None)
os.environ["SAIKAI_SUMMARIZE_ENABLED"] = "0"
os.environ["SAIKAI_AUTO_REFRESH"] = "0"

# ---- 2. fictional sessions -------------------------------------------------
now = datetime.now(timezone.utc)


def _enc(p: str) -> str:
    return re.sub(r"[:/\\.]", "-", p)


def demo_session(project: str, title: str, msgs: list[str], age_h: float,
                 branch: str = "main", turns_pad: int = 0) -> None:
    # The cwd is just a string in the JSONL — use a fictional path so no real
    # machine path (user name, temp dir) can end up in a published screenshot.
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

# ---- 3. import saikai against the fake home, patch, and drive --------------
sys.path.insert(0, str(REPO))
import saikai  # noqa: E402

saikai._build_resume_invocation = lambda full_id, sessions: (
    [sys.executable, str(MOCK_CLAUDE)], str(demo_home), dict(os.environ))

from textual.app import App  # noqa: E402

SIZE = (128, 35)


def fake_run(self, *a, **kw):
    async def go():
        async with self.run_test(size=SIZE) as pilot:
            await pilot.pause(0.8)
            ASSETS.mkdir(parents=True, exist_ok=True)
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
suspicious = {demo_home.name.lower(), "temp", "appdata"}
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
