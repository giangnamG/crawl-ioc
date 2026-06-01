from __future__ import annotations

import html
import os
import random
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path

from .db import ROOT_DIR
from .normalizers import normalize_url


USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0 Safari/537.36"
)

GOOGLE_BLOCK_PATTERNS = (
    "unusual traffic",
    "our systems have detected",
    "sorry/index",
    "detected unusual traffic",
)

PROXY_RETRY_PATTERNS = (
    "TimeoutError",
    "ERR_TIMED_OUT",
    "ERR_CONNECTION_TIMED_OUT",
    "ERR_CONNECTION_RESET",
    "ERR_CONNECTION_CLOSED",
    "ERR_EMPTY_RESPONSE",
    "ERR_NAME_NOT_RESOLVED",
    "ERR_ADDRESS_UNREACHABLE",
    "ERR_INTERNET_DISCONNECTED",
    "ERR_NETWORK_CHANGED",
    "ERR_INVALID_AUTH_CREDENTIALS",
    "ERR_PROXY_AUTH_UNSUPPORTED",
    "ERR_PROXY_CONNECTION_FAILED",
    "ERR_TUNNEL_CONNECTION_FAILED",
    "ERR_SOCKS_CONNECTION_FAILED",
    "net::ERR_TIMED_OUT",
)


@dataclass
class SearchResult:
    url: str
    title: str
    snippet: str
    rank: int
    page_no: int


@dataclass
class FetchResult:
    final_url: str
    status_code: int | None
    html: str
    text: str
    links: list[str]
    redirects: list[str]
    content_type: str | None = None
    content_length: int | None = None
    fetch_method: str = "cloak_browser"
    is_text: bool = True
    error: str | None = None


class TextAndLinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.text_parts: list[str] = []
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_dict = dict(attrs)
        for key in ("href", "src", "action"):
            value = attrs_dict.get(key)
            if value:
                self.links.append(html.unescape(value))

    def handle_data(self, data: str) -> None:
        data = data.strip()
        if data:
            self.text_parts.append(data)

    @property
    def text(self) -> str:
        return "\n".join(self.text_parts)


