from __future__ import annotations

import json
import os
import re
import sqlite3
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

from .normalizers import (
    get_domain,
    is_media_asset_url,
    is_probable_phone_vn_evidence,
    normalize_by_rule,
    normalize_url,
    normalize_url_without_query,
)


ROOT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_DB_PATH = ROOT_DIR / "data" / "ioc_investigator.sqlite3"
DEFAULT_WORKSPACE_ID = 1
DEFAULT_WORKSPACE_NAME = "Default"
DEFAULT_WORKSPACE_SLUG = "default"
POSTGRES_BACKENDS = {"postgres", "postgresql"}
SQLITE_BACKENDS = {"sqlite", "sqlite3"}
WORKSPACE_SCOPED_TABLES = (
    "queues",
    "queue_routes",
    "keywords",
    "search_queries",
    "jobs",
    "worker_slots",
    "search_queue_items",
    "urls",
    "whitelist_urls",
    "url_queue_items",
    "url_sources",
    "iocs",
    "ioc_sources",
)
TIMESTAMP_COLUMNS = (
    "created_at",
    "updated_at",
    "started_at",
    "finished_at",
    "stopped_at",
    "crawled_at",
    "heartbeat_at",
    "counts_updated_at",
    "deleted_at",
    "collected_at",
)


def db_path() -> Path:
    return Path(os.environ.get("IOC_DB_PATH", DEFAULT_DB_PATH))


def db_backend() -> str:
    explicit = os.environ.get("DB_BACKEND", "").strip().lower()
    if explicit in POSTGRES_BACKENDS:
        return "postgresql"
    if explicit in SQLITE_BACKENDS:
        return "sqlite"

    database_url = os.environ.get("DATABASE_URL", "").strip().lower()
    if database_url.startswith(("postgres://", "postgresql://")):
        return "postgresql"
    return "sqlite"


def is_postgres_backend() -> bool:
    return db_backend() == "postgresql"


def database_label() -> str:
    if not is_postgres_backend():
        return str(db_path())
    database_url = os.environ.get("DATABASE_URL", "").strip()
    if database_url:
        return redact_database_url(database_url)
    host = os.environ.get("POSTGRES_HOST", "localhost")
    port = os.environ.get("POSTGRES_PORT", "5432")
    database = os.environ.get("POSTGRES_DB", "ioc_investigator")
    user = os.environ.get("POSTGRES_USER", "ioc_app")
    return f"postgresql://{user}:***@{host}:{port}/{database}"


def redact_database_url(database_url: str) -> str:
    return re.sub(r"://([^:/@]+):([^@]+)@", r"://\1:***@", database_url)


def connect() -> sqlite3.Connection | "PostgresConnection":
    if is_postgres_backend():
        return connect_postgres()

    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=float(os.environ.get("SQLITE_BUSY_TIMEOUT", "30")))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def connect_postgres() -> "PostgresConnection":
    try:
        import psycopg
    except ImportError as exc:
        raise RuntimeError(
            "PostgreSQL mode requires psycopg. Install requirements.txt or run the production image."
        ) from exc

    database_url = os.environ.get("DATABASE_URL", "").strip()
    connect_timeout = int(os.environ.get("POSTGRES_CONNECT_TIMEOUT", "30"))
    if database_url:
        raw_conn = psycopg.connect(database_url, connect_timeout=connect_timeout)
    else:
        raw_conn = psycopg.connect(
            host=os.environ.get("POSTGRES_HOST", "localhost"),
            port=int(os.environ.get("POSTGRES_PORT", "5432")),
            dbname=os.environ.get("POSTGRES_DB", "ioc_investigator"),
            user=os.environ.get("POSTGRES_USER", "ioc_app"),
            password=os.environ.get("POSTGRES_PASSWORD", ""),
            connect_timeout=connect_timeout,
        )
    return PostgresConnection(raw_conn)


class DbRow(Mapping[str, Any]):
    def __init__(self, columns: list[str], values: tuple[Any, ...]):
        self._columns = tuple(columns)
        self._values = tuple(values)
        self._data = dict(zip(self._columns, self._values))

    def __getitem__(self, key: str | int) -> Any:
        if isinstance(key, int):
            return self._values[key]
        return self._data[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._columns)

    def __len__(self) -> int:
        return len(self._columns)

    def __getattr__(self, name: str) -> Any:
        try:
            return self._data[name]
        except KeyError as exc:
            raise AttributeError(name) from exc

    def keys(self):
        return self._data.keys()


class PostgresCursor:
    def __init__(self, cursor):
        self._cursor = cursor
        self.rowcount = cursor.rowcount

    def fetchone(self):
        row = self._cursor.fetchone()
        if row is None:
            return None
        return self._wrap_row(row)

    def fetchall(self):
        return [self._wrap_row(row) for row in self._cursor.fetchall()]

    def __iter__(self):
        for row in self._cursor:
            yield self._wrap_row(row)

    def _wrap_row(self, row):
        if self._cursor.description is None:
            return row
        columns = [column.name for column in self._cursor.description]
        return DbRow(columns, tuple(row))


class PostgresConnection:
    driver = "postgresql"

    def __init__(self, conn):
        self._conn = conn
        self.total_changes = 0

    def execute(self, sql: str, params: Any = None) -> PostgresCursor:
        translated_sql, translated_params = translate_postgres_sql(sql, params)
        cursor = self._conn.cursor()
        cursor.execute(translated_sql, translated_params)
        self._record_changes(cursor.rowcount)
        return PostgresCursor(cursor)

    def executemany(self, sql: str, params_seq) -> PostgresCursor:
        translated_sql, _ = translate_postgres_sql(sql, None)
        cursor = self._conn.cursor()
        cursor.executemany(translated_sql, params_seq)
        self._record_changes(cursor.rowcount)
        return PostgresCursor(cursor)

    def executescript(self, script: str) -> None:
        for statement in split_sql_script(script):
            if statement.strip():
                self.execute(statement)

    def commit(self) -> None:
        self._conn.commit()

    def rollback(self) -> None:
        self._conn.rollback()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "PostgresConnection":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if exc_type is None:
            self.commit()
        else:
            self.rollback()
        self.close()

    def _record_changes(self, rowcount: int) -> None:
        if rowcount and rowcount > 0:
            self.total_changes += rowcount


def is_postgres_connection(conn) -> bool:
    return getattr(conn, "driver", "") == "postgresql"


def translate_postgres_sql(sql: str, params: Any = None) -> tuple[str, Any]:
    translated = sql
    translated = re.sub(
        r"\bBEGIN\s+IMMEDIATE\b",
        "SELECT pg_advisory_xact_lock(hashtext('url_hunter_write_transaction'))",
        translated,
        flags=re.IGNORECASE,
    )
    translated = translate_postgres_ddl(translated)
    translated = re.sub(
        r"datetime\('now'\s*,\s*\?\)",
        "(CURRENT_TIMESTAMP + ?::interval)",
        translated,
        flags=re.IGNORECASE,
    )
    translated = re.sub(
        r"GROUP_CONCAT\(\s*DISTINCT\s+([^)]+?)\s*\)",
        r"STRING_AGG(DISTINCT \1::text, ',')",
        translated,
        flags=re.IGNORECASE,
    )

    insert_or_ignore = bool(
        re.search(r"\bINSERT\s+OR\s+IGNORE\s+INTO\b", translated, flags=re.IGNORECASE)
    )
    translated = re.sub(
        r"\bINSERT\s+OR\s+IGNORE\s+INTO\b",
        "INSERT INTO",
        translated,
        flags=re.IGNORECASE,
    )
    if insert_or_ignore and "ON CONFLICT" not in translated.upper():
        translated = append_on_conflict_do_nothing(translated)

    if isinstance(params, dict):
        translated = replace_named_placeholders(translated)
    else:
        translated = replace_qmark_placeholders(translated)
    return translated, params


def translate_postgres_ddl(sql: str) -> str:
    translated = re.sub(
        r"\bINTEGER\s+PRIMARY\s+KEY\s+AUTOINCREMENT\b",
        "BIGSERIAL PRIMARY KEY",
        sql,
        flags=re.IGNORECASE,
    )
    translated = re.sub(
        r"\b([a-zA-Z_]+_id)\s+INTEGER\b",
        r"\1 BIGINT",
        translated,
        flags=re.IGNORECASE,
    )
    for column in TIMESTAMP_COLUMNS:
        translated = re.sub(
            rf"\b{column}\s+TEXT\b",
            f"{column} TIMESTAMPTZ",
            translated,
            flags=re.IGNORECASE,
        )
    return translated


def append_on_conflict_do_nothing(sql: str) -> str:
    stripped = sql.rstrip()
    if stripped.endswith(";"):
        return f"{stripped[:-1]} ON CONFLICT DO NOTHING;"
    return f"{stripped} ON CONFLICT DO NOTHING"


def replace_qmark_placeholders(sql: str) -> str:
    result: list[str] = []
    in_single = False
    in_double = False
    index = 0
    while index < len(sql):
        char = sql[index]
        if char == "'" and not in_double:
            result.append(char)
            if in_single and index + 1 < len(sql) and sql[index + 1] == "'":
                index += 1
                result.append(sql[index])
            else:
                in_single = not in_single
        elif char == '"' and not in_single:
            in_double = not in_double
            result.append(char)
        elif char == "?" and not in_single and not in_double:
            result.append("%s")
        else:
            result.append(char)
        index += 1
    return "".join(result)


def replace_named_placeholders(sql: str) -> str:
    result: list[str] = []
    in_single = False
    in_double = False
    index = 0
    while index < len(sql):
        char = sql[index]
        if char == "'" and not in_double:
            result.append(char)
            if in_single and index + 1 < len(sql) and sql[index + 1] == "'":
                index += 1
                result.append(sql[index])
            else:
                in_single = not in_single
            index += 1
            continue
        if char == '"' and not in_single:
            in_double = not in_double
            result.append(char)
            index += 1
            continue
        if char == ":" and not in_single and not in_double:
            match = re.match(r":([A-Za-z_][A-Za-z0-9_]*)", sql[index:])
            if match:
                name = match.group(1)
                result.append(f"%({name})s")
                index += len(name) + 1
                continue
        result.append(char)
        index += 1
    return "".join(result)


def split_sql_script(script: str) -> list[str]:
    statements: list[str] = []
    current: list[str] = []
    in_single = False
    in_double = False
    index = 0
    while index < len(script):
        char = script[index]
        if char == "'" and not in_double:
            current.append(char)
            if in_single and index + 1 < len(script) and script[index + 1] == "'":
                index += 1
                current.append(script[index])
            else:
                in_single = not in_single
        elif char == '"' and not in_single:
            in_double = not in_double
            current.append(char)
        elif char == ";" and not in_single and not in_double:
            statement = "".join(current).strip()
            if statement:
                statements.append(statement)
            current = []
        else:
            current.append(char)
        index += 1
    statement = "".join(current).strip()
    if statement:
        statements.append(statement)
    return statements


def whitelist_match_sql(url_expr: str, alias: str = "wu") -> str:
    match_value = f"COALESCE({alias}.match_value, {alias}.url_norm)"
    match_type = f"COALESCE({alias}.match_type, 'exact')"
    return f"""
    (
      ({match_type} = 'exact' AND {alias}.url_norm = {url_expr})
      OR (
        {match_type} = 'prefix'
        AND (
          {url_expr} = {match_value}
          OR substr({url_expr}, 1, length({match_value})) = {match_value}
          OR (
            substr({match_value}, length({match_value}), 1) = '/'
            AND (
              {url_expr} = substr({match_value}, 1, length({match_value}) - 1)
              OR substr({url_expr}, 1, length({match_value})) =
                substr({match_value}, 1, length({match_value}) - 1) || '?'
            )
          )
        )
      )
    )
    """


def is_url_whitelisted(
    conn: sqlite3.Connection,
    url_norm: str,
    workspace_id: int | None = None,
) -> bool:
    workspace_id = workspace_id or DEFAULT_WORKSPACE_ID
    return bool(
        conn.execute(
            f"""
            SELECT 1
            FROM whitelist_urls wu
            WHERE wu.enabled = 1
              AND wu.workspace_id = :workspace_id
              AND {whitelist_match_sql(':url_norm', 'wu')}
            LIMIT 1
            """,
            {"url_norm": url_norm, "workspace_id": workspace_id},
        ).fetchone()
    )


def is_url_whitelisted_latest(url_norm: str, workspace_id: int | None = None) -> bool:
    with connect() as conn:
        return is_url_whitelisted(conn, url_norm, workspace_id)


