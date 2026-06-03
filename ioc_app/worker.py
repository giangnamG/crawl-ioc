from __future__ import annotations

import json
import os
import threading
import time
import traceback
import urllib.parse
import uuid
from sqlite3 import Connection

from .browser import BrowserClient, is_google_antibot_error, is_retryable_proxy_error
from .db import (
    connect,
    is_url_whitelisted,
    is_url_whitelisted_latest,
    record_keyword_search_url_ioc,
    upsert_ioc_record,
    upsert_ioc_source,
)
from .extractor import extract_iocs_by_rules
from .normalizers import get_domain, is_media_asset_url, normalize_domain, normalize_url


class PausedJob(Exception):
    """Raised when a queued job has been paused before execution."""


class LeaseLostJob(Exception):
    """Raised when a stale worker lease has been reclaimed by the maintainer."""


TERMINAL_CRAWL_STATUSES = {"crawled", "metadata_only"}

HTTP_TEXT_EXTENSIONS = {
    ".atom",
    ".css",
    ".csv",
    ".js",
    ".json",
    ".map",
    ".mjs",
    ".rss",
    ".svg",
    ".tsv",
    ".txt",
    ".xml",
}

HTTP_BINARY_EXTENSIONS = {
    ".7z",
    ".avif",
    ".bmp",
    ".eot",
    ".gif",
    ".gz",
    ".ico",
    ".jpeg",
    ".jpg",
    ".mp3",
    ".mp4",
    ".pdf",
    ".png",
    ".rar",
    ".tar",
    ".ttf",
    ".webm",
    ".webp",
    ".woff",
    ".woff2",
    ".zip",
}


def env_int(name: str, default: int, minimum: int = 0, maximum: int | None = None) -> int:
    raw = os.environ.get(name)
    try:
        value = int(raw) if raw is not None else default
    except (TypeError, ValueError):
        value = default
    value = max(minimum, value)
    if maximum is not None:
        value = min(maximum, value)
    return value


def env_float(name: str, default: float, minimum: float = 0.0) -> float:
    raw = os.environ.get(name)
    try:
        value = float(raw) if raw is not None else default
    except (TypeError, ValueError):
        value = default
    return max(minimum, value)


def url_crawl_concurrency() -> int:
    return env_int("URL_CRAWL_CONCURRENCY", 1, minimum=1, maximum=32)


def url_crawl_worker_threads() -> int:
    concurrency = url_crawl_concurrency()
    return env_int("URL_CRAWL_WORKER_THREADS", concurrency + 1, minimum=concurrency, maximum=64)


def worker_slot_stale_seconds() -> int:
    return env_int("WORKER_SLOT_STALE_SECONDS", 900, minimum=60)


def worker_slot_max_seconds() -> int:
    return env_int("WORKER_SLOT_MAX_SECONDS", 1800, minimum=120)


def worker_heartbeat_seconds() -> float:
    return env_float("WORKER_HEARTBEAT_SECONDS", 5.0, minimum=1.0)


def search_job_max_attempts() -> int:
    return env_int("SEARCH_JOB_MAX_ATTEMPTS", 3, minimum=1, maximum=20)


def is_retryable_search_error(exc: Exception) -> bool:
    return is_retryable_proxy_error(exc) or is_google_antibot_error(exc)


def payload_int(payload: dict[str, object], key: str) -> int | None:
    value = payload.get(key)
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def enqueue_job(
    conn: Connection,
    job_type: str,
    payload: dict[str, object],
    dedupe_key: str,
    queue_id: int | None = None,
    initial_status: str = "pending",
) -> None:
    payload_json = json.dumps(payload)
    target_url_id = payload_int(payload, "url_id") if job_type == "crawl_url" else None
    target_queue_item_id = (
        payload_int(payload, "url_queue_item_id") if job_type == "crawl_url" else None
    )
    existing = conn.execute(
        "SELECT id, status FROM jobs WHERE dedupe_key = ?", (dedupe_key,)
    ).fetchone()
    if existing:
        if existing["status"] == "running":
            return
        next_status = (
            existing["status"]
            if existing["status"] == "pending" and initial_status == "paused"
            else initial_status
        )
        conn.execute(
            """
            UPDATE jobs
            SET queue_id = ?,
                status = ?,
                payload = ?,
                target_url_id = ?,
                target_queue_item_id = ?,
                attempts = CASE WHEN status IN ('failed', 'done') THEN 0 ELSE attempts END,
                error = NULL,
                started_at = NULL,
                finished_at = NULL,
                worker_slot_key = NULL,
                run_token = NULL,
                heartbeat_at = NULL
            WHERE id = ?
            """,
            (
                queue_id,
                next_status,
                payload_json,
                target_url_id,
                target_queue_item_id,
                existing["id"],
            ),
        )
        return

    conn.execute(
        """
        INSERT OR IGNORE INTO jobs(
          queue_id, type, status, payload, dedupe_key, target_url_id, target_queue_item_id
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            queue_id,
            job_type,
            initial_status,
            payload_json,
            dedupe_key,
            target_url_id,
            target_queue_item_id,
        ),
    )


def claim_next_job(
    job_types: tuple[str, ...] | None = None,
    worker_slot_key: str | None = None,
    worker_type: str | None = None,
) -> dict[str, object] | None:
    run_token = uuid.uuid4().hex
    type_filter = ""
    params: list[object] = []
    if job_types:
        placeholders = ",".join("?" for _ in job_types)
        type_filter = f"AND j.type IN ({placeholders})"
        params.extend(job_types)

    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        if worker_slot_key:
            conn.execute(
                """
                INSERT INTO worker_slots(slot_key, worker_type, enabled, status)
                VALUES (?, ?, 1, 'idle')
                ON CONFLICT(slot_key) DO UPDATE SET
                    worker_type = excluded.worker_type,
                    enabled = 1,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (worker_slot_key, worker_type or "worker"),
            )

        row = conn.execute(
            f"""
            SELECT j.*
            FROM jobs j
            LEFT JOIN queues q ON q.id = j.queue_id
            WHERE j.status = 'pending'
              AND (j.queue_id IS NULL OR q.status = 'running')
              {type_filter}
              AND (
                j.type != 'crawl_url'
                OR (
                  (
                    SELECT COUNT(*)
                    FROM jobs running
                    WHERE running.type = 'crawl_url'
                      AND running.status = 'running'
                  ) < ?
                  AND (
                    j.queue_id IS NULL
                    OR (
                      (
                        SELECT COUNT(*)
                        FROM jobs queue_running
                        WHERE queue_running.type = 'crawl_url'
                          AND queue_running.status = 'running'
                          AND queue_running.queue_id = j.queue_id
                      ) < COALESCE(q.active_max_concurrent_jobs, q.max_concurrent_jobs, 1)
                    )
                  )
                  AND (
                    j.target_url_id IS NULL
                    OR NOT EXISTS (
                      SELECT 1
                      FROM jobs same_url
                      WHERE same_url.type = 'crawl_url'
                        AND same_url.status = 'running'
                        AND same_url.target_url_id = j.target_url_id
                    )
                  )
                )
              )
            ORDER BY
              CASE
                WHEN j.type = 'crawl_url' AND j.queue_id IS NOT NULL THEN (
                  SELECT COUNT(*)
                  FROM jobs queue_running
                  WHERE queue_running.type = 'crawl_url'
                    AND queue_running.status = 'running'
                    AND queue_running.queue_id = j.queue_id
                )
                ELSE 0
              END,
              j.id
            LIMIT 1
            """,
            (*params, url_crawl_concurrency()),
        ).fetchone()
        if not row:
            if worker_slot_key:
                conn.execute(
                    """
                    UPDATE worker_slots
                    SET status = CASE WHEN status = 'running' THEN status ELSE 'idle' END,
                        heartbeat_at = CURRENT_TIMESTAMP,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE slot_key = ?
                    """,
                    (worker_slot_key,),
                )
            conn.commit()
            return None

        updated = conn.execute(
            """
            UPDATE jobs
            SET status = 'running',
                worker_slot_key = ?,
                run_token = ?,
                heartbeat_at = CURRENT_TIMESTAMP
            WHERE id = ?
              AND status = 'pending'
            """,
            (worker_slot_key, run_token, row["id"]),
        ).rowcount
        if updated == 1 and worker_slot_key:
            conn.execute(
                """
                UPDATE worker_slots
                SET status = 'running',
                    job_id = ?,
                    queue_id = ?,
                    target_url_id = ?,
                    target_queue_item_id = ?,
                    run_token = ?,
                    thread_name = ?,
                    pid = ?,
                    started_at = CURRENT_TIMESTAMP,
                    heartbeat_at = CURRENT_TIMESTAMP,
                    finished_at = NULL,
                    error = NULL,
                    updated_at = CURRENT_TIMESTAMP
                WHERE slot_key = ?
                """,
                (
                    row["id"],
                    row["queue_id"],
                    row["target_url_id"],
                    row["target_queue_item_id"],
                    run_token,
                    threading.current_thread().name,
                    os.getpid(),
                    worker_slot_key,
                ),
            )
        conn.commit()
        if updated != 1:
            return None
        claimed = dict(row)
        claimed["run_token"] = run_token
        claimed["worker_slot_key"] = worker_slot_key
        return claimed


