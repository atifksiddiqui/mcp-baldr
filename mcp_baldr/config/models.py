"""Pydantic models for validating gateway configuration.

These models define the shape of ``gateway_config.yaml`` and enforce
constraints at load time so invalid configs fail fast with clear errors.
"""

from __future__ import annotations

import re

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


class StdioServerConfig(BaseModel):
    """Configuration for a single downstream stdio MCP server subprocess.

    Attributes:
        name: Unique identifier used as the namespace prefix for tools.
        command: The executable to launch (e.g. ``"python"`` or ``"npx"``).
        args: Positional arguments passed to *command*.
        env: Extra environment variables merged into the subprocess env.
        enabled: When ``False`` the gateway skips this server at startup.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., description="Unique server name; used for tool namespacing.")
    command: str = Field(..., description="Executable to run for this server.")
    args: list[str] = Field(default_factory=list, description="Arguments for the command.")
    env: dict[str, str] = Field(
        default_factory=dict,
        description="Environment variables for the subprocess.",
    )
    enabled: bool = Field(default=True, description="Whether this server is started.")

    # -- Resiliency (Phase 3) -------------------------------------------------
    timeout: float = Field(
        default=30.0,
        gt=0,
        description="Seconds to wait for a response from this server before timing out.",
    )
    auto_restart: bool = Field(
        default=False,
        description="If true, the gateway attempts to restart this server when it crashes.",
    )
    max_restarts: int = Field(
        default=3,
        ge=0,
        description="Maximum restart attempts before giving up (0 disables restarts).",
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

    @field_validator("command")
    @classmethod
    def validate_command(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("Command must not be empty.")
        return v.strip()


class GatewayConfig(BaseModel):
    """Top-level gateway configuration loaded from ``gateway_config.yaml``.

    Attributes:
        servers: List of downstream stdio server definitions.
        logging: Logging configuration for structured JSON output.
    """

    model_config = ConfigDict(extra="forbid")

    servers: list[StdioServerConfig] = Field(
        default_factory=list,
        description="Downstream stdio MCP server definitions.",
    )
    logging: LoggingConfig = Field(
        default_factory=LoggingConfig,
        description="Gateway logging configuration.",
    )
    learning: LearningConfig = Field(
        default_factory=LearningConfig,
        description="Educational protocol inspection configuration.",
    )

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

    def enabled_servers(self) -> list[StdioServerConfig]:
        """Return only servers whose ``enabled`` flag is ``True``."""
        return [s for s in self.servers if s.enabled]
