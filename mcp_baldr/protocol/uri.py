"""Reversible, collision-safe resource-URI namespacing.

A downstream MCP server exposes resources under its own URI scheme (e.g.
``file:///x``, ``mock://docs/readme.txt``).  When the gateway aggregates
resources from many servers, two servers may expose the same URI, and the
gateway must be able to route a client's ``resources/read`` back to the
server that owns the URI.

Rather than blindly prefix or mutate the original URI (which could break URI
semantics or create ambiguity), the gateway wraps it in a dedicated scheme:

    baldr+<server>://<percent-encoded-original-uri>

Properties:

- **Reversible**: :func:`decode_namespaced_uri` recovers the exact original
  URI — the original is percent-encoded as a whole, so nothing is lost.
- **Collision-safe**: the server name is part of the authority, so two
  servers exposing identical original URIs get distinct namespaced URIs.
- **Unambiguous routing**: the server is recoverable from the namespaced URI
  alone, with no dependence on separators appearing in the original URI.
- **Non-destructive**: the original URI's own scheme/host/path are never
  rewritten; they are carried verbatim inside the encoded payload.

The scheme prefix ``baldr+`` keeps the namespaced URI a syntactically valid
URI while making its gateway origin obvious to anyone inspecting traffic.
"""

from __future__ import annotations

from urllib.parse import quote, unquote

#: Scheme prefix marking a gateway-namespaced URI.
SCHEME_PREFIX = "baldr+"

#: Separator between the scheme (``baldr+<server>``) and the encoded payload.
_SCHEME_SEP = "://"

#: Characters kept literal when percent-encoding the original URI.  We encode
#: aggressively (safe="") so the original URI cannot introduce structure into
#: the namespaced URI.
_QUOTE_SAFE = ""


class NamespacedURIError(ValueError):
    """Raised when a namespaced URI is malformed or cannot be decoded."""


def encode_namespaced_uri(server: str, original_uri: str) -> str:
    """Wrap *original_uri* owned by *server* into a namespaced gateway URI.

    Args:
        server: The downstream server name that owns the resource.
        original_uri: The resource URI as advertised by that server.

    Returns:
        A namespaced URI of the form
        ``baldr+<server>://<percent-encoded-original-uri>``.

    Raises:
        NamespacedURIError: If *server* is empty or contains characters that
            would make the scheme invalid.
    """
    if not server:
        raise NamespacedURIError("server name must not be empty")
    # Server becomes part of the scheme, so it must be scheme-safe.  Our
    # config already restricts server names to [a-z0-9_], which is safe here.
    if any(c in server for c in ":/?#@ "):
        raise NamespacedURIError(f"server name is not URI-scheme safe: {server!r}")
    encoded = quote(original_uri, safe=_QUOTE_SAFE)
    return f"{SCHEME_PREFIX}{server}{_SCHEME_SEP}{encoded}"


def is_namespaced_uri(uri: str) -> bool:
    """Return True if *uri* was produced by :func:`encode_namespaced_uri`."""
    return uri.startswith(SCHEME_PREFIX) and _SCHEME_SEP in uri


def decode_namespaced_uri(namespaced_uri: str) -> tuple[str, str]:
    """Recover ``(server, original_uri)`` from a namespaced gateway URI.

    Args:
        namespaced_uri: A URI produced by :func:`encode_namespaced_uri`.

    Returns:
        A ``(server, original_uri)`` tuple.

    Raises:
        NamespacedURIError: If *namespaced_uri* is not a valid namespaced URI.
    """
    if not namespaced_uri.startswith(SCHEME_PREFIX):
        raise NamespacedURIError(
            f"not a namespaced URI (missing {SCHEME_PREFIX!r} prefix): {namespaced_uri!r}"
        )
    body = namespaced_uri[len(SCHEME_PREFIX) :]
    if _SCHEME_SEP not in body:
        raise NamespacedURIError(f"malformed namespaced URI: {namespaced_uri!r}")
    server, _, encoded = body.partition(_SCHEME_SEP)
    if not server:
        raise NamespacedURIError(f"namespaced URI has empty server: {namespaced_uri!r}")
    original = unquote(encoded)
    return server, original