def soft_delete_whitelisted_iocs(
    conn: sqlite3.Connection,
    whitelist_id: int | None = None,
    workspace_id: int | None = None,
) -> int:
    scope_filter = ""
    scope_params: list[object] = []
    scope_clauses: list[str] = []
    if workspace_id is not None:
        scope_clauses.append("workspace_wu.workspace_id = ?")
        scope_params.append(workspace_id)
    if whitelist_id is not None:
        scope_clauses.append("workspace_wu.id = ?")
        scope_params.append(whitelist_id)
    if scope_clauses:
        scope_filter = f"AND ({' OR '.join(scope_clauses)})"

    whitelist_filter = "wu.enabled = 1"
    whitelist_params: list[object] = []
    if workspace_id is not None:
        whitelist_filter = f"{whitelist_filter} AND wu.workspace_id = ?"
        whitelist_params.append(workspace_id)
    if whitelist_id is not None:
        whitelist_filter = f"{whitelist_filter} AND wu.id = ?"
        whitelist_params.append(whitelist_id)

    row_count = conn.execute(
        f"""
        UPDATE iocs
        SET deleted = 1,
            deleted_at = CURRENT_TIMESTAMP
        WHERE COALESCE(deleted, 0) = 0
          AND type IN ('url', 'domain')
          AND EXISTS (
            SELECT 1
            FROM whitelist_urls workspace_wu
            WHERE workspace_wu.enabled = 1
              AND workspace_wu.workspace_id = iocs.workspace_id
              {scope_filter}
          )
          AND EXISTS (
            SELECT 1
            FROM whitelist_urls wu
            WHERE {whitelist_filter}
              AND wu.workspace_id = iocs.workspace_id
              AND (
                (
                  iocs.type = 'url'
                  AND {whitelist_match_sql('iocs.value_norm', 'wu')}
                )
                OR (
                  iocs.type = 'domain'
                  AND (
                    {whitelist_match_sql("'https://' || iocs.value_norm || '/'", 'wu')}
                    OR {whitelist_match_sql("'http://' || iocs.value_norm || '/'", 'wu')}
                  )
                )
              )
          )
        """,
        (*scope_params, *whitelist_params),
    ).rowcount
    return max(row_count, 0)


def soft_delete_static_url_iocs(conn: sqlite3.Connection) -> int:
    if not table_exists(conn, "iocs"):
        return 0

    rows = conn.execute(
        """
        SELECT id, value_norm
        FROM iocs
        WHERE type = 'url'
          AND COALESCE(deleted, 0) = 0
        """
    ).fetchall()
    deleted_count = 0
    for row in rows:
        if not is_media_asset_url(row["value_norm"]):
            continue
        changed = conn.execute(
            """
            UPDATE iocs
            SET deleted = 1,
                deleted_at = CURRENT_TIMESTAMP
            WHERE id = ?
              AND COALESCE(deleted, 0) = 0
            """,
            (row["id"],),
        ).rowcount
        if changed and changed > 0:
            deleted_count += changed
    return deleted_count


def init_db() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)
        migrate_db(conn)
        recover_interrupted_jobs(conn)
        cleanup_malformed_review_queue_urls(conn)
        seed_defaults(conn)


def migrate_db(conn: sqlite3.Connection) -> None:
    ensure_workspace_schema(conn)
    ensure_workspace_columns(conn)
    backfill_workspace_ids(conn)
    migrate_workspace_unique_constraints(conn)
    ensure_column(conn, "queues", "max_concurrent_jobs", "INTEGER NOT NULL DEFAULT 1")
    ensure_column(conn, "queues", "active_max_concurrent_jobs", "INTEGER")
    ensure_column(conn, "search_queries", "queue_id", "INTEGER REFERENCES queues(id)")
    ensure_column(conn, "search_queries", "result_count", "INTEGER NOT NULL DEFAULT 0")
    ensure_column(conn, "search_queries", "page_count", "INTEGER NOT NULL DEFAULT 0")
    ensure_column(conn, "search_queries", "last_error", "TEXT")
    ensure_column(conn, "search_queries", "started_at", "TEXT")
    ensure_column(conn, "search_queries", "finished_at", "TEXT")
    ensure_column(conn, "keywords", "paused_by_user", "INTEGER NOT NULL DEFAULT 0")
    ensure_column(conn, "jobs", "queue_id", "INTEGER REFERENCES queues(id)")
    ensure_column(conn, "jobs", "target_url_id", "INTEGER")
    ensure_column(conn, "jobs", "target_queue_item_id", "INTEGER")
    ensure_column(conn, "jobs", "worker_slot_key", "TEXT")
    ensure_column(conn, "jobs", "run_token", "TEXT")
    ensure_column(conn, "jobs", "heartbeat_at", "TEXT")
    ensure_column(conn, "search_queue_items", "output_url_queue_id", "INTEGER REFERENCES queues(id)")
    ensure_column(conn, "url_sources", "queue_id", "INTEGER REFERENCES queues(id)")
    ensure_column(conn, "url_queue_items", "source_queue_id", "INTEGER REFERENCES queues(id)")
    ensure_column(conn, "url_queue_items", "source_search_query_id", "INTEGER REFERENCES search_queries(id)")
    ensure_column(conn, "url_queue_items", "source_search_queue_item_id", "INTEGER REFERENCES search_queue_items(id)")
    ensure_column(conn, "url_queue_items", "source_url_queue_item_id", "INTEGER REFERENCES url_queue_items(id)")
    ensure_column(conn, "urls", "title", "TEXT")
    ensure_column(conn, "urls", "content_type", "TEXT")
    ensure_column(conn, "urls", "content_length", "INTEGER")
    ensure_column(conn, "urls", "fetch_method", "TEXT")
    ensure_column(conn, "urls", "crawl_error", "TEXT")
    ensure_column(conn, "whitelist_urls", "match_type", "TEXT NOT NULL DEFAULT 'exact'")
    ensure_column(conn, "whitelist_urls", "match_value", "TEXT")
    ensure_column(conn, "whitelist_urls", "matched_url_count", "INTEGER NOT NULL DEFAULT 0")
    ensure_column(conn, "whitelist_urls", "queue_item_count", "INTEGER NOT NULL DEFAULT 0")
    ensure_column(conn, "whitelist_urls", "counts_updated_at", "TEXT")
    ensure_column(conn, "iocs", "deleted", "INTEGER NOT NULL DEFAULT 0")
    ensure_column(
        conn,
        "iocs",
        "collected_at",
        "TIMESTAMPTZ" if is_postgres_connection(conn) else "TEXT",
    )
    ensure_column(
        conn,
        "iocs",
        "deleted_at",
        "TIMESTAMPTZ" if is_postgres_connection(conn) else "TEXT",
    )
    ensure_column(conn, "ioc_sources", "source_type", "TEXT NOT NULL DEFAULT 'crawl'")
    conn.execute(
        """
        UPDATE whitelist_urls
        SET match_type = COALESCE(match_type, 'exact'),
            match_value = COALESCE(match_value, url_norm)
        """
    )
    conn.execute(
        """
        UPDATE queues
        SET max_concurrent_jobs = 1
        WHERE max_concurrent_jobs IS NULL
           OR max_concurrent_jobs < 1
        """
    )
    conn.execute(
        """
        UPDATE queues
        SET active_max_concurrent_jobs = max_concurrent_jobs
        WHERE active_max_concurrent_jobs IS NULL
           OR active_max_concurrent_jobs < 1
        """
    )
    conn.execute(
        """
        UPDATE queues
        SET status = 'draft',
            stopped_at = NULL,
            updated_at = CURRENT_TIMESTAMP
        WHERE status = 'paused'
          AND started_at IS NULL
          AND NOT EXISTS (
            SELECT 1
            FROM jobs j
            WHERE j.queue_id = queues.id
              AND j.status IN ('pending', 'running', 'failed', 'done')
          )
        """
    )
    migrate_domain_tables_into_urls(conn)
    remove_crawled_urls_from_queue_items(conn)
    remove_ignored_asset_urls_from_queue_items(conn)
    migrate_job_targets(conn)
    migrate_url_bodies(conn)
    backfill_keyword_search_url_iocs(conn)
    sync_ioc_workspaces_from_sources(conn)
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS worker_slots (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          slot_key TEXT NOT NULL UNIQUE,
          workspace_id INTEGER NOT NULL DEFAULT 1,
          worker_type TEXT NOT NULL,
          enabled INTEGER NOT NULL DEFAULT 1,
          status TEXT NOT NULL DEFAULT 'idle',
          job_id INTEGER,
          queue_id INTEGER,
          target_url_id INTEGER,
          target_queue_item_id INTEGER,
          run_token TEXT,
          thread_name TEXT,
          pid INTEGER,
          started_at TEXT,
          heartbeat_at TEXT,
          finished_at TEXT,
          error TEXT,
          created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
          updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        DROP INDEX IF EXISTS idx_jobs_status_type_id;
        CREATE INDEX IF NOT EXISTS idx_jobs_queue ON jobs(queue_id, status, id);
        CREATE INDEX IF NOT EXISTS idx_jobs_type_status_id ON jobs(type, status, id);
        CREATE INDEX IF NOT EXISTS idx_jobs_crawl_target ON jobs(type, status, target_url_id);
        CREATE INDEX IF NOT EXISTS idx_jobs_worker_slot ON jobs(worker_slot_key, status);
        CREATE INDEX IF NOT EXISTS idx_search_queries_queue ON search_queries(queue_id, status);
        CREATE INDEX IF NOT EXISTS idx_search_queue_items_queue ON search_queue_items(queue_id, status);
        CREATE INDEX IF NOT EXISTS idx_search_queue_items_output ON search_queue_items(output_url_queue_id);
        CREATE INDEX IF NOT EXISTS idx_url_queue_items_queue ON url_queue_items(queue_id, status);
        CREATE INDEX IF NOT EXISTS idx_url_queue_items_url ON url_queue_items(url_id, queue_id, status);
        CREATE INDEX IF NOT EXISTS idx_url_sources_url ON url_sources(url_id);
        CREATE INDEX IF NOT EXISTS idx_url_sources_queue_type ON url_sources(queue_id, source_type, created_at);
        CREATE INDEX IF NOT EXISTS idx_url_sources_search_query ON url_sources(search_query_id);
        CREATE INDEX IF NOT EXISTS idx_ioc_sources_ioc ON ioc_sources(ioc_id);
        CREATE INDEX IF NOT EXISTS idx_ioc_sources_source_url ON ioc_sources(source_url_id);
        CREATE INDEX IF NOT EXISTS idx_ioc_sources_type ON ioc_sources(source_type);
        CREATE INDEX IF NOT EXISTS idx_ioc_sources_workspace ON ioc_sources(workspace_id, ioc_id, source_url_id);
        CREATE INDEX IF NOT EXISTS idx_queue_routes_keyword ON queue_routes(keyword_queue_id);
        CREATE INDEX IF NOT EXISTS idx_queue_routes_url ON queue_routes(url_queue_id);
        CREATE INDEX IF NOT EXISTS idx_whitelist_urls_enabled ON whitelist_urls(enabled, url_norm);
        CREATE INDEX IF NOT EXISTS idx_whitelist_urls_match ON whitelist_urls(enabled, match_type, match_value);
        CREATE INDEX IF NOT EXISTS idx_worker_slots_type ON worker_slots(worker_type, enabled, status);
        CREATE INDEX IF NOT EXISTS idx_queues_workspace ON queues(workspace_id, queue_type, status);
        CREATE INDEX IF NOT EXISTS idx_keywords_workspace ON keywords(workspace_id, status, id);
        CREATE INDEX IF NOT EXISTS idx_jobs_workspace ON jobs(workspace_id, status, id);
        CREATE INDEX IF NOT EXISTS idx_urls_workspace_review ON urls(workspace_id, review_status, crawl_status);
        CREATE INDEX IF NOT EXISTS idx_urls_workspace_domain ON urls(workspace_id, domain);
        CREATE INDEX IF NOT EXISTS idx_whitelist_urls_workspace ON whitelist_urls(workspace_id, enabled, url_norm);
        CREATE INDEX IF NOT EXISTS idx_iocs_workspace_type ON iocs(workspace_id, type, deleted);
        """
    )
    soft_delete_whitelisted_iocs(conn)
    soft_delete_static_url_iocs(conn)


def table_exists(conn: sqlite3.Connection, table: str) -> bool:
    if is_postgres_connection(conn):
        return bool(
            conn.execute(
                """
                SELECT 1
                FROM information_schema.tables
                WHERE table_schema = 'public'
                  AND table_name = ?
                """,
                (table,),
            ).fetchone()
        )
    return bool(
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,),
        ).fetchone()
    )


def ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    if not table_exists(conn, table):
        return
    existing = table_columns(conn, table)
    if column not in existing:
        if is_postgres_connection(conn):
            conn.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {definition}")
        else:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    if is_postgres_connection(conn):
        return {
            row["column_name"]
            for row in conn.execute(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = ?
                """,
                (table,),
            )
        }
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