def validate_search_job_payload(conn: Connection, job: dict[str, object], payload: dict[str, object]):
    queue_id = job.get("queue_id")
    if not queue_id:
        return None
    search_query_id = int(payload["search_query_id"])
    raw_item_id = payload.get("search_queue_item_id")
    if raw_item_id is None:
        fallback_item = conn.execute(
            """
            SELECT id
            FROM search_queue_items
            WHERE queue_id = ? AND search_query_id = ?
            """,
            (queue_id, search_query_id),
        ).fetchone()
        if not fallback_item:
            raise ValueError("Search job payload has no queue item.")
        raw_item_id = fallback_item["id"]
    item_id = int(raw_item_id)
    row = conn.execute(
        """
        SELECT sqi.*, q.status AS queue_status
        FROM search_queue_items sqi
        JOIN queues q ON q.id = sqi.queue_id
        WHERE sqi.id = ?
          AND sqi.queue_id = ?
          AND sqi.search_query_id = ?
          AND q.queue_type = 'keyword_search'
        """,
        (item_id, queue_id, search_query_id),
    ).fetchone()
    if not row:
        raise ValueError("Search job payload does not match its keyword queue item.")
    current_job = conn.execute(
        "SELECT status FROM jobs WHERE id = ?",
        (job["id"],),
    ).fetchone()
    queue_allows_claimed_job = (
        row["queue_status"] == "running"
        or (
            row["queue_status"] == "stopping"
            and current_job
            and current_job["status"] == "running"
        )
    )
    if not queue_allows_claimed_job or row["status"] not in {"pending", "running"}:
        raise PausedJob("Search queue item is not runnable.")

    output_url_queue_id = payload.get("output_url_queue_id") or row["output_url_queue_id"]
    if output_url_queue_id is None:
        output_queue = get_bound_output_url_queue(conn, int(queue_id))
        output_url_queue_id = output_queue["id"] if output_queue else None
    if output_url_queue_id is None:
        raise ValueError("Search job has no output URL queue.")
    output_url_queue_id = int(output_url_queue_id)

    output_queue = conn.execute(
        "SELECT id FROM queues WHERE id = ? AND queue_type = 'url_crawl'",
        (output_url_queue_id,),
    ).fetchone()
    if not output_queue:
        raise ValueError("Search job output URL queue is invalid.")
    if row["output_url_queue_id"] and int(row["output_url_queue_id"]) != output_url_queue_id:
        raise ValueError("Search job output URL queue does not match the queue item.")

    conn.execute(
        """
        UPDATE search_queue_items
        SET output_url_queue_id = ?
        WHERE id = ?
          AND output_url_queue_id IS NULL
        """,
        (output_url_queue_id, item_id),
    )
    return {"id": item_id, "output_url_queue_id": output_url_queue_id}


def validate_crawl_job_payload(conn: Connection, job: dict[str, object], payload: dict[str, object]):
    queue_id = job.get("queue_id")
    if not queue_id:
        return None
    url_id = int(payload["url_id"])
    item_id = int(payload["url_queue_item_id"])
    row = conn.execute(
        """
        SELECT uqi.*, q.status AS queue_status, u.review_status
        FROM url_queue_items uqi
        JOIN queues q ON q.id = uqi.queue_id
        JOIN urls u ON u.id = uqi.url_id
        WHERE uqi.id = ?
          AND uqi.queue_id = ?
          AND uqi.url_id = ?
          AND q.queue_type = 'url_crawl'
        """,
        (item_id, queue_id, url_id),
    ).fetchone()
    if not row:
        raise ValueError("Crawl job payload does not match its URL queue item.")
    current_job = conn.execute(
        "SELECT status FROM jobs WHERE id = ?",
        (job["id"],),
    ).fetchone()
    queue_allows_claimed_job = (
        row["queue_status"] == "running"
        or (
            row["queue_status"] == "stopping"
            and current_job
            and current_job["status"] == "running"
        )
    )
    if not queue_allows_claimed_job or row["status"] not in {"pending", "running"}:
        raise PausedJob("URL queue item is not runnable.")
    if row["review_status"] != "approved":
        conn.execute("UPDATE url_queue_items SET status = 'pending_review' WHERE id = ?", (item_id,))
        raise PausedJob("URL queue item is waiting for review approval.")
    return row


