"""Agent-specific launch contracts for saikai.

Providers describe differences between agent CLIs. Application policy such as
cwd recovery, permission choices, history caching, and UI behavior stays in
saikai.py; PTY mechanics stay in saikai_terminal.py.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
import os
from pathlib import Path
import shutil
from typing import Mapping, Sequence


@dataclass(frozen=True)
class ProviderCapabilities:
    can_resume: bool = True
    can_create: bool = True
    can_preassign_id: bool = False
    has_reliable_live_status: bool = False
    has_transcript_changes: bool = False
    has_desktop_sync: bool = False


@dataclass(frozen=True)
class LaunchSpec:
    argv: list[str]
    cwd: str | None
    env: dict[str, str]
    session_id: str | None


class AgentProvider(ABC):
    id: str
    display_name: str
    executable_name: str
    status_profile: str = "generic"
    history_format: str
    capabilities = ProviderCapabilities()

    @abstractmethod
    def history_roots(
        self,
        *,
        home: Path | None = None,
        env: Mapping[str, str] | None = None,
    ) -> list[Path]:
        raise NotImplementedError

    def resolve_executable(self, env: Mapping[str, str]) -> str:
        return shutil.which(self.executable_name, path=env.get("PATH")) or self.executable_name

    @abstractmethod
    def build_resume(
        self,
        session_id: str,
        *,
        cwd: str | None,
        env: Mapping[str, str],
        extra_args: Sequence[str] = (),
        executable: str | None = None,
    ) -> LaunchSpec:
        raise NotImplementedError

    @abstractmethod
    def build_new(
        self,
        *,
        cwd: str | None,
        requested_id: str | None,
        env: Mapping[str, str],
        extra_args: Sequence[str] = (),
        executable: str | None = None,
    ) -> LaunchSpec:
        raise NotImplementedError

    def _spec(
        self,
        args: Sequence[str],
        *,
        cwd: str | None,
        env: Mapping[str, str],
        session_id: str | None,
        executable: str | None,
    ) -> LaunchSpec:
        child_env = dict(env)
        binary = executable or self.resolve_executable(child_env)
        return LaunchSpec([binary, *args], cwd, child_env, session_id)


class ClaudeProvider(AgentProvider):
    id = "claude"
    display_name = "Claude Code"
    executable_name = "claude"
    status_profile = "claude"
    history_format = "claude-project-jsonl"
    capabilities = ProviderCapabilities(
        can_preassign_id=True,
        has_reliable_live_status=True,
        has_transcript_changes=True,
        has_desktop_sync=True,
    )

    def history_roots(
        self,
        *,
        home: Path | None = None,
        env: Mapping[str, str] | None = None,
    ) -> list[Path]:
        env_map = os.environ if env is None else env
        root = Path(env_map.get("CLAUDE_CONFIG_DIR") or ((home or Path.home()) / ".claude"))
        return [root / "projects"]

    def build_resume(
        self,
        session_id: str,
        *,
        cwd: str | None,
        env: Mapping[str, str],
        extra_args: Sequence[str] = (),
        executable: str | None = None,
    ) -> LaunchSpec:
        return self._spec(
            ["--resume", session_id, *extra_args],
            cwd=cwd, env=env, session_id=session_id, executable=executable,
        )

    def build_new(
        self,
        *,
        cwd: str | None,
        requested_id: str | None,
        env: Mapping[str, str],
        extra_args: Sequence[str] = (),
        executable: str | None = None,
    ) -> LaunchSpec:
        if not requested_id:
            raise ValueError("ClaudeProvider requires a preassigned new-session id")
        return self._spec(
            ["--session-id", requested_id, *extra_args],
            cwd=cwd, env=env, session_id=requested_id, executable=executable,
        )


class CodexProvider(AgentProvider):
    """Contract-level Codex adapter; not user-selectable until history is wired."""

    id = "codex"
    display_name = "Codex"
    executable_name = "codex"
    status_profile = "generic"
    history_format = "codex-rollout-jsonl"
    capabilities = ProviderCapabilities()

    def history_roots(
        self,
        *,
        home: Path | None = None,
        env: Mapping[str, str] | None = None,
    ) -> list[Path]:
        env_map = os.environ if env is None else env
        root = Path(env_map.get("CODEX_HOME") or ((home or Path.home()) / ".codex"))
        return [root / "sessions"]

    def build_resume(
        self,
        session_id: str,
        *,
        cwd: str | None,
        env: Mapping[str, str],
        extra_args: Sequence[str] = (),
        executable: str | None = None,
    ) -> LaunchSpec:
        return self._spec(
            ["resume", session_id, *extra_args],
            cwd=cwd, env=env, session_id=session_id, executable=executable,
        )

    def build_new(
        self,
        *,
        cwd: str | None,
        requested_id: str | None,
        env: Mapping[str, str],
        extra_args: Sequence[str] = (),
        executable: str | None = None,
    ) -> LaunchSpec:
        return self._spec(
            list(extra_args), cwd=cwd, env=env, session_id=None, executable=executable,
        )


_PROVIDER_TYPES = {
    ClaudeProvider.id: ClaudeProvider,
    CodexProvider.id: CodexProvider,
}


def get_provider(provider_id: str) -> AgentProvider:
    try:
        provider_type = _PROVIDER_TYPES[provider_id.strip().lower()]
    except (AttributeError, KeyError) as exc:
        raise ValueError(f"unknown provider: {provider_id!r}") from exc
    return provider_type()
