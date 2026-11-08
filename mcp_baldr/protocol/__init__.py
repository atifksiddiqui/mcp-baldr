"""Protocol-level helpers for the MCP aggregation gateway."""

from mcp_baldr.protocol.pagination import (
    DEFAULT_PAGE_SIZE,
    InvalidCursorError,
    decode_cursor,
    encode_cursor,
    paginate,
)
from mcp_baldr.protocol.uri import (
    NamespacedURIError,
    decode_namespaced_uri,
    encode_namespaced_uri,
    is_namespaced_uri,
)

__all__ = [
    "DEFAULT_PAGE_SIZE",
    "InvalidCursorError",
    "NamespacedURIError",
    "decode_cursor",
    "decode_namespaced_uri",
    "encode_cursor",
    "encode_namespaced_uri",
    "is_namespaced_uri",
    "paginate",
]
