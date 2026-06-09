"""Runtime config — everything an operator can tune without touching code."""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # SSH target — empty defaults force the operator to fill .env. We don't
    # want a real VPS hostname in the source so a forked clone can't trip
    # into operating on the wrong box.
    infra_ssh_host: str = Field(default="", alias="INFRA_SSH_HOST")
    infra_compose_dir: str = Field(default="/opt/stack", alias="INFRA_COMPOSE_DIR")

    # Public domain Caddy terminates TLS on. Used by service_url.
    infra_public_domain: str = Field(default="example.com", alias="INFRA_PUBLIC_DOMAIN")

    # Safety
    infra_allowed_services: str = Field(default="", alias="INFRA_ALLOWED_SERVICES")
    infra_max_log_lines: int = Field(default=500, alias="INFRA_MAX_LOG_LINES")

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


settings = Settings()
