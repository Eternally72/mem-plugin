"""One scoped physical-forget path for explicit requests and opt-in retention deletion."""

from uuid import uuid4

from mem_plugin.engine.errors import ArchiveError
from mem_plugin.engine.services.graph import refresh_contexts
from mem_plugin.observability.models import AuditEvent


def forget_in_transaction(
    uow, target, access, trace, *, purge=None, schedule=False, reason="explicit user request"
):
    affected = uow.graph.ancestors(uow.graph.correction_family(target.id))
    neighbors = uow.graph.context_neighbors(affected)
    uow.consolidation.suppress(target.id)
    for node_id in uow.nodes.clear_hidden_context_dependents(affected):
        uow.audit.append(
            AuditEvent(
                uuid4(),
                node_id,
                access.agent_id,
                "context_evolved",
                None,
                None,
                "dependent annotation purged",
                trace.trace_id,
            )
        )
    if purge is not None:
        purge(uow, affected)
    elif uow.lifecycle.related_archives(affected):
        raise ArchiveError("archive storage is required to forget archived memory")
    uow.nodes.forget(affected)
    refresh_contexts(uow, neighbors, access.agent_id, trace.trace_id, schedule=schedule)
    uow.audit.append(
        AuditEvent(
            uuid4(),
            target.id,
            access.agent_id,
            "forgotten",
            target.version,
            None,
            reason,
            trace.trace_id,
        )
    )
    return len(affected)