def heartbeat_job(job_id: int, run_token: str, worker_slot_key: str | None = None) -> None:
    with connect() as conn:
        conn.execute(
            """
            UPDATE jobs
            SET heartbeat_at = CURRENT_TIMESTAMP
            WHERE id = ?
              AND run_token = ?
              AND status = 'running'
            """,
            (job_id, run_token),
        )
        if worker_slot_key:
            conn.execute(
                """
                UPDATE worker_slots
                SET heartbeat_at = CURRENT_TIMESTAMP,
                    updated_at = CURRENT_TIMESTAMP
                WHERE slot_key = ?
                  AND run_token = ?
                  AND status = 'running'
                """,
                (worker_slot_key, run_token),
            )


def release_worker_slot(
    worker_slot_key: str | None,
    run_token: str | None,
    status: str = "idle",
    error: str | None = None,
) -> None:
    if not worker_slot_key or not run_token:
        return
    with connect() as conn:
        release_worker_slot_on_conn(conn, worker_slot_key, run_token, status, error)


def release_worker_slot_on_conn(
    conn: Connection,
    worker_slot_key: str | None,
    run_token: str | None,
    status: str = "idle",
    error: str | None = None,
) -> None:
    if not worker_slot_key or not run_token:
        return
    conn.execute(
        """
        UPDATE worker_slots
        SET status = ?,
            job_id = NULL,
            queue_id = NULL,
            target_url_id = NULL,
            target_queue_item_id = NULL,
            run_token = NULL,
            finished_at = CURRENT_TIMESTAMP,
            error = ?,
            updated_at = CURRENT_TIMESTAMP
        WHERE slot_key = ?
          AND run_token = ?
        """,
        (status, error, worker_slot_key, run_token),
    )


def start_job_heartbeat(
    job_id: int,
    run_token: str,
    worker_slot_key: str | None,
) -> tuple[threading.Event, threading.Thread]:
    stop_event = threading.Event()
    interval = worker_heartbeat_seconds()

    def beat() -> None:
        while not stop_event.wait(interval):
            try:
                heartbeat_job(job_id, run_token, worker_slot_key)
            except Exception:
                pass

    thread = threading.Thread(
        target=beat,
        name=f"heartbeat-{job_id}",
        daemon=True,
    )
    thread.start()
    return stop_event, thread


def ensure_job_lease(conn: Connection, job_id: int | None, run_token: str | None) -> None:
    if not job_id or not run_token:
        return
    row = conn.execute(
        """
        SELECT 1
        FROM jobs
        WHERE id = ?
          AND run_token = ?
          AND status = 'running'
        """,
        (job_id, run_token),
    ).fetchone()
    if not row:
        raise LeaseLostJob(f"Job #{job_id} lease is no longer active.")


