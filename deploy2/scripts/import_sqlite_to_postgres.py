#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any

from psycopg import connect
from psycopg import sql


TABLE_ORDER = [
    "keywords",
    "queues",
    "search_dorks",
    "extraction_rules",
    "urls",
    "whitelist_urls",
    "queue_routes",
    "search_queries",
    "jobs",
    "search_queue_items",
    "url_bodies",
    "url_queue_items",
    "url_sources",
    "iocs",
    "ioc_sources",
    "worker_slots",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Import the old SQLite app database into the production PostgreSQL database."
    )
    parser.add_argument("--sqlite-path", required=True, help="Path to the old SQLite database.")
    parser.add_argument("--env-file", help="Optional app env file to load before connecting to PostgreSQL.")
    parser.add_argument(
        "--replace",
        action="store_true",
        help="Truncate target app tables before import. Required to avoid accidental mixed data.",
    )
    parser.add_argument("--batch-size", type=int, default=1000)
    args = parser.parse_args()
    if not args.replace:
        parser.error("--replace is required for production imports")
    return args


def load_env_file(path: str | os.PathLike[str]) -> None:
    env_path = Path(path)
    if not env_path.exists():
        raise FileNotFoundError(f"Env file not found: {env_path}")
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key or key in os.environ:
            continue
        value = value.strip().strip("\"'")
        os.environ[key] = value


def find_project_root() -> Path:
    candidates: list[Path] = [Path.cwd(), Path(__file__).resolve().parent]
    candidates.extend(Path(__file__).resolve().parents)
    for candidate in candidates:
        if (candidate / "ioc_app" / "db.py").exists() and (candidate / "app.py").exists():
            return candidate
    raise RuntimeError("Could not find project root containing app.py and ioc_app/db.py")


def sqlite_identifier(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def sqlite_user_tables(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        """
        SELECT name
        FROM sqlite_master
        WHERE type = 'table'
          AND name NOT LIKE 'sqlite_%'
        ORDER BY name
        """
    ).fetchall()
    return [row["name"] for row in rows]


def sqlite_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    rows = conn.execute(f"PRAGMA table_info({sqlite_identifier(table)})").fetchall()
    return [row["name"] for row in rows]


def postgres_tables(conn) -> set[str]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT table_name
            FROM information_schema.tables
            WHERE table_schema = 'public'
              AND table_type = 'BASE TABLE'
            """
        )
        return {row[0] for row in cur.fetchall()}


def postgres_columns(conn, table: str) -> dict[str, str]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT column_name, data_type
            FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name = %s
            ORDER BY ordinal_position
            """,
            (table,),
        )
        return {row[0]: row[1] for row in cur.fetchall()}


def connect_postgres_raw():
    database_url = os.environ.get("DATABASE_URL", "").strip()
    connect_timeout = int(os.environ.get("POSTGRES_CONNECT_TIMEOUT", "30"))
    if database_url:
        return connect(database_url, connect_timeout=connect_timeout)
    return connect(
        host=os.environ.get("POSTGRES_HOST", "localhost"),
        port=int(os.environ.get("POSTGRES_PORT", "5432")),
        dbname=os.environ.get("POSTGRES_DB", "ioc_investigator"),
        user=os.environ.get("POSTGRES_USER", "ioc_app"),
        password=os.environ.get("POSTGRES_PASSWORD", ""),
        connect_timeout=connect_timeout,
    )


def initialize_postgres_schema(project_root: Path) -> None:
    sys.path.insert(0, str(project_root))
    os.environ.setdefault("DB_BACKEND", "postgresql")
    os.environ.setdefault("FLASK_SKIP_DOTENV", "1")
    os.environ.setdefault("AUTO_WORKER_ENABLED", "0")
    from ioc_app.db import init_db

    init_db()


def ordered_tables(source_tables: list[str], target_tables: set[str]) -> list[str]:
    source_set = set(source_tables)
    ordered = [table for table in TABLE_ORDER if table in source_set and table in target_tables]
    ordered.extend(
        table
        for table in source_tables
        if table not in ordered and table in target_tables
    )
    return ordered


def truncate_tables(conn, tables: list[str]) -> None:
    if not tables:
        return
    with conn.cursor() as cur:
        cur.execute(
            sql.SQL("TRUNCATE TABLE {} RESTART IDENTITY CASCADE").format(
                sql.SQL(", ").join(sql.Identifier(table) for table in tables)
            )
        )
    conn.commit()


def normalize_value(value: Any, target_type: str) -> Any:
    if value == "" and ("timestamp" in target_type or target_type == "date"):
        return None
    return value


