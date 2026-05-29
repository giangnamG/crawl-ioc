from __future__ import annotations

import json
import traceback
from sqlite3 import Connection

from .browser import BrowserClient
from .db import connect
from .extractor import extract_iocs_by_rules
from .normalizers import get_domain, normalize_domain, normalize_url


class PausedJob(Exception):
    """Raised when a queued job has been paused before execution."""


def enqueue_job(conn: Connection, job_type: str, payload: dict[str, object], dedupe_key: str) -> None:
    payload_json = json.dumps(payload)
    existing = conn.execute(
        "SELECT id, status FROM jobs WHERE dedupe_key = ?", (dedupe_key,)
    ).fetchone()
    if existing:
        if existing["status"] in {"pending", "running"}:
            return
        conn.execute(
            """
            UPDATE jobs
            SET status = 'pending',
                payload = ?,
                attempts = 0,
                error = NULL,
                started_at = NULL,
                finished_at = NULL
            WHERE id = ?
            """,
            (payload_json, existing["id"]),
        )
        return

    conn.execute(
        """
        INSERT OR IGNORE INTO jobs(type, status, payload, dedupe_key)
        VALUES (?, 'pending', ?, ?)
        """,
        (job_type, payload_json, dedupe_key),
    )


def run_one() -> str:
    with connect() as conn:
        job = conn.execute(
            "SELECT * FROM jobs WHERE status = 'pending' ORDER BY id LIMIT 1"
        ).fetchone()
        if not job:
            return "No pending job."

        conn.execute(
            "UPDATE jobs SET status = 'running', attempts = attempts + 1, started_at = CURRENT_TIMESTAMP WHERE id = ?",
            (job["id"],),
        )

        try:
            payload = json.loads(job["payload"])
            if job["type"] == "search_query":
                process_search_query(conn, int(payload["search_query_id"]))
            elif job["type"] == "crawl_url":
                process_crawl_url(conn, int(payload["url_id"]))
            else:
                raise ValueError(f"Unsupported job type: {job['type']}")

            conn.execute(
                "UPDATE jobs SET status = 'done', finished_at = CURRENT_TIMESTAMP, error = NULL WHERE id = ?",
                (job["id"],),
            )
            return f"Job #{job['id']} completed."
        except PausedJob:
            conn.execute(
                """
                UPDATE jobs
                SET status = 'paused',
                    finished_at = CURRENT_TIMESTAMP,
                    error = NULL
                WHERE id = ?
                """,
                (job["id"],),
            )
            return f"Job #{job['id']} paused."
        except Exception as exc:
            conn.execute(
                "UPDATE jobs SET status = 'failed', finished_at = CURRENT_TIMESTAMP, error = ? WHERE id = ?",
                (traceback.format_exc(limit=5), job["id"]),
            )
            return f"Job #{job['id']} failed: {exc}"


def run_all(limit: int = 25) -> list[str]:
    messages = []
    for _ in range(limit):
        message = run_one()
        messages.append(message)
        if message == "No pending job.":
            break
    return messages


