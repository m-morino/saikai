"""Remote-roots phase 2 (docs/design/remote-roots.md): a Desktop-SSH mirror
session (projects/ssh-*) whose foreign cwd matches a configured [remotes]
prefix is resumed WHERE IT RAN, via

    ssh -t <host> 'cd <cwd> && exec claude --resume <sid>'

— the pane machinery doesn't care that the child is ssh. Unmatched mirrors
keep the explanatory block toast.

Unit: [remotes] parsing + cwd-prefix host mapping (path-prefix, first-wins).
Pilot (textual): Enter-path on a mapped remote row builds the ssh argv
instead of being blocked; an unmapped one still blocks with the toast.

Run:  python tests/test_remote_roots.py
"""
import json
import os
import shlex
import sys
import tempfile
import uuid

# Isolate from a developer's ambient SAIKAI_MIRROR (same reasoning as
# tests/test_keyboard_leader.py:14-17).
os.environ.pop("SAIKAI_MIRROR", None)
from pathlib import Path

# Point saikai at a throwaway home BEFORE importing it (it derives CACHE_DIR /
# state files from Path.home() at import time).
_FAKE_HOME = Path(tempfile.mkdtemp(prefix="saikai-remote-test-"))
for _var in ("USERPROFILE", "HOME", "APPDATA", "LOCALAPPDATA", "XDG_CONFIG_HOME"):
    os.environ[_var] = str(_FAKE_HOME)
os.environ.pop("SAIKAI_CONFIG", None)
os.environ["SAIKAI_SUMMARIZE_ENABLED"] = "0"
os.environ["SAIKAI_NO_TERMINAL_WATCHDOG"] = "1"

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import saikai


def _with_remotes(toml_text: str) -> None:
    """Point SAIKAI_CONFIG at a fresh file with the given content."""
    f = _FAKE_HOME / f"remotes-{uuid.uuid4().hex[:8]}.toml"
    f.write_text(toml_text, encoding="utf-8")
    os.environ["SAIKAI_CONFIG"] = str(f)
    saikai._reset_config_cache()


def _clear_config() -> None:
    os.environ.pop("SAIKAI_CONFIG", None)
    saikai._reset_config_cache()


REMOTES = (
    "[remotes]\n"
    'pi  = { host = "mm@192.168.11.4", cwd_prefixes = ["/home/mm", "/opt"] }\n'
    'nuc = { host = "mm@nuc",          cwd_prefixes = ["/home/mm/srv", "/srv"] }\n'
)


def test_remotes_map_parses_in_declaration_order():
    _with_remotes(REMOTES)
    try:
        m = saikai._remotes_map()
        assert [e.name for e in m] == ["pi", "nuc"], m
        assert m[0].host == "mm@192.168.11.4" and m[0].prefixes == ["/home/mm", "/opt"]
        assert m[1].host == "mm@nuc"
        assert m[0].ssh_args == []            # optional, defaults empty
        assert m[0].discover is True          # phase 3: fleet by default …
        assert m[0].config_root == "~/.claude"
    finally:
        _clear_config()


def test_remotes_map_skips_malformed_entries():
    # Since phase 3, cwd_prefixes are OPTIONAL (a fleet remote resumes by
    # name) — host-only entries are now VALID; relative prefixes are still
    # dropped from the list. Broken host / ssh_args still reject the entry.
    _with_remotes(
        "[remotes]\n"
        'bad1 = "just-a-string"\n'
        'bad2 = { host = "", cwd_prefixes = ["/x"] }\n'
        'ok3  = { host = "mm@h" }\n'
        'ok4  = { host = "mm@h", cwd_prefixes = ["rel/only"] }\n'
        'bad5 = { host = "mm@h", cwd_prefixes = ["/x"], ssh_args = "not-a-list" }\n'
        'bad6 = { host = "mm@h", cwd_prefixes = ["/x"], ssh_args = [["nested"]] }\n'
        'bad7 = { host = "mm@h", cwd_prefixes = "not-a-list" }\n'
        'good = { host = "mm@h", cwd_prefixes = ["rel/dropped", "/abs/"], '
        'ssh_args = ["-p", 2222], discover = false, config_root = "/opt/cc" }\n')
    try:
        m = saikai._remotes_map()
        assert [e.name for e in m] == ["ok3", "ok4", "good"], m
        assert m[0].prefixes == [] and m[1].prefixes == []
        g = m[2]
        # trailing slash normalised away; the relative prefix dropped
        assert g.prefixes == ["/abs"], m
        assert g.ssh_args == ["-p", "2222"]   # TOML int coerced to str
        assert g.discover is False and g.config_root == "/opt/cc"
    finally:
        _clear_config()


