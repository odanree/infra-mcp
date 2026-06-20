"""Runtime config — everything an operator can tune without touching code."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

log = logging.getLogger(__name__)

# When Claude Code launches the MCP server it sets its own CWD, so a
# relative .env path won't find ours. Resolve to the repo root absolutely.
_ENV_PATH = Path(__file__).resolve().parents[1] / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=str(_ENV_PATH), extra="ignore")

    # SSH target — empty defaults force the operator to fill .env. We don't
    # want a real VPS hostname in the source so a forked clone can't trip
    # into operating on the wrong box.
    infra_ssh_host: str = Field(default="", alias="INFRA_SSH_HOST")
    infra_compose_dir: str = Field(default="/opt/stack", alias="INFRA_COMPOSE_DIR")

    # Optional override for the SSH binary path. On Windows, `which ssh` may
    # find Git Bash's ssh.exe which has different ~/.ssh resolution than
    # Windows OpenSSH and can hang from a subprocess context. Pin this to
    # `C:\Windows\System32\OpenSSH\ssh.exe` if a vanilla `ssh` call doesn't
    # work from a Claude Code MCP server context.
    infra_ssh_bin: str = Field(default="", alias="INFRA_SSH_BIN")

    # Public domain Caddy terminates TLS on. Used by service_url.
    infra_public_domain: str = Field(default="example.com", alias="INFRA_PUBLIC_DOMAIN")

    # Safety
    infra_allowed_services: str = Field(default="", alias="INFRA_ALLOWED_SERVICES")
    infra_max_log_lines: int = Field(default=500, alias="INFRA_MAX_LOG_LINES")

    # Multi-stack support — one VPS often hosts several independent compose
    # projects (portfolio-infra, beacon, lumen, ...) under different dirs.
    # INFRA_STACKS is a JSON map of {stack_name: compose_dir}; tools accept
    # an optional `stack` arg to pick which one. When unset, the MCP falls
    # back to single-stack mode using INFRA_COMPOSE_DIR (back-compat).
    infra_stacks: str = Field(default="", alias="INFRA_STACKS")
    infra_default_stack: str = Field(default="", alias="INFRA_DEFAULT_STACK")

    # GitHub deploys lookup
    github_token: str = Field(default="", alias="GITHUB_TOKEN")
    infra_deploy_repos: str = Field(default="", alias="INFRA_DEPLOY_REPOS")

    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    @property
    def allowed_services_set(self) -> set[str]:
        return {s.strip() for s in self.infra_allowed_services.split(",") if s.strip()}

    @property
    def deploy_repos_list(self) -> list[str]:
        return [r.strip() for r in self.infra_deploy_repos.split(",") if r.strip()]

    @property
    def stacks_map(self) -> dict[str, str]:
        """Resolved {stack_name: compose_dir} map.

        Precedence:
          1. If INFRA_STACKS is a parseable JSON object with at least one
             {str: str} entry, use it verbatim.
          2. Otherwise fall back to a single-entry map {"default": INFRA_COMPOSE_DIR}
             so existing single-stack deployments keep working without config
             changes.
        """
        if self.infra_stacks.strip():
            try:
                parsed = json.loads(self.infra_stacks)
                if isinstance(parsed, dict):
                    clean = {
                        str(k).strip(): str(v).strip()
                        for k, v in parsed.items()
                        if isinstance(k, str)
                        and isinstance(v, str)
                        and k.strip()
                        and v.strip()
                    }
                    if clean:
                        return clean
            except (json.JSONDecodeError, ValueError):
                log.warning(
                    "INFRA_STACKS is set but unparseable as a JSON object — "
                    "falling back to single-stack mode via INFRA_COMPOSE_DIR.",
                )
        return {"default": self.infra_compose_dir}

    @property
    def default_stack_name(self) -> str:
        """Stack used when a tool call omits `stack`.

        Picks INFRA_DEFAULT_STACK if set and present in stacks_map; otherwise
        the first key (insertion order from the JSON, or "default" in
        single-stack mode).
        """
        stacks = self.stacks_map
        if self.infra_default_stack and self.infra_default_stack in stacks:
            return self.infra_default_stack
        return next(iter(stacks))


settings = Settings()
