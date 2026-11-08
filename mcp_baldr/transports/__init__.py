"""Transport adapters for downstream MCP server communication."""

from mcp_baldr.transports.base import (
    CrashCallback,
    DownstreamTransport,
    NotificationCallback,
    RequestCallback,
    TransportError,
)
from mcp_baldr.transports.http import HttpTransport, HttpTransportError
from mcp_baldr.transports.stdio import StdioTransport, StdioTransportError

__all__ = [
    "CrashCallback",
    "DownstreamTransport",
    "HttpTransport",
    "HttpTransportError",
    "NotificationCallback",
    "RequestCallback",
    "StdioTransport",
    "StdioTransportError",
    "TransportError",
]
