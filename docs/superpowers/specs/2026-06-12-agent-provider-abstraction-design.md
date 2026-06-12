# Agent provider abstraction

- **Date:** 2026-06-12
- **Status:** approved for implementation
- **Scope:** extract the existing Claude launch/runtime contract behind a small
  provider interface, make the PTY widget agent-neutral, and add a non-selectable
  Codex provider contract that validates the abstraction.

## Goal

Keep all current Claude behavior unchanged while separating agent-specific
launch and status semantics from saikai's reusable session UI and PTY runtime.
The resulting boundary must be useful for a later Codex integration without
claiming Codex support before its history and live-state behavior are integrated.

## Decisions

- Claude remains the only user-selectable provider in this change.
- `saikai_provider.py` owns provider identity, capabilities, binary resolution,
  resume/new launch arguments, and status-classifier profile.
- `saikai_terminal.py` owns PTY rendering, input, resize, process-tree teardown,
  and the actual classifier implementations.
- `saikai.py` owns application policy: cwd resolution, auto-permission opt-in,
  environment cleanup, Claude notification suppression, and UI behavior.
- Codex is included as a contract-level provider only. Its stable CLI supports
  `codex resume <SESSION_ID>` and new `codex` sessions, but it cannot accept a
  preassigned new-session ID. Codex history parsing, app-server events, and a
  provider selector are deferred.
- Capabilities are explicit. UI code must eventually branch on capabilities
  rather than provider names.

## Provider contract

```python
@dataclass(frozen=True)
class ProviderCapabilities:
    can_resume: bool
    can_create: bool
    can_preassign_id: bool
    has_reliable_live_status: bool
    has_transcript_changes: bool
    has_desktop_sync: bool


@dataclass(frozen=True)
class LaunchSpec:
    argv: list[str]
    cwd: str | None
    env: dict[str, str]
    session_id: str | None
```

An `AgentProvider` supplies:

- stable `id` and display name;
- capability declaration;
- status classifier profile (`claude` or `generic`);
- executable resolution;
- resume launch specification;
- new-session launch specification.

## Claude extraction

The existing `_build_claude_invocation`, `_build_resume_invocation`, and
`_build_new_invocation` remain as compatibility/application-policy wrappers.
After they prepare cwd, env, and optional auto-permission arguments, they
delegate the final argv construction to `ClaudeProvider`.

The live terminal receives a classifier selected from the provider's status
profile. Claude keeps its OSC-0 braille-spinner behavior exactly as today.

## Agent-neutral PTY

`ClaudeTerminal` is renamed to `AgentTerminal`. A compatibility alias remains so
external imports and older tests do not break immediately. Comments, labels, and
generic process-tree teardown must not assume the child is Claude. Claude-specific
status parsing remains available as one injected classifier.

## Codex boundary

`CodexProvider` verifies that the abstraction supports a materially different
CLI:

- resume: `codex resume <SESSION_ID>`;
- new: `codex`;
- no preassigned new-session ID;
- generic status classifier profile;
- no Claude Desktop sync or Claude transcript-change capability.

It is intentionally not exposed through CLI/config in this change. Exposing it
before normalized Codex history discovery exists would produce a provider that
can launch but cannot reliably list, attach, refresh, or restore sessions.

## Testing

- Provider contract tests cover capabilities, binary resolution, Claude argv,
  Codex argv, and Codex's lack of preassigned IDs.
- Terminal tests cover classifier selection and injected classifier use.
- Existing Claude launch tests remain green, proving behavior preservation.
- The full existing suite, `py_compile`, `uv build`, and `git diff --check` run
  before completion.

## Follow-up

The next Codex phase adds normalized history discovery and parsing from
`$CODEX_HOME/sessions`, then exposes provider selection. Accurate Codex live
state should use app-server thread/turn events rather than screen scraping.
