"""Explicit named snapshots, independent of the disposable open-pane cache."""
from __future__ import annotations

import copy
import errno
import json
import os
import tempfile
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Callable, Literal

ProviderId = Literal["claude", "codex", "agy"]
ResumeMode = Literal["new", "id", "picker", "continue"]


@dataclass(frozen=True)
class ResumeTarget:
    mode: ResumeMode
    session_id: str | None = None


@dataclass(frozen=True)
class Project:
    id: str
    host_id: str
    cwd: str
    title: str
    last_provider: ProviderId | None
    targets: dict[str, ResumeTarget]


@dataclass(frozen=True)
class WorksetEntry:
    id: str
    host_id: str
    cwd: str
    provider: ProviderId
    target: ResumeTarget
    title: str


@dataclass(frozen=True)
class Workset:
    id: str
    name: str
    entries: tuple[WorksetEntry, ...]


@dataclass(frozen=True)
class WorkspaceState:
    schema_version: int
    revision: int
    host_id: str
    projects: tuple[Project, ...]
    worksets: tuple[Workset, ...]


class StoreCorruptError(ValueError):
    pass


class StoreVersionError(ValueError):
    pass


class StoreConflictError(RuntimeError):
    pass


class StoreBusyError(TimeoutError):
    pass


def _fields(value, names):
    if not isinstance(value, dict) or set(value) != set(names.split()):
        raise StoreCorruptError("Unexpected workspace fields")
    return value


def _text(value, *, empty=False):
    if not isinstance(value, str) or (not empty and not value.strip()) or any(
            ord(c) < 32 or 127 <= ord(c) < 160 for c in value):
        raise StoreCorruptError("Invalid workspace text")
    return value


def _provider(value):
    if not isinstance(value, str) or value not in ("claude", "codex", "agy"):
        raise StoreCorruptError("Unknown provider")
    return value


def _target(value):
    v = _fields(value, "mode session_id")
    mode = v["mode"]
    if mode not in ("new", "id", "picker", "continue"):
        raise StoreCorruptError("Unknown resume mode")
    sid = v["session_id"]
    if mode == "id":
        _text(sid)
    elif sid is not None:
        raise StoreCorruptError("Only id mode accepts a session ID")
    return ResumeTarget(mode, sid)


def _items(value, parse):
    if not isinstance(value, (list, tuple)):
        raise StoreCorruptError("Expected workspace array")
    items = tuple(parse(v) for v in value)
    if len({v.id for v in items}) != len(items):
        raise StoreCorruptError("Duplicate workspace ID")
    return items


def _entry(value):
    v = _fields(value, "id host_id cwd provider target title")
    return WorksetEntry(_text(v["id"]), _text(v["host_id"]), _text(v["cwd"]),
                        _provider(v["provider"]), _target(v["target"]),
                        _text(v["title"], empty=True))


def _workset(value):
    v = _fields(value, "id name entries")
    name = _text(v["name"]).strip()
    if len(name) > 80:
        raise StoreCorruptError("Workset names must be 1–80 characters")
    return Workset(_text(v["id"]), name, _items(v["entries"], _entry))


def _project(value):
    v = _fields(value, "id host_id cwd title last_provider targets")
    provider = v["last_provider"]
    if provider is not None:
        _provider(provider)
    if not isinstance(v["targets"], dict):
        raise StoreCorruptError("Expected provider targets")
    targets = {_provider(k): _target(t) for k, t in v["targets"].items()}
    return Project(_text(v["id"]), _text(v["host_id"]), _text(v["cwd"]),
                   _text(v["title"], empty=True), provider, targets)


def _state(value):
    if not isinstance(value, dict):
        raise StoreCorruptError("Expected workspace object")
    version = value.get("schema_version")
    if type(version) is not int:
        raise StoreCorruptError("Invalid schema version")
    if version != 1:
        raise StoreVersionError(f"Unsupported workspace schema: {version}")
    v = _fields(value, "schema_version revision host_id projects worksets")
    if type(v["revision"]) is not int or v["revision"] < 0:
        raise StoreCorruptError("Invalid workspace revision")
    sets = _items(v["worksets"], _workset)
    if len({s.name for s in sets}) != len(sets):
        raise StoreCorruptError("A workset with that name already exists")
    return WorkspaceState(version, v["revision"], _text(v["host_id"]),
                          _items(v["projects"], _project), sets)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise StoreCorruptError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


class WorkspaceStore:
    def __init__(self, path: Path):
        self.path = Path(path)

    def read(self) -> WorkspaceState:
        """Read without creating files or replacing malformed data."""
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"),
                               object_pairs_hook=_unique_object)
        except (json.JSONDecodeError, UnicodeError) as exc:
            raise StoreCorruptError("Cannot read workspace JSON") from exc
        return _state(value)

    @contextmanager
    def _lock(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.with_suffix(".lock").open("a+b") as handle:
            if os.name == "nt":
                import msvcrt
                # Locking past EOF is supported; ensure the byte exists without
                # overwriting another holder's byte on subsequent acquisitions.
                if os.fstat(handle.fileno()).st_size == 0:
                    handle.write(b"\0")
                    handle.flush()
                def acquire():
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                def release():
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                def acquire():
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                def release():
                    fcntl.flock(handle, fcntl.LOCK_UN)
            deadline = time.monotonic() + 2.0
            while True:
                try:
                    acquire()
                    break
                except OSError as exc:
                    if exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                        raise
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise StoreBusyError("Workspace is busy; try again") from exc
                    time.sleep(min(0.05, remaining))
            try:
                yield
            finally:
                release()

    def _write(self, state: WorkspaceState):
        name = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", suffix=".tmp",
                                             dir=self.path.parent, delete=False) as f:
                name = f.name
                json.dump(asdict(state), f, ensure_ascii=False, indent=2, allow_nan=False)
                f.write("\n")
                f.flush()
                os.fsync(f.fileno())
            os.replace(name, self.path)
        finally:
            if name is not None:
                Path(name).unlink(missing_ok=True)

    def initialize(self) -> WorkspaceState:
        with self._lock():
            try:
                return self.read()
            except FileNotFoundError:
                state = WorkspaceState(1, 0, str(uuid.uuid4()), (), ())
                self._write(state)
                return state

    def update(self, expected_revision: int,
               change: Callable[[WorkspaceState], WorkspaceState]) -> WorkspaceState:
        with self._lock():
            current = self.read()
            if type(expected_revision) is not int or current.revision != expected_revision:
                raise StoreConflictError("Worksets changed; reload and try again")
            candidate = change(copy.deepcopy(current))
            if not isinstance(candidate, WorkspaceState):
                raise StoreCorruptError("Expected WorkspaceState")
            candidate = _state(asdict(candidate))
            if (candidate.host_id, candidate.revision) != (current.host_id, current.revision):
                raise StoreCorruptError("Cannot change workspace host or revision")
            result = replace(candidate, revision=current.revision + 1)
            self._write(result)
            return result