def test_remotes_map_empty_without_config():
    _clear_config()
    assert saikai._remotes_map() == []
    assert saikai._remote_for_cwd("/home/mm") is None


def test_remote_for_cwd_is_a_path_prefix_not_a_string_prefix():
    _with_remotes(REMOTES)
    try:
        assert saikai._remote_for_cwd("/home/mm").name == "pi"          # exact
        assert saikai._remote_for_cwd("/home/mm/work/x").name == "pi"   # child
        assert saikai._remote_for_cwd("/home/mmx") is None              # NOT /home/mm*
        assert saikai._remote_for_cwd("/srv/app").name == "nuc"
        assert saikai._remote_for_cwd("/etc") is None
        assert saikai._remote_for_cwd(None) is None
        assert saikai._remote_for_cwd("") is None
        # declaration order wins: /home/mm/srv/x hits pi's /home/mm first —
        # users must declare more-specific remotes first.
        assert saikai._remote_for_cwd("/home/mm/srv/x").name == "pi"
    finally:
        _clear_config()


def test_remote_for_cwd_root_prefix_catches_all_absolute():
    _with_remotes('[remotes]\nany = { host = "mm@h", cwd_prefixes = ["/"] }\n')
    try:
        r = saikai._remote_for_cwd("/anything/at/all")
        assert (r.name, r.host) == ("any", "mm@h")
        assert saikai._remote_for_cwd("relative/path") is None
    finally:
        _clear_config()


def test_remote_resume_target_requires_remote_origin():
    _with_remotes(REMOTES)
    try:
        # a LOCAL session whose cwd happens to match a prefix must never ssh
        assert saikai._remote_resume_target({"id": "x", "cwd": "/home/mm/w"}) is None
        mirrored = {"id": "x", "remote_origin": True, "origin_cwd": "/home/mm/w"}
        assert saikai._remote_resume_target(mirrored).host == "mm@192.168.11.4"
        # origin_cwd wins over cwd; falls back to cwd when origin_cwd missing
        assert saikai._remote_resume_target(
            {"id": "x", "remote_origin": True, "cwd": "/srv/a"}).name == "nuc"
        assert saikai._remote_resume_target(
            {"id": "x", "remote_origin": True, "origin_cwd": "/data/unmapped"}) is None
        assert saikai._remote_resume_target(None) is None
    finally:
        _clear_config()


def _remote_cmd(cwd: "str | None", sid: str) -> str:
    """The expected pane command: login shell (reads ~/.profile → ~/.local/bin
    on PATH) wrapping `cd <cwd> && exec claude --resume <sid>`."""
    inner = (f"cd {shlex.quote(cwd)} && " if cwd else "") + \
        f"exec claude --resume {shlex.quote(sid)}"
    return f"exec bash -lc {shlex.quote(inner)}"


