"""Pydantic models for validating gateway configuration.

These models define the shape of ``gateway_config.yaml`` and enforce
constraints at load time so invalid configs fail fast with clear errors.
"""

from __future__ import annotations

import re
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# Server names are used as namespace prefixes (e.g. ``server_name__tool_name``),
# so they must be valid Python-style identifiers to keep routing unambiguous.
_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")


class LoggingConfig(BaseModel):
    """Structured-logging configuration for the gateway."""

    model_config = ConfigDict(extra="forbid")

    level: str = Field(
        default="INFO",
        description="Log level (DEBUG, INFO, WARNING, ERROR, CRITICAL).",
    )
    format: str = Field(
        default="json",
        description="Output format for log records: 'json' (default) or 'text'.",
    )

    @field_validator("level")
    @classmethod
    def validate_level(cls, v: str) -> str:
        allowed = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        upper = v.upper()
        if upper not in allowed:
            raise ValueError(
                f"Invalid log level '{v}'. Must be one of: {', '.join(sorted(allowed))}."
            )
        return upper

    @field_validator("format")
    @classmethod
    def validate_format(cls, v: str) -> str:
        allowed = {"json", "text"}
        lower = v.lower()
        if lower not in allowed:
            raise ValueError(
                f"Invalid log format '{v}'. Must be one of: {', '.join(sorted(allowed))}."
            )
        return lower


class LearningConfig(BaseModel):
    """Educational protocol inspection configuration (Phase 5).

    When ``learning_mode`` is enabled, the gateway prints rich,
    tree-formatted JSON-RPC traces to **stderr** for students and
    developers learning the MCP protocol.  Output never touches
    stdout, so the underlying stdio transport remains uncorrupted.
    """

    model_config = ConfigDict(extra="forbid")

    learning_mode: bool = Field(
        default=False,
        description="Enable educational protocol inspection output on stderr.",
    )
    verbose_handshake: bool = Field(
        default=True,
        description="Show detailed initialize handshake breakdown when learning_mode is on.",
    )


class PaginationConfig(BaseModel):
    """Pagination settings for aggregated ``*/list`` results.

    The gateway paginates the *aggregate* of all downstream items with an
    opaque, stable cursor.  ``page_size`` bounds how many items each
    ``tools/list`` / ``resources/list`` / ``prompts/list`` page returns.
    """

    model_config = ConfigDict(extra="forbid")

    page_size: int = Field(
        default=100,
        gt=0,
        le=10_000,
        description="Maximum items per page for aggregated list results.",
    )


