"""YAML configuration loader with Pydantic validation.

Usage::

    from mcp_gateway.config import load_config

    config = load_config("gateway_config.yaml")
    for server in config.enabled_servers():
        print(server.name, server.command, server.args)
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from mcp_gateway.config.models import GatewayConfig

logger = logging.getLogger(__name__)


class ConfigError(Exception):
    """Raised when the configuration file is missing, unreadable, or invalid."""


def load_config(path: str | Path) -> GatewayConfig:
    """Load and validate a gateway configuration from a YAML file.

    Args:
        path: Path to the YAML configuration file.

    Returns:
        A validated :class:`~mcp_gateway.config.models.GatewayConfig`.

    Raises:
        ConfigError: If the file does not exist, is not valid YAML,
            or fails Pydantic validation.
    """
    config_path = Path(path)

    if not config_path.exists():
        raise ConfigError(f"Configuration file not found: {config_path}")
    if not config_path.is_file():
        raise ConfigError(f"Configuration path is not a file: {config_path}")

    try:
        raw: Any = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"Failed to parse YAML in {config_path}: {exc}") from exc

    if raw is None:
        raw = {}

    if not isinstance(raw, dict):
        raise ConfigError(
            f"Expected a YAML mapping at the top level of {config_path}, got {type(raw).__name__}."
        )

    try:
        config = GatewayConfig.model_validate(raw)
    except ValidationError as exc:
        # Pydantic's error messages are already informative; wrap for context.
        raise ConfigError(f"Invalid configuration in {config_path}:\n{exc}") from exc

    server_names = [s.name for s in config.servers]
    enabled_count = len(config.enabled_servers())
    logger.info(
        "Loaded config from %s: %d server(s) defined, %d enabled",
        config_path,
        len(server_names),
        enabled_count,
    )

    return config
