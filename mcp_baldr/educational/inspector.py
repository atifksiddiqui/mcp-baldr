"""ProtocolInspector: educational JSON-RPC traffic visualizer.

Intercepts raw JSON-RPC messages flowing through the gateway and prints
richly formatted, tree-like traces to **stderr** so students and developers
can observe the MCP protocol in real time.

Protocol safety
---------------
Every output method writes exclusively to ``sys.stderr``.  Writing to
``sys.stdout`` would corrupt the newline-delimited JSON-RPC stream that AI
clients read from the gateway, so this rule is enforced unconditionally.

All public methods are wrapped in a top-level ``try/except`` so that any
unexpected error (missing key, serialization failure, etc.) fails silently
without disrupting the gateway's request handling.
"""

from __future__ import annotations

import sys
from typing import Any

from mcp_baldr.config.models import LearningConfig

# -- ANSI color codes ----------------------------------------------------------

_ANSI = {
    "reset": "\033[0m",
    "dim": "\033[2m",
    "cyan": "\033[36m",
    "yellow": "\033[33m",
    "blue": "\033[34m",
    "green": "\033[32m",
    "magenta": "\033[35m",
    "red": "\033[31m",
    "bold": "\033[1m",
}

# Width of the output box (characters).
_BOX_WIDTH = 72


