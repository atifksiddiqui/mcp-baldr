"""Tests for the Pydantic config models and YAML loader."""

import textwrap
from pathlib import Path

import pytest

from mcp_baldr.config.loader import ConfigError, load_config
from mcp_baldr.config.models import GatewayConfig, StdioServerConfig

# -- StdioServerConfig -------------------------------------------------------


class TestStdioServerConfig:
    def test_minimal_valid(self):
        s = StdioServerConfig(name="weather", command="python")
        assert s.name == "weather"
        assert s.command == "python"
        assert s.args == []
        assert s.env == {}
        assert s.enabled is True

    def test_full_valid(self):
        s = StdioServerConfig(
            name="fs_server",
            command="npx",
            args=["-y", "@modelcontextprotocol/server-filesystem", "/tmp"],
            env={"NODE_ENV": "production"},
            enabled=False,
        )
        assert s.args == ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"]
        assert s.env == {"NODE_ENV": "production"}
        assert s.enabled is False

    @pytest.mark.parametrize("bad_name", ["Bad-Name", "UPPER", "1server", "sp ace", ""])
    def test_invalid_name(self, bad_name):
        with pytest.raises(Exception, match="name"):
            StdioServerConfig(name=bad_name, command="python")

    def test_empty_command(self):
        with pytest.raises(Exception, match="command"):
            StdioServerConfig(name="test", command="")

    def test_extra_fields_rejected(self):
        with pytest.raises(Exception, match="extra"):
            StdioServerConfig(name="test", command="python", unexpected="value")


# -- GatewayConfig ------------------------------------------------------------


class TestGatewayConfig:
    def _server(self, name="test", **kw):
        return StdioServerConfig(name=name, command="python", **kw)

    def test_empty_config(self):
        g = GatewayConfig(servers=[])
        assert g.servers == []
        assert g.enabled_servers() == []

    def test_enabled_servers_filters_disabled(self):
        g = GatewayConfig(
            servers=[
                self._server("a", enabled=True),
                self._server("b", enabled=False),
                self._server("c", enabled=True),
            ]
        )
        assert [s.name for s in g.enabled_servers()] == ["a", "c"]

    def test_duplicate_names_rejected(self):
        with pytest.raises(Exception, match="(?i)duplicate"):
            GatewayConfig(servers=[self._server("dup"), self._server("dup")])

    def test_logging_defaults(self):
        g = GatewayConfig(servers=[])
        assert g.logging.level == "INFO"
        assert g.logging.format == "json"


# -- LoggingConfig -------------------------------------------------------------


class TestLoggingConfig:
    def test_level_normalised_to_upper(self):
        g = GatewayConfig(servers=[])
        g.logging.level = "debug"  # pydantic validator uppercases

    def test_invalid_level(self):
        from mcp_baldr.config.models import LoggingConfig

        with pytest.raises(Exception, match="level"):
            LoggingConfig(level="VERBOSE")

    def test_invalid_format(self):
        from mcp_baldr.config.models import LoggingConfig

        with pytest.raises(Exception, match="format"):
            LoggingConfig(format="xml")


# -- load_config --------------------------------------------------------------


class TestLoadConfig:
    def test_valid_yaml(self, tmp_path: Path):
        yaml_text = textwrap.dedent("""\
            servers:
              - name: echo_server
                command: python
                args: ["-m", "mock"]
                env:
                  MOCK_MODE: "1"
              - name: math_server
                command: python
                enabled: false
            logging:
              level: debug
              format: text
        """)
        path = tmp_path / "config.yaml"
        path.write_text(yaml_text)

        config = load_config(path)
        assert len(config.servers) == 2
        assert config.servers[0].name == "echo_server"
        assert config.servers[0].args == ["-m", "mock"]
        assert config.servers[0].env == {"MOCK_MODE": "1"}
        assert config.servers[1].enabled is False
        assert config.logging.level == "DEBUG"
        assert config.logging.format == "text"
        assert len(config.enabled_servers()) == 1

    def test_missing_file(self, tmp_path: Path):
        with pytest.raises(ConfigError, match="not found"):
            load_config(tmp_path / "nonexistent.yaml")

    def test_empty_yaml(self, tmp_path: Path):
        path = tmp_path / "empty.yaml"
        path.write_text("")

        config = load_config(path)
        assert config.servers == []

    def test_invalid_yaml_syntax(self, tmp_path: Path):
        path = tmp_path / "bad.yaml"
        path.write_text("servers: [unclosed")

        with pytest.raises(ConfigError, match="parse"):
            load_config(path)

    def test_invalid_config_values(self, tmp_path: Path):
        yaml_text = textwrap.dedent("""\
            servers:
              - name: Bad-Name
                command: python
        """)
        path = tmp_path / "bad_values.yaml"
        path.write_text(yaml_text)

        with pytest.raises(ConfigError, match="Invalid configuration"):
            load_config(path)

    def test_top_level_not_dict(self, tmp_path: Path):
        path = tmp_path / "list.yaml"
        path.write_text("- item1\n- item2\n")

        with pytest.raises(ConfigError, match="mapping"):
            load_config(path)

    def test_duplicate_names_in_yaml(self, tmp_path: Path):
        yaml_text = textwrap.dedent("""\
            servers:
              - name: same
                command: python
              - name: same
                command: python
        """)
        path = tmp_path / "dups.yaml"
        path.write_text(yaml_text)

        with pytest.raises(ConfigError, match="Invalid configuration"):
            load_config(path)


