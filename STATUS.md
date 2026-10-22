# saikai — STATUS

Current state, open threads, and next steps. Architecture and invariants live in
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md); this file is what changes week to week.

## 2026-10-22(Thu)

### In flight
- **`feature/codex-parallel`**: Claude and Codex panes side by side (not pushed yet).
  - C1: codex threads in the list; Enter → `codex resume` in a pane.
  - Status classifier built from real codex-cli 0.163.0 screens
    (`tests/fixtures/codex_screens_0163.json`).
  - New codex session: Shift+F8, then Shift+F8 inside the dialog. The pane starts
    under a `codex-new-…` placeholder and is re-keyed to the thread once codex
    writes its rollout, which happens after the first prompt.
  - Review fixes (5-angle audit, 2026-10-22):
    - a pane still mounting is no longer dropped as closed;
    - `!`/done state moves with the re-key;
    - open-panes and worksets record the provider, so a restored codex pane no
      longer runs `claude --resume <thread>`;
    - no `claude -p` titles for codex threads;
    - a `› 1. …` prompt echoed in history is no longer read as a choice.
  - Waiting for: approval of the UI screenshots, then merge to master and push.

### Not done for codex
- `!` reply-due on dormant codex threads, open-elsewhere detection, checkpoint.
- Two new codex panes in the same folder before either prompt: the matcher refuses
  to guess, so neither is re-keyed. They still work, but only as placeholders.

### Backlog
- Merge `feature/remote-roots` (far behind master).
- Live state from `claude agents --json` (positioning audit, 2026-08).
- Release session memory (`#session-memory`, pushed, not released).
- Claude Desktop–style multi-pane split view (future).
- Remove the obsolete `feature/codex-provider` branch and worktree.
