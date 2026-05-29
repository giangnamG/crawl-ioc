import os
import sqlite3
from pathlib import Path


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


def init_db() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)
        migrate_db(conn)
        recover_interrupted_jobs(conn)
        seed_defaults(conn)


def migrate_db(conn: sqlite3.Connection) -> None:
    ensure_column(conn, "search_queries", "result_count", "INTEGER NOT NULL DEFAULT 0")
    ensure_column(conn, "search_queries", "page_count", "INTEGER NOT NULL DEFAULT 0")
    ensure_column(conn, "search_queries", "last_error", "TEXT")
    ensure_column(conn, "search_queries", "started_at", "TEXT")
    ensure_column(conn, "search_queries", "finished_at", "TEXT")


def ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


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

    conn.execute(
        """
        UPDATE extraction_rules
        SET pattern = ?,
            normalizer = 'phone_vn',
            input_scope = 'text',
            updated_at = CURRENT_TIMESTAMP
        WHERE builtin = 1 AND name = 'Vietnam mobile phone'
        """,
        (r"(?<!\d)(?:\+?84|0)[\s.\-]?(?:3|5|7|8|9)(?:[\s.\-]?\d){8}(?!\d)",),
    )
    conn.execute(
        """
        UPDATE extraction_rules
        SET exclude_pattern = ?,
            updated_at = CURRENT_TIMESTAMP
        WHERE builtin = 1 AND name = 'Domain basic'
        """,
        (r"^(example\.com|localhost|.*\.(?:php|html?|aspx?|jsp|css|js|png|jpe?g|gif|svg|webp|json|xml|txt|pdf))$",),
    )


SCHEMA = """
CREATE TABLE IF NOT EXISTS keywords (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  text TEXT NOT NULL UNIQUE,
  status TEXT NOT NULL DEFAULT 'pending',
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
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
  UNIQUE (keyword_id, dork_id, query_text)
);

CREATE TABLE IF NOT EXISTS jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
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
  html TEXT,
  crawled_at TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS domains (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  domain TEXT NOT NULL UNIQUE,
  first_source TEXT NOT NULL,
  review_status TEXT NOT NULL DEFAULT 'pending_review',
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS domain_sources (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  domain_id INTEGER NOT NULL REFERENCES domains(id),
  source_type TEXT NOT NULL,
  dedupe_key TEXT NOT NULL UNIQUE,
  keyword_id INTEGER REFERENCES keywords(id),
  search_query_id INTEGER REFERENCES search_queries(id),
  source_url_id INTEGER REFERENCES urls(id),
  discovered_url_id INTEGER REFERENCES urls(id),
  rank INTEGER,
  page_no INTEGER,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS url_sources (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  url_id INTEGER NOT NULL REFERENCES urls(id),
  source_type TEXT NOT NULL,
  dedupe_key TEXT NOT NULL UNIQUE,
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
CREATE INDEX IF NOT EXISTS idx_iocs_type ON iocs(type);
"""
