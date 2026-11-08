"""Shared pytest fixtures and constants for the MCP Gateway test suite."""

from __future__ import annotations

import socket
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from mcp_baldr.config.models import StdioServerConfig

# -- Paths ---------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parent.parent
MOCK_SERVER_PATH = str(PROJECT_ROOT / "examples" / "mock_server.py")
SLOW_SERVER_PATH = str(PROJECT_ROOT / "examples" / "slow_server.py")
HTTP_MOCK_SERVER_PATH = str(PROJECT_ROOT / "examples" / "http_mock_server.py")


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


# -- HTTP mock server helper (Streamable HTTP downstream) ---------------------


def _free_port() -> int:
    """Return an OS-assigned free TCP port on localhost."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for_port(host: str, port: int, timeout: float = 15.0) -> None:
    """Block until *host:port* accepts a TCP connection, or raise on timeout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            try:
                s.connect((host, port))
                return
            except OSError:
                time.sleep(0.1)
    raise TimeoutError(f"HTTP mock server did not open {host}:{port} within {timeout}s")


class HttpMockServer:
    """A spawned Streamable HTTP mock MCP server for integration tests.

    Starts ``examples/http_mock_server.py`` as a subprocess on a free local
    port and exposes its ``/mcp`` endpoint URL.  Used as a context manager so
    the process is always torn down::

        with HttpMockServer() as srv:
            cfg = HttpServerConfig(type="http", name="remote", url=srv.url)
            ...
    """

    def __init__(self, server_name: str | None = None):
        self.host = "127.0.0.1"
        self.port = _free_port()
        self.url = f"http://{self.host}:{self.port}/mcp"
        self._server_name = server_name
        self._proc: subprocess.Popen | None = None

    def __enter__(self) -> HttpMockServer:
        env = None
        if self._server_name is not None:
            import os

            env = {**os.environ, "MOCK_SERVER_NAME": self._server_name}
        self._proc = subprocess.Popen(
            [
                sys.executable,
                HTTP_MOCK_SERVER_PATH,
                "--host",
                self.host,
                "--port",
                str(self.port),
            ],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        _wait_for_port(self.host, self.port)
        return self

    def __exit__(self, *exc: object) -> None:
        if self._proc is not None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
                self._proc.wait()


# -- Fixtures ------------------------------------------------------------------


@pytest.fixture
def mock_server_config() -> StdioServerConfig:
    """A StdioServerConfig pointing at the default mock server."""
    return make_mock_server_config()


@pytest.fixture
def slow_server_config() -> StdioServerConfig:
    """A StdioServerConfig pointing at the slow server (5s delay)."""
    return make_slow_server_config()
