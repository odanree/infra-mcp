"""
The actual MCP-callable tools. Each is a small async function that:

  1. Validates its inputs against the allowlist (if it's a write tool).
  2. Builds a docker / df / curl command from typed params (never strings
     interpolated from MCP client input).
  3. Runs it over SSH with a bounded timeout.
  4. Returns a stable Pydantic-shaped response — the calling LLM never
     sees raw stderr or unbounded log dumps.

Read tools (status / logs / disk / deploys / url) are always callable.
Write tools (restart / caddy_reload) require an explicit `confirm=true`
parameter so the model can't blunder into a production bounce.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import httpx
from pydantic import BaseModel, Field

from server.config import settings
from server.tools.ssh import compose, is_service_allowed, run


# ---------------------------------------------------------------------------
# Read tools
# ---------------------------------------------------------------------------


class ContainerStatus(BaseModel):
    name: str
    image: str
    state: str       # "running" | "exited" | "restarting" | ...
    status: str      # the docker-rendered "Up 2 hours" / "Exited (137)" string
    health: str | None = None


class StatusResult(BaseModel):
    host: str
    containers: list[ContainerStatus]
    total: int
    running: int
    error: str | None = None  # populated when SSH/parse fails so the LLM sees the cause


async def infra_status() -> StatusResult:
    """List every container in the compose stack with its current state.

    Reads `docker compose ps --format json` on the VPS. Output is bounded
    by the number of services in the compose file — typically ≤30 rows.
    """
    res = await run(compose("docker compose ps --format json --all"))
    if not res.ok:
        # Surface SSH failures explicitly instead of silently returning an
        # empty list. Earlier version of this tool swallowed the rc + stderr
        # and the caller had no way to tell "SSH down" from "stack empty".
        err = (res.stderr or f"ssh rc={res.rc}").strip()[:500]
        return StatusResult(
            host=settings.infra_ssh_host,
            containers=[], total=0, running=0,
            error=f"ssh exec failed: {err}",
        )

    containers: list[ContainerStatus] = []
    for line in res.stdout.strip().splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        containers.append(ContainerStatus(
            name=row.get("Name") or row.get("Service") or "?",
            image=row.get("Image") or "?",
            state=row.get("State") or "?",
            status=row.get("Status") or "?",
            health=row.get("Health") or None,
        ))

    return StatusResult(
        host=settings.infra_ssh_host,
        containers=containers,
        total=len(containers),
        running=sum(1 for c in containers if c.state == "running"),
    )


class LogsResult(BaseModel):
    service: str
    lines: int
    truncated: bool
    since: str | None
    content: str


async def infra_logs(
    service: str,
    tail: int = 100,
    since: str | None = None,
    grep: str | None = None,
) -> LogsResult:
    """Tail log lines from one compose service.

    Args:
        service: Container service name (must exist on the box; not
                 allowlist-restricted because reading logs is non-destructive).
        tail:    Lines per service, clamped to INFRA_MAX_LOG_LINES.
        since:   Relative time like "10m", "1h", "2d" — passed to
                 `docker compose logs --since`.
        grep:    Optional substring filter applied via `grep -F` on the box.
                 Plain string match only; never a regex (no injection surface).
    """
    tail = max(1, min(tail, settings.infra_max_log_lines))
    cmd = f"docker compose logs --tail={tail}"
    if since:
        # docker compose only accepts certain units, so validate cheaply.
        if not (since[:-1].isdigit() and since[-1] in {"s", "m", "h", "d"}):
            since = None
        else:
            cmd += f" --since={since}"
    cmd += f" {service}"

    if grep:
        safe = grep.replace("'", "'\\''")
        cmd += f" 2>&1 | grep -F -- '{safe}'"
    else:
        cmd += " 2>&1"

    res = await run(compose(cmd), timeout_s=45.0)
    content = res.stdout if res.ok else f"(ssh rc={res.rc}) {res.stderr}"
    # Final safety: never emit more than INFRA_MAX_LOG_LINES of output text.
    actual_lines = content.splitlines()
    truncated = len(actual_lines) > settings.infra_max_log_lines
    if truncated:
        content = "\n".join(actual_lines[-settings.infra_max_log_lines :])

    return LogsResult(
        service=service,
        lines=min(len(actual_lines), settings.infra_max_log_lines),
        truncated=truncated,
        since=since,
        content=content,
    )


class DiskResult(BaseModel):
    root_used_pct: int | None
    root_avail_gib: float | None
    docker_images_size_gib: float | None
    docker_volumes_size_gib: float | None
    docker_build_cache_gib: float | None
    raw: str


async def infra_disk() -> DiskResult:
    """Return disk + docker storage usage on the VPS.

    Combines `df -h /` and `docker system df` so the model can answer
    "is there enough room to rebuild this image?" in one round-trip.
    """
    df = await run("df -h / | tail -1")
    dsd = await run("docker system df --format '{{.Type}}\\t{{.Size}}'")

    root_used_pct = None
    root_avail_gib = None
    if df.ok and df.stdout.strip():
        parts = df.stdout.split()
        if len(parts) >= 5:
            avail = parts[3]
            used_pct = parts[4].rstrip("%")
            try:
                root_used_pct = int(used_pct)
            except ValueError:
                pass
            root_avail_gib = _parse_size_gib(avail)

    images = volumes = cache = None
    if dsd.ok:
        for line in dsd.stdout.splitlines():
            kind, _, size = line.partition("\t")
            if kind == "Images":
                images = _parse_size_gib(size)
            elif kind == "Local Volumes":
                volumes = _parse_size_gib(size)
            elif kind == "Build Cache":
                cache = _parse_size_gib(size)

    return DiskResult(
        root_used_pct=root_used_pct,
        root_avail_gib=root_avail_gib,
        docker_images_size_gib=images,
        docker_volumes_size_gib=volumes,
        docker_build_cache_gib=cache,
        raw=(df.stdout + "\n---\n" + dsd.stdout).strip(),
    )


def _parse_size_gib(size: str) -> float | None:
    """Convert "1.5G" / "850M" / "2.1TB" / "11G" → float GiB."""
    s = size.strip().upper().replace("IB", "B").rstrip("B").strip()
    if not s:
        return None
    unit = s[-1] if s[-1].isalpha() else ""
    try:
        n = float(s[:-1] if unit else s)
    except ValueError:
        return None
    factor = {"K": 1 / 1024 / 1024, "M": 1 / 1024, "G": 1, "T": 1024, "P": 1024 * 1024}.get(unit, 1)
    return round(n * factor, 2)


class ServiceURLResult(BaseModel):
    service: str
    url: str
    http_status: int | None
    error: str | None


async def service_url(service: str, scheme: str = "https") -> ServiceURLResult:
    """Resolve a subdomain on the deploy host and probe its health.

    Returns `<scheme>://<service>.<INFRA_PUBLIC_DOMAIN>` plus a fresh HTTP
    status code. Use this to confirm a service came back after restart.
    """
    url = f"{scheme}://{service}.{settings.infra_public_domain}"
    try:
        async with httpx.AsyncClient(timeout=8.0, follow_redirects=True) as client:
            r = await client.get(url)
        return ServiceURLResult(service=service, url=url, http_status=r.status_code, error=None)
    except httpx.HTTPError as e:
        return ServiceURLResult(service=service, url=url, http_status=None, error=str(e))


class DeployRunSummary(BaseModel):
    repo: str
    run_id: int
    conclusion: str | None
    status: str
    head_branch: str
    commit_message: str
    created_at: str


async def infra_deploys(limit_per_repo: int = 5) -> list[DeployRunSummary]:
    """Recent GitHub Actions runs across INFRA_DEPLOY_REPOS.

    Lets the model answer "did the deploy succeed?" without leaving chat.
    """
    if not settings.github_token or not settings.deploy_repos_list:
        return []

    headers = {
        "Authorization": f"Bearer {settings.github_token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    summaries: list[DeployRunSummary] = []
    async with httpx.AsyncClient(timeout=15.0) as client:
        for repo in settings.deploy_repos_list:
            try:
                r = await client.get(
                    f"https://api.github.com/repos/{repo}/actions/runs",
                    params={"per_page": str(limit_per_repo)},
                    headers=headers,
                )
                r.raise_for_status()
            except httpx.HTTPError:
                continue
            for run_row in r.json().get("workflow_runs", []):
                summaries.append(DeployRunSummary(
                    repo=repo,
                    run_id=run_row["id"],
                    conclusion=run_row.get("conclusion"),
                    status=run_row.get("status", "unknown"),
                    head_branch=run_row.get("head_branch", ""),
                    commit_message=(run_row.get("head_commit") or {}).get("message", "").split("\n")[0],
                    created_at=run_row.get("created_at", datetime.now(timezone.utc).isoformat()),
                ))
    return summaries


# ---------------------------------------------------------------------------
# Write tools — require explicit confirm=true
# ---------------------------------------------------------------------------


class RestartResult(BaseModel):
    service: str
    rc: int
    stdout: str
    stderr: str
    confirmed: bool


async def infra_restart(service: str, confirm: bool = False) -> RestartResult:
    """Restart a compose service. Requires confirm=true and service in allowlist.

    Use after a config change or when a container looks stuck. Health-check
    follow-up is the caller's responsibility — run `infra_status` afterwards
    to confirm it came back.
    """
    if not confirm:
        return RestartResult(
            service=service, rc=-2, stdout="",
            stderr=f"confirm=true required to restart {service}. This is intentional.",
            confirmed=False,
        )
    if not is_service_allowed(service):
        return RestartResult(
            service=service, rc=-3, stdout="",
            stderr=f"{service!r} is not in INFRA_ALLOWED_SERVICES",
            confirmed=False,
        )
    res = await run(compose(f"docker compose restart {service}"), timeout_s=60.0)
    return RestartResult(
        service=service, rc=res.rc, stdout=res.stdout, stderr=res.stderr, confirmed=True,
    )


class CaddyReloadResult(BaseModel):
    rc: int
    stdout: str
    stderr: str
    confirmed: bool


async def caddy_reload(confirm: bool = False) -> CaddyReloadResult:
    """Reload the Caddy reverse proxy in-place (no downtime).

    Use after editing the Caddyfile. Requires confirm=true since a typo
    in the Caddyfile prevents reload and Caddy keeps serving the old config —
    which can be more confusing than just returning an error here.
    """
    if not confirm:
        return CaddyReloadResult(
            rc=-2, stdout="",
            stderr="confirm=true required to reload Caddy",
            confirmed=False,
        )
    res = await run("docker exec portfolio-caddy caddy reload --config /etc/caddy/Caddyfile")
    return CaddyReloadResult(
        rc=res.rc, stdout=res.stdout, stderr=res.stderr, confirmed=True,
    )
