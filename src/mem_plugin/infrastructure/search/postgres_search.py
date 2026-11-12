"""Parameterized FTS/trigram SQL; no query is allowed to escape its access predicate."""

from sqlalchemy import bindparam, text
from sqlalchemy.dialects.postgresql import JSONB

from mem_plugin.engine.models import SearchFilters


def filter_sql(filters: SearchFilters) -> tuple[str, dict[str, object]]:
    conditions = [
        "lifecycle_status='active'",
        "(valid_from IS NULL OR valid_from<=:as_of)",
        "(valid_until IS NULL OR valid_until>:as_of)",
    ]
    values: dict[str, object] = {"as_of": filters.as_of}
    for key in ("scope", "domain", "level", "memory_type"):
        value = getattr(filters, key)
        if value is not None:
            conditions.append(f"{key}=:{key}_filter")
            values[key + "_filter"] = str(value) if key == "scope" else value
    for key, operator in (("occurred_after", ">="), ("occurred_before", "<=")):
        value = getattr(filters, key)
        if value is not None:
            conditions.append(f"occurred_at{operator}:{key}")
            values[key] = value
    for key in ("attributes", "domain_payload"):
        value = getattr(filters, key)
        if value:
            conditions.append(f"{key} @> :{key}_filter")
            values[key + "_filter"] = value
    return " AND ".join(conditions), values


def bind_filters(statement, values):
    for key in ("attributes_filter", "domain_payload_filter"):
        if key in values:
            statement = statement.bindparams(bindparam(key, type_=JSONB))
    return statement


def lexical_rows(connection, access, visibility, query, limit, filters, threshold):
    predicate, values = filter_sql(filters)
    # Transaction-local threshold is reset when this short read transaction closes.
    connection.execute(
        text("SELECT set_config('pg_trgm.similarity_threshold',:value,true)"),
        {"value": str(threshold)},
    )
    escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    statement = text(f"""SELECT * FROM memory_nodes
        WHERE {visibility} AND {predicate} AND (
            search_vector @@ websearch_to_tsquery('simple',:query)
            OR lower(content) LIKE :substring ESCAPE '\\'
            OR lower(content) % lower(:query))
        ORDER BY (strpos(lower(content),lower(:query))>0) DESC,
            ts_rank_cd(search_vector,websearch_to_tsquery('simple',:query)) DESC,
            similarity(lower(content),lower(:query)) DESC,updated_at DESC,id
        LIMIT :limit""")
    return (
        connection.execute(
            bind_filters(statement, values),
            {
                **access,
                **values,
                "query": query,
                "substring": "%" + escaped.lower() + "%",
                "limit": limit,
            },
        )
        .mappings()
        .all()
    )