def ensure_workspace_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS workspaces (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          name TEXT NOT NULL UNIQUE,
          slug TEXT NOT NULL UNIQUE,
          description TEXT,
          created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
          updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        """
    )
    conn.execute(
        """
        INSERT INTO workspaces(id, name, slug, description)
        VALUES (?, ?, ?, 'Default workspace for data collected before workspace support.')
        ON CONFLICT(id) DO UPDATE SET
            name = COALESCE(workspaces.name, excluded.name),
            slug = COALESCE(workspaces.slug, excluded.slug)
        """,
        (DEFAULT_WORKSPACE_ID, DEFAULT_WORKSPACE_NAME, DEFAULT_WORKSPACE_SLUG),
    )
    if is_postgres_connection(conn):
        conn.execute(
            "SELECT setval(pg_get_serial_sequence('workspaces', 'id'), GREATEST((SELECT MAX(id) FROM workspaces), 1))"
        )


def ensure_workspace_columns(conn: sqlite3.Connection) -> None:
    for table in WORKSPACE_SCOPED_TABLES:
        ensure_column(conn, table, "workspace_id", "INTEGER NOT NULL DEFAULT 1")


def backfill_workspace_ids(conn: sqlite3.Connection) -> None:
    updates = [
        "UPDATE queues SET workspace_id = ? WHERE workspace_id IS NULL",
        "UPDATE keywords SET workspace_id = ? WHERE workspace_id IS NULL",
        "UPDATE urls SET workspace_id = ? WHERE workspace_id IS NULL",
        "UPDATE whitelist_urls SET workspace_id = ? WHERE workspace_id IS NULL",
        "UPDATE iocs SET workspace_id = ? WHERE workspace_id IS NULL",
        """
        UPDATE queue_routes
        SET workspace_id = COALESCE((
            SELECT workspace_id FROM queues WHERE queues.id = queue_routes.keyword_queue_id
        ), ?)
        WHERE workspace_id IS NULL
        """,
        """
        UPDATE search_queries
        SET workspace_id = COALESCE(
            (SELECT workspace_id FROM queues WHERE queues.id = search_queries.queue_id),
            (SELECT workspace_id FROM keywords WHERE keywords.id = search_queries.keyword_id),
            ?
        )
        WHERE workspace_id IS NULL
        """,
        """
        UPDATE search_queue_items
        SET workspace_id = COALESCE(
            (SELECT workspace_id FROM queues WHERE queues.id = search_queue_items.queue_id),
            (SELECT workspace_id FROM keywords WHERE keywords.id = search_queue_items.keyword_id),
            ?
        )
        WHERE workspace_id IS NULL
        """,
        """
        UPDATE url_queue_items
        SET workspace_id = COALESCE(
            (SELECT workspace_id FROM queues WHERE queues.id = url_queue_items.queue_id),
            (SELECT workspace_id FROM urls WHERE urls.id = url_queue_items.url_id),
            ?
        )
        WHERE workspace_id IS NULL
        """,
        """
        UPDATE url_sources
        SET workspace_id = COALESCE(
            (SELECT workspace_id FROM queues WHERE queues.id = url_sources.queue_id),
            (SELECT workspace_id FROM urls WHERE urls.id = url_sources.url_id),
            ?
        )
        WHERE workspace_id IS NULL
        """,
        """
        UPDATE jobs
        SET workspace_id = COALESCE(
            (SELECT workspace_id FROM queues WHERE queues.id = jobs.queue_id),
            (SELECT workspace_id FROM urls WHERE urls.id = jobs.target_url_id),
            ?
        )
        WHERE workspace_id IS NULL
        """,
        """
        UPDATE worker_slots
        SET workspace_id = COALESCE(
            (SELECT workspace_id FROM queues WHERE queues.id = worker_slots.queue_id),
            (SELECT workspace_id FROM urls WHERE urls.id = worker_slots.target_url_id),
            ?
        )
        WHERE workspace_id IS NULL
        """,
        """
        UPDATE ioc_sources
        SET workspace_id = COALESCE(
            (SELECT workspace_id FROM urls WHERE urls.id = ioc_sources.source_url_id),
            (SELECT workspace_id FROM iocs WHERE iocs.id = ioc_sources.ioc_id),
            ?
        )
        WHERE workspace_id IS NULL
        """,
    ]
    for sql in updates:
        table_match = re.search(r"UPDATE\s+([a-z_]+)", sql, flags=re.IGNORECASE)
        if table_match and not table_exists(conn, table_match.group(1)):
            continue
        if table_match and "workspace_id" not in table_columns(conn, table_match.group(1)):
            continue
        conn.execute(sql, (DEFAULT_WORKSPACE_ID,))


def sync_ioc_workspaces_from_sources(conn: sqlite3.Connection) -> int:
    required_tables = ("iocs", "ioc_sources", "urls")
    if not all(table_exists(conn, table) for table in required_tables):
        return 0
    if not all("workspace_id" in table_columns(conn, table) for table in required_tables):
        return 0

    rows = conn.execute(
        """
        SELECT
          s.id AS source_id,
          s.ioc_id,
          s.workspace_id AS source_workspace_id,
          s.source_url_id,
          s.source_type,
          s.extraction_rule_id,
          s.evidence_text,
          i.workspace_id AS ioc_workspace_id,
          i.type,
          i.value_raw,
          i.value_norm,
          COALESCE(i.deleted, 0) AS deleted,
          i.deleted_at,
          i.collected_at,
          i.created_at,
          u.workspace_id AS url_workspace_id
        FROM ioc_sources s
        JOIN iocs i ON i.id = s.ioc_id
        JOIN urls u ON u.id = s.source_url_id
        WHERE u.workspace_id IS NOT NULL
          AND (
            s.workspace_id <> u.workspace_id
            OR i.workspace_id <> u.workspace_id
          )
        ORDER BY s.id
        """
    ).fetchall()

    changed = 0
    moved_from_ioc_ids: set[int] = set()
    for row in rows:
        target_workspace_id = int(row["url_workspace_id"] or DEFAULT_WORKSPACE_ID)
        source_id = int(row["source_id"])
        current_ioc_id = int(row["ioc_id"])
        current_ioc_workspace_id = int(row["ioc_workspace_id"] or DEFAULT_WORKSPACE_ID)
        if target_workspace_id == current_ioc_workspace_id:
            target_ioc_id = current_ioc_id
        else:
            moved_from_ioc_ids.add(current_ioc_id)
            target_ioc_id = ensure_ioc_in_workspace(conn, row, target_workspace_id)

        if relink_ioc_source(conn, row, target_workspace_id, target_ioc_id):
            changed += 1

    for ioc_id in moved_from_ioc_ids:
        conn.execute(
            """
            UPDATE iocs
            SET deleted = 1,
                deleted_at = COALESCE(deleted_at, CURRENT_TIMESTAMP)
            WHERE id = ?
              AND COALESCE(deleted, 0) = 0
              AND NOT EXISTS (
                SELECT 1
                FROM ioc_sources s
                WHERE s.ioc_id = iocs.id
                  AND s.workspace_id = iocs.workspace_id
              )
            """,
            (ioc_id,),
        )

    return changed


def ensure_ioc_in_workspace(conn: sqlite3.Connection, source_row, workspace_id: int) -> int:
    row = conn.execute(
        """
        SELECT id
        FROM iocs
        WHERE workspace_id = ?
          AND type = ?
          AND value_norm = ?
        """,
        (workspace_id, source_row["type"], source_row["value_norm"]),
    ).fetchone()
    if row:
        return int(row["id"])

    conn.execute(
        """
        INSERT OR IGNORE INTO iocs(
          workspace_id, type, value_raw, value_norm, deleted, deleted_at, collected_at, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            workspace_id,
            source_row["type"],
            source_row["value_raw"],
            source_row["value_norm"],
            int(source_row["deleted"] or 0),
            source_row["deleted_at"],
            source_row["collected_at"],
            source_row["created_at"],
        ),
    )
    row = conn.execute(
        """
        SELECT id
        FROM iocs
        WHERE workspace_id = ?
          AND type = ?
          AND value_norm = ?
        """,
        (workspace_id, source_row["type"], source_row["value_norm"]),
    ).fetchone()
    return int(row["id"]) if row else int(source_row["ioc_id"])


def relink_ioc_source(
    conn: sqlite3.Connection,
    source_row,
    workspace_id: int,
    ioc_id: int,
) -> bool:
    source_id = int(source_row["source_id"])
    existing = find_ioc_source(
        conn,
        workspace_id,
        ioc_id,
        int(source_row["source_url_id"]),
        source_row["extraction_rule_id"],
    )
    if existing and int(existing["id"]) != source_id:
        merged_evidence = merge_evidence_text(existing["evidence_text"], source_row["evidence_text"])
        conn.execute(
            """
            UPDATE ioc_sources
            SET evidence_text = ?
            WHERE id = ?
            """,
            (merged_evidence, int(existing["id"])),
        )
        conn.execute("DELETE FROM ioc_sources WHERE id = ?", (source_id,))
        return True

    if (
        int(source_row["source_workspace_id"] or DEFAULT_WORKSPACE_ID) == workspace_id
        and int(source_row["ioc_id"]) == ioc_id
    ):
        return False

    conn.execute(
        """
        UPDATE ioc_sources
        SET workspace_id = ?,
            ioc_id = ?
        WHERE id = ?
        """,
        (workspace_id, ioc_id, source_id),
    )
    return True


def find_ioc_source(
    conn: sqlite3.Connection,
    workspace_id: int,
    ioc_id: int,
    source_url_id: int,
    extraction_rule_id: int | None,
):
    if extraction_rule_id is None:
        return conn.execute(
            """
            SELECT id, evidence_text
            FROM ioc_sources
            WHERE workspace_id = ?
              AND ioc_id = ?
              AND source_url_id = ?
              AND extraction_rule_id IS NULL
            LIMIT 1
            """,
            (workspace_id, ioc_id, source_url_id),
        ).fetchone()
    return conn.execute(
        """
        SELECT id, evidence_text
        FROM ioc_sources
        WHERE workspace_id = ?
          AND ioc_id = ?
          AND source_url_id = ?
          AND extraction_rule_id = ?
        LIMIT 1
        """,
        (workspace_id, ioc_id, source_url_id, extraction_rule_id),
    ).fetchone()


def merge_evidence_text(existing: str | None, incoming: str | None) -> str | None:
    existing_text = (existing or "").strip()
    incoming_text = (incoming or "").strip()
    if not existing_text:
        return incoming_text or None
    if not incoming_text or incoming_text in existing_text:
        return existing
    return f"{existing_text}\n\n---\n\n{incoming_text}"


def migrate_workspace_unique_constraints(conn: sqlite3.Connection) -> None:
    if is_postgres_connection(conn):
        migrate_postgres_workspace_unique_constraints(conn)
        return
    rebuild_sqlite_workspace_unique_tables(conn)


