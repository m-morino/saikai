# /// script
# requires-python = ">=3.11"
# dependencies = ["ptyprocess>=0.7; sys_platform != 'win32'"]
# ///
"""Re-audit saikai's claude-CLI seams against the INSTALLED claude, empirically.

Why this exists: the 2026-07 checkpoint audit found that saikai's worst bugs
live at the seams mocked tests cannot reach — assumptions about the real
`claude` binary's behaviour that a claude update silently invalidates (e.g.
/clear began minting the child transcript INSTANTLY, so b2's reseed fired into
a still-initialising UI and its CR was absorbed). Static review scored those
seams healthy; only driving a real claude exposed them. This script makes that
E2E audit repeatable: run it after every claude major/minor update.

What it checks (each line prints PASS/FAIL):
  flags      every CLI flag saikai passes still exists in `claude --help`
  headless   `claude -p` with saikai's exact flag suite returns JSON w/ `result`
  persist    a pane spawned with saikai's _child_spawn_env writes its transcript
  registry   ~/.claude/sessions/<pid>.json names the live session (the @ marker)
  parsers    every transcript reader returns sane values on a FRESH transcript
  compact    b1's injection (paste → 0.6s → CR) actually starts a compact turn
  clear      /clear (b2 timing) mints the child; cwd+ts within the scan window
  reseed     paste + CR + verify/resend lands as the child's first real turn
  resume     `claude --resume <sid>` reopens the session

COST: spends a few SMALL turns + one /compact on your account, and briefly
creates sessions under ~/.claude/projects (deleted afterwards). Never run in
CI. POSIX-only (ptyprocess); on Windows, run it from WSL or port to pywinpty.

Usage:  uv run scripts/probe_claude_seams.py [--skip-compact]
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import saikai  # noqa: E402

SCRATCH = Path.home() / ".cache" / "saikai" / "seam-probe-work"
# Claude's project-dir slug transliterates [:/\.] to '-' (same rule
# _new_session_stub uses — dots matter: '.cache' becomes '-cache').
PDIR = (Path.home() / ".claude" / "projects"
        / re.sub(r"[:/\\.]", "-", str(SCRATCH)))
TITLE = re.compile(r"\x1b\]0;(.)")
ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"
                  r"|\x1b[()][AB0-2]")

RESULTS: list[tuple[str, bool, str]] = []


def report(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name:9s} {detail}")


def probe_env() -> dict:
    """saikai's child env + the full nested-claude scrub this harness needs
    (running the probe from INSIDE a claude session must not disable the
    spawned claude's transcript persistence — measured 2026-07)."""
    env = saikai._child_spawn_env()
    env["TERM"] = "xterm-256color"
    for k in list(env):
        if ((k.startswith(("CLAUDE_", "CLAUDECODE")) and k != "CLAUDE_CONFIG_DIR")
                or k in ("SSH_CONNECTION", "SSH_TTY", "SSH_CLIENT")):
            env.pop(k, None)
    return env


class Pane:
    """A tiny PTY harness that mimics how saikai drives a claude pane."""

    def __init__(self, argv: list[str]):
        import ptyprocess
        self._buf: list[str] = []
        self._lock = threading.Lock()
        self.p = ptyprocess.PtyProcessUnicode.spawn(
            argv, cwd=str(SCRATCH), env=probe_env(), dimensions=(35, 120))
        threading.Thread(target=self._reader, daemon=True).start()

    def _reader(self) -> None:
        while True:
            try:
                c = self.p.read(4096)
            except Exception:
                return
            with self._lock:
                self._buf.append(c)

    def text(self) -> str:
        with self._lock:
            return "".join(self._buf)

    def paste(self, t: str) -> None:            # saikai paste_text equivalent
        self.p.write("\x1b[200~" + t + "\x1b[201~")

    def cr(self) -> None:                        # saikai submit equivalent
        self.p.write("\r")

    def busy(self) -> bool:                      # saikai's title-spinner signal
        m = TITLE.findall(self.text())
        return bool(m) and 0x2800 <= ord(m[-1]) <= 0x28FF

    def wait_input_ready(self, timeout: float = 30) -> bool:
        t0 = time.time()
        while time.time() - t0 < timeout:
            if "trust" in self.text():
                self.cr()                       # accept the trust prompt
            if "\x1b[?2004h" in self.text():
                return True
            time.sleep(0.2)
        return False

    def wait_turn_done(self, timeout: float) -> bool:
        """b2-equivalent: must SEE busy, then settle back to non-busy."""
        t0 = time.time()
        seen = False
        while time.time() - t0 < timeout:
            if self.busy():
                seen = True
            elif seen:
                time.sleep(0.8)
                if not self.busy():
                    return True
            time.sleep(0.3)
        return False

    def close(self) -> None:
        try:
            self.p.write("\x03")
            time.sleep(0.3)
            self.p.write("\x03")
            time.sleep(0.5)
            self.p.terminate(force=True)
        except Exception:
            pass


def sids() -> set:
    try:
        return {q.stem for q in PDIR.glob("*.jsonl")}
    except OSError:
        return set()


def check_flags() -> None:
    need = ("-p,", "--session-id", "--setting-sources", "--strict-mcp-config",
            "--disable-slash-commands", "--no-session-persistence",
            "--output-format", "--model", "--effort", "--resume")
    try:
        out = subprocess.run(["claude", "--help"], capture_output=True,
                             text=True, timeout=60, env=probe_env()).stdout
    except Exception as e:
        report("flags", False, f"claude --help failed: {e}")
        return
    missing = [f for f in need if f not in out]
    report("flags", not missing, f"missing: {missing}" if missing else "all present")


def check_headless() -> None:
    cmd = ["claude", "-p", "--model", "haiku",
           "--session-id", str(uuid.uuid4()),
           "--setting-sources", "", "--strict-mcp-config",
           "--disable-slash-commands", "--no-session-persistence",
           "--output-format", "json"]
    try:
        out = subprocess.run(cmd, input="Reply with exactly: OK",
                             capture_output=True, text=True, timeout=180,
                             env=probe_env()).stdout
        d = json.loads(out)
        report("headless", isinstance(d.get("result"), str),
               f"result={d.get('result')!r}")
    except Exception as e:
        report("headless", False, repr(e))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-compact", action="store_true",
                    help="skip the /compact probe (saves its token cost)")
    args = ap.parse_args()

    shutil.rmtree(SCRATCH, ignore_errors=True)
    SCRATCH.mkdir(parents=True)
    shutil.rmtree(PDIR, ignore_errors=True)

    check_flags()
    check_headless()

    pane = Pane(["claude"])
    try:
        if not pane.wait_input_ready():
            report("persist", False, "claude never became input-ready")
            return 1
        time.sleep(2.0)

        # ── persist: first turn writes the transcript under saikai's child env
        pane.paste("Remember: the magic word is banana. Reply with exactly: noted")
        time.sleep(0.6)
        pane.cr()
        t0 = time.time()
        sid = None
        while time.time() - t0 < 60:
            if sids():
                sid = sorted(sids())[0]
                break
            time.sleep(0.3)
        report("persist", sid is not None,
               f"transcript in {time.time()-t0:.1f}s" if sid else "no jsonl in 60s")
        if sid is None:
            return 1
        pane.wait_turn_done(90)
        time.sleep(1.5)
        jsonl = PDIR / f"{sid}.jsonl"

        # ── registry: the @ open-elsewhere source
        hit = False
        try:
            for rf in (Path.home() / ".claude" / "sessions").glob("*.json"):
                try:
                    if sid in rf.read_text(encoding="utf-8"):
                        hit = True
                        break
                except OSError:
                    continue
        except OSError:
            pass
        report("registry", hit, "sessions/<pid>.json names the live sid")

        # ── parsers: every transcript reader saikai ships
        checks = {
            "ctx_tokens": lambda: (saikai._ctx_tokens_from_jsonl(jsonl) or 0) > 0,
            "ctx_usage_model": lambda: bool(saikai._ctx_usage_from_jsonl(jsonl)[1]),
            "last_assistant": lambda: "noted" in
                (saikai._last_assistant_text_from_jsonl(jsonl) or ""),
            "session_turns": lambda: len(saikai._session_turns(jsonl)) >= 2,
            "first_cwd": lambda: saikai._first_cwd_from_jsonl(jsonl) == str(SCRATCH),
            "first_ts": lambda: bool(saikai._first_ts_from_jsonl(jsonl)),
            "last_record": lambda: bool(saikai._read_last_jsonl_record(jsonl)),
            "surface_model": lambda: bool(saikai._session_surface_model(jsonl)[1]),
        }
        bad = []
        for name, fn in checks.items():
            try:
                if not fn():
                    bad.append(name)
            except Exception as e:      # noqa: BLE001
                bad.append(f"{name}({type(e).__name__})")
        report("parsers", not bad, f"broken: {bad}" if bad else
               f"{len(checks)} readers OK")

        # ── compact: b1's exact injection contract
        if args.skip_compact:
            print("skip  compact   (--skip-compact)")
        else:
            mark = len(pane.text())
            pane.paste("/compact")
            time.sleep(0.6)              # b1 settle (palette absorb)
            pane.cr()
            t0 = time.time()
            started, how = False, ""
            while time.time() - t0 < 20:
                if pane.busy():
                    started, how = True, "title spinner"
                    break
                # A tiny session's /compact finishes (or errors: "Not enough
                # messages to compact.") without ever flipping the title — the
                # command still EXECUTED. The robust evidence is claude's own
                # result marker: an executed slash command renders "❯ /compact"
                # followed by a "⎿ <result>" block, so look for ⎿ AFTER the
                # echoed command (the palette preview never renders one).
                delta = ANSI.sub("", pane.text()[mark:])
                after_echo = delta.split("/compact")[-1]
                if "⎿" in after_echo or "esc to interrupt" in after_echo:
                    started, how = True, "result block"
                    break
                time.sleep(0.3)
            report("compact", started,
                   f"executed in {time.time()-t0:.1f}s ({how})" if started
                   else "no execution evidence in 20s after paste+settle+CR")
            if started:
                pane.wait_turn_done(180)
                time.sleep(1)

        # ── clear: b2's mint-detection contract
        pre = sids()
        clear_ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        pane.paste("/clear")
        time.sleep(0.6)
        pane.cr()
        child = None
        prev, streak = None, 0
        t0 = time.time()
        for _ in range(34):              # _B2_DETECT_TICKS
            c = saikai._cleared_child_candidates(str(PDIR), pre, str(SCRATCH),
                                                 clear_ts)
            if len(c) == 1 and prev == c[0]:
                streak += 1
            elif len(c) == 1:
                prev, streak = c[0], 1
            else:
                prev, streak = None, 0
            if streak >= 3:              # _B2_CHILD_CONFIRM_TICKS
                child = prev
                break
            time.sleep(0.3)
        report("clear", child is not None,
               f"child bound in {time.time()-t0:.1f}s" if child
               else "no stable child in the b2 window")

        # ── reseed: b2's verify/resend contract
        if child is not None:
            pane.paste("resumed context line\nReply with exactly: PONG")
            time.sleep(0.6)              # settle (same as inject_reseed)
            pane.cr()
            verified, resends = False, 0
            for tick in range(34):       # _B2_RESEED_VERIFY_TICKS
                if pane.busy():
                    verified = True
                    break
                if tick > 0 and tick % 7 == 0:
                    pane.cr()
                    resends += 1
                time.sleep(0.3)
            report("reseed", verified,
                   f"submitted (CR resends={resends})" if verified
                   else "never went busy — CR-absorb hardening regressed?")
            if verified:
                pane.wait_turn_done(90)
    finally:
        pane.close()
    time.sleep(1)

    # ── resume
    pane2 = Pane(["claude", "--resume", sid])
    try:
        ready = pane2.wait_input_ready(30)
        time.sleep(3)
        plain = ANSI.sub("", pane2.text())
        ok = ready and ("banana" in plain or "noted" in plain)
        report("resume", ok, "prior content visible" if ok else "content missing")
    finally:
        pane2.close()

    # ── cleanup: remove everything this probe created
    shutil.rmtree(SCRATCH, ignore_errors=True)
    shutil.rmtree(PDIR, ignore_errors=True)

    fails = [n for n, ok, _ in RESULTS if not ok]
    print(f"\n{'ALL SEAMS PASS' if not fails else 'SEAM FAILURES: ' + ', '.join(fails)}")
    return 1 if fails else 0


if __name__ == "__main__":
    if sys.platform == "win32":
        sys.exit("POSIX-only (ptyprocess). Run from WSL, or port to pywinpty.")
    sys.exit(main())