def import_table(
    sqlite_conn: sqlite3.Connection,
    pg_conn,
    table: str,
    batch_size: int,
) -> int:
    source_columns = sqlite_columns(sqlite_conn, table)
    target_columns = postgres_columns(pg_conn, table)
    common_columns = [column for column in source_columns if column in target_columns]
    if not common_columns:
        print(f"Skipping {table}: no common columns")
        return 0

    defer_self_ref = table == "url_queue_items" and "source_url_queue_item_id" in common_columns
    insert_columns = [
        column
        for column in common_columns
        if not (defer_self_ref and column == "source_url_queue_item_id")
    ]

    select_sql = (
        f"SELECT {', '.join(sqlite_identifier(column) for column in common_columns)} "
        f"FROM {sqlite_identifier(table)}"
    )
    if "id" in common_columns:
        select_sql += " ORDER BY id"

    insert_stmt = sql.SQL("INSERT INTO {} ({}) VALUES ({})").format(
        sql.Identifier(table),
        sql.SQL(", ").join(sql.Identifier(column) for column in insert_columns),
        sql.SQL(", ").join(sql.Placeholder() for _ in insert_columns),
    )
    self_ref_updates: list[tuple[Any, Any]] = []
    inserted = 0

    cursor = sqlite_conn.execute(select_sql)
    while True:
        rows = cursor.fetchmany(batch_size)
        if not rows:
            break
        values = []
        for row in rows:
            values.append(
                tuple(
                    normalize_value(row[column], target_columns[column])
                    for column in insert_columns
                )
            )
            if defer_self_ref and row["source_url_queue_item_id"] is not None:
                self_ref_updates.append((row["source_url_queue_item_id"], row["id"]))
        with pg_conn.cursor() as pg_cursor:
            pg_cursor.executemany(insert_stmt, values)
        inserted += len(values)
        pg_conn.commit()

    if self_ref_updates:
        update_stmt = sql.SQL(
            "UPDATE {} SET source_url_queue_item_id = %s WHERE id = %s"
        ).format(sql.Identifier(table))
        with pg_conn.cursor() as pg_cursor:
            pg_cursor.executemany(update_stmt, self_ref_updates)
        pg_conn.commit()

    skipped_columns = [column for column in source_columns if column not in target_columns]
    if skipped_columns:
        print(f"{table}: skipped source-only columns: {', '.join(skipped_columns)}")
    print(f"{table}: imported {inserted} rows")
    return inserted


def reset_sequences(conn, tables: list[str]) -> None:
    with conn.cursor() as cur:
        for table in tables:
            columns = postgres_columns(conn, table)
            if "id" not in columns:
                continue
            cur.execute("SELECT pg_get_serial_sequence(%s, %s)", (f"public.{table}", "id"))
            row = cur.fetchone()
            sequence_name = row[0] if row else None
            if not sequence_name:
                continue
            cur.execute(
                sql.SQL("SELECT MAX({}) FROM {}").format(
                    sql.Identifier("id"),
                    sql.Identifier(table),
                )
            )
            max_id = cur.fetchone()[0]
            if max_id is None:
                cur.execute("SELECT setval(%s, 1, false)", (sequence_name,))
            else:
                cur.execute("SELECT setval(%s, %s, true)", (sequence_name, max_id))
    conn.commit()


def count_sqlite_rows(conn: sqlite3.Connection, table: str) -> int:
    row = conn.execute(f"SELECT COUNT(*) AS count FROM {sqlite_identifier(table)}").fetchone()
    return int(row["count"])


def main() -> int:
    args = parse_args()
    sqlite_path = Path(args.sqlite_path)
    if not sqlite_path.exists():
        raise FileNotFoundError(f"SQLite database not found: {sqlite_path}")
    if args.env_file:
        load_env_file(args.env_file)

    project_root = find_project_root()
    initialize_postgres_schema(project_root)

    sqlite_conn = sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True)
    sqlite_conn.row_factory = sqlite3.Row

    with sqlite_conn:
        source_tables = sqlite_user_tables(sqlite_conn)

    pg_conn = connect_postgres_raw()
    try:
        target_tables = postgres_tables(pg_conn)
        tables_to_import = ordered_tables(source_tables, target_tables)
        skipped_tables = [table for table in source_tables if table not in target_tables]
        if skipped_tables:
            print(f"Skipping source tables not present in PostgreSQL: {', '.join(skipped_tables)}")

        app_target_tables = [table for table in TABLE_ORDER if table in target_tables]
        replace_tables = list(dict.fromkeys(app_target_tables + tables_to_import))
        print("Replacing PostgreSQL app tables...")
        truncate_tables(pg_conn, replace_tables)

        imported_counts: dict[str, int] = {}
        for table in tables_to_import:
            source_count = count_sqlite_rows(sqlite_conn, table)
            imported_count = import_table(sqlite_conn, pg_conn, table, args.batch_size)
            imported_counts[table] = imported_count
            if imported_count != source_count:
                raise RuntimeError(
                    f"Row count mismatch for {table}: source={source_count}, imported={imported_count}"
                )

        reset_sequences(pg_conn, tables_to_import)
    finally:
        pg_conn.close()
        sqlite_conn.close()

    print("Import completed successfully.")
    print(f"Tables imported: {len(imported_counts)}")
    print(f"Rows imported: {sum(imported_counts.values())}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
