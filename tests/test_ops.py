"""Tests for the ops tools — SSH mocked at the run() boundary."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from server.tools.ops import (
    DiskResult,
    LogsResult,
    StatusResult,
    _parse_size_gib,
    infra_disk,
    infra_logs,
    infra_prune,
    infra_restart,
    infra_status,
)
from server.tools.ssh import SshResult


@pytest.fixture(autouse=True)
def _stub_ssh_host(monkeypatch):
    """Tests run against a placeholder SSH host so we never trip into
    the operator's real VPS even if their .env is loaded."""
    import server.config as cfg
    monkeypatch.setattr(cfg.settings, "infra_ssh_host", "test@stub.invalid")
    monkeypatch.setattr(cfg.settings, "infra_compose_dir", "/tmp/stack")


def _ok(stdout: str) -> SshResult:
    return SshResult(rc=0, stdout=stdout, stderr="")


@pytest.mark.asyncio
async def test_infra_status_parses_compose_ps_json_lines():
    sample = (
        '{"Name":"app-api","Image":"app-api:latest","State":"running","Status":"Up 2 hours","Health":"healthy"}\n'
        '{"Name":"app-web","Image":"app-web:latest","State":"running","Status":"Up 2 hours","Health":null}\n'
        '{"Name":"app-cache","Image":"redis:7-alpine","State":"exited","Status":"Exited (137) 5m ago","Health":null}\n'
    )
    with patch("server.tools.ops.run", new=AsyncMock(return_value=_ok(sample))):
        result = await infra_status()
    assert isinstance(result, StatusResult)
    assert result.total == 3
    assert result.running == 2
    names = {c.name for c in result.containers}
    assert names == {"app-api", "app-web", "app-cache"}


@pytest.mark.asyncio
async def test_infra_status_handles_ssh_failure_gracefully():
    fail = SshResult(rc=255, stdout="", stderr="connection refused")
    with patch("server.tools.ops.run", new=AsyncMock(return_value=fail)):
        result = await infra_status()
    assert result.containers == []
    assert result.total == 0


@pytest.mark.asyncio
async def test_infra_logs_clamps_tail_to_max():
    captured: list[str] = []

    async def fake_run(cmd: str, timeout_s: float = 30.0):
        captured.append(cmd)
        return _ok("line1\nline2\nline3")

    with patch("server.tools.ops.run", new=fake_run):
        out = await infra_logs("app-api", tail=99999)

    assert isinstance(out, LogsResult)
    assert "--tail=500" in captured[0]    # default INFRA_MAX_LOG_LINES
    assert out.service == "app-api"


@pytest.mark.asyncio
async def test_infra_logs_rejects_malformed_since():
    captured: list[str] = []

    async def fake_run(cmd: str, timeout_s: float = 30.0):
        captured.append(cmd)
        return _ok("")

    with patch("server.tools.ops.run", new=fake_run):
        out = await infra_logs("app-api", since="forever")

    assert out.since is None
    assert "--since" not in captured[0]


@pytest.mark.asyncio
async def test_infra_logs_grep_quotes_dangerous_chars():
    captured: list[str] = []

    async def fake_run(cmd: str, timeout_s: float = 30.0):
        captured.append(cmd)
        return _ok("matched line")

    with patch("server.tools.ops.run", new=fake_run):
        await infra_logs("app-api", grep="o'reilly && rm -rf /")

    # Single quote in the needle gets safely doubled — no shell injection.
    assert "grep -F --" in captured[0]
    assert "rm -rf" in captured[0]   # it's there as plain grep arg, not a command
    assert captured[0].count("'") >= 2


@pytest.mark.asyncio
async def test_infra_disk_parses_df_and_docker_system_df():
    df_out = "/dev/sda1       75G   61G   11G  85% /"
    dsd_out = "Images\t31.6GB\nContainers\t800MB\nLocal Volumes\t12.9GB\nBuild Cache\t5.0GB"

    async def fake_run(cmd: str, timeout_s: float = 30.0):
        if "df -h" in cmd:
            return _ok(df_out)
        if "docker system df" in cmd:
            return _ok(dsd_out)
        return SshResult(rc=1, stdout="", stderr="?")

    with patch("server.tools.ops.run", new=fake_run):
        result = await infra_disk()
    assert isinstance(result, DiskResult)
    assert result.root_used_pct == 85
    assert result.root_avail_gib == 11.0
    assert result.docker_images_size_gib == 31.6
    assert result.docker_build_cache_gib == 5.0


