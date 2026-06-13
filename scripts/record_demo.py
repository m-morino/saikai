"""Prepare, record, and audit an isolated real-Claude saikai demo."""
from __future__ import annotations

import argparse
import os
import shlex
import shutil
import subprocess
from pathlib import Path

from audit_demo_cast import audit_cast
from demo_fixture import DemoFixture, build_demo_fixture

if os.name != "nt":
    import pwd
else:
    pwd = None


REPO = Path(__file__).resolve().parent.parent
DEFAULT_ROOT = (Path("/home/demo/saikai-demo")
                if os.name != "nt" else REPO / ".demo-work")

GUIDE = """\
Public real-Claude recordings require a disposable Linux environment owned by
the OS user `demo`. Do not record from a normal workstation or real HOME.

Read docs/demo-recording.md before continuing.

  python scripts/record_demo.py --setup-only --root /home/demo/saikai-demo
  python scripts/record_demo.py --record-real --root /home/demo/saikai-demo
  python scripts/record_demo.py --audit /home/demo/saikai-demo/saikai-real.cast
"""


def _setup(root: Path) -> DemoFixture:
    fixture = build_demo_fixture(root)
    print(f"fixture root: {fixture.root}")
    print(f"fictional HOME: {fixture.home}")
    print(f"hero repo: {fixture.hero_repo}")
    print(f"synthetic sessions: {len(fixture.sessions)}")
    print("\nBefore seeding or recording:")
    print(f"  export HOME={shlex.quote(str(fixture.home))}")
    print(f"  export CLAUDE_CONFIG_DIR={shlex.quote(str(fixture.home / '.claude'))}")
    print("  export SAIKAI_DEMO_BARE_WRAPPER=1")
    return fixture


def _require_real_recording_environment(root: Path) -> None:
    failures: list[str] = []
    if os.name == "nt":
        failures.append("real recording must run in a dedicated Linux environment")
    else:
        try:
            assert pwd is not None
            if pwd.getpwuid(os.getuid()).pw_name != "demo":
                failures.append("OS user must be demo")
        except KeyError:
            failures.append("could not verify OS user demo")

    resolved = root.resolve()
    try:
        resolved.relative_to(Path("/home/demo"))
    except ValueError:
        failures.append("fixture root must be under /home/demo")
    if Path("/mnt/c").exists():
        failures.append("Windows drive mount /mnt/c is present")
    if os.environ.get("SSH_AUTH_SOCK"):
        failures.append("SSH_AUTH_SOCK must be unset")
    if Path(os.environ.get("HOME", "")).resolve() != (resolved / "home").resolve():
        failures.append("HOME must be the fixture home")
    expected_config = (resolved / "home" / ".claude").resolve()
    if Path(os.environ.get("CLAUDE_CONFIG_DIR", "")).resolve() != expected_config:
        failures.append("CLAUDE_CONFIG_DIR must be the fixture .claude directory")
    if not os.environ.get("ANTHROPIC_API_KEY"):
        failures.append("a recording-only ANTHROPIC_API_KEY must be injected")
    if os.environ.get("SAIKAI_DEMO_BARE_WRAPPER") != "1":
        failures.append("the documented bare Claude wrapper must be active")
    for command in ("asciinema", "claude", "saikai"):
        if not shutil.which(command):
            failures.append(f"required command not found: {command}")
    if failures:
        raise SystemExit("unsafe recording environment:\n  - " + "\n  - ".join(failures))


def _record_real(root: Path, cast: Path) -> None:
    _require_real_recording_environment(root)
    cast.parent.mkdir(parents=True, exist_ok=True)
    command = shlex.join(["saikai", "--all"])
    subprocess.run(
        [
            "asciinema", "rec", str(cast),
            "--cols", "128", "--rows", "35",
            "--command", command,
            "--overwrite",
        ],
        check=True,
    )
    audit_cast(cast)
    print(f"recorded and audited: {cast}")
    print("Review every frame before converting or publishing.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--guide", action="store_true")
    parser.add_argument("--setup-only", action="store_true")
    parser.add_argument("--record-real", action="store_true")
    parser.add_argument("--audit", type=Path)
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--cast", type=Path)
    args = parser.parse_args()

    if args.audit:
        audit_cast(args.audit)
        print(f"demo audit passed: {args.audit}")
        return 0
    if args.setup_only:
        _setup(args.root)
        return 0
    if args.record_real:
        _record_real(args.root, args.cast or (args.root / "saikai-real.cast"))
        return 0
    print(GUIDE)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
