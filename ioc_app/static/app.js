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
      return [
        row.dataset.patternDomain || "",
        row.dataset.patternUrl || "",
        row.dataset.patternSource || "",
        row.dataset.patternQuery || "",
      ].join("\n");
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
    const table = panel ? panel.querySelector("table.review-table") : null;
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
  } else {
    restoreScroll();
  }
  window.addEventListener("pageshow", restoreScroll);
})();
