from __future__ import annotations

import json
import os
import threading
import time
from urllib.parse import urlsplit

from flask import Flask, flash, jsonify, redirect, render_template, request, url_for

from .db import connect, db_path, init_db, is_url_whitelisted, whitelist_match_sql
from .extractor import test_rule, validate_rule
from .normalizers import get_domain, is_media_asset_url, normalize_url
from .worker import (
    TERMINAL_CRAWL_STATUSES,
    approve_url_and_enqueue,
    enqueue_job,
    refresh_keyword_status,
    refresh_queue_status,
    remove_url_from_queue_items,
    reject_url,
    recover_stale_worker_slots,
    run_all,
    run_one,
    upsert_url,
    url_crawl_concurrency,
    url_crawl_worker_threads,
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

    @app.template_filter("bytes_label")
    def bytes_label(value: int | None) -> str:
        if value is None:
            return ""
        size = float(value)
        for unit in ("B", "KB", "MB", "GB"):
            if size < 1024 or unit == "GB":
                return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
            size /= 1024
        return ""

    @app.get("/")
    def dashboard():
        with connect() as conn:
            counts = {
                "queues": scalar(conn, "SELECT COUNT(*) FROM queues"),
                "keywords": scalar(conn, "SELECT COUNT(*) FROM keywords"),
                "search_queries": scalar(conn, "SELECT COUNT(*) FROM search_queries"),
                "pending_jobs": scalar(conn, "SELECT COUNT(*) FROM jobs WHERE status = 'pending'"),
                "pending_review": scalar(
                    conn,
                    """
                    SELECT COUNT(*)
                    FROM urls
                    WHERE review_status = 'pending_review'
                      AND crawl_status NOT IN ('crawled', 'metadata_only')
                    """,
                ),
                "approved_urls": scalar(conn, "SELECT COUNT(*) FROM urls WHERE review_status = 'approved'"),
                "crawled_urls": scalar(
                    conn,
                    "SELECT COUNT(*) FROM urls WHERE crawl_status IN ('crawled', 'metadata_only')",
                ),
                "iocs": scalar(conn, "SELECT COUNT(*) FROM iocs"),
            }
            recent_jobs = conn.execute(
                "SELECT * FROM jobs ORDER BY id DESC LIMIT 8"
            ).fetchall()
            recent_iocs = conn.execute(
                "SELECT * FROM iocs ORDER BY id DESC LIMIT 8"
            ).fetchall()
        return render_template("dashboard.html", counts=counts, recent_jobs=recent_jobs, recent_iocs=recent_iocs)

    @app.route("/whitelist", methods=["GET", "POST"])
    def whitelist():
        with connect() as conn:
            if request.method == "POST":
                raw_urls = request.form.get("urls", "")
                note = request.form.get("note", "").strip() or None
                added = 0
                skipped = 0
                removed_items = 0
                removed_jobs = 0
                ignored_urls = 0
                for item in parse_lines(raw_urls):
                    whitelist_entry = normalize_whitelist_entry(item)
                    if not whitelist_entry:
                        skipped += 1
                        continue
                    before = conn.total_changes
                    conn.execute(
                        """
                        INSERT INTO whitelist_urls(
                          url_raw, url_norm, match_type, match_value, note, enabled
                        )
                        VALUES (?, ?, ?, ?, ?, 1)
                        ON CONFLICT(url_norm) DO UPDATE SET
                            url_raw = excluded.url_raw,
                            match_type = excluded.match_type,
                            match_value = excluded.match_value,
                            note = COALESCE(excluded.note, whitelist_urls.note),
                            enabled = 1,
                            updated_at = CURRENT_TIMESTAMP
                        """,
                        (
                            item,
                            whitelist_entry["url_norm"],
                            whitelist_entry["match_type"],
                            whitelist_entry["match_value"],
                            note,
                        ),
                    )
                    if conn.total_changes > before:
                        added += 1
                    cleanup = apply_url_whitelist(conn, whitelist_entry["url_norm"])
                    ignored_urls += cleanup["ignored_urls"]
                    removed_items += cleanup["removed_items"]
                    removed_jobs += cleanup["removed_jobs"]
                flash(
                    f"Whitelist updated. Added/updated {added} URLs, skipped {skipped}. Ignored {ignored_urls} existing URL rows, removed {removed_items} queue items and {removed_jobs} jobs.",
                    "success" if added or ignored_urls or removed_items else "error",
                )
                return redirect(url_for("whitelist"))

            rows = conn.execute(
                f"""
                SELECT wu.*,
                       (
                         SELECT COUNT(*)
                         FROM urls u
                         WHERE {whitelist_match_sql('u.url_norm', 'wu')}
                       ) AS matched_url_count,
                       (
                         SELECT COUNT(DISTINCT qi.id)
                         FROM url_queue_items qi
                         JOIN urls u ON u.id = qi.url_id
                         WHERE {whitelist_match_sql('u.url_norm', 'wu')}
                       ) AS queue_item_count
                FROM whitelist_urls wu
                ORDER BY wu.enabled DESC, wu.id DESC
                LIMIT 500
                """
            ).fetchall()
        return render_template("whitelist.html", rows=rows)

    @app.post("/whitelist/<int:item_id>/delete")
    def delete_whitelist_url(item_id: int):
        with connect() as conn:
            row = conn.execute("SELECT url_norm FROM whitelist_urls WHERE id = ?", (item_id,)).fetchone()
            if not row:
                flash("Whitelist URL not found.", "error")
            else:
                conn.execute("DELETE FROM whitelist_urls WHERE id = ?", (item_id,))
                flash(f"Removed whitelist URL: {row['url_norm']}", "success")
        return redirect(url_for("whitelist"))

    def handle_queue_post(conn, default_queue_id: int | None = None):
        action = request.form.get("action")
        if action == "create":
            name = request.form.get("name", "").strip()
            queue_type = request.form.get("queue_type", "keyword_search")
            if not name:
                flash("Queue name is required.", "error")
                return redirect(url_for("queue_create"))
            if queue_type not in {"keyword_search", "url_crawl"}:
                flash("Queue type is invalid.", "error")
                return redirect(url_for("queue_create"))
            queue_id = get_or_create_queue(conn, name, queue_type)
            flash(f"Queue created: {name}", "success")
            return redirect(url_for("queue_detail", queue_id=queue_id))

        if action == "add_urls":
            queue_id = request.form.get("queue_id", type=int) or default_queue_id
            queue = get_queue(conn, queue_id, "url_crawl")
            if not queue:
                flash("URL queue is required.", "error")
                return redirect(url_for("queues"))
            added, requeued, skipped = add_urls_to_queue(
                conn, queue_id, request.form.get("targets", "")
            )
            flash(
                f"Added {added} URL/domain targets and requeued {requeued} existing targets in queue '{queue['name']}'. Skipped {skipped}. Start the queue when ready.",
                "success" if added or requeued else "error",
            )
            return redirect(url_for("queue_detail", queue_id=queue_id))

        if action == "link_queues":
            keyword_queue_id = request.form.get("keyword_queue_id", type=int)
            url_queue_id = request.form.get("url_queue_id", type=int)
            ok, message = bind_keyword_queue_to_url_queue(conn, keyword_queue_id, url_queue_id)
            flash(message, "success" if ok else "error")
            if keyword_queue_id:
                return redirect(url_for("queue_detail", queue_id=keyword_queue_id))
            if url_queue_id:
                return redirect(url_for("queue_detail", queue_id=url_queue_id))
            return redirect(url_for("queue_create"))

        flash("Queue action is invalid.", "error")
        return redirect(url_for("queues"))

    @app.get("/queue")
    def queue_singular():
        if request.args.get("queue_id", type=int):
            return redirect(url_for("queue_detail", queue_id=request.args.get("queue_id", type=int)))
        return redirect(url_for("queues"))

    @app.get("/queue/new")
    def queue_create_singular():
        return redirect(url_for("queue_create"))

    @app.get("/queue/<int:queue_id>")
    def queue_detail_singular(queue_id: int):
        return redirect(url_for("queue_detail", queue_id=queue_id))

    @app.route("/queues", methods=["GET", "POST"])
    def queues():
        with connect() as conn:
            if request.method == "POST":
                return handle_queue_post(conn)

            selected_queue_id = request.args.get("queue_id", type=int)
            if selected_queue_id:
                return redirect(url_for("queue_detail", queue_id=selected_queue_id))

            rows = query_queues(conn)
            url_overview = query_url_overview(conn)
        return render_template("queues.html", rows=rows, url_overview=url_overview)

    @app.route("/queues/new", methods=["GET", "POST"])
    def queue_create():
        with connect() as conn:
            if request.method == "POST":
                return handle_queue_post(conn)
            keyword_queues = conn.execute(
                "SELECT * FROM queues WHERE queue_type = 'keyword_search' ORDER BY id DESC"
            ).fetchall()
            url_queues = conn.execute(
                "SELECT * FROM queues WHERE queue_type = 'url_crawl' ORDER BY id DESC"
            ).fetchall()
        return render_template(
            "queue_create.html",
            keyword_queues=keyword_queues,
            url_queues=url_queues,
        )

    @app.route("/queues/<int:queue_id>", methods=["GET", "POST"])
    def queue_detail(queue_id: int):
        with connect() as conn:
            if request.method == "POST":
                return handle_queue_post(conn, default_queue_id=queue_id)

            selected_rows = query_queues(conn, queue_id=queue_id)
            selected_queue = selected_rows[0] if selected_rows else None
            if not selected_queue:
                flash("Queue not found.", "error")
                return redirect(url_for("queues"))
            selected_jobs = conn.execute(
                """
                SELECT * FROM jobs
                WHERE queue_id = ?
                ORDER BY id DESC
                LIMIT 80
                """,
                (queue_id,),
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
                (queue_id,),
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
                (queue_id,),
            ).fetchall()
            selected_crawling_urls = query_crawling_urls(conn, queue_id)
            selected_keyword_search_status = query_keyword_search_status(conn, queue_id)
            selected_keyword_search_results = query_keyword_search_results(conn, queue_id)
        return render_template(
            "queue_detail.html",
            selected_queue=selected_queue,
            selected_jobs=selected_jobs,
            selected_url_items=selected_url_items,
            selected_queries=selected_queries,
            selected_crawling_urls=selected_crawling_urls,
            selected_keyword_search_status=selected_keyword_search_status,
            selected_keyword_search_results=selected_keyword_search_results,
        )

    @app.get("/api/queues/<int:queue_id>/crawling-urls")
    def queue_crawling_urls_api(queue_id: int):
        with connect() as conn:
            queue = conn.execute(
                "SELECT id, name, queue_type, status FROM queues WHERE id = ?",
                (queue_id,),
            ).fetchone()
            if not queue:
                return jsonify({"ok": False, "error": "Queue not found."}), 404
            rows = query_crawling_urls(conn, queue_id)
        return jsonify(
            {
                "ok": True,
                "queue": {
                    "id": queue["id"],
                    "name": queue["name"],
                    "queue_type": queue["queue_type"],
                    "status": queue["status"],
                },
                "count": len(rows),
                "items": [dict(row) for row in rows],
            }
        )

    @app.get("/api/queues/<int:queue_id>/keyword-search-status")
    def queue_keyword_search_status_api(queue_id: int):
        with connect() as conn:
            queue = conn.execute(
                "SELECT id, name, queue_type, status FROM queues WHERE id = ?",
                (queue_id,),
            ).fetchone()
            if not queue:
                return jsonify({"ok": False, "error": "Queue not found."}), 404
            rows = query_keyword_search_status(conn, queue_id)
        return jsonify(
            {
                "ok": True,
                "queue": {
                    "id": queue["id"],
                    "name": queue["name"],
                    "queue_type": queue["queue_type"],
                    "status": queue["status"],
                },
                "count": len(rows),
                "items": [dict(row) for row in rows],
            }
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
                return redirect(url_for("queue_detail", queue_id=queue_id))
            jobs_started, items_started = start_queue_work(conn, queue_id)
        if queue["queue_type"] == "keyword_search" and jobs_started == 0 and items_started == 0:
            flash(
                f"No pending keyword searches are ready in queue '{queue['name']}'. Add new keywords to search again.",
                "error",
            )
            return redirect(url_for("queue_detail", queue_id=queue_id))
        if queue["queue_type"] == "url_crawl" and jobs_started == 0 and items_started == 0:
            flash(
                f"No approved URL/domain items are ready in queue '{queue['name']}'. Approve items in Review first, then start the queue again.",
                "error",
            )
            return redirect(url_for("queue_detail", queue_id=queue_id))
        flash(
            f"Started queue '{queue['name']}'. Requeued {jobs_started} jobs and {items_started} items.",
            "success",
        )
        return redirect(url_for("queue_detail", queue_id=queue_id))

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
        return redirect(url_for("queue_detail", queue_id=queue_id))

    @app.post("/queues/<int:queue_id>/resume")
    def resume_queue(queue_id: int):
        return start_queue(queue_id)

    @app.post("/url-queue-items/<int:item_id>/delete")
    def delete_url_queue_item_route(item_id: int):
        with connect() as conn:
            deleted, queue_id, job_count, error = delete_url_queue_item(conn, item_id)
        if not deleted:
            flash(error, "error")
            return redirect(request.referrer or url_for("queues"))
        flash(
            f"Removed URL item #{item_id} from queue. Removed {job_count} crawl jobs. URL/domain/IOC records were kept.",
            "success",
        )
        return redirect(url_for("queue_detail", queue_id=queue_id))

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
                queries_requeued = 0
                runnable_now = (
                    queue["status"] in {"running", "done"}
                    and output_url_queue_id is not None
                )
                initial_work_status = "pending" if runnable_now else "paused"

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
                            SET status = ?,
                                last_error = NULL,
                                started_at = NULL,
                                finished_at = NULL
                            WHERE id = ?
                              AND status != 'running'
                            """,
                            (initial_work_status, search_query_id),
                        )
                        conn.execute(
                            """
                            UPDATE search_queries
                            SET queue_id = COALESCE(queue_id, ?)
                            WHERE id = ?
                            """,
                            (queue_id, search_query_id),
                        )
                        existing_item = get_search_queue_item(conn, queue_id, search_query_id)
                        queue_item_id = upsert_search_queue_item(
                            conn,
                            queue_id,
                            search_query_id,
                            keyword_id,
                            output_url_queue_id,
                            initial_work_status=initial_work_status,
                        )
                        if existing_item:
                            if reactivate_search_queue_item(
                                conn,
                                queue_item_id,
                                search_query_id,
                                output_url_queue_id,
                                initial_work_status=initial_work_status,
                            ):
                                queries_requeued += 1
                        queue_search_job(
                            conn,
                            queue_id=queue_id,
                            search_query_id=search_query_id,
                            queue_item_id=queue_item_id,
                            output_url_queue_id=output_url_queue_id,
                            initial_status=initial_work_status,
                        )
                    refresh_keyword_status(conn, keyword_id)

                refresh_queue_status(conn, queue_id)

                next_step = (
                    "New queries were queued because the queue is active."
                    if runnable_now
                    else "Start the queue when ready."
                )
                flash(
                    f"Added {keywords_imported} keyword lines, created {queries_created} search queries, and requeued {queries_requeued} existing queries. {next_step}",
                    "success",
                )
                return redirect(url_for("queue_detail", queue_id=queue_id))

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
        source = request.args.get("source", "")
        q = request.args.get("q", "").strip()
        with connect() as conn:
            urls = list(query_review_urls(conn, url_status, q))
            if source:
                urls = [row for row in urls if source_matches(row["sources"] or "", source)]
        return render_template(
            "review.html",
            urls=urls,
            url_status=url_status,
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

    @app.post("/urls/bulk")
    def bulk_review_urls():
        action = request.form.get("bulk_action", "")
        url_ids = unique_ints(request.form.getlist("url_ids"))
        if not url_ids:
            flash("Select at least one URL row.", "error")
            return redirect(request.referrer or url_for("review"))
        changed = 0
        with connect() as conn:
            if action == "approve":
                for url_id in url_ids:
                    if approve_url_and_enqueue(conn, url_id):
                        changed += 1
            elif action == "reject":
                for url_id in url_ids:
                    if reject_url(conn, url_id):
                        changed += 1
            else:
                flash("Bulk URL action is invalid.", "error")
                return redirect(request.referrer or url_for("review"))
        label = "approved" if action == "approve" else "rejected"
        flash(f"Bulk URL action completed: {changed} of {len(url_ids)} selected URLs {label}.", "success")
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
            worker_slots = conn.execute(
                """
                SELECT ws.*, q.name AS queue_name, q.queue_type
                FROM worker_slots ws
                LEFT JOIN queues q ON q.id = ws.queue_id
                ORDER BY ws.worker_type, ws.slot_key
                """
            ).fetchall()
        return render_template(
            "jobs.html",
            rows=rows,
            worker_slots=worker_slots,
            url_crawl_concurrency=url_crawl_concurrency(),
        )

    @app.get("/iocs")
    def iocs():
        selected_id = request.args.get("ioc_id", type=int)
        ioc_type = request.args.get("type", "")
        q = request.args.get("q", "").strip()
        with connect() as conn:
            rows = query_iocs(conn, ioc_type, q)
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
        return render_template(
            "iocs.html",
            rows=rows,
            selected=selected,
            sources=sources,
            ioc_type=ioc_type,
            q=q,
        )

    @app.post("/iocs/<int:ioc_id>/delete")
    def delete_ioc(ioc_id: int):
        with connect() as conn:
            row = conn.execute("SELECT * FROM iocs WHERE id = ?", (ioc_id,)).fetchone()
            if not row:
                flash("IOC not found.", "error")
                return redirect(url_for("iocs", type=request.form.get("type", ""), q=request.form.get("q", "")))
            source_count = scalar(conn, "SELECT COUNT(*) FROM ioc_sources WHERE ioc_id = ?", (ioc_id,))
            conn.execute("DELETE FROM ioc_sources WHERE ioc_id = ?", (ioc_id,))
            conn.execute("DELETE FROM iocs WHERE id = ?", (ioc_id,))
        flash(
            f"Deleted IOC #{ioc_id} and {source_count} evidence rows. Source URLs were kept.",
            "success",
        )
        return redirect(url_for("iocs", type=request.form.get("type", ""), q=request.form.get("q", "")))

    @app.post("/iocs/bulk-delete")
    def bulk_delete_iocs():
        ioc_ids = unique_ints(request.form.getlist("ioc_ids"))
        if not ioc_ids:
            flash("Select at least one IOC row.", "error")
            return redirect(url_for("iocs", type=request.form.get("type", ""), q=request.form.get("q", "")))

        placeholders = ",".join("?" for _ in ioc_ids)
        with connect() as conn:
            existing_count = scalar(
                conn,
                f"SELECT COUNT(*) FROM iocs WHERE id IN ({placeholders})",
                tuple(ioc_ids),
            )
            source_count = scalar(
                conn,
                f"SELECT COUNT(*) FROM ioc_sources WHERE ioc_id IN ({placeholders})",
                tuple(ioc_ids),
            )
            conn.execute(f"DELETE FROM ioc_sources WHERE ioc_id IN ({placeholders})", tuple(ioc_ids))
            conn.execute(f"DELETE FROM iocs WHERE id IN ({placeholders})", tuple(ioc_ids))

        flash(
            f"Deleted {existing_count} selected IOCs and {source_count} evidence rows. Source URLs were kept.",
            "success",
        )
        return redirect(url_for("iocs", type=request.form.get("type", ""), q=request.form.get("q", "")))

    if start_worker:
        start_background_worker(app)

    return app


NAV = [
    ("Dashboard", "dashboard"),
    ("Queues", "queues"),
    ("Keywords", "keywords"),
    ("Search Dorks", "dorks"),
    ("Whitelist", "whitelist"),
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


def unique_ints(values: list[str]) -> list[int]:
    seen = set()
    rows = []
    for value in values:
        try:
            item = int(value)
        except (TypeError, ValueError):
            continue
        if item in seen:
            continue
        seen.add(item)
        rows.append(item)
    return rows


def normalize_whitelist_entry(value: str) -> dict[str, str] | None:
    raw = (value or "").strip()
    if not raw:
        return None

    match_type = "prefix" if raw.endswith("*") else "exact"
    candidate = raw[:-1].strip() if match_type == "prefix" else raw
    if not candidate:
        return None

    url_norm = normalize_url(candidate)
    if not url_norm:
        return None
    match_value = url_norm
    if match_type == "prefix":
        parts = urlsplit(url_norm)
        if not parts.path and not parts.query:
            match_value = f"{url_norm}/"

    return {
        "url_raw": raw,
        "url_norm": url_norm,
        "match_type": match_type,
        "match_value": match_value,
    }


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

    running_items = conn.execute(
        """
        SELECT id
        FROM search_queue_items
        WHERE keyword_id = ?
          AND status = 'running'
        """,
        (keyword_id,),
    ).fetchall()
    if running_items:
        return False, 0, 0, "Cannot delete keyword while a queue item is running. Stop it and wait for the current browser session to finish."

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
    affected_queue_ids = {
        int(row["queue_id"])
        for row in conn.execute(
            """
            SELECT DISTINCT queue_id
            FROM search_queue_items
            WHERE keyword_id = ?
            """,
            (keyword_id,),
        ).fetchall()
        if row["queue_id"] is not None
    }

    if query_ids:
        query_placeholders = ",".join("?" for _ in query_ids)
        source_params = [keyword_id, *query_ids]
        queue_item_rows = conn.execute(
            f"""
            SELECT id
            FROM search_queue_items
            WHERE keyword_id = ? OR search_query_id IN ({query_placeholders})
            """,
            source_params,
        ).fetchall()
        queue_item_ids = [int(row["id"]) for row in queue_item_rows]
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
            UPDATE url_queue_items
            SET source_search_query_id = NULL
            WHERE source_search_query_id IN ({query_placeholders})
            """,
            query_ids,
        )
        if queue_item_ids:
            item_placeholders = ",".join("?" for _ in queue_item_ids)
            conn.execute(
                f"""
                UPDATE url_queue_items
                SET source_search_queue_item_id = NULL
                WHERE source_search_queue_item_id IN ({item_placeholders})
                """,
                queue_item_ids,
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
        conn.execute(
            f"""
            DELETE FROM search_queue_items
            WHERE keyword_id = ? OR search_query_id IN ({query_placeholders})
            """,
            source_params,
        )
        conn.execute(f"DELETE FROM search_queries WHERE id IN ({query_placeholders})", query_ids)
    else:
        conn.execute("UPDATE url_sources SET keyword_id = NULL WHERE keyword_id = ?", (keyword_id,))
        queue_item_rows = conn.execute(
            "SELECT id FROM search_queue_items WHERE keyword_id = ?",
            (keyword_id,),
        ).fetchall()
        queue_item_ids = [int(row["id"]) for row in queue_item_rows]
        if queue_item_ids:
            item_placeholders = ",".join("?" for _ in queue_item_ids)
            conn.execute(
                f"""
                UPDATE url_queue_items
                SET source_search_queue_item_id = NULL
                WHERE source_search_queue_item_id IN ({item_placeholders})
                """,
                queue_item_ids,
            )
        conn.execute("DELETE FROM search_queue_items WHERE keyword_id = ?", (keyword_id,))

    conn.execute("DELETE FROM keywords WHERE id = ?", (keyword_id,))
    for queue_id in affected_queue_ids:
        refresh_queue_status(conn, queue_id)
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


def query_queues(conn, queue_id: int | None = None):
    where_sql = "WHERE q.id = ?" if queue_id else ""
    params = (queue_id,) if queue_id else ()
    return conn.execute(
        f"""
        SELECT q.*,
               COUNT(DISTINCT j.id) AS job_count,
               COUNT(DISTINCT CASE WHEN j.status = 'pending' THEN j.id END) AS pending_jobs,
               COUNT(DISTINCT CASE WHEN j.status = 'running' THEN j.id END) AS running_jobs,
               COUNT(DISTINCT CASE WHEN j.status = 'paused' THEN j.id END) AS paused_jobs,
               COUNT(DISTINCT CASE WHEN j.status = 'done' THEN j.id END) AS done_jobs,
               COUNT(DISTINCT CASE WHEN j.status = 'failed' THEN j.id END) AS failed_jobs,
               COUNT(DISTINCT sqi.id) AS search_query_count,
               COUNT(DISTINCT uqi.id) AS url_item_count,
               COUNT(DISTINCT CASE WHEN q.queue_type = 'url_crawl' THEN queue_url.id END) AS queue_url_total,
               COUNT(DISTINCT CASE
                 WHEN q.queue_type = 'url_crawl'
                  AND (queue_url.crawl_status = 'crawling' OR uqi.status = 'running')
                 THEN queue_url.id
               END) AS queue_url_crawling,
               COUNT(DISTINCT CASE
                 WHEN q.queue_type = 'url_crawl'
                  AND uqi.status IN ('pending_review', 'pending', 'paused')
                  AND queue_url.review_status != 'rejected'
                  AND queue_url.crawl_status NOT IN ('crawled', 'metadata_only')
                 THEN queue_url.id
               END) AS queue_url_pending,
               COUNT(DISTINCT CASE
                 WHEN q.queue_type = 'url_crawl'
                  AND queue_url.review_status = 'rejected'
                 THEN queue_url.id
               END) AS queue_url_rejected,
               qr.url_queue_id AS output_url_queue_id,
               uq.name AS output_url_queue_name
        FROM queues q
        LEFT JOIN jobs j ON j.queue_id = q.id
        LEFT JOIN search_queue_items sqi ON sqi.queue_id = q.id
        LEFT JOIN url_queue_items uqi ON uqi.queue_id = q.id
        LEFT JOIN urls queue_url ON queue_url.id = uqi.url_id
        LEFT JOIN queue_routes qr ON qr.keyword_queue_id = q.id
        LEFT JOIN queues uq ON uq.id = qr.url_queue_id
        {where_sql}
        GROUP BY q.id
        ORDER BY q.id DESC
        LIMIT 200
        """,
        params,
    ).fetchall()


def query_url_overview(conn):
    return conn.execute(
        """
        SELECT
          COUNT(*) AS total_urls,
          SUM(CASE WHEN crawl_status = 'crawling' THEN 1 ELSE 0 END) AS crawling_urls,
          SUM(
            CASE
              WHEN review_status = 'pending_review'
               AND crawl_status NOT IN ('crawled', 'metadata_only')
              THEN 1 ELSE 0
            END
          ) AS pending_urls,
          SUM(CASE WHEN review_status = 'rejected' THEN 1 ELSE 0 END) AS rejected_urls
        FROM urls
        """
    ).fetchone()


def query_crawling_urls(conn, queue_id: int):
    return conn.execute(
        """
        SELECT qi.id AS queue_item_id,
               qi.status AS queue_status,
               qi.started_at AS queue_started_at,
               qi.error AS queue_error,
               u.id AS url_id,
               u.domain,
               u.url_norm,
               u.review_status,
               u.crawl_status,
               u.status_code,
               u.fetch_method,
               u.crawl_error,
               j.id AS job_id,
               j.attempts,
               j.started_at AS job_started_at,
               source_q.name AS source_queue_name
        FROM url_queue_items qi
        JOIN urls u ON u.id = qi.url_id
        LEFT JOIN jobs j ON j.type = 'crawl_url'
          AND j.queue_id = qi.queue_id
          AND j.dedupe_key = 'queue:' || qi.queue_id || ':crawl:' || qi.url_id
          AND j.status = 'running'
        LEFT JOIN queues source_q ON source_q.id = qi.source_queue_id
        WHERE qi.queue_id = ?
          AND (
            qi.status = 'running'
            OR u.crawl_status = 'crawling'
            OR j.id IS NOT NULL
          )
        ORDER BY COALESCE(qi.started_at, j.started_at, qi.created_at) DESC, qi.id DESC
        LIMIT 200
        """,
        (queue_id,),
    ).fetchall()


def query_keyword_search_status(conn, queue_id: int):
    return conn.execute(
        """
        SELECT sqi.id AS queue_item_id,
               sqi.status AS queue_item_status,
               sqi.started_at AS queue_item_started_at,
               sqi.error AS queue_item_error,
               sq.id AS search_query_id,
               sq.query_text,
               sq.status AS search_status,
               sq.result_count,
               sq.page_count,
               sq.last_error AS search_error,
               sq.started_at AS search_started_at,
               k.text AS keyword_text,
               output_q.id AS output_url_queue_id,
               output_q.name AS output_url_queue_name,
               j.id AS job_id,
               j.status AS job_status,
               j.attempts,
               j.started_at AS job_started_at,
               j.error AS job_error
        FROM search_queue_items sqi
        JOIN search_queries sq ON sq.id = sqi.search_query_id
        JOIN keywords k ON k.id = sqi.keyword_id
        LEFT JOIN queues output_q ON output_q.id = sqi.output_url_queue_id
        LEFT JOIN jobs j ON j.type = 'search_query'
          AND j.queue_id = sqi.queue_id
          AND j.dedupe_key = 'queue:' || sqi.queue_id || ':search:' || sqi.search_query_id
          AND j.status IN ('pending', 'running')
        WHERE sqi.queue_id = ?
          AND (
            sqi.status IN ('pending', 'running')
            OR sq.status IN ('pending', 'running')
            OR j.id IS NOT NULL
          )
        ORDER BY COALESCE(sqi.started_at, sq.started_at, j.started_at, sqi.created_at) DESC, sqi.id DESC
        LIMIT 200
        """,
        (queue_id,),
    ).fetchall()


def query_keyword_search_results(conn, queue_id: int):
    return conn.execute(
        """
        SELECT us.id AS source_id,
               us.created_at,
               us.title,
               us.snippet,
               us.rank,
               us.page_no,
               u.id AS url_id,
               u.url_norm,
               u.domain,
               u.review_status,
               u.crawl_status,
               u.status_code,
               u.fetch_method,
               k.text AS keyword_text,
               sq.query_text,
               output_q.id AS output_url_queue_id,
               output_q.name AS output_url_queue_name,
               uqi.id AS output_queue_item_id,
               uqi.status AS output_queue_item_status
        FROM url_sources us
        JOIN urls u ON u.id = us.url_id
        LEFT JOIN keywords k ON k.id = us.keyword_id
        LEFT JOIN search_queries sq ON sq.id = us.search_query_id
        LEFT JOIN search_queue_items sqi ON sqi.queue_id = us.queue_id
          AND sqi.search_query_id = us.search_query_id
        LEFT JOIN queues output_q ON output_q.id = sqi.output_url_queue_id
        LEFT JOIN url_queue_items uqi ON uqi.url_id = us.url_id
          AND uqi.queue_id = sqi.output_url_queue_id
        WHERE us.queue_id = ?
          AND us.source_type = 'google_search'
        ORDER BY us.created_at DESC,
                 COALESCE(us.page_no, 0),
                 COALESCE(us.rank, 0),
                 us.id DESC
        LIMIT 500
        """,
        (queue_id,),
    ).fetchall()


def query_iocs(conn, ioc_type: str, q: str):
    params: list[object] = []
    where = []
    if ioc_type:
        where.append("i.type = ?")
        params.append(ioc_type)
    if q:
        like = f"%{q}%"
        where.append(
            """
            (
              i.value_norm LIKE ?
              OR i.value_raw LIKE ?
              OR EXISTS (
                SELECT 1
                FROM ioc_sources s2
                JOIN urls u2 ON u2.id = s2.source_url_id
                LEFT JOIN extraction_rules r2 ON r2.id = s2.extraction_rule_id
                WHERE s2.ioc_id = i.id
                  AND (
                    u2.domain LIKE ?
                    OR u2.url_norm LIKE ?
                    OR r2.name LIKE ?
                    OR s2.evidence_text LIKE ?
                  )
              )
            )
            """
        )
        params.extend([like, like, like, like, like, like])
    where_sql = "WHERE " + " AND ".join(where) if where else ""
    return conn.execute(
        f"""
        SELECT i.*,
               COUNT(DISTINCT s.id) AS source_count,
               GROUP_CONCAT(DISTINCT u.domain) AS source_domains,
               GROUP_CONCAT(DISTINCT u.url_norm) AS source_urls,
               GROUP_CONCAT(DISTINCT r.name) AS rule_names
        FROM iocs i
        LEFT JOIN ioc_sources s ON s.ioc_id = i.id
        LEFT JOIN urls u ON u.id = s.source_url_id
        LEFT JOIN extraction_rules r ON r.id = s.extraction_rule_id
        {where_sql}
        GROUP BY i.id
        ORDER BY i.id DESC
        LIMIT 500
        """,
        params,
    ).fetchall()


def start_queue_work(conn, queue_id: int) -> tuple[int, int]:
    queue = conn.execute("SELECT * FROM queues WHERE id = ?", (queue_id,)).fetchone()
    if not queue:
        return 0, 0
    if queue["queue_type"] == "keyword_search" and not get_output_url_queue(conn, queue_id):
        return 0, 0

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
        if not item_rows:
            refresh_queue_status(conn, queue_id)
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
        search_query_ids = [int(item["search_query_id"]) for item in item_rows]
        if search_query_ids:
            placeholders = ",".join("?" for _ in search_query_ids)
            conn.execute(
                f"""
                UPDATE search_queries
                SET status = 'pending',
                    last_error = NULL,
                    started_at = NULL,
                    finished_at = NULL
                WHERE id IN ({placeholders})
                  AND status != 'running'
                """,
                search_query_ids,
            )
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
        url_count = conn.execute(
            """
            UPDATE url_queue_items
            SET status = 'pending',
                started_at = NULL,
                finished_at = NULL,
                error = NULL
            WHERE queue_id = ?
              AND status IN ('pending_review', 'pending', 'paused', 'failed')
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
        if jobs_started == 0 and url_count == 0:
            refresh_queue_status(conn, queue_id)
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


def crawl_job_dedupe_key(queue_id: int, url_id: int) -> str:
    return f"queue:{queue_id}:crawl:{url_id}"


def reset_url_for_manual_rerun(conn, url_id: int) -> None:
    conn.execute(
        """
        UPDATE urls
        SET crawl_status = 'not_crawled',
            final_url = NULL,
            status_code = NULL,
            content_type = NULL,
            content_length = NULL,
            fetch_method = NULL,
            crawl_error = NULL,
            html = NULL,
            crawled_at = NULL
        WHERE id = ?
          AND crawl_status != 'crawling'
        """,
        (url_id,),
    )


def requeue_url_queue_item(conn, queue_id: int, url_id: int, queue_item_id: int) -> bool:
    row = conn.execute(
        """
        SELECT qi.status, u.crawl_status
        FROM url_queue_items qi
        JOIN urls u ON u.id = qi.url_id
        WHERE qi.id = ?
        """,
        (queue_item_id,),
    ).fetchone()
    if not row or row["status"] == "running":
        return False
    if row["crawl_status"] in TERMINAL_CRAWL_STATUSES:
        remove_url_from_queue_items(conn, url_id)
        return False

    running_job = conn.execute(
        """
        SELECT id
        FROM jobs
        WHERE type = 'crawl_url'
          AND dedupe_key = ?
          AND status = 'running'
        """,
        (crawl_job_dedupe_key(queue_id, url_id),),
    ).fetchone()
    if running_job:
        return False

    reset_url_for_manual_rerun(conn, url_id)
    conn.execute(
        """
        UPDATE url_queue_items
        SET status = 'paused',
            error = NULL,
            started_at = NULL,
            finished_at = NULL
        WHERE id = ?
        """,
        (queue_item_id,),
    )
    enqueue_job(
        conn,
        "crawl_url",
        {"url_id": url_id, "url_queue_item_id": queue_item_id},
        crawl_job_dedupe_key(queue_id, url_id),
        queue_id=queue_id,
        initial_status="paused",
    )
    return True


def add_urls_to_queue(conn, queue_id: int, raw_targets: str) -> tuple[int, int, int]:
    added = 0
    requeued = 0
    skipped = 0
    for item in parse_lines(raw_targets):
        url_norm = normalize_url(item)
        if not url_norm or is_media_asset_url(url_norm) or is_url_whitelisted(conn, url_norm):
            skipped += 1
            continue
        domain = get_domain(url_norm)
        if not domain:
            skipped += 1
            continue

        url_id = upsert_url(conn, item, url_norm, domain, "manual_queue")
        target = conn.execute("SELECT crawl_status FROM urls WHERE id = ?", (url_id,)).fetchone()
        if target and target["crawl_status"] in TERMINAL_CRAWL_STATUSES:
            remove_url_from_queue_items(conn, url_id)
            skipped += 1
            continue
        conn.execute("UPDATE urls SET review_status = 'approved' WHERE id = ?", (url_id,))
        existing_item = conn.execute(
            """
            SELECT id
            FROM url_queue_items
            WHERE queue_id = ? AND url_id = ?
            """,
            (queue_id, url_id),
        ).fetchone()
        if existing_item:
            if requeue_url_queue_item(conn, queue_id, url_id, int(existing_item["id"])):
                requeued += 1
            else:
                skipped += 1
            continue

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
            if item_row:
                requeue_url_queue_item(conn, queue_id, url_id, int(item_row["id"]))
        else:
            skipped += 1
    refresh_queue_status(conn, queue_id)
    return added, requeued, skipped


def apply_url_whitelist(conn, url_norm: str) -> dict[str, int]:
    whitelist = conn.execute(
        "SELECT * FROM whitelist_urls WHERE url_norm = ? AND enabled = 1",
        (url_norm,),
    ).fetchone()
    if not whitelist:
        return {"ignored_urls": 0, "removed_items": 0, "removed_jobs": 0}

    match_value = whitelist["match_value"] or whitelist["url_norm"]
    if whitelist["match_type"] == "prefix":
        params: list[object] = [match_value, len(match_value), match_value]
        where = "(url_norm = ? OR substr(url_norm, 1, ?) = ?)"
        if match_value.endswith("/"):
            root_match_value = match_value.rstrip("/")
            where = f"({where} OR url_norm = ? OR substr(url_norm, 1, ?) = ?)"
            params.extend([root_match_value, len(match_value), f"{root_match_value}?"])
    else:
        params = [whitelist["url_norm"]]
        where = "url_norm = ?"

    rows = conn.execute(f"SELECT id FROM urls WHERE {where}", params).fetchall()
    if not rows:
        return {"ignored_urls": 0, "removed_items": 0, "removed_jobs": 0}
    url_ids = [int(row["id"]) for row in rows]
    placeholders = ",".join("?" for _ in url_ids)
    ignored_urls = conn.execute(
        f"""
        UPDATE urls
        SET review_status = 'ignored_whitelist'
        WHERE id IN ({placeholders})
          AND review_status != 'ignored_whitelist'
        """,
        url_ids,
    ).rowcount
    removed_items = 0
    removed_jobs = 0
    for url_id in url_ids:
        item_count, job_count = remove_url_from_queue_items(conn, url_id)
        removed_items += item_count
        removed_jobs += job_count
    return {
        "ignored_urls": max(ignored_urls, 0),
        "removed_items": removed_items,
        "removed_jobs": removed_jobs,
    }


def delete_url_queue_item(conn, item_id: int) -> tuple[bool, int | None, int, str]:
    item = conn.execute(
        """
        SELECT qi.*, u.url_norm
        FROM url_queue_items qi
        JOIN urls u ON u.id = qi.url_id
        WHERE qi.id = ?
        """,
        (item_id,),
    ).fetchone()
    if not item:
        return False, None, 0, "URL queue item not found."
    queue_id = int(item["queue_id"])
    url_id = int(item["url_id"])
    if item["status"] == "running":
        return (
            False,
            queue_id,
            0,
            "Cannot remove this URL item while it is running. Stop the queue and wait for the current crawl job to finish.",
        )

    dedupe_key = crawl_job_dedupe_key(queue_id, url_id)
    running_job = conn.execute(
        """
        SELECT id
        FROM jobs
        WHERE type = 'crawl_url'
          AND dedupe_key = ?
          AND status = 'running'
        """,
        (dedupe_key,),
    ).fetchone()
    if running_job:
        return (
            False,
            queue_id,
            0,
            "Cannot remove this URL item while its crawl job is running. Stop the queue and wait for the current job to finish.",
        )

    conn.execute(
        """
        UPDATE url_queue_items
        SET source_url_queue_item_id = NULL
        WHERE source_url_queue_item_id = ?
        """,
        (item_id,),
    )
    job_count = conn.execute(
        """
        DELETE FROM jobs
        WHERE type = 'crawl_url'
          AND dedupe_key = ?
          AND status != 'running'
        """,
        (dedupe_key,),
    ).rowcount
    conn.execute("DELETE FROM url_queue_items WHERE id = ?", (item_id,))
    refresh_queue_status(conn, queue_id)
    return True, queue_id, max(job_count, 0), ""


def upsert_search_queue_item(
    conn,
    queue_id: int,
    search_query_id: int,
    keyword_id: int,
    output_url_queue_id: int | None = None,
    initial_work_status: str = "paused",
) -> int:
    conn.execute(
        """
        INSERT OR IGNORE INTO search_queue_items(
          queue_id, search_query_id, keyword_id, output_url_queue_id, status
        )
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            queue_id,
            search_query_id,
            keyword_id,
            output_url_queue_id,
            initial_work_status,
        ),
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


def get_search_queue_item(conn, queue_id: int, search_query_id: int):
    return conn.execute(
        """
        SELECT *
        FROM search_queue_items
        WHERE queue_id = ? AND search_query_id = ?
        """,
        (queue_id, search_query_id),
    ).fetchone()


def reactivate_search_queue_item(
    conn,
    queue_item_id: int,
    search_query_id: int,
    output_url_queue_id: int | None,
    initial_work_status: str = "paused",
) -> bool:
    row = conn.execute(
        "SELECT status FROM search_queue_items WHERE id = ?",
        (queue_item_id,),
    ).fetchone()
    if not row or row["status"] == "running":
        return False

    conn.execute(
        """
        UPDATE search_queue_items
        SET status = ?,
            output_url_queue_id = COALESCE(?, output_url_queue_id),
            result_count = 0,
            page_count = 0,
            error = NULL,
            started_at = NULL,
            finished_at = NULL
        WHERE id = ?
        """,
        (initial_work_status, output_url_queue_id, queue_item_id),
    )
    conn.execute(
        """
        UPDATE search_queries
        SET status = ?,
            result_count = 0,
            page_count = 0,
            last_error = NULL,
            started_at = NULL,
            finished_at = NULL
        WHERE id = ?
          AND status != 'running'
        """,
        (initial_work_status, search_query_id),
    )
    return True


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
    where.append("u.crawl_status NOT IN ('crawled', 'metadata_only')")
    where.append("u.review_status != 'ignored_whitelist'")
    where.append(
        f"""
        NOT EXISTS (
          SELECT 1
          FROM whitelist_urls wu
          WHERE wu.enabled = 1
            AND {whitelist_match_sql('u.url_norm', 'wu')}
        )
        """
    )
    if q:
        where.append("(u.url_norm LIKE ? OR u.domain LIKE ?)")
        params.extend([f"%{q}%", f"%{q}%"])
    where_sql = "WHERE " + " AND ".join(where) if where else ""
    rows = conn.execute(
        f"""
        SELECT u.*,
               GROUP_CONCAT(DISTINCT us.source_type) AS sources,
               GROUP_CONCAT(DISTINCT sq.query_text) AS queries,
               MIN(us.title) AS title,
               MIN(us.snippet) AS snippet
        FROM urls u
        LEFT JOIN url_sources us ON us.url_id = u.id
        LEFT JOIN search_queries sq ON sq.id = us.search_query_id
        {where_sql}
        GROUP BY u.id
        ORDER BY u.created_at DESC
        LIMIT 2000
        """,
        params,
    ).fetchall()
    return [row for row in rows if not is_media_asset_url(row["url_norm"])][:500]


def source_matches(sources: str, wanted: str) -> bool:
    parts = set((sources or "").split(","))
    if wanted == "both":
        return {"google_search", "extracted_from_crawl"}.issubset(parts)
    return wanted in parts


def start_background_worker(app: Flask) -> None:
    if app.config.get("AUTO_WORKER_THREADS"):
        return

    poll_seconds = float(os.environ.get("WORKER_POLL_SECONDS", "3"))
    maintenance_seconds = float(os.environ.get("WORKER_MAINTAINER_SECONDS", str(max(10.0, poll_seconds * 2))))
    threads: list[threading.Thread] = []

    def worker_loop(
        job_types: tuple[str, ...],
        worker_slot_key: str,
        worker_type: str,
        idle_delay: float = 0.3,
    ) -> None:
        while True:
            try:
                message = run_one(
                    job_types=job_types,
                    worker_slot_key=worker_slot_key,
                    worker_type=worker_type,
                )
                time.sleep(poll_seconds if message == "No pending job." else idle_delay)
            except Exception:
                time.sleep(poll_seconds)

    def maintainer_loop() -> None:
        while True:
            try:
                recover_stale_worker_slots()
            except Exception:
                pass
            time.sleep(maintenance_seconds)

    search_thread = threading.Thread(
        target=worker_loop,
        args=(("search_query",), "search_query:1", "search_query"),
        name="ioc-search-worker-1",
        daemon=True,
    )
    search_thread.start()
    threads.append(search_thread)

    for index in range(1, url_crawl_worker_threads() + 1):
        thread = threading.Thread(
            target=worker_loop,
            args=(("crawl_url",), f"crawl_url:{index}", "crawl_url"),
            name=f"ioc-crawl-worker-{index}",
            daemon=True,
        )
        thread.start()
        threads.append(thread)

    maintainer_thread = threading.Thread(
        target=maintainer_loop,
        name="ioc-worker-maintainer",
        daemon=True,
    )
    maintainer_thread.start()
    threads.append(maintainer_thread)

    app.config["AUTO_WORKER_THREADS"] = threads
    app.config["AUTO_WORKER_THREAD"] = threads[0] if threads else True
