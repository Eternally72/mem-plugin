"""Bind deployment identity to authorized tool namespaces."""

from dataclasses import dataclass, replace

from mem_plugin.engine.errors import MemoryAccessDenied
from mem_plugin.engine.models import AccessContext


@dataclass(frozen=True, slots=True)
class ToolIdentity:
    principal: AccessContext
    namespaces: frozenset[str]

    def access(self, namespace: str | None = None) -> AccessContext:
        namespace = self.principal.namespace if namespace is None else namespace
        if namespace not in self.namespaces:
            raise MemoryAccessDenied("namespace is not authorized")
        return replace(self.principal, namespace=namespace)


class ScopedToolIdentity(ToolIdentity):
    """Use the identity verified on the current HTTP request for each tool call."""

    def access(self, namespace: str | None = None) -> AccessContext:
        from fastmcp.server.dependencies import get_access_token

        token = get_access_token()
        if token is None:
            raise MemoryAccessDenied("MCP authentication is required")
        label = token.claims.get("memory_label")
        if label is None:
            return super().access(namespace)
        bound = token.claims["memory_namespace"]
        if namespace not in (None, label, bound):
            raise MemoryAccessDenied("namespace is not authorized")
        return AccessContext(bound, bound, bound)
