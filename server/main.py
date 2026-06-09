"""FastMCP entrypoint — registers each ops tool with a docstring the LLM reads."""

from __future__ import annotations

import logging

from fastmcp import FastMCP

from server.config import settings
from server.tools.ops import (
    caddy_reload,
    infra_deploys,
    infra_disk,
    infra_logs,
    infra_restart,
    infra_status,
    service_url,
)

logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s")

mcp = FastMCP(
    "infra-mcp",
    instructions=(
        "Operate a remote docker-compose stack (Hetzner VPS) over SSH. "
        "Read tools (infra_status, infra_logs, infra_disk, infra_deploys, service_url) "
        "are always safe. Write tools (infra_restart, caddy_reload) need confirm=true "
        "and only operate on services in the configured allowlist. "
        "Use service_url to check public availability after a restart."
    ),
)


@mcp.tool()
async def infra_status_tool() -> dict:
    """List every container in the compose stack with state + status string."""
    return (await infra_status()).model_dump()


@mcp.tool()
async def infra_logs_tool(
    service: str,
    tail: int = 100,
    since: str | None = None,
    grep: str | None = None,
) -> dict:
    """Tail logs from one compose service. `since` accepts 10m / 1h / 2d. `grep` is plain text."""
    return (await infra_logs(service, tail=tail, since=since, grep=grep)).model_dump()


@mcp.tool()
async def infra_disk_tool() -> dict:
    """VPS disk + docker storage summary. Use before heavy rebuilds."""
    return (await infra_disk()).model_dump()


@mcp.tool()
async def service_url_tool(service: str, scheme: str = "https") -> dict:
    """Resolve <service>.<INFRA_PUBLIC_DOMAIN> and probe its HTTP status. Quick liveness check."""
    return (await service_url(service, scheme=scheme)).model_dump()


@mcp.tool()
async def infra_deploys_tool(limit_per_repo: int = 5) -> list[dict]:
    """Recent GitHub Actions runs across the configured deploy repos."""
    return [s.model_dump() for s in await infra_deploys(limit_per_repo=limit_per_repo)]


@mcp.tool()
async def infra_restart_tool(service: str, confirm: bool = False) -> dict:
    """Restart a compose service. Requires confirm=true AND service in INFRA_ALLOWED_SERVICES."""
    return (await infra_restart(service, confirm=confirm)).model_dump()


@mcp.tool()
async def caddy_reload_tool(confirm: bool = False) -> dict:
    """Reload Caddy after a Caddyfile change. Requires confirm=true."""
    return (await caddy_reload(confirm=confirm)).model_dump()


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