@pytest.mark.asyncio
async def test_infra_restart_requires_confirm():
    with patch("server.tools.ops.run", new=AsyncMock(return_value=_ok(""))) as m:
        result = await infra_restart("app-api")
    assert result.confirmed is False
    assert result.rc == -2
    m.assert_not_called()


@pytest.mark.asyncio
async def test_infra_restart_rejects_service_not_in_allowlist():
    with patch("server.tools.ops.run", new=AsyncMock(return_value=_ok(""))) as m, \
         patch("server.tools.ops.is_service_allowed", return_value=False):
        result = await infra_restart("rogue-svc", confirm=True)
    assert result.confirmed is False
    assert result.rc == -3
    m.assert_not_called()


@pytest.mark.asyncio
async def test_infra_restart_runs_when_confirmed_and_allowlisted():
    with patch("server.tools.ops.run", new=AsyncMock(return_value=_ok("restarted"))) as m, \
         patch("server.tools.ops.is_service_allowed", return_value=True):
        result = await infra_restart("app-api", confirm=True)
    assert result.confirmed is True
    assert result.rc == 0
    m.assert_awaited_once()


@pytest.mark.asyncio
async def test_infra_logs_grep_no_match_is_not_an_error():
    """grep exits 1 when there are no matches — should report grep_no_match=True,
    NOT a docker compose failure. Catches the bug where infra_logs returned
    '(ssh rc=1)' to the caller and looked like an SSH failure."""
    no_match = SshResult(rc=1, stdout="", stderr="")
    with patch("server.tools.ops.run", new=AsyncMock(return_value=no_match)):
        result = await infra_logs("oci-api", since="1h", grep="error")
    assert result.error is None
    assert result.grep_no_match is True
    assert result.lines == 0
    assert result.content == ""


@pytest.mark.asyncio
async def test_infra_logs_surfaces_real_ssh_failure():
    """When SSH genuinely fails (rc != 0 with stderr), error is populated and
    the caller can render a useful diagnostic instead of bare logs."""
    ssh_down = SshResult(
        rc=255, stdout="",
        stderr="ssh: connect to host 10.0.0.1 port 22: Connection refused\n",
    )
    with patch("server.tools.ops.run", new=AsyncMock(return_value=ssh_down)):
        result = await infra_logs("oci-api", since="1h")
    assert result.error is not None
    assert "Connection refused" in result.error
    assert result.grep_no_match is False
    assert result.content == ""


@pytest.mark.asyncio
async def test_infra_prune_requires_confirm():
    """No SSH should fire without confirm=true."""
    with patch("server.tools.ops.run", new=AsyncMock(return_value=_ok(""))) as m:
        result = await infra_prune("build_cache")
    assert result.confirmed is False
    assert result.rc == -2
    m.assert_not_called()


@pytest.mark.asyncio
async def test_infra_prune_rejects_unknown_target():
    """Unknown targets short-circuit before any SSH so a typo can't accidentally
    do something the operator didn't mean."""
    with patch("server.tools.ops.run", new=AsyncMock(return_value=_ok(""))) as m:
        result = await infra_prune("rm-rf-everything", confirm=True)
    assert result.confirmed is False
    assert result.rc == -3
    m.assert_not_called()


@pytest.mark.asyncio
async def test_infra_prune_extracts_reclaimed_summary_when_present():
    """Docker prune prints 'Total reclaimed space: N.NN GB' — surface that
    as a structured field instead of making the LLM parse stdout."""
    stdout = (
        "deleted: sha256:abc...\n"
        "deleted: sha256:def...\n"
        "Total reclaimed space: 9.448GB\n"
    )
    with patch("server.tools.ops.run", new=AsyncMock(return_value=_ok(stdout))):
        result = await infra_prune("build_cache", confirm=True)
    assert result.confirmed is True
    assert result.rc == 0
    assert result.reclaimed_summary == "Total reclaimed space: 9.448GB"


def test_parse_size_gib_handles_units():
    assert _parse_size_gib("11G") == 11.0
    assert _parse_size_gib("31.6GB") == 31.6
    assert _parse_size_gib("850M") == round(850 / 1024, 2)
    assert _parse_size_gib("2T") == 2048.0
    assert _parse_size_gib("") is None
    assert _parse_size_gib("garbage") is None
