from __future__ import annotations

import json
import os
import threading
import time

from flask import Flask, flash, redirect, render_template, request, url_for

from .db import connect, db_path, init_db
from .extractor import test_rule, validate_rule
from .normalizers import get_domain, normalize_url
from .worker import (
    approve_url_and_enqueue,
    enqueue_job,
    refresh_keyword_status,
    refresh_queue_status,
    reject_domain,
    reject_url,
    run_all,
    run_one,
    upsert_domain,
    upsert_url,
)


def create_app(start_worker: bool = False) -> Flask:
    app = Flask(__name__)
    app.config["SECRET_KEY"] = "local-dev-secret"
    app.config["AUTO_WORKER_ENABLED"] = start_worker
    init_db()

    @app.context_processor
    def inject_globals() -> dict[str, object]:
        return {
            "auto_worker_enabled": app.config.get("AUTO_WORKER_ENABLED", False),
            "db_path": str(db_path()),
            "nav": NAV,
        }

    @app.template_filter("status_label")
    def status_label(value: str | None) -> str:
        return (value or "").replace("_", " ").title()

    @app.template_filter("queue_type_label")
    def queue_type_label(value: str | None) -> str:
        labels = {
            "keyword_search": "Keyword Search",
            "url_crawl": "URL Crawl",
        }
        return labels.get(value or "", value or "")

    @app.template_filter("truncate_middle")
    def truncate_middle(value: str | None, size: int = 80) -> str:
        text = value or ""
        if len(text) <= size:
            return text
        side = max(8, (size - 3) // 2)
        return f"{text[:side]}...{text[-side:]}"

    @app.get("/")
    def dashboard():
        with connect() as conn:
            counts = {
                "queues": scalar(conn, "SELECT COUNT(*) FROM queues"),
                "keywords": scalar(conn, "SELECT COUNT(*) FROM keywords"),
                "search_queries": scalar(conn, "SELECT COUNT(*) FROM search_queries"),
                "pending_jobs": scalar(conn, "SELECT COUNT(*) FROM jobs WHERE status = 'pending'"),
                "pending_review": scalar(
                    conn, "SELECT COUNT(*) FROM urls WHERE review_status = 'pending_review'"
                ),
                "approved_urls": scalar(conn, "SELECT COUNT(*) FROM urls WHERE review_status = 'approved'"),
                "crawled_urls": scalar(conn, "SELECT COUNT(*) FROM urls WHERE crawl_status = 'crawled'"),
                "iocs": scalar(conn, "SELECT COUNT(*) FROM iocs"),
                "domains": scalar(conn, "SELECT COUNT(*) FROM domains"),
            }
            recent_jobs = conn.execute(
                "SELECT * FROM jobs ORDER BY id DESC LIMIT 8"
            ).fetchall()
            recent_iocs = conn.execute(
                "SELECT * FROM iocs ORDER BY id DESC LIMIT 8"
            ).fetchall()
        return render_template("dashboard.html", counts=counts, recent_jobs=recent_jobs, recent_iocs=recent_iocs)

    @app.route("/queues", methods=["GET", "POST"])
    def queues():
        with connect() as conn:
            if request.method == "POST":
                action = request.form.get("action")
                if action == "create":
                    name = request.form.get("name", "").strip()
                    queue_type = request.form.get("queue_type", "keyword_search")
                    if not name:
                        flash("Queue name is required.", "error")
                    elif queue_type not in {"keyword_search", "url_crawl"}:
                        flash("Queue type is invalid.", "error")
                    else:
                        queue_id = get_or_create_queue(conn, name, queue_type)
                        flash(f"Queue created: {name}", "success")
                        return redirect(url_for("queues", queue_id=queue_id))

                if action == "add_urls":
                    queue_id = request.form.get("queue_id", type=int)
                    queue = get_queue(conn, queue_id, "url_crawl")
                    if not queue:
                        flash("URL queue is required.", "error")
                    else:
                        added, skipped = add_urls_to_queue(
                            conn, queue_id, request.form.get("targets", "")
                        )
                        flash(
                            f"Added {added} URL/domain targets to queue '{queue['name']}'. Skipped {skipped}. Start the queue when ready.",
                            "success" if added else "error",
                        )
                        return redirect(url_for("queues", queue_id=queue_id))

                if action == "link_queues":
                    keyword_queue_id = request.form.get("keyword_queue_id", type=int)
                    url_queue_id = request.form.get("url_queue_id", type=int)
                    ok, message = bind_keyword_queue_to_url_queue(conn, keyword_queue_id, url_queue_id)
                    flash(message, "success" if ok else "error")
                    return redirect(url_for("queues", queue_id=keyword_queue_id or url_queue_id))

            rows = query_queues(conn)
            keyword_queues = conn.execute(
                "SELECT * FROM queues WHERE queue_type = 'keyword_search' ORDER BY id DESC"
            ).fetchall()
            url_queues = conn.execute(
                "SELECT * FROM queues WHERE queue_type = 'url_crawl' ORDER BY id DESC"
            ).fetchall()
            selected_queue_id = request.args.get("queue_id", type=int)
            selected_queue = None
            selected_jobs = []
            selected_url_items = []
            selected_queries = []
            if selected_queue_id:
                selected_queue = conn.execute(
                    "SELECT * FROM queues WHERE id = ?", (selected_queue_id,)
                ).fetchone()
                selected_jobs = conn.execute(
                    """
                    SELECT * FROM jobs
                    WHERE queue_id = ?
                    ORDER BY id DESC
                    LIMIT 80
                    """,
                    (selected_queue_id,),
                ).fetchall()
                selected_url_items = conn.execute(
                    """
                    SELECT qi.*,
                           u.url_norm,
                           u.domain,
                           u.crawl_status,
                           source_q.name AS source_queue_name,
                           source_q.queue_type AS source_queue_type
                    FROM url_queue_items qi
                    JOIN urls u ON u.id = qi.url_id
                    LEFT JOIN queues source_q ON source_q.id = qi.source_queue_id
                    WHERE qi.queue_id = ?
                    ORDER BY qi.id DESC
                    LIMIT 120
                    """,
                    (selected_queue_id,),
                ).fetchall()
                selected_queries = conn.execute(
                    """
                    SELECT sqi.*,
                           sq.query_text,
                           k.text AS keyword_text,
                           output_q.name AS output_url_queue_name
                    FROM search_queue_items sqi
                    JOIN search_queries sq ON sq.id = sqi.search_query_id
                    JOIN keywords k ON k.id = sqi.keyword_id
                    LEFT JOIN queues output_q ON output_q.id = sqi.output_url_queue_id
                    WHERE sqi.queue_id = ?
                    ORDER BY sqi.id DESC
                    LIMIT 120
                    """,
                    (selected_queue_id,),
                ).fetchall()
        return render_template(
            "queues.html",
            rows=rows,
            keyword_queues=keyword_queues,
            url_queues=url_queues,
            selected_queue=selected_queue,
            selected_jobs=selected_jobs,
            selected_url_items=selected_url_items,
            selected_queries=selected_queries,
        )

    @app.post("/queues/<int:queue_id>/start")
    def start_queue(queue_id: int):
        with connect() as conn:
            queue = conn.execute("SELECT * FROM queues WHERE id = ?", (queue_id,)).fetchone()
            if not queue:
                flash("Queue not found.", "error")
                return redirect(url_for("queues"))
            if queue["queue_type"] == "keyword_search" and not get_output_url_queue(conn, queue_id):
                flash("Bind this Keyword Search Queue to a specific URL Crawl Queue before starting it.", "error")
                return redirect(url_for("queues", queue_id=queue_id))
            jobs_started, items_started = start_queue_work(conn, queue_id)
        flash(
            f"Started queue '{queue['name']}'. Requeued {jobs_started} jobs and {items_started} items.",
            "success",
        )
        return redirect(url_for("queues", queue_id=queue_id))

    @app.post("/queues/<int:queue_id>/stop")
    def stop_queue(queue_id: int):
        with connect() as conn:
            queue = conn.execute("SELECT * FROM queues WHERE id = ?", (queue_id,)).fetchone()
            if not queue:
                flash("Queue not found.", "error")
                return redirect(url_for("queues"))
            jobs_paused, items_paused, running_jobs = stop_queue_work(conn, queue_id)
        suffix = " Running job will finish its current browser session." if running_jobs else ""
        flash(
            f"Stopped queue '{queue['name']}'. Paused {jobs_paused} jobs and {items_paused} items.{suffix}",
            "success",
        )
        return redirect(url_for("queues", queue_id=queue_id))

    @app.post("/queues/<int:queue_id>/resume")
    def resume_queue(queue_id: int):
        return start_queue(queue_id)

    @app.route("/keywords", methods=["GET", "POST"])
    def keywords():
        with connect() as conn:
            if request.method == "POST":
                raw_keywords = request.form.get("keywords", "")
                queue_id = request.form.get("queue_id", type=int)
                queue = get_queue(conn, queue_id, "keyword_search")
                if not queue:
                    flash("Select a specific Keyword Search Queue before adding keywords.", "error")
                    return redirect(url_for("keywords"))

                keyword_lines = parse_lines(raw_keywords)
                if not keyword_lines:
                    flash("Keyword input is required.", "error")
                    return redirect(url_for("keywords", queue_id=queue_id))

                query_mode = request.form.get("query_mode", "dork")
                direct_query_mode = query_mode == "direct"
                dork_ids = [int(item) for item in request.form.getlist("dork_ids")]
                dorks = [get_or_create_direct_dork(conn)] if direct_query_mode else get_selected_dorks(conn, dork_ids)
                output_url_queue = get_output_url_queue(conn, queue_id)
                output_url_queue_id = int(output_url_queue["id"]) if output_url_queue else None
                keywords_imported = 0
                queries_created = 0

                for text in keyword_lines:
                    keyword_id = upsert_keyword(conn, text)
                    keywords_imported += 1
                    for dork in dorks:
                        query_text = text if direct_query_mode else render_dork(dork["template"], text)
                        if not query_text:
                            continue
                        search_query_id, created = upsert_search_query(
                            conn, keyword_id, dork["id"], query_text, queue_id
                        )
                        if created:
                            queries_created += 1
                        conn.execute(
                            """
                            UPDATE search_queries
                            SET queue_id = COALESCE(queue_id, ?)
                            WHERE id = ?
                            """,
                            (queue_id, search_query_id),
                        )
                        queue_item_id = upsert_search_queue_item(
                            conn, queue_id, search_query_id, keyword_id, output_url_queue_id
                        )
                        queue_search_job(
                            conn,
                            queue_id=queue_id,
                            search_query_id=search_query_id,
                            queue_item_id=queue_item_id,
                            output_url_queue_id=output_url_queue_id,
                            initial_status="paused",
                        )

                flash(
                    f"Added {keywords_imported} keyword lines and {queries_created} search queries to queue. Start the queue when ready.",
                    "success",
                )
                return redirect(url_for("queues", queue_id=queue_id))

            dorks = conn.execute("SELECT * FROM search_dorks ORDER BY enabled DESC, id").fetchall()
            keyword_queues = conn.execute(
                "SELECT * FROM queues WHERE queue_type = 'keyword_search' ORDER BY id DESC"
            ).fetchall()
            selected_keyword_queue_id = request.args.get("queue_id", type=int)
            rows = conn.execute(
                """
                SELECT k.*,
                       GROUP_CONCAT(DISTINCT q.name) AS queue_names,
                       COUNT(DISTINCT sq.id) AS query_count,
                       COUNT(DISTINCT CASE WHEN sq.status = 'done' THEN sq.id END) AS done_count,
                       COUNT(DISTINCT CASE WHEN sq.status = 'failed' THEN sq.id END) AS failed_count,
                       COUNT(DISTINCT CASE WHEN sq.status = 'paused' THEN sq.id END) AS paused_count,
                       COALESCE(SUM(sq.result_count), 0) AS result_count,
                       COALESCE(MAX(sq.page_count), 0) AS page_count
                FROM keywords k
                LEFT JOIN search_queries sq ON sq.keyword_id = k.id
                LEFT JOIN search_queue_items sqi ON sqi.keyword_id = k.id
                LEFT JOIN queues q ON q.id = sqi.queue_id
                GROUP BY k.id
                ORDER BY k.id DESC
                LIMIT 200
                """
            ).fetchall()
        return render_template(
            "keywords.html",
            dorks=dorks,
            rows=rows,
            keyword_queues=keyword_queues,
            selected_keyword_queue_id=selected_keyword_queue_id,
        )

    @app.post("/keywords/<int:keyword_id>/stop")
    def stop_keyword(keyword_id: int):
        with connect() as conn:
            keyword = conn.execute("SELECT * FROM keywords WHERE id = ?", (keyword_id,)).fetchone()
            if not keyword:
                flash("Keyword not found.", "error")
                return redirect(url_for("keywords"))
            paused_queries, paused_jobs, running_queries = pause_keyword_work(conn, keyword_id)
        suffix = " Running query will finish its current browser session." if running_queries else ""
        flash(
            f"Stopped keyword '{keyword['text']}'. Paused {paused_queries} pending queries and {paused_jobs} jobs.{suffix}",
            "success",
        )
        return redirect(url_for("keywords"))

    @app.post("/keywords/<int:keyword_id>/resume")
    def resume_keyword(keyword_id: int):
        with connect() as conn:
            keyword = conn.execute("SELECT * FROM keywords WHERE id = ?", (keyword_id,)).fetchone()
            if not keyword:
                flash("Keyword not found.", "error")
                return redirect(url_for("keywords"))
            resumed_queries = resume_keyword_work(conn, keyword_id)
        flash(f"Resumed keyword '{keyword['text']}' and queued {resumed_queries} queries.", "success")
        return redirect(url_for("keywords"))

    @app.post("/keywords/<int:keyword_id>/delete")
    def delete_keyword(keyword_id: int):
        with connect() as conn:
            keyword = conn.execute("SELECT * FROM keywords WHERE id = ?", (keyword_id,)).fetchone()
            if not keyword:
                flash("Keyword not found.", "error")
                return redirect(url_for("keywords"))
            deleted, query_count, job_count, error = delete_keyword_from_queue(conn, keyword_id)
        if not deleted:
            flash(error, "error")
            return redirect(url_for("keywords"))

        flash(
            f"Deleted keyword '{keyword['text']}' from queue. Removed {query_count} search queries and {job_count} jobs.",
            "success",
        )
        return redirect(url_for("keywords"))

    @app.route("/dorks", methods=["GET", "POST"])
    def dorks():
        preview = None
        with connect() as conn:
            if request.method == "POST":
                action = request.form.get("action")
                name = request.form.get("name", "").strip()
                template = request.form.get("template", "").strip()
                sample = request.form.get("sample_keyword", "").strip() or "88i"

                if action == "preview":
                    preview = validate_and_preview_dork(template, sample)
                else:
                    ok, message = validate_dork(template)
                    if not ok:
                        flash(message, "error")
                    elif not name:
                        flash("Dork name is required.", "error")
                    else:
                        conn.execute(
                            """
                            INSERT INTO search_dorks(name, template, description, enabled)
                            VALUES (?, ?, ?, 1)
                            """,
                            (name, template, request.form.get("description", "").strip()),
                        )
                        flash("Search dork created.", "success")
                        return redirect(url_for("dorks"))

            rows = conn.execute("SELECT * FROM search_dorks ORDER BY enabled DESC, id DESC").fetchall()
        return render_template("dorks.html", rows=rows, preview=preview)

    @app.post("/dorks/<int:dork_id>/toggle")
    def toggle_dork(dork_id: int):
        with connect() as conn:
            conn.execute(
                "UPDATE search_dorks SET enabled = CASE WHEN enabled = 1 THEN 0 ELSE 1 END, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (dork_id,),
            )
        return redirect(url_for("dorks"))

    @app.post("/dorks/<int:dork_id>/delete")
    def delete_dork(dork_id: int):
        with connect() as conn:
            used = scalar(conn, "SELECT COUNT(*) FROM search_queries WHERE dork_id = ?", (dork_id,))
            if used:
                flash("Cannot delete a dork that already has search queries. Disable it instead.", "error")
            else:
                conn.execute("DELETE FROM search_dorks WHERE id = ?", (dork_id,))
                flash("Search dork deleted.", "success")
        return redirect(url_for("dorks"))

    @app.route("/rules", methods=["GET", "POST"])
    def rules():
        test_output = None
        test_warnings = []
        with connect() as conn:
            if request.method == "POST":
                action = request.form.get("action")
                rule_payload = read_rule_form()

                if action == "test":
                    matches, test_warnings = test_rule(rule_payload, request.form.get("sample_text", ""))
                    test_output = matches
                else:
                    ok, error = validate_rule(
                        rule_payload["pattern"], rule_payload["flags"], rule_payload["value_group"]
                    )
                    if not ok:
                        flash(error, "error")
                    elif not rule_payload["name"]:
                        flash("Rule name is required.", "error")
                    else:
                        conn.execute(
                            """
                            INSERT INTO extraction_rules(
                              name, ioc_type, pattern, flags, value_group, input_scope,
                              exclude_pattern, normalizer, priority, enabled, description
                            )
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                rule_payload["name"],
                                rule_payload["ioc_type"],
                                rule_payload["pattern"],
                                rule_payload["flags"],
                                rule_payload["value_group"],
                                rule_payload["input_scope"],
                                rule_payload["exclude_pattern"],
                                rule_payload["normalizer"],
                                rule_payload["priority"],
                                1 if rule_payload["enabled"] else 0,
                                rule_payload["description"],
                            ),
                        )
                        flash("Extraction rule created.", "success")
                        return redirect(url_for("rules"))

            rows = conn.execute(
                "SELECT * FROM extraction_rules ORDER BY enabled DESC, priority, id"
            ).fetchall()
        return render_template(
            "rules.html", rows=rows, test_output=test_output, test_warnings=test_warnings
        )

    @app.post("/rules/<int:rule_id>/toggle")
    def toggle_rule(rule_id: int):
        with connect() as conn:
            conn.execute(
                "UPDATE extraction_rules SET enabled = CASE WHEN enabled = 1 THEN 0 ELSE 1 END, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (rule_id,),
            )
        return redirect(url_for("rules"))

    @app.post("/rules/<int:rule_id>/delete")
    def delete_rule(rule_id: int):
        with connect() as conn:
            row = conn.execute("SELECT builtin FROM extraction_rules WHERE id = ?", (rule_id,)).fetchone()
            if row and row["builtin"]:
                flash("Built-in rules should be disabled instead of deleted.", "error")
            else:
                conn.execute("DELETE FROM extraction_rules WHERE id = ?", (rule_id,))
                flash("Extraction rule deleted.", "success")
        return redirect(url_for("rules"))

    @app.get("/review")
    def review():
        legacy_status = request.args.get("status")
        url_status = normalize_review_status(
            request.args.get("url_status") or legacy_status or "pending_review"
        )
        domain_status = normalize_review_status(
            request.args.get("domain_status") or legacy_status or "pending_review"
        )
        source = request.args.get("source", "")
        q = request.args.get("q", "").strip()
        with connect() as conn:
            urls = list(query_review_urls(conn, url_status, q))
            if source:
                urls = [row for row in urls if source_matches(row["domain_sources"] or "", source)]
            domains = list(query_review_domains(conn, domain_status, q))
            if source:
                domains = [row for row in domains if source_matches(row["sources"] or "", source)]
        return render_template(
            "review.html",
            urls=urls,
            domains=domains,
            url_status=url_status,
            domain_status=domain_status,
            review_tabs=REVIEW_TABS,
            source=source,
            q=q,
        )

    @app.post("/urls/<int:url_id>/approve")
    def approve_url(url_id: int):
        with connect() as conn:
            changed = approve_url_and_enqueue(conn, url_id)
        flash(
            "URL approved and crawl job queued if needed." if changed else "URL is already reviewed and is read-only.",
            "success" if changed else "error",
        )
        return redirect(request.referrer or url_for("review"))

    @app.post("/urls/<int:url_id>/reject")
    def reject_url_route(url_id: int):
        with connect() as conn:
            changed = reject_url(conn, url_id)
        flash(
            "URL rejected." if changed else "URL is already reviewed and is read-only.",
            "success" if changed else "error",
        )
        return redirect(request.referrer or url_for("review"))

    @app.post("/domains/reject")
    def reject_domain_route():
        domain = request.form.get("domain", "")
        with connect() as conn:
            changed = reject_domain(conn, domain)
        flash(
            f"Domain rejected: {domain}" if changed else f"Domain is already reviewed and is read-only: {domain}",
            "success" if changed else "error",
        )
        return redirect(request.referrer or url_for("review"))

    @app.post("/domains/approve")
    def approve_domain_route():
        domain = request.form.get("domain", "")
        with connect() as conn:
            domain_row = conn.execute(
                "SELECT review_status FROM domains WHERE domain = ?", (domain,)
            ).fetchone()
            if not domain_row or domain_row["review_status"] != "pending_review":
                changed = False
            else:
                conn.execute(
                    """
                    UPDATE domains
                    SET review_status = 'approved'
                    WHERE domain = ?
                      AND review_status = 'pending_review'
                    """,
                    (domain,),
                )
                pending_urls = conn.execute(
                    """
                    SELECT id
                    FROM urls
                    WHERE domain = ?
                      AND review_status = 'pending_review'
                    """,
                    (domain,),
                ).fetchall()
                for row in pending_urls:
                    approve_url_and_enqueue(conn, int(row["id"]))
                changed = True
        flash(
            f"Domain approved and pending URL crawl jobs queued: {domain}"
            if changed
            else f"Domain is already reviewed and is read-only: {domain}",
            "success" if changed else "error",
        )
        return redirect(request.referrer or url_for("review"))

    @app.get("/crawl")
    def crawl_status():
        with connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM urls
                WHERE review_status = 'approved' OR crawl_status != 'not_crawled'
                ORDER BY crawled_at DESC, id DESC
                LIMIT 300
                """
            ).fetchall()
        return render_template("crawl.html", rows=rows)

    @app.route("/jobs", methods=["GET", "POST"])
    def jobs():
        messages = []
        if request.method == "POST":
            action = request.form.get("action")
            if action == "pause_all":
                with connect() as conn:
                    paused_jobs, paused_queries = pause_pending_jobs(conn)
                flash(f"Stopped queue. Paused {paused_jobs} pending jobs and {paused_queries} search queries.", "success")
                return redirect(url_for("jobs"))
            if action == "resume_all":
                with connect() as conn:
                    resumed_jobs, resumed_queries = resume_paused_jobs(conn)
                flash(f"Resumed queue. Requeued {resumed_jobs} jobs and {resumed_queries} search queries.", "success")
                return redirect(url_for("jobs"))
            if action == "run_all":
                messages = run_all()
            else:
                messages = [run_one()]
            for message in messages:
                ok = any(marker in message for marker in ("completed", "No pending", "paused"))
                flash(message, "success" if ok else "error")
            return redirect(url_for("jobs"))

        with connect() as conn:
            rows = conn.execute(
                """
                SELECT j.*, q.name AS queue_name, q.queue_type
                FROM jobs j
                LEFT JOIN queues q ON q.id = j.queue_id
                ORDER BY j.id DESC
                LIMIT 200
                """
            ).fetchall()
        return render_template("jobs.html", rows=rows)

    @app.get("/iocs")
    def iocs():
        selected_id = request.args.get("ioc_id", type=int)
        ioc_type = request.args.get("type", "")
        with connect() as conn:
            params: list[object] = []
            where = ""
            if ioc_type:
                where = "WHERE i.type = ?"
                params.append(ioc_type)
            rows = conn.execute(
                f"""
                SELECT i.*,
                       COUNT(DISTINCT s.id) AS source_count,
                       GROUP_CONCAT(DISTINCT r.name) AS rule_names
                FROM iocs i
                LEFT JOIN ioc_sources s ON s.ioc_id = i.id
                LEFT JOIN extraction_rules r ON r.id = s.extraction_rule_id
                {where}
                GROUP BY i.id
                ORDER BY i.id DESC
                LIMIT 500
                """,
                params,
            ).fetchall()
            sources = []
            selected = None
            if selected_id:
                selected = conn.execute("SELECT * FROM iocs WHERE id = ?", (selected_id,)).fetchone()
                sources = conn.execute(
                    """
                    SELECT s.*, u.url_norm, u.domain, r.name AS rule_name
                    FROM ioc_sources s
                    JOIN urls u ON u.id = s.source_url_id
                    LEFT JOIN extraction_rules r ON r.id = s.extraction_rule_id
                    WHERE s.ioc_id = ?
                    ORDER BY s.id DESC
                    """,
                    (selected_id,),
                ).fetchall()
        return render_template("iocs.html", rows=rows, selected=selected, sources=sources, ioc_type=ioc_type)

    if start_worker:
        start_background_worker(app)

    return app


NAV = [
    ("Dashboard", "dashboard"),
    ("Queues", "queues"),
    ("Keywords", "keywords"),
    ("Search Dorks", "dorks"),
    ("Review", "review"),
    ("Rules", "rules"),
    ("Crawl", "crawl_status"),
    ("Jobs", "jobs"),
    ("IOCs", "iocs"),
]

REVIEW_TABS = [
    {"value": "pending_review", "label": "Need Action"},
    {"value": "approved", "label": "Approved"},
    {"value": "rejected", "label": "Rejected"},
]

REVIEW_STATUS_VALUES = {item["value"] for item in REVIEW_TABS}


def scalar(conn, sql: str, params: tuple[object, ...] = ()) -> int:
    return int(conn.execute(sql, params).fetchone()[0])


def normalize_review_status(value: str | None) -> str:
    return value if value in REVIEW_STATUS_VALUES else "pending_review"


def parse_lines(value: str) -> list[str]:
    seen = set()
    rows = []
    for line in value.splitlines():
        item = line.strip()
        if item and item not in seen:
            seen.add(item)
            rows.append(item)
    return rows


def upsert_keyword(conn, text: str) -> int:
    conn.execute("INSERT OR IGNORE INTO keywords(text) VALUES (?)", (text,))
    conn.execute(
        """
        UPDATE keywords
        SET status = 'pending'
        WHERE text = ? AND status IN ('paused', 'failed', 'done')
        """,
        (text,),
    )
    row = conn.execute("SELECT id FROM keywords WHERE text = ?", (text,)).fetchone()
    return int(row["id"])


def pause_keyword_work(conn, keyword_id: int) -> tuple[int, int, int]:
    query_rows = conn.execute(
        """
        SELECT id
        FROM search_queries
        WHERE keyword_id = ? AND status = 'pending'
        """,
        (keyword_id,),
    ).fetchall()
    query_ids = [int(row["id"]) for row in query_rows]
    running_queries = scalar(
        conn,
        "SELECT COUNT(*) FROM search_queries WHERE keyword_id = ? AND status = 'running'",
        (keyword_id,),
    )

    conn.execute("UPDATE keywords SET status = 'paused' WHERE id = ?", (keyword_id,))
    if not query_ids:
        return 0, 0, running_queries

    placeholders = ",".join("?" for _ in query_ids)
    conn.execute(
        f"""
        UPDATE search_queries
        SET status = 'paused',
            started_at = NULL,
            finished_at = NULL
        WHERE id IN ({placeholders})
        """,
        query_ids,
    )
    conn.execute(
        """
        UPDATE search_queue_items
        SET status = 'paused',
            started_at = NULL,
            finished_at = NULL
        WHERE keyword_id = ?
          AND status = 'pending'
        """,
        (keyword_id,),
    )

    paused_jobs = 0
    for query_id in query_ids:
        cursor = conn.execute(
            """
            UPDATE jobs
            SET status = 'paused',
                started_at = NULL,
                finished_at = NULL,
                error = NULL
            WHERE type = 'search_query'
              AND (dedupe_key = ? OR dedupe_key LIKE ?)
              AND status = 'pending'
            """,
            (f"search:{query_id}", f"queue:%:search:{query_id}"),
        )
        paused_jobs += cursor.rowcount if cursor.rowcount and cursor.rowcount > 0 else 0
    return len(query_ids), paused_jobs, running_queries


def resume_keyword_work(conn, keyword_id: int) -> int:
    query_rows = conn.execute(
        """
        SELECT sqi.id AS queue_item_id,
               sqi.queue_id,
               sqi.search_query_id,
               COALESCE(sqi.output_url_queue_id, qr.url_queue_id) AS output_url_queue_id,
               q.status AS queue_status
        FROM search_queue_items sqi
        JOIN queues q ON q.id = sqi.queue_id
        LEFT JOIN queue_routes qr ON qr.keyword_queue_id = sqi.queue_id
        WHERE sqi.keyword_id = ?
          AND sqi.status = 'paused'
        ORDER BY sqi.id
        """,
        (keyword_id,),
    ).fetchall()
    conn.execute("UPDATE keywords SET status = 'pending' WHERE id = ?", (keyword_id,))
    resumed_count = 0

    for row in query_rows:
        query_id = int(row["search_query_id"])
        queue_id = int(row["queue_id"])
        output_url_queue_id = int(row["output_url_queue_id"]) if row["output_url_queue_id"] else None
        if row["queue_status"] != "running" or not output_url_queue_id:
            continue
        resumed_count += 1
        conn.execute(
            """
            UPDATE search_queue_items
            SET status = 'pending',
                output_url_queue_id = ?,
                started_at = NULL,
                finished_at = NULL,
                error = NULL
            WHERE id = ?
            """,
            (output_url_queue_id, row["queue_item_id"]),
        )
        conn.execute(
            """
            UPDATE search_queries
            SET status = 'pending',
                started_at = NULL,
                finished_at = NULL
            WHERE id = ?
            """,
            (query_id,),
        )
        queue_search_job(
            conn,
            queue_id=queue_id,
            search_query_id=query_id,
            queue_item_id=int(row["queue_item_id"]),
            output_url_queue_id=output_url_queue_id,
            initial_status="pending",
        )

    refresh_keyword_status(conn, keyword_id)
    return resumed_count


def delete_keyword_from_queue(conn, keyword_id: int) -> tuple[bool, int, int, str]:
    query_rows = conn.execute(
        "SELECT id, status FROM search_queries WHERE keyword_id = ? ORDER BY id",
        (keyword_id,),
    ).fetchall()
    query_ids = [int(row["id"]) for row in query_rows]
    running_queries = [query_id for query_id, row in zip(query_ids, query_rows) if row["status"] == "running"]
    if running_queries:
        return False, 0, 0, "Cannot delete keyword while a search query is running. Stop it and wait for the current browser session to finish."

    running_jobs = []
    for query_id in query_ids:
        row = conn.execute(
            """
            SELECT id
            FROM jobs
            WHERE type = 'search_query'
              AND (dedupe_key = ? OR dedupe_key LIKE ?)
              AND status = 'running'
            """,
            (f"search:{query_id}", f"queue:%:search:{query_id}"),
        ).fetchone()
        if row:
            running_jobs.append(int(row["id"]))
    if running_jobs:
        return False, 0, 0, "Cannot delete keyword while a search job is running. Wait for the current job to finish."

    conn.execute("UPDATE keywords SET status = 'paused' WHERE id = ?", (keyword_id,))
    job_count = 0

    if query_ids:
        query_placeholders = ",".join("?" for _ in query_ids)
        source_params = [keyword_id, *query_ids]
        conn.execute(
            f"""
            UPDATE url_sources
            SET keyword_id = NULL,
                search_query_id = NULL
            WHERE keyword_id = ? OR search_query_id IN ({query_placeholders})
            """,
            source_params,
        )
        conn.execute(
            f"""
            UPDATE domain_sources
            SET keyword_id = NULL,
                search_query_id = NULL
            WHERE keyword_id = ? OR search_query_id IN ({query_placeholders})
            """,
            source_params,
        )

        for query_id in query_ids:
            job_cursor = conn.execute(
                """
                DELETE FROM jobs
                WHERE type = 'search_query'
                  AND (dedupe_key = ? OR dedupe_key LIKE ?)
                """,
                (f"search:{query_id}", f"queue:%:search:{query_id}"),
            )
            job_count += max(job_cursor.rowcount, 0)
        conn.execute(f"DELETE FROM search_queries WHERE id IN ({query_placeholders})", query_ids)
    else:
        conn.execute("UPDATE url_sources SET keyword_id = NULL WHERE keyword_id = ?", (keyword_id,))
        conn.execute("UPDATE domain_sources SET keyword_id = NULL WHERE keyword_id = ?", (keyword_id,))

    conn.execute("DELETE FROM keywords WHERE id = ?", (keyword_id,))
    return True, len(query_ids), job_count, ""


def pause_pending_jobs(conn) -> tuple[int, int]:
    queue_ids = {
        int(row["queue_id"])
        for row in conn.execute(
            "SELECT DISTINCT queue_id FROM jobs WHERE status = 'pending' AND queue_id IS NOT NULL"
        ).fetchall()
    }
    search_query_ids = search_query_ids_from_jobs(
        conn.execute(
            "SELECT * FROM jobs WHERE status = 'pending' AND type = 'search_query'"
        ).fetchall()
    )
    paused_jobs = conn.execute(
        """
        UPDATE jobs
        SET status = 'paused',
            started_at = NULL,
            finished_at = NULL,
            error = NULL
        WHERE status = 'pending'
        """
    ).rowcount

    affected_keyword_ids = set()
    for query_id in search_query_ids:
        row = conn.execute("SELECT keyword_id FROM search_queries WHERE id = ?", (query_id,)).fetchone()
        if row:
            affected_keyword_ids.add(int(row["keyword_id"]))
        conn.execute("UPDATE search_queries SET status = 'paused' WHERE id = ?", (query_id,))

    for keyword_id in affected_keyword_ids:
        conn.execute("UPDATE keywords SET status = 'paused' WHERE id = ?", (keyword_id,))
    conn.execute(
        """
        UPDATE search_queue_items
        SET status = 'paused',
            started_at = NULL,
            finished_at = NULL
        WHERE status = 'pending'
        """
    )
    conn.execute(
        """
        UPDATE url_queue_items
        SET status = 'paused',
            started_at = NULL,
            finished_at = NULL
        WHERE status = 'pending'
        """
    )
    for queue_id in queue_ids:
        conn.execute(
            "UPDATE queues SET status = 'paused', stopped_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (queue_id,),
        )

    return max(paused_jobs, 0), len(search_query_ids)


def resume_paused_jobs(conn) -> tuple[int, int]:
    paused_queues = conn.execute(
        """
        SELECT id
        FROM queues
        WHERE status = 'paused'
        ORDER BY id
        """
    ).fetchall()
    resumed_jobs = 0
    resumed_items = 0
    for row in paused_queues:
        jobs_started, items_started = start_queue_work(conn, int(row["id"]))
        resumed_jobs += jobs_started
        resumed_items += items_started
    return max(resumed_jobs, 0), resumed_items


def search_query_ids_from_jobs(rows) -> list[int]:
    query_ids = []
    for row in rows:
        try:
            payload = json.loads(row["payload"])
            query_id = int(payload["search_query_id"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            continue
        query_ids.append(query_id)
    return query_ids


def get_queue(conn, queue_id: int | None, queue_type: str | None = None):
    if not queue_id:
        return None
    if queue_type:
        return conn.execute(
            "SELECT * FROM queues WHERE id = ? AND queue_type = ?", (queue_id, queue_type)
        ).fetchone()
    return conn.execute("SELECT * FROM queues WHERE id = ?", (queue_id,)).fetchone()


def get_or_create_queue(conn, name: str, queue_type: str) -> int:
    conn.execute(
        """
        INSERT OR IGNORE INTO queues(name, queue_type, status)
        VALUES (?, ?, 'draft')
        """,
        (name, queue_type),
    )
    row = conn.execute(
        "SELECT id FROM queues WHERE name = ? AND queue_type = ?", (name, queue_type)
    ).fetchone()
    return int(row["id"])


def get_output_url_queue(conn, keyword_queue_id: int):
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


def bind_keyword_queue_to_url_queue(
    conn, keyword_queue_id: int | None, url_queue_id: int | None
) -> tuple[bool, str]:
    keyword_queue = get_queue(conn, keyword_queue_id, "keyword_search")
    if not keyword_queue:
        return False, "Select a valid Keyword Search Queue."
    url_queue = get_queue(conn, url_queue_id, "url_crawl")
    if not url_queue:
        return False, "Select a valid URL Crawl Queue."

    linked_url = conn.execute(
        """
        SELECT keyword_queue_id
        FROM queue_routes
        WHERE url_queue_id = ?
          AND keyword_queue_id != ?
        """,
        (url_queue_id, keyword_queue_id),
    ).fetchone()
    if linked_url:
        return False, "This URL Crawl Queue is already bound to another Keyword Search Queue."

    existing = conn.execute(
        "SELECT id, url_queue_id FROM queue_routes WHERE keyword_queue_id = ?", (keyword_queue_id,)
    ).fetchone()
    changing_existing_route = existing and int(existing["url_queue_id"]) != int(url_queue_id)
    if changing_existing_route:
        active_count = scalar(
            conn,
            """
            SELECT COUNT(*)
            FROM jobs
            WHERE queue_id = ?
              AND type = 'search_query'
              AND status IN ('pending', 'running')
            """,
            (keyword_queue_id,),
        )
        running_items = scalar(
            conn,
            """
            SELECT COUNT(*)
            FROM search_queue_items
            WHERE queue_id = ?
              AND status IN ('pending', 'running')
            """,
            (keyword_queue_id,),
        )
        if keyword_queue["status"] == "running" or active_count or running_items:
            return (
                False,
                "Stop this Keyword Search Queue and wait for active search jobs to finish before changing its output URL queue.",
            )

    if existing:
        conn.execute(
            """
            UPDATE queue_routes
            SET url_queue_id = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE keyword_queue_id = ?
            """,
            (url_queue_id, keyword_queue_id),
        )
    else:
        conn.execute(
            """
            INSERT INTO queue_routes(keyword_queue_id, url_queue_id)
            VALUES (?, ?)
            """,
            (keyword_queue_id, url_queue_id),
        )
    conn.execute(
        """
        UPDATE search_queue_items
        SET output_url_queue_id = ?
        WHERE queue_id = ?
          AND status NOT IN ('running', 'done')
        """,
        (url_queue_id, keyword_queue_id),
    )
    return True, f"Bound keyword queue '{keyword_queue['name']}' to URL queue '{url_queue['name']}'."


def query_queues(conn):
    return conn.execute(
        """
        SELECT q.*,
               COUNT(DISTINCT j.id) AS job_count,
               COUNT(DISTINCT CASE WHEN j.status = 'pending' THEN j.id END) AS pending_jobs,
               COUNT(DISTINCT CASE WHEN j.status = 'running' THEN j.id END) AS running_jobs,
               COUNT(DISTINCT CASE WHEN j.status = 'paused' THEN j.id END) AS paused_jobs,
               COUNT(DISTINCT CASE WHEN j.status = 'done' THEN j.id END) AS done_jobs,
               COUNT(DISTINCT CASE WHEN j.status = 'failed' THEN j.id END) AS failed_jobs,
               COUNT(DISTINCT sqi.id) AS search_query_count,
               COUNT(DISTINCT uqi.id) AS url_item_count,
               qr.url_queue_id AS output_url_queue_id,
               uq.name AS output_url_queue_name
        FROM queues q
        LEFT JOIN jobs j ON j.queue_id = q.id
        LEFT JOIN search_queue_items sqi ON sqi.queue_id = q.id
        LEFT JOIN url_queue_items uqi ON uqi.queue_id = q.id
        LEFT JOIN queue_routes qr ON qr.keyword_queue_id = q.id
        LEFT JOIN queues uq ON uq.id = qr.url_queue_id
        GROUP BY q.id
        ORDER BY q.id DESC
        LIMIT 200
        """
    ).fetchall()


def start_queue_work(conn, queue_id: int) -> tuple[int, int]:
    queue = conn.execute("SELECT * FROM queues WHERE id = ?", (queue_id,)).fetchone()
    if not queue:
        return 0, 0
    if queue["queue_type"] == "keyword_search" and not get_output_url_queue(conn, queue_id):
        return 0, 0
    conn.execute(
        """
        UPDATE queues
        SET status = 'running',
            started_at = COALESCE(started_at, CURRENT_TIMESTAMP),
            stopped_at = NULL,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (queue_id,),
    )

    if queue["queue_type"] == "keyword_search":
        output_url_queue = get_output_url_queue(conn, queue_id)
        output_url_queue_id = int(output_url_queue["id"])
        item_rows = conn.execute(
            """
            SELECT id, search_query_id
            FROM search_queue_items
            WHERE queue_id = ?
              AND status IN ('pending', 'paused', 'failed')
            ORDER BY id
            """,
            (queue_id,),
        ).fetchall()
        query_count = conn.execute(
            """
            UPDATE search_queue_items
            SET status = 'pending',
                output_url_queue_id = ?,
                started_at = NULL,
                finished_at = NULL,
                error = NULL
            WHERE queue_id = ?
              AND status IN ('pending', 'paused', 'failed')
            """,
            (output_url_queue_id, queue_id),
        ).rowcount
        url_count = 0
        jobs_started = 0
        for item in item_rows:
            jobs_started += queue_search_job(
                conn,
                queue_id=queue_id,
                search_query_id=int(item["search_query_id"]),
                queue_item_id=int(item["id"]),
                output_url_queue_id=output_url_queue_id,
                initial_status="pending",
            )
    else:
        url_count = conn.execute(
            """
            UPDATE url_queue_items
            SET status = 'pending',
                started_at = NULL,
                finished_at = NULL,
                error = NULL
            WHERE queue_id = ?
              AND status IN ('paused', 'failed')
              AND EXISTS (
                SELECT 1 FROM urls u
                WHERE u.id = url_queue_items.url_id
                  AND u.review_status = 'approved'
              )
            """,
            (queue_id,),
        ).rowcount
        conn.execute(
            """
            UPDATE url_queue_items
            SET status = 'pending_review'
            WHERE queue_id = ?
              AND status IN ('paused', 'failed')
              AND EXISTS (
                SELECT 1 FROM urls u
                WHERE u.id = url_queue_items.url_id
                  AND u.review_status != 'approved'
              )
            """,
            (queue_id,),
        )
        query_count = 0
        jobs_started = conn.execute(
            """
            UPDATE jobs
            SET status = 'pending',
                started_at = NULL,
                finished_at = NULL,
                error = NULL
            WHERE queue_id = ?
              AND type = 'crawl_url'
              AND status IN ('paused', 'failed')
              AND EXISTS (
                SELECT 1
                FROM url_queue_items qi
                JOIN urls u ON u.id = qi.url_id
                WHERE qi.queue_id = jobs.queue_id
                  AND jobs.dedupe_key = 'queue:' || qi.queue_id || ':crawl:' || qi.url_id
                  AND qi.status = 'pending'
                  AND u.review_status = 'approved'
              )
            """,
            (queue_id,),
        ).rowcount
    return max(jobs_started, 0), max(query_count, 0) + max(url_count, 0)


def stop_queue_work(conn, queue_id: int) -> tuple[int, int, int]:
    running_jobs = scalar(
        conn,
        "SELECT COUNT(*) FROM jobs WHERE queue_id = ? AND status = 'running'",
        (queue_id,),
    )
    jobs_paused = conn.execute(
        """
        UPDATE jobs
        SET status = 'paused',
            started_at = NULL,
            finished_at = NULL,
            error = NULL
        WHERE queue_id = ?
          AND status = 'pending'
        """,
        (queue_id,),
    ).rowcount
    query_count = conn.execute(
        "UPDATE search_queue_items SET status = 'paused' WHERE queue_id = ? AND status = 'pending'",
        (queue_id,),
    ).rowcount
    url_count = conn.execute(
        "UPDATE url_queue_items SET status = 'paused' WHERE queue_id = ? AND status = 'pending'",
        (queue_id,),
    ).rowcount
    conn.execute(
        """
        UPDATE queues
        SET status = 'paused',
            stopped_at = CURRENT_TIMESTAMP,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (queue_id,),
    )
    return max(jobs_paused, 0), max(query_count, 0) + max(url_count, 0), running_jobs


def add_urls_to_queue(conn, queue_id: int, raw_targets: str) -> tuple[int, int]:
    added = 0
    skipped = 0
    for item in parse_lines(raw_targets):
        url_norm = normalize_url(item)
        if not url_norm:
            skipped += 1
            continue
        domain = get_domain(url_norm)
        if not domain:
            skipped += 1
            continue

        url_id = upsert_url(conn, item, url_norm, domain, "manual_queue")
        upsert_domain(conn, domain, "manual_queue")
        conn.execute("UPDATE urls SET review_status = 'approved' WHERE id = ?", (url_id,))
        conn.execute(
            "UPDATE domains SET review_status = 'approved' WHERE domain = ? AND review_status != 'rejected'",
            (domain,),
        )
        before = conn.total_changes
        conn.execute(
            """
            INSERT OR IGNORE INTO url_queue_items(queue_id, url_id, status)
            VALUES (?, ?, 'paused')
            """,
            (queue_id, url_id),
        )
        item_row = conn.execute(
            """
            SELECT id
            FROM url_queue_items
            WHERE queue_id = ? AND url_id = ?
            """,
            (queue_id, url_id),
        ).fetchone()
        if conn.total_changes > before:
            added += 1
        else:
            skipped += 1
        if item_row:
            enqueue_job(
                conn,
                "crawl_url",
                {"url_id": url_id, "url_queue_item_id": int(item_row["id"])},
                f"queue:{queue_id}:crawl:{url_id}",
                queue_id=queue_id,
                initial_status="paused",
            )
    return added, skipped


def upsert_search_queue_item(
    conn,
    queue_id: int,
    search_query_id: int,
    keyword_id: int,
    output_url_queue_id: int | None = None,
) -> int:
    conn.execute(
        """
        INSERT OR IGNORE INTO search_queue_items(
          queue_id, search_query_id, keyword_id, output_url_queue_id, status
        )
        VALUES (?, ?, ?, ?, 'paused')
        """,
        (queue_id, search_query_id, keyword_id, output_url_queue_id),
    )
    if output_url_queue_id:
        conn.execute(
            """
            UPDATE search_queue_items
            SET output_url_queue_id = COALESCE(output_url_queue_id, ?)
            WHERE queue_id = ?
              AND search_query_id = ?
            """,
            (output_url_queue_id, queue_id, search_query_id),
        )
    row = conn.execute(
        """
        SELECT id
        FROM search_queue_items
        WHERE queue_id = ? AND search_query_id = ?
        """,
        (queue_id, search_query_id),
    ).fetchone()
    return int(row["id"])


def build_search_job_payload(
    search_query_id: int,
    queue_item_id: int,
    output_url_queue_id: int | None,
) -> dict[str, int]:
    payload = {
        "search_query_id": int(search_query_id),
        "search_queue_item_id": int(queue_item_id),
    }
    if output_url_queue_id:
        payload["output_url_queue_id"] = int(output_url_queue_id)
    return payload


def queue_search_job(
    conn,
    queue_id: int,
    search_query_id: int,
    queue_item_id: int,
    output_url_queue_id: int | None,
    initial_status: str,
) -> int:
    payload = build_search_job_payload(search_query_id, queue_item_id, output_url_queue_id)
    payload_json = json.dumps(payload)
    dedupe_key = f"queue:{queue_id}:search:{search_query_id}"
    running = conn.execute(
        "SELECT id FROM jobs WHERE dedupe_key = ? AND status = 'running'",
        (dedupe_key,),
    ).fetchone()
    if running:
        return 0
    updated = conn.execute(
        """
        UPDATE jobs
        SET queue_id = ?,
            payload = ?,
            status = CASE
                WHEN status = 'pending' AND ? = 'paused' THEN status
                ELSE ?
            END,
            attempts = CASE WHEN status IN ('failed', 'done') THEN 0 ELSE attempts END,
            error = NULL,
            started_at = NULL,
            finished_at = NULL
        WHERE dedupe_key = ?
          AND status != 'running'
        """,
        (queue_id, payload_json, initial_status, initial_status, dedupe_key),
    ).rowcount
    if updated:
        return max(updated, 0)

    enqueue_job(
        conn,
        "search_query",
        payload,
        dedupe_key,
        queue_id=queue_id,
        initial_status=initial_status,
    )
    return 1


def get_selected_dorks(conn, dork_ids: list[int]):
    if dork_ids:
        placeholders = ",".join("?" for _ in dork_ids)
        return conn.execute(
            f"SELECT * FROM search_dorks WHERE id IN ({placeholders}) AND enabled = 1 ORDER BY id",
            dork_ids,
        ).fetchall()
    return conn.execute("SELECT * FROM search_dorks WHERE enabled = 1 ORDER BY id").fetchall()


def get_or_create_direct_dork(conn):
    row = conn.execute(
        "SELECT * FROM search_dorks WHERE template = '{keyword}' ORDER BY id LIMIT 1"
    ).fetchone()
    if row:
        return row
    conn.execute(
        """
        INSERT INTO search_dorks(name, template, description, enabled)
        VALUES ('Direct query', '{keyword}', 'Use each input line as a complete Google query.', 1)
        """
    )
    return conn.execute(
        "SELECT * FROM search_dorks WHERE template = '{keyword}' ORDER BY id LIMIT 1"
    ).fetchone()


def render_dork(template: str, keyword: str) -> str:
    keyword = " ".join((keyword or "").replace('"', " ").split())
    return template.replace("{keyword}", keyword).strip()


def validate_dork(template: str) -> tuple[bool, str]:
    if not template:
        return False, "Template is required."
    if "{keyword}" not in template:
        return False, "Template must contain {keyword}."
    if len(template) > 500:
        return False, "Template is too long."
    if any(char in template for char in "\r\n\t"):
        return False, "Template cannot contain control characters."
    return True, "OK"


def validate_and_preview_dork(template: str, sample_keyword: str) -> dict[str, object]:
    ok, message = validate_dork(template)
    query = render_dork(template, sample_keyword) if ok else ""
    warnings = []
    if len(query) > 1500:
        warnings.append("Rendered query is longer than 1,500 characters.")
    return {"ok": ok, "message": message, "query_text": query, "warnings": warnings}


def upsert_search_query(
    conn, keyword_id: int, dork_id: int, query_text: str, queue_id: int | None = None
) -> tuple[int, bool]:
    before = conn.total_changes
    conn.execute(
        """
        INSERT OR IGNORE INTO search_queries(queue_id, keyword_id, dork_id, query_text)
        VALUES (?, ?, ?, ?)
        """,
        (queue_id, keyword_id, dork_id, query_text),
    )
    created = conn.total_changes > before
    row = conn.execute(
        """
        SELECT id
        FROM search_queries
        WHERE keyword_id = ?
          AND dork_id = ?
          AND query_text = ?
          AND (queue_id = ? OR (queue_id IS NULL AND ? IS NULL))
        """,
        (keyword_id, dork_id, query_text, queue_id, queue_id),
    ).fetchone()
    if not row:
        row = conn.execute(
            """
            SELECT id
            FROM search_queries
            WHERE keyword_id = ? AND dork_id = ? AND query_text = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (keyword_id, dork_id, query_text),
        ).fetchone()
    return int(row["id"]), created


def read_rule_form() -> dict[str, object]:
    return {
        "name": request.form.get("name", "").strip(),
        "ioc_type": request.form.get("ioc_type", "email"),
        "pattern": request.form.get("pattern", "").strip(),
        "flags": request.form.get("flags", "").strip(),
        "value_group": request.form.get("value_group", type=int) or 0,
        "input_scope": request.form.get("input_scope", "text"),
        "exclude_pattern": request.form.get("exclude_pattern", "").strip() or None,
        "normalizer": request.form.get("normalizer", "default"),
        "priority": request.form.get("priority", type=int) or 100,
        "enabled": request.form.get("enabled") == "1",
        "description": request.form.get("description", "").strip(),
    }


def query_review_urls(conn, status: str, q: str):
    params: list[object] = []
    where = []
    if status:
        where.append("u.review_status = ?")
        params.append(status)
    if q:
        where.append("(u.url_norm LIKE ? OR u.domain LIKE ?)")
        params.extend([f"%{q}%", f"%{q}%"])
    where_sql = "WHERE " + " AND ".join(where) if where else ""
    return conn.execute(
        f"""
        SELECT u.*,
               GROUP_CONCAT(DISTINCT ds.source_type) AS domain_sources,
               GROUP_CONCAT(DISTINCT sq.query_text) AS queries,
               MIN(us.title) AS title,
               MIN(us.snippet) AS snippet
        FROM urls u
        LEFT JOIN domains d ON d.domain = u.domain
        LEFT JOIN domain_sources ds ON ds.domain_id = d.id
        LEFT JOIN url_sources us ON us.url_id = u.id
        LEFT JOIN search_queries sq ON sq.id = us.search_query_id
        {where_sql}
        GROUP BY u.id
        ORDER BY u.created_at DESC
        LIMIT 500
        """,
        params,
    ).fetchall()


def query_review_domains(conn, status: str, q: str):
    params: list[object] = []
    where = []
    if status:
        where.append("d.review_status = ?")
        params.append(status)
    if q:
        where.append("d.domain LIKE ?")
        params.append(f"%{q}%")
    where_sql = "WHERE " + " AND ".join(where) if where else ""
    return conn.execute(
        f"""
        SELECT d.*,
               GROUP_CONCAT(DISTINCT ds.source_type) AS sources,
               COUNT(DISTINCT u.id) AS url_count
        FROM domains d
        LEFT JOIN domain_sources ds ON ds.domain_id = d.id
        LEFT JOIN urls u ON u.domain = d.domain
        {where_sql}
        GROUP BY d.id
        ORDER BY d.created_at DESC
        LIMIT 500
        """,
        params,
    ).fetchall()


def source_matches(sources: str, wanted: str) -> bool:
    parts = set((sources or "").split(","))
    if wanted == "both":
        return {"google_search", "extracted_from_crawl"}.issubset(parts)
    return wanted in parts


def start_background_worker(app: Flask) -> None:
    if app.config.get("AUTO_WORKER_THREAD"):
        return

    poll_seconds = float(os.environ.get("WORKER_POLL_SECONDS", "3"))

    def worker_loop() -> None:
        while True:
            try:
                message = run_one()
                time.sleep(poll_seconds if message == "No pending job." else 0.3)
            except Exception:
                time.sleep(poll_seconds)

    thread = threading.Thread(target=worker_loop, name="ioc-background-worker", daemon=True)
    thread.start()
    app.config["AUTO_WORKER_THREAD"] = thread
