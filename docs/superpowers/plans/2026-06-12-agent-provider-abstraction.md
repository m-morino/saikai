# Agent Provider Abstraction Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extract Claude's launch/runtime contract behind an agent-provider
interface and validate the boundary with a minimal Codex provider.

**Architecture:** `saikai_provider.py` defines provider contracts and concrete
Claude/Codex launch behavior. `saikai.py` retains application policy and delegates
final launch construction. `saikai_terminal.py` becomes agent-neutral and accepts
an injected status classifier selected by provider profile.

**Tech Stack:** Python 3.11+, dataclasses, Textual, pyte, pywinpty/ptyprocess.

---

### Task 1: Provider contract

**Files:**
- Create: `saikai_provider.py`
- Create: `tests/test_providers.py`

- [ ] Write failing contract tests for Claude/Codex capabilities and launch specs.
- [ ] Run `uv run python tests/test_providers.py` and confirm import failure.
- [ ] Implement `ProviderCapabilities`, `LaunchSpec`, `AgentProvider`,
  `ClaudeProvider`, `CodexProvider`, and `get_provider`.
- [ ] Run `uv run python tests/test_providers.py` and confirm all pass.
- [ ] Commit the provider contract.

### Task 2: Agent-neutral terminal

**Files:**
- Modify: `saikai_terminal.py`
- Modify: `tests/test_terminal_concurrency.py`

- [ ] Add failing tests for classifier profiles and classifier injection.
- [ ] Run `uv run python tests/test_terminal_concurrency.py` and confirm failure.
- [ ] Add `generic` classifier selection, inject the classifier into the terminal,
  rename the widget to `AgentTerminal`, and retain `ClaudeTerminal` as an alias.
- [ ] Run the terminal concurrency suite and confirm all pass.
- [ ] Commit the terminal abstraction.

### Task 3: Claude provider integration

**Files:**
- Modify: `saikai.py`
- Modify: `tests/test_sort_recency.py`
- Modify: `tests/test_keyboard_leader.py`

- [ ] Add failing tests proving the Claude launch wrappers delegate to the
  provider and preserve current argv behavior.
- [ ] Run the affected suites and confirm failure.
- [ ] Delegate final argv construction and live classifier selection to the
  active Claude provider.
- [ ] Update agent-neutral widget references without changing UI behavior.
- [ ] Run affected suites and confirm all pass.
- [ ] Commit the Claude integration.

### Task 4: Documentation and full verification

**Files:**
- Modify: `README.md`
- Modify: `README.ja.md`
- Modify: `CHANGELOG.md`
- Modify: `CONTRIBUTING.md`
- Modify: `.github/workflows/ci.yml`

- [ ] Document Claude-only current support and the provider extension boundary.
- [ ] Add `tests/test_providers.py` to CI and contributor commands.
- [ ] Run every test suite through `uv run`.
- [ ] Run `python -m py_compile saikai.py saikai_terminal.py saikai_provider.py`.
- [ ] Run `uv build` and `git diff --check`.
- [ ] Commit and push the completed abstraction.
