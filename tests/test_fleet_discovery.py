"""Fleet discovery (remote-roots phase 3): the batched scan/fetch scripts,
their parsers, the manifest diff, and RemoteFetcher's cache tick.

The sh scripts are exercised for REAL — run locally via `sh -c` against a
scratch config_root — so the quoting/format contract is tested end-to-end
without ssh; the fetcher is driven by a fake runner with canned outputs.

Run:  python tests/test_fleet_discovery.py
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

os.environ.pop("SAIKAI_MIRROR", None)
_FAKE_HOME = Path(tempfile.mkdtemp(prefix="saikai-fleet-test-"))
for _var in ("USERPROFILE", "HOME", "APPDATA", "LOCALAPPDATA", "XDG_CONFIG_HOME"):
    os.environ[_var] = str(_FAKE_HOME)
os.environ.pop("SAIKAI_CONFIG", None)
os.environ["SAIKAI_SUMMARIZE_ENABLED"] = "0"
os.environ["SAIKAI_NO_TERMINAL_WATCHDOG"] = "1"

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import saikai_remote as sr


def _sh(script: str) -> "tuple[int, str]":
    r = subprocess.run(["sh", "-c", script], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=30)
    return r.returncode, r.stdout


def _mk_remote_root() -> Path:
    root = Path(tempfile.mkdtemp(prefix="fleet-remote-root-"))
    (root / "projects" / "-home-mm-proj-app").mkdir(parents=True)
    (root / "sessions").mkdir()
    return root


def test_root_expr_home_expansion():
    assert sr._root_expr("~/.claude") == '"$HOME"/.claude'
    assert sr._root_expr("~") == '"$HOME"'
    assert sr._root_expr("/opt/claude root") == "'/opt/claude root'"
    rc, out = _sh("R=" + sr._root_expr("~/.claude") + '; printf "%s" "$R"')
    assert rc == 0 and out == str(Path.home() / ".claude"), out


def test_scan_script_lists_files_and_filters_dead_registry():
    root = _mk_remote_root()
    sid = str(uuid.uuid4())
    f = root / "projects" / "-home-mm-proj-app" / f"{sid}.jsonl"
    f.write_text('{"type":"user","timestamp":"2026-07-13T00:00:00.000Z"}\n',
                 encoding="utf-8")
    # registry: one entry with a pid that is NOT a claude process (this python)
    # → the remote liveness guard must drop it; one with a dead pid too.
    (root / "sessions" / "1.json").write_text(
        json.dumps({"pid": os.getpid(), "sessionId": "self", "status": "idle"}))
    (root / "sessions" / "2.json").write_text(
        json.dumps({"pid": 999999999, "sessionId": "gone", "status": "idle"}))
    rc, out = _sh(sr.build_scan_script(str(root)))
    assert rc == 0, out
    files, live, complete = sr.parse_scan_output(out)
    assert complete is True
    rel = f"-home-mm-proj-app/{sid}.jsonl"
    assert rel in files, files
    size, mtime = files[rel]
    assert size == f.stat().st_size
    assert abs(mtime - f.stat().st_mtime) < 1.0
    assert live == [], live       # neither pid is a live claude


def test_parse_scan_output_synthetic_live_and_truncation():
    sid = str(uuid.uuid4())
    good = ("===FILES===\n"
            f"-p/x.jsonl\t10\t1700000000.5\n"
            "===SESS===\n"
            "1\t" + json.dumps({"pid": 5, "sessionId": sid, "status": "busy",
                                "kind": "interactive"}) + "\n"
            "0\t" + json.dumps({"pid": 6, "sessionId": "dead", "status": "idle"}) + "\n"
            "===END===\n")
    files, live, complete = sr.parse_scan_output(good)
    assert complete and files == {"-p/x.jsonl": (10, 1700000000.5)}
    assert [d["sessionId"] for d in live] == [sid]
    # truncated (ssh died mid-stream): must NOT look like "everything deleted"
    _f, _l, complete2 = sr.parse_scan_output(good.replace("===END===\n", ""))
    assert complete2 is False


def test_diff_manifest():
    old = {"a": (10, 100.0), "b": (20, 200.0), "c": (5, 50.0)}
    new = {"a": (10, 100.4), "b": (21, 200.0), "d": (1, 10.0)}
    changed, deleted = sr.diff_manifest(old, new)
    assert set(changed) == {"b", "d"}          # size moved / brand new
    assert deleted == ["c"]
    assert sr.diff_manifest({}, {})[0] == []


def test_fetch_script_small_whole_and_big_stitched():
    root = _mk_remote_root()
    pd = root / "projects" / "-home-mm-proj-app"
    small = pd / "small.jsonl"
    small.write_text('{"n":1}\n{"n":2}\n', encoding="utf-8")
    big = pd / "big.jsonl"
    head_rec = json.dumps({"type": "first", "timestamp": "T0"})
    tail_rec = json.dumps({"type": "last", "timestamp": "T9"})
    filler = "\n".join(json.dumps({"pad": i, "x": "y" * 50}) for i in range(200))
    big.write_text(head_rec + "\n" + filler + "\n" + tail_rec + "\n",
                   encoding="utf-8")
    rels = ["-home-mm-proj-app/small.jsonl", "-home-mm-proj-app/big.jsonl"]
    rc, out = _sh(sr.build_fetch_script(rels, str(root),
                                        head_bytes=300, tail_bytes=200))
    assert rc == 0, out
    got = sr.parse_fetch_output(out)
    assert got[rels[0]].rstrip("\n") == '{"n":1}\n{"n":2}'   # small file: exact body
    stitched = got[rels[1]]
    assert stitched.startswith(head_rec)        # head survives
    assert tail_rec in stitched                 # tail survives
    # the seam merges two half-lines into one junk line: every OTHER line is
    # either valid json or that single junk — the parser discipline holds
    bad = sum(1 for l in stitched.splitlines()
              if l.strip() and not _loads_ok(l))
    assert bad <= 2, f"too many corrupt lines at the seam: {bad}"


def _loads_ok(line: str) -> bool:
    try:
        json.loads(line)
        return True
    except Exception:
        return False


def test_stitched_partial_still_parses_as_session():
    """The bounded pull must still yield a listable session via saikai's own
    parse_session (first/last ts, title from the head)."""
    import saikai
    root = _mk_remote_root()
    pd = root / "projects" / "-home-mm-proj-app"
    sid = str(uuid.uuid4())
    recs = [{"type": "ai-title", "aiTitle": "Fleet parse probe",
             "timestamp": "2026-07-13T00:00:00.000Z", "cwd": "/home/mm/proj/app"},
            {"type": "user", "timestamp": "2026-07-13T00:01:00.000Z",
             "cwd": "/home/mm/proj/app",
             "message": {"content": "remote prompt long enough"}}]
    recs += [{"type": "pad", "timestamp": f"2026-07-13T00:0{i%10}:30.000Z",
              "x": "y" * 120} for i in range(300)]
    recs.append({"type": "user", "timestamp": "2026-07-13T09:00:00.000Z",
                 "cwd": "/home/mm/proj/app",
                 "message": {"content": "the very last remote turn"}})
    big = pd / f"{sid}.jsonl"
    big.write_text("\n".join(json.dumps(r) for r in recs) + "\n", encoding="utf-8")
    rel = f"-home-mm-proj-app/{sid}.jsonl"
    rc, out = _sh(sr.build_fetch_script([rel], str(root),
                                        head_bytes=2000, tail_bytes=600))
    assert rc == 0
    body = sr.parse_fetch_output(out)[rel]
    cache = Path(tempfile.mkdtemp()) / "-home-mm-proj-app"
    cache.mkdir(parents=True)
    (cache / f"{sid}.jsonl").write_text(body + "\n", encoding="utf-8")
    s = saikai.parse_session(cache / f"{sid}.jsonl")
    assert s is not None
    assert s["first_ts"] == "2026-07-13T00:00:00.000Z"
    assert s["last_ts"] == "2026-07-13T09:00:00.000Z"     # tail reached
    assert s["ai_title"] == "Fleet parse probe"
    assert s["origin_cwd"] == "/home/mm/proj/app"


# ── RemoteFetcher with a fake runner ─────────────────────────────────────────

def _scan_text(files: dict, live: list) -> str:
    lines = ["===FILES==="]
    lines += [f"{rel}\t{sz}\t{mt}" for rel, (sz, mt) in files.items()]
    lines.append("===SESS===")
    lines += ["1\t" + json.dumps(d) for d in live]
    lines.append("===END===")
    return "\n".join(lines) + "\n"


def _fetch_text(bodies: dict) -> str:
    parts = []
    for rel, body in bodies.items():
        parts.append(f"===F {len(body)} {rel}===")
        parts.append(body)
        parts.append("===EOF===")
    return "\n".join(parts) + "\n"


def test_fetcher_tick_updates_cache_and_is_idempotent():
    cache = Path(tempfile.mkdtemp(prefix="fleet-cache-"))
    fx = sr.RemoteFetcher("pi", "mm@pi", [], cache)
    sid = str(uuid.uuid4())
    rel = f"-home-mm-app/{sid}.jsonl"
    body = '{"type":"user","timestamp":"2026-07-13T01:00:00.000Z"}'
    mt = 1783900000.25
    scan = _scan_text({rel: (len(body) + 1, mt)},
                      [{"pid": 7, "sessionId": sid, "status": "idle",
                        "kind": "interactive"}])
    calls = []

    def runner(argv):
        calls.append(argv)
        if "while IFS= read" in argv[-1]:
            return 0, _fetch_text({rel: body})
        return 0, scan

    assert fx.tick(runner) is True
    dst = cache / "pi" / "projects" / rel
    assert dst.is_file() and dst.read_text().startswith('{"type":"user"')
    assert abs(dst.stat().st_mtime - mt) < 0.01       # remote recency restored
    assert fx.registry() == {sid: {"status": "idle", "kind": "interactive"}}
    assert fx.stale_after(60) is False
    # BatchMode is non-negotiable for a daemon-thread poller
    assert all("BatchMode=yes" in " ".join(a) for a in calls)

    man = cache / "pi" / "manifest.json"
    m1 = man.stat().st_mtime_ns
    n_calls = len(calls)
    assert fx.tick(runner) is True                    # nothing changed
    assert man.stat().st_mtime_ns == m1, "unchanged manifest must not be rewritten"
    assert len(calls) == n_calls + 1, "no-change tick must be scan-only (no fetch)"


def test_fetcher_down_host_keeps_cache_and_reports_false():
    cache = Path(tempfile.mkdtemp(prefix="fleet-cache-"))
    fx = sr.RemoteFetcher("pi", "mm@pi", [], cache)
    sid = str(uuid.uuid4())
    rel = f"-x/{sid}.jsonl"
    ok_scan = _scan_text({rel: (10, 1783900001.0)}, [])

    def up(argv):
        s = argv[-1]
        return (0, _fetch_text({rel: '{"a":1}'})) if "while IFS= read" in s else (0, ok_scan)

    assert fx.tick(up) is True
    assert (cache / "pi" / "projects" / rel).is_file()

    def down(argv):
        return 255, ""

    assert fx.tick(down) is False
    assert (cache / "pi" / "projects" / rel).is_file(), "cache must survive an outage"
    # truncated scan (ssh died mid-stream) must not mass-delete either
    def truncated(argv):
        return 0, ok_scan.replace("===END===\n", "").replace(rel, "")

    assert fx.tick(truncated) is False
    assert (cache / "pi" / "projects" / rel).is_file()


def test_fetcher_deletion_and_vanished_fetch_retry():
    cache = Path(tempfile.mkdtemp(prefix="fleet-cache-"))
    fx = sr.RemoteFetcher("pi", "mm@pi", [], cache)
    rel_a, rel_b = "-x/a.jsonl", "-x/b.jsonl"
    scan1 = _scan_text({rel_a: (5, 1.0), rel_b: (5, 2.0)}, [])

    def r1(argv):
        s = argv[-1]
        if "while IFS= read" in s:
            return 0, _fetch_text({rel_a: '{"a":1}'})   # b vanished mid-tick
        return 0, scan1

    assert fx.tick(r1) is True
    assert (cache / "pi" / "projects" / rel_a).is_file()
    assert rel_b not in fx._manifest(), "unfetched file must be retried next tick"

    scan2 = _scan_text({rel_b: (5, 2.0)}, [])           # a deleted on the remote

    def r2(argv):
        s = argv[-1]
        if "while IFS= read" in s:
            return 0, _fetch_text({rel_b: '{"b":1}'})
        return 0, scan2

    assert fx.tick(r2) is True
    assert not (cache / "pi" / "projects" / rel_a).exists(), "remote deletion propagates"
    assert (cache / "pi" / "projects" / rel_b).is_file()


def test_fetcher_stale_clock():
    cache = Path(tempfile.mkdtemp(prefix="fleet-cache-"))
    fx = sr.RemoteFetcher("pi", "mm@pi", [], cache)
    assert fx.stale_after(1) is True                    # never fetched
    (fx.dir).mkdir(parents=True, exist_ok=True)
    (fx.dir / "last-ok").touch()
    assert fx.stale_after(60) is False
    old = time.time() - 3600
    os.utime(fx.dir / "last-ok", (old, old))
    assert fx.stale_after(60) is True


# ── saikai.py wiring ─────────────────────────────────────────────────────────

import saikai


def _with_remotes(toml_text: str) -> None:
    f = _FAKE_HOME / f"fleet-{uuid.uuid4().hex[:8]}.toml"
    f.write_text(toml_text, encoding="utf-8")
    os.environ["SAIKAI_CONFIG"] = str(f)
    saikai._reset_config_cache()


def _clear_config() -> None:
    os.environ.pop("SAIKAI_CONFIG", None)
    saikai._reset_config_cache()


def _seed_fleet_cache(name: str, sids: "list[str]", live: "list | None" = None,
                      cwd: str = "/data/mm/webapp") -> None:
    """Populate CACHE_DIR/remote/<name> exactly the way ONE fetcher tick would:
    real claude-schema transcripts + registry, via RemoteFetcher itself."""
    fx = sr.RemoteFetcher(name, f"mm@{name}", [], saikai.CACHE_DIR / "remote")
    bodies, files = {}, {}
    for sid in sids:
        rel = f"-data-mm-webapp/{sid}.jsonl"
        recs = [
            {"type": "ai-title", "aiTitle": f"Fleet work on {name}",
             "timestamp": "2026-07-13T01:00:00.000Z", "cwd": cwd},
            {"type": "user", "timestamp": "2026-07-13T01:01:00.000Z", "cwd": cwd,
             "message": {"content": "remote fleet prompt long enough"}},
        ]
        bodies[rel] = "\n".join(json.dumps(r) for r in recs)
        files[rel] = (len(bodies[rel]) + 1, 1783990000.0)
    scan = _scan_text(files, live or [])

    def runner(argv):
        if "while IFS= read" in argv[-1]:
            return 0, _fetch_text(bodies)
        return 0, scan

    assert fx.tick(runner) is True


def test_load_fleet_sessions_tags_rows_and_registry_liveness():
    _with_remotes('[remotes]\npi = { host = "mm@pi" }\n')
    try:
        open_sid, bg_sid = str(uuid.uuid4()), str(uuid.uuid4())
        _seed_fleet_cache("pi", [open_sid, bg_sid],
                          live=[{"pid": 5, "sessionId": open_sid,
                                 "status": "busy", "kind": "interactive"},
                                {"pid": 6, "sessionId": bg_sid,
                                 "status": "running", "kind": "bg"}])
        rows = {s["id"]: s for s in saikai.load_fleet_sessions()}
        assert set(rows) == {open_sid, bg_sid}, rows
        r = rows[open_sid]
        assert r["remote_name"] == "pi" and r["remote_origin"] is True
        assert r["is_open"] is True and r["session_status"] == "busy"
        assert r["_fleet_stale"] is False
        assert r["ai_title"] == "Fleet work on pi"
        # a REMOTE bg session must not read as attachable-open here
        assert not rows[bg_sid].get("is_open")
        # host goes quiet → rows degrade to the stale snapshot
        old = time.time() - 7200
        fx = sr.RemoteFetcher("pi", "mm@pi", [], saikai.CACHE_DIR / "remote")
        os.utime(fx.dir / "last-ok", (old, old))
        rows2 = {s["id"]: s for s in saikai.load_fleet_sessions()}
        assert rows2[open_sid]["_fleet_stale"] is True
    finally:
        _clear_config()


def test_remote_resume_target_by_fleet_name_needs_no_prefix():
    _with_remotes('[remotes]\npi = { host = "mm@pi", ssh_args = ["-p", 2299] }\n')
    try:
        row = {"id": "x", "remote_name": "pi", "remote_origin": True,
               "origin_cwd": "/data/anywhere"}
        t = saikai._remote_resume_target(row)
        assert t is not None and t.host == "mm@pi" and t.ssh_args == ["-p", "2299"]
        # unknown name → no target (row was cached from a since-removed remote)
        assert saikai._remote_resume_target(
            {"id": "x", "remote_name": "gone", "remote_origin": True}) is None
        # and the resume argv routes over ssh with the entry's ssh_args
        sid = str(uuid.uuid4())
        sessions = [{"id": sid, "remote_name": "pi", "remote_origin": True,
                     "origin_cwd": "/data/mm/webapp", "cwd": "/data/mm/webapp"}]
        argv, _cwd, _env = saikai._build_resume_invocation(sid, sessions)
        assert argv[:5] == ["ssh", "-p", "2299", "-t", "mm@pi"], argv
        assert "claude --resume" in argv[5]
    finally:
        _clear_config()


def test_dedup_prefers_fleet_row_over_desktop_mirror():
    sid = str(uuid.uuid4())
    mirror = {"id": sid, "mtime": 999.0, "remote_origin": True}      # fresher mtime…
    fleet = {"id": sid, "mtime": 100.0, "remote_name": "pi",
             "remote_origin": True}
    for order in ([mirror, fleet], [fleet, mirror]):
        kept = saikai._dedup_sessions_by_id(list(order))
        assert len(kept) == 1 and kept[0].get("remote_name") == "pi", order
    # plain same-sid duplicates keep the newest-mtime rule (#H2)
    a, b = {"id": sid, "mtime": 1.0}, {"id": sid, "mtime": 2.0}
    assert saikai._dedup_sessions_by_id([a, b])[0]["mtime"] == 2.0


def test_pilot_fleet_row_lists_with_host_badge_and_resumes():
    try:
        from textual.app import App  # noqa: F401
    except Exception:
        print("SKIP test_pilot_fleet_row_lists_with_host_badge_and_resumes (textual unavailable)")
        return

    import asyncio
    from textual.app import App

    _with_remotes('[remotes]\npi = { host = "mm@testpi" }\n')
    sid = str(uuid.uuid4())
    _seed_fleet_cache("pi", [sid])        # dormant on the remote (no live entry)

    facts: dict = {"argvs": []}
    real_build = saikai._build_resume_invocation

    def spy(s, sessions):
        facts["argvs"].append(real_build(s, sessions)[0])
        raise RuntimeError("stop-before-spawn (test)")

    def fake_run(self, *a, **kw):
        async def go():
            async with self.run_test(size=(120, 32)) as pilot:
                await pilot.pause(0.5)
                idx = getattr(self, "_sid_index", {})
                facts["listed"] = sid in idx and idx[sid].get("remote_name") == "pi"
                table = self.query_one("#table")
                titles = []
                for rk in list(getattr(table, "rows", {})):
                    try:
                        titles.append(str(table.get_row(rk)[-1]))
                    except Exception:
                        pass
                facts["badged"] = any("⟨pi⟩" in t for t in titles)
                self._open_or_attach_live(sid)
                await pilot.pause(0.2)
        asyncio.run(go())

    orig_run, App.run = App.run, fake_run
    orig_argv = sys.argv
    saikai._build_resume_invocation = spy
    try:
        sys.argv = ["saikai", "--all"]
        saikai.main()
    finally:
        saikai._build_resume_invocation = real_build
        App.run = orig_run
        sys.argv = orig_argv
        _clear_config()

    assert facts.get("listed"), facts
    assert facts.get("badged"), f"fleet row must carry the ⟨pi⟩ badge: {facts}"
    assert len(facts["argvs"]) == 1, facts
    argv = facts["argvs"][0]
    assert argv[:3] == ["ssh", "-t", "mm@testpi"], argv
    assert f"claude --resume {sid}" in argv[3], argv


if __name__ == "__main__":
    test_root_expr_home_expansion()
    print("PASS test_root_expr_home_expansion")
    test_scan_script_lists_files_and_filters_dead_registry()
    print("PASS test_scan_script_lists_files_and_filters_dead_registry")
    test_parse_scan_output_synthetic_live_and_truncation()
    print("PASS test_parse_scan_output_synthetic_live_and_truncation")
    test_diff_manifest()
    print("PASS test_diff_manifest")
    test_fetch_script_small_whole_and_big_stitched()
    print("PASS test_fetch_script_small_whole_and_big_stitched")
    test_stitched_partial_still_parses_as_session()
    print("PASS test_stitched_partial_still_parses_as_session")
    test_fetcher_tick_updates_cache_and_is_idempotent()
    print("PASS test_fetcher_tick_updates_cache_and_is_idempotent")
    test_fetcher_down_host_keeps_cache_and_reports_false()
    print("PASS test_fetcher_down_host_keeps_cache_and_reports_false")
    test_fetcher_deletion_and_vanished_fetch_retry()
    print("PASS test_fetcher_deletion_and_vanished_fetch_retry")
    test_fetcher_stale_clock()
    print("PASS test_fetcher_stale_clock")
    test_load_fleet_sessions_tags_rows_and_registry_liveness()
    print("PASS test_load_fleet_sessions_tags_rows_and_registry_liveness")
    test_remote_resume_target_by_fleet_name_needs_no_prefix()
    print("PASS test_remote_resume_target_by_fleet_name_needs_no_prefix")
    test_dedup_prefers_fleet_row_over_desktop_mirror()
    print("PASS test_dedup_prefers_fleet_row_over_desktop_mirror")
    test_pilot_fleet_row_lists_with_host_badge_and_resumes()
    print("PASS test_pilot_fleet_row_lists_with_host_badge_and_resumes")
    print("ALL PASS")
