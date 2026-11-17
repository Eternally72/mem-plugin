"""Append tasks in the same connection and transaction as the node write."""

from uuid import uuid4

from sqlalchemy import bindparam, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import SQLAlchemyError

from mem_plugin.engine.errors import MemoryStorageError
from mem_plugin.engine.models import AccessContext, ClaimedTask, MemoryTask
from mem_plugin.observability.context import current_trace
from mem_plugin.observability.models import TraceContext


class OutboxRepository:
    def __init__(self, connection: Connection, access: AccessContext):
        self.connection, self.access = connection, access

    def append(self, task: MemoryTask) -> None:
        trace = current_trace.get()
        if task.event_type == "consolidation.refresh":
            self.connection.execute(
                text("""UPDATE outbox_events SET status='completed',
                completed_at=now(),last_error='superseded generation'
                WHERE user_id=:user AND namespace=:ns AND node_id=:node
                AND event_type=:event AND generation<:generation AND status='pending'"""),
                {
                    "user": self.access.user_id,
                    "ns": self.access.namespace,
                    "node": task.node_id,
                    "event": task.event_type,
                    "generation": task.generation,
                },
            )
        statement = text("""INSERT INTO outbox_events
            (id,user_id,namespace,node_id,node_version,event_type,payload,agent_id,authorized_domains,
             trace_id,parent_span_id,correlation_id,generation)
            VALUES (:id,:user_id,:namespace,:node,:version,:event_type,
                    :payload,:agent,:domains,:trace,:parent,:correlation,:generation)
            ON CONFLICT (user_id,namespace,node_id,node_version,event_type,generation)
            DO NOTHING""").bindparams(
            bindparam("payload", type_=JSONB), bindparam("domains", type_=JSONB)
        )
        self.connection.execute(
            statement,
            {
                "id": task.id,
                "event_type": task.event_type,
                "generation": task.generation,
                "user_id": self.access.user_id,
                "namespace": self.access.namespace,
                "node": task.node_id,
                "agent": self.access.agent_id,
                "domains": sorted(self.access.domains),
                "version": task.node_version,
                "payload": {
                    "node_id": str(task.node_id),
                    "version": task.node_version,
                    "content_hash": task.content_hash,
                },
                "trace": task.trace_id,
                "parent": task.parent_span_id,
                "correlation": trace.correlation_id if trace else task.trace_id,
            },
        )

    def _lease(self, task: ClaimedTask) -> dict[str, object]:
        return {
            "id": task.id,
            "token": task.lease_token,
            "user": self.access.user_id,
            "namespace": self.access.namespace,
        }

    def lock_claim(self, task: ClaimedTask) -> bool:
        return (
            self.connection.execute(
                text("""SELECT id FROM outbox_events
            WHERE id=:id AND user_id=:user AND namespace=:namespace
              AND status='running' AND lease_token=:token AND locked_until>clock_timestamp()
            FOR UPDATE"""),
                self._lease(task),
            ).first()
            is not None
        )

    def complete(self, task: ClaimedTask) -> bool:
        return (
            self.connection.execute(
                text("""UPDATE outbox_events SET status='completed',
            completed_at=now(),applied_at=COALESCE(applied_at,clock_timestamp()),
            locked_until=NULL,locked_by=NULL,lease_token=NULL,last_error=NULL
            WHERE id=:id AND user_id=:user AND namespace=:namespace AND status='running'
                AND lease_token=:token AND locked_until>clock_timestamp()"""),
                self._lease(task),
            ).rowcount
            == 1
        )

    def was_applied(self, task: ClaimedTask) -> bool:
        return bool(
            self.connection.execute(
                text("""SELECT applied_at IS NOT NULL
            FROM outbox_events WHERE id=:id AND user_id=:user AND namespace=:namespace"""),
                self._lease(task),
            ).scalar_one()
        )

    def fail(self, task: ClaimedTask, error_type: str, retry_delay: float | None) -> bool:
        return (
            self.connection.execute(
                text("""UPDATE outbox_events SET status=:status,
            available_at=clock_timestamp()+(:delay*interval '1 second'),
            last_error=:error,locked_until=NULL,locked_by=NULL,lease_token=NULL
            WHERE id=:id AND user_id=:user AND namespace=:namespace AND status='running'
                AND lease_token=:token AND locked_until>clock_timestamp()"""),
                {
                    **self._lease(task),
                    "status": "failed" if retry_delay is None else "pending",
                    "delay": retry_delay or 0,
                    "error": error_type,
                },
            ).rowcount
            == 1
        )


class PostgresTaskQueue:
    """Claim in a short transaction; no connection is held during model requests."""

    def __init__(
        self,
        engine: Engine,
        *,
        access: AccessContext | None = None,
        exclude_test_runs: bool = False,
        l0_only: bool = False,
    ):
        self.engine = engine
        self.l0_only = l0_only
        self.access, self.exclude_test_runs = access, exclude_test_runs

    def claim(
        self,
        worker_id: str,
        lease_seconds: float,
        event_types: tuple[str, ...] = ("embedding.requested",),
    ) -> ClaimedTask | None:
        try:
            return self._claim(worker_id, lease_seconds, event_types)
        except SQLAlchemyError:
            raise MemoryStorageError("task storage operation failed") from None

    def _claim(
        self, worker_id: str, lease_seconds: float, event_types: tuple[str, ...]
    ) -> ClaimedTask | None:
        with self.engine.begin() as connection:
            row = (
                connection.execute(
                    text("""WITH candidate AS (
                SELECT id FROM outbox_events WHERE event_type=ANY(CAST(:types AS text[]))
                AND (:all_scopes OR (user_id=:user AND namespace=:ns AND agent_id=:agent))
                AND (NOT :exclude_test OR left(namespace,9)<>'test_run_')
                AND (NOT :l0_only OR EXISTS (
                    SELECT 1 FROM memory_nodes n WHERE n.id=outbox_events.node_id
                    AND n.user_id=outbox_events.user_id AND n.namespace=outbox_events.namespace
                    AND n.level=0)) AND (
                    (status='pending' AND available_at<=clock_timestamp()) OR
                    (status='running' AND locked_until<=clock_timestamp()))
                ORDER BY available_at,created_at,id FOR UPDATE SKIP LOCKED LIMIT 1)
                UPDATE outbox_events e SET status='running',attempts=e.attempts+1,
                    locked_by=:worker,locked_until=clock_timestamp()+(:lease*interval '1 second'),
                    lease_token=:token
                FROM candidate WHERE e.id=candidate.id RETURNING e.*"""),
                    {
                        "worker": worker_id,
                        "lease": lease_seconds,
                        "token": uuid4(),
                        "types": list(event_types),
                        "all_scopes": self.access is None,
                        "user": self.access.user_id if self.access else "",
                        "ns": self.access.namespace if self.access else "",
                        "agent": self.access.agent_id if self.access else "",
                        "exclude_test": self.exclude_test_runs,
                        "l0_only": self.l0_only,
                    },
                )
                .mappings()
                .first()
            )
        if row is None:
            return None
        payload = row["payload"]
        return ClaimedTask(
            row["id"],
            AccessContext(
                row["user_id"],
                row["agent_id"],
                row["namespace"],
                frozenset(row["authorized_domains"]),
            ),
            row["node_id"],
            row["node_version"],
            payload["content_hash"],
            TraceContext(row["trace_id"], row["parent_span_id"], None, row["correlation_id"]),
            row["lease_token"],
            row["attempts"],
            row["event_type"],
            row["generation"],
        )
