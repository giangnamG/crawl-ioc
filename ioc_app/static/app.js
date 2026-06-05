(function () {
  const KEY_PREFIX = "ioc-investigator:scroll:";
  const RESTORE_TTL_MS = 30000;

  if ("scrollRestoration" in window.history) {
    window.history.scrollRestoration = "manual";
  }

  function keyForPath(pathname) {
    return `${KEY_PREFIX}${pathname}`;
  }

  function saveScrollFor(pathname) {
    if (!pathname) {
      return;
    }
    const payload = {
      x: window.scrollX,
      y: window.scrollY,
      ts: Date.now(),
    };
    try {
      window.sessionStorage.setItem(keyForPath(pathname), JSON.stringify(payload));
    } catch (_error) {
      // Ignore private-mode/sessionStorage failures.
    }
  }

  function saveCurrentScroll() {
    saveScrollFor(window.location.pathname);
  }

  function sameOriginPath(urlText) {
    try {
      const url = new URL(urlText, window.location.href);
      if (url.origin !== window.location.origin) {
        return "";
      }
      return url.pathname;
    } catch (_error) {
      return "";
    }
  }

  document.addEventListener(
    "submit",
    function (event) {
      const form = event.target;
      if (!(form instanceof HTMLFormElement)) {
        return;
      }
      saveCurrentScroll();

      const actionPath = sameOriginPath(form.getAttribute("action") || window.location.href);
      if (actionPath && actionPath === window.location.pathname) {
        saveScrollFor(actionPath);
      }
    },
    true
  );

  document.addEventListener(
    "click",
    function (event) {
      if (!(event.target instanceof Element)) {
        return;
      }
      const submitButton = event.target.closest("button[type='submit'], button:not([type])");
      if (submitButton && submitButton.form) {
        saveCurrentScroll();
        return;
      }

      const link = event.target.closest("a.button, a.tab-pill");
      if (!link || link.target && link.target !== "_self") {
        return;
      }

      const targetPath = sameOriginPath(link.href);
      if (targetPath && targetPath === window.location.pathname) {
        saveCurrentScroll();
      }
    },
    true
  );

  document.addEventListener("change", function (event) {
    const toggle = event.target;
    if (!(toggle instanceof HTMLInputElement) || !toggle.classList.contains("bulk-toggle")) {
      return;
    }
    const targetName = toggle.getAttribute("data-bulk-target");
    if (!targetName) {
      return;
    }
    const table = toggle.closest("table");
    if (!table) {
      return;
    }
    const escapedName = window.CSS && CSS.escape
      ? CSS.escape(targetName)
      : targetName.replace(/"/g, '\\"');
    table
      .querySelectorAll(`tbody input[type="checkbox"][name="${escapedName}"]:not(:disabled)`)
      .forEach(function (checkbox) {
        checkbox.checked = toggle.checked;
      });
  });

  function textForPattern(row, field) {
    if (!(row instanceof HTMLElement)) {
      return "";
    }
    if (field === "all") {
      return Object.keys(row.dataset)
        .filter(function (key) {
          return key.indexOf("pattern") === 0;
        })
        .map(function (key) {
          return row.dataset[key] || "";
        })
        .join("\n");
    }
    return row.dataset[`pattern${field.charAt(0).toUpperCase()}${field.slice(1)}`] || "";
  }

  function matcherForPattern(patternText) {
    const raw = (patternText || "").trim();
    if (!raw) {
      return null;
    }
    try {
      const regex = new RegExp(raw, "i");
      return function (value) {
        return regex.test(value || "");
      };
    } catch (_error) {
      const needle = raw.toLowerCase();
      return function (value) {
        return (value || "").toLowerCase().includes(needle);
      };
    }
  }

  function setPatternFeedback(bar, message) {
    const feedback = bar.querySelector("[data-pattern-feedback]");
    if (feedback) {
      feedback.textContent = message;
    }
  }

  document.addEventListener("click", function (event) {
    if (!(event.target instanceof Element)) {
      return;
    }
    const applyButton = event.target.closest("[data-pattern-apply]");
    const clearButton = event.target.closest("[data-pattern-clear]");
    if (!applyButton && !clearButton) {
      return;
    }

    const bar = event.target.closest("[data-pattern-scope]");
    if (!bar) {
      return;
    }
    const panel = bar.closest(".panel");
    const table = panel ? panel.querySelector("table[data-pattern-table], table.review-table") : null;
    if (!table) {
      return;
    }
    const checkboxes = Array.from(
      table.querySelectorAll('tbody input[type="checkbox"]:not(:disabled)')
    );

    if (clearButton) {
      checkboxes.forEach(function (checkbox) {
        checkbox.checked = false;
      });
      setPatternFeedback(bar, `Cleared ${checkboxes.length} rows.`);
      return;
    }

    const input = bar.querySelector("[data-pattern-input]");
    const field = bar.querySelector("[data-pattern-field]");
    const matcher = matcherForPattern(input ? input.value : "");
    if (!matcher) {
      setPatternFeedback(bar, "Enter a pattern first.");
      return;
    }

    let matched = 0;
    checkboxes.forEach(function (checkbox) {
      const row = checkbox.closest("tr");
      const value = textForPattern(row, field ? field.value : "all");
      const isMatch = matcher(value);
      checkbox.checked = isMatch;
      if (isMatch) {
        matched += 1;
      }
    });
    setPatternFeedback(bar, `Ticked ${matched} rows.`);
  });

  function escapeHtml(value) {
    return String(value == null ? "" : value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  let lastEvidenceTrigger = null;

  function evidenceModalElements() {
    const modal = document.querySelector("[data-ioc-evidence-modal]");
    if (!modal) {
      return null;
    }
    return {
      modal,
      closeButton: modal.querySelector("[data-ioc-evidence-close]"),
      title: modal.querySelector("#ioc-evidence-modal-title"),
      type: modal.querySelector("[data-ioc-evidence-type]"),
      value: modal.querySelector("[data-ioc-evidence-value]"),
      count: modal.querySelector("[data-ioc-evidence-count]"),
      collected: modal.querySelector("[data-ioc-evidence-collected]"),
      status: modal.querySelector("[data-ioc-evidence-status]"),
      list: modal.querySelector("[data-ioc-evidence-list]"),
    };
  }

  function setEvidenceStatus(elements, message, isError) {
    if (!elements.status) {
      return;
    }
    elements.status.textContent = message || "";
    elements.status.hidden = !message;
    elements.status.classList.toggle("error", Boolean(isError));
  }

  function openEvidenceModal(trigger) {
    const elements = evidenceModalElements();
    if (!elements) {
      return null;
    }
    lastEvidenceTrigger = trigger || null;
    elements.modal.hidden = false;
    document.body.classList.add("modal-open");
    if (elements.title) {
      elements.title.textContent = `Evidence For IOC #${trigger ? trigger.dataset.iocId || "" : ""}`;
    }
    if (elements.type) {
      elements.type.textContent = trigger ? trigger.dataset.iocType || "" : "";
    }
    if (elements.value) {
      elements.value.textContent = trigger ? trigger.dataset.iocValue || "" : "";
    }
    if (elements.count) {
      elements.count.textContent = "";
    }
    if (elements.list) {
      elements.list.innerHTML = "";
      elements.list.hidden = true;
    }
    setEvidenceStatus(elements, "Loading evidence...", false);
    if (elements.modal) {
      elements.modal.focus();
    }
    return elements;
  }

  function closeEvidenceModal() {
    const elements = evidenceModalElements();
    if (!elements) {
      return;
    }
    elements.modal.hidden = true;
    document.body.classList.remove("modal-open");
    if (lastEvidenceTrigger && typeof lastEvidenceTrigger.focus === "function") {
      lastEvidenceTrigger.focus();
    }
    lastEvidenceTrigger = null;
  }

  function renderEvidenceRows(elements, sources) {
    if (!elements.list) {
      return;
    }
    if (!sources || sources.length === 0) {
      elements.list.hidden = true;
      setEvidenceStatus(elements, "No source rows found for this IOC.", false);
      return;
    }
    elements.list.innerHTML = sources
      .map(function (source, index) {
        const sourceUrl = source.source_url || "";
        const evidence = source.evidence_text || "";
        return `
          <article class="evidence-card">
            <div class="evidence-card-head">
              <div class="evidence-source-index">#${escapeHtml(index + 1)}</div>
              <div class="evidence-source-main">
                <a class="evidence-source-url" href="${escapeHtml(sourceUrl)}" target="_blank" rel="noreferrer">${escapeHtml(sourceUrl || "No source URL")}</a>
                <div class="evidence-source-meta">
                  <span>${escapeHtml(source.domain || "No domain")}</span>
                  <span>${escapeHtml(source.source_type || "crawl")}</span>
                  <span>${escapeHtml(source.rule_name || "No rule")}</span>
                </div>
              </div>
            </div>
            <div class="evidence-code-block">${escapeHtml(evidence || "No evidence text recorded.")}</div>
          </article>
        `;
      })
      .join("");
    elements.list.hidden = false;
    setEvidenceStatus(elements, "", false);
  }

  async function loadEvidence(trigger) {
    const endpoint = trigger.getAttribute("data-evidence-url");
    if (!endpoint) {
      return;
    }
    const elements = openEvidenceModal(trigger);
    if (!elements) {
      return;
    }
    try {
      const response = await fetch(endpoint, {
        headers: { Accept: "application/json" },
        cache: "no-store",
      });
      if (!response.ok) {
        throw new Error("Unable to load IOC evidence.");
      }
      const payload = await response.json();
      const ioc = payload.ioc || {};
      const sources = Array.isArray(payload.sources) ? payload.sources : [];
      if (elements.title) {
        elements.title.textContent = `Evidence For IOC #${ioc.id || trigger.dataset.iocId || ""}`;
      }
      if (elements.type) {
        elements.type.textContent = ioc.type || trigger.dataset.iocType || "";
      }
      if (elements.value) {
        elements.value.textContent = ioc.value_norm || trigger.dataset.iocValue || "";
      }
      if (elements.count) {
        elements.count.textContent = `${sources.length} source row${sources.length === 1 ? "" : "s"}`;
      }
      if (elements.collected) {
        elements.collected.textContent = ioc.collected_at || "";
      }
      renderEvidenceRows(elements, sources);
    } catch (error) {
      if (elements.list) {
        elements.list.hidden = true;
      }
      setEvidenceStatus(elements, error && error.message ? error.message : "Unable to load IOC evidence.", true);
    }
  }

  document.addEventListener("click", function (event) {
    if (!(event.target instanceof Element)) {
      return;
    }
    const trigger = event.target.closest("[data-ioc-evidence-trigger]");
    if (trigger) {
      event.preventDefault();
      loadEvidence(trigger);
      return;
    }

    if (event.target.closest("[data-ioc-evidence-close]")) {
      closeEvidenceModal();
      return;
    }

    const modal = event.target.closest("[data-ioc-evidence-modal]");
    if (modal && event.target === modal) {
      closeEvidenceModal();
    }
  });

  document.addEventListener("keydown", function (event) {
    if (event.key !== "Escape") {
      return;
    }
    const modal = document.querySelector("[data-ioc-evidence-modal]");
    if (modal && !modal.hidden) {
      closeEvidenceModal();
    }
  });

  function statusClass(value) {
    return String(value || "").replace(/[^a-zA-Z0-9_-]/g, "_");
  }

  function statusLabel(value) {
    return String(value || "")
      .split("_")
      .filter(Boolean)
      .map(function (part) {
        return part.charAt(0).toUpperCase() + part.slice(1);
      })
      .join(" ");
  }

  function reviewPanel() {
    return document.querySelector("[data-review-panel]");
  }

  function sourceLabel(value) {
    const labels = {
      google_search: "Google",
      extracted_from_crawl: "From Crawl",
      keyword_search: "Keyword Search",
      keyword_search_result: "Keyword Search",
    };
    return labels[value] || statusLabel(value);
  }

  function reviewPageUrl(params) {
    const panel = reviewPanel();
    const base = panel ? panel.getAttribute("data-review-page-url") || window.location.pathname : window.location.pathname;
    const query = params.toString();
    return `${base}${query ? `?${query}` : ""}`;
  }

  function currentReviewParams() {
    const panel = reviewPanel();
    const params = new URLSearchParams(window.location.search);
    if (!params.get("url_status")) {
      params.set("url_status", panel ? panel.dataset.urlStatus || "pending_review" : "pending_review");
    }
    if (!params.has("source")) {
      params.set("source", panel ? panel.dataset.source || "" : "");
    }
    if (!params.has("q")) {
      params.set("q", panel ? panel.dataset.q || "" : "");
    }
    if (!params.get("page_size")) {
      params.set("page_size", panel ? panel.dataset.pageSize || "100" : "100");
    }
    if (!params.get("page")) {
      params.set("page", "1");
    }
    return params;
  }

  function setReviewFeedback(message, isError) {
    const panel = reviewPanel();
    const feedback = panel ? panel.querySelector("[data-review-feedback]") : null;
    if (!feedback) {
      return;
    }
    feedback.textContent = message || "";
    feedback.classList.toggle("error", Boolean(isError));
  }

  function setFormBusy(form, busy) {
    if (!form) {
      return;
    }
    form.querySelectorAll("button").forEach(function (button) {
      button.disabled = busy;
    });
  }

  function renderSourceTags(sources) {
    return String(sources || "")
      .split(",")
      .map(function (item) {
        return item.trim();
      })
      .filter(Boolean)
      .map(function (item) {
        return `<span class="tag">${escapeHtml(sourceLabel(item))}</span>`;
      })
      .join("");
  }

  function renderReviewUrlRows(body, rows) {
    if (!body) {
      return;
    }
    if (!rows || rows.length === 0) {
      body.innerHTML = '<tr><td colspan="10" class="empty">No URLs in this tab.</td></tr>';
      return;
    }

    const panel = reviewPanel();
    const apiBase = panel ? (panel.getAttribute("data-review-endpoint") || "/api/review/urls").replace(/\/$/, "") : "/api/review/urls";
    body.innerHTML = rows
      .map(function (row) {
        const id = row.id || "";
        const url = row.url_norm || "";
        const title = row.title || "";
        const queries = row.queries || "";
        const reviewStatus = row.review_status || "";
        const crawlStatus = row.crawl_status || "";
        const pending = reviewStatus === "pending_review";
        const checkbox = pending
          ? `<input type="checkbox" name="url_ids" value="${escapeHtml(id)}" form="bulk-url-review-form" aria-label="Select URL #${escapeHtml(id)}">`
          : '<span class="readonly-note">-</span>';
        const actions = pending
          ? `
            <div class="row-actions">
              <form method="post" action="/urls/${escapeHtml(id)}/approve" data-review-action-form data-review-action="approve" data-api-action="${escapeHtml(apiBase)}/${escapeHtml(id)}/approve">
                <button class="button small primary" type="submit">Approve</button>
              </form>
              <form method="post" action="/urls/${escapeHtml(id)}/reject" data-review-action-form data-review-action="reject" data-api-action="${escapeHtml(apiBase)}/${escapeHtml(id)}/reject">
                <button class="button small" type="submit">Reject</button>
              </form>
            </div>
          `
          : '<span class="readonly-note">Read only</span>';
        return `
          <tr
            data-review-url-row
            data-review-url-id="${escapeHtml(id)}"
            data-pattern-domain="${escapeHtml(row.domain || "")}"
            data-pattern-url="${escapeHtml(url)}"
            data-pattern-title="${escapeHtml(title)}"
            data-pattern-source="${escapeHtml(row.sources || "")}"
            data-pattern-query="${escapeHtml(queries)}"
          >
            <td class="cell-select">${checkbox}</td>
            <td class="cell-id">#${escapeHtml(id)}</td>
            <td class="cell-domain"><strong title="${escapeHtml(row.domain || "")}">${escapeHtml(row.domain || "")}</strong></td>
            <td class="cell-tags">${renderSourceTags(row.sources)}</td>
            <td class="cell-url">
              <a class="truncate-line" href="${escapeHtml(url)}" target="_blank" rel="noreferrer" title="${escapeHtml(url)}">${escapeHtml(url)}</a>
            </td>
            <td class="cell-title">
              ${
                title
                  ? `<span class="truncate-line" title="${escapeHtml(title)}">${escapeHtml(title)}</span>`
                  : '<span class="muted">-</span>'
              }
            </td>
            <td class="cell-query"><code class="truncate-line" title="${escapeHtml(queries)}">${escapeHtml(queries)}</code></td>
            <td class="cell-status"><span class="badge ${statusClass(reviewStatus)}">${escapeHtml(statusLabel(reviewStatus))}</span></td>
            <td class="cell-status"><span class="badge ${statusClass(crawlStatus)}">${escapeHtml(statusLabel(crawlStatus))}</span></td>
            <td class="cell-actions review-url-actions-cell">${actions}</td>
          </tr>
        `;
      })
      .join("");
  }

  function syncReviewChrome(payload, params) {
    const panel = reviewPanel();
    if (!panel) {
      return;
    }
    const pagination = payload.pagination || {};
    panel.dataset.urlStatus = payload.url_status || params.get("url_status") || "pending_review";
    panel.dataset.source = payload.source || "";
    panel.dataset.q = payload.q || "";
    panel.dataset.pageSize = String(pagination.page_size || params.get("page_size") || "100");

    const summary = panel.querySelector("[data-review-summary]");
    if (summary) {
      summary.textContent = `${pagination.total || 0} matches · page ${pagination.page || 1}/${pagination.total_pages || 1}`;
    }
    const showing = panel.querySelector("[data-review-showing]");
    if (showing) {
      showing.textContent = `Showing ${payload.count || 0} rows on this page`;
    }

    const filterForm = document.querySelector("[data-review-filter-form]");
    if (filterForm) {
      const filterStatus = filterForm.querySelector("input[name='url_status']");
      const filterSource = filterForm.querySelector("select[name='source']");
      const filterQuery = filterForm.querySelector("input[name='q']");
      const filterPageSize = filterForm.querySelector("input[name='page_size']");
      if (filterStatus) {
        filterStatus.value = panel.dataset.urlStatus;
      }
      if (filterSource) {
        filterSource.value = panel.dataset.source;
      }
      if (filterQuery) {
        filterQuery.value = panel.dataset.q;
      }
      if (filterPageSize) {
        filterPageSize.value = panel.dataset.pageSize;
      }
    }

    const paginationBar = panel.querySelector("[data-review-pagination]");
    if (paginationBar) {
      const previousParams = new URLSearchParams(params);
      previousParams.set("page", String(pagination.prev_page || 1));
      const nextParams = new URLSearchParams(params);
      nextParams.set("page", String(pagination.next_page || 1));
      paginationBar.innerHTML = `
        ${
          pagination.has_prev
            ? `<a class="button small" href="${escapeHtml(reviewPageUrl(previousParams))}" data-review-page="${escapeHtml(pagination.prev_page || 1)}">Previous</a>`
            : '<span class="button small disabled">Previous</span>'
        }
        <span class="muted" data-review-showing>Showing ${escapeHtml(payload.count || 0)} rows on this page</span>
        ${
          pagination.has_next
            ? `<a class="button small" href="${escapeHtml(reviewPageUrl(nextParams))}" data-review-page="${escapeHtml(pagination.next_page || 1)}">Next</a>`
            : '<span class="button small disabled">Next</span>'
        }
      `;
    }
  }

  async function fetchReviewRows(params, options) {
    const panel = reviewPanel();
    const body = document.querySelector("[data-review-url-body]");
    if (!panel || !body) {
      return;
    }
    const endpoint = panel.getAttribute("data-review-endpoint");
    if (!endpoint) {
      return;
    }
    const requestUrl = `${endpoint}?${params.toString()}`;
    setReviewFeedback(options && options.loadingMessage ? options.loadingMessage : "Fetching rows...", false);
    try {
      const response = await fetch(requestUrl, {
        headers: { Accept: "application/json" },
        cache: "no-store",
      });
      const payload = await response.json();
      if (!response.ok || !payload.ok) {
        throw new Error(payload.error || "Unable to fetch review rows.");
      }
      const rows = Array.isArray(payload.items) ? payload.items : [];
      renderReviewUrlRows(body, rows);
      syncReviewChrome(payload, params);
      const page = payload.pagination && payload.pagination.page ? payload.pagination.page : params.get("page") || "1";
      params.set("page", String(page));
      if (options && options.historyMode === "push") {
        window.history.pushState({}, "", reviewPageUrl(params));
      } else if (options && options.historyMode === "replace") {
        window.history.replaceState({}, "", reviewPageUrl(params));
      }
      setReviewFeedback(options && options.doneMessage ? options.doneMessage : "Rows updated.", false);
    } catch (error) {
      setReviewFeedback(error && error.message ? error.message : "Unable to fetch review rows.", true);
    }
  }

  async function postReviewJson(endpoint, payload) {
    const response = await fetch(endpoint, {
      method: "POST",
      headers: {
        Accept: "application/json",
        "Content-Type": "application/json",
      },
      body: JSON.stringify(payload || {}),
    });
    const data = await response.json();
    if (!response.ok || !data.ok) {
      throw new Error(data.error || data.message || "Review action failed.");
    }
    return data;
  }

  async function submitReviewAction(form) {
    const endpoint = form.getAttribute("data-api-action");
    if (!endpoint) {
      return;
    }
    setFormBusy(form, true);
    setReviewFeedback("Applying action...", false);
    try {
      const result = await postReviewJson(endpoint, {});
      const params = currentReviewParams();
      await fetchReviewRows(params, {
        historyMode: "replace",
        loadingMessage: result.message || "Action applied. Refreshing rows...",
        doneMessage: result.message || "Action applied.",
      });
    } catch (error) {
      setReviewFeedback(error && error.message ? error.message : "Review action failed.", true);
    } finally {
      setFormBusy(form, false);
    }
  }

  async function submitReviewBulk(form, submitter) {
    const endpoint = form.getAttribute("data-api-action");
    if (!endpoint) {
      return;
    }
    const action = submitter && submitter.name === "bulk_action" ? submitter.value : "";
    const ids = new FormData(form).getAll("url_ids");
    if (ids.length === 0) {
      setReviewFeedback("Select at least one URL row.", true);
      return;
    }
    setFormBusy(form, true);
    setReviewFeedback("Applying bulk action...", false);
    try {
      const result = await postReviewJson(endpoint, {
        action,
        bulk_action: action,
        url_ids: ids,
      });
      const params = currentReviewParams();
      await fetchReviewRows(params, {
        historyMode: "replace",
        loadingMessage: result.message || "Bulk action applied. Refreshing rows...",
        doneMessage: result.message || "Bulk action applied.",
      });
    } catch (error) {
      setReviewFeedback(error && error.message ? error.message : "Bulk review action failed.", true);
    } finally {
      setFormBusy(form, false);
    }
  }

  function startReviewUrlActions() {
    if (!reviewPanel()) {
      return;
    }

    document.addEventListener("submit", function (event) {
      const form = event.target;
      if (!(form instanceof HTMLFormElement)) {
        return;
      }
      if (form.matches("[data-review-action-form]")) {
        event.preventDefault();
        submitReviewAction(form);
        return;
      }
      if (form.matches("[data-review-bulk-form]")) {
        event.preventDefault();
        submitReviewBulk(form, event.submitter);
        return;
      }
      if (form.matches("[data-review-filter-form]")) {
        event.preventDefault();
        const params = new URLSearchParams(new FormData(form));
        params.set("page", "1");
        fetchReviewRows(params, {
          historyMode: "push",
          loadingMessage: "Applying filters...",
          doneMessage: "Filters applied.",
        });
      }
    });

    document.addEventListener("click", function (event) {
      if (!(event.target instanceof Element)) {
        return;
      }
      const fetchButton = event.target.closest("[data-review-fetch-new]");
      if (fetchButton) {
        event.preventDefault();
        const params = currentReviewParams();
        params.set("page", "1");
        fetchReviewRows(params, {
          historyMode: "replace",
          loadingMessage: "Fetching new rows...",
          doneMessage: "Fetched latest rows.",
        });
        return;
      }

      const pageLink = event.target.closest("[data-review-page]");
      if (pageLink) {
        event.preventDefault();
        const params = currentReviewParams();
        params.set("page", pageLink.getAttribute("data-review-page") || "1");
        fetchReviewRows(params, {
          historyMode: "push",
          loadingMessage: "Loading page...",
          doneMessage: "Page loaded.",
        });
      }
    });

    window.addEventListener("popstate", function () {
      fetchReviewRows(currentReviewParams(), {
        historyMode: false,
        loadingMessage: "Loading rows...",
        doneMessage: "Rows updated.",
      });
    });
  }

  function renderCrawlingUrlRows(body, rows) {
    if (!rows || rows.length === 0) {
      body.innerHTML = '<tr><td colspan="9" class="empty">No URLs are currently crawling.</td></tr>';
      return;
    }

    body.innerHTML = rows
      .map(function (item) {
        const queueStatus = item.queue_status || "";
        const crawlStatus = item.crawl_status || "";
        const error = item.queue_error || item.crawl_error || "";
        const started = item.queue_started_at || item.job_started_at || "";
        const url = item.url_norm || "";
        return `
          <tr>
            <td>#${escapeHtml(item.queue_item_id || "")}</td>
            <td><strong>${escapeHtml(item.domain || "")}</strong></td>
            <td><a class="truncate-line" href="${escapeHtml(url)}" target="_blank" rel="noreferrer" title="${escapeHtml(url)}">${escapeHtml(url)}</a></td>
            <td><span class="badge ${statusClass(queueStatus)}">${escapeHtml(statusLabel(queueStatus))}</span></td>
            <td><span class="badge ${statusClass(crawlStatus)}">${escapeHtml(statusLabel(crawlStatus))}</span></td>
            <td>${escapeHtml(item.status_code || "")}</td>
            <td><code>${escapeHtml(item.fetch_method || "")}</code></td>
            <td>${escapeHtml(started)}</td>
            <td><code class="truncate-line" title="${escapeHtml(error)}">${escapeHtml(error)}</code></td>
          </tr>
        `;
      })
      .join("");
  }

  function renderQueueUrlItemRows(body, rows) {
    if (!rows || rows.length === 0) {
      body.innerHTML = '<tr><td colspan="8" class="empty">No approved URL/domain items in this queue.</td></tr>';
      return;
    }

    body.innerHTML = rows
      .map(function (item) {
        const itemId = item.id || "";
        const url = item.url_norm || "";
        const queueStatus = item.status || "";
        const crawlStatus = item.crawl_status || "";
        const error = item.error || "";
        const source = item.source_queue_id
          ? `#${escapeHtml(item.source_queue_id)}${item.source_queue_name ? ` · ${escapeHtml(item.source_queue_name)}` : ""}`
          : "-";
        const action = queueStatus === "running"
          ? '<span class="muted">Running</span>'
          : `
            <form method="post" action="/url-queue-items/${escapeHtml(itemId)}/delete">
              <button class="button small danger" type="submit">Remove</button>
            </form>
          `;
        return `
          <tr>
            <td>#${escapeHtml(itemId)}</td>
            <td><strong>${escapeHtml(item.domain || "")}</strong></td>
            <td><code class="truncate-line" title="${escapeHtml(url)}">${escapeHtml(url)}</code></td>
            <td><span class="badge ${statusClass(queueStatus)}">${escapeHtml(statusLabel(queueStatus))}</span></td>
            <td>${source}</td>
            <td><span class="badge ${statusClass(crawlStatus)}">${escapeHtml(statusLabel(crawlStatus))}</span></td>
            <td><code class="truncate-line" title="${escapeHtml(error)}">${escapeHtml(error)}</code></td>
            <td class="cell-actions">${action}</td>
          </tr>
        `;
      })
      .join("");
  }

  function renderKeywordSearchRows(body, rows) {
    if (!rows || rows.length === 0) {
      body.innerHTML = '<tr><td colspan="9" class="empty">No CloakBrowser keyword searches are running or waiting.</td></tr>';
      return;
    }

    body.innerHTML = rows
      .map(function (item) {
        const searchStatus = item.search_status || "";
        const jobStatus = item.job_status || item.queue_item_status || "";
        const outputQueue = item.output_url_queue_id
          ? `#${escapeHtml(item.output_url_queue_id)} · ${escapeHtml(item.output_url_queue_name || "queue_url")}`
          : '<span class="badge failed">Not Bound</span>';
        const error = item.queue_item_error || item.search_error || item.job_error || "";
        const started = item.queue_item_started_at || item.search_started_at || item.job_started_at || "";
        return `
          <tr>
            <td>#${escapeHtml(item.queue_item_id || "")}</td>
            <td><strong>${escapeHtml(item.keyword_text || "")}</strong></td>
            <td><code class="truncate-line" title="${escapeHtml(item.query_text || "")}">${escapeHtml(item.query_text || "")}</code></td>
            <td>${outputQueue}</td>
            <td><span class="badge ${statusClass(searchStatus)}">${escapeHtml(statusLabel(searchStatus))}</span></td>
            <td><span class="badge ${statusClass(jobStatus)}">${escapeHtml(statusLabel(jobStatus))}</span></td>
            <td>${escapeHtml(item.attempts || 0)}</td>
            <td>${escapeHtml(started)}</td>
            <td><code class="truncate-line" title="${escapeHtml(error)}">${escapeHtml(error)}</code></td>
          </tr>
        `;
      })
      .join("");
  }

  function renderKeywordItemRows(body, rows) {
    if (!rows || rows.length === 0) {
      body.innerHTML = '<tr><td colspan="8" class="empty">No keyword search items in this queue.</td></tr>';
      return;
    }

    body.innerHTML = rows
      .map(function (item) {
        const status = item.status || "";
        const outputQueue = item.output_url_queue_id
          ? `#${escapeHtml(item.output_url_queue_id)} · ${escapeHtml(item.output_url_queue_name || "queue_url")}`
          : '<span class="badge failed">Not Bound</span>';
        const error = item.error || "";
        const resultCount = item.result_count == null ? 0 : item.result_count;
        const pageCount = item.page_count == null ? 0 : item.page_count;
        return `
          <tr>
            <td>#${escapeHtml(item.id || "")}</td>
            <td><strong>${escapeHtml(item.keyword_text || "")}</strong></td>
            <td><code class="truncate-line" title="${escapeHtml(item.query_text || "")}">${escapeHtml(item.query_text || "")}</code></td>
            <td>${outputQueue}</td>
            <td><span class="badge ${statusClass(status)}">${escapeHtml(statusLabel(status))}</span></td>
            <td>${escapeHtml(resultCount)}</td>
            <td>${escapeHtml(pageCount)}</td>
            <td><code class="truncate-line" title="${escapeHtml(error)}">${escapeHtml(error)}</code></td>
          </tr>
        `;
      })
      .join("");
  }

  function updateQueueStatus(queue) {
    if (!queue || !queue.status) {
      return;
    }
    const badge = document.querySelector("[data-queue-status-badge]");
    if (!badge) {
      return;
    }
    const previous = badge.getAttribute("data-status") || "";
    const status = String(queue.status);
    if (previous === status) {
      return;
    }
    if (previous) {
      badge.classList.remove(statusClass(previous));
    }
    badge.classList.add(statusClass(status));
    badge.setAttribute("data-status", status);
    badge.textContent = statusLabel(status);
    const wasActive = previous === "running" || previous === "stopping";
    const isActive = status === "running" || status === "stopping";
    if (wasActive && !isActive && !document.documentElement.dataset.queueReloading) {
      document.documentElement.dataset.queueReloading = "1";
      window.setTimeout(function () {
        window.location.reload();
      }, 300);
    }
  }

  function startPolling(panelSelector, bodySelector, countSelector, renderRows) {
    const panel = document.querySelector(panelSelector);
    const body = document.querySelector(bodySelector);
    if (!panel || !body) {
      return;
    }
    const endpoint = panel.getAttribute("data-endpoint");
    if (!endpoint) {
      return;
    }
    const count = document.querySelector(countSelector);

    async function refresh() {
      if (document.hidden) {
        return;
      }
      try {
        const response = await fetch(endpoint, {
          headers: { Accept: "application/json" },
          cache: "no-store",
        });
        if (!response.ok) {
          return;
        }
        const payload = await response.json();
        const rows = Array.isArray(payload.items) ? payload.items : [];
        updateQueueStatus(payload.queue);
        renderRows(body, rows);
        if (count) {
          count.textContent = String(rows.length);
        }
      } catch (_error) {
        // Keep the last rendered state if polling temporarily fails.
      }
    }

    refresh();
    window.setInterval(refresh, 1500);
  }

  function startCrawlingUrlPolling() {
    startPolling(
      "[data-crawling-url-panel]",
      "[data-crawling-url-body]",
      "[data-crawling-url-count]",
      renderCrawlingUrlRows
    );
  }

  function startQueueUrlItemsPolling() {
    startPolling(
      "[data-queue-url-items-panel]",
      "[data-queue-url-items-body]",
      "[data-queue-url-items-count]",
      renderQueueUrlItemRows
    );
  }

  function startKeywordSearchPolling() {
    startPolling(
      "[data-keyword-search-panel]",
      "[data-keyword-search-body]",
      "[data-keyword-search-count]",
      renderKeywordSearchRows
    );
  }

  function startKeywordItemsPolling() {
    startPolling(
      "[data-keyword-items-panel]",
      "[data-keyword-items-body]",
      "[data-keyword-items-count]",
      renderKeywordItemRows
    );
  }

  function restoreScroll() {
    let payload;
    const key = keyForPath(window.location.pathname);
    try {
      payload = JSON.parse(window.sessionStorage.getItem(key) || "null");
    } catch (_error) {
      payload = null;
    }
    if (!payload) {
      return;
    }

    try {
      window.sessionStorage.removeItem(key);
    } catch (_error) {
      // Ignore private-mode/sessionStorage failures.
    }

    if (Date.now() - Number(payload.ts || 0) > RESTORE_TTL_MS) {
      return;
    }

    window.requestAnimationFrame(function () {
      window.requestAnimationFrame(function () {
        const maxY = Math.max(0, document.documentElement.scrollHeight - window.innerHeight);
        const maxX = Math.max(0, document.documentElement.scrollWidth - window.innerWidth);
        window.scrollTo({
          left: Math.min(Number(payload.x || 0), maxX),
          top: Math.min(Number(payload.y || 0), maxY),
          behavior: "auto",
        });
      });
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", restoreScroll, { once: true });
    document.addEventListener("DOMContentLoaded", startReviewUrlActions, { once: true });
    document.addEventListener("DOMContentLoaded", startCrawlingUrlPolling, { once: true });
    document.addEventListener("DOMContentLoaded", startQueueUrlItemsPolling, { once: true });
    document.addEventListener("DOMContentLoaded", startKeywordSearchPolling, { once: true });
    document.addEventListener("DOMContentLoaded", startKeywordItemsPolling, { once: true });
  } else {
    restoreScroll();
    startReviewUrlActions();
    startCrawlingUrlPolling();
    startQueueUrlItemsPolling();
    startKeywordSearchPolling();
    startKeywordItemsPolling();
  }
  window.addEventListener("pageshow", restoreScroll);
})();
