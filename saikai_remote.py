"""Fleet discovery over ssh — remote-roots phase 3 (docs/design/remote-roots.md).

One batched ssh command per host per tick enumerates the remote's
``<config_root>/projects/**.jsonl`` (path, size, mtime) and its live-session
registry (``<config_root>/sessions/*.json`` + a remote /proc liveness check);
only CHANGED transcripts are pulled — bounded head+tail — into a local cache
dir that mirrors the remote layout, with the REMOTE mtime restored on each
file so saikai's existing parse/recency pipeline works unchanged. All I/O
runs on a worker thread that only writes files; the UI never waits on ssh
(the 2s stat-gate notices the cache moving).

Pure logic (script builders, parsers, manifest diff, the fetch tick) lives
here and is unit-tested with a fake ssh runner; saikai.py wires rows and the
polling thread.
"""
from __future__ import annotations

import json
import os
import shlex
import subprocess
import time
from pathlib import Path
from typing import Callable, Mapping

# Bounded pull: enough head for session_meta/first turns (title, cwd,
# first_ts) and enough tail for last_ts / the attention heuristic. Lines cut
# at the seam fail json.loads and are skipped by the parser — by design.
HEAD_BYTES = 196_608
TAIL_BYTES = 65_536

_F, _S, _E = "===FILES===", "===SESS===", "===END==="


def _root_expr(config_root: str) -> str:
    """A remote-shell expression for the config root. shlex.quote would stop
    ``~`` from expanding, so a leading ~ becomes an explicit $HOME reference."""
    cr = config_root or "~/.claude"
    if cr == "~":
        return '"$HOME"'
    if cr.startswith("~/"):
        return '"$HOME"' + shlex.quote(cr[1:])
    return shlex.quote(cr)


def build_scan_script(config_root: str = "~/.claude") -> str:
    """One POSIX-sh pass: transcript listing + live registry with a REMOTE
    pid-reuse-guarded liveness check (same comm discipline as the local
    #audit-pidreuse guard). GNU find (-printf) — Linux remotes; macOS remotes
    are out of scope for phase 3."""
    r = _root_expr(config_root)
    return (
        f'R={r}; '
        f'printf "%s\\n" "{_F}"; '
        f'cd "$R/projects" 2>/dev/null && '
        f'find . -type f -name "*.jsonl" -printf "%P\\t%s\\t%T@\\n" 2>/dev/null; '
        f'printf "%s\\n" "{_S}"; '
        f'for f in "$R"/sessions/*.json; do '
        f'[ -f "$f" ] || continue; '
        f'pid=$(sed -n \'s/.*"pid"[^0-9]*\\([0-9][0-9]*\\).*/\\1/p\' "$f" | head -n1); '
        f'a=0; '
        f'if [ -n "$pid" ] && [ -r "/proc/$pid/comm" ]; then '
        f'case "$(cat /proc/$pid/comm 2>/dev/null)" in claude*|node) a=1;; esac; '
        f'fi; '
        f'printf "%s\\t" "$a"; tr -d "\\n" <"$f"; printf "\\n"; '
        f'done; '
        f'printf "%s\\n" "{_E}"'
    )


def parse_scan_output(text: str) -> "tuple[dict, list, bool]":
    """(files, live_sessions, complete). files = {relpath: (size, mtime)};
    live_sessions = the registry dicts whose pid passed the REMOTE liveness
    check. complete=False when the END sentinel is missing (truncated ssh
    output — treat the whole scan as failed rather than mass-deleting)."""
    files: dict[str, tuple[int, float]] = {}
    sessions: list[dict] = []
    section = ""
    complete = False
    for line in (text or "").splitlines():
        if line == _F:
            section = "files"
            continue
        if line == _S:
            section = "sess"
            continue
        if line == _E:
            complete = True
            break
        if section == "files":
            parts = line.split("\t")
            if len(parts) == 3:
                try:
                    files[parts[0]] = (int(parts[1]), float(parts[2]))
                except ValueError:
                    continue
        elif section == "sess":
            alive, _, raw = line.partition("\t")
            if alive != "1":
                continue
            try:
                d = json.loads(raw)
            except Exception:
                continue
            if isinstance(d, dict) and d.get("sessionId"):
                sessions.append(d)
    return files, sessions, complete


def diff_manifest(old: Mapping, new: Mapping) -> "tuple[list, list]":
    """(changed, deleted) relpaths. Changed = new or size/mtime moved. mtime
    compares with 1s slack — remote find emits sub-second precision but a
    round-trip through JSON may not keep it bit-identical."""
    changed = []
    for rel, (size, mtime) in new.items():
        o = old.get(rel)
        if o is None or int(o[0]) != int(size) or abs(float(o[1]) - float(mtime)) > 1.0:
            changed.append(rel)
    deleted = [rel for rel in old if rel not in new]
    return changed, deleted


def build_fetch_script(rels: "list[str]", config_root: str = "~/.claude",
                       head_bytes: int = HEAD_BYTES,
                       tail_bytes: int = TAIL_BYTES) -> str:
    """Emit the changed transcripts in one pass, each delimited and bounded:
    whole file when small, else head + GAP marker + tail. Relpaths ride a
    quoted heredoc so no filename ever meets the shell unquoted."""
    lst = "\n".join(rels)
    r = _root_expr(config_root)
    return (
        f'R={r}; HB={int(head_bytes)}; TB={int(tail_bytes)}; '
        f'while IFS= read -r p; do '
        f'f="$R/projects/$p"; '
        f's=$(wc -c <"$f" 2>/dev/null || echo 0); '
        f'printf "===F %s %s===\\n" "$s" "$p"; '
        f'if [ "$s" -le $((HB+TB)) ]; then cat "$f" 2>/dev/null; '
        f'else head -c "$HB" "$f"; printf "\\n===GAP===\\n"; tail -c "$TB" "$f"; fi; '
        f'printf "\\n===EOF===\\n"; '
        f"done <<'SAIKAI_LIST'\n{lst}\nSAIKAI_LIST"
    )


