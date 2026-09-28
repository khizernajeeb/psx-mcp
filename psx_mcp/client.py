"""Polite, cached async client for the PSX Data Portal (dps.psx.com.pk).

How the portal works (mapped Sep 2026):
  * Normal pages (/, /indices, /company/<SYM>) are plain HTML.
  * Data endpoints (/market-watch, /symbols, /timeseries/..., /announcements,
    /company/payouts, /sector-summary/sectorwise) only answer AJAX calls: they
    need `X-Requested-With: XMLHttpRequest` AND an `X-Req-Id` token that the
    portal embeds in every HTML page as `window.__ps._k`. Without the header
    they 404; with a missing/stale token they 403.
  * Rapid bursts get 403/503, so this client throttles, caches and retries.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any, Awaitable, Callable

import httpx

from . import parsers

log = logging.getLogger("psx_mcp.client")

BASE_URL = parsers.BASE_URL
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0 Safari/537.36"
)

# Cache lifetimes (seconds). Prices move, reference data doesn't.
TTL = {
    "market_watch": 30,
    "indices": 30,
    "constituents": 60,
    "intraday": 60,
    "sectorwise": 120,
    "company": 600,
    "announcements": 600,
    "eod": 3600,
    "payouts": 6 * 3600,
    "symbols": 24 * 3600,
}


class PSXError(RuntimeError):
    pass


class _TTLCache:
    def __init__(self) -> None:
        self._data: dict[str, tuple[float, Any]] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._loop: asyncio.AbstractEventLoop | None = None

    async def get_or_set(self, key: str, ttl: float, fn: Callable[[], Awaitable[Any]]) -> Any:
        hit = self._data.get(key)
        if hit and hit[0] > time.monotonic():
            return hit[1]
        if self._loop is not asyncio.get_running_loop():
            self._loop, self._locks = asyncio.get_running_loop(), {}
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:  # one in-flight fetch per key
            hit = self._data.get(key)
            if hit and hit[0] > time.monotonic():
                return hit[1]
            try:
                value = await fn()
            except Exception:
                if hit:  # serve stale data rather than fail outright
                    log.warning("serving stale cache for %s", key)
                    return hit[1]
                raise
            self._data[key] = (time.monotonic() + ttl, value)
            if len(self._data) > 2000:
                self._evict()
            return value

    def _evict(self) -> None:
        now = time.monotonic()
        for k in [k for k, (exp, _) in self._data.items() if exp < now]:
            self._data.pop(k, None)

    def clear(self) -> None:
        self._data.clear()

    def reset_locks(self) -> None:
        self._locks = {}


class PSXClient:
    def __init__(
        self,
        transport: httpx.AsyncBaseTransport | None = None,
        min_interval: float | None = None,
        max_retries: int = 3,
    ) -> None:
        self._transport = transport
        self._loop: asyncio.AbstractEventLoop | None = None
        self._http: httpx.AsyncClient | None = None
        self._token: str | None = None
        self._token_at = 0.0
        self._min_interval = (
            float(os.environ.get("PSX_MIN_INTERVAL", "0.35")) if min_interval is None else min_interval
        )
        self._last_req = 0.0
        self._max_retries = max_retries
        self.cache = _TTLCache()

    def _bind(self) -> None:
        """(Re)create loop-bound objects when the running event loop changes.

        Long-running servers keep one loop forever. Serverless hosts (Vercel)
        may run each request on a fresh loop while keeping this module alive;
        httpx clients and asyncio locks cannot cross loops, but cached data can."""
        loop = asyncio.get_running_loop()
        if loop is self._loop and self._http is not None:
            return
        self._loop = loop
        self._http = httpx.AsyncClient(
            base_url=BASE_URL,
            timeout=httpx.Timeout(20.0, connect=10.0),
            headers={"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9", "Referer": BASE_URL + "/"},
            transport=self._transport,
            follow_redirects=True,
        )
        self._token_lock = asyncio.Lock()
        self._sem = asyncio.Semaphore(2)
        self._pace_lock = asyncio.Lock()

    async def aclose(self) -> None:
        if self._http is not None:
            await self._http.aclose()

    # ------------------------------------------------------------------ low level

    async def _pace(self) -> None:
        async with self._pace_lock:
            wait = self._last_req + self._min_interval - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_req = time.monotonic()

    async def _raw(self, method: str, path: str, **kw: Any) -> httpx.Response:
        self._bind()
        async with self._sem:
            await self._pace()
            return await self._http.request(method, path, **kw)

    async def _refresh_token(self, force: bool = False) -> str | None:
        self._bind()
        async with self._token_lock:
            if self._token and not force and time.monotonic() - self._token_at < 3 * 3600:
                return self._token
            r = await self._raw("GET", "/", headers={"Accept": "text/html"})
            if r.status_code != 200:
                raise PSXError(f"PSX homepage returned HTTP {r.status_code} while fetching access token")
            tok = parsers.extract_token(r.text)
            if not tok:
                log.warning("No X-Req-Id token found on PSX homepage; continuing without it")
            self._token, self._token_at = tok, time.monotonic()
            return tok

    async def page(self, path: str) -> str:
        """Plain HTML page."""
        return await self._request("GET", path, ajax=False)

    async def ajax(self, path: str, method: str = "GET", data: dict | None = None) -> str:
        return await self._request(method, path, ajax=True, data=data)

    async def _request(self, method: str, path: str, ajax: bool, data: dict | None = None) -> str:
        last: str = ""
        for attempt in range(self._max_retries + 1):
            headers = {"Accept": "text/html, application/json, */*; q=0.01"}
            if ajax:
                tok = await self._refresh_token()
                headers["X-Requested-With"] = "XMLHttpRequest"
                if tok:
                    headers["X-Req-Id"] = tok
            try:
                r = await self._raw(method, path, headers=headers, data=data)
            except httpx.HTTPError as e:
                last = f"network error: {e!r}"
                await asyncio.sleep(0.8 * (attempt + 1))
                continue
            if r.status_code == 200:
                return r.text
            last = f"{r.status_code}"
            if r.status_code in (403, 404) and ajax and attempt == 0:
                # Stale/missing token: refresh and retry once immediately.
                last = "403"
                await self._refresh_token(force=True)
                continue
            if r.status_code == 404:
                raise PSXError(f"PSX returned 404 for {path} (unknown symbol or endpoint moved)")
            if r.status_code in (403, 429, 500, 502, 503, 504):
                await asyncio.sleep(1.0 * (attempt + 1))
                continue
            raise PSXError(f"PSX returned HTTP {r.status_code} for {path}")
        raise PSXError(
            f"PSX did not answer {path} after {self._max_retries + 1} tries (last: {last}). "
            "The portal may be rate-limiting or blocking this server's IP."
        )

    # ------------------------------------------------------------------ typed fetchers

    async def market_watch(self) -> list[dict]:
        async def f():
            rows = parsers.parse_market_watch(await self.ajax("/market-watch"))
            if not rows:
                raise PSXError("Market watch came back empty (layout change?)")
            return rows

        return await self.cache.get_or_set("mw", TTL["market_watch"], f)

    async def symbols(self) -> list[dict]:
        async def f():
            return parsers.parse_symbols(await self.ajax("/symbols"))

        return await self.cache.get_or_set("symbols", TTL["symbols"], f)

    async def sectorwise(self) -> list[dict]:
        async def f():
            return parsers.parse_sectorwise(await self.ajax("/sector-summary/sectorwise"))

        return await self.cache.get_or_set("sectorwise", TTL["sectorwise"], f)

    async def sector_names(self) -> dict[str, str]:
        try:
            return {s["sector_code"]: s["sector"] for s in await self.sectorwise()}
        except PSXError:
            return {}

    async def indices(self) -> list[dict]:
        async def f():
            return parsers.parse_indices(await self.page("/indices"))

        return await self.cache.get_or_set("indices", TTL["indices"], f)

    async def constituents(self, index: str) -> list[dict]:
        code = index.upper().replace("-", "")

        async def f():
            return parsers.parse_index_constituents(await self.ajax(f"/indices/{code}"))

        return await self.cache.get_or_set(f"const:{code}", TTL["constituents"], f)

    async def company(self, symbol: str) -> dict:
        sym = symbol.upper().strip()

        async def f():
            return parsers.parse_company(await self.page(f"/company/{sym}"), sym)

        return await self.cache.get_or_set(f"company:{sym}", TTL["company"], f)

    async def payouts(self, symbol: str) -> list[dict]:
        sym = symbol.upper().strip()

        async def f():
            return parsers.parse_payouts(await self.ajax("/company/payouts", "POST", {"symbol": sym}))

        return await self.cache.get_or_set(f"payouts:{sym}", TTL["payouts"], f)

    async def eod(self, symbol: str) -> list[dict]:
        sym = symbol.upper().strip()

        async def f():
            return parsers.parse_timeseries(await self.ajax(f"/timeseries/eod/{sym}"), "eod")

        return await self.cache.get_or_set(f"eod:{sym}", TTL["eod"], f)

    async def intraday(self, symbol: str) -> list[dict]:
        sym = symbol.upper().strip()

        async def f():
            return parsers.parse_timeseries(await self.ajax(f"/timeseries/int/{sym}"), "int")

        return await self.cache.get_or_set(f"int:{sym}", TTL["intraday"], f)

    async def announcements(
        self,
        symbol: str = "",
        kind: str = "C",
        keyword: str = "",
        count: int = 20,
        offset: int = 0,
        date_from: str = "",
        date_to: str = "",
    ) -> dict:
        form = {
            "type": kind,
            "symbol": symbol.upper().strip(),
            "query": keyword,
            "count": str(count),
            "offset": str(offset),
            "date_from": date_from,
            "date_to": date_to,
            "page": "annc",
        }
        key = "ann:" + "|".join(form.values())

        async def f():
            return parsers.parse_announcements(await self.ajax("/announcements", "POST", form))

        return await self.cache.get_or_set(key, TTL["announcements"], f)
