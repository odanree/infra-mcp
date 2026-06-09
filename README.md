# infra-mcp

**MCP server that wraps Docker / Caddy / GitHub Actions ops on a remote VPS.** Lets Claude take read-then-write-with-confirmation infra actions over SSH, with a service allowlist and audit-trail-friendly responses.

`MCP` · `FastMCP` · `Claude Code` · `Claude Desktop` · `Docker Compose` · `Caddy` · `GitHub Actions` · `Pydantic v2` · `httpx` · `pytest-asyncio`

| Tool | Read/Write | What it does |
|---|---|---|
| `infra_status` | R | `docker compose ps` parsed into one row per container |
| `infra_logs` | R | tail logs from one service with `--since` + plain-text `grep` |
| `infra_disk` | R | `df -h /` + `docker system df` for "is there room to rebuild?" |
| `infra_deploys` | R | last N GitHub Actions runs per configured repo |
| `service_url` | R | resolves `<svc>.<INFRA_PUBLIC_DOMAIN>` and probes HTTP status |
| `infra_restart` | **W** | restart a compose service (`confirm=true` + allowlisted) |
| `caddy_reload` | **W** | reload Caddy in-place (`confirm=true`) |

## Why this exists

I spent a full afternoon on a single deploy SSH'ing to a VPS, running `docker compose logs ...`, scrolling, restarting things, re-SSH'ing. Every one of those steps is a tool call this server exposes. Now: *"what's broken on `<service>` in the last 20 minutes?"* in Claude Code — done.

This is also the canonical interview answer for *"build us an MCP server for internal team operations":* an ops surface wrapped as MCP, with three safety layers (allowlist, confirm gate, structured responses).

## Safety model

- **Read vs write split.** Status/logs/disk/deploys/url are always callable. Restart/reload require an explicit `confirm: true` parameter so the model can't trip into a production bounce by accident.
- **Service allowlist.** Write tools cross-check against `INFRA_ALLOWED_SERVICES` from `.env`. Anything not listed is rejected even with `confirm=true`. Editing the allowlist is a deliberate human action.
- **No shell injection.** Tool inputs are typed (`tail: int`, `since: str` validated against a `\d+[smhd]` shape, `grep` strings quoted with single-quote escaping). Commands are assembled from typed params on the server, never from raw MCP-client strings.
- **Bounded outputs.** Logs are clamped to `INFRA_MAX_LOG_LINES` (500 by default) so a tool call can't dump 50 MB into the model context.
- **Hard timeouts.** Every SSH invocation runs under `asyncio.wait_for` and a `ConnectTimeout`. A wedged container or unreachable box returns an error, not a hang.

## Install

```bash
git clone https://github.com/odanree/infra-mcp
cd infra-mcp
python -m venv .venv
.venv/Scripts/activate           # Windows: .venv\Scripts\activate
pip install -e ".[dev]"

cp .env.example .env             # edit ssh host + allowlist + (optional) GH token
```

Requires the `ssh` binary on PATH and SSH-key auth already configured for the target host (your normal `~/.ssh/config` works).

## Register with Claude Code (user-scope)

```bash
claude mcp add infra -s user -- "/abs/path/to/infra-mcp/.venv/Scripts/python.exe" -m server.main
claude mcp list                  # confirms "infra" connected
```

Restart Claude Code, then try:

> "Show me the infra status. Anything not running?"
>
> "Tail logs for the api service since 10m and grep for 'Error'."
>
> "Restart the api service — confirm=true."

## Tests

```bash
pytest
# SSH is patched at the run() boundary — no network in the test suite.
```

## Roadmap

- `infra_logs` Langfuse export: when a service has Langfuse-traced calls, return a list of recent trace IDs alongside log lines so you can jump straight from a 500 to the trace.
- `service_url` + cert expiry: when the HTTP probe succeeds, also surface days-until-cert-expiry from Caddy's storage.
- Per-tool audit-log file on the target so any write action has a permanent local record independent of Claude's chat log.

## License

MIT
