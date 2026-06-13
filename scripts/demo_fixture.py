"""Build the isolated fictional workspace used by every public demo path."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path


@dataclass(frozen=True)
class DemoFixture:
    root: Path
    home: Path
    hero_repo: Path
    sessions: list[Path]


_SESSION_SPECS = (
    ("webapp", "Fix flaky auth token refresh test",
     ("The auth token refresh test fails about 1 in 5 runs on CI. "
      "Check the expiry boundary and pin both paths to the same clock.",
      "Run the focused test before changing the implementation."),
     0.4, "fix/flaky-auth-test", 3),
    ("webapp", "Add dark mode toggle to settings",
     ("Add a dark mode toggle to settings and persist the choice.",),
     3.1, "main", 5),
    ("webapp", "Migrate the build from webpack to Vite",
     ("Migrate the app from webpack 5 to Vite without changing env handling.",),
     27, "main", 11),
    ("api-server", "Fix N+1 queries in /orders endpoint",
     ("Profile GET /orders and fix the line-item N+1 queries.",),
     1.2, "perf/orders-n-plus-1", 7),
    ("api-server", "Migrate models to Pydantic v2",
     ("Upgrade the API models to Pydantic v2 and remove deprecation warnings.",),
     30, "main", 9),
    ("api-server", "Add rate limiting middleware",
     ("Add per-API-key sliding-window rate limiting with proper 429 responses.",),
     51, "main", 4),
    ("data-pipeline", "Backfill 2025 events into warehouse",
     ("Write an idempotent and resumable backfill job, chunked by day.",),
     6.5, "main", 6),
    ("data-pipeline", "Debug Airflow DAG timeout",
     ("Find why the nightly dedup task started timing out and fix it.",),
     73, "main", 8),
    ("dotfiles", "Set up neovim LSP for Rust",
     ("Configure rust-analyzer, inlay hints, and format-on-save.",),
     95, "main", 2),
)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )


def _init_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    try:
        _git(repo, "init", "-q", "--initial-branch=main")
    except subprocess.CalledProcessError:
        _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "Demo User")
    _git(repo, "config", "user.email", "demo@example.invalid")


def _write_repos(repos: Path) -> Path:
    hero = repos / "webapp"
    for name in ("webapp", "api-server", "data-pipeline", "dotfiles"):
        repo = repos / name
        _init_repo(repo)
        (repo / "README.md").write_text(
            f"# {name}\n\nFictional repository used by the saikai public demo.\n",
            encoding="utf-8",
        )
        if name == "webapp":
            (repo / "app").mkdir()
            (repo / "tests").mkdir()
            (repo / "app" / "__init__.py").write_text("", encoding="utf-8")
            (repo / "app" / "auth.py").write_text(
                "def should_refresh(expires_at: int, now: int) -> bool:\n"
                "    \"\"\"Return whether a token should be refreshed.\"\"\"\n"
                "    return expires_at < now\n",
                encoding="utf-8",
            )
            (repo / "tests" / "test_auth.py").write_text(
                "import unittest\n\n"
                "from app.auth import should_refresh\n\n\n"
                "class RefreshBoundaryTest(unittest.TestCase):\n"
                "    def test_refreshes_at_expiry_boundary(self):\n"
                "        self.assertTrue(should_refresh(100, 100))\n\n\n"
                "if __name__ == \"__main__\":\n"
                "    unittest.main()\n",
                encoding="utf-8",
            )
        _git(repo, "add", ".")
        _git(repo, "commit", "-q", "-m", "Initial fictional demo state")
    return hero


def _write_session(home: Path, now: datetime, project: str, title: str,
                   messages: tuple[str, ...], age_h: float, branch: str,
    turns_pad: int) -> Path:
    recorded_cwd = f"/home/demo/work/{project}"
    # A public fixture uses stable project directory labels so screenshots are
    # readable and never encode the host's temporary path. The JSONL itself
    # still records only the public /home/demo identity.
    project_dir = home / ".claude" / "projects" / project
    project_dir.mkdir(parents=True, exist_ok=True)
    started = now - timedelta(hours=age_h)
    records = [{
        "type": "ai-title",
        "aiTitle": title,
        "timestamp": started.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        "cwd": recorded_cwd,
        "gitBranch": branch,
    }]
    prompts = list(messages) + ["Looks good, thanks."] * turns_pad
    for index, message in enumerate(prompts, 1):
        timestamp = (started + timedelta(minutes=3 * index)).strftime(
            "%Y-%m-%dT%H:%M:%S.000Z"
        )
        records.append({
            "type": "user",
            "timestamp": timestamp,
            "cwd": recorded_cwd,
            "gitBranch": branch,
            "message": {"content": message},
        })
        records.append({
            "type": "assistant",
            "timestamp": timestamp,
            "message": {"content": [{
                "type": "text",
                "text": f"(fictional demo reply {index})",
            }]},
        })

    session_id = uuid.uuid5(uuid.NAMESPACE_URL, f"saikai-demo:{project}:{title}")
    output = project_dir / f"{session_id}.jsonl"
    output.write_text(
        "\n".join(json.dumps(record) for record in records) + "\n",
        encoding="utf-8",
    )
    activity = (started + timedelta(minutes=3 * max(1, len(prompts)))).timestamp()
    os.utime(output, (activity, activity))
    return output


def build_demo_fixture(root: Path, *, synthetic_history: bool = True) -> DemoFixture:
    """Create a demo workspace without reading the caller's home or history."""
    root = Path(root)
    home = root / "home"
    repos = root / "repos"
    home.mkdir(parents=True, exist_ok=True)
    repos.mkdir(parents=True, exist_ok=True)
    hero_repo = _write_repos(repos)

    sessions: list[Path] = []
    if synthetic_history:
        now = datetime.now(timezone.utc).replace(microsecond=0)
        sessions = [
            _write_session(home, now, project, title, messages, age_h, branch, turns_pad)
            for project, title, messages, age_h, branch, turns_pad in _SESSION_SPECS
        ]
    return DemoFixture(root=root, home=home, hero_repo=hero_repo, sessions=sessions)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--no-history", action="store_true")
    args = parser.parse_args()
    fixture = build_demo_fixture(args.root, synthetic_history=not args.no_history)
    print(f"root={fixture.root}")
    print(f"home={fixture.home}")
    print(f"hero_repo={fixture.hero_repo}")
    print(f"sessions={len(fixture.sessions)}")


if __name__ == "__main__":
    main()
