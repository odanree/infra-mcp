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
    argv = [
        "ssh",
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", "BatchMode=yes",
        "-o", f"ConnectTimeout={int(min(timeout_s, 15))}",
        settings.infra_ssh_host,
        remote_cmd,
    ]
    log.debug("ssh exec: %s", argv)
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(), timeout=timeout_s,
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
            rc=-1, stdout="",
            stderr="ssh binary not found on PATH — install OpenSSH client",
        )
    return SshResult(
        rc=proc.returncode if proc.returncode is not None else -1,
        stdout=stdout_bytes.decode(errors="replace"),
        stderr=stderr_bytes.decode(errors="replace"),
    )


def compose(cmd: str) -> str:
    """Wrap a `docker compose <cmd>` invocation in a cd to the project dir.

    Lets every tool build a one-liner without repeating the cd. Single quotes
    around `cmd` keep it as a single argv item from the ssh perspective.
    """
    return f"cd {settings.infra_compose_dir} && {cmd}"


def is_service_allowed(service: str) -> bool:
    """Allowlist check — runs before every write tool."""
    return service in settings.allowed_services_set