def recover_stale_worker_slots(stale_seconds: int | None = None) -> int:
    stale_seconds = stale_seconds or worker_slot_stale_seconds()
    heartbeat_cutoff = f"-{int(stale_seconds)} seconds"
    max_runtime_cutoff = f"-{worker_slot_max_seconds()} seconds"
    recovered = 0
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute(
            """
            SELECT *
            FROM worker_slots
            WHERE status = 'running'
              AND (
                heartbeat_at IS NULL
                OR heartbeat_at < datetime('now', ?)
                OR (
                  worker_type = 'crawl_url'
                  AND started_at IS NOT NULL
                  AND started_at < datetime('now', ?)
                )
              )
            """,
            (heartbeat_cutoff, max_runtime_cutoff),
        ).fetchall()
        for slot in rows:
            message = f"Recovered stale worker slot {slot['slot_key']}."
            if slot["worker_type"] == "crawl_url" and slot["started_at"]:
                maxed_out = conn.execute(
                    "SELECT ? < datetime('now', ?)",
                    (slot["started_at"], max_runtime_cutoff),
                ).fetchone()
                if maxed_out and maxed_out[0]:
                    message = f"Recovered overrun crawl slot {slot['slot_key']}."
            job = None
            if slot["job_id"] and slot["run_token"]:
                job = conn.execute(
                    """
                    SELECT *
                    FROM jobs
                    WHERE id = ?
                      AND status = 'running'
                      AND run_token = ?
                    """,
                    (slot["job_id"], slot["run_token"]),
                ).fetchone()
            if job:
                payload = {}
                try:
                    payload = json.loads(job["payload"] or "{}")
                except Exception:
                    payload = {}
                conn.execute(
                    """
                    UPDATE jobs
                    SET status = 'pending',
                        started_at = NULL,
                        worker_slot_key = NULL,
                        run_token = NULL,
                        heartbeat_at = NULL,
                        error = ?
                    WHERE id = ?
                    """,
                    (message, job["id"]),
                )
                if job["type"] == "crawl_url":
                    item_id = payload_int(payload, "url_queue_item_id")
                    url_id = payload_int(payload, "url_id")
                    if item_id:
                        conn.execute(
                            """
                            UPDATE url_queue_items
                            SET status = 'pending',
                                error = ?
                            WHERE id = ?
                              AND status = 'running'
                            """,
                            (message, item_id),
                        )
                    if url_id:
                        conn.execute(
                            """
                            UPDATE urls
                            SET crawl_status = 'not_crawled',
                                crawl_error = ?
                            WHERE id = ?
                              AND crawl_status = 'crawling'
                            """,
                            (message, url_id),
                        )
                if job["queue_id"]:
                    refresh_queue_status(conn, int(job["queue_id"]))
            conn.execute(
                """
                UPDATE worker_slots
                SET status = 'idle',
                    job_id = NULL,
                    queue_id = NULL,
                    target_url_id = NULL,
                    target_queue_item_id = NULL,
                    run_token = NULL,
                    finished_at = CURRENT_TIMESTAMP,
                    error = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (message, slot["id"]),
            )
            recovered += 1
        conn.commit()
    return recovered


def run_one(
    job_types: tuple[str, ...] | None = None,
    worker_slot_key: str | None = None,
    worker_type: str | None = None,
) -> str:
    recover_stale_worker_slots()
    job = claim_next_job(job_types=job_types, worker_slot_key=worker_slot_key, worker_type=worker_type)
    if not job:
        return "No pending job."

    run_token = str(job.get("run_token") or "")
    heartbeat_stop, heartbeat_thread = start_job_heartbeat(
        int(job["id"]),
        run_token,
        worker_slot_key,
    )
    final_slot_status = "idle"
    final_slot_error = None
    with connect() as conn:
        queue_id = job["queue_id"]
        conn.execute(
            """
            UPDATE jobs
            SET attempts = attempts + 1,
                started_at = CURRENT_TIMESTAMP,
                finished_at = NULL
            WHERE id = ?
              AND run_token = ?
              AND status = 'running'
            """,
            (job["id"], run_token),
        )

        payload: dict[str, object] = {}
        try:
            payload = json.loads(job["payload"])
            if job["type"] == "search_query":
                search_item = validate_search_job_payload(conn, job, payload)
                search_queue_item_id = int(search_item["id"]) if search_item else None
                output_url_queue_id = (
                    int(search_item["output_url_queue_id"]) if search_item else None
                )
                mark_search_queue_item(conn, search_queue_item_id, "running")
                process_search_query(
                    conn,
                    int(payload["search_query_id"]),
                    queue_id=int(queue_id) if queue_id else None,
                    search_queue_item_id=search_queue_item_id,
                    output_url_queue_id=output_url_queue_id,
                )
                mark_search_queue_item(conn, search_queue_item_id, "done")
            elif job["type"] == "crawl_url":
                url_item = validate_crawl_job_payload(conn, job, payload)
                url_queue_item_id = int(url_item["id"]) if url_item else payload.get("url_queue_item_id")
                mark_url_queue_item(conn, url_queue_item_id, "running")
                process_crawl_url(
                    conn,
                    int(payload["url_id"]),
                    queue_id=int(queue_id) if queue_id else None,
                    url_queue_item_id=url_queue_item_id,
                    job_id=int(job["id"]),
                    run_token=run_token,
                )
                mark_url_queue_item(conn, url_queue_item_id, "done")
                remove_url_from_queue_items(conn, int(payload["url_id"]))
            else:
                raise ValueError(f"Unsupported job type: {job['type']}")

            ensure_job_lease(conn, int(job["id"]), run_token)
            conn.execute(
                """
                UPDATE jobs
                SET status = 'done',
                    finished_at = CURRENT_TIMESTAMP,
                    error = NULL,
                    run_token = NULL,
                    heartbeat_at = NULL
                WHERE id = ?
                  AND run_token = ?
                """,
                (job["id"], run_token),
            )
            if queue_id:
                refresh_queue_status(conn, int(queue_id))
            return f"Job #{job['id']} completed."
        except LeaseLostJob as exc:
            final_slot_error = str(exc)
            return f"Job #{job['id']} lease lost."
        except PausedJob:
            conn.execute(
                """
                UPDATE jobs
                SET status = 'paused',
                    finished_at = CURRENT_TIMESTAMP,
                    error = NULL,
                    run_token = NULL,
                    heartbeat_at = NULL
                WHERE id = ?
                  AND run_token = ?
                """,
                (job["id"], run_token),
            )
            mark_url_queue_item_if_not_pending_review(conn, payload.get("url_queue_item_id"), "paused")
            mark_search_queue_item(conn, payload.get("search_queue_item_id"), "paused")
            if queue_id:
                refresh_queue_status(conn, int(queue_id))
            return f"Job #{job['id']} paused."
        except Exception as exc:
            if is_retryable_search_error(exc):
                error_text = str(exc)
            else:
                error_text = traceback.format_exc(limit=5)
            current_attempts = int(
                conn.execute("SELECT attempts FROM jobs WHERE id = ?", (job["id"],)).fetchone()["attempts"]
            )
            if (
                job["type"] == "search_query"
                and is_retryable_search_error(exc)
                and current_attempts < search_job_max_attempts()
            ):
                query_id = payload.get("search_query_id")
                search_item_id = payload.get("search_queue_item_id")
                if query_id:
                    conn.execute(
                        """
                        UPDATE search_queries
                        SET status = 'pending',
                            last_error = ?,
                            finished_at = NULL
                        WHERE id = ?
                        """,
                        (error_text, query_id),
                    )
                if search_item_id:
                    conn.execute(
                        """
                        UPDATE search_queue_items
                        SET status = 'pending',
                            error = ?,
                            finished_at = NULL
                        WHERE id = ?
                        """,
                        (error_text, search_item_id),
                    )
                conn.execute(
                    """
                    UPDATE jobs
                    SET status = 'pending',
                        started_at = NULL,
                        finished_at = NULL,
                        error = ?,
                        run_token = NULL,
                        heartbeat_at = NULL
                    WHERE id = ?
                      AND run_token = ?
                    """,
                    (error_text, job["id"], run_token),
                )
                if queue_id:
                    refresh_queue_status(conn, int(queue_id))
                return (
                    f"Job #{job['id']} retrying transient search error "
                    f"({current_attempts}/{search_job_max_attempts()}): {exc}"
                )
            final_slot_status = "error"
            final_slot_error = error_text
            mark_url_queue_item(conn, payload.get("url_queue_item_id"), "failed", error_text)
            mark_search_queue_item(conn, payload.get("search_queue_item_id"), "failed", error_text)
            conn.execute(
                """
                UPDATE jobs
                SET status = 'failed',
                    finished_at = CURRENT_TIMESTAMP,
                    error = ?,
                    run_token = NULL,
                    heartbeat_at = NULL
                WHERE id = ?
                  AND run_token = ?
                """,
                (error_text, job["id"], run_token),
            )
            if queue_id:
                refresh_queue_status(conn, int(queue_id))
            return f"Job #{job['id']} failed: {exc}"
        finally:
            heartbeat_stop.set()
            heartbeat_thread.join(timeout=1)
            release_worker_slot_on_conn(conn, worker_slot_key, run_token, final_slot_status, final_slot_error)


def run_all(limit: int = 25) -> list[str]:
    messages = []
    for _ in range(limit):
        message = run_one()
        messages.append(message)
        if message == "No pending job.":
            break
    return messages


def process_search_query(
    conn: Connection,
    search_query_id: int,
    queue_id: int | None = None,
    search_queue_item_id: int | None = None,
    output_url_queue_id: int | None = None,
) -> None:
    search_query = conn.execute(
        """
        SELECT sq.*,
               k.text AS keyword_text,
               d.template AS dork_template,
               q.status AS queue_status
        FROM search_queries sq
        JOIN keywords k ON k.id = sq.keyword_id
        JOIN search_dorks d ON d.id = sq.dork_id
        LEFT JOIN queues q ON q.id = sq.queue_id
        WHERE sq.id = ?
        """,
        (search_query_id,),
    ).fetchone()
    if not search_query:
        raise ValueError(f"Search query not found: {search_query_id}")
    effective_queue_id = queue_id or search_query["queue_id"]
    queue_status = search_query["queue_status"]
    if search_query["status"] == "paused" and not effective_queue_id:
        raise PausedJob(f"Search query paused: {search_query_id}")
    if effective_queue_id and effective_queue_id != search_query["queue_id"]:
        queue = conn.execute("SELECT status FROM queues WHERE id = ?", (effective_queue_id,)).fetchone()
        queue_status = queue["status"] if queue else None
    if effective_queue_id and queue_status not in {"running", "stopping"}:
        conn.execute(
            "UPDATE search_queries SET status = 'paused' WHERE id = ?",
            (search_query_id,),
        )
        raise PausedJob(f"Queue is not running for search query: {search_query_id}")
    output_url_queue = None
    if output_url_queue_id:
        output_url_queue = conn.execute(
            "SELECT * FROM queues WHERE id = ? AND queue_type = 'url_crawl'",
            (output_url_queue_id,),
        ).fetchone()
    elif effective_queue_id:
        output_url_queue = get_bound_output_url_queue(conn, int(effective_queue_id))
    if effective_queue_id and not output_url_queue:
        raise RuntimeError("Keyword queue has no bound URL crawl queue.")

    keyword = conn.execute(
        "SELECT status, COALESCE(paused_by_user, 0) AS paused_by_user FROM keywords WHERE id = ?",
        (search_query["keyword_id"],),
    ).fetchone()
    if keyword and keyword["paused_by_user"]:
        conn.execute(
            "UPDATE search_queries SET status = 'paused' WHERE id = ?",
            (search_query_id,),
        )
        raise PausedJob(f"Keyword paused: {search_query['keyword_text']}")

    conn.execute(
        """
        UPDATE search_queries
        SET status = 'running',
            result_count = 0,
            page_count = 0,
            last_error = NULL,
            started_at = CURRENT_TIMESTAMP,
            finished_at = NULL
        WHERE id = ?
        """,
        (search_query_id,),
    )
    conn.execute(
        "UPDATE keywords SET status = 'running' WHERE id = ? AND status != 'paused'",
        (search_query["keyword_id"],),
    )
    conn.commit()

    try:
        browser = BrowserClient()
        results = browser.search_google(search_query["query_text"])
        saved_urls = 0

        for item in results:
            url_norm = normalize_url(item.url)
            if not url_norm:
                continue
            domain = get_domain(url_norm)
            if not domain:
                continue

            if is_media_asset_url(url_norm) or is_url_whitelisted(conn, url_norm):
                continue

            url_id = upsert_url(conn, item.url, url_norm, domain, "google_search", title=item.title)
            saved_urls += 1
            upsert_url_source(
                conn,
                url_id=url_id,
                source_type="google_search",
                dedupe_key=f"google:{effective_queue_id or 'global'}:{search_query_id}:{item.page_no}:{item.rank}:{url_norm}",
                queue_id=effective_queue_id,
                keyword_id=search_query["keyword_id"],
                search_query_id=search_query_id,
                title=item.title,
                snippet=item.snippet,
                rank=item.rank,
                page_no=item.page_no,
            )
            if output_url_queue:
                enqueue_url_to_queue(
                    conn,
                    url_queue_id=int(output_url_queue["id"]),
                    url_id=url_id,
                    source_queue_id=int(effective_queue_id),
                    source_search_query_id=search_query_id,
                    source_search_queue_item_id=search_queue_item_id,
                )

        page_count = max((item.page_no for item in results), default=0)
        conn.execute(
            """
            UPDATE search_queries
            SET status = 'done',
                result_count = ?,
                page_count = ?,
                last_error = NULL,
                finished_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (saved_urls, page_count, search_query_id),
        )
        refresh_keyword_status(conn, int(search_query["keyword_id"]))
        if effective_queue_id:
            refresh_queue_status(conn, int(effective_queue_id))
    except Exception as exc:
        conn.execute(
            """
            UPDATE search_queries
            SET status = 'failed',
                last_error = ?,
                finished_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (str(exc), search_query_id),
        )
        refresh_keyword_status(conn, int(search_query["keyword_id"]))
        if effective_queue_id:
            refresh_queue_status(conn, int(effective_queue_id))
        raise


def classify_crawl_strategy(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    path = (parsed.path or "").lower()
    query = (parsed.query or "").lower()
    filename = path.rsplit("/", 1)[-1]
    extension = f".{filename.rsplit('.', 1)[-1]}" if "." in filename else ""

    if extension in HTTP_TEXT_EXTENSIONS:
        return "http_text"
    if extension in HTTP_BINARY_EXTENSIONS:
        return "http_binary_metadata"
    if path.endswith(("/feed", "/feed/", "/wp-json", "/wp-json/", "/robots.txt")):
        return "http_text"
    if "/feed/" in path or "/wp-json/" in path or path.endswith("sitemap.xml"):
        return "http_text"
    if "feed=" in query or "rest_route=" in query:
        return "http_text"
    return "cloak_browser"


def enqueue_discovered_url_if_new(
    conn: Connection,
    url_queue_id: int | None,
    url_id: int,
    source_url_queue_item_id: int | None = None,
) -> int | None:
    if not url_queue_id:
        return None

    target = conn.execute(
        "SELECT review_status, crawl_status FROM urls WHERE id = ?",
        (url_id,),
    ).fetchone()
    if (
        not target
        or target["review_status"] != "pending_review"
        or target["crawl_status"] in TERMINAL_CRAWL_STATUSES
    ):
        return None

    existing_item = conn.execute(
        """
        SELECT id
        FROM url_queue_items
        WHERE queue_id = ? AND url_id = ?
        """,
        (url_queue_id, url_id),
    ).fetchone()
    if existing_item:
        return None

    return enqueue_url_to_queue(
        conn,
        url_queue_id=url_queue_id,
        url_id=url_id,
        source_queue_id=url_queue_id,
        source_url_queue_item_id=source_url_queue_item_id,
    )


def env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def process_crawl_url(
    conn: Connection,
    url_id: int,
    queue_id: int | None = None,
    url_queue_item_id: int | None = None,
    job_id: int | None = None,
    run_token: str | None = None,
) -> None:
    ensure_job_lease(conn, job_id, run_token)
    target = conn.execute("SELECT * FROM urls WHERE id = ?", (url_id,)).fetchone()
    if not target:
        raise ValueError(f"URL not found: {url_id}")
    if target["review_status"] != "approved":
        if url_queue_item_id:
            conn.execute(
                "UPDATE url_queue_items SET status = 'pending_review' WHERE id = ?",
                (url_queue_item_id,),
            )
        raise PausedJob(f"URL is waiting for review approval: {target['url_norm']}")
    if target["crawl_status"] in TERMINAL_CRAWL_STATUSES:
        return

    conn.execute(
        """
        UPDATE urls
        SET crawl_status = 'crawling',
            crawl_error = NULL
        WHERE id = ?
        """,
        (url_id,),
    )
    conn.commit()
    browser = BrowserClient()
    strategy = classify_crawl_strategy(target["url_norm"])
    try:
        if strategy == "http_text":
            result = browser.fetch_text_resource(target["url_norm"])
        elif strategy == "http_binary_metadata":
            result = browser.fetch_binary_metadata(target["url_norm"])
        else:
            try:
                result = browser.fetch_url(target["url_norm"])
            except Exception as exc:
                if not env_bool("CRAWL_CLOAK_HTTP_FALLBACK", True):
                    raise
                result = browser.fetch_text_resource(
                    target["url_norm"],
                    fetch_method="http_text_fallback",
                )
                result.error = f"CloakBrowser failed; HTTP text fallback used: {exc}"
    except Exception:
        error_text = traceback.format_exc(limit=5)
        ensure_job_lease(conn, job_id, run_token)
        conn.execute(
            """
            UPDATE urls
            SET crawl_status = 'failed',
                crawl_error = ?
            WHERE id = ?
            """,
            (error_text, url_id),
        )
        raise
    ensure_job_lease(conn, job_id, run_token)
    next_crawl_status = "crawled" if result.is_text else "metadata_only"
    conn.execute(
        """
        UPDATE urls
        SET crawl_status = ?,
            final_url = ?,
            status_code = ?,
            content_type = ?,
            content_length = ?,
            fetch_method = ?,
            crawl_error = ?,
            crawled_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (
            next_crawl_status,
            result.final_url,
            result.status_code,
            result.content_type,
            result.content_length,
            result.fetch_method,
            result.error,
            url_id,
        ),
    )
    if result.is_text:
        conn.execute(
            """
            INSERT INTO url_bodies(url_id, html, updated_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(url_id) DO UPDATE SET
                html = excluded.html,
                updated_at = CURRENT_TIMESTAMP
            """,
            (url_id, result.html),
        )
    else:
        conn.execute("DELETE FROM url_bodies WHERE url_id = ?", (url_id,))

    if not result.is_text:
        return

    ensure_job_lease(conn, job_id, run_token)
    rules = conn.execute(
        "SELECT * FROM extraction_rules WHERE enabled = 1 ORDER BY priority, id"
    ).fetchall()
    extraction_input = {
        "text": result.text,
        "html": result.html,
        "links": result.links,
        "final_url": result.final_url,
        "redirects": result.redirects,
        "all": "\n".join([result.final_url, "\n".join(result.links), result.text, result.html]),
    }
    iocs = extract_iocs_by_rules(extraction_input, rules)

    for ioc in iocs:
        ensure_job_lease(conn, job_id, run_token)
        ioc_id = upsert_ioc(conn, ioc.type, ioc.raw, ioc.norm)
        if not ioc_id:
            continue
        upsert_ioc_source(
            conn,
            ioc_id,
            url_id,
            "crawl",
            extraction_rule_id=ioc.rule_id,
            evidence_text=ioc.evidence,
        )

        if ioc.type == "url":
            discovered_url = normalize_url(ioc.norm)
            if discovered_url:
                discovered_domain = get_domain(discovered_url)
                if discovered_domain:
                    if is_media_asset_url(discovered_url) or is_url_whitelisted(conn, discovered_url):
                        continue
                    discovered_url_id = upsert_url(
                        conn, discovered_url, discovered_url, discovered_domain, "extracted_from_crawl"
                    )
                    upsert_url_source(
                        conn,
                        url_id=discovered_url_id,
                        source_type="extracted_from_crawl",
                        dedupe_key=f"crawl:{queue_id or 'global'}:{url_id}:{discovered_url}",
                        queue_id=queue_id,
                        source_url_id=url_id,
                    )
                    enqueue_discovered_url_if_new(
                        conn,
                        url_queue_id=queue_id,
                        url_id=discovered_url_id,
                        source_url_queue_item_id=url_queue_item_id,
                    )

        if ioc.type == "domain":
            discovered_domain = normalize_domain(ioc.norm)
            if discovered_domain:
                discovered_url = normalize_url(f"https://{discovered_domain}/")
                if discovered_url:
                    if is_media_asset_url(discovered_url) or is_url_whitelisted(conn, discovered_url):
                        continue
                    discovered_url_id = upsert_url(
                        conn, discovered_url, discovered_url, discovered_domain, "extracted_from_crawl"
                    )
                    upsert_url_source(
                        conn,
                        url_id=discovered_url_id,
                        source_type="extracted_from_crawl",
                        dedupe_key=f"crawl-domain:{queue_id or 'global'}:{url_id}:{discovered_url}",
                        queue_id=queue_id,
                        source_url_id=url_id,
                    )
                    enqueue_discovered_url_if_new(
                        conn,
                        url_queue_id=queue_id,
                        url_id=discovered_url_id,
                        source_url_queue_item_id=url_queue_item_id,
                    )