class ProtocolInspector:
    """Educational JSON-RPC traffic visualizer.

    Created by :class:`~mcp_baldr.gateway.server.Gateway` when learning
    mode is enabled.  Each public method corresponds to a point in the
    gateway's request-handling pipeline where raw protocol data can be
    observed.
    """

    def __init__(self, config: LearningConfig) -> None:
        self._verbose_handshake = config.verbose_handshake
        # Only use ANSI colors when stderr is a TTY.  When piping to a
        # file (e.g. ``2> trace.log``) plain text is more useful.
        self._color = sys.stderr.isatty()

    # -- Color helpers --------------------------------------------------------

    def _c(self, name: str, text: str) -> str:
        """Wrap *text* in ANSI color *name* if color is enabled."""
        if not self._color:
            return text
        return f"{_ANSI[name]}{text}{_ANSI['reset']}"

    # -- Tree formatting ------------------------------------------------------

    def _format_value(self, value: Any) -> str:
        """Format a scalar value for display."""
        if isinstance(value, str):
            return self._c("green", f'"{value}"')
        if isinstance(value, bool):
            return self._c("magenta", "true" if value else "false")
        if value is None:
            return self._c("dim", "null")
        if isinstance(value, (int, float)):
            return self._c("yellow", str(value))
        return str(value)

    def _format_tree(self, data: Any, indent: int = 0) -> list[str]:
        """Recursively format a nested structure as indented lines."""
        lines: list[str] = []
        prefix = "  " * indent

        if isinstance(data, dict):
            for key, value in data.items():
                if isinstance(value, dict) and value:
                    lines.append(f"{prefix}{self._c('blue', str(key))}:")
                    lines.extend(self._format_tree(value, indent + 1))
                elif isinstance(value, list) and value:
                    lines.append(f"{prefix}{self._c('blue', str(key))}:")
                    for i, item in enumerate(value):
                        if isinstance(item, (dict, list)):
                            lines.append(f"{prefix}  [{i}]:")
                            lines.extend(self._format_tree(item, indent + 2))
                        else:
                            lines.append(f"{prefix}  [{i}]: {self._format_value(item)}")
                elif isinstance(value, dict):
                    lines.append(f"{prefix}{self._c('blue', str(key))}: {{}}")
                elif isinstance(value, list):
                    lines.append(f"{prefix}{self._c('blue', str(key))}: []")
                else:
                    lines.append(
                        f"{prefix}{self._c('blue', str(key))}: {self._format_value(value)}"
                    )
        elif isinstance(data, list):
            for i, item in enumerate(data):
                if isinstance(item, (dict, list)):
                    lines.append(f"{prefix}[{i}]:")
                    lines.extend(self._format_tree(item, indent + 1))
                else:
                    lines.append(f"{prefix}[{i}]: {self._format_value(item)}")
        else:
            lines.append(f"{prefix}{self._format_value(data)}")

        return lines

    # -- Box drawing ----------------------------------------------------------

    def _print_box(self, title: str, body_lines: list[str]) -> None:
        """Print a bordered box with a title line and body lines to stderr."""
        dim = "dim"
        top = f"{self._c(dim, '┌─')} {title} {self._c(dim, '─' * max(1, _BOX_WIDTH - len(title) - 4))}"
        sep = self._c(dim, f"├{'─' * (_BOX_WIDTH - 1)}")
        bottom = self._c(dim, f"└{'─' * (_BOX_WIDTH - 1)}")

        print(top, file=sys.stderr)
        if body_lines:
            print(sep, file=sys.stderr)
            for line in body_lines:
                print(f"{self._c(dim, '│')} {line}", file=sys.stderr)
        print(bottom, file=sys.stderr)

    # -- Public inspection methods (all safe, all stderr) -------------------

    def client_request(self, method: str, params: dict[str, Any], msg_id: Any) -> None:
        """Inspect a JSON-RPC request from the AI client to the gateway."""
        try:
            if method == "initialize" and not self._verbose_handshake:
                return

            title = self._c("cyan", "[AI CLIENT ──► GATEWAY]")
            title += f"  {self._c('yellow', method)}  (id={msg_id})"

            body = ["params:"]
            body.extend(self._format_tree(params, indent=1))

            self._print_box(title, body)
        except Exception:
            pass  # Fail silently — never disrupt the gateway.

    def client_response(self, method: str, result: dict[str, Any], msg_id: Any) -> None:
        """Inspect a JSON-RPC response from the gateway back to the AI client."""
        try:
            label = f"{method} result" if method != "initialize" else "initialize result"
            title = self._c("cyan", "[GATEWAY ──► AI CLIENT]")
            title += f"  {self._c('yellow', label)}  (id={msg_id})"

            body = self._format_tree(result, indent=0)
            self._print_box(title, body)
        except Exception:
            pass

    def client_notification(self, method: str, params: dict[str, Any]) -> None:
        """Inspect a notification from the AI client (no response expected)."""
        try:
            title = self._c("cyan", "[AI CLIENT ──► GATEWAY]")
            title += f"  {self._c('yellow', method)}  (notification)"

            body = ["params:"]
            body.extend(self._format_tree(params, indent=1))

            self._print_box(title, body)
        except Exception:
            pass

    def routing(self, namespaced_name: str, server: str, tool: str) -> None:
        """Inspect the namespace prefix resolution step."""
        try:
            title = self._c("cyan", "[GATEWAY]")
            title += f"  {self._c('yellow', 'Namespace Resolution')}"

            body = [
                f"requested: {self._c('green', repr(namespaced_name))}",
                f"  {self._c('dim', '└─►')} server: {self._c('green', repr(server))}"
                f"   tool: {self._c('green', repr(tool))}",
            ]
            self._print_box(title, body)
        except Exception:
            pass

    def downstream_request(self, server: str, method: str, params: dict[str, Any]) -> None:
        """Inspect a JSON-RPC request forwarded to a downstream server."""
        try:
            title = self._c("cyan", f"[GATEWAY ──► {server}]")
            title += f"  {self._c('yellow', method)}"

            body = self._format_tree(params, indent=0)
            self._print_box(title, body)
        except Exception:
            pass

    def downstream_response(self, server: str, method: str, result: dict[str, Any]) -> None:
        """Inspect a JSON-RPC response from a downstream server."""
        try:
            label = f"{method} result" if method != "initialize" else "initialize result"
            title = self._c("cyan", f"[{server} ──► GATEWAY]")
            title += f"  {self._c('yellow', label)}"

            body = self._format_tree(result, indent=0)
            self._print_box(title, body)
        except Exception:
            pass

    def error(self, msg_id: Any, code: int, message: str) -> None:
        """Inspect a JSON-RPC error response sent to the AI client."""
        try:
            title = self._c("red", "[GATEWAY ──► AI CLIENT]")
            title += f"  {self._c('red', 'ERROR')}  (id={msg_id})"

            body = [
                f"code: {self._c('red', str(code))}",
                f"message: {self._c('red', repr(message))}",
            ]
            self._print_box(title, body)
        except Exception:
            pass

    def gateway_ready(self, servers: list[str], tool_count: int) -> None:
        """Print a startup banner showing discovered servers and tools."""
        try:
            title = self._c("cyan", "[GATEWAY READY]")
            body = [
                f"servers: {', '.join(self._c('green', s) for s in servers)}",
                f"total tools: {self._c('yellow', str(tool_count))}",
            ]
            self._print_box(title, body)
        except Exception:
            pass