def migrate_postgres_workspace_unique_constraints(conn: sqlite3.Connection) -> None:
    for statement in (
        "ALTER TABLE keywords DROP CONSTRAINT IF EXISTS keywords_text_key",
        "ALTER TABLE queues DROP CONSTRAINT IF EXISTS queues_name_queue_type_key",
        "ALTER TABLE jobs DROP CONSTRAINT IF EXISTS jobs_dedupe_key_key",
        "ALTER TABLE urls DROP CONSTRAINT IF EXISTS urls_url_norm_key",
        "ALTER TABLE whitelist_urls DROP CONSTRAINT IF EXISTS whitelist_urls_url_norm_key",
        "ALTER TABLE url_sources DROP CONSTRAINT IF EXISTS url_sources_dedupe_key_key",
        "ALTER TABLE iocs DROP CONSTRAINT IF EXISTS iocs_type_value_norm_key",
    ):
        try:
            conn.execute(statement)
        except Exception:
            pass
    conn.executescript(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_keywords_workspace_text ON keywords(workspace_id, text);
        CREATE UNIQUE INDEX IF NOT EXISTS uq_queues_workspace_name_type ON queues(workspace_id, name, queue_type);
        CREATE UNIQUE INDEX IF NOT EXISTS uq_jobs_workspace_dedupe ON jobs(workspace_id, dedupe_key);
        CREATE UNIQUE INDEX IF NOT EXISTS uq_urls_workspace_norm ON urls(workspace_id, url_norm);
        CREATE UNIQUE INDEX IF NOT EXISTS uq_whitelist_workspace_norm ON whitelist_urls(workspace_id, url_norm);
        CREATE UNIQUE INDEX IF NOT EXISTS uq_url_sources_workspace_dedupe ON url_sources(workspace_id, dedupe_key);
        CREATE UNIQUE INDEX IF NOT EXISTS uq_iocs_workspace_type_norm ON iocs(workspace_id, type, value_norm);
        """
    )


def sqlite_table_sql(conn: sqlite3.Connection, table: str) -> str:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    return row["sql"] or "" if row else ""


def rebuild_sqlite_workspace_unique_tables(conn: sqlite3.Connection) -> None:
    targets = {
        "keywords": (
            "UNIQUE (workspace_id, text)",
            """
            CREATE TABLE {table} (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              workspace_id INTEGER NOT NULL DEFAULT 1 REFERENCES workspaces(id),
              text TEXT NOT NULL,
              status TEXT NOT NULL DEFAULT 'pending',
              paused_by_user INTEGER NOT NULL DEFAULT 0,
              created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
              UNIQUE (workspace_id, text)
            )
            """,
            [
                ("id", "id"),
                ("workspace_id", str(DEFAULT_WORKSPACE_ID)),
                ("text", "''"),
                ("status", "'pending'"),
                ("paused_by_user", "0"),
                ("created_at", "CURRENT_TIMESTAMP"),
            ],
        ),
        "queues": (
            "UNIQUE (workspace_id, name, queue_type)",
            """
            CREATE TABLE {table} (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              workspace_id INTEGER NOT NULL DEFAULT 1 REFERENCES workspaces(id),
              name TEXT NOT NULL,
              queue_type TEXT NOT NULL CHECK(queue_type IN ('keyword_search', 'url_crawl')),
              status TEXT NOT NULL DEFAULT 'draft',
              max_concurrent_jobs INTEGER NOT NULL DEFAULT 1,
              active_max_concurrent_jobs INTEGER NOT NULL DEFAULT 1,
              created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
              updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
              started_at TEXT,
              stopped_at TEXT,
              UNIQUE (workspace_id, name, queue_type)
            )
            """,
            [
                ("id", "id"),
                ("workspace_id", str(DEFAULT_WORKSPACE_ID)),
                ("name", "''"),
                ("queue_type", "'keyword_search'"),
                ("status", "'draft'"),
                ("max_concurrent_jobs", "1"),
                ("active_max_concurrent_jobs", "1"),
                ("created_at", "CURRENT_TIMESTAMP"),
                ("updated_at", "CURRENT_TIMESTAMP"),
                ("started_at", "NULL"),
                ("stopped_at", "NULL"),
            ],
        ),
        "jobs": (
            "UNIQUE (workspace_id, dedupe_key)",
            """
            CREATE TABLE {table} (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              workspace_id INTEGER NOT NULL DEFAULT 1 REFERENCES workspaces(id),
              queue_id INTEGER REFERENCES queues(id),
              type TEXT NOT NULL,
              status TEXT NOT NULL DEFAULT 'pending',
              payload TEXT NOT NULL,
              dedupe_key TEXT,
              attempts INTEGER NOT NULL DEFAULT 0,
              error TEXT,
              target_url_id INTEGER,
              target_queue_item_id INTEGER,
              worker_slot_key TEXT,
              run_token TEXT,
              heartbeat_at TEXT,
              created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
              started_at TEXT,
              finished_at TEXT,
              UNIQUE (workspace_id, dedupe_key)
            )
            """,
            [
                ("id", "id"),
                ("workspace_id", str(DEFAULT_WORKSPACE_ID)),
                ("queue_id", "NULL"),
                ("type", "'unknown'"),
                ("status", "'pending'"),
                ("payload", "'{}'"),
                ("dedupe_key", "NULL"),
                ("attempts", "0"),
                ("error", "NULL"),
                ("target_url_id", "NULL"),
                ("target_queue_item_id", "NULL"),
                ("worker_slot_key", "NULL"),
                ("run_token", "NULL"),
                ("heartbeat_at", "NULL"),
                ("created_at", "CURRENT_TIMESTAMP"),
                ("started_at", "NULL"),
                ("finished_at", "NULL"),
            ],
        ),
        "urls": (
            "UNIQUE (workspace_id, url_norm)",
            """
            CREATE TABLE {table} (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              workspace_id INTEGER NOT NULL DEFAULT 1 REFERENCES workspaces(id),
              url_raw TEXT NOT NULL,
              url_norm TEXT NOT NULL,
              domain TEXT NOT NULL,
              title TEXT,
              first_source TEXT NOT NULL,
              review_status TEXT NOT NULL DEFAULT 'pending_review',
              crawl_status TEXT NOT NULL DEFAULT 'not_crawled',
              final_url TEXT,
              status_code INTEGER,
              content_type TEXT,
              content_length INTEGER,
              fetch_method TEXT,
              crawl_error TEXT,
              html TEXT,
              crawled_at TEXT,
              created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
              UNIQUE (workspace_id, url_norm)
            )
            """,
            [
                ("id", "id"),
                ("workspace_id", str(DEFAULT_WORKSPACE_ID)),
                ("url_raw", "''"),
                ("url_norm", "''"),
                ("domain", "''"),
                ("title", "NULL"),
                ("first_source", "'unknown'"),
                ("review_status", "'pending_review'"),
                ("crawl_status", "'not_crawled'"),
                ("final_url", "NULL"),
                ("status_code", "NULL"),
                ("content_type", "NULL"),
                ("content_length", "NULL"),
                ("fetch_method", "NULL"),
                ("crawl_error", "NULL"),
                ("html", "NULL"),
                ("crawled_at", "NULL"),
                ("created_at", "CURRENT_TIMESTAMP"),
            ],
        ),
        "whitelist_urls": (
            "UNIQUE (workspace_id, url_norm)",
            """
            CREATE TABLE {table} (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              workspace_id INTEGER NOT NULL DEFAULT 1 REFERENCES workspaces(id),
              url_raw TEXT NOT NULL,
              url_norm TEXT NOT NULL,
              match_type TEXT NOT NULL DEFAULT 'exact',
              match_value TEXT,
              note TEXT,
              enabled INTEGER NOT NULL DEFAULT 1,
              matched_url_count INTEGER NOT NULL DEFAULT 0,
              queue_item_count INTEGER NOT NULL DEFAULT 0,
              counts_updated_at TEXT,
              created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
              updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
              UNIQUE (workspace_id, url_norm)
            )
            """,
            [
                ("id", "id"),
                ("workspace_id", str(DEFAULT_WORKSPACE_ID)),
                ("url_raw", "''"),
                ("url_norm", "''"),
                ("match_type", "'exact'"),
                ("match_value", "NULL"),
                ("note", "NULL"),
                ("enabled", "1"),
                ("matched_url_count", "0"),
                ("queue_item_count", "0"),
                ("counts_updated_at", "NULL"),
                ("created_at", "CURRENT_TIMESTAMP"),
                ("updated_at", "CURRENT_TIMESTAMP"),
            ],
        ),
        "url_sources": (
            "UNIQUE (workspace_id, dedupe_key)",
            """
            CREATE TABLE {table} (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              workspace_id INTEGER NOT NULL DEFAULT 1 REFERENCES workspaces(id),
              url_id INTEGER NOT NULL REFERENCES urls(id),
              source_type TEXT NOT NULL,
              dedupe_key TEXT NOT NULL,
              queue_id INTEGER REFERENCES queues(id),
              keyword_id INTEGER REFERENCES keywords(id),
              search_query_id INTEGER REFERENCES search_queries(id),
              source_url_id INTEGER REFERENCES urls(id),
              title TEXT,
              snippet TEXT,
              rank INTEGER,
              page_no INTEGER,
              created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
              UNIQUE (workspace_id, dedupe_key)
            )
            """,
            [
                ("id", "id"),
                ("workspace_id", str(DEFAULT_WORKSPACE_ID)),
                ("url_id", "NULL"),
                ("source_type", "'unknown'"),
                ("dedupe_key", "''"),
                ("queue_id", "NULL"),
                ("keyword_id", "NULL"),
                ("search_query_id", "NULL"),
                ("source_url_id", "NULL"),
                ("title", "NULL"),
                ("snippet", "NULL"),
                ("rank", "NULL"),
                ("page_no", "NULL"),
                ("created_at", "CURRENT_TIMESTAMP"),
            ],
        ),
        "iocs": (
            "UNIQUE (workspace_id, type, value_norm)",
            """
            CREATE TABLE {table} (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              workspace_id INTEGER NOT NULL DEFAULT 1 REFERENCES workspaces(id),
              type TEXT NOT NULL,
              value_raw TEXT NOT NULL,
              value_norm TEXT NOT NULL,
              deleted INTEGER NOT NULL DEFAULT 0,
              deleted_at TEXT,
              collected_at TEXT,
              created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
              UNIQUE (workspace_id, type, value_norm)
            )
            """,
            [
                ("id", "id"),
                ("workspace_id", str(DEFAULT_WORKSPACE_ID)),
                ("type", "''"),
                ("value_raw", "''"),
                ("value_norm", "''"),
                ("deleted", "0"),
                ("deleted_at", "NULL"),
                ("collected_at", "NULL"),
                ("created_at", "CURRENT_TIMESTAMP"),
            ],
        ),
    }

    rebuilds = [
        (table, create_sql, columns)
        for table, (unique_phrase, create_sql, columns) in targets.items()
        if table_exists(conn, table) and unique_phrase not in sqlite_table_sql(conn, table)
    ]
    if not rebuilds:
        return

    conn.commit()
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("PRAGMA legacy_alter_table = ON")
    try:
        for table, create_sql, columns in rebuilds:
            rebuild_sqlite_table(conn, table, create_sql, columns)
    finally:
        conn.commit()
        conn.execute("PRAGMA legacy_alter_table = OFF")
        conn.execute("PRAGMA foreign_keys = ON")


def rebuild_sqlite_table(
    conn: sqlite3.Connection,
    table: str,
    create_sql: str,
    columns: list[tuple[str, str]],
) -> None:
    old_table = f"{table}__workspace_old"
    conn.execute(f"DROP TABLE IF EXISTS {old_table}")
    existing_columns = table_columns(conn, table)
    conn.execute(f"ALTER TABLE {table} RENAME TO {old_table}")
    conn.execute(create_sql.format(table=table))
    insert_columns = [column for column, _ in columns]
    select_exprs = [
        column if column in existing_columns else default_sql
        for column, default_sql in columns
    ]
    conn.execute(
        f"""
        INSERT OR IGNORE INTO {table}({", ".join(insert_columns)})
        SELECT {", ".join(select_exprs)}
        FROM {old_table}
        """
    )
    conn.execute(f"DROP TABLE {old_table}")


def upsert_ioc_record(
    conn: sqlite3.Connection,
    ioc_type: str,
    value_raw: str,
    value_norm: str,
    workspace_id: int | None = None,
) -> int | None:
    workspace_id = workspace_id or DEFAULT_WORKSPACE_ID
    row = conn.execute(
        """
        SELECT id, COALESCE(deleted, 0) AS deleted
        FROM iocs
        WHERE workspace_id = ?
          AND type = ?
          AND value_norm = ?
        """,
        (workspace_id, ioc_type, value_norm),
    ).fetchone()
    if row:
        if int(row["deleted"] or 0):
            return None
        return int(row["id"])

    conn.execute(
        """
        INSERT OR IGNORE INTO iocs(workspace_id, type, value_raw, value_norm, collected_at)
        VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
        """,
        (workspace_id, ioc_type, value_raw, value_norm),
    )
    row = conn.execute(
        """
        SELECT id, COALESCE(deleted, 0) AS deleted
        FROM iocs
        WHERE workspace_id = ?
          AND type = ?
          AND value_norm = ?
        """,
        (workspace_id, ioc_type, value_norm),
    ).fetchone()
    if not row or int(row["deleted"] or 0):
        return None
    return int(row["id"])


def upsert_ioc_source(
    conn: sqlite3.Connection,
    ioc_id: int,
    source_url_id: int,
    source_type: str,
    extraction_rule_id: int | None = None,
    evidence_text: str | None = None,
) -> int:
    ioc = conn.execute(
        "SELECT workspace_id, COALESCE(deleted, 0) AS deleted FROM iocs WHERE id = ?",
        (ioc_id,),
    ).fetchone()
    if not ioc or int(ioc["deleted"] or 0):
        return 0
    source_url = conn.execute(
        "SELECT workspace_id FROM urls WHERE id = ?",
        (source_url_id,),
    ).fetchone()
    if not source_url or int(source_url["workspace_id"]) != int(ioc["workspace_id"]):
        return 0
    workspace_id = int(ioc["workspace_id"])

    source_type = source_type or "crawl"
    if extraction_rule_id is None:
        row = conn.execute(
            """
            SELECT id
            FROM ioc_sources
            WHERE workspace_id = ?
              AND ioc_id = ?
              AND source_url_id = ?
              AND source_type = ?
              AND extraction_rule_id IS NULL
            LIMIT 1
            """,
            (workspace_id, ioc_id, source_url_id, source_type),
        ).fetchone()
    else:
        row = conn.execute(
            """
            SELECT id
            FROM ioc_sources
            WHERE workspace_id = ?
              AND ioc_id = ?
              AND source_url_id = ?
              AND source_type = ?
              AND extraction_rule_id = ?
            LIMIT 1
            """,
            (workspace_id, ioc_id, source_url_id, source_type, extraction_rule_id),
        ).fetchone()

    if row:
        source_id = int(row["id"])
        if evidence_text:
            conn.execute(
                """
                UPDATE ioc_sources
                SET evidence_text = CASE
                    WHEN evidence_text IS NULL OR evidence_text = '' THEN ?
                    ELSE evidence_text
                END
                WHERE id = ?
                """,
                (evidence_text, source_id),
            )
        return source_id

    conn.execute(
        """
        INSERT OR IGNORE INTO ioc_sources(
          workspace_id, ioc_id, source_url_id, source_type, extraction_rule_id, evidence_text
        )
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (workspace_id, ioc_id, source_url_id, source_type, extraction_rule_id, evidence_text),
    )

    if extraction_rule_id is None:
        row = conn.execute(
            """
            SELECT id
            FROM ioc_sources
            WHERE workspace_id = ?
              AND ioc_id = ?
              AND source_url_id = ?
              AND source_type = ?
              AND extraction_rule_id IS NULL
            ORDER BY id
            LIMIT 1
            """,
            (workspace_id, ioc_id, source_url_id, source_type),
        ).fetchone()
    else:
        row = conn.execute(
            """
            SELECT id
            FROM ioc_sources
            WHERE workspace_id = ?
              AND ioc_id = ?
              AND source_url_id = ?
              AND source_type = ?
              AND extraction_rule_id = ?
            ORDER BY id
            LIMIT 1
            """,
            (workspace_id, ioc_id, source_url_id, source_type, extraction_rule_id),
        ).fetchone()

    if row:
        return int(row["id"])

    extraction_filter = "extraction_rule_id IS NULL"
    fallback_params: tuple[object, ...] = (workspace_id, ioc_id, source_url_id)
    if extraction_rule_id is not None:
        extraction_filter = "extraction_rule_id = ?"
        fallback_params = (*fallback_params, extraction_rule_id)

    fallback = conn.execute(
        f"""
        SELECT id
        FROM ioc_sources
        WHERE workspace_id = ?
          AND ioc_id = ?
          AND source_url_id = ?
          AND {extraction_filter}
        ORDER BY id
        LIMIT 1
        """,
        fallback_params,
    ).fetchone()
    return int(fallback["id"]) if fallback else 0


