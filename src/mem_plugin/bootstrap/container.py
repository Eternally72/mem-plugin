"""Composition root: initialize storage and inject it into the independent memory engine."""

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace

from mem_plugin.engine.api import NoteAPI
from mem_plugin.engine.ports import EmbeddingProvider, LLMProvider, RerankProvider


@dataclass(frozen=True, slots=True)
class ModelClients:
    embedding: EmbeddingProvider
    llm: LLMProvider
    reranker: RerankProvider


def _lifecycle(uow, tracer, settings, consolidation_enabled):
    from mem_plugin.engine.models import LifecyclePolicy, TypeLifecyclePolicy
    from mem_plugin.engine.services.lifecycle import LifecycleService
    from mem_plugin.infrastructure.archive.file_store import FileArchiveStore

    defaults = LifecyclePolicy()
    rules = {rule.memory_type: rule for rule in defaults.rules}
    for name, changes in settings.type_policies.items():
        rules[name] = replace(rules.get(name, TypeLifecyclePolicy(name)), **changes)
    return LifecycleService(
        uow,
        FileArchiveStore(settings.archive_dir, settings.max_bytes),
        tracer,
        LifecyclePolicy(
            tuple(rules.values()), settings.decay_interval_seconds, settings.restore_grace_days
        ),
        max_bytes=settings.max_bytes,
        consolidation_enabled=consolidation_enabled,
    )


def create_model_clients(*, llm_provider: str | None = None) -> ModelClients:
    """Read independent configs; only explicit client methods send requests."""
    from mem_plugin.config.settings import (
        load_embedding_settings,
        load_llm_settings,
        load_rerank_settings,
    )
    from mem_plugin.infrastructure.models.clients import ChatClient, EmbeddingClient, RerankClient

    embedding, llm, rerank = (
        load_embedding_settings(),
        load_llm_settings(provider=llm_provider),
        load_rerank_settings(),
    )
    return ModelClients(
        EmbeddingClient(
            embedding.base_url, embedding.api_key, embedding.model, embedding.dimension
        ),
        ChatClient(
            llm.provider,
            llm.base_url,
            llm.api_key,
            llm.model,
            timeout_seconds=llm.timeout_seconds,
            enable_thinking=llm.enable_thinking,
            reasoning_effort=llm.reasoning_effort,
        ),
        RerankClient(rerank.provider, rerank.base_url, rerank.api_key, rerank.model),
    )


@contextmanager
def create_note_service(
    database_url: str, *, use_models: bool = True, environ=None
) -> Iterator[NoteAPI]:
    """Check schema without migrating; close the pool on normal and exceptional exit."""
    from mem_plugin.config.settings import (
        load_consolidation_settings,
        load_embedding_settings,
        load_engine_settings,
        load_lifecycle_settings,
        load_rerank_settings,
    )
    from mem_plugin.engine.models import RetrievalPolicy
    from mem_plugin.engine.services.notes import NoteService
    from mem_plugin.engine.services.retrieval import RetrievalService
    from mem_plugin.infrastructure.models.clients import EmbeddingClient, RerankClient
    from mem_plugin.infrastructure.observability.postgres_store import PostgresTraceSink
    from mem_plugin.infrastructure.persistence.postgres.database import Database
    from mem_plugin.infrastructure.persistence.postgres.unit_of_work import PostgresUnitOfWork
    from mem_plugin.observability.tracing import Tracer

    database = Database(database_url)
    try:
        embedding = load_embedding_settings(environ=environ)
        policy = load_engine_settings(environ=environ)
        rerank = load_rerank_settings(environ=environ)
        database.check_schema()

        def uow(access):
            return PostgresUnitOfWork(database, access, embedding.model, embedding.dimension)

        tracer = Tracer(PostgresTraceSink(database.engine))
        retrieval = RetrievalService(
            uow,
            tracer,
            embedding=EmbeddingClient(
                embedding.base_url, embedding.api_key, embedding.model, embedding.dimension
            )
            if use_models and embedding.api_key
            else None,
            reranker=RerankClient(rerank.provider, rerank.base_url, rerank.api_key, rerank.model)
            if use_models and policy.rerank_enabled and rerank.api_key
            else None,
            policy=RetrievalPolicy(
                policy.candidate_limit,
                policy.rrf_k,
                policy.trigram_threshold,
                policy.rerank_enabled,
            ),
        )
        yield NoteService(
            uow,
            tracer,
            retrieval=retrieval,
            max_note_characters=policy.max_note_characters,
            consolidation_enabled=load_consolidation_settings(environ=environ).enabled,
            lifecycle=_lifecycle(
                uow,
                tracer,
                load_lifecycle_settings(environ=environ),
                load_consolidation_settings(environ=environ).enabled,
            ),
        )
    finally:
        database.close()


