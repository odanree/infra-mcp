"""
Thin async wrapper around the local `ssh` binary.

We shell out instead of using paramiko/asyncssh because:
  1. Users already have their SSH config + keys + known_hosts set up.
  2. No per-call key/auth code to maintain.
  3. ProxyJump, agent forwarding, etc. just work.

Every command runs with a hard timeout. We never assemble shell strings
from user-controlled fragments — the typed callers in tools/*.py construct
the docker / df / etc. commands themselves and pass them as a list.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from server.config import settings

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT_S = 30.0


@dataclass(frozen=True)
class SshResult:
    """Outcome of one SSH invocation."""

    rc: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.rc == 0


async def run(remote_cmd: str, timeout_s: float = DEFAULT_TIMEOUT_S) -> SshResult:
    """Execute a single shell command on the configured SSH host.

    `remote_cmd` is sent verbatim — quoting and chaining is the caller's
    responsibility, since this is internal-only and called from typed tool
    handlers (never from MCP-client string args).
    """
    # INFRA_SSH_BIN overrides PATH resolution. On Windows, `which ssh` may
    # return Git Bash's ssh.exe which has different ~/.ssh resolution than
    # Windows OpenSSH and can hang even with BatchMode=yes. Default to
    # whatever PATH gives us; user pins via .env when needed.
    ssh_bin = settings.infra_ssh_bin or "ssh"
    argv = [
        ssh_bin,
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        "BatchMode=yes",
        "-o",
        f"ConnectTimeout={int(min(timeout_s, 15))}",
        settings.infra_ssh_host,
        remote_cmd,
    ]
    log.debug("ssh exec: %s", argv)
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            # stdin=DEVNULL: otherwise SSH inherits the MCP server's stdin
            # (the stdio MCP protocol pipe) and can hang waiting on it. The
            # symptom is "ssh timed out after Ns" even though BatchMode=yes
            # should prevent interactive prompts.
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(),
                timeout=timeout_s,
            )
        except TimeoutError:
            proc.kill()
            await proc.wait()
            return SshResult(
                rc=-1,
                stdout="",
                stderr=f"ssh timed out after {timeout_s}s",
            )
    except FileNotFoundError:
        return SshResult(
            rc=-1,
            stdout="",
            stderr="ssh binary not found on PATH — install OpenSSH client",
        )
    return SshResult(
        rc=proc.returncode if proc.returncode is not None else -1,
        stdout=stdout_bytes.decode(errors="replace"),
        stderr=stderr_bytes.decode(errors="replace"),
    )


class UnknownStackError(ValueError):
    """Raised when a tool is invoked with `stack` not in INFRA_STACKS.

    Carries the offending name + the allowed names so the caller's error
    response can list the alternatives. Surfaced as a structured rejection
    by the tools, never as an SSH call.
    """

    def __init__(self, requested: str, available: list[str]) -> None:
        self.requested = requested
        self.available = available
        super().__init__(
            f"unknown stack {requested!r}; available: {', '.join(sorted(available))}"
        )


def resolve_stack(stack: str | None) -> tuple[str, str]:
    """Map a (possibly None) stack name to (name, compose_dir).

    None resolves to the configured default. Unknown names raise
    UnknownStackError so callers can return a structured rejection
    without duplicating validation in every tool.
    """
    stacks = settings.stacks_map
    if stack is None:
        name = settings.default_stack_name
        return name, stacks[name]
    if stack not in stacks:
        raise UnknownStackError(stack, list(stacks))
    return stack, stacks[stack]


def compose(cmd: str, stack: str | None = None) -> str:
    """Wrap a `docker compose <cmd>` invocation in a cd to the project dir.

    Lets every tool build a one-liner without repeating the cd. `stack`
    selects which compose project on the box (multi-stack mode); None
    uses the operator's configured default. Unknown names raise
    UnknownStackError before any SSH call.
    """
    _, compose_dir = resolve_stack(stack)
    return f"cd {compose_dir} && {cmd}"


def is_service_allowed(service: str) -> bool:
    """Allowlist check — runs before every write tool."""
    return service in settings.allowed_services_set