def keyword_search_url_source_context(conn: sqlite3.Connection, url_id: int):
    if not table_exists(conn, "url_sources"):
        return None
    return conn.execute(
        """
        SELECT us.title,
               us.snippet,
               us.rank,
               us.page_no,
               k.text AS keyword_text,
               sq.query_text
        FROM url_sources us
        LEFT JOIN keywords k ON k.id = us.keyword_id
        LEFT JOIN search_queries sq ON sq.id = us.search_query_id
        WHERE us.url_id = ?
          AND us.source_type = 'google_search'
        ORDER BY COALESCE(us.page_no, 0),
                 COALESCE(us.rank, 0),
                 us.id
        LIMIT 1
        """,
        (url_id,),
    ).fetchone()


def build_keyword_search_ioc_evidence(row) -> str:
    parts = ["source=keyword_search"]
    if row["keyword_text"]:
        parts.append(f"keyword={row['keyword_text']}")
    if row["query_text"]:
        parts.append(f"query={row['query_text']}")
    if row["page_no"]:
        parts.append(f"page={row['page_no']}")
    if row["rank"]:
        parts.append(f"rank={row['rank']}")
    if row["title"]:
        parts.append(f"title={row['title']}")
    if row["snippet"]:
        parts.append(f"snippet={row['snippet']}")
    return " | ".join(parts)


def record_keyword_search_url_ioc(conn: sqlite3.Connection, url_id: int) -> bool:
    if not all(table_exists(conn, table) for table in ("urls", "url_sources", "iocs", "ioc_sources")):
        return False

    row = conn.execute(
        """
        SELECT u.id, u.workspace_id, u.url_raw, u.url_norm
        FROM urls u
        WHERE u.id = ?
          AND u.review_status = 'approved'
          AND EXISTS (
            SELECT 1
            FROM url_sources us
            WHERE us.url_id = u.id
              AND us.source_type = 'google_search'
          )
        """,
        (url_id,),
    ).fetchone()
    if not row:
        return False

    url_norm = normalize_url_without_query(row["url_norm"] or row["url_raw"])
    workspace_id = int(row["workspace_id"] or DEFAULT_WORKSPACE_ID)
    if not url_norm or is_media_asset_url(url_norm) or is_url_whitelisted(conn, url_norm, workspace_id):
        return False

    ioc_id = upsert_ioc_record(conn, "url", row["url_raw"] or url_norm, url_norm, workspace_id)
    source_context = keyword_search_url_source_context(conn, int(row["id"]))
    if not ioc_id or not source_context:
        return False

    upsert_ioc_source(
        conn,
        ioc_id,
        int(row["id"]),
        "keyword_search",
        extraction_rule_id=None,
        evidence_text=build_keyword_search_ioc_evidence(source_context),
    )
    return True


def backfill_keyword_search_url_iocs(conn: sqlite3.Connection) -> None:
    if not all(table_exists(conn, table) for table in ("urls", "url_sources", "iocs", "ioc_sources")):
        return
    rows = conn.execute(
        """
        SELECT DISTINCT u.id
        FROM urls u
        JOIN url_sources us ON us.url_id = u.id
        WHERE u.review_status = 'approved'
          AND us.source_type = 'google_search'
        """
    ).fetchall()
    for row in rows:
        record_keyword_search_url_ioc(conn, int(row["id"]))


def migrate_job_targets(conn: sqlite3.Connection) -> None:
    if not table_exists(conn, "jobs"):
        return
    columns = table_columns(conn, "jobs")
    if {"target_url_id", "target_queue_item_id", "payload", "type"} - columns:
        return
    rows = conn.execute(
        """
        SELECT id, payload
        FROM jobs
        WHERE type = 'crawl_url'
          AND (target_url_id IS NULL OR target_queue_item_id IS NULL)
        """
    ).fetchall()
    for row in rows:
        try:
            payload = json.loads(row["payload"] or "{}")
        except Exception:
            continue
        target_url_id = payload.get("url_id")
        target_queue_item_id = payload.get("url_queue_item_id")
        conn.execute(
            """
            UPDATE jobs
            SET target_url_id = COALESCE(target_url_id, ?),
                target_queue_item_id = COALESCE(target_queue_item_id, ?)
            WHERE id = ?
            """,
            (target_url_id, target_queue_item_id, row["id"]),
        )


def migrate_url_bodies(conn: sqlite3.Connection) -> None:
    if not table_exists(conn, "urls"):
        return
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS url_bodies (
          url_id INTEGER PRIMARY KEY REFERENCES urls(id) ON DELETE CASCADE,
          html TEXT NOT NULL DEFAULT '',
          created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
          updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        """
    )
    columns = table_columns(conn, "urls")
    if "html" not in columns:
        return
    conn.execute(
        """
        INSERT INTO url_bodies(url_id, html, updated_at)
        SELECT id, html, CURRENT_TIMESTAMP
        FROM urls
        WHERE html IS NOT NULL
          AND html != ''
        ON CONFLICT(url_id) DO UPDATE SET
            html = excluded.html,
            updated_at = CURRENT_TIMESTAMP
        """
    )
    conn.execute(
        """
        UPDATE urls
        SET html = NULL
        WHERE html IS NOT NULL
          AND html != ''
        """
    )


def migrate_domain_tables_into_urls(conn: sqlite3.Connection) -> None:
    if not table_exists(conn, "domains"):
        return

    for row in conn.execute("SELECT * FROM domains").fetchall():
        domain = row["domain"]
        url_norm = normalize_url(f"https://{domain}/")
        if not url_norm:
            continue
        normalized_domain = get_domain(url_norm)
        if not normalized_domain:
            continue
        review_status = row["review_status"] or "pending_review"
        conn.execute(
            """
            INSERT OR IGNORE INTO urls(workspace_id, url_raw, url_norm, domain, first_source, review_status)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                DEFAULT_WORKSPACE_ID,
                url_norm,
                url_norm,
                normalized_domain,
                row["first_source"] or "domain_migration",
                review_status,
            ),
        )
        conn.execute(
            """
            UPDATE urls
            SET review_status = CASE
                WHEN review_status = 'pending_review' THEN ?
                ELSE review_status
            END
            WHERE url_norm = ?
              AND workspace_id = ?
            """,
            (review_status, url_norm, DEFAULT_WORKSPACE_ID),
        )

    if table_exists(conn, "domain_sources"):
        rows = conn.execute(
            """
            SELECT ds.*, d.domain
            FROM domain_sources ds
            JOIN domains d ON d.id = ds.domain_id
            ORDER BY ds.id
            """
        ).fetchall()
        for row in rows:
            domain_url = normalize_url(f"https://{row['domain']}/")
            if not domain_url:
                continue
            url = conn.execute(
                "SELECT id FROM urls WHERE workspace_id = ? AND url_norm = ?",
                (DEFAULT_WORKSPACE_ID, domain_url),
            ).fetchone()
            if not url:
                continue
            conn.execute(
                """
                INSERT OR IGNORE INTO url_sources(
                  workspace_id, url_id, source_type, dedupe_key, queue_id, keyword_id, search_query_id,
                  source_url_id, title, snippet, rank, page_no
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?)
                """,
                (
                    DEFAULT_WORKSPACE_ID,
                    url["id"],
                    row["source_type"],
                    f"migrated-domain:{row['dedupe_key']}",
                    row["queue_id"],
                    row["keyword_id"],
                    row["search_query_id"],
                    row["source_url_id"],
                    row["rank"],
                    row["page_no"],
                ),
            )
        conn.execute("DROP TABLE domain_sources")

    conn.execute("DROP TABLE domains")


def remove_crawled_urls_from_queue_items(conn: sqlite3.Connection) -> None:
    if not table_exists(conn, "url_queue_items") or not table_exists(conn, "urls"):
        return
    item_rows = conn.execute(
        """
        SELECT qi.id, qi.queue_id, qi.url_id
        FROM url_queue_items qi
        JOIN urls u ON u.id = qi.url_id
        WHERE u.crawl_status IN ('crawled', 'metadata_only')
          AND qi.status != 'running'
        """
    ).fetchall()
    item_ids = [int(row["id"]) for row in item_rows]
    if not item_ids:
        return
    for row in item_rows:
        conn.execute(
            """
            DELETE FROM jobs
            WHERE type = 'crawl_url'
              AND dedupe_key = ?
              AND status != 'running'
            """,
            (f"queue:{row['queue_id']}:crawl:{row['url_id']}",),
        )
    placeholders = ",".join("?" for _ in item_ids)
    conn.execute(
        f"""
        UPDATE url_queue_items
        SET source_url_queue_item_id = NULL
        WHERE source_url_queue_item_id IN ({placeholders})
        """,
        item_ids,
    )
    conn.execute(f"DELETE FROM url_queue_items WHERE id IN ({placeholders})", item_ids)


def remove_ignored_asset_urls_from_queue_items(conn: sqlite3.Connection) -> None:
    if not table_exists(conn, "url_queue_items") or not table_exists(conn, "urls"):
        return

    asset_rows = [
        row
        for row in conn.execute("SELECT id, url_norm FROM urls").fetchall()
        if is_media_asset_url(row["url_norm"])
    ]
    if not asset_rows:
        return

    asset_ids = [int(row["id"]) for row in asset_rows]
    placeholders = ",".join("?" for _ in asset_ids)
    item_rows = conn.execute(
        f"""
        SELECT id, queue_id, url_id
        FROM url_queue_items
        WHERE url_id IN ({placeholders})
          AND status != 'running'
        """,
        asset_ids,
    ).fetchall()

    for row in item_rows:
        conn.execute(
            """
            DELETE FROM jobs
            WHERE type = 'crawl_url'
              AND dedupe_key = ?
              AND status != 'running'
            """,
            (f"queue:{row['queue_id']}:crawl:{row['url_id']}",),
        )

    item_ids = [int(row["id"]) for row in item_rows]
    if item_ids:
        item_placeholders = ",".join("?" for _ in item_ids)
        conn.execute(
            f"""
            UPDATE url_queue_items
            SET source_url_queue_item_id = NULL
            WHERE source_url_queue_item_id IN ({item_placeholders})
            """,
            item_ids,
        )
        conn.execute(f"DELETE FROM url_queue_items WHERE id IN ({item_placeholders})", item_ids)

    conn.execute(
        f"""
        UPDATE urls
        SET review_status = 'ignored_asset'
        WHERE id IN ({placeholders})
          AND review_status = 'pending_review'
        """,
        asset_ids,
    )


