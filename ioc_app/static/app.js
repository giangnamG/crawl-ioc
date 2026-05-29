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
