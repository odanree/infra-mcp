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
    assert "--tail=500" in captured[0]  # default INFRA_MAX_LOG_LINES
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
    assert "rm -rf" in captured[0]  # it's there as plain grep arg, not a command
    assert captured[0].count("'") >= 2


@pytest.mark.asyncio
async def test_infra_disk_parses_df_and_docker_system_df():
    df_out = "/dev/sda1       75G   61G   11G  85% /"
    dsd_out = (
        "Images\t31.6GB\nContainers\t800MB\nLocal Volumes\t12.9GB\nBuild Cache\t5.0GB"
    )

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
    with (
        patch("server.tools.ops.run", new=AsyncMock(return_value=_ok(""))) as m,
        patch("server.tools.ops.is_service_allowed", return_value=False),
    ):
        result = await infra_restart("rogue-svc", confirm=True)
    assert result.confirmed is False
    assert result.rc == -3
    m.assert_not_called()


@pytest.mark.asyncio
async def test_infra_restart_runs_when_confirmed_and_allowlisted():
    with (
        patch(
            "server.tools.ops.run", new=AsyncMock(return_value=_ok("restarted"))
        ) as m,
        patch("server.tools.ops.is_service_allowed", return_value=True),
    ):
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
        rc=255,
        stdout="",
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


# ---------------------------------------------------------------------------
# Multi-stack routing — covers config.stacks_map + ssh.resolve_stack +
# integration into infra_status / infra_logs / infra_restart.
# ---------------------------------------------------------------------------


@pytest.fixture
def _multi_stack(monkeypatch):
    """Configure 3 stacks at distinct compose dirs so the routing under test
    can be observed via the cd-path baked into compose() output."""
    import server.config as cfg

    monkeypatch.setattr(
        cfg.settings,
        "infra_stacks",
        '{"portfolio":"/opt/portfolio-infra","beacon":"/opt/beacon","lumen":"/opt/lumen"}',
    )
    monkeypatch.setattr(cfg.settings, "infra_default_stack", "portfolio")
    return cfg.settings


def test_stacks_map_falls_back_to_single_stack_when_unset(monkeypatch):
    """Existing single-stack deployments must keep working without config edits."""
    import server.config as cfg

    monkeypatch.setattr(cfg.settings, "infra_stacks", "")
    monkeypatch.setattr(cfg.settings, "infra_compose_dir", "/opt/legacy")
    assert cfg.settings.stacks_map == {"default": "/opt/legacy"}
    assert cfg.settings.default_stack_name == "default"


def test_stacks_map_parses_multi_stack_json(_multi_stack):
    m = _multi_stack.stacks_map
    assert m == {
        "portfolio": "/opt/portfolio-infra",
        "beacon": "/opt/beacon",
        "lumen": "/opt/lumen",
    }
    assert _multi_stack.default_stack_name == "portfolio"


def test_stacks_map_invalid_json_falls_back(monkeypatch):
    """A typo in INFRA_STACKS must not crash startup — degrade to single-stack."""
    import server.config as cfg

    monkeypatch.setattr(cfg.settings, "infra_stacks", '{"broken": missing-quotes}')
    monkeypatch.setattr(cfg.settings, "infra_compose_dir", "/opt/legacy")
    assert cfg.settings.stacks_map == {"default": "/opt/legacy"}


def test_default_stack_name_picks_first_when_configured_default_unknown(monkeypatch):
    import server.config as cfg

    monkeypatch.setattr(cfg.settings, "infra_stacks", '{"a":"/opt/a","b":"/opt/b"}')
    monkeypatch.setattr(cfg.settings, "infra_default_stack", "does-not-exist")
    assert cfg.settings.default_stack_name == "a"


def test_resolve_stack_returns_default_when_none(_multi_stack):
    from server.tools.ssh import resolve_stack

    assert resolve_stack(None) == ("portfolio", "/opt/portfolio-infra")


def test_resolve_stack_returns_named_when_provided(_multi_stack):
    from server.tools.ssh import resolve_stack

    assert resolve_stack("beacon") == ("beacon", "/opt/beacon")


def test_resolve_stack_raises_on_unknown(_multi_stack):
    from server.tools.ssh import UnknownStackError, resolve_stack

    with pytest.raises(UnknownStackError) as excinfo:
        resolve_stack("nope")
    assert "nope" in str(excinfo.value)
    assert "beacon" in str(excinfo.value)  # available stacks surfaced