def cleanup_malformed_review_queue_urls(conn: sqlite3.Connection) -> dict[str, int]:
    stats = {
        "updated": 0,
        "merged": 0,
        "ignored_asset": 0,
        "ignored_invalid": 0,
        "removed_queue_items": 0,
        "removed_jobs": 0,
    }
    if not table_exists(conn, "urls"):
        return stats

    if table_exists(conn, "url_queue_items"):
        rows = conn.execute(
            """
            SELECT DISTINCT u.id,
                   u.workspace_id,
                   u.url_raw,
                   u.url_norm,
                   u.domain,
                   u.review_status,
                   u.crawl_status
            FROM urls u
            WHERE u.crawl_status NOT IN ('crawling', 'crawled', 'metadata_only')
              AND (
                u.review_status = 'pending_review'
                OR EXISTS (
                  SELECT 1
                  FROM url_queue_items qi
                  WHERE qi.url_id = u.id
                )
              )
            ORDER BY u.id
            """
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT u.id,
                   u.workspace_id,
                   u.url_raw,
                   u.url_norm,
                   u.domain,
                   u.review_status,
                   u.crawl_status
            FROM urls u
            WHERE u.crawl_status NOT IN ('crawling', 'crawled', 'metadata_only')
              AND u.review_status = 'pending_review'
            ORDER BY u.id
            """
        ).fetchall()

    for row in rows:
        url_id = int(row["id"])
        if url_has_running_work(conn, url_id):
            continue

        current_norm = row["url_norm"] or row["url_raw"]
        cleaned_norm = normalize_url_without_query(current_norm)
        if not cleaned_norm:
            cleanup_stats = ignore_pending_url_record(conn, url_id, "ignored_malformed")
            stats["ignored_invalid"] += 1 if cleanup_stats["changed_status"] else 0
            stats["removed_queue_items"] += cleanup_stats["removed_queue_items"]
            stats["removed_jobs"] += cleanup_stats["removed_jobs"]
            continue

        if is_media_asset_url(cleaned_norm):
            cleanup_stats = ignore_pending_url_record(conn, url_id, "ignored_asset")
            stats["ignored_asset"] += 1 if cleanup_stats["changed_status"] else 0
            stats["removed_queue_items"] += cleanup_stats["removed_queue_items"]
            stats["removed_jobs"] += cleanup_stats["removed_jobs"]
            continue

        if table_exists(conn, "whitelist_urls") and is_url_whitelisted(
            conn,
            cleaned_norm,
            int(row["workspace_id"] or DEFAULT_WORKSPACE_ID),
        ):
            cleanup_stats = ignore_pending_url_record(conn, url_id, "ignored_whitelist")
            stats["removed_queue_items"] += cleanup_stats["removed_queue_items"]
            stats["removed_jobs"] += cleanup_stats["removed_jobs"]
            continue

        cleaned_domain = get_domain(cleaned_norm)
        if not cleaned_domain:
            cleanup_stats = ignore_pending_url_record(conn, url_id, "ignored_malformed")
            stats["ignored_invalid"] += 1 if cleanup_stats["changed_status"] else 0
            stats["removed_queue_items"] += cleanup_stats["removed_queue_items"]
            stats["removed_jobs"] += cleanup_stats["removed_jobs"]
            continue

        if cleaned_norm == row["url_norm"] and cleaned_domain == row["domain"]:
            continue

        existing = conn.execute(
            """
            SELECT id
            FROM urls
            WHERE url_norm = ?
              AND workspace_id = ?
              AND id != ?
            """,
            (cleaned_norm, int(row["workspace_id"] or DEFAULT_WORKSPACE_ID), url_id),
        ).fetchone()
        if existing:
            merge_stats = merge_url_record(conn, url_id, int(existing["id"]))
            stats["merged"] += 1
            stats["removed_queue_items"] += merge_stats["removed_queue_items"]
            stats["removed_jobs"] += merge_stats["removed_jobs"]
            continue

        conn.execute(
            """
            UPDATE urls
            SET url_norm = ?,
                domain = ?
            WHERE id = ?
            """,
            (cleaned_norm, cleaned_domain, url_id),
        )
        stats["updated"] += 1

    return stats


def url_has_running_work(conn: sqlite3.Connection, url_id: int) -> bool:
    if table_exists(conn, "url_queue_items") and conn.execute(
        """
        SELECT 1
        FROM url_queue_items
        WHERE url_id = ?
          AND status = 'running'
        LIMIT 1
        """,
        (url_id,),
    ).fetchone():
        return True

    if table_exists(conn, "jobs"):
        columns = table_columns(conn, "jobs")
        if "target_url_id" in columns and conn.execute(
            """
            SELECT 1
            FROM jobs
            WHERE target_url_id = ?
              AND status = 'running'
            LIMIT 1
            """,
            (url_id,),
        ).fetchone():
            return True
    return False


def ignore_pending_url_record(conn: sqlite3.Connection, url_id: int, review_status: str) -> dict[str, int]:
    stats = remove_url_queue_refs(conn, url_id)
    changed = conn.execute(
        """
        UPDATE urls
        SET review_status = ?
        WHERE id = ?
          AND review_status = 'pending_review'
        """,
        (review_status, url_id),
    ).rowcount
    stats["changed_status"] = max(changed, 0)
    return stats


def remove_url_queue_refs(conn: sqlite3.Connection, url_id: int) -> dict[str, int]:
    stats = {"removed_queue_items": 0, "removed_jobs": 0}
    if not table_exists(conn, "url_queue_items"):
        return stats

    rows = conn.execute(
        """
        SELECT id, queue_id
        FROM url_queue_items
        WHERE url_id = ?
          AND status != 'running'
        """,
        (url_id,),
    ).fetchall()
    for row in rows:
        stats["removed_jobs"] += delete_crawl_jobs_for_queue_url(
            conn,
            int(row["queue_id"]),
            url_id,
            int(row["id"]),
        )
        conn.execute(
            """
            UPDATE url_queue_items
            SET source_url_queue_item_id = NULL
            WHERE source_url_queue_item_id = ?
            """,
            (row["id"],),
        )
        deleted = conn.execute("DELETE FROM url_queue_items WHERE id = ?", (row["id"],)).rowcount
        stats["removed_queue_items"] += max(deleted, 0)
    return stats


def merge_url_record(conn: sqlite3.Connection, old_url_id: int, new_url_id: int) -> dict[str, int]:
    stats = {"removed_queue_items": 0, "removed_jobs": 0}
    if old_url_id == new_url_id:
        return stats

    stats.update(merge_url_queue_items(conn, old_url_id, new_url_id))
    rewrite_jobs_for_url(conn, old_url_id, new_url_id)
    merge_url_sources(conn, old_url_id, new_url_id)
    merge_ioc_source_urls(conn, old_url_id, new_url_id)
    merge_url_body(conn, old_url_id, new_url_id)
    if table_exists(conn, "worker_slots") and "target_url_id" in table_columns(conn, "worker_slots"):
        conn.execute(
            "UPDATE worker_slots SET target_url_id = ? WHERE target_url_id = ?",
            (new_url_id, old_url_id),
        )
    conn.execute("DELETE FROM urls WHERE id = ?", (old_url_id,))
    return stats


def merge_url_queue_items(conn: sqlite3.Connection, old_url_id: int, new_url_id: int) -> dict[str, int]:
    stats = {"removed_queue_items": 0, "removed_jobs": 0}
    if not table_exists(conn, "url_queue_items"):
        return stats

    rows = conn.execute(
        """
        SELECT *
        FROM url_queue_items
        WHERE url_id = ?
          AND status != 'running'
        ORDER BY id
        """,
        (old_url_id,),
    ).fetchall()
    for row in rows:
        existing = conn.execute(
            """
            SELECT id
            FROM url_queue_items
            WHERE queue_id = ?
              AND url_id = ?
            """,
            (row["queue_id"], new_url_id),
        ).fetchone()
        if existing:
            existing_item_id = int(existing["id"])
            conn.execute(
                """
                UPDATE url_queue_items
                SET source_queue_id = COALESCE(source_queue_id, ?),
                    source_search_query_id = COALESCE(source_search_query_id, ?),
                    source_search_queue_item_id = COALESCE(source_search_queue_item_id, ?),
                    source_url_queue_item_id = COALESCE(source_url_queue_item_id, ?)
                WHERE id = ?
                """,
                (
                    row["source_queue_id"],
                    row["source_search_query_id"],
                    row["source_search_queue_item_id"],
                    row["source_url_queue_item_id"],
                    existing_item_id,
                ),
            )
            conn.execute(
                """
                UPDATE url_queue_items
                SET source_url_queue_item_id = ?
                WHERE source_url_queue_item_id = ?
                """,
                (existing_item_id, row["id"]),
            )
            stats["removed_jobs"] += delete_crawl_jobs_for_queue_url(
                conn,
                int(row["queue_id"]),
                old_url_id,
                int(row["id"]),
            )
            deleted = conn.execute("DELETE FROM url_queue_items WHERE id = ?", (row["id"],)).rowcount
            stats["removed_queue_items"] += max(deleted, 0)
            continue

        conn.execute(
            """
            UPDATE url_queue_items
            SET url_id = ?
            WHERE id = ?
            """,
            (new_url_id, row["id"]),
        )
        rewrite_queue_crawl_jobs(
            conn,
            int(row["queue_id"]),
            old_url_id,
            new_url_id,
            int(row["id"]),
        )
    return stats


def delete_crawl_jobs_for_queue_url(
    conn: sqlite3.Connection,
    queue_id: int,
    url_id: int,
    queue_item_id: int | None = None,
) -> int:
    if not table_exists(conn, "jobs"):
        return 0
    columns = table_columns(conn, "jobs")
    filters = ["dedupe_key = ?"]
    params: list[object] = [f"queue:{queue_id}:crawl:{url_id}"]
    if queue_item_id is not None and "target_queue_item_id" in columns:
        filters.append("target_queue_item_id = ?")
        params.append(queue_item_id)
    if "target_url_id" in columns:
        filters.append("(queue_id = ? AND target_url_id = ? AND type = 'crawl_url')")
        params.extend([queue_id, url_id])
    row_count = conn.execute(
        f"""
        DELETE FROM jobs
        WHERE type = 'crawl_url'
          AND status != 'running'
          AND ({" OR ".join(filters)})
        """,
        tuple(params),
    ).rowcount
    return max(row_count, 0)


def rewrite_queue_crawl_jobs(
    conn: sqlite3.Connection,
    queue_id: int,
    old_url_id: int,
    new_url_id: int,
    queue_item_id: int,
) -> None:
    old_dedupe = f"queue:{queue_id}:crawl:{old_url_id}"
    new_dedupe = f"queue:{queue_id}:crawl:{new_url_id}"
    rewrite_crawl_jobs(
        conn,
        old_url_id,
        new_url_id,
        old_dedupe,
        new_dedupe,
        queue_item_id=queue_item_id,
    )


def rewrite_jobs_for_url(conn: sqlite3.Connection, old_url_id: int, new_url_id: int) -> None:
    rewrite_crawl_jobs(
        conn,
        old_url_id,
        new_url_id,
        f"crawl:{old_url_id}",
        f"crawl:{new_url_id}",
        queue_item_id=None,
        include_target_url_filter=False,
    )


def rewrite_crawl_jobs(
    conn: sqlite3.Connection,
    old_url_id: int,
    new_url_id: int,
    old_dedupe: str,
    new_dedupe: str,
    queue_item_id: int | None,
    include_target_url_filter: bool = True,
) -> None:
    if not table_exists(conn, "jobs"):
        return

    columns = table_columns(conn, "jobs")
    filters = ["dedupe_key = ?"]
    params: list[object] = [old_dedupe]
    if include_target_url_filter and "target_url_id" in columns:
        filters.append("target_url_id = ?")
        params.append(old_url_id)
    if queue_item_id is not None and "target_queue_item_id" in columns:
        filters.append("target_queue_item_id = ?")
        params.append(queue_item_id)

    jobs = conn.execute(
        f"""
        SELECT id, payload
        FROM jobs
        WHERE type = 'crawl_url'
          AND status != 'running'
          AND ({" OR ".join(filters)})
        ORDER BY id
        """,
        tuple(params),
    ).fetchall()
    for job in jobs:
        duplicate = conn.execute(
            """
            SELECT id
            FROM jobs
            WHERE dedupe_key = ?
              AND id != ?
            LIMIT 1
            """,
            (new_dedupe, job["id"]),
        ).fetchone()
        if duplicate:
            conn.execute("DELETE FROM jobs WHERE id = ? AND status != 'running'", (job["id"],))
            continue

        payload = rewrite_crawl_job_payload(job["payload"], new_url_id, queue_item_id)
        assignments = ["payload = ?", "dedupe_key = ?"]
        values: list[object] = [payload, new_dedupe]
        if "target_url_id" in columns:
            assignments.append("target_url_id = ?")
            values.append(new_url_id)
        if queue_item_id is not None and "target_queue_item_id" in columns:
            assignments.append("target_queue_item_id = ?")
            values.append(queue_item_id)
        values.append(job["id"])
        conn.execute(
            f"""
            UPDATE jobs
            SET {", ".join(assignments)}
            WHERE id = ?
            """,
            tuple(values),
        )


def rewrite_crawl_job_payload(payload_text: str, url_id: int, queue_item_id: int | None) -> str:
    try:
        payload = json.loads(payload_text or "{}")
    except Exception:
        payload = {}
    payload["url_id"] = url_id
    if queue_item_id is not None:
        payload["url_queue_item_id"] = queue_item_id
    return json.dumps(payload, separators=(",", ":"))


def merge_url_sources(conn: sqlite3.Connection, old_url_id: int, new_url_id: int) -> None:
    if not table_exists(conn, "url_sources"):
        return
    conn.execute("UPDATE url_sources SET source_url_id = ? WHERE source_url_id = ?", (new_url_id, old_url_id))
    conn.execute("UPDATE url_sources SET url_id = ? WHERE url_id = ?", (new_url_id, old_url_id))


def merge_ioc_source_urls(conn: sqlite3.Connection, old_url_id: int, new_url_id: int) -> None:
    if not table_exists(conn, "ioc_sources"):
        return
    rows = conn.execute(
        """
        SELECT ioc_id, source_type, extraction_rule_id, evidence_text
        FROM ioc_sources
        WHERE source_url_id = ?
        """,
        (old_url_id,),
    ).fetchall()
    for row in rows:
        upsert_ioc_source(
            conn,
            int(row["ioc_id"]),
            new_url_id,
            row["source_type"] or "crawl",
            extraction_rule_id=row["extraction_rule_id"],
            evidence_text=row["evidence_text"],
        )
    conn.execute("DELETE FROM ioc_sources WHERE source_url_id = ?", (old_url_id,))


def merge_url_body(conn: sqlite3.Connection, old_url_id: int, new_url_id: int) -> None:
    if not table_exists(conn, "url_bodies"):
        return
    old_body = conn.execute(
        "SELECT html, updated_at FROM url_bodies WHERE url_id = ?",
        (old_url_id,),
    ).fetchone()
    if not old_body:
        return
    new_body = conn.execute("SELECT 1 FROM url_bodies WHERE url_id = ?", (new_url_id,)).fetchone()
    if not new_body:
        conn.execute(
            """
            UPDATE url_bodies
            SET url_id = ?
            WHERE url_id = ?
            """,
            (new_url_id, old_url_id),
        )
        return
    conn.execute("DELETE FROM url_bodies WHERE url_id = ?", (old_url_id,))


def recover_interrupted_jobs(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        UPDATE jobs
        SET status = 'pending',
            started_at = NULL,
            worker_slot_key = NULL,
            run_token = NULL,
            heartbeat_at = NULL,
            error = COALESCE(error, 'Recovered after interrupted process.')
        WHERE status = 'running'
        """
    )
    if table_exists(conn, "worker_slots"):
        conn.execute(
            """
            UPDATE worker_slots
            SET status = 'idle',
                job_id = NULL,
                queue_id = NULL,
                target_url_id = NULL,
                target_queue_item_id = NULL,
                run_token = NULL,
                heartbeat_at = NULL,
                finished_at = CURRENT_TIMESTAMP,
                error = COALESCE(error, 'Recovered after interrupted process.'),
                updated_at = CURRENT_TIMESTAMP
            WHERE status = 'running'
            """
        )
    conn.execute(
        """
        UPDATE search_queries
        SET status = 'pending',
            last_error = COALESCE(last_error, 'Recovered after interrupted process.'),
            started_at = NULL
        WHERE status = 'running'
        """
    )
    conn.execute(
        "UPDATE urls SET crawl_status = 'not_crawled' WHERE crawl_status = 'crawling'"
    )
    conn.execute(
        """
        UPDATE url_queue_items
        SET status = 'pending',
            error = COALESCE(error, 'Recovered after interrupted process.'),
            started_at = NULL
        WHERE status = 'running'
          AND EXISTS (
            SELECT 1 FROM urls u
            WHERE u.id = url_queue_items.url_id
              AND u.review_status = 'approved'
          )
        """
    )
    conn.execute(
        """
        UPDATE url_queue_items
        SET status = 'pending_review',
            error = COALESCE(error, 'Recovered after interrupted process.'),
            started_at = NULL
        WHERE status = 'running'
          AND EXISTS (
            SELECT 1 FROM urls u
            WHERE u.id = url_queue_items.url_id
              AND u.review_status != 'approved'
          )
        """
    )
    conn.execute(
        """
        UPDATE search_queue_items
        SET status = 'pending',
            error = COALESCE(error, 'Recovered after interrupted process.'),
            started_at = NULL
        WHERE status = 'running'
        """
    )
    conn.execute(
        """
        UPDATE queues
        SET status = 'paused',
            stopped_at = CURRENT_TIMESTAMP,
            updated_at = CURRENT_TIMESTAMP
        WHERE status = 'stopping'
        """
    )


