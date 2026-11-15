"""Network access to Bina.az: throttled, with bounded retries and a clear error model.

  BinaBlocked     the site refuses us (HTTP 403 / 429 / 522 ...). NEVER retried here;
                  the monitor pauses all requests (cool-down) and tells the admin.
  BinaUnavailable network error / HTTP 5xx that survived the configured retries.
  SchemaError     the answer changed shape (see parser.py).
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from email.utils import parsedate_to_datetime

from .parser import Ad, parse_search_payload

log = logging.getLogger("bina")

GRAPHQL_URL = "https://bina.az/graphql"
PAGE_SIZE = 16
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36")
API_HEADERS = {"User-Agent": UA, "Accept": "*/*",
               "Accept-Language": "az,en-US;q=0.9,en;q=0.8,ru;q=0.7",
               "Content-Type": "application/json", "Referer": "https://bina.az/",
               "Origin": "https://bina.az", "x-platform": "desktop"}
IMG_HEADERS = {"User-Agent": UA, "Accept": "image/avif,image/webp,image/*,*/*;q=0.8",
               "Referer": "https://bina.az/"}
HTML_HEADERS = {"User-Agent": UA, "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
                "Accept-Language": "az,en-US;q=0.9,en;q=0.8,ru;q=0.7"}
RETRYABLE = {500, 502, 503, 504}


class BinaBlocked(Exception):
    def __init__(self, status: int, retry_after: float | None = None):
        super().__init__(f"HTTP {status}")
        self.status, self.retry_after = status, retry_after


class BinaUnavailable(Exception):
    pass


@dataclass
class Response:
    status: int
    headers: dict
    content: bytes

    def json(self):
        return json.loads(self.content.decode("utf-8", "replace"))


class Http:
    """Tiny async GET wrapper: curl_cffi (Chrome TLS fingerprint) when installed,
    otherwise aiohttp. Tests replace this object with a fake."""

    def __init__(self, timeout: float = 30):
        self.timeout = timeout
        self._s = None
        self._kind = None

    async def _session(self):
        if self._s is None:
            try:
                from curl_cffi.requests import AsyncSession
                self._s, self._kind = AsyncSession(impersonate="chrome"), "curl"
            except Exception:
                import aiohttp
                self._s, self._kind = aiohttp.ClientSession(), "aiohttp"
        return self._s

    async def get(self, url, params=None, headers=None) -> Response:
        s = await self._session()
        if self._kind == "curl":
            r = await s.get(url, params=params, headers=headers, timeout=self.timeout)
            return Response(r.status_code, {k.lower(): v for k, v in r.headers.items()}, r.content)
        import aiohttp
        async with s.get(url, params=params, headers=headers,
                         timeout=aiohttp.ClientTimeout(total=self.timeout)) as r:
            return Response(r.status, {k.lower(): v for k, v in r.headers.items()}, await r.read())

    async def close(self):
        if self._s is not None:
            await self._s.close()
            self._s = None


def _retry_after(headers) -> float | None:
    v = str((headers or {}).get("retry-after") or "").strip()
    if not v:
        return None
    if v.isdigit():
        return float(v)
    try:
        return max(0.0, parsedate_to_datetime(v).timestamp() - time.time())
    except Exception:
        return None


class BinaClient:
    def __init__(self, http, persisted_hash: str, min_interval: float = 3.0, retries: int = 2,
                 sleep=asyncio.sleep):
        self.http, self.hash = http, persisted_hash
        self.min_interval, self.retries = min_interval, retries
        self._sleep = sleep
        self._lock = asyncio.Lock()
        self._last = 0.0
        self.requests_made = 0

    async def _get(self, url, params=None, headers=None) -> Response:
        """One polite request: global spacing; retries ONLY for network errors / 5xx."""
        last = "cavab yoxdur"
        for attempt in range(self.retries + 1):
            async with self._lock:                       # serialises + spaces all requests
                wait = self._last + self.min_interval - time.monotonic()
                if wait > 0:
                    await self._sleep(wait)
                try:
                    self.requests_made += 1
                    r = await self.http.get(url, params=params, headers=headers)
                except asyncio.CancelledError:
                    raise
                except Exception as e:                   # timeout, DNS, reset ...
                    r, last = None, f"şəbəkə xətası: {e.__class__.__name__}"
                finally:
                    self._last = time.monotonic()
            if r is not None:
                if r.status == 200 or r.status in (404, 410):
                    return r
                if r.status not in RETRYABLE:            # 403, 429, 522, anything odd
                    raise BinaBlocked(r.status, _retry_after(r.headers))
                last = f"HTTP {r.status}"
            if attempt < self.retries:
                await self._sleep(2.0 * (attempt + 1))
        raise BinaUnavailable(last)

    def _params(self, flt: dict, cursor: str | None) -> dict:
        variables = {"first": PAGE_SIZE, "filter": flt, "sort": "BUMPED_AT_DESC"}
        if cursor:
            variables["cursor"] = cursor
        return {"operationName": "SearchItems",
                "variables": json.dumps(variables, separators=(",", ":"), ensure_ascii=False),
                "extensions": json.dumps({"persistedQuery": {"version": 1, "sha256Hash": self.hash}},
                                         separators=(",", ":"))}

    async def search_page(self, flt: dict, cursor: str | None = None):
        r = await self._get(GRAPHQL_URL, params=self._params(flt, cursor), headers=API_HEADERS)
        if r.status != 200:
            raise BinaBlocked(r.status)
        try:
            payload = r.json()
        except ValueError:
            raise BinaUnavailable("cavab JSON deyil")
        return parse_search_payload(payload)

    async def search(self, flt: dict, max_pages: int, keep_going=None) -> list[Ad]:
        """Newest-first pages. After each page `keep_going(page_ads)` decides whether the
        next page is needed, so a quiet search costs exactly one request."""
        out, cursor = [], None
        for page in range(max_pages):
            ads, cursor, _total = await self.search_page(flt, cursor)
            out.extend(ads)
            if not cursor or not ads:
                break
            if keep_going is not None and not await keep_going(ads):
                break
        return out

    async def fetch_image(self, url: str | None) -> bytes | None:
        if not url:
            return None
        try:
            r = await self._get(url, headers=IMG_HEADERS)
        except (BinaBlocked, BinaUnavailable) as e:
            log.info("image not fetched (%s): %s", e, url)
            return None
        if r.status == 200 and len(r.content) > 1000:
            return r.content
        return None

    async def ad_exists(self, url: str):
        """True / False (deleted) / None (could not tell - never blocks a notification)."""
        try:
            r = await self._get(url, headers=HTML_HEADERS)
        except (BinaBlocked, BinaUnavailable):
            return None
        return False if r.status in (404, 410) else True