class BrowserClient:
    """Browser adapter.

    Default provider is CloakBrowser. Set BROWSER_PROVIDER=http only for local
    tests that must avoid opening/downloading a browser.
    """

    def __init__(self) -> None:
        self.provider = os.environ.get("BROWSER_PROVIDER", "cloak").strip().lower()
        self.max_pages = int(os.environ.get("SEARCH_MAX_PAGES", "1"))
        self.search_hard_page_limit = int(os.environ.get("SEARCH_HARD_PAGE_LIMIT", "100"))
        self.timeout_ms = int(float(os.environ.get("BROWSER_TIMEOUT", "45")) * 1000)
        self.fast_mode = env_bool("SEARCH_FAST_MODE", False)
        self.headless = env_bool("CLOAK_HEADLESS", False)
        self.humanize = env_bool("CLOAK_HUMANIZE", True)
        self.human_preset = os.environ.get("CLOAK_HUMAN_PRESET", "careful")
        self.type_delay_min = int(os.environ.get("SEARCH_TYPE_DELAY_MIN", "0" if self.fast_mode else "8"))
        self.type_delay_max = int(os.environ.get("SEARCH_TYPE_DELAY_MAX", "5" if self.fast_mode else "35"))
        self.locale = os.environ.get("CLOAK_LOCALE", "en-US")
        self.timezone = os.environ.get("CLOAK_TIMEZONE", "Asia/Saigon")
        self.proxy_candidates = proxy_candidates_from_env()
        self.proxy = select_proxy(self.proxy_candidates)
        self.direct_fallback = env_bool("CLOAK_DIRECT_FALLBACK", True)
        self.geoip = env_bool("CLOAK_GEOIP", bool(self.proxy))
        self.backend = os.environ.get("CLOAK_BACKEND") or None
        self.stealth_args = env_bool("CLOAK_STEALTH_ARGS", True)
        self.fingerprint_seed = os.environ.get("CLOAK_FINGERPRINT_SEED")
        self.fingerprint_noise = os.environ.get("CLOAK_FINGERPRINT_NOISE", "false").strip().lower()
        self.storage_quota = os.environ.get("CLOAK_STORAGE_QUOTA", "5000").strip()
        self.disable_http2 = env_bool("CLOAK_DISABLE_HTTP2", False)
        self.viewport_width = int(os.environ.get("CLOAK_VIEWPORT_WIDTH", "1920"))
        self.viewport_height = int(os.environ.get("CLOAK_VIEWPORT_HEIGHT", "1080"))
        self.screen_width = int(os.environ.get("CLOAK_SCREEN_WIDTH", str(self.viewport_width)))
        self.screen_height = int(os.environ.get("CLOAK_SCREEN_HEIGHT", str(self.viewport_height)))
        self.search_entry = os.environ.get("GOOGLE_SEARCH_ENTRY", "homepage").strip().lower()
        self.next_fallback_direct = env_bool("GOOGLE_NEXT_FALLBACK_DIRECT", True)
        self.page_delay_min = float(os.environ.get("SEARCH_PAGE_DELAY_MIN", "0" if self.fast_mode else "2.5"))
        self.page_delay_max = float(os.environ.get("SEARCH_PAGE_DELAY_MAX", "0" if self.fast_mode else "6.0"))
        self.profile_root = Path(os.environ.get("CLOAK_PROFILE_ROOT", ROOT_DIR / "data" / "cloak_profiles"))

    def search_google(self, query_text: str) -> list[SearchResult]:
        if self.provider == "http":
            return HttpFallbackClient(self.max_pages, self.timeout_ms).search_google(query_text)
        return self._run_with_proxy_retries(lambda: self._search_google_with_cloak(query_text))

    def fetch_url(self, url: str) -> FetchResult:
        if self.provider == "http":
            return HttpFallbackClient(self.max_pages, self.timeout_ms).fetch_url(url)
        return self._run_with_proxy_retries(lambda: self._fetch_url_with_cloak(url))

    def fetch_text_resource(self, url: str, fetch_method: str = "http_text") -> FetchResult:
        return HttpFallbackClient(self.max_pages, self.timeout_ms).fetch_text_resource(
            url,
            fetch_method=fetch_method,
        )

    def fetch_binary_metadata(self, url: str) -> FetchResult:
        return HttpFallbackClient(self.max_pages, self.timeout_ms).fetch_binary_metadata(url)

    def _run_with_proxy_retries(self, operation):
        attempts = self._proxy_attempts()
        last_exc: Exception | None = None
        for index, proxy in enumerate(attempts):
            self.proxy = proxy
            try:
                return operation()
            except Exception as exc:
                last_exc = exc
                if index == len(attempts) - 1 or not is_retryable_proxy_error(exc):
                    raise
        if last_exc:
            raise last_exc
        return operation()

    def _proxy_attempts(self) -> list[str | None]:
        if not self.proxy_candidates:
            return [None]

        selected = self.proxy or select_proxy(self.proxy_candidates)
        attempts = [selected] if selected else []
        attempts.extend(proxy for proxy in self.proxy_candidates if proxy != selected)
        if self.direct_fallback:
            attempts.append(None)
        return attempts or [None]

    def _search_google_with_cloak(self, query_text: str) -> list[SearchResult]:
        results: list[SearchResult] = []
        seen: set[str] = set()
        rank = 1
        page_no = 1
        page_limit = self.max_pages if self.max_pages > 0 else self.search_hard_page_limit

        ctx = self._launch_context("google")
        try:
            page = ctx.new_page()
            page.set_default_timeout(self.timeout_ms)
            self._open_google_search(page, query_text)

            while page_no <= page_limit:
                page_items = self._collect_google_results_page(page, page_no, rank, seen)
                has_result_candidates = self._count_google_result_candidates(page) > 0
                if not page_items and not has_result_candidates:
                    break

                if page_items:
                    results.extend(page_items)
                    rank += len(page_items)

                next_page_no = page_no + 1
                if next_page_no > page_limit:
                    break

                self._sleep_between_pages()
                try:
                    if not self._go_google_next_page_prefer_link(page, query_text, next_page_no):
                        break
                except RuntimeError as exc:
                    if "anti-bot/CAPTCHA" in str(exc) and results:
                        break
                    raise
                page_no = next_page_no

            return results
        finally:
            ctx.close()

    def _open_google_search(self, page, query_text: str) -> None:
        if self.search_entry == "direct":
            search_url = "https://www.google.com/search?" + urllib.parse.urlencode(
                {"q": query_text, "num": 10, "hl": "en"}
            )
            page.goto(search_url, wait_until="domcontentloaded", timeout=self.timeout_ms)
            self._dismiss_google_consent(page)
            self._wait_for_google_results(page)
            return

        page.goto("https://www.google.com/?hl=en", wait_until="domcontentloaded", timeout=self.timeout_ms)
        self._dismiss_google_consent(page)
        if not self.fast_mode:
            self._wait_for_settle(page)
        self._raise_if_google_blocked(page)

        search_box = self._first_available_locator(
            page,
            [
                "textarea[name='q']",
                "input[name='q']",
                "textarea[aria-label='Search']",
                "textarea[title='Search']",
            ],
        )
        if search_box is None:
            raise RuntimeError("Google search box was not found.")

        search_box.click(timeout=5000)
        try:
            search_box.fill("", timeout=3000)
        except Exception:
            pass
        type_delay = random.randint(
            min(self.type_delay_min, self.type_delay_max),
            max(self.type_delay_min, self.type_delay_max),
        )
        search_box.type(query_text, delay=type_delay, timeout=self.timeout_ms)
        page.keyboard.press("Enter")
        page.wait_for_load_state("domcontentloaded", timeout=self.timeout_ms)
        self._wait_for_google_results(page)

    def _collect_google_results_page(
        self, page, page_no: int, first_rank: int, seen: set[str]
    ) -> list[SearchResult]:
        if not self.fast_mode:
            self._wait_for_settle(page)
        self._wait_for_google_results(page)
        self._raise_if_google_blocked(page)
        self._scroll_results_page(page)

        page_items = self._extract_google_results(page, page_no, first_rank, seen)
        if page_items:
            return page_items

        self._wait_for_google_results(page, extra_wait=True)
        self._scroll_results_page(page)
        return self._extract_google_results(page, page_no, first_rank, seen)

    def _fetch_url_with_cloak(self, url: str) -> FetchResult:
        ctx = self._launch_context("crawl")
        redirects: list[str] = []
        try:
            page = ctx.new_page()
            page.set_default_timeout(self.timeout_ms)

            def on_response(response):
                if response.request.is_navigation_request():
                    redirects.append(response.url)

            page.on("response", on_response)
            response = page.goto(url, wait_until="domcontentloaded", timeout=self.timeout_ms)
            self._wait_for_settle(page)

            html_text = page.content()
            text = safe_body_text(page)
            links = page.evaluate(
                """
                () => Array.from(document.querySelectorAll('[href],[src],[action]'))
                  .map(el => el.getAttribute('href') || el.getAttribute('src') || el.getAttribute('action'))
                  .filter(Boolean)
                """
            )

            normalized_links: list[str] = []
            for link in links:
                absolute = urllib.parse.urljoin(page.url, str(link))
                norm = normalize_url(absolute)
                if norm and norm not in normalized_links:
                    normalized_links.append(norm)

            return FetchResult(
                final_url=page.url,
                status_code=response.status if response else None,
                html=html_text,
                text=text,
                links=normalized_links,
                redirects=dedupe_keep_order(redirects),
                content_type=(response.headers.get("content-type") if response else None),
                content_length=parse_int(response.headers.get("content-length")) if response else len(html_text),
                fetch_method="cloak_browser",
            )
        finally:
            ctx.close()

    def _launch_context(self, profile_name: str):
        try:
            from cloakbrowser import launch_persistent_context
        except ImportError as exc:
            raise RuntimeError(
                "CloakBrowser is required. Install it with: python -m pip install cloakbrowser"
            ) from exc

        profile_dir = self.profile_root / profile_name
        profile_dir.mkdir(parents=True, exist_ok=True)

        kwargs = {
            "headless": self.headless,
            "humanize": self.humanize,
            "human_preset": self.human_preset,
            "locale": self.locale,
            "timezone": self.timezone,
            "viewport": {"width": self.viewport_width, "height": self.viewport_height},
            "geoip": self.geoip,
            "stealth_args": self.stealth_args,
        }
        if self.backend:
            kwargs["backend"] = self.backend
        args = []
        if self.fingerprint_seed:
            args.append(f"--fingerprint={self.fingerprint_seed}")
        if self.fingerprint_noise in {"true", "false"}:
            args.append(f"--fingerprint-noise={self.fingerprint_noise}")
        if self.screen_width > 0:
            args.append(f"--fingerprint-screen-width={self.screen_width}")
        if self.screen_height > 0:
            args.append(f"--fingerprint-screen-height={self.screen_height}")
        if self.storage_quota:
            args.append(f"--fingerprint-storage-quota={self.storage_quota}")
        if self.disable_http2:
            args.append("--disable-http2")
        if args:
            kwargs["args"] = args
        if self.proxy:
            kwargs["proxy"] = self.proxy

        return launch_persistent_context(str(profile_dir), **kwargs)

    def _dismiss_google_consent(self, page) -> None:
        selectors = [
            "button:has-text('Accept all')",
            "button:has-text('I agree')",
            "button:has-text('Tôi đồng ý')",
            "button:has-text('Chấp nhận tất cả')",
            "button:has-text('Reject all')",
        ]
        for selector in selectors:
            try:
                button = page.locator(selector).first
                if button.count() > 0:
                    button.click(timeout=2500)
                    self._wait_for_settle(page)
                    return
            except Exception:
                continue

    def _wait_for_settle(self, page) -> None:
        try:
            page.wait_for_load_state("networkidle", timeout=min(self.timeout_ms, 10000))
        except Exception:
            pass

    def _wait_for_google_results(self, page, extra_wait: bool = False) -> None:
        default_timeout = 10000 if self.fast_mode else 12000
        extra_timeout = 14000 if self.fast_mode else 20000
        timeout = min(self.timeout_ms, extra_timeout if extra_wait else default_timeout)
        selectors = [
            "#search",
            "#rso",
            "a h3",
            "div[data-sokoban-container]",
            "div.g",
        ]
        for selector in selectors:
            try:
                page.wait_for_selector(selector, state="attached", timeout=timeout)
                return
            except Exception:
                continue
        self._raise_if_google_blocked(page)

    def _scroll_results_page(self, page) -> None:
        if self.fast_mode:
            return
        try:
            page.evaluate(
                """
                async () => {
                  const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
                  const height = Math.max(document.body.scrollHeight, document.documentElement.scrollHeight);
                  for (const y of [0.25, 0.55, 0.85]) {
                    window.scrollTo({ top: Math.floor(height * y), behavior: 'smooth' });
                    await sleep(250);
                  }
                  window.scrollTo({ top: 0, behavior: 'smooth' });
                  await sleep(150);
                }
                """
            )
        except Exception:
            pass

    def _raise_if_google_blocked(self, page) -> None:
        haystack = f"{page.url}\n{safe_body_text(page)[:3000]}".lower()
        if any(pattern in haystack for pattern in GOOGLE_BLOCK_PATTERNS):
            raise RuntimeError(
                "Google returned an anti-bot/CAPTCHA page. Use a stable profile, headed mode, and a residential proxy."
            )

    def _extract_google_results(
        self, page, page_no: int, first_rank: int, seen: set[str]
    ) -> list[SearchResult]:
        raw_items = page.evaluate(
            """
            () => {
              const clean = value => (value || '').replace(/\\s+/g, ' ').trim();
              const anchors = Array.from(document.querySelectorAll('#search a[href], #rso a[href]'));
              return anchors
                .map(a => {
                  const h3 = a.querySelector('h3');
                  const block = a.closest('div.MjjYud, div.g, div[data-sokoban-container], div[jscontroller], div');
                  const heading = block?.querySelector('h3, [role="heading"]');
                  return {
                    href: a.href || a.getAttribute('href') || '',
                    title: clean(h3?.innerText || heading?.innerText || a.getAttribute('aria-label') || a.innerText),
                    snippet: clean(block?.innerText || '')
                  };
                })
                .filter(item => item.href);
            }
            """
        )

        output: list[SearchResult] = []
        rank = first_rank
        for item in raw_items:
            target = clean_google_href(str(item.get("href", "")))
            if not target:
                continue
            norm = normalize_url(target)
            if not norm or norm in seen:
                continue
            seen.add(norm)

            title = clean_text(str(item.get("title", "")))[:300] or norm
            snippet = clean_text(str(item.get("snippet", "")))[:500]
            output.append(
                SearchResult(
                    url=norm,
                    title=title,
                    snippet=snippet,
                    rank=rank,
                    page_no=page_no,
                )
            )
            rank += 1
        return output

    def _count_google_result_candidates(self, page) -> int:
        try:
            return int(
                page.evaluate(
                    """
                    () => Array.from(document.querySelectorAll('#search a[href], #rso a[href]'))
                      .filter(a => {
                        const href = a.href || a.getAttribute('href') || '';
                        if (!href || href.startsWith('#') || href.startsWith('javascript:')) return false;
                        return Boolean(a.querySelector('h3')) || Boolean((a.innerText || '').trim());
                      }).length
                    """
                )
            )
        except Exception:
            return 0

    def _go_google_next_page(self, page) -> bool:
        try:
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            time.sleep(0.4)
        except Exception:
            pass

        next_link = self._first_available_locator(
            page,
            [
                "a#pnnext",
                "a[aria-label='Next page']",
                "a[aria-label='Next']",
                "a:has-text('Next')",
                "a:has-text('Tiếp')",
            ],
        )
        if next_link is None:
            return False
        try:
            next_link.scroll_into_view_if_needed(timeout=3000)
            next_link.click(timeout=8000)
            page.wait_for_load_state("domcontentloaded", timeout=self.timeout_ms)
            self._wait_for_google_results(page)
            return True
        except Exception:
            return False

    def _go_google_next_page_prefer_link(self, page, query_text: str, page_no: int) -> bool:
        next_href = self._get_google_next_href(page)
        if next_href:
            try:
                page.goto(next_href, wait_until="domcontentloaded", timeout=self.timeout_ms)
                self._dismiss_google_consent(page)
                self._wait_for_google_results(page)
                self._raise_if_google_blocked(page)
                return True
            except RuntimeError as exc:
                if "anti-bot/CAPTCHA" in str(exc):
                    raise
            except Exception:
                pass

        if self._go_google_next_page(page):
            return True

        if self.next_fallback_direct:
            return self._go_google_next_page_by_start(page, query_text, page_no)
        return False

    def _get_google_next_href(self, page) -> str | None:
        try:
            href = page.evaluate(
                """
                () => {
                  const anchors = Array.from(document.querySelectorAll('a[href]'));
                  const next = anchors.find(a => {
                    const id = (a.id || '').toLowerCase();
                    const aria = (a.getAttribute('aria-label') || '').toLowerCase();
                    const text = (a.innerText || a.textContent || '').trim().toLowerCase();
                    return id === 'pnnext'
                      || aria === 'next'
                      || aria === 'next page'
                      || text === 'next'
                      || text === 'tiếp';
                  });
                  return next ? next.href : '';
                }
                """
            )
            if href:
                return str(href)
        except Exception:
            pass
        return None

    def _go_google_next_page_by_start(self, page, query_text: str, page_no: int) -> bool:
        if page_no <= 1:
            return False
        start = (page_no - 1) * 10
        search_url = "https://www.google.com/search?" + urllib.parse.urlencode(
            {"q": query_text, "start": start, "num": 10, "hl": "en"}
        )
        try:
            page.goto(search_url, wait_until="domcontentloaded", timeout=self.timeout_ms)
            self._dismiss_google_consent(page)
            self._wait_for_google_results(page)
            self._raise_if_google_blocked(page)
            return True
        except RuntimeError as exc:
            if "anti-bot/CAPTCHA" in str(exc):
                raise
            return False
        except Exception:
            self._raise_if_google_blocked(page)
            return False

    def _first_available_locator(self, page, selectors: list[str]):
        for selector in selectors:
            try:
                locator = page.locator(selector).first
                if locator.count() > 0 and locator.is_visible(timeout=1500):
                    return locator
            except Exception:
                continue
        return None

    def _sleep_between_pages(self) -> None:
        low = min(self.page_delay_min, self.page_delay_max)
        high = max(self.page_delay_min, self.page_delay_max)
        if high > 0:
            time.sleep(random.uniform(low, high))