def parse_fetch_output(text: str) -> "dict[str, str]":
    """{relpath: stitched content}. The GAP marker becomes a newline — the two
    half-lines at the seam merge into one junk line that json.loads rejects,
    which parse_session already skips per-line."""
    out: dict[str, str] = {}
    cur: "str | None" = None
    buf: list[str] = []
    for line in (text or "").split("\n"):
        if line.startswith("===F ") and line.endswith("==="):
            cur = line[5:-3].split(" ", 1)[1] if " " in line[5:-3] else None
            buf = []
            continue
        if line == "===EOF===":
            if cur is not None:
                out[cur] = "\n".join(buf)
            cur = None
            continue
        if cur is not None:
            buf.append("\n" if line == "===GAP===" else line)
    return out


# ── the per-remote fetcher ───────────────────────────────────────────────────

Runner = Callable[[list], "tuple[int, str]"]     # argv -> (returncode, stdout)


def default_runner(argv: list, timeout: float = 45.0) -> "tuple[int, str]":
    try:
        r = subprocess.run(argv, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
        return r.returncode, r.stdout
    except Exception:
        return 255, ""


class RemoteFetcher:
    """Cache-updating tick for ONE configured remote. Writes only under its
    cache dir; safe to run from a worker thread. State files:

      manifest.json   {relpath: [size, mtime]} — written only when it changes
                      (its mtime is the change signal saikai's 2s gate stats)
      registry.json   {"sessions": {sid: {...}}} — live sessions on the host,
                      written only when it changes
      last-ok         empty file touched on every SUCCESSFUL tick — its mtime
                      is the staleness clock (NOT watched by the gate, so a
                      quiet healthy host doesn't cause rebuilds)
    """

    def __init__(self, name: str, host: str, ssh_args: "list[str]",
                 cache_root: Path, config_root: str = "~/.claude"):
        self.name, self.host, self.ssh_args = name, host, list(ssh_args)
        self.dir = Path(cache_root) / name
        self.config_root = config_root or "~/.claude"

    # BatchMode: a background poller must fail fast, never hang on a
    # password/hostkey prompt inside a daemon thread.
    def _argv(self, script: str) -> list:
        return ["ssh", *self.ssh_args, "-o", "BatchMode=yes",
                "-o", "ConnectTimeout=5", self.host, script]

    def _manifest(self) -> dict:
        try:
            with open(self.dir / "manifest.json", encoding="utf-8") as f:
                d = json.load(f)
            return d if isinstance(d, dict) else {}
        except Exception:
            return {}

    def _write_json_if_changed(self, path: Path, obj) -> None:
        data = json.dumps(obj, ensure_ascii=False, sort_keys=True)
        try:
            if path.read_text(encoding="utf-8") == data:
                return
        except Exception:
            pass
        tmp = path.with_suffix(".tmp")
        tmp.write_text(data, encoding="utf-8")
        os.replace(tmp, path)

    def tick(self, runner: Runner = default_runner) -> bool:
        """One poll. True = the host answered (cache is current); False = it
        didn't (cache untouched → rows degrade to the stale snapshot)."""
        self.dir.mkdir(parents=True, exist_ok=True)
        rc, out = runner(self._argv(build_scan_script(self.config_root)))
        files, live, complete = parse_scan_output(out)
        if rc != 0 or not complete:
            return False
        old = self._manifest()
        changed, deleted = diff_manifest(old, files)
        if changed:
            rc2, out2 = runner(self._argv(build_fetch_script(
                changed, self.config_root)))
            if rc2 != 0:
                return False
            got = parse_fetch_output(out2)
            for rel in changed:
                body = got.get(rel)
                if body is None:
                    # listed but not fetched (vanished mid-tick): leave the old
                    # manifest entry so the next tick retries it
                    files.pop(rel, None)
                    continue
                dst = self.dir / "projects" / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                tmp = dst.with_suffix(".tmp")
                tmp.write_text(body if body.endswith("\n") else body + "\n",
                               encoding="utf-8")
                os.replace(tmp, dst)
                mt = files[rel][1]
                os.utime(dst, (mt, mt))          # remote recency IS the recency
        for rel in deleted:
            try:
                (self.dir / "projects" / rel).unlink()
            except OSError:
                pass
        self._write_json_if_changed(
            self.dir / "manifest.json",
            {rel: [sz, mt] for rel, (sz, mt) in files.items()})
        self._write_json_if_changed(
            self.dir / "registry.json",
            {"sessions": {d["sessionId"]: {"status": d.get("status", ""),
                                           "kind": d.get("kind", "")}
                          for d in live}})
        (self.dir / "last-ok").touch()
        return True

    def registry(self) -> dict:
        try:
            with open(self.dir / "registry.json", encoding="utf-8") as f:
                d = json.load(f)
            s = d.get("sessions") if isinstance(d, dict) else None
            return s if isinstance(s, dict) else {}
        except Exception:
            return {}

    def stale_after(self, seconds: float) -> bool:
        """True when the last successful tick is older than `seconds` (or has
        never happened) — the row badge's 'unreachable, cached snapshot' cue."""
        try:
            return (time.time() - (self.dir / "last-ok").stat().st_mtime) > seconds
        except OSError:
            return True