def test_build_resume_invocation_remote_argv_and_quoting():
    _with_remotes(REMOTES)
    try:
        sid = str(uuid.uuid4())
        evil = "/home/mm/w s'p; $(rm -rf ~)"   # must reach the remote sh as ONE word
        sessions = [{"id": sid, "remote_origin": True,
                     "origin_cwd": evil, "cwd": evil}]
        argv, cwd, env = saikai._build_resume_invocation(sid, sessions)
        assert argv[:3] == ["ssh", "-t", "mm@192.168.11.4"], argv
        assert argv[3] == _remote_cmd(evil, sid), argv[3]
        assert cwd is None            # the LOCAL ssh process just inherits saikai's cwd
        assert "TEXTUAL_LOG" not in env   # child-env hygiene applies to ssh too
    finally:
        _clear_config()


def test_build_resume_invocation_remote_ssh_args_inserted():
    _with_remotes('[remotes]\nany = { host = "mm@h", cwd_prefixes = ["/"], '
                  'ssh_args = ["-p", 2299, "-i", "/tmp/k"] }\n')
    try:
        sid = str(uuid.uuid4())
        sessions = [{"id": sid, "remote_origin": True, "origin_cwd": "/d/x"}]
        argv, _cwd, _env = saikai._build_resume_invocation(sid, sessions)
        assert argv[:7] == ["ssh", "-p", "2299", "-i", "/tmp/k", "-t", "mm@h"], argv
        assert argv[7] == _remote_cmd("/d/x", sid)
    finally:
        _clear_config()


def test_build_resume_invocation_remote_without_cwd_still_resumes():
    _with_remotes('[remotes]\nany = { host = "mm@h", cwd_prefixes = ["/"] }\n')
    try:
        sid = str(uuid.uuid4())
        # a mirror whose records carried no usable cwd at all:
        sessions = [{"id": sid, "remote_origin": True, "origin_cwd": "/d/x"}]
        argv, _cwd, _env = saikai._build_resume_invocation(sid, sessions)
        assert argv[3] == _remote_cmd("/d/x", sid)
    finally:
        _clear_config()


def test_build_resume_invocation_local_sessions_unchanged():
    _with_remotes(REMOTES)   # config present, but the session is NOT remote_origin
    try:
        sid = str(uuid.uuid4())
        sessions = [{"id": sid, "cwd": "/home/mm/work"}]
        argv, _cwd, env = saikai._build_resume_invocation(sid, sessions)
        assert argv[0] != "ssh" and "--resume" in argv and sid in argv, argv
        assert env.get("SAIKAI_RESUME") == "1"
    finally:
        _clear_config()


def test_marker_legend_reflects_mapped_remote():
    _with_remotes(REMOTES)
    try:
        mapped = {"id": "x", "remote_origin": True, "origin_cwd": "/home/mm/w"}
        unmapped = {"id": "y", "remote_origin": True, "origin_cwd": "/data/z"}
        lm = " ".join(saikai._marker_legend(mapped, set(), set()))
        lu = " ".join(saikai._marker_legend(unmapped, set(), set()))
        assert "ssh" in lm and "pi" in lm, lm          # names the mapped remote
        assert "not resumable here" in lu, lu          # unmapped keeps the block wording
    finally:
        _clear_config()


# ── Pilot: the Enter path (gate + spawn) against the real PickerApp ─────────

def _write_mirror_session(cwd: str, title: str) -> str:
    """A session under projects/ssh-<uuid>/ — parse_session flags it
    remote_origin from the dir name alone (#remote-origin)."""
    sid = str(uuid.uuid4())
    pdir = _FAKE_HOME / ".claude" / "projects" / f"ssh-{uuid.uuid4()}"
    pdir.mkdir(parents=True, exist_ok=True)
    recs = [
        {"type": "ai-title", "aiTitle": title,
         "timestamp": "2026-07-10T00:00:00.000Z", "cwd": cwd},
        {"type": "user", "timestamp": "2026-07-10T00:01:00.000Z", "cwd": cwd,
         "message": {"content": f"remote work in {cwd}"}},
    ]
    (pdir / f"{sid}.jsonl").write_text(
        "\n".join(json.dumps(r) for r in recs) + "\n", encoding="utf-8")
    return sid


