"""Audit shares the business connection; trace uses independent short transactions."""

from dataclasses import asdict

from sqlalchemy import bindparam, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import Connection, Engine

from mem_plugin.engine.models import AccessContext
from mem_plugin.observability.models import AuditEvent, Span


class AuditRepository:
    def __init__(self, connection: Connection, access: AccessContext):
        self.connection, self.access = connection, access

    def append(self, event: AuditEvent) -> None:
        self.connection.execute(
            text("""INSERT INTO memory_audit_events
            (id,user_id,namespace,memory_id,actor,action,old_version,new_version,reason,trace_id)
            VALUES (:id,:user_id,:namespace,:memory_id,:actor,:action,
                    :old_version,:new_version,:reason,:trace_id)"""),
            {**asdict(event), "user_id": self.access.user_id, "namespace": self.access.namespace},
        )


class PostgresTraceSink:
    def __init__(self, engine: Engine):
        self.engine = engine

    def write(self, span: Span) -> None:
        with self.engine.begin() as connection:
            connection.execute(
                text("""INSERT INTO memory_traces
                (trace_id,user_id,namespace,agent_id,correlation_id,operation,status,
                 started_at,ended_at,process_name)
                VALUES (:trace,:user_id,:namespace,:agent_id,:correlation,
                        :operation,:status,:started,:ended,:process)
                ON CONFLICT (trace_id) DO UPDATE SET status=
                    CASE WHEN :root THEN EXCLUDED.status ELSE memory_traces.status END,
                ended_at=CASE WHEN :root THEN EXCLUDED.ended_at ELSE memory_traces.ended_at END"""),
                {
                    "trace": span.context.trace_id,
                    "user_id": span.user_id,
                    "namespace": span.namespace,
                    "agent_id": span.agent_id,
                    "correlation": span.context.correlation_id,
                    "operation": span.operation,
                    "status": span.status,
                    "started": span.started_at,
                    "ended": span.ended_at,
                    "root": span.context.parent_span_id is None,
                    "process": span.process_name,
                },
            )
            connection.execute(
                text("""INSERT INTO memory_trace_spans
                (span_id,trace_id,parent_span_id,operation,status,started_at,
                 ended_at,duration_ms,error_type,attributes)
                VALUES (:span,:trace,:parent,:operation,:status,:started,:ended,
                        :duration,:error,:attributes)
                ON CONFLICT (span_id) DO UPDATE SET status=EXCLUDED.status,
                ended_at=EXCLUDED.ended_at,
                duration_ms=EXCLUDED.duration_ms,error_type=EXCLUDED.error_type,
                attributes=EXCLUDED.attributes""").bindparams(bindparam("attributes", type_=JSONB)),
                {
                    "span": span.context.span_id,
                    "trace": span.context.trace_id,
                    "parent": span.context.parent_span_id,
                    "operation": span.operation,
                    "status": span.status,
                    "started": span.started_at,
                    "ended": span.ended_at,
                    "duration": span.duration_ms,
                    "error": span.error_type,
                    "attributes": {**span.context.attributes, "process_name": span.process_name},
                },
            )
