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
