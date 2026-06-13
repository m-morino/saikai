"""Record a saikai demo as an asciinema cast → convert to GIF with agg.

Sets up the same fictional demo data as make_screenshots.py so nothing private
can appear in the recording.

Prerequisites
─────────────
  pip install asciinema          (or: uv add asciinema)
  cargo install agg              (converts .cast → .gif)
    OR: npm install -g svg-term-cli   (alternative: .cast → .svg)

Usage
─────
  uv run scripts/record_demo.py          # auto-mode: records + prints convert cmd
  uv run scripts/record_demo.py --guide  # print manual-recording instructions only

The output is  docs/assets/saikai-demo.cast  (and a ready-to-run agg command).
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from demo_fixture import build_demo_fixture

REPO = Path(__file__).resolve().parent.parent
ASSETS = REPO / "docs" / "assets"
CAST_OUT = ASSETS / "saikai-demo.cast"
GIF_OUT  = ASSETS / "saikai-demo.gif"

def _build_demo_home() -> Path:
    fixture = build_demo_fixture(Path(tempfile.mkdtemp(prefix="saikai-demo-")))
    demo_home = fixture.home
    for var in ("USERPROFILE", "HOME", "APPDATA", "LOCALAPPDATA", "XDG_CONFIG_HOME"):
        os.environ[var] = str(demo_home)
    os.environ.pop("SAIKAI_CONFIG", None)
    os.environ["SAIKAI_SUMMARIZE_ENABLED"] = "0"
    os.environ["SAIKAI_AUTO_REFRESH"]       = "0"
    os.environ["SAIKAI_SPLIT_LIVE"]         = "0"   # list-only for a clean recording
    return demo_home


# ── manual-recording guide ────────────────────────────────────────────────────

GUIDE = """
┌─────────────────────────────────────────────────────────────────────────────┐
│  saikai demo recording guide                                                │
└─────────────────────────────────────────────────────────────────────────────┘

The script sets up a throwaway home with fictional demo sessions so nothing
private appears in the recording.  Run it in a terminal that is at least
128 columns × 35 rows.

Option A — asciinema (recommended, cross-platform)
──────────────────────────────────────────────────
  pip install asciinema
  uv run scripts/record_demo.py          ← sets up demo home + launches recorder

  Convert to GIF afterwards:
    cargo install agg
    agg docs/assets/saikai-demo.cast docs/assets/saikai-demo.gif \\
        --theme monokai --speed 1.5 --cols 128 --rows 35

  Or to animated SVG (no Rust needed):
    npx svg-term-cli --in docs/assets/saikai-demo.cast \\
        --out docs/assets/saikai-demo.svg --width 128 --height 35

Option B — WezTerm built-in (Windows/macOS/Linux, no extra deps)
──────────────────────────────────────────────────────────────────
  1. Run:  uv run scripts/record_demo.py --setup-only
     (exits after creating the demo home; prints the SAIKAI_ env vars to set)
  2. Set the printed env vars in a WezTerm shell.
  3. Start a cast:  wezterm record -o docs/assets/saikai-native.cast
  4. In the recorded shell:  uv run saikai --all-projects
  5. Exit the recorded shell, then replay with:
       wezterm replay docs/assets/saikai-native.cast

For an MP4 that captures terminal chrome, IME, and mouse behavior, use the OS
screen recorder while following the same sequence.

Suggested demo sequence (≈ 45 seconds)
───────────────────────────────────────
  1. App opens — full list visible with Date grouping (Shift+F7)
  2. Type "auth" — live filter narrows to matching sessions
  3. Esc — clear filter
  4. Down arrow — move to a different session
  5. Space f — mark favourite (★ appears)
  6. Shift+F7 twice — cycle through Project / State grouping
  7. ? — open help overlay, pause 2 s, Esc
  8. Alt+→ (×3) — shrink the list, showing more of the description column
"""


# ── auto record mode ──────────────────────────────────────────────────────────

def _find_saikai() -> list[str]:
    """Return the command list to run saikai (uv run or direct)."""
    saikai_py = REPO / "saikai.py"
    if saikai_py.exists():
        return [sys.executable, str(saikai_py)]
    return ["saikai"]


def _record(demo_home: Path) -> None:
    try:
        subprocess.run(["asciinema", "--version"], capture_output=True, check=True)
    except (FileNotFoundError, subprocess.CalledProcessError):
        print("asciinema not found — install it:  pip install asciinema")
        print("Or use --guide for manual recording instructions.")
        sys.exit(1)

    ASSETS.mkdir(parents=True, exist_ok=True)
    cmd = _find_saikai() + ["--all-projects"]

    env = {**os.environ, "HOME": str(demo_home), "USERPROFILE": str(demo_home)}
    print(f"Recording to {CAST_OUT}")
    print("Follow the suggested demo sequence from --guide, then press Ctrl-D or q.")
    print()

    subprocess.run(
        ["asciinema", "rec", str(CAST_OUT),
         "--cols", "128", "--rows", "35",
         "--command", " ".join(cmd),
         "--overwrite"],
        env=env,
        check=True,
    )

    print(f"\n✓ Saved: {CAST_OUT}")
    print("\nConvert to GIF (requires  cargo install agg):")
    print(f"  agg {CAST_OUT} {GIF_OUT} --theme monokai --speed 1.5")
    print("\nConvert to animated SVG (requires  npm install -g svg-term-cli):")
    svg = ASSETS / "saikai-demo.svg"
    print(f"  npx svg-term-cli --in {CAST_OUT} --out {svg} --width 128 --height 35")


# ── entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    # Windows may default stdout to CP932; the guide contains box drawing and
    # arrows. Preserve the guide where supported and degrade unsupported glyphs
    # instead of crashing before recording starts.
    try:
        sys.stdout.reconfigure(errors="replace")
    except (AttributeError, OSError):
        pass

    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--guide",      action="store_true",
                    help="Print manual recording instructions and exit")
    ap.add_argument("--setup-only", action="store_true",
                    help="Create demo home, print env vars, exit (for manual recording)")
    args = ap.parse_args()

    if args.guide:
        print(GUIDE)
        return

    demo_home = _build_demo_home()

    if args.setup_only:
        print("Demo home created.  Set these env vars before running saikai:\n")
        values = {
            "HOME": str(demo_home),
            "USERPROFILE": str(demo_home),
            "APPDATA": str(demo_home),
            "LOCALAPPDATA": str(demo_home),
            "SAIKAI_SUMMARIZE_ENABLED": "0",
            "SAIKAI_AUTO_REFRESH": "0",
        }
        if sys.platform == "win32":
            for var, value in values.items():
                escaped = value.replace("'", "''")
                print(f"  $env:{var} = '{escaped}'")
        else:
            import shlex
            for var, value in values.items():
                print(f"  export {var}={shlex.quote(value)}")
        print(f"\nThen run:  saikai --all-projects")
        return

    _record(demo_home)


if __name__ == "__main__":
    main()
