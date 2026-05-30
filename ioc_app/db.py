import os
import re
import sqlite3
from pathlib import Path

from .normalizers import get_domain, normalize_by_rule, normalize_url


ROOT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_DB_PATH = ROOT_DIR / "data" / "ioc_investigator.sqlite3"


def db_path() -> Path:
    return Path(os.environ.get("IOC_DB_PATH", DEFAULT_DB_PATH))


def connect() -> sqlite3.Connection:
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def is_url_whitelisted(conn: sqlite3.Connection, url_norm: str) -> bool:
    return bool(
        conn.execute(
            """
            SELECT 1
            FROM whitelist_urls
            WHERE url_norm = ?
              AND enabled = 1
            LIMIT 1
            """,
            (url_norm,),
        ).fetchone()
    )


def init_db() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)
        migrate_db(conn)
        recover_interrupted_jobs(conn)
        seed_defaults(conn)


def migrate_db(conn: sqlite3.Connection) -> None:
    ensure_column(conn, "search_queries", "queue_id", "INTEGER REFERENCES queues(id)")
    ensure_column(conn, "search_queries", "result_count", "INTEGER NOT NULL DEFAULT 0")
    ensure_column(conn, "search_queries", "page_count", "INTEGER NOT NULL DEFAULT 0")
    ensure_column(conn, "search_queries", "last_error", "TEXT")
    ensure_column(conn, "search_queries", "started_at", "TEXT")
    ensure_column(conn, "search_queries", "finished_at", "TEXT")
    ensure_column(conn, "jobs", "queue_id", "INTEGER REFERENCES queues(id)")
    ensure_column(conn, "search_queue_items", "output_url_queue_id", "INTEGER REFERENCES queues(id)")
    ensure_column(conn, "url_sources", "queue_id", "INTEGER REFERENCES queues(id)")
    ensure_column(conn, "url_queue_items", "source_queue_id", "INTEGER REFERENCES queues(id)")
    ensure_column(conn, "url_queue_items", "source_search_query_id", "INTEGER REFERENCES search_queries(id)")
    ensure_column(conn, "url_queue_items", "source_search_queue_item_id", "INTEGER REFERENCES search_queue_items(id)")
    ensure_column(conn, "url_queue_items", "source_url_queue_item_id", "INTEGER REFERENCES url_queue_items(id)")
    ensure_column(conn, "urls", "content_type", "TEXT")
    ensure_column(conn, "urls", "content_length", "INTEGER")
    ensure_column(conn, "urls", "fetch_method", "TEXT")
    ensure_column(conn, "urls", "crawl_error", "TEXT")
    migrate_domain_tables_into_urls(conn)
    remove_crawled_urls_from_queue_items(conn)
    conn.executescript(
        """
        CREATE INDEX IF NOT EXISTS idx_jobs_queue ON jobs(queue_id, status, id);
        CREATE INDEX IF NOT EXISTS idx_search_queries_queue ON search_queries(queue_id, status);
        CREATE INDEX IF NOT EXISTS idx_search_queue_items_queue ON search_queue_items(queue_id, status);
        CREATE INDEX IF NOT EXISTS idx_search_queue_items_output ON search_queue_items(output_url_queue_id);
        CREATE INDEX IF NOT EXISTS idx_url_queue_items_queue ON url_queue_items(queue_id, status);
        CREATE INDEX IF NOT EXISTS idx_queue_routes_keyword ON queue_routes(keyword_queue_id);
        CREATE INDEX IF NOT EXISTS idx_queue_routes_url ON queue_routes(url_queue_id);
        CREATE INDEX IF NOT EXISTS idx_whitelist_urls_enabled ON whitelist_urls(enabled, url_norm);
        """
    )


def table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return bool(
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,),
        ).fetchone()
    )


def ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    if not table_exists(conn, table):
        return
    existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


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
            INSERT OR IGNORE INTO urls(url_raw, url_norm, domain, first_source, review_status)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
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
            """,
            (review_status, url_norm),
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
            url = conn.execute("SELECT id FROM urls WHERE url_norm = ?", (domain_url,)).fetchone()
            if not url:
                continue
            conn.execute(
                """
                INSERT OR IGNORE INTO url_sources(
                  url_id, source_type, dedupe_key, queue_id, keyword_id, search_query_id,
                  source_url_id, title, snippet, rank, page_no
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?)
                """,
                (
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


def recover_interrupted_jobs(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        UPDATE jobs
        SET status = 'pending',
            started_at = NULL,
            error = COALESCE(error, 'Recovered after interrupted process.')
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
                    "text",
                    r"@(example|test)\.",
                    "email",
                    10,
                    "General email extraction from rendered text.",
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
                    r"(?<!\d)(?:\+?84|0)[\s.\-]?(?:3|5|7|8|9)(?:[\s.\-]?\d){8}(?!\d)",
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
            "text",
            r"@(example|test|invalid|localhost)\.",
            "email",
            "Strict email extraction with validated domain/TLD.",
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
            r"(?<![\d+])(?:\+84|84|0)[\s.\-]?(?:3|5|7|8|9)(?:[\s.\-]?\d){8}(?!\d)",
            "",
            0,
            "text",
            None,
            "phone_vn",
            "Vietnam mobile phone extraction for 0xxx, +84xxx, and spaced/dotted forms.",
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
        SELECT id, type, value_norm
        FROM iocs
        WHERE type IN ('domain', 'email', 'phone', 'url', 'address')
        """
    ).fetchall()
    for row in rows:
        normalized = normalize_by_rule(row["value_norm"], row["type"], row["type"])
        if row["type"] == "domain" and domain_ioc_is_email_localpart(conn, int(row["id"]), row["value_norm"]):
            normalized = None
        if row["type"] == "domain" and domain_ioc_is_full_url_host(conn, int(row["id"]), row["value_norm"]):
            normalized = None
        if normalized == row["value_norm"]:
            continue
        if normalized:
            merge_or_update_ioc(conn, int(row["id"]), row["type"], normalized)
        else:
            conn.execute("DELETE FROM ioc_sources WHERE ioc_id = ?", (row["id"],))
            conn.execute("DELETE FROM iocs WHERE id = ?", (row["id"],))


def merge_or_update_ioc(conn: sqlite3.Connection, ioc_id: int, ioc_type: str, normalized: str) -> None:
    existing = conn.execute(
        "SELECT id FROM iocs WHERE type = ? AND value_norm = ?",
        (ioc_type, normalized),
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

    sources = conn.execute(
        """
        SELECT source_url_id, extraction_rule_id, evidence_text
        FROM ioc_sources
        WHERE ioc_id = ?
        """,
        (ioc_id,),
    ).fetchall()
    for source in sources:
        conn.execute(
            """
            INSERT OR IGNORE INTO ioc_sources(
              ioc_id, source_url_id, extraction_rule_id, evidence_text
            )
            VALUES (?, ?, ?, ?)
            """,
            (
                existing_id,
                source["source_url_id"],
                source["extraction_rule_id"],
                source["evidence_text"],
            ),
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
CREATE TABLE IF NOT EXISTS keywords (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  text TEXT NOT NULL UNIQUE,
  status TEXT NOT NULL DEFAULT 'pending',
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS queues (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  queue_type TEXT NOT NULL CHECK(queue_type IN ('keyword_search', 'url_crawl')),
  status TEXT NOT NULL DEFAULT 'draft',
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  started_at TEXT,
  stopped_at TEXT,
  UNIQUE (name, queue_type)
);

CREATE TABLE IF NOT EXISTS queue_routes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
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
  queue_id INTEGER REFERENCES queues(id),
  type TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  payload TEXT NOT NULL,
  dedupe_key TEXT UNIQUE,
  attempts INTEGER NOT NULL DEFAULT 0,
  error TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  started_at TEXT,
  finished_at TEXT
);

CREATE TABLE IF NOT EXISTS search_queue_items (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
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
  url_raw TEXT NOT NULL,
  url_norm TEXT NOT NULL UNIQUE,
  domain TEXT NOT NULL,
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
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS whitelist_urls (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  url_raw TEXT NOT NULL,
  url_norm TEXT NOT NULL UNIQUE,
  note TEXT,
  enabled INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS url_queue_items (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
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
  url_id INTEGER NOT NULL REFERENCES urls(id),
  source_type TEXT NOT NULL,
  dedupe_key TEXT NOT NULL UNIQUE,
  queue_id INTEGER REFERENCES queues(id),
  keyword_id INTEGER REFERENCES keywords(id),
  search_query_id INTEGER REFERENCES search_queries(id),
  source_url_id INTEGER REFERENCES urls(id),
  title TEXT,
  snippet TEXT,
  rank INTEGER,
  page_no INTEGER,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
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
  type TEXT NOT NULL,
  value_raw TEXT NOT NULL,
  value_norm TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE (type, value_norm)
);

CREATE TABLE IF NOT EXISTS ioc_sources (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ioc_id INTEGER NOT NULL REFERENCES iocs(id),
  source_url_id INTEGER NOT NULL REFERENCES urls(id),
  extraction_rule_id INTEGER REFERENCES extraction_rules(id),
  evidence_text TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE (ioc_id, source_url_id, extraction_rule_id)
);

CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status, id);
CREATE INDEX IF NOT EXISTS idx_urls_review ON urls(review_status);
CREATE INDEX IF NOT EXISTS idx_urls_crawl ON urls(crawl_status);
CREATE INDEX IF NOT EXISTS idx_urls_domain ON urls(domain);
CREATE INDEX IF NOT EXISTS idx_whitelist_urls_norm ON whitelist_urls(url_norm);
CREATE INDEX IF NOT EXISTS idx_iocs_type ON iocs(type);
"""