@contextmanager
def create_worker(database_url: str, *, environ=None, task_access=None):
    """Register only configured handlers; lifecycle needs no model credentials."""
    from mem_plugin.config.settings import (
        ConfigurationError,
        load_consolidation_settings,
        load_embedding_settings,
        load_graph_settings,
        load_lifecycle_settings,
        load_llm_settings,
        load_worker_settings,
    )
    from mem_plugin.engine.models import ConsolidationPolicy, GraphPolicy
    from mem_plugin.engine.services.consolidation import ConsolidationService
    from mem_plugin.engine.services.embedding import EmbeddingService
    from mem_plugin.engine.services.graph import GraphService
    from mem_plugin.infrastructure.jobs.postgres_outbox import PostgresTaskQueue
    from mem_plugin.infrastructure.models.clients import ChatClient, EmbeddingClient
    from mem_plugin.infrastructure.observability.postgres_store import PostgresTraceSink
    from mem_plugin.infrastructure.persistence.postgres.database import Database
    from mem_plugin.infrastructure.persistence.postgres.unit_of_work import PostgresUnitOfWork
    from mem_plugin.observability.tracing import Tracer
    from mem_plugin.workers.runner import Worker

    embedding, settings = (
        load_embedding_settings(environ=environ),
        load_worker_settings(environ=environ),
    )
    graph_settings = load_graph_settings(environ=environ)
    consolidation_settings = load_consolidation_settings(environ=environ)
    llm = load_llm_settings(environ=environ)
    lifecycle_settings = load_lifecycle_settings(environ=environ)
    if not embedding.api_key and not lifecycle_settings.enabled:
        raise ConfigurationError("EMBEDDING_API_KEY is required to start the worker")
    # Missing LLM does not block accepted notes from acquiring embeddings. Graph jobs wait.
    graph_enabled = graph_settings.enabled and bool(llm.api_key and llm.model)
    if graph_settings.enabled and not graph_enabled:
        import logging

        logging.getLogger(__name__).warning("graph_jobs_waiting_for_llm_configuration")
    database = Database(database_url)
    try:
        database.check_schema()
        tracer = Tracer(PostgresTraceSink(database.engine), process_name="worker")

        def uow(access):
            return PostgresUnitOfWork(database, access, embedding.model, embedding.dimension)

        service = (
            EmbeddingService(
                uow,
                EmbeddingClient(
                    embedding.base_url, embedding.api_key, embedding.model, embedding.dimension
                ),
                tracer,
                embedding.model,
                association_enabled=graph_settings.enabled,
                consolidation_enabled=consolidation_settings.enabled,
            )
            if embedding.api_key
            else None
        )
        graph = (
            GraphService(
                uow,
                ChatClient(
                    llm.provider,
                    llm.base_url,
                    llm.api_key,
                    llm.model,
                    timeout_seconds=llm.timeout_seconds,
                    enable_thinking=llm.enable_thinking,
                    reasoning_effort=llm.reasoning_effort,
                ),
                tracer,
                GraphPolicy(
                    graph_settings.candidate_limit,
                    graph_settings.max_distance,
                    graph_settings.min_confidence,
                    graph_settings.max_input_characters,
                    graph_settings.max_output_tokens,
                ),
                consolidation_enabled=consolidation_settings.enabled,
            )
            if graph_enabled
            else None
        )
        yield Worker(
            PostgresTaskQueue(
                database.engine, access=task_access, exclude_test_runs=task_access is None
            ),
            service,
            tracer,
            lease_seconds=settings.lease_seconds,
            max_attempts=settings.max_attempts,
            retry_seconds=settings.retry_seconds,
            graph=graph,
            lifecycle=_lifecycle(uow, tracer, lifecycle_settings, consolidation_settings.enabled)
            if lifecycle_settings.enabled
            else None,
            consolidation=ConsolidationService(
                uow,
                ChatClient(
                    llm.provider,
                    llm.base_url,
                    llm.api_key,
                    llm.model,
                    timeout_seconds=llm.timeout_seconds,
                    enable_thinking=llm.enable_thinking,
                    reasoning_effort=llm.reasoning_effort,
                ),
                tracer,
                ConsolidationPolicy(
                    **{
                        key: value
                        for key, value in asdict(consolidation_settings).items()
                        if key != "enabled"
                    }
                ),
            )
            if consolidation_settings.enabled and llm.api_key and llm.model
            else None,
        )
    finally:
        database.close()


@contextmanager
def create_scheduler(database_url: str, *, namespace: str | None = None, environ=None):
    from mem_plugin.config.settings import (
        ConfigurationError,
        load_consolidation_settings,
        load_embedding_settings,
        load_lifecycle_settings,
        load_scheduler_settings,
    )
    from mem_plugin.engine.models import AccessContext
    from mem_plugin.engine.services.scheduling import SchedulingService
    from mem_plugin.infrastructure.observability.postgres_store import PostgresTraceSink
    from mem_plugin.infrastructure.persistence.postgres.database import Database
    from mem_plugin.infrastructure.persistence.postgres.unit_of_work import PostgresUnitOfWork
    from mem_plugin.observability.tracing import Tracer
    from mem_plugin.scheduler.runner import Scheduler

    settings = load_scheduler_settings(environ=environ)
    consolidation_enabled = load_consolidation_settings(environ=environ).enabled
    lifecycle_settings = load_lifecycle_settings(environ=environ)
    if not consolidation_enabled and not lifecycle_settings.enabled:
        raise ConfigurationError("enable consolidation or lifecycle to start scheduler")
    if namespace is not None and namespace not in settings.namespaces:
        raise ConfigurationError("scheduler namespace is not authorized")
    embedding = load_embedding_settings(environ=environ)
    database = Database(database_url)
    try:
        database.check_schema()
        tracer = Tracer(PostgresTraceSink(database.engine), process_name="scheduler")
        service = SchedulingService(
            lambda ctx: PostgresUnitOfWork(database, ctx, embedding.model, embedding.dimension),
            tracer,
        )
        contexts = tuple(
            AccessContext(settings.user_id, settings.agent_id, ns, settings.domains)
            for ns in ((namespace,) if namespace else settings.namespaces)
        )

        def uow(ctx):
            return PostgresUnitOfWork(database, ctx, embedding.model, embedding.dimension)

        yield Scheduler(
            service,
            contexts,
            settings.intervals,
            settings.batch_size,
            consolidation_enabled=consolidation_enabled,
            lifecycle=_lifecycle(uow, tracer, lifecycle_settings, consolidation_enabled)
            if lifecycle_settings.enabled
            else None,
            lifecycle_interval=lifecycle_settings.interval_seconds,
        )
    finally:
        database.close()