def normalize_url_title(title: str | None, url_norm: str) -> str | None:
    text = " ".join((title or "").split())[:300]
    if not text or text == url_norm:
        return None
    return text


def upsert_url(
    conn: Connection,
    url_raw: str,
    url_norm: str,
    domain: str,
    first_source: str,
    title: str | None = None,
) -> int:
    title_norm = normalize_url_title(title, url_norm)
    conn.execute(
        """
        INSERT OR IGNORE INTO urls(url_raw, url_norm, domain, title, first_source)
        VALUES (?, ?, ?, ?, ?)
        """,
        (url_raw, url_norm, domain, title_norm, first_source),
    )
    if title_norm:
        conn.execute(
            """
            UPDATE urls
            SET title = ?
            WHERE url_norm = ?
              AND (title IS NULL OR title = '')
            """,
            (title_norm, url_norm),
        )
    row = conn.execute("SELECT id FROM urls WHERE url_norm = ?", (url_norm,)).fetchone()
    return int(row["id"])


def get_bound_output_url_queue(conn: Connection, keyword_queue_id: int):
    return conn.execute(
        """
        SELECT uq.*
        FROM queue_routes qr
        JOIN queues uq ON uq.id = qr.url_queue_id
        WHERE qr.keyword_queue_id = ?
          AND uq.queue_type = 'url_crawl'
        """,
        (keyword_queue_id,),
    ).fetchone()


