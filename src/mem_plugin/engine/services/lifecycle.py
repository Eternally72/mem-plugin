"""Validity, review, confidence and verified archival without deleting primary facts."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from mem_plugin.engine.errors import (
    ArchiveError,
    InvalidMemoryInput,
    MemoryConflict,
    MemoryNotFound,
    TaskLeaseLost,
)
from mem_plugin.engine.models import ArchiveManifest, LifecyclePolicy
from mem_plugin.engine.policies import embedding_hash
from mem_plugin.engine.services.archive import checksum, encode_archive, verify_archive
from mem_plugin.engine.services.forget import forget_in_transaction
from mem_plugin.engine.services.graph import refresh_contexts
from mem_plugin.engine.services.tasks import dirty, enqueue
from mem_plugin.observability.models import AuditEvent


def _id(value):
    try:
        return UUID(str(value))
    except ValueError as exc:
        raise InvalidMemoryInput("invalid memory ID") from exc


class LifecycleService:
    def __init__(
        self,
        uow,
        store,
        tracer,
        policy=None,
        *,
        max_bytes=8388608,
        clock=None,
        consolidation_enabled=True,
    ):
        self._uow, self._store, self._tracer = uow, store, tracer
        self._policy = policy or LifecyclePolicy()
        self._max_bytes = max_bytes
        self._clock = clock or (lambda: datetime.now(UTC))
        self._consolidation_enabled = consolidation_enabled

    def _node(self, uow, node_id, *, lock=True):
        node = uow.nodes.get(_id(node_id), lock=lock)
        if node is None:
            raise MemoryNotFound("memory not found in access scope")
        return node

    def _lease(self, uow, task):
        if task is not None and not uow.outbox.lock_claim(task):
            raise TaskLeaseLost("task ownership was lost")

    def _complete(self, uow, task):
        if task is not None and not uow.outbox.complete(task):
            raise TaskLeaseLost("task lease expired")
        uow.commit()

    def _audit(self, uow, node, actor, trace, action, reason):
        uow.audit.append(
            AuditEvent(
                uuid4(), node.id, actor, action, node.version, node.version, reason, trace.trace_id
            )
        )

    def _bundle(self, uow, node):
        normalized = node
        if node.lifecycle_status == "archived":
            normalized = replace(
                node, lifecycle_status=node.archived_status or "stale", archived_status=None
            )
        records = uow.lifecycle.snapshot(normalized)
        payload = encode_archive(records, self._max_bytes)
        references = {node.id}
        for record in records:
            for key in ("node_id", "source_id", "target_id", "child_id", "parent_id"):
                value = record["data"].get(key)
                if value is not None:
                    references.add(_id(value))
        return payload, len(records), tuple(sorted(references))

    def archive(self, access, node_id, *, repair=False):
        with self._tracer.span(
            "archive", access.user_id, access.namespace, access.agent_id
        ) as trace:
            return self._archive(access, _id(node_id), uuid4(), trace, repair=repair)

    def _archive(self, access, node_id, operation, trace, *, task=None, repair=False):
        # Reservation survives a process crash; every final/temp file has a discoverable key.
        with self._uow(access) as uow:
            uow.graph.lock_scope()
            node = self._node(uow, node_id)
            self._lease(uow, task)
            if node.lifecycle_status not in {"active", "stale", "archived"}:
                raise MemoryConflict("memory cannot be archived in its current state")
            old = uow.lifecycle.manifest(node.id)
            if (
                node.lifecycle_status == "archived"
                and old
                and old.status == "complete"
                and not repair
            ):
                verify_archive(self._store.read(old.storage_key), old, node)
                self._complete(uow, task)
                return node
            payload, count, refs = self._bundle(uow, node)
            manifest = ArchiveManifest(
                node.id,
                old.storage_key if old else uuid4().hex,
                operation,
                checksum(payload),
                count,
                len(payload),
                node.version,
                node.archived_status
                or ("stale" if node.lifecycle_status == "archived" else node.lifecycle_status),
                "writing",
                refs,
            )
            uow.lifecycle.reserve(manifest)
            uow.commit()
        # Local bounded filesystem I/O holds the scope lock to fence concurrent forget/restore.
        with self._uow(access) as uow:
            uow.graph.lock_scope()
            node = self._node(uow, node_id)
            self._lease(uow, task)
            current = uow.lifecycle.manifest(node.id)
            if current is None or current.operation_id != operation or current.status != "writing":
                raise MemoryConflict("archive reservation changed")
            payload, count, _ = self._bundle(uow, node)
            if checksum(payload) != current.checksum or count != current.record_count:
                raise MemoryConflict("memory changed during archive preparation")
            with self._tracer.span(
                "archive.write_verify", access.user_id, access.namespace, access.agent_id
            ) as span:
                self._store.write(current.storage_key, payload)
                verify_archive(self._store.read(current.storage_key), current, node)
                span.attributes.update(record_count=count, byte_count=len(payload))
            uow.lifecycle.finish_archive(node)
            self._audit(uow, node, access.agent_id, trace, "archived", "verified archive committed")
            result = self._node(uow, node.id)
            self._complete(uow, task)
            return result

    def restore(self, access, node_id):
        with self._tracer.span(
            "restore", access.user_id, access.namespace, access.agent_id
        ) as trace:
            node_id = _id(node_id)
            with self._uow(access) as uow:
                uow.graph.lock_scope()
                node = self._node(uow, node_id)
                if self._policy.for_type(
                    node.memory_type
                ).retention_action == "forget" and self._archive_due(node, self._clock()):
                    raise MemoryConflict("retention deletion is due; restore is not permitted")
                manifest = uow.lifecycle.manifest(node_id)
                if node.lifecycle_status != "archived":
                    if manifest is None or manifest.status != "writing":
                        return node
                    self._store.delete(manifest.storage_key)
                    uow.lifecycle.invalidate_manifest(node.id, ())
                    uow.lifecycle.restore(node, node.lifecycle_status, self._clock())
                    self._audit(
                        uow, node, access.agent_id, trace, "restored", "pending archive cancelled"
                    )
                    result = self._node(uow, node_id)
                    uow.commit()
                    return result
                repair = manifest is not None and manifest.status == "invalidated"
            if repair:
                self._archive(access, node_id, uuid4(), trace, repair=True)
            with self._uow(access) as uow:
                uow.graph.lock_scope()
                node = self._node(uow, node_id)
                if node.lifecycle_status != "archived":
                    return node
                manifest = uow.lifecycle.manifest(node_id)
                if manifest is None or manifest.status != "complete":
                    raise ArchiveError("verified archive is not available")
                verify_archive(self._store.read(manifest.storage_key), manifest, node)
                now = self._clock()
                status = node.archived_status or manifest.source_status
                if (node.valid_until and node.valid_until <= now) or (
                    node.level > 0 and node.is_dirty
                ):
                    status = "stale"
                uow.lifecycle.restore(node, status, now)
                result = self._node(uow, node_id)
                if status == "active":
                    enqueue(uow, result, trace, "embedding.requested", result.lifecycle_revision)
                if result.level > 0 and result.is_dirty and self._consolidation_enabled:
                    dirty(uow, (result.id,), access.agent_id, trace, schedule=True)
                refresh_contexts(
                    uow,
                    (node_id,),
                    access.agent_id,
                    trace.trace_id,
                    schedule=self._consolidation_enabled,
                )
                self._audit(
                    uow,
                    node,
                    access.agent_id,
                    trace,
                    "restored",
                    "archive identity and checksum verified",
                )
                result = self._node(uow, node_id)
                uow.commit()
                return result

    def purge(self, uow, node_ids):
        # Includes archives of surviving neighbors whose context/relations mention forgotten IDs.
        for manifest in uow.lifecycle.related_archives(node_ids):
            self._store.delete(manifest.storage_key)
            if manifest.node_id in node_ids:
                uow.lifecycle.remove_manifest(manifest.node_id)
            else:
                uow.lifecycle.invalidate_manifest(manifest.node_id, node_ids)

    def _archive_due(self, node, now):
        rule = self._policy.for_type(node.memory_type)
        if node.lifecycle_status == "archived" and rule.retention_action != "forget":
            return False
        if (
            rule.retention_action == "archive"
            and node.restored_at
            and now < node.restored_at + timedelta(days=self._policy.restore_grace_days)
        ):
            return False
        anchor = max(node.created_at, node.last_confirmed_at or node.created_at)
        return bool(
            (node.expires_at and node.expires_at <= now)
            or (rule.archive_after_days and anchor + timedelta(days=rule.archive_after_days) <= now)
        )

    def schedule(self, access, limit=50):
        if type(limit) is not int or not 1 <= limit <= 200:
            raise InvalidMemoryInput("lifecycle batch must be 1..200")
        with self._tracer.span(
            "scheduler.lifecycle", access.user_id, access.namespace, access.agent_id
        ) as trace:
            with self._uow(access) as uow:
                uow.graph.lock_scope()
                nodes = uow.lifecycle.due(self._clock(), self._policy, limit)
                for node in nodes:
                    enqueue(uow, node, trace, "lifecycle.maintain", node.lifecycle_revision)
                uow.commit()
            trace.attributes["enqueued_count"] = len(nodes)
            return len(nodes)

    def execute(self, task):
        with self._tracer.span(
            "lifecycle", task.access.user_id, task.access.namespace, task.access.agent_id
        ) as trace:
            now = self._clock()
            with self._uow(task.access) as uow:
                uow.graph.lock_scope()
                self._lease(uow, task)
                node = uow.nodes.get(task.node_id, lock=True)
                manifest = uow.lifecycle.manifest(task.node_id)
                resume = (
                    manifest is not None
                    and manifest.status == "writing"
                    and manifest.operation_id == task.id
                )
                if (
                    node is None
                    or uow.outbox.was_applied(task)
                    or node.version != task.node_version
                    or embedding_hash(node.content) != task.content_hash
                    or (node.lifecycle_revision != task.generation and not resume)
                ):
                    self._complete(uow, task)
                    return "skipped"
                active = node.lifecycle_status == "active" or node.archived_status == "active"
                expire = bool(active and node.valid_until and node.valid_until <= now)
                rule = self._policy.for_type(node.memory_type)
                anchor = max(node.created_at, node.last_confirmed_at or node.created_at)
                review = bool(
                    node.lifecycle_status == "active"
                    and not node.needs_review
                    and rule.review_after_days
                    and anchor + timedelta(days=rule.review_after_days) <= now
                )
                confidence, decay_at = node.confidence, node.last_decay_at
                decay_anchor = max(anchor, node.last_decay_at or anchor)
                decay = bool(
                    node.lifecycle_status == "active"
                    and confidence is not None
                    and rule.confidence_half_life_days
                    and decay_anchor + timedelta(seconds=self._policy.decay_interval_seconds) <= now
                )
                if decay:
                    confidence *= 2 ** (
                        -(now - decay_anchor).total_seconds()
                        / 86400
                        / rule.confidence_half_life_days
                    )
                    decay_at = now
                if expire or review or decay:
                    uow.lifecycle.update(
                        node, expire=expire, review=review, confidence=confidence, decay_at=decay_at
                    )
                    for action, changed in (
                        ("validity_expired", expire),
                        ("review_requested", review),
                        ("confidence_decayed", decay),
                    ):
                        if changed:
                            self._audit(uow, node, task.access.agent_id, trace, action, action)
                if expire:
                    affected = uow.graph.ancestors((node.id,))
                    neighbors = uow.graph.context_neighbors(affected)
                    uow.nodes.invalidate(tuple(v for v in affected if v != node.id))
                    dirty(
                        uow,
                        affected,
                        task.access.agent_id,
                        trace,
                        schedule=self._consolidation_enabled,
                    )
                    refresh_contexts(
                        uow,
                        tuple(set(affected) | set(neighbors)),
                        task.access.agent_id,
                        trace.trace_id,
                        schedule=self._consolidation_enabled,
                    )
                node = self._node(uow, node.id)
                archive_due = self._archive_due(node, now)
                trace.attributes.update(
                    expired=expire, review_requested=review, decayed=decay, archive_due=archive_due
                )
                if not archive_due:
                    if manifest is not None and manifest.status == "writing":
                        self._store.delete(manifest.storage_key)
                        uow.lifecycle.invalidate_manifest(node.id, ())
                        if not (expire or review or decay):
                            # Cancellation is a new state even if no temporal rule fired.
                            # Otherwise this completed generation would suppress future TTL scans.
                            uow.lifecycle.update(
                                node,
                                expire=False,
                                review=False,
                                confidence=node.confidence,
                                decay_at=node.last_decay_at,
                            )
                    self._complete(uow, task)
                    return "completed"
                if rule.retention_action == "forget":
                    forget_in_transaction(
                        uow,
                        node,
                        task.access,
                        trace,
                        purge=self.purge,
                        schedule=self._consolidation_enabled,
                        reason="configured retention deletion",
                    )
                    uow.commit()
                    return "completed"
                # Keep metadata/audit durable even if later archival needs a retry.
                uow.commit()
            self._archive(task.access, node.id, task.id, trace, task=task)
            return "completed"

    def failure(self, task, error_type, retry_delay):
        with self._uow(task.access) as uow:
            self._lease(uow, task)
            if not uow.outbox.fail(task, error_type, retry_delay):
                raise TaskLeaseLost("task lease expired")
            uow.commit()
        return "failed" if retry_delay is None else "retry"
