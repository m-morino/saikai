# Remote roots — supervise sessions on other hosts (0.6 goal)

Status: **design** (branch `feature/remote-roots`, targeting 0.6).
Shipped groundwork in 0.5.0: Desktop-SSH mirror sessions (`projects/ssh-*`)
are badged `s` / `remote_origin` and refuse a local resume (#remote-origin).

## Problem

Claude Code sessions live on the machine they ran on. Today saikai supervises
exactly one machine — the one it runs on. But a real setup is a fleet: a
Windows workstation, a Pi, a NUC. Claude Desktop's SSH integration proves the
demand (it mirrors remote sessions into local `projects/ssh-<uuid>/`), but it
is one-host-one-session and its mirrors are dead weight for every other tool.

The goal: saikai on any machine shows **local + remote sessions in one list**,
and Enter opens a live pane on the right host.

## Phase 2 — `ssh -t` resume panes (the minimal useful step)

The pane machinery (`AgentTerminal`) spawns an arbitrary argv on a PTY and
does not care what the child is. So a remote resume is just:

```
ssh -t <host> 'cd <cwd> && claude --resume <sid>'
```

Everything downstream — pyte rendering, status classification, checkpoint
injection, the mirror tee — works unchanged, because the pane IS a terminal.

What's genuinely new:

1. **Host mapping** — transcripts do not record a hostname (verified against
   Code 2.1.198: the path/slug derive from `CLAUDE_CONFIG_DIR ?? ~/.claude` +
   cwd only; `ssh-<uuid>` mirror dirs don't encode the host either). So the
   user declares it in config:

   ```toml
   [remotes]
   # name = "ssh destination"; match by cwd prefix, first match wins
   pi  = { host = "mm@192.168.11.4",  cwd_prefixes = ["/home/mm", "/opt"] }
   nuc = { host = "mm@192.168.11.20", cwd_prefixes = ["/srv"] }
   ```

2. **Resume invocation** — `_build_resume_invocation` grows a remote variant:
   argv `["ssh", "-t", host, "cd <shq(cwd)> && exec claude --resume <sid>"]`,
   with `_resolve_resume_cwd`'s fallback chain evaluated against the REMOTE
   filesystem (cheap probe: `ssh host test -d <cwd>` before spawn, or just
   let claude's loud "No conversation found" surface in the pane — it dies
   visibly, which 0.5's audits verified is detectable).

3. **Which sessions get this** — `remote_origin` sessions whose cwd matches a
   configured prefix flip from "blocked" to "resumable via <name>"; the toast
   becomes the fallback for unmatched ones.

Known limitation (accepted for phase 2): after an ssh resume, new turns are
written on the REMOTE host's `~/.claude/projects` — the local `ssh-*` mirror
(written by Desktop) stops updating, so list freshness for that session comes
only from the open pane itself. Phase 3 fixes this properly.

Prerequisites to start:
- [ ] key-based ssh (no passphrase prompt) from the saikai host to each remote
      — an interactive prompt inside the pane is survivable but ugly
- [ ] one real `ssh-*` jsonl sample committed as a test fixture (schema is
      currently reconstructed from field notes: queue-operation records,
      foreign cwd)
- [ ] Windows: confirm `ssh.exe` (built-in OpenSSH) + ConPTY interop in a pane

## Phase 3 — remote discovery (fleet supervisor)

Discovery itself goes over ssh: the configured remotes' `~/.claude/projects`
are enumerated and merged into the one list, each row tagged with its host.

Design sketch:

- **Two-tier freshness.** Local keeps the 2s stat-gate. Remotes poll on a
  slow tick (15–30s) with ONE batched command per host per tick, e.g.
  `ssh host 'cd ~/.claude/projects && find . -name "*.jsonl" -newer .stamp
  -printf "%p %s %T@\n" ; touch .stamp'` — the local gate lesson applies
  (#audit-attention-freshness): watch file growth, not directory mtimes.
  Open panes are live via their own PTY regardless of the tick.
- **Transcript access.** Only CHANGED transcripts are pulled (scp/`ssh cat`),
  parsed with the existing pipeline into a per-host cache dir, so the list,
  preview, search and the `!` attention marker work identically. Bound the
  pull (tail-N first, full file on demand).
- **Liveness / is_open.** The remote pid registry (`~/.claude/sessions`) is
  part of the same batched read; `is_open` on a remote row means "open on
  that host" and resume switches to attach semantics there (or refuses, as
  local already does for open sessions).
- **Failure posture.** A host that doesn't answer degrades to its cached
  snapshot with a stale badge — never blocks the local list (the UI thread
  never waits on ssh; all remote I/O on worker threads, marshal results).
- **Identity.** Rows keyed (host, sid) — the same sid CAN exist on two hosts
  (Desktop mirrors); the local `ssh-*` mirror row and the authoritative
  remote row should merge, preferring the remote (fresher, resumable).

## Non-goals

- Re-implementing Desktop's queue protocol / driving its ssh agent binaries.
- Browser-side resize authority (the host owns the grid — settled in 0.5).
- Multi-user/multi-tenant anything: remotes are the operator's own machines.

## Test strategy

- Unit: host mapping (prefix match, first-wins, no-match → blocked toast).
- Pilot: remote invocation argv construction; `remote_origin` + matched
  prefix flips the resume gate.
- E2E (Pi as its own "remote" via `ssh localhost`): open an ssh pane against
  a seeded session, assert the pane goes live and the child is claude;
  phase 3: seed a change on the "remote", assert the slow tick flips `!`.