def seed_defaults(conn: sqlite3.Connection) -> None:
    dork_count = conn.execute("SELECT COUNT(*) FROM search_dorks").fetchone()[0]
    if dork_count == 0:
        conn.executemany(
            """
            INSERT INTO search_dorks(name, template, description)
            VALUES (?, ?, ?)
            """,
            [
                ("Raw keyword", "{keyword}", "Search the keyword as typed."),
                ("Exact keyword", '"{keyword}"', "Force exact-match discovery."),
                ("In URL", "inurl:{keyword}", "Find pages with keyword in the URL."),
                ("Title exact", 'intitle:"{keyword}"', "Find pages with keyword in the page title."),
                ("Contact", '"{keyword}" "contact"', "Find contact pages around the keyword."),
                ("Hotline", '"{keyword}" "hotline"', "Find pages exposing hotline text."),
                ("Telegram", '"{keyword}" "telegram"', "Find social/contact artifacts."),
            ],
        )

    rule_count = conn.execute("SELECT COUNT(*) FROM extraction_rules").fetchone()[0]
    if rule_count == 0:
        conn.executemany(
            """
            INSERT INTO extraction_rules(
              name, ioc_type, pattern, flags, value_group, input_scope,
              exclude_pattern, normalizer, priority, description, builtin
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
            """,
            [
                (
                    "Email basic",
                    "email",
                    r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}",
                    "i",
                    0,
                    "all",
                    r"@(example|test)\.",
                    "email",
                    10,
                    "General email extraction from rendered text, links, HTML, and encoded variants.",
                ),
                (
                    "HTTP URL",
                    "url",
                    r"https?://[^\s\"'<>]+",
                    "i",
                    0,
                    "all",
                    None,
                    "url",
                    20,
                    "Absolute HTTP/HTTPS URL extraction.",
                ),
                (
                    "Domain basic",
                    "domain",
                    r"(?<![@\w.-])(?:[a-zA-Z0-9-]+\.)+[a-zA-Z]{2,}(?![\w.-])",
                    "i",
                    0,
                    "text",
                    r"^(example\.com|localhost|.*\.(?:php|html?|aspx?|jsp|css|js|png|jpe?g|gif|svg|webp|json|xml|txt|pdf))$",
                    "domain",
                    30,
                    "Domain extraction with simple boundary checks.",
                ),
                (
                    "Vietnam mobile phone",
                    "phone",
                    r"(?<![A-Za-z0-9+])(?:(?:\+?84|0)(?:3|5|7|8|9)\d{8}|(?:\+?84|0)[\s.\-]?(?:3|5|7|8|9)\d{1,2}[\s.\-]?\d{3}[\s.\-]?\d{3,4})(?![A-Za-z0-9])",
                    "",
                    0,
                    "text",
                    None,
                    "phone_vn",
                    40,
                    "Vietnam mobile phone extraction.",
                ),
                (
                    "MD5",
                    "hash_md5",
                    r"(?<![a-fA-F0-9])[a-fA-F0-9]{32}(?![a-fA-F0-9])",
                    "",
                    0,
                    "all",
                    None,
                    "hash",
                    50,
                    "MD5 hash extraction.",
                ),
                (
                    "SHA1",
                    "hash_sha1",
                    r"(?<![a-fA-F0-9])[a-fA-F0-9]{40}(?![a-fA-F0-9])",
                    "",
                    0,
                    "all",
                    None,
                    "hash",
                    60,
                    "SHA1 hash extraction.",
                ),
                (
                    "SHA256",
                    "hash_sha256",
                    r"(?<![a-fA-F0-9])[a-fA-F0-9]{64}(?![a-fA-F0-9])",
                    "",
                    0,
                    "all",
                    None,
                    "hash",
                    70,
                    "SHA256 hash extraction.",
                ),
                (
                    "SHA512",
                    "hash_sha512",
                    r"(?<![a-fA-F0-9])[a-fA-F0-9]{128}(?![a-fA-F0-9])",
                    "",
                    0,
                    "all",
                    None,
                    "hash",
                    80,
                    "SHA512 hash extraction.",
                ),
                (
                    "Address context",
                    "address",
                    r"(?:address|dia chi|địa chỉ|location|office)\s*[:\-]?\s*([^\n\r<]{8,160})",
                    "i",
                    1,
                    "text",
                    None,
                    "address",
                    90,
                    "Address extraction only when a context keyword is present.",
                ),
            ],
        )

    refresh_builtin_extraction_rules(conn)
    cleanup_invalid_iocs(conn)


def refresh_builtin_extraction_rules(conn: sqlite3.Connection) -> None:
    updates = [
        (
            "Email basic",
            r"(?<![A-Z0-9._%+-])([A-Z0-9](?:[A-Z0-9._%+-]{0,62}[A-Z0-9])?@[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?(?:\.[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?)+)(?![A-Z0-9._%+-])",
            "i",
            1,
            "all",
            r"@(example|test|invalid|localhost)\.",
            "email",
            "Strict email extraction from rendered text, links, HTML, and encoded variants with validated domain/TLD.",
        ),
        (
            "HTTP URL",
            r"https?://[^\s\"'<>]+",
            "i",
            0,
            "all",
            None,
            "url",
            "Absolute HTTP/HTTPS URL extraction with URL/domain validation.",
        ),
        (
            "Domain basic",
            r"(?<!://)(?<![@\w.-])((?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63})(?![\w.-]*@)(?![\w.-])",
            "i",
            1,
            "text",
            r"^(example\.com|localhost|.*\.(?:php|html?|aspx?|jsp|css|js|mjs|map|png|jpe?g|gif|svg|webp|json|xml|txt|pdf|woff2?|ttf|eot))$",
            "domain",
            "Strict domain extraction with TLD validation to avoid JavaScript property false positives.",
        ),
        (
            "Vietnam mobile phone",
            r"(?<![A-Za-z0-9+])(?:(?:\+?84|0)(?:3|5|7|8|9)\d{8}|(?:\+?84|0)[\s.\-]?(?:3|5|7|8|9)\d{1,2}[\s.\-]?\d{3}[\s.\-]?\d{3,4})(?![A-Za-z0-9])",
            "",
            0,
            "text",
            None,
            "phone_vn",
            "Vietnam mobile phone extraction with strict grouping and code/URL false-positive guards.",
        ),
        (
            "Address context",
            r"(?:địa\s*chỉ|dia\s*chi|address|office\s+address)\s*[:：-]\s*([^\n\r<>{}();=]{8,180}?)(?=\s+(?:email|e-mail|mail|sdt|sđt|phone|tel|telephone|hotline|website|web|social)\b\s*:?|[\n\r<]|$)",
            "i",
            1,
            "text",
            None,
            "address",
            "Address extraction only from explicit address labels.",
        ),
    ]
    for name, pattern, flags, value_group, input_scope, exclude_pattern, normalizer, description in updates:
        conn.execute(
            """
            UPDATE extraction_rules
            SET pattern = ?,
                flags = ?,
                value_group = ?,
                input_scope = ?,
                exclude_pattern = ?,
                normalizer = ?,
                description = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE builtin = 1 AND name = ?
            """,
            (
                pattern,
                flags,
                value_group,
                input_scope,
                exclude_pattern,
                normalizer,
                description,
                name,
            ),
        )