def enqueue_url_to_queue(
    conn: Connection,
    url_queue_id: int,
    url_id: int,
    source_queue_id: int | None = None,
    source_search_query_id: int | None = None,
    source_search_queue_item_id: int | None = None,
    source_url_queue_item_id: int | None = None,
) -> int | None:
    queue = conn.execute(
        "SELECT * FROM queues WHERE id = ? AND queue_type = 'url_crawl'", (url_queue_id,)
    ).fetchone()
    target = conn.execute("SELECT * FROM urls WHERE id = ?", (url_id,)).fetchone()
    if not queue or not target:
        return None
    if is_media_asset_url(target["url_norm"]):
        return None
    if url_matches_current_or_latest_whitelist(conn, int(target["id"]), target["url_norm"]):
        return None
    if target["crawl_status"] in TERMINAL_CRAWL_STATUSES:
        remove_url_from_queue_items(conn, url_id)
        return None
    if target["review_status"] != "pending_review":
        return None

    initial_status = "pending_review"
    job_status = "paused"

    if url_matches_current_or_latest_whitelist(conn, int(target["id"]), target["url_norm"]):
        return None

    conn.execute(
        """
        INSERT OR IGNORE INTO url_queue_items(
          queue_id, url_id, source_queue_id, source_search_query_id,
          source_search_queue_item_id, source_url_queue_item_id, status
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            url_queue_id,
            url_id,
            source_queue_id,
            source_search_query_id,
            source_search_queue_item_id,
            source_url_queue_item_id,
            initial_status,
        ),
    )
    row = conn.execute(
        """
        SELECT id, status
        FROM url_queue_items
        WHERE queue_id = ? AND url_id = ?
        """,
        (url_queue_id, url_id),
    ).fetchone()
    if not row:
        return None
    if url_matches_current_or_latest_whitelist(conn, int(target["id"]), target["url_norm"]):
        remove_url_from_queue_items(conn, url_id)
        return None

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
            source_queue_id,
            source_search_query_id,
            source_search_queue_item_id,
            source_url_queue_item_id,
            row["id"],
        ),
    )
    enqueue_job(
        conn,
        "crawl_url",
        {"url_id": url_id, "url_queue_item_id": int(row["id"])},
        f"queue:{url_queue_id}:crawl:{url_id}",
        queue_id=url_queue_id,
        initial_status=job_status,
    )
    return int(row["id"])


def url_matches_current_or_latest_whitelist(conn: Connection, url_id: int, url_norm: str) -> bool:
    whitelisted = is_url_whitelisted(conn, url_norm) or is_url_whitelisted_latest(url_norm)
    if whitelisted:
        conn.execute(
            """
            UPDATE urls
            SET review_status = 'ignored_whitelist'
            WHERE id = ?
              AND review_status = 'pending_review'
            """,
            (url_id,),
        )
    return whitelisted


def upsert_url_source(conn: Connection, **kwargs: object) -> None:
    conn.execute(
        """
        INSERT OR IGNORE INTO url_sources(
          url_id, source_type, dedupe_key, queue_id, keyword_id, search_query_id,
          source_url_id, title, snippet, rank, page_no
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            kwargs.get("url_id"),
            kwargs.get("source_type"),
            kwargs.get("dedupe_key"),
            kwargs.get("queue_id"),
            kwargs.get("keyword_id"),
            kwargs.get("search_query_id"),
            kwargs.get("source_url_id"),
            kwargs.get("title"),
            kwargs.get("snippet"),
            kwargs.get("rank"),
            kwargs.get("page_no"),
        ),
    )


