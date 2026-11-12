"""Scoped lifecycle persistence and archive inventory on the business transaction."""

from dataclasses import asdict

from sqlalchemy import text

from mem_plugin.engine.errors import MemoryAccessDenied
from mem_plugin.engine.models import ArchiveManifest
from mem_plugin.infrastructure.persistence.postgres.repositories import VISIBLE, _node, parameters


class LifecycleRepository:
    def __init__(self, connection, access, nodes):
        self.connection, self.access, self.nodes = connection, access, nodes

    def due(self, now, policy, limit):
        params = {
            **parameters(self.access),
            "now": now,
            "limit": limit,
            "decay_seconds": policy.decay_interval_seconds,
            "grace": policy.restore_grace_days,
        }
        expressions = {}
        for field in ("review_after_days", "archive_after_days", "confidence_half_life_days"):
            clauses = []
            for index, rule in enumerate(policy.rules):
                params[f"type_{index}"] = rule.memory_type
                params[f"{field}_{index}"] = getattr(rule, field)
                clauses.append(f"WHEN :type_{index} THEN :{field}_{index}")
            expressions[field] = (
                "CASE memory_type " + " ".join(clauses) + " ELSE 0 END" if clauses else "0"
            )
        delete_clauses = []
        for index, rule in enumerate(policy.rules):
            params[f"delete_{index}"] = int(rule.retention_action == "forget")
            delete_clauses.append(f"WHEN :type_{index} THEN :delete_{index}")
        expressions["retention_delete"] = (
            "CASE memory_type " + " ".join(delete_clauses) + " ELSE 0 END"
            if delete_clauses
            else "0"
        )
        rows = self.connection.execute(
            text(f"""WITH scoped AS (
            SELECT *,{expressions["review_after_days"]} AS review_days,
              {expressions["archive_after_days"]} AS archive_days,
              {expressions["confidence_half_life_days"]} AS half_life,
              {expressions["retention_delete"]} AS retention_delete,
              GREATEST(created_at,COALESCE(last_confirmed_at,created_at)) AS anchor
            FROM memory_nodes WHERE {VISIBLE})
            SELECT * FROM memory_nodes WHERE {VISIBLE} AND id IN (
             SELECT n.id FROM scoped n WHERE lifecycle_status IN ('active','stale','archived')
             AND (
              ((lifecycle_status='active' OR archived_status='active')
                AND valid_until IS NOT NULL AND valid_until<=:now)
              OR (lifecycle_status='active' AND NOT needs_review AND review_days>0
                  AND anchor+review_days*interval '1 day'<=:now)
              OR (lifecycle_status='active' AND confidence IS NOT NULL AND half_life>0
                  AND GREATEST(anchor,COALESCE(last_decay_at,anchor))
                      +:decay_seconds*interval '1 second'<=:now)
              OR ((lifecycle_status<>'archived' OR retention_delete=1)
                  AND (retention_delete=1 OR restored_at IS NULL
            OR restored_at+:grace*interval '1 day'<=:now)
                  AND ((expires_at IS NOT NULL AND expires_at<=:now)
                       OR (archive_days>0 AND anchor+archive_days*interval '1 day'<=:now)))
              OR EXISTS(SELECT 1 FROM memory_archive_manifests m WHERE m.user_id=:user_id
                  AND m.namespace=:namespace AND m.node_id=n.id AND m.status='writing'))
             AND NOT EXISTS(SELECT 1 FROM outbox_events e WHERE e.user_id=:user_id
               AND e.namespace=:namespace AND e.node_id=n.id AND e.event_type='lifecycle.maintain'
               AND (e.status IN ('pending','running') OR
                    (e.node_version=n.version AND e.generation=n.lifecycle_revision))))
            ORDER BY id LIMIT :limit"""),
            params,
        ).mappings()
        return [_node(row) for row in rows]

    def snapshot(self, node):
        records = [{"schema": 1, "type": "node", "data": asdict(node)}]
        for table, kind, order in (
            ("memory_versions", "version", "version"),
            ("memory_version_sources", "version_source", "version,source_id"),
        ):
            values = self.connection.execute(
                text(f"""SELECT * FROM {table}
                WHERE user_id=:user_id AND namespace=:namespace
            AND node_id=:id ORDER BY {order}"""),
                {**parameters(self.access), "id": node.id},
            ).mappings()
            for row in values:
                if kind == "version_source" and self.nodes.get(row["source_id"]) is None:
                    raise MemoryAccessDenied("archive source is not visible")
                records.append({"type": kind, "data": dict(row)})
        for table, kind, first, second in (
            ("memory_hierarchy_edges", "hierarchy", "child_id", "parent_id"),
            ("memory_semantic_edges", "semantic", "source_id", "target_id"),
        ):
            values = self.connection.execute(
                text(f"""WITH visible AS (
                SELECT id FROM memory_nodes WHERE {VISIBLE})
                SELECT e.* FROM {table} e JOIN visible a ON e.{first}=a.id
                JOIN visible b ON e.{second}=b.id WHERE e.user_id=:user_id
            AND e.namespace=:namespace
                AND (e.{first}=:id
            OR e.{second}=:id) ORDER BY e.{first},e.{second},e.relation_type"""),
                {**parameters(self.access), "id": node.id},
            ).mappings()
            records.extend({"type": kind, "data": dict(row)} for row in values)
        return tuple(records)

    def manifest(self, node_id):
        if self.nodes.get(node_id) is None:
            return None
        row = (
            self.connection.execute(
                text("""SELECT node_id,storage_key,operation_id,checksum,
            record_count,byte_count,node_version,source_status,status,source_ids
            FROM memory_archive_manifests
            WHERE user_id=:user_id AND namespace=:namespace AND node_id=:id"""),
                {**parameters(self.access), "id": node_id},
            )
            .mappings()
            .first()
        )
        return ArchiveManifest(**row) if row else None

    def reserve(self, manifest):
        self.connection.execute(
            text("""INSERT INTO memory_archive_manifests
            (user_id,namespace,node_id,storage_key,operation_id,checksum,record_count,
             byte_count,node_version,source_status,status,source_ids)
            VALUES (:user_id,:namespace,:node_id,:storage_key,:operation_id,:checksum,
                    :record_count,:byte_count,:node_version,:source_status,:status,
                    CAST(:source_ids AS uuid[]))
            ON CONFLICT(user_id,namespace,node_id) DO UPDATE SET operation_id=EXCLUDED.operation_id,
              checksum=EXCLUDED.checksum,record_count=EXCLUDED.record_count,
              byte_count=EXCLUDED.byte_count,
              node_version=EXCLUDED.node_version,source_status=EXCLUDED.source_status,
              status=EXCLUDED.status,source_ids=EXCLUDED.source_ids,updated_at=now()"""),
            {
                **parameters(self.access),
                **asdict(manifest),
                "source_ids": list(manifest.source_ids),
            },
        )

    def finish_archive(self, node):
        params = {**parameters(self.access), "id": node.id}
        self.connection.execute(
            text(f"""UPDATE memory_nodes SET
            archived_status=CASE WHEN lifecycle_status='archived'
            THEN COALESCE(archived_status,'stale') ELSE lifecycle_status END,
            lifecycle_status='archived',embedding_status='pending',lifecycle_revision=lifecycle_revision+1,
            updated_at=now() WHERE {VISIBLE} AND id=:id"""),
            params,
        )
        self.connection.execute(
            text("""DELETE FROM memory_embeddings WHERE user_id=:user_id
            AND namespace=:namespace AND node_id=:id"""),
            params,
        )
        self.connection.execute(
            text("""UPDATE memory_archive_manifests SET status='complete',updated_at=now()
            WHERE user_id=:user_id AND namespace=:namespace AND node_id=:id"""),
            params,
        )
        self.connection.execute(
            text("""UPDATE outbox_events SET status='failed',last_error='memory archived',
            locked_until=NULL,locked_by=NULL,lease_token=NULL WHERE user_id=:user_id
            AND namespace=:namespace AND node_id=:id AND event_type<>'lifecycle.maintain'
            AND status IN ('pending','running')"""),
            params,
        )

    def restore(self, node, status, now):
        self.connection.execute(
            text(f"""UPDATE memory_nodes SET lifecycle_status=:status,
            archived_status=NULL,restored_at=:now,
            embedding_status=CASE WHEN lifecycle_status='archived' THEN 'pending'
            ELSE embedding_status END,
            lifecycle_revision=lifecycle_revision+1,updated_at=:now WHERE {VISIBLE} AND id=:id"""),
            {**parameters(self.access), "id": node.id, "status": status, "now": now},
        )

    def update(self, node, *, expire, review, confidence, decay_at):
        self.connection.execute(
            text(f"""UPDATE memory_nodes SET
            lifecycle_status=CASE WHEN :expire AND lifecycle_status='active' THEN 'stale'
                ELSE lifecycle_status END,
            archived_status=CASE WHEN :expire AND lifecycle_status='archived' THEN 'stale'
                ELSE archived_status END,
            needs_review=needs_review OR :review,confidence=:confidence,last_decay_at=:decay_at,
            lifecycle_revision=lifecycle_revision+1 WHERE {VISIBLE} AND id=:id"""),
            {
                **parameters(self.access),
                "id": node.id,
                "expire": expire,
                "review": review,
                "confidence": confidence,
                "decay_at": decay_at,
            },
        )

    def remove_manifest(self, node_id):
        self.connection.execute(
            text("""DELETE FROM memory_archive_manifests
            WHERE user_id=:user_id AND namespace=:namespace AND node_id=:id"""),
            {**parameters(self.access), "id": node_id},
        )

    def related_archives(self, node_ids):
        rows = self.connection.execute(
            text("""SELECT node_id,storage_key,operation_id,checksum,
            record_count,byte_count,node_version,source_status,status,source_ids
            FROM memory_archive_manifests WHERE user_id=:user_id AND namespace=:namespace
            AND (node_id=ANY(CAST(:ids AS uuid[])) OR source_ids && CAST(:ids AS uuid[]))"""),
            {**parameters(self.access), "ids": list(node_ids)},
        ).mappings()
        return [ArchiveManifest(**row) for row in rows]

    def invalidate_manifest(self, node_id, removed_ids):
        self.connection.execute(
            text("""UPDATE memory_archive_manifests SET status='invalidated',
            source_ids=ARRAY(SELECT ref
            FROM unnest(source_ids) ref WHERE NOT ref=ANY(CAST(:removed AS uuid[]))),
            updated_at=now() WHERE user_id=:user_id AND namespace=:namespace AND node_id=:id"""),
            {**parameters(self.access), "id": node_id, "removed": list(removed_ids)},
        )