class BaseServerConfig(BaseModel):
    """Fields common to every downstream server, regardless of transport.

    A downstream server is identified by a discriminated ``type`` tag.  The
    gateway and :meth:`GatewayConfig.enabled_servers` depend only on the
    common surface defined here (``name``, ``enabled``, ``timeout``), so new
    transports can be added without changing routing or lifecycle code.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., description="Unique server name; used for tool namespacing.")
    enabled: bool = Field(default=True, description="Whether this server is started.")
    timeout: float = Field(
        default=30.0,
        gt=0,
        description="Seconds to wait for a response from this server before timing out.",
    )

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: str) -> str:
        if not _NAME_RE.match(v):
            raise ValueError(
                f"Invalid server name '{v}'. Must start with a lowercase letter "
                f"and contain only lowercase letters, digits, and underscores."
            )
        return v


class StdioServerConfig(BaseServerConfig):
    """Configuration for a single downstream stdio MCP server subprocess.

    Attributes:
        type: Transport discriminator; always ``"stdio"`` (the default, so
            existing configs without a ``type`` field validate unchanged).
        name: Unique identifier used as the namespace prefix for tools.
        command: The executable to launch (e.g. ``"python"`` or ``"npx"``).
        args: Positional arguments passed to *command*.
        env: Extra environment variables merged into the subprocess env.
        enabled: When ``False`` the gateway skips this server at startup.
    """

    type: Literal["stdio"] = Field(
        default="stdio",
        description="Transport type discriminator (stdio subprocess).",
    )
    command: str = Field(..., description="Executable to run for this server.")
    args: list[str] = Field(default_factory=list, description="Arguments for the command.")
    env: dict[str, str] = Field(
        default_factory=dict,
        description="Environment variables for the subprocess.",
    )

    # -- Resiliency (Phase 3) -------------------------------------------------
    auto_restart: bool = Field(
        default=False,
        description="If true, the gateway attempts to restart this server when it crashes.",
    )
    max_restarts: int = Field(
        default=3,
        ge=0,
        description="Maximum restart attempts before giving up (0 disables restarts).",
    )

    @field_validator("command")
    @classmethod
    def validate_command(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("Command must not be empty.")
        return v.strip()


class HttpServerConfig(BaseServerConfig):
    """Configuration for a downstream MCP server reached over Streamable HTTP.

    Targets the MCP ``2026-07-28`` Streamable HTTP binding: a single MCP
    endpoint accepting ``POST``, with per-request JSON or SSE responses.

    Attributes:
        type: Transport discriminator; must be ``"http"``.
        url: The server's single MCP endpoint URL (``POST`` target).
        headers: Static headers sent on every request — the supported form of
            authentication for now (e.g. ``Authorization: Bearer ...``).
            Header values are redacted in educational ``--learn`` output.
        enabled: When ``False`` the gateway skips this server at startup.
    """

    type: Literal["http"] = Field(
        ...,
        description="Transport type discriminator (Streamable HTTP).",
    )
    url: str = Field(..., description="The downstream server's MCP endpoint URL.")
    headers: dict[str, str] = Field(
        default_factory=dict,
        description="Static headers sent on every request (e.g. bearer auth).",
    )

    @field_validator("url")
    @classmethod
    def validate_url(cls, v: str) -> str:
        v = v.strip()
        if not (v.startswith("http://") or v.startswith("https://")):
            raise ValueError(f"Invalid URL '{v}'. Must start with http:// or https://.")
        return v


#: A downstream server config, discriminated on the ``type`` field.  Configs
#: that omit ``type`` default to stdio, preserving backward compatibility.
ServerConfig = Annotated[
    StdioServerConfig | HttpServerConfig,
    Field(discriminator="type"),
]


class GatewayConfig(BaseModel):
    """Top-level gateway configuration loaded from ``gateway_config.yaml``.

    Attributes:
        servers: List of downstream server definitions (stdio or http).
        logging: Logging configuration for structured JSON output.
    """

    model_config = ConfigDict(extra="forbid")

    servers: list[ServerConfig] = Field(
        default_factory=list,
        description="Downstream MCP server definitions (stdio or http).",
    )
    logging: LoggingConfig = Field(
        default_factory=LoggingConfig,
        description="Gateway logging configuration.",
    )
    learning: LearningConfig = Field(
        default_factory=LearningConfig,
        description="Educational protocol inspection configuration.",
    )
    pagination: PaginationConfig = Field(
        default_factory=PaginationConfig,
        description="Pagination settings for aggregated list results.",
    )

    @model_validator(mode="before")
    @classmethod
    def default_server_type(cls, data: Any) -> Any:
        """Default each server's ``type`` to ``"stdio"`` when omitted.

        The discriminated ``servers`` union requires a ``type`` tag, but
        configs written before the HTTP transport existed have no ``type``
        field.  Injecting the default here (before the union resolves)
        preserves backward compatibility while keeping the discriminated-union
        error messages for genuinely malformed entries.
        """
        if isinstance(data, dict):
            servers = data.get("servers")
            if isinstance(servers, list):
                for entry in servers:
                    if isinstance(entry, dict) and "type" not in entry:
                        entry["type"] = "stdio"
        return data

    @model_validator(mode="after")
    def validate_unique_server_names(self) -> GatewayConfig:
        names = [s.name for s in self.servers]
        duplicates = {n for n in names if names.count(n) > 1}
        if duplicates:
            raise ValueError(
                f"Duplicate server name(s): {', '.join(sorted(duplicates))}. "
                f"Each server name must be unique."
            )
        return self

    def enabled_servers(self) -> list[StdioServerConfig | HttpServerConfig]:
        """Return only servers whose ``enabled`` flag is ``True``."""
        return [s for s in self.servers if s.enabled]
