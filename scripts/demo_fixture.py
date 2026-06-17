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
    # The deliberately context-BLOATED session (red gauge) the checkpoint /
    # context-lifecycle demo targets: its session id, the JSONL path, and the
    # project dir Claude would mint the post-/clear child transcript into. None
    # when history isn't seeded.
    bloated_sid: "str | None" = None
    bloated_jsonl: "Path | None" = None
    bloated_project_dir: "Path | None" = None
    bloated_cwd: "str | None" = None


# Each spec: (project, title, opening messages, age_h, branch, turns_pad,
# ctx_tokens). ctx_tokens seeds the LAST assistant turn's `usage` block so
# saikai's per-pane context gauge renders ground-truth fill (input +
# cache_read + cache_creation). Every fictional session here stays comfortably
# GREEN (well under the 55% warn band at its inferred tier) so the ONE bloated
# session below reads as the obvious standout. The bloated session is appended
# separately by build_demo_fixture (it carries the NEW SESSION PROMPT handoff).
_SESSION_SPECS = (
    ("webapp", "Fix flaky auth token refresh test",
     ("The auth token refresh test fails about 1 in 5 runs on CI. "
      "Check the expiry boundary and pin both paths to the same clock.",
      "Run the focused test before changing the implementation."),
     0.4, "fix/flaky-auth-test", 3, 38_000),
    ("webapp", "Add dark mode toggle to settings",
     ("Add a dark mode toggle to settings and persist the choice.",),
     3.1, "main", 5, 61_000),
    ("webapp", "Migrate the build from webpack to Vite",
     ("Migrate the app from webpack 5 to Vite without changing env handling.",),
     27, "main", 11, 88_000),
    ("api-server", "Fix N+1 queries in /orders endpoint",
     ("Profile GET /orders and fix the line-item N+1 queries.",),
     1.2, "perf/orders-n-plus-1", 7, 72_000),
    ("api-server", "Migrate models to Pydantic v2",
     ("Upgrade the API models to Pydantic v2 and remove deprecation warnings.",),
     30, "main", 9, 96_000),
    ("api-server", "Add rate limiting middleware",
     ("Add per-API-key sliding-window rate limiting with proper 429 responses.",),
     51, "main", 4, 47_000),
    ("data-pipeline", "Backfill 2025 events into warehouse",
     ("Write an idempotent and resumable backfill job, chunked by day.",),
     6.5, "main", 6, 64_000),
    ("data-pipeline", "Debug Airflow DAG timeout",
     ("Find why the nightly dedup task started timing out and fix it.",),
     73, "main", 8, 103_000),
    ("dotfiles", "Set up neovim LSP for Rust",
     ("Configure rust-analyzer, inlay hints, and format-on-save.",),
     95, "main", 2, 29_000),
)