def cleanup_invalid_iocs(conn: sqlite3.Connection) -> None:
    rows = conn.execute(
        """
        SELECT id, workspace_id, type, value_norm
        FROM iocs
        WHERE type IN ('domain', 'email', 'phone', 'url', 'address')
          AND COALESCE(deleted, 0) = 0
        """
    ).fetchall()
    for row in rows:
        normalizer = "phone_vn" if row["type"] == "phone" else row["type"]
        normalized = normalize_by_rule(row["value_norm"], normalizer, row["type"])
        if row["type"] == "domain" and domain_ioc_is_email_localpart(conn, int(row["id"]), row["value_norm"]):
            normalized = None
        if row["type"] == "domain" and domain_ioc_is_full_url_host(conn, int(row["id"]), row["value_norm"]):
            normalized = None
        if row["type"] == "phone" and normalized:
            cleanup_invalid_phone_sources(conn, int(row["id"]), normalized)
            if not conn.execute("SELECT 1 FROM ioc_sources WHERE ioc_id = ? LIMIT 1", (row["id"],)).fetchone():
                normalized = None
        if normalized == row["value_norm"]:
            continue
        if normalized:
            merge_or_update_ioc(conn, int(row["id"]), int(row["workspace_id"]), row["type"], normalized)
        else:
            conn.execute("DELETE FROM ioc_sources WHERE ioc_id = ?", (row["id"],))
            conn.execute("DELETE FROM iocs WHERE id = ?", (row["id"],))


def cleanup_invalid_phone_sources(conn: sqlite3.Connection, ioc_id: int, value_norm: str) -> None:
    sources = conn.execute(
        """
        SELECT id, evidence_text
        FROM ioc_sources
        WHERE ioc_id = ?
        """,
        (ioc_id,),
    ).fetchall()
    for source in sources:
        if is_probable_phone_vn_evidence(value_norm, source["evidence_text"]):
            continue
        conn.execute("DELETE FROM ioc_sources WHERE id = ?", (source["id"],))


def merge_or_update_ioc(
    conn: sqlite3.Connection,
    ioc_id: int,
    workspace_id: int,
    ioc_type: str,
    normalized: str,
) -> None:
    existing = conn.execute(
        """
        SELECT id, COALESCE(deleted, 0) AS deleted
        FROM iocs
        WHERE workspace_id = ?
          AND type = ?
          AND value_norm = ?
        """,
        (workspace_id, ioc_type, normalized),
    ).fetchone()
    if not existing:
        conn.execute(
            """
            UPDATE iocs
            SET value_raw = ?,
                value_norm = ?
            WHERE id = ?
            """,
            (normalized, normalized, ioc_id),
        )
        return

    existing_id = int(existing["id"])
    if existing_id == ioc_id:
        return

    if int(existing["deleted"] or 0):
        conn.execute(
            """
            UPDATE iocs
            SET deleted = 1,
                deleted_at = COALESCE(deleted_at, CURRENT_TIMESTAMP)
            WHERE id = ?
            """,
            (ioc_id,),
        )
        return

    sources = conn.execute(
        """
        SELECT source_url_id, source_type, extraction_rule_id, evidence_text
        FROM ioc_sources
        WHERE ioc_id = ?
        """,
        (ioc_id,),
    ).fetchall()
    for source in sources:
        upsert_ioc_source(
            conn,
            existing_id,
            int(source["source_url_id"]),
            source["source_type"] or "crawl",
            extraction_rule_id=source["extraction_rule_id"],
            evidence_text=source["evidence_text"],
        )
    conn.execute("DELETE FROM ioc_sources WHERE ioc_id = ?", (ioc_id,))
    conn.execute("DELETE FROM iocs WHERE id = ?", (ioc_id,))


def domain_ioc_is_email_localpart(conn: sqlite3.Connection, ioc_id: int, domain: str) -> bool:
    sources = conn.execute(
        "SELECT evidence_text FROM ioc_sources WHERE ioc_id = ?",
        (ioc_id,),
    ).fetchall()
    if not sources:
        return False

    localpart_pattern = re.compile(rf"(?<![\w.-]){re.escape(domain)}@", re.IGNORECASE)
    return all(localpart_pattern.search(source["evidence_text"] or "") for source in sources)


def domain_ioc_is_full_url_host(conn: sqlite3.Connection, ioc_id: int, domain: str) -> bool:
    sources = conn.execute(
        "SELECT evidence_text FROM ioc_sources WHERE ioc_id = ?",
        (ioc_id,),
    ).fetchall()
    if not sources:
        return False

    url_host_pattern = re.compile(
        rf"https?://{re.escape(domain)}(?=[:/?#\"'\s]|$)",
        re.IGNORECASE,
    )
    return all(url_host_pattern.search(source["evidence_text"] or "") for source in sources)


SCHEMA = """
CREATE TABLE IF NOT EXISTS workspaces (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL UNIQUE,
  slug TEXT NOT NULL UNIQUE,
  description TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS keywords (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  workspace_id INTEGER NOT NULL DEFAULT 1 REFERENCES workspaces(id),
  text TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  paused_by_user INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE (workspace_id, text)
);

CREATE TABLE IF NOT EXISTS queues (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  workspace_id INTEGER NOT NULL DEFAULT 1 REFERENCES workspaces(id),
  name TEXT NOT NULL,
  queue_type TEXT NOT NULL CHECK(queue_type IN ('keyword_search', 'url_crawl')),
  status TEXT NOT NULL DEFAULT 'draft',
  max_concurrent_jobs INTEGER NOT NULL DEFAULT 1,
  active_max_concurrent_jobs INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  started_at TEXT,
  stopped_at TEXT,
  UNIQUE (workspace_id, name, queue_type)
);

CREATE TABLE IF NOT EXISTS queue_routes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  workspace_id INTEGER NOT NULL DEFAULT 1 REFERENCES workspaces(id),
  keyword_queue_id INTEGER NOT NULL REFERENCES queues(id) ON DELETE CASCADE,
  url_queue_id INTEGER NOT NULL REFERENCES queues(id) ON DELETE CASCADE,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE (keyword_queue_id),
  UNIQUE (url_queue_id)
);

CREATE TABLE IF NOT EXISTS search_dorks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  template TEXT NOT NULL,
  enabled INTEGER NOT NULL DEFAULT 1,
  description TEXT,
  created_by TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS search_queries (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  workspace_id INTEGER NOT NULL DEFAULT 1 REFERENCES workspaces(id),
  queue_id INTEGER REFERENCES queues(id),
  keyword_id INTEGER NOT NULL REFERENCES keywords(id),
  dork_id INTEGER NOT NULL REFERENCES search_dorks(id),
  query_text TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  result_count INTEGER NOT NULL DEFAULT 0,
  page_count INTEGER NOT NULL DEFAULT 0,
  last_error TEXT,
  started_at TEXT,
  finished_at TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE (queue_id, keyword_id, dork_id, query_text)
);

CREATE TABLE IF NOT EXISTS jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  workspace_id INTEGER NOT NULL DEFAULT 1 REFERENCES workspaces(id),
  queue_id INTEGER REFERENCES queues(id),
  type TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  payload TEXT NOT NULL,
  dedupe_key TEXT,
  attempts INTEGER NOT NULL DEFAULT 0,
  error TEXT,
  target_url_id INTEGER,
  target_queue_item_id INTEGER,
  worker_slot_key TEXT,
  run_token TEXT,
  heartbeat_at TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  started_at TEXT,
  finished_at TEXT,
  UNIQUE (workspace_id, dedupe_key)
);

CREATE TABLE IF NOT EXISTS search_queue_items (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  workspace_id INTEGER NOT NULL DEFAULT 1 REFERENCES workspaces(id),
  queue_id INTEGER NOT NULL REFERENCES queues(id) ON DELETE CASCADE,
  search_query_id INTEGER NOT NULL REFERENCES search_queries(id),
  keyword_id INTEGER NOT NULL REFERENCES keywords(id),
  output_url_queue_id INTEGER REFERENCES queues(id),
  status TEXT NOT NULL DEFAULT 'paused',
  result_count INTEGER NOT NULL DEFAULT 0,
  page_count INTEGER NOT NULL DEFAULT 0,
  error TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  started_at TEXT,
  finished_at TEXT,
  UNIQUE (queue_id, search_query_id)
);

CREATE TABLE IF NOT EXISTS urls (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  workspace_id INTEGER NOT NULL DEFAULT 1 REFERENCES workspaces(id),
  url_raw TEXT NOT NULL,
  url_norm TEXT NOT NULL,
  domain TEXT NOT NULL,
  title TEXT,
  first_source TEXT NOT NULL,
  review_status TEXT NOT NULL DEFAULT 'pending_review',
  crawl_status TEXT NOT NULL DEFAULT 'not_crawled',
  final_url TEXT,
  status_code INTEGER,
  content_type TEXT,
  content_length INTEGER,
  fetch_method TEXT,
  crawl_error TEXT,
  html TEXT,
  crawled_at TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE (workspace_id, url_norm)
);

CREATE TABLE IF NOT EXISTS whitelist_urls (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  workspace_id INTEGER NOT NULL DEFAULT 1 REFERENCES workspaces(id),
  url_raw TEXT NOT NULL,
  url_norm TEXT NOT NULL,
  match_type TEXT NOT NULL DEFAULT 'exact',
  match_value TEXT,
  note TEXT,
  enabled INTEGER NOT NULL DEFAULT 1,
  matched_url_count INTEGER NOT NULL DEFAULT 0,
  queue_item_count INTEGER NOT NULL DEFAULT 0,
  counts_updated_at TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE (workspace_id, url_norm)
);

CREATE TABLE IF NOT EXISTS url_bodies (
  url_id INTEGER PRIMARY KEY REFERENCES urls(id) ON DELETE CASCADE,
  html TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS url_queue_items (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  workspace_id INTEGER NOT NULL DEFAULT 1 REFERENCES workspaces(id),
  queue_id INTEGER NOT NULL REFERENCES queues(id) ON DELETE CASCADE,
  url_id INTEGER NOT NULL REFERENCES urls(id),
  source_queue_id INTEGER REFERENCES queues(id),
  source_search_query_id INTEGER REFERENCES search_queries(id),
  source_search_queue_item_id INTEGER REFERENCES search_queue_items(id),
  source_url_queue_item_id INTEGER REFERENCES url_queue_items(id),
  status TEXT NOT NULL DEFAULT 'paused',
  error TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  started_at TEXT,
  finished_at TEXT,
  UNIQUE (queue_id, url_id)
);

CREATE TABLE IF NOT EXISTS url_sources (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  workspace_id INTEGER NOT NULL DEFAULT 1 REFERENCES workspaces(id),
  url_id INTEGER NOT NULL REFERENCES urls(id),
  source_type TEXT NOT NULL,
  dedupe_key TEXT NOT NULL,
  queue_id INTEGER REFERENCES queues(id),
  keyword_id INTEGER REFERENCES keywords(id),
  search_query_id INTEGER REFERENCES search_queries(id),
  source_url_id INTEGER REFERENCES urls(id),
  title TEXT,
  snippet TEXT,
  rank INTEGER,
  page_no INTEGER,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE (workspace_id, dedupe_key)
);

CREATE TABLE IF NOT EXISTS extraction_rules (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  ioc_type TEXT NOT NULL,
  pattern TEXT NOT NULL,
  flags TEXT NOT NULL DEFAULT '',
  value_group INTEGER NOT NULL DEFAULT 0,
  input_scope TEXT NOT NULL DEFAULT 'text',
  exclude_pattern TEXT,
  normalizer TEXT NOT NULL DEFAULT 'default',
  priority INTEGER NOT NULL DEFAULT 100,
  enabled INTEGER NOT NULL DEFAULT 1,
  builtin INTEGER NOT NULL DEFAULT 0,
  description TEXT,
  created_by TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS iocs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  workspace_id INTEGER NOT NULL DEFAULT 1 REFERENCES workspaces(id),
  type TEXT NOT NULL,
  value_raw TEXT NOT NULL,
  value_norm TEXT NOT NULL,
  deleted INTEGER NOT NULL DEFAULT 0,
  deleted_at TEXT,
  collected_at TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE (workspace_id, type, value_norm)
);

CREATE TABLE IF NOT EXISTS ioc_sources (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  workspace_id INTEGER NOT NULL DEFAULT 1 REFERENCES workspaces(id),
  ioc_id INTEGER NOT NULL REFERENCES iocs(id),
  source_url_id INTEGER NOT NULL REFERENCES urls(id),
  source_type TEXT NOT NULL DEFAULT 'crawl',
  extraction_rule_id INTEGER REFERENCES extraction_rules(id),
  evidence_text TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE (ioc_id, source_url_id, extraction_rule_id)
);

CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status, id);
CREATE INDEX IF NOT EXISTS idx_urls_review ON urls(review_status);
CREATE INDEX IF NOT EXISTS idx_urls_crawl ON urls(crawl_status);
CREATE INDEX IF NOT EXISTS idx_urls_domain ON urls(domain);
CREATE INDEX IF NOT EXISTS idx_url_sources_url ON url_sources(url_id);
CREATE INDEX IF NOT EXISTS idx_whitelist_urls_norm ON whitelist_urls(url_norm);
CREATE INDEX IF NOT EXISTS idx_iocs_type ON iocs(type);
"""