def test_pilot_remote_resume_gate_flips_only_for_mapped():
    """Enter-path behavior on remote_origin rows: a [remotes]-mapped one reaches
    _build_resume_invocation (ssh argv), an unmapped one is blocked with the
    explanatory toast BEFORE any spawn."""
    try:
        from textual.app import App  # noqa: F401
    except Exception:
        print("SKIP test_pilot_remote_resume_gate_flips_only_for_mapped (textual unavailable)")
        return

    import asyncio
    from textual.app import App

    mapped_sid = _write_mirror_session("/data/mm/app", "Mapped remote work")
    unmapped_sid = _write_mirror_session("/elsewhere/x", "Unmapped remote work")
    _with_remotes('[remotes]\npi = { host = "mm@testpi", cwd_prefixes = ["/data"] }\n')

    facts: dict = {"argvs": [], "toasts": []}
    real_build = saikai._build_resume_invocation

    def spy(sid, sessions):
        # capture the REAL invocation, then stop before an actual ssh spawn
        facts["argvs"].append(real_build(sid, sessions)[0])
        raise RuntimeError("stop-before-spawn (test)")

    def fake_run(self, *a, **kw):
        async def go():
            async with self.run_test(size=(110, 30)) as pilot:
                await pilot.pause(0.5)
                _orig_notify = self.notify

                def note(msg, *na, **nk):
                    facts["toasts"].append(f"{nk.get('title', '')}: {msg}")
                    return _orig_notify(msg, *na, **nk)
                self.notify = note
                self._open_or_attach_live(mapped_sid)     # same method Enter uses
                await pilot.pause(0.2)
                facts["after_mapped"] = len(facts["argvs"])
                self._open_or_attach_live(unmapped_sid)
                await pilot.pause(0.2)
                facts["after_unmapped"] = len(facts["argvs"])
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

    assert facts.get("after_mapped") == 1, f"mapped row must reach the builder: {facts}"
    argv = facts["argvs"][0]
    assert argv[:3] == ["ssh", "-t", "mm@testpi"], argv
    assert f"exec claude --resume {mapped_sid}" in argv[3], argv
    assert argv[3].startswith("exec bash -lc "), argv
    assert facts.get("after_unmapped") == 1, f"unmapped row must NOT spawn: {facts}"
    assert any("remote session" in t for t in facts["toasts"]), facts["toasts"]


if __name__ == "__main__":
    test_remotes_map_parses_in_declaration_order()
    print("PASS test_remotes_map_parses_in_declaration_order")
    test_remotes_map_skips_malformed_entries()
    print("PASS test_remotes_map_skips_malformed_entries")
    test_remotes_map_empty_without_config()
    print("PASS test_remotes_map_empty_without_config")
    test_remote_for_cwd_is_a_path_prefix_not_a_string_prefix()
    print("PASS test_remote_for_cwd_is_a_path_prefix_not_a_string_prefix")
    test_remote_for_cwd_root_prefix_catches_all_absolute()
    print("PASS test_remote_for_cwd_root_prefix_catches_all_absolute")
    test_remote_resume_target_requires_remote_origin()
    print("PASS test_remote_resume_target_requires_remote_origin")
    test_build_resume_invocation_remote_argv_and_quoting()
    print("PASS test_build_resume_invocation_remote_argv_and_quoting")
    test_build_resume_invocation_remote_ssh_args_inserted()
    print("PASS test_build_resume_invocation_remote_ssh_args_inserted")
    test_build_resume_invocation_remote_without_cwd_still_resumes()
    print("PASS test_build_resume_invocation_remote_without_cwd_still_resumes")
    test_build_resume_invocation_local_sessions_unchanged()
    print("PASS test_build_resume_invocation_local_sessions_unchanged")
    test_marker_legend_reflects_mapped_remote()
    print("PASS test_marker_legend_reflects_mapped_remote")
    test_pilot_remote_resume_gate_flips_only_for_mapped()
    print("PASS test_pilot_remote_resume_gate_flips_only_for_mapped")
    print("ALL PASS")
