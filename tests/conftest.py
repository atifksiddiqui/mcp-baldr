"""Shared pytest fixtures and constants for the MCP Gateway test suite."""

import sys
import textwrap
from pathlib import Path

import pytest

from mcp_gateway.config.models import StdioServerConfig

# -- Paths ---------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parent.parent
MOCK_SERVER_PATH = str(PROJECT_ROOT / "examples" / "mock_server.py")
SLOW_SERVER_PATH = str(PROJECT_ROOT / "examples" / "slow_server.py")


# -- Config helpers ------------------------------------------------------------


def make_mock_server_config(
    name: str = "mock",
    *,
    mock_tools: str | None = None,
    mock_server_name: str | None = None,
    **kwargs,
) -> StdioServerConfig:
    """Build a StdioServerConfig pointing at the mock server."""
    env: dict[str, str] = {}
    if mock_tools is not None:
        env["MOCK_TOOLS"] = mock_tools
    if mock_server_name is not None:
        env["MOCK_SERVER_NAME"] = mock_server_name
    env.update(kwargs.pop("env", {}))
    return StdioServerConfig(
        name=name,
        command=sys.executable,
        args=[MOCK_SERVER_PATH],
        env=env,
        **kwargs,
    )


def make_slow_server_config(
    name: str = "slow",
    *,
    delay: float = 5.0,
    **kwargs,
) -> StdioServerConfig:
    """Build a StdioServerConfig pointing at the slow server."""
    return StdioServerConfig(
        name=name,
        command=sys.executable,
        args=[SLOW_SERVER_PATH],
        env={"MOCK_DELAY": str(delay)},
        **kwargs,
    )


def write_gateway_config(
    tmp_path: Path,
    servers_yaml: str,
) -> Path:
    """Write a gateway_config.yaml to *tmp_path* and return its path."""
    path = tmp_path / "gateway_config.yaml"
    path.write_text(textwrap.dedent(servers_yaml))
    return path


def write_two_server_config(
    tmp_path: Path,
    *,
    auto_restart: bool = False,
    max_restarts: int = 3,
    timeout: float = 30.0,
) -> Path:
    """Write a config with mock_a (default tools) and mock_b (alt tools)."""
    yaml = f"""\
        servers:
          - name: mock_a
            command: {sys.executable}
            args: ["{MOCK_SERVER_PATH}"]
            auto_restart: {str(auto_restart).lower()}
            max_restarts: {max_restarts}
            timeout: {timeout}
          - name: mock_b
            command: {sys.executable}
            args: ["{MOCK_SERVER_PATH}"]
            env:
              MOCK_TOOLS: "alt"
              MOCK_SERVER_NAME: "mock-server-b"
            auto_restart: {str(auto_restart).lower()}
            max_restarts: {max_restarts}
            timeout: {timeout}
        logging:
          level: WARNING
          format: text
        """
    return write_gateway_config(tmp_path, yaml)


# -- Fixtures ------------------------------------------------------------------


@pytest.fixture
def mock_server_config() -> StdioServerConfig:
    """A StdioServerConfig pointing at the default mock server."""
    return make_mock_server_config()


@pytest.fixture
def slow_server_config() -> StdioServerConfig:
    """A StdioServerConfig pointing at the slow server (5s delay)."""
    return make_slow_server_config()