class HttpFallbackClient:
    def __init__(self, max_pages: int, timeout_ms: int) -> None:
        self.max_pages = max_pages
        self.timeout = max(1, timeout_ms // 1000)
        self.max_body_bytes = int(os.environ.get("HTTP_FETCH_MAX_BYTES", "5000000"))

    def search_google(self, query_text: str) -> list[SearchResult]:
        results: list[SearchResult] = []
        seen: set[str] = set()
        rank = 1
        page_limit = self.max_pages if self.max_pages > 0 else 100

        for page_no in range(1, page_limit + 1):
            start = (page_no - 1) * 10
            url = "https://www.google.com/search?" + urllib.parse.urlencode(
                {"q": query_text, "start": start, "num": 10, "hl": "en"}
            )
            html_text, _, _ = self._open(url)
            page_results = self._parse_google_results(html_text, page_no, rank, seen)
            if not page_results:
                break
            results.extend(page_results)
            rank += len(page_results)

        return results

    def fetch_url(self, url: str) -> FetchResult:
        return self.fetch_text_resource(url, fetch_method="http_text")

    def fetch_text_resource(self, url: str, fetch_method: str = "http_text") -> FetchResult:
        body, final_url, status_code, content_type, content_length = self._open_bytes(url)
        text = decode_http_body(body, content_type)
        final = final_url or url
        html_text, extracted_text, links = parse_text_resource(text, final)
        effective_length = content_length if content_length is not None else len(body)
        return FetchResult(
            final_url=final,
            status_code=status_code,
            html=html_text,
            text=extracted_text,
            links=links,
            redirects=[],
            content_type=content_type,
            content_length=effective_length,
            fetch_method=fetch_method,
            is_text=True,
        )

    def fetch_binary_metadata(self, url: str) -> FetchResult:
        body, final_url, status_code, content_type, content_length = self._open_bytes(url, read_limit=0)
        return FetchResult(
            final_url=final_url or url,
            status_code=status_code,
            html="",
            text="",
            links=[],
            redirects=[],
            content_type=content_type,
            content_length=content_length if content_length is not None else len(body),
            fetch_method="http_binary_metadata",
            is_text=False,
        )

    def _open(self, url: str) -> tuple[str, str, int | None]:
        body, final_url, status_code, content_type, _ = self._open_bytes(url)
        return decode_http_body(body, content_type), final_url, status_code

    def _open_bytes(
        self,
        url: str,
        read_limit: int | None = None,
    ) -> tuple[bytes, str, int | None, str | None, int | None]:
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        limit = self.max_body_bytes if read_limit is None else max(0, read_limit)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = response.read(limit + 1) if limit > 0 else b""
                content_type = response.headers.get("Content-Type")
                content_length = parse_int(response.headers.get("Content-Length"))
                body = body[:limit] if limit > 0 else body
                return body, response.geturl(), response.status, content_type, content_length
        except urllib.error.HTTPError as exc:
            body = exc.read(limit + 1) if limit > 0 else b""
            content_type = exc.headers.get("Content-Type") if exc.headers else None
            content_length = parse_int(exc.headers.get("Content-Length")) if exc.headers else None
            body = body[:limit] if limit > 0 else body
            return body, exc.geturl(), exc.code, content_type, content_length
        except Exception as exc:
            raise RuntimeError(f"HTTP fallback fetch failed for {url}: {exc}") from exc

    def _parse_google_results(
        self, html_text: str, page_no: int, first_rank: int, seen: set[str]
    ) -> list[SearchResult]:
        parser = LinkTextParser()
        parser.feed(html_text)
        results: list[SearchResult] = []
        rank = first_rank

        for anchor in parser.anchors:
            target = clean_google_href(anchor["href"])
            if not target:
                continue
            norm = normalize_url(target)
            if not norm or norm in seen:
                continue
            seen.add(norm)
            results.append(
                SearchResult(
                    url=norm,
                    title=anchor["text"][:300] or norm,
                    snippet="",
                    rank=rank,
                    page_no=page_no,
                )
            )
            rank += 1

        return results


class LinkTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.anchors: list[dict[str, str]] = []
        self._href: str | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() == "a":
            attrs_dict = dict(attrs)
            self._href = attrs_dict.get("href")
            self._text = []

    def handle_data(self, data: str) -> None:
        if self._href is not None:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "a" and self._href is not None:
            text = clean_text("".join(self._text))
            self.anchors.append({"href": self._href, "text": html.unescape(text)})
            self._href = None
            self._text = []


def parse_text_resource(text: str, final_url: str) -> tuple[str, str, list[str]]:
    is_html = looks_like_html(text)
    parser = TextAndLinkParser()
    parser.feed(text)
    parser_links = parser.links if is_html else []
    links = extract_resource_links(text, final_url, parser_links)
    extracted_text = parser.text if is_html else text
    return text, extracted_text, links


def extract_resource_links(text: str, base_url: str, seed_links: list[str] | None = None) -> list[str]:
    candidates: list[str] = list(seed_links or [])

    for match in re.finditer(r"(?i)\bhttps?://[^\s\"'<>()]+", text):
        candidates.append(match.group(0))

    for match in re.finditer(r"(?i)(?<!:)//[a-z0-9][a-z0-9.-]+[^\s\"'<>()]*", text):
        candidates.append(match.group(0))

    for match in re.finditer(r"(?is)\burl\(\s*['\"]?([^'\")]+)['\"]?\s*\)", text):
        candidates.append(match.group(1))

    normalized_links: list[str] = []
    for candidate in candidates:
        cleaned = html.unescape((candidate or "").strip().strip(".,;:)]}'\""))
        if not cleaned or cleaned.startswith(("data:", "mailto:", "tel:", "#")):
            continue
        if cleaned.startswith("//"):
            scheme = urllib.parse.urlsplit(base_url).scheme or "https"
            cleaned = f"{scheme}:{cleaned}"
        absolute = urllib.parse.urljoin(base_url, cleaned)
        norm = normalize_url(absolute)
        if norm and norm not in normalized_links:
            normalized_links.append(norm)
    return normalized_links


def looks_like_html(text: str) -> bool:
    sample = text[:1000].lower()
    return "<html" in sample or "<body" in sample or "<a " in sample


def decode_http_body(body: bytes, content_type: str | None) -> str:
    charset = "utf-8"
    if content_type:
        match = re.search(r"charset=([^\s;]+)", content_type, re.IGNORECASE)
        if match:
            charset = match.group(1).strip("\"'")
    return body.decode(charset, errors="replace")


def parse_int(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def clean_google_href(href: str) -> str | None:
    if not href:
        return None

    parsed = urllib.parse.urlsplit(href)
    host = (parsed.hostname or "").lower()
    path = parsed.path or ""

    if href.startswith("/url?") or (re.search(r"(^|\.)google\.", host) and path == "/url"):
        query = dict(urllib.parse.parse_qsl(parsed.query))
        href = query.get("q") or query.get("url") or ""
        parsed = urllib.parse.urlsplit(href)
        host = (parsed.hostname or "").lower()
    elif href.startswith("/"):
        return None

    if not href.startswith(("http://", "https://")):
        return None

    if is_ignored_google_host(host):
        return None
    return href


def is_ignored_google_host(host: str) -> bool:
    if not host:
        return True
    ignored_hosts = {
        "webcache.googleusercontent.com",
        "accounts.google.com",
        "support.google.com",
        "policies.google.com",
    }
    return host in ignored_hosts or bool(re.search(r"(^|\.)google\.", host))


def safe_body_text(page) -> str:
    try:
        return page.locator("body").inner_text(timeout=5000)
    except Exception:
        try:
            return page.evaluate("() => document.body ? document.body.innerText : ''")
        except Exception:
            return ""


def clean_text(value: str) -> str:
    return " ".join((value or "").split())


def dedupe_keep_order(items: list[str]) -> list[str]:
    seen: set[str] = set()
    output: list[str] = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        output.append(item)
    return output


def env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def is_retryable_proxy_error(exc: Exception) -> bool:
    message = str(exc)
    return any(pattern in message for pattern in PROXY_RETRY_PATTERNS)


def normalize_proxy_value(value: str | None) -> str | None:
    if not value:
        return None
    proxy = value.strip().strip("\"'")
    if not proxy:
        return None
    if "://" in proxy:
        return proxy

    parts = proxy.split(":")
    if len(parts) >= 4:
        host = parts[0].strip()
        port = parts[1].strip()
        username = parts[2].strip()
        password = ":".join(parts[3:]).strip()
        if host and port and username and password:
            return (
                "http://"
                f"{urllib.parse.quote(username, safe='')}:"
                f"{urllib.parse.quote(password, safe='')}@"
                f"{host}:{port}"
            )
    return f"http://{proxy}"


def proxy_candidates_from_env() -> list[str]:
    candidates: list[str] = []

    for name in ("CLOAK_PROXY", "CLOAK_PROXY_BACKUP"):
        proxy = normalize_proxy_value(os.environ.get(name))
        if proxy:
            candidates.append(proxy)

    pool = os.environ.get("CLOAK_PROXY_POOL")
    if pool:
        for raw_proxy in re.split(r"[\s,]+", pool):
            proxy = normalize_proxy_value(raw_proxy)
            if proxy:
                candidates.append(proxy)

    return dedupe_keep_order(candidates)


def select_proxy(candidates: list[str]) -> str | None:
    if not candidates:
        return None

    raw_index = os.environ.get("CLOAK_PROXY_INDEX")
    if raw_index is not None:
        try:
            index = int(raw_index)
            if 0 <= index < len(candidates):
                return candidates[index]
        except ValueError:
            pass

    strategy = os.environ.get("CLOAK_PROXY_STRATEGY", "first").strip().lower()
    if strategy == "random":
        return random.choice(candidates)
    return candidates[0]