def process_search_query(conn: Connection, search_query_id: int) -> None:
    search_query = conn.execute(
        """
        SELECT sq.*, k.text AS keyword_text, d.template AS dork_template
        FROM search_queries sq
        JOIN keywords k ON k.id = sq.keyword_id
        JOIN search_dorks d ON d.id = sq.dork_id
        WHERE sq.id = ?
        """,
        (search_query_id,),
    ).fetchone()
    if not search_query:
        raise ValueError(f"Search query not found: {search_query_id}")
    if search_query["status"] == "paused":
        raise PausedJob(f"Search query paused: {search_query_id}")

    keyword = conn.execute(
        "SELECT status FROM keywords WHERE id = ?", (search_query["keyword_id"],)
    ).fetchone()
    if keyword and keyword["status"] == "paused":
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

            url_id = upsert_url(conn, item.url, url_norm, domain, "google_search")
            saved_urls += 1
            upsert_domain(conn, domain, "google_search")
            upsert_url_source(
                conn,
                url_id=url_id,
                source_type="google_search",
                dedupe_key=f"google:{search_query_id}:{item.page_no}:{item.rank}:{url_norm}",
                keyword_id=search_query["keyword_id"],
                search_query_id=search_query_id,
                title=item.title,
                snippet=item.snippet,
                rank=item.rank,
                page_no=item.page_no,
            )
            upsert_domain_source(
                conn,
                domain=domain,
                source_type="google_search",
                dedupe_key=f"domain:google:{search_query_id}:{item.page_no}:{item.rank}:{domain}",
                keyword_id=search_query["keyword_id"],
                search_query_id=search_query_id,
                discovered_url_id=url_id,
                rank=item.rank,
                page_no=item.page_no,
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
        raise


def process_crawl_url(conn: Connection, url_id: int) -> None:
    target = conn.execute("SELECT * FROM urls WHERE id = ?", (url_id,)).fetchone()
    if not target:
        raise ValueError(f"URL not found: {url_id}")
    if target["review_status"] != "approved":
        return
    if target["crawl_status"] == "crawled":
        return

    conn.execute("UPDATE urls SET crawl_status = 'crawling' WHERE id = ?", (url_id,))
    browser = BrowserClient()
    try:
        result = browser.fetch_url(target["url_norm"])
    except Exception:
        conn.execute("UPDATE urls SET crawl_status = 'failed' WHERE id = ?", (url_id,))
        raise
    conn.execute(
        """
        UPDATE urls
        SET crawl_status = 'crawled',
            final_url = ?,
            status_code = ?,
            html = ?,
            crawled_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (result.final_url, result.status_code, result.html, url_id),
    )

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
        ioc_id = upsert_ioc(conn, ioc.type, ioc.raw, ioc.norm)
        conn.execute(
            """
            INSERT OR IGNORE INTO ioc_sources(
              ioc_id, source_url_id, extraction_rule_id, evidence_text
            )
            VALUES (?, ?, ?, ?)
            """,
            (ioc_id, url_id, ioc.rule_id, ioc.evidence),
        )

        if ioc.type == "url":
            discovered_url = normalize_url(ioc.norm)
            if discovered_url:
                discovered_domain = get_domain(discovered_url)
                if discovered_domain:
                    discovered_url_id = upsert_url(
                        conn, ioc.raw, discovered_url, discovered_domain, "extracted_from_crawl"
                    )
                    upsert_domain(conn, discovered_domain, "extracted_from_crawl")
                    upsert_url_source(
                        conn,
                        url_id=discovered_url_id,
                        source_type="extracted_from_crawl",
                        dedupe_key=f"crawl:{url_id}:{discovered_url}",
                        source_url_id=url_id,
                    )
                    upsert_domain_source(
                        conn,
                        domain=discovered_domain,
                        source_type="extracted_from_crawl",
                        dedupe_key=f"domain:crawl:{url_id}:{discovered_domain}",
                        source_url_id=url_id,
                        discovered_url_id=discovered_url_id,
                    )

        if ioc.type == "domain":
            discovered_domain = normalize_domain(ioc.norm)
            if discovered_domain:
                upsert_domain(conn, discovered_domain, "extracted_from_crawl")
                discovered_url = normalize_url(f"https://{discovered_domain}/")
                discovered_url_id = None
                if discovered_url:
                    discovered_url_id = upsert_url(
                        conn, discovered_url, discovered_url, discovered_domain, "extracted_from_crawl"
                    )
                    upsert_url_source(
                        conn,
                        url_id=discovered_url_id,
                        source_type="extracted_from_crawl",
                        dedupe_key=f"crawl-domain:{url_id}:{discovered_url}",
                        source_url_id=url_id,
                    )
                upsert_domain_source(
                    conn,
                    domain=discovered_domain,
                    source_type="extracted_from_crawl",
                    dedupe_key=f"domain:crawl:{url_id}:{discovered_domain}",
                    source_url_id=url_id,
                    discovered_url_id=discovered_url_id,
                )


def upsert_url(conn: Connection, url_raw: str, url_norm: str, domain: str, first_source: str) -> int:
    conn.execute(
        """
        INSERT OR IGNORE INTO urls(url_raw, url_norm, domain, first_source)
        VALUES (?, ?, ?, ?)
        """,
        (url_raw, url_norm, domain, first_source),
    )
    row = conn.execute("SELECT id FROM urls WHERE url_norm = ?", (url_norm,)).fetchone()
    return int(row["id"])


def upsert_domain(conn: Connection, domain: str, first_source: str) -> int:
    conn.execute(
        """
        INSERT OR IGNORE INTO domains(domain, first_source)
        VALUES (?, ?)
        """,
        (domain, first_source),
    )
    row = conn.execute("SELECT id FROM domains WHERE domain = ?", (domain,)).fetchone()
    return int(row["id"])


def upsert_url_source(conn: Connection, **kwargs: object) -> None:
    conn.execute(
        """
        INSERT OR IGNORE INTO url_sources(
          url_id, source_type, dedupe_key, keyword_id, search_query_id,
          source_url_id, title, snippet, rank, page_no
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            kwargs.get("url_id"),
            kwargs.get("source_type"),
            kwargs.get("dedupe_key"),
            kwargs.get("keyword_id"),
            kwargs.get("search_query_id"),
            kwargs.get("source_url_id"),
            kwargs.get("title"),
            kwargs.get("snippet"),
            kwargs.get("rank"),
            kwargs.get("page_no"),
        ),
    )


def upsert_domain_source(conn: Connection, **kwargs: object) -> None:
    domain_id = upsert_domain(conn, str(kwargs["domain"]), str(kwargs.get("source_type") or "manual"))
    conn.execute(
        """
        INSERT OR IGNORE INTO domain_sources(
          domain_id, source_type, dedupe_key, keyword_id, search_query_id,
          source_url_id, discovered_url_id, rank, page_no
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            domain_id,
            kwargs.get("source_type"),
            kwargs.get("dedupe_key"),
            kwargs.get("keyword_id"),
            kwargs.get("search_query_id"),
            kwargs.get("source_url_id"),
            kwargs.get("discovered_url_id"),
            kwargs.get("rank"),
            kwargs.get("page_no"),
        ),
    )


def upsert_ioc(conn: Connection, ioc_type: str, value_raw: str, value_norm: str) -> int:
    conn.execute(
        """
        INSERT OR IGNORE INTO iocs(type, value_raw, value_norm)
        VALUES (?, ?, ?)
        """,
        (ioc_type, value_raw, value_norm),
    )
    row = conn.execute(
        "SELECT id FROM iocs WHERE type = ? AND value_norm = ?", (ioc_type, value_norm)
    ).fetchone()
    return int(row["id"])


def approve_url_and_enqueue(conn: Connection, url_id: int) -> None:
    target = conn.execute("SELECT * FROM urls WHERE id = ?", (url_id,)).fetchone()
    if not target:
        return
    conn.execute("UPDATE urls SET review_status = 'approved' WHERE id = ?", (url_id,))
    conn.execute(
        "UPDATE domains SET review_status = 'approved' WHERE domain = ? AND review_status != 'rejected'",
        (target["domain"],),
    )
    pending = conn.execute(
        """
        SELECT 1 FROM jobs
        WHERE type = 'crawl_url'
          AND dedupe_key = ?
          AND status IN ('pending', 'running')
        """,
        (f"crawl:{url_id}",),
    ).fetchone()
    if target["crawl_status"] != "crawled" and not pending:
        enqueue_job(conn, "crawl_url", {"url_id": url_id}, f"crawl:{url_id}")


def reject_url(conn: Connection, url_id: int) -> None:
    conn.execute("UPDATE urls SET review_status = 'rejected' WHERE id = ?", (url_id,))


def reject_domain(conn: Connection, domain: str) -> None:
    conn.execute("UPDATE domains SET review_status = 'rejected' WHERE domain = ?", (domain,))
    conn.execute("UPDATE urls SET review_status = 'rejected' WHERE domain = ?", (domain,))


def refresh_keyword_status(conn: Connection, keyword_id: int) -> None:
    keyword = conn.execute("SELECT status FROM keywords WHERE id = ?", (keyword_id,)).fetchone()
    if not keyword or keyword["status"] == "paused":
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
