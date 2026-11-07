"""Sensitive-data redaction for educational events.

Before any observed protocol data becomes an :class:`~mcp_baldr.learning.
events.EducationalEvent`, it passes through :func:`redact`.  This is a
security-critical boundary (see the Learning Lab requirements): access
tokens, API keys, passwords, authorization headers, and other secrets must
never reach an educational consumer.

The approach is conservative — when in doubt, redact:

- Keys whose name matches a sensitive pattern have their values masked,
  regardless of value type.
- Values that *look* like secrets (long high-entropy-ish tokens, bearer
  headers, common key prefixes) are masked even under innocuous keys.
- Redaction recurses through nested dicts and lists.
- Strings are truncated to a bounded length to avoid leaking large blobs
  (e.g. full resource contents) into educational output.
"""

from __future__ import annotations

import re
from typing import Any

#: Placeholder substituted for any redacted value.
REDACTED = "«redacted»"

#: Maximum length of a retained string value before truncation.
_MAX_STR_LEN = 256

#: Key-name fragments (case-insensitive) that mark a value as sensitive.
_SENSITIVE_KEY_PARTS: tuple[str, ...] = (
    "api_key",
    "apikey",
    "token",
    "secret",
    "password",
    "passwd",
    "pwd",
    "authorization",
    "auth",
    "credential",
    "private_key",
    "client_secret",
    "access_key",
    "session_key",
    "bearer",
)

#: Value patterns that indicate a secret even under an innocuous key name.
_SENSITIVE_VALUE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^Bearer\s+\S+", re.IGNORECASE),
    re.compile(r"^(sk|pk|ghp|gho|xox[baprs])[-_][A-Za-z0-9]{8,}"),  # common key prefixes
    re.compile(r"^[A-Za-z0-9_\-]{40,}$"),  # long opaque token-like strings
)


def _is_sensitive_key(key: str) -> bool:
    """Return True if *key* names a field that should be redacted."""
    lowered = key.lower()
    return any(part in lowered for part in _SENSITIVE_KEY_PARTS)


def _looks_like_secret(value: str) -> bool:
    """Return True if *value* resembles a secret by shape."""
    return any(pat.search(value) for pat in _SENSITIVE_VALUE_PATTERNS)


def _redact_value(value: Any) -> Any:
    """Redact a single value (recursing into containers)."""
    if isinstance(value, dict):
        return redact(value)
    if isinstance(value, list):
        return [_redact_value(item) for item in value]
    if isinstance(value, str):
        if _looks_like_secret(value):
            return REDACTED
        if len(value) > _MAX_STR_LEN:
            return value[:_MAX_STR_LEN] + "…(truncated)"
        return value
    # ints, floats, bools, None pass through unchanged.
    return value


def redact(data: dict[str, Any]) -> dict[str, Any]:
    """Return a redacted copy of *data* safe for educational display.

    Args:
        data: An arbitrary mapping of observed protocol metadata.

    Returns:
        A new dict with sensitive keys masked, secret-shaped values masked,
        long strings truncated, and nested structures redacted recursively.
        The input is never mutated.
    """
    result: dict[str, Any] = {}
    for key, value in data.items():
        if _is_sensitive_key(str(key)):
            result[key] = REDACTED
        else:
            result[key] = _redact_value(value)
    return result


def redact_env(env: dict[str, str]) -> dict[str, str]:
    """Redact an environment-variable mapping.

    Environment blocks frequently carry API keys and tokens, so every value
    whose key *or* value looks sensitive is masked.  Non-sensitive values are
    retained to keep the educational example useful.
    """
    result: dict[str, str] = {}
    for key, value in env.items():
        if _is_sensitive_key(str(key)) or _looks_like_secret(str(value)):
            result[key] = REDACTED
        else:
            result[key] = value
    return result