def test_compose_helper_routes_to_named_stack(_multi_stack):
    """The cd path in compose() output is the canonical evidence that the
    stack argument actually changes which compose project the SSH call hits."""
    from server.tools.ssh import compose

    assert "cd /opt/beacon" in compose("docker compose ps", stack="beacon")
    assert "cd /opt/lumen" in compose("docker compose ps", stack="lumen")
    # Default when omitted
    assert "cd /opt/portfolio-infra" in compose("docker compose ps")


@pytest.mark.asyncio
async def test_infra_status_routes_to_named_stack(_multi_stack):
    captured: list[str] = []

    async def fake_run(cmd: str, timeout_s: float = 30.0):
        captured.append(cmd)
        return _ok(
            '{"Name":"beacon-worker","Image":"i","State":"running","Status":"Up","Health":null}\n'
        )

    with patch("server.tools.ops.run", new=fake_run):
        result = await infra_status(stack="beacon")

    assert "cd /opt/beacon" in captured[0]
    assert result.stack == "beacon"
    assert result.containers[0].name == "beacon-worker"


@pytest.mark.asyncio
async def test_infra_status_uses_default_stack_when_omitted(_multi_stack):
    captured: list[str] = []

    async def fake_run(cmd: str, timeout_s: float = 30.0):
        captured.append(cmd)
        return _ok("")

    with patch("server.tools.ops.run", new=fake_run):
        result = await infra_status()

    assert "cd /opt/portfolio-infra" in captured[0]
    assert result.stack == "portfolio"


@pytest.mark.asyncio
async def test_infra_status_unknown_stack_returns_structured_error(_multi_stack):
    """An unknown stack must not trigger SSH — it must return a structured error."""
    with patch("server.tools.ops.run", new=AsyncMock(return_value=_ok(""))) as m:
        result = await infra_status(stack="typo")
    m.assert_not_called()
    assert result.error and "typo" in result.error
    assert result.containers == []


@pytest.mark.asyncio
async def test_infra_logs_routes_to_named_stack(_multi_stack):
    captured: list[str] = []

    async def fake_run(cmd: str, timeout_s: float = 30.0):
        captured.append(cmd)
        return _ok("log line")

    with patch("server.tools.ops.run", new=fake_run):
        result = await infra_logs("beacon-worker", stack="beacon")

    assert "cd /opt/beacon" in captured[0]
    assert result.stack == "beacon"
    assert result.service == "beacon-worker"


@pytest.mark.asyncio
async def test_infra_logs_unknown_stack_returns_structured_error(_multi_stack):
    with patch("server.tools.ops.run", new=AsyncMock(return_value=_ok(""))) as m:
        result = await infra_logs("worker", stack="typo")
    m.assert_not_called()
    assert result.error and "typo" in result.error
    assert result.content == ""


@pytest.mark.asyncio
async def test_infra_restart_routes_to_named_stack(_multi_stack):
    captured: list[str] = []

    async def fake_run(cmd: str, timeout_s: float = 30.0):
        captured.append(cmd)
        return _ok("restarted")

    with (
        patch("server.tools.ops.run", new=fake_run),
        patch("server.tools.ops.is_service_allowed", return_value=True),
    ):
        result = await infra_restart("beacon-worker", confirm=True, stack="beacon")

    assert result.confirmed is True
    assert "cd /opt/beacon" in captured[0]
    assert "docker compose restart beacon-worker" in captured[0]


@pytest.mark.asyncio
async def test_infra_restart_unknown_stack_does_not_call_ssh(_multi_stack):
    """The unknown-stack rejection must fire AFTER confirm + allowlist checks
    pass, but BEFORE the SSH call — proving the safety check works without
    the service or confirm requirements being relaxed."""
    with (
        patch("server.tools.ops.run", new=AsyncMock(return_value=_ok(""))) as m,
        patch("server.tools.ops.is_service_allowed", return_value=True),
    ):
        result = await infra_restart("beacon-worker", confirm=True, stack="typo")
    m.assert_not_called()
    assert result.confirmed is False
    assert result.rc == -4
    assert "typo" in result.stderr