# The deliberately context-BLOATED session (the context-lifecycle / Checkpoint
# standout). Its last assistant turn's usage sums to ~860K which — at the 1.0M
# tier saikai infers for a >200K reading — lands deep in the RED band (~86%).
# That turn is ALSO a finished /handoff: it ends with ONE fenced block whose
# first line is exactly `NEW SESSION PROMPT`, so the real b2 state machine can
# extract it (_extract_handoff_prompt), show it in the confirm modal, and
# reseed the fresh post-/clear session with it. Entirely fictional, /home/demo.
_BLOATED_SPEC = (
    "api-server", "Refactor the legacy billing module",
    ("The billing module has grown unmaintainable — invoices, proration, and "
     "tax all tangled in one 3k-line file. Plan a safe refactor into modules.",
     "Keep the public API stable; we have external callers.",
     "Walk the call graph first; don't move anything until we agree the seams."),
    2.3, "refactor/billing-module", 14,
)
# Context tokens just over the 70% red threshold at the 1.0M tier (86%).
_BLOATED_TOKENS = 862_000
# The /handoff turn b2 reads + reseeds with. ONE fenced block, first line
# exactly `NEW SESSION PROMPT`, standing fully alone (the contract b2 enforces).
_BLOATED_HANDOFF_TURN = (
    "Here's the handoff so a fresh session can pick this up without re-reading "
    "the whole 3k-line file.\n"
    "\n"
    "Decided: split billing into invoices / proration / tax behind the existing "
    "facade. Ruled out a big-bang rewrite (external callers depend on the public "
    "API). Seams agreed; nothing moved yet.\n"
    "\n"
    "```\n"
    "NEW SESSION PROMPT\n"
    "Continue the billing-module refactor in api-server (branch "
    "refactor/billing-module). Goal: split billing/legacy.py into invoices.py, "
    "proration.py, and tax.py behind the existing BillingFacade, keeping the "
    "public API byte-for-byte stable for external callers.\n"
    "\n"
    "State to resume from: nothing moved yet; the call graph is mapped and the "
    "module seams are agreed. Start by extracting invoices first (smallest, "
    "fewest cross-calls), run the billing tests after each extraction, and do "
    "NOT change BillingFacade's signatures. Center of gravity: billing/legacy.py "
    "and tests/test_billing.py.\n"
    "```"
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
    _git(repo, "config", "user.email", "demo" + "@" + "example.invalid")


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


def _usage_block(ctx_tokens: int) -> dict:
    """A realistic `usage` block whose three input components SUM to `ctx_tokens`
    — exactly what saikai's gauge reads (input + cache_read + cache_creation of
    the last record carrying usage). Most of a grown session's context lives in
    the cache-read field, so split it that way for authenticity."""
    ctx_tokens = max(0, int(ctx_tokens))
    cache_read = int(ctx_tokens * 0.86)
    cache_creation = int(ctx_tokens * 0.11)
    inp = ctx_tokens - cache_read - cache_creation
    return {
        "input_tokens": inp,
        "cache_read_input_tokens": cache_read,
        "cache_creation_input_tokens": cache_creation,
        "output_tokens": 420,
    }


def _write_session(home: Path, now: datetime, project: str, title: str,
                   messages: tuple[str, ...], age_h: float, branch: str,
                   turns_pad: int, ctx_tokens: int = 0,
                   final_assistant_text: "str | None" = None) -> Path:
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
    # When this session ends with a /handoff turn, append it as a final
    # user(/handoff) + assistant(handoff reply) exchange — that assistant turn
    # becomes the LAST assistant text b2 extracts the NEW SESSION PROMPT from.
    handoff_idx = None
    if final_assistant_text is not None:
        handoff_idx = len(prompts)              # 0-based index in `prompts`
        prompts = prompts + ["/handoff"]
    last_index = len(prompts)                   # the turn that carries `usage`
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
        if handoff_idx is not None and (index - 1) == handoff_idx:
            text = final_assistant_text                       # the handoff reply
        else:
            text = f"(fictional demo reply {index})"
        a_msg = {"content": [{"type": "text", "text": text}]}
        # Seed the context gauge on the LAST assistant turn (the record b2's
        # _ctx_tokens_from_jsonl reads). Other turns carry no usage, exactly like
        # a real transcript where only the latest reading is current.
        if ctx_tokens and index == last_index:
            a_msg["usage"] = _usage_block(ctx_tokens)
        records.append({
            "type": "assistant",
            "timestamp": timestamp,
            "message": a_msg,
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
    bloated_sid = bloated_jsonl = bloated_project_dir = bloated_cwd = None
    if synthetic_history:
        now = datetime.now(timezone.utc).replace(microsecond=0)
        sessions = [
            _write_session(home, now, project, title, messages, age_h, branch,
                           turns_pad, ctx_tokens)
            for project, title, messages, age_h, branch, turns_pad, ctx_tokens
            in _SESSION_SPECS
        ]
        # The bloated / NEW-SESSION-PROMPT session the context-lifecycle demo
        # drives (red gauge → Checkpoint → /clear → reseed).
        b_project, b_title, b_msgs, b_age, b_branch, b_pad = _BLOATED_SPEC
        bloated_jsonl = _write_session(
            home, now, b_project, b_title, b_msgs, b_age, b_branch, b_pad,
            _BLOATED_TOKENS, final_assistant_text=_BLOATED_HANDOFF_TURN)
        sessions.append(bloated_jsonl)
        bloated_sid = str(
            uuid.uuid5(uuid.NAMESPACE_URL, f"saikai-demo:{b_project}:{b_title}"))
        bloated_project_dir = bloated_jsonl.parent
        bloated_cwd = f"/home/demo/work/{b_project}"
    return DemoFixture(
        root=root, home=home, hero_repo=hero_repo, sessions=sessions,
        bloated_sid=bloated_sid, bloated_jsonl=bloated_jsonl,
        bloated_project_dir=bloated_project_dir, bloated_cwd=bloated_cwd)


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
    print(f"bloated_sid={fixture.bloated_sid}")
    print(f"bloated_project_dir={fixture.bloated_project_dir}")


if __name__ == "__main__":
    main()