def upsert_ioc(conn: Connection, ioc_type: str, value_raw: str, value_norm: str) -> int | None:
    return upsert_ioc_record(conn, ioc_type, value_raw, value_norm)


def remove_url_from_queue_items(conn: Connection, url_id: int) -> tuple[int, int]:
    item_rows = conn.execute(
        """
        SELECT id, queue_id, status
        FROM url_queue_items
        WHERE url_id = ?
          AND status != 'running'
        """,
        (url_id,),
    ).fetchall()
    item_ids = [int(row["id"]) for row in item_rows]
    affected_queue_ids = {int(row["queue_id"]) for row in item_rows}
    job_count = 0
    for row in item_rows:
        cursor = conn.execute(
            """
            DELETE FROM jobs
            WHERE type = 'crawl_url'
              AND dedupe_key = ?
              AND status != 'running'
            """,
            (f"queue:{row['queue_id']}:crawl:{url_id}",),
        )
        job_count += max(cursor.rowcount, 0)
    if item_ids:
        placeholders = ",".join("?" for _ in item_ids)
        conn.execute(
            f"""
            UPDATE url_queue_items
            SET source_url_queue_item_id = NULL
            WHERE source_url_queue_item_id IN ({placeholders})
            """,
            item_ids,
        )
        conn.execute(
            f"DELETE FROM url_queue_items WHERE id IN ({placeholders})",
            item_ids,
        )
    for queue_id in affected_queue_ids:
        refresh_queue_status(conn, queue_id)
    return len(item_ids), job_count


def approve_url_and_enqueue(conn: Connection, url_id: int, queue_id: int | None = None) -> bool:
    target = conn.execute("SELECT * FROM urls WHERE id = ?", (url_id,)).fetchone()
    if not target:
        return False
    if target["crawl_status"] in TERMINAL_CRAWL_STATUSES:
        remove_url_from_queue_items(conn, url_id)
        return False
    whitelisted = is_url_whitelisted(conn, target["url_norm"])
    if is_media_asset_url(target["url_norm"]) or whitelisted:
        conn.execute(
            """
            UPDATE urls
            SET review_status = CASE
                WHEN ? = 1 THEN 'ignored_whitelist'
                ELSE 'ignored_media'
            END
            WHERE id = ?
            """,
            (1 if whitelisted else 0, url_id),
        )
        remove_url_from_queue_items(conn, url_id)
        return False
    if target["review_status"] != "pending_review":
        return False
    conn.execute(
        "UPDATE urls SET review_status = 'approved' WHERE id = ? AND review_status = 'pending_review'",
        (url_id,),
    )
    record_keyword_search_url_ioc(conn, url_id)
    queue_rows = []
    if queue_id:
        queue_rows = conn.execute(
            "SELECT * FROM url_queue_items WHERE queue_id = ? AND url_id = ?",
            (queue_id, url_id),
        ).fetchall()
    else:
        queue_rows = conn.execute(
            "SELECT * FROM url_queue_items WHERE url_id = ?", (url_id,)
        ).fetchall()

    if queue_rows:
        for item in queue_rows:
            queue = conn.execute("SELECT status FROM queues WHERE id = ?", (item["queue_id"],)).fetchone()
            next_status = "pending" if queue and queue["status"] == "running" else "paused"
            conn.execute(
                """
                UPDATE url_queue_items
                SET status = ?,
                    error = NULL
                WHERE id = ?
                """,
                (next_status, item["id"]),
            )
            enqueue_job(
                conn,
                "crawl_url",
                {"url_id": url_id, "url_queue_item_id": int(item["id"])},
                f"queue:{item['queue_id']}:crawl:{url_id}",
                queue_id=int(item["queue_id"]),
                initial_status=next_status,
            )
        return True

    if queue_id:
        enqueue_url_to_queue(conn, queue_id, url_id)
        return True

    if target["crawl_status"] not in TERMINAL_CRAWL_STATUSES:
        enqueue_job(conn, "crawl_url", {"url_id": url_id}, f"crawl:{url_id}")
    return True