# -- PaginationConfig (Stage 8) ----------------------------------------------


class TestPaginationConfig:
    def test_default_page_size(self):
        assert GatewayConfig().pagination.page_size == 100

    def test_custom_page_size(self):
        cfg = GatewayConfig.model_validate({"pagination": {"page_size": 25}})
        assert cfg.pagination.page_size == 25

    @pytest.mark.parametrize("bad", [0, -1, 10_001, 999_999])
    def test_invalid_page_size_rejected(self, bad):
        with pytest.raises(Exception, match="page_size"):
            GatewayConfig.model_validate({"pagination": {"page_size": bad}})

    def test_extra_pagination_key_rejected(self):
        with pytest.raises(Exception, match="extra"):
            GatewayConfig.model_validate({"pagination": {"bogus": 1}})

    def test_pagination_loaded_from_yaml(self, tmp_path: Path):
        path = tmp_path / "gateway_config.yaml"
        path.write_text(
            textwrap.dedent(
                """\
                servers: []
                pagination:
                  page_size: 7
                """
            )
        )
        cfg = load_config(path)
        assert cfg.pagination.page_size == 7


# -- Transport tagged union (downstream Streamable HTTP) ---------------------


class TestServerTransportUnion:
    def test_no_type_defaults_to_stdio(self):
        """Backward compat: a server config without `type` is stdio."""
        from mcp_baldr.config.models import StdioServerConfig

        cfg = GatewayConfig.model_validate(
            {"servers": [{"name": "legacy", "command": "python3", "args": ["x.py"]}]}
        )
        assert isinstance(cfg.servers[0], StdioServerConfig)
        assert cfg.servers[0].type == "stdio"

    def test_explicit_stdio_type(self):
        from mcp_baldr.config.models import StdioServerConfig

        cfg = GatewayConfig.model_validate(
            {"servers": [{"type": "stdio", "name": "a", "command": "python3"}]}
        )
        assert isinstance(cfg.servers[0], StdioServerConfig)

    def test_http_type(self):
        from mcp_baldr.config.models import HttpServerConfig

        cfg = GatewayConfig.model_validate(
            {
                "servers": [
                    {
                        "type": "http",
                        "name": "remote",
                        "url": "https://example.com/mcp",
                        "headers": {"Authorization": "Bearer tok"},
                    }
                ]
            }
        )
        s = cfg.servers[0]
        assert isinstance(s, HttpServerConfig)
        assert s.url == "https://example.com/mcp"
        assert s.headers["Authorization"] == "Bearer tok"

    def test_mixed_stdio_and_http(self):
        from mcp_baldr.config.models import HttpServerConfig, StdioServerConfig

        cfg = GatewayConfig.model_validate(
            {
                "servers": [
                    {"name": "local", "command": "python3"},
                    {"type": "http", "name": "remote", "url": "http://localhost:9000/mcp"},
                ]
            }
        )
        assert isinstance(cfg.servers[0], StdioServerConfig)
        assert isinstance(cfg.servers[1], HttpServerConfig)
        assert len(cfg.enabled_servers()) == 2

    def test_http_requires_url(self):
        with pytest.raises(Exception, match="url"):
            GatewayConfig.model_validate({"servers": [{"type": "http", "name": "x"}]})

    @pytest.mark.parametrize("bad_url", ["ftp://nope", "example.com/mcp", "", "ws://x"])
    def test_http_url_scheme_validated(self, bad_url):
        with pytest.raises(Exception, match="url|URL"):
            GatewayConfig.model_validate(
                {"servers": [{"type": "http", "name": "x", "url": bad_url}]}
            )

    def test_unknown_transport_type_rejected(self):
        with pytest.raises(Exception, match="type|discriminator|tag"):
            GatewayConfig.model_validate({"servers": [{"type": "carrier_pigeon", "name": "x"}]})

    def test_http_rejects_stdio_only_fields(self):
        """An http entry carrying stdio-only keys (command) is rejected."""
        with pytest.raises(Exception, match="command|extra"):
            GatewayConfig.model_validate(
                {
                    "servers": [
                        {"type": "http", "name": "x", "url": "http://a/mcp", "command": "python3"}
                    ]
                }
            )

    def test_direct_http_server_config_construction(self):
        from mcp_baldr.config.models import HttpServerConfig

        s = HttpServerConfig(type="http", name="h", url="https://a/mcp")
        assert s.name == "h"
        assert s.enabled is True
        assert s.timeout == 30.0

    def test_http_name_validation_shared(self):
        """Name rules from the shared base apply to http configs too."""
        with pytest.raises(Exception, match="name"):
            GatewayConfig.model_validate(
                {"servers": [{"type": "http", "name": "Bad-Name", "url": "http://a/mcp"}]}
            )