def reject_url(conn: Connection, url_id: int) -> bool:
    changed = (
        conn.execute(
            """
            UPDATE urls
            SET review_status = 'rejected'
            WHERE id = ?
              AND review_status = 'pending_review'
            """,
            (url_id,),
        ).rowcount
        > 0
    )
    if changed:
        remove_url_from_queue_items(conn, url_id)
    return changed


def refresh_keyword_status(conn: Connection, keyword_id: int) -> None:
    keyword = conn.execute(
        "SELECT status, COALESCE(paused_by_user, 0) AS paused_by_user FROM keywords WHERE id = ?",
        (keyword_id,),
    ).fetchone()
    if not keyword or keyword["paused_by_user"]:
        return

    counts = conn.execute(
        """
        SELECT
          COUNT(*) AS total,
          SUM(CASE WHEN status = 'running' THEN 1 ELSE 0 END) AS running_count,
          SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END) AS pending_count,
          SUM(CASE WHEN status = 'paused' THEN 1 ELSE 0 END) AS paused_count,
          SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed_count,
          SUM(CASE WHEN status = 'done' THEN 1 ELSE 0 END) AS done_count
        FROM search_queries
        WHERE keyword_id = ?
        """,
        (keyword_id,),
    ).fetchone()
    if not counts or not counts["total"]:
        status = "pending"
    elif counts["running_count"]:
        status = "running"
    elif counts["pending_count"]:
        status = "pending"
    elif counts["paused_count"] == counts["total"]:
        status = "paused"
    elif counts["failed_count"]:
        status = "failed"
    else:
        status = "done"

    conn.execute("UPDATE keywords SET status = ? WHERE id = ?", (status, keyword_id))


def is_queue_running(conn: Connection, queue_id: int) -> bool:
    row = conn.execute("SELECT status FROM queues WHERE id = ?", (queue_id,)).fetchone()
    return bool(row and row["status"] == "running")


def mark_url_queue_item(
    conn: Connection, item_id: object, status: str, error: str | None = None
) -> None:
    if not item_id:
        return
    try:
        item_id_int = int(item_id)
    except (TypeError, ValueError):
        return

    if status == "running":
        conn.execute(
            """
            UPDATE url_queue_items
            SET status = 'running',
                started_at = CURRENT_TIMESTAMP,
                finished_at = NULL,
                error = NULL
            WHERE id = ?
            """,
            (item_id_int,),
        )
        return

    conn.execute(
        """
        UPDATE url_queue_items
        SET status = ?,
            finished_at = CURRENT_TIMESTAMP,
            error = ?
        WHERE id = ?
        """,
        (status, error, item_id_int),
    )


def mark_url_queue_item_if_not_pending_review(
    conn: Connection, item_id: object, status: str, error: str | None = None
) -> None:
    if not item_id:
        return
    try:
        item_id_int = int(item_id)
    except (TypeError, ValueError):
        return
    row = conn.execute("SELECT status FROM url_queue_items WHERE id = ?", (item_id_int,)).fetchone()
    if row and row["status"] == "pending_review":
        return
    mark_url_queue_item(conn, item_id_int, status, error)


def mark_search_queue_item(
    conn: Connection, item_id: object, status: str, error: str | None = None
) -> None:
    if not item_id:
        return
    try:
        item_id_int = int(item_id)
    except (TypeError, ValueError):
        return

    if status == "running":
        conn.execute(
            """
            UPDATE search_queue_items
            SET status = 'running',
                started_at = CURRENT_TIMESTAMP,
                finished_at = NULL,
                error = NULL
            WHERE id = ?
            """,
            (item_id_int,),
        )
        return

    if status == "done":
        row = conn.execute(
            """
            SELECT sq.result_count, sq.page_count
            FROM search_queue_items sqi
            JOIN search_queries sq ON sq.id = sqi.search_query_id
            WHERE sqi.id = ?
            """,
            (item_id_int,),
        ).fetchone()
        result_count = int(row["result_count"]) if row else 0
        page_count = int(row["page_count"]) if row else 0
        conn.execute(
            """
            UPDATE search_queue_items
            SET status = 'done',
                result_count = ?,
                page_count = ?,
                finished_at = CURRENT_TIMESTAMP,
                error = NULL
            WHERE id = ?
            """,
            (result_count, page_count, item_id_int),
        )
        return

    conn.execute(
        """
        UPDATE search_queue_items
        SET status = ?,
            finished_at = CURRENT_TIMESTAMP,
            error = ?
        WHERE id = ?
        """,
        (status, error, item_id_int),
    )


def refresh_queue_status(conn: Connection, queue_id: int) -> None:
    queue = conn.execute("SELECT status, started_at FROM queues WHERE id = ?", (queue_id,)).fetchone()
    if not queue:
        return

    counts = conn.execute(
        """
        SELECT
          COUNT(*) AS total,
          SUM(CASE WHEN status = 'running' THEN 1 ELSE 0 END) AS running_count,
          SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END) AS pending_count,
          SUM(CASE WHEN status = 'paused' THEN 1 ELSE 0 END) AS paused_count,
          SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed_count,
          SUM(CASE WHEN status = 'done' THEN 1 ELSE 0 END) AS done_count
        FROM jobs
        WHERE queue_id = ?
        """,
        (queue_id,),
    ).fetchone()

    running_count = int(counts["running_count"] or 0) if counts else 0
    pending_count = int(counts["pending_count"] or 0) if counts else 0
    paused_count = int(counts["paused_count"] or 0) if counts else 0
    failed_count = int(counts["failed_count"] or 0) if counts else 0
    done_count = int(counts["done_count"] or 0) if counts else 0

    if queue["status"] == "stopping":
        next_status = "stopping" if running_count else "paused"
    elif not counts or not counts["total"]:
        next_status = "draft"
    elif running_count or pending_count:
        next_status = "running"
    elif (
        paused_count
        and not failed_count
        and not done_count
        and not queue["started_at"]
    ):
        next_status = "draft"
    elif paused_count:
        next_status = "paused"
    elif failed_count:
        next_status = "failed"
    else:
        next_status = "done"

    conn.execute(
        """
        UPDATE queues
        SET status = ?,
            updated_at = CURRENT_TIMESTAMP,
            stopped_at = CASE WHEN ? IN ('paused', 'done', 'failed') THEN COALESCE(stopped_at, CURRENT_TIMESTAMP) ELSE stopped_at END
        WHERE id = ?
        """,
        (next_status, next_status, queue_id),
    )
