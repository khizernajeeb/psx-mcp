"""PSX MCP server: Pakistan Stock Exchange data tools for Claude.

Run locally:   MCP_SECRET=devsecret python -m psx_mcp.server
Connector URL: https://<host>/<MCP_SECRET>/mcp
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import Annotated, Any, Literal

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pydantic import Field

from . import analytics
from .client import PSXClient, PSXError
from .parsers import PKT

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
log = logging.getLogger("psx_mcp")

INSTRUCTIONS = """\
Live and historical data from the Pakistan Stock Exchange (PSX) Data Portal.
Prices are in PKR and times in PKT. Outside market hours, "current" is the
last traded price of the most recent session.
Tips: use psx_market_summary for "how is the market", psx_top_movers for
gainers/losers/most active (shariah_only=True restricts to KMI All-Share members),
psx_quote for several symbols at once, psx_company for fundamentals/financials,
psx_price_history for returns/RSI/moving averages, psx_dividends for payouts,
psx_announcements for results and corporate notices, psx_screener to filter
all stocks by P/E, dividend yield, market cap, 1Y return, sector or index,
psx_compare for side-by-side, psx_portfolio to value holdings,
psx_recent_payouts for market-wide dividend news, psx_corporate_calendar for
AGMs/EOGMs, psx_financial_reports for report PDFs. psx_price_history also
accepts index codes (KSE100, KMI30). Symbols are PSX tickers
like MEBL, LUCK, OGDC; use psx_search when you only know the company name.
Data is scraped from the public portal and may lag slightly; it is not
investment advice."""

_client: PSXClient | None = None


def client() -> PSXClient:
    global _client
    if _client is None:
        _client = PSXClient()
    return _client


def _now() -> str:
    return datetime.now(PKT).strftime("%Y-%m-%d %H:%M PKT")


def ro(title: str) -> ToolAnnotations:
    return ToolAnnotations(title=title, readOnlyHint=True, destructiveHint=False, openWorldHint=True, idempotentHint=True)

mcp = FastMCP(
    "PSX",
    instructions=INSTRUCTIONS,
    stateless_http=True,
    json_response=True,
    streamable_http_path="/mcp",
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)


def _err(e: Exception) -> dict:
    return {"error": str(e), "fetched_at": _now()}


async def _mw_with_sectors() -> list[dict]:
    rows = await client().market_watch()
    names = await client().sector_names()
    return [{**r, "sector": names.get(r["sector_code"], r["sector_code"])} for r in rows]


def _slim(r: dict) -> dict:
    return {
        "symbol": r["symbol"],
        "name": r.get("name"),
        "sector": r.get("sector"),
        "price": r["current"],
        "change": r["change"],
        "change_pct": r["change_pct"],
        "volume": r["volume"],
        "open": r["open"],
        "high": r["high"],
        "low": r["low"],
        "ldcp": r["ldcp"],
        "shariah": r["shariah"],
    }


# =============================================================================== tools


@mcp.tool(annotations=ro("Market Summary"))
async def psx_market_summary() -> dict:
    """Snapshot of the whole PSX market right now: all index levels (KSE100, KSE30,
    KMI30, KMIALLSHR, ALLSHR, ...), market breadth (advancers/decliners/unchanged),
    total volume, and the top 5 gainers and losers (volume >= 10,000) and most
    active stocks."""
    try:
        idx = await client().indices()
        rows = await _mw_with_sectors()
    except PSXError as e:
        return _err(e)
    traded = [r for r in rows if (r["volume"] or 0) > 0]
    adv = sum(1 for r in traded if (r["change"] or 0) > 0)
    dec = sum(1 for r in traded if (r["change"] or 0) < 0)
    liquid = [r for r in traded if (r["volume"] or 0) >= 10_000]  # skip 100-share freak moves
    by = lambda k, rev, pool: [_slim(r) for r in sorted(pool, key=lambda r: r[k] or 0, reverse=rev)[:5]]  # noqa: E731
    return {
        "fetched_at": _now(),
        "indices": idx,
        "breadth": {
            "symbols_traded": len(traded),
            "advancers": adv,
            "decliners": dec,
            "unchanged": len(traded) - adv - dec,
            "total_volume": int(sum(r["volume"] or 0 for r in traded)),
        },
        "top_gainers": by("change_pct", True, liquid),
        "top_losers": by("change_pct", False, liquid),
        "most_active": by("volume", True, traded),
        "note": "Gainers/losers only include stocks with volume >= 10,000; use psx_top_movers for other filters.",
    }


@mcp.tool(annotations=ro("Stock Quote"))
async def psx_quote(
    symbols: Annotated[list[str], Field(description="One or more PSX tickers, e.g. ['MEBL','LUCK']", min_length=1, max_length=60)],
) -> dict:
    """Current price, change, change %, volume, day open/high/low and previous close
    (LDCP) for one or more PSX symbols. Also says whether each is Shariah-compliant
    (member of KMI All-Share)."""
    try:
        rows = {r["symbol"]: r for r in await _mw_with_sectors()}
    except PSXError as e:
        return _err(e)
    quotes, missing = [], []
    for s in symbols:
        s = s.upper().strip()
        if s in rows:
            quotes.append(_slim(rows[s]))
            continue
        try:  # not on today's market watch (suspended, debt, not traded): use company page
            c = await client().company(s)
            quotes.append(
                {
                    "symbol": s,
                    "name": c["name"],
                    "sector": c["sector"],
                    "price": c["price"],
                    "change": c["change"],
                    "change_pct": c["change_pct"],
                    "volume": c["stats"].get("volume"),
                    "ldcp": c["stats"].get("ldcp"),
                    "note": "from company page (not on today's market watch)",
                }
            )
        except PSXError:
            missing.append(s)
    out: dict[str, Any] = {"fetched_at": _now(), "quotes": quotes}
    if missing:
        out["not_found"] = missing
        out["hint"] = "Use psx_search to find the right ticker."
    return out


@mcp.tool(annotations=ro("Top Movers"))
async def psx_top_movers(
    category: Annotated[Literal["gainers", "losers", "active", "value"], Field(description="gainers/losers by % change, active by volume, value by traded value (price x volume)")] = "gainers",
    limit: Annotated[int, Field(ge=1, le=100)] = 10,
    shariah_only: Annotated[bool, Field(description="Only KMI All-Share (Shariah-compliant) members")] = False,
    sector: Annotated[str | None, Field(description="Filter by sector name (partial match), e.g. 'cement', 'banks', 'fertilizer'")] = None,
    min_volume: Annotated[int, Field(ge=0, description="Ignore thinly traded stocks below this volume")] = 0,
    min_price: Annotated[float, Field(ge=0, description="Ignore penny stocks below this price (PKR)")] = 0,
) -> dict:
    """Today's top gainers, losers, most active (volume) or highest traded value on
    PSX, with optional Shariah, sector, volume and price filters."""
    try:
        rows = await _mw_with_sectors()
    except PSXError as e:
        return _err(e)
    rows = [r for r in rows if (r["volume"] or 0) > 0 and (r["volume"] or 0) >= min_volume and (r["current"] or 0) >= min_price]
    if shariah_only:
        rows = [r for r in rows if r["shariah"]]
    if sector:
        q = sector.lower().rstrip("s")
        rows = [r for r in rows if q in (r.get("sector") or "").lower()]
    if category == "gainers":
        rows = sorted([r for r in rows if (r["change_pct"] or 0) > 0], key=lambda r: r["change_pct"], reverse=True)
    elif category == "losers":
        rows = sorted([r for r in rows if (r["change_pct"] or 0) < 0], key=lambda r: r["change_pct"])
    elif category == "active":
        rows = sorted(rows, key=lambda r: r["volume"] or 0, reverse=True)
    else:
        rows = sorted(rows, key=lambda r: (r["volume"] or 0) * (r["current"] or 0), reverse=True)
    out = []
    for r in rows[:limit]:
        s = _slim(r)
        s["traded_value_pkr"] = round((r["volume"] or 0) * (r["current"] or 0))
        out.append(s)
    return {"fetched_at": _now(), "category": category, "count": len(out), "results": out}


@mcp.tool(annotations=ro("Company Profile"))
async def psx_company(
    symbol: Annotated[str, Field(description="PSX ticker, e.g. MEBL")],
    sections: Annotated[
        list[Literal["quote", "profile", "equity", "financials", "ratios", "announcements"]] | None,
        Field(description="Limit output to these sections; default is all"),
    ] = None,
) -> dict:
    """Full company snapshot from its PSX page: price and trading stats (P/E TTM,
    52-week range, circuit breakers, bid/ask, 1-year and YTD change), business
    profile, key people, auditor, market cap, shares and free float, annual and
    quarterly financials (revenue/mark-up, profit after tax, EPS), ratios (net
    margin, EPS growth, PEG) and the latest announcements."""
    try:
        c = await client().company(symbol)
    except PSXError as e:
        return _err(e)
    c = dict(c)
    c["fetched_at"] = _now()
    c["units_note"] = "Financials are in PKR thousands (000s) except EPS (PKR). Market cap is PKR 000s."
    if sections:
        keep = {"symbol", "name", "sector", "fetched_at", "url", "units_note"}
        mapping = {
            "quote": ["price", "change", "change_pct", "as_of", "stats"],
            "profile": ["profile"],
            "equity": ["equity"],
            "financials": ["financials"],
            "ratios": ["ratios"],
            "announcements": ["recent_announcements"],
        }
        for s in sections:
            keep.update(mapping[s])
        c = {k: v for k, v in c.items() if k in keep}
    return c


_PERIOD_DAYS = {"1m": 31, "3m": 92, "6m": 183, "1y": 366, "3y": 1096, "5y": 1827, "max": 100000}


@mcp.tool(annotations=ro("Price History"))
async def psx_price_history(
    symbol: Annotated[str, Field(description="PSX ticker, or an index code such as KSE100, KMI30, ALLSHR")],
    period: Annotated[Literal["1m", "3m", "6m", "1y", "3y", "5y", "max"], Field(description="Window of bars to return")] = "3m",
    interval: Annotated[Literal["daily", "weekly", "intraday"], Field(description="intraday = today's ticks")] = "daily",
    include_bars: Annotated[bool, Field(description="False returns only the indicator summary (smaller)")] = True,
) -> dict:
    """Historical prices plus a technical summary: 1w/1m/3m/6m/YTD/1y/3y returns,
    SMA 20/50/200, RSI(14), MACD, 52-week high/low, 60-day annualized volatility
    and 20-day average volume. Daily bars have date, open, close, volume.
    Works for indices too (symbol='KSE100') for market history questions."""
    try:
        if interval == "intraday":
            ticks = await client().intraday(symbol)
            prices = [t["price"] for t in ticks]
            return {
                "symbol": symbol.upper(),
                "fetched_at": _now(),
                "ticks": len(ticks),
                "first": ticks[0] if ticks else None,
                "last": ticks[-1] if ticks else None,
                "high": max(prices) if prices else None,
                "low": min(prices) if prices else None,
                "total_volume": sum(t["volume"] or 0 for t in ticks),
                "series": ticks[-400:] if include_bars else None,
            }
        bars = await client().eod(symbol)
    except PSXError as e:
        return _err(e)
    if not bars:
        return {"symbol": symbol.upper(), "error": "No price history returned"}
    summary = analytics.summarize(bars)
    out: dict[str, Any] = {"symbol": symbol.upper(), "fetched_at": _now(), "summary": summary}
    if include_bars:
        last = datetime.fromisoformat(bars[-1]["date"]).date()
        from datetime import timedelta

        start = last - timedelta(days=_PERIOD_DAYS[period])
        window = [b for b in bars if datetime.fromisoformat(b["date"]).date() >= start]
        if interval == "weekly" or len(window) > 400:
            weekly: dict[tuple, dict] = {}
            for b in window:
                d = datetime.fromisoformat(b["date"]).date()
                key = d.isocalendar()[:2]
                w = weekly.get(key)
                if w is None:
                    weekly[key] = {"week_ending": b["date"], "open": b["open"], "close": b["close"], "volume": b["volume"] or 0}
                else:
                    w.update(week_ending=b["date"], close=b["close"], volume=w["volume"] + (b["volume"] or 0))
            window = list(weekly.values())
            out["interval"] = "weekly"
        else:
            out["interval"] = "daily"
        out["bars"] = window
        out["history_available_from"] = bars[0]["date"]
    return out


@mcp.tool(annotations=ro("Dividend History"))
async def psx_dividends(symbol: Annotated[str, Field(description="PSX ticker")]) -> dict:
    """Dividend / bonus / right-share payout history with book-closure dates.
    PSX quotes payouts as % of face value (usually PKR 10), so 75% = PKR 7.50
    per share; cash_per_share_pkr_if_face_10 does that conversion."""
    try:
        rows = await client().payouts(symbol)
    except PSXError as e:
        return _err(e)
    return {
        "symbol": symbol.upper(),
        "fetched_at": _now(),
        "payouts": rows,
        "note": "Check face value on the company page if it is not PKR 10 (some companies use PKR 5 or 1).",
    }


_ANN_TYPES = {"companies": "C", "psx": "E", "secp": "B", "cdc": "A", "nccpl": "D"}


@mcp.tool(annotations=ro("Corporate Announcements"))
async def psx_announcements(
    symbol: Annotated[str | None, Field(description="PSX ticker; omit for all companies")] = None,
    keyword: Annotated[str, Field(description="Search in titles, e.g. 'financial results', 'dividend', 'board meeting'")] = "",
    source: Annotated[Literal["companies", "psx", "secp", "cdc", "nccpl"], Field()] = "companies",
    limit: Annotated[int, Field(ge=1, le=100)] = 20,
    offset: Annotated[int, Field(ge=0)] = 0,
    date_from: Annotated[str, Field(description="YYYY-MM-DD, optional")] = "",
    date_to: Annotated[str, Field(description="YYYY-MM-DD, optional")] = "",
) -> dict:
    """Latest corporate announcements (financial results, board meetings, dividends,
    material information) with links to the PDF. Use keyword='financial results'
    to find the newest results."""
    try:
        res = await client().announcements(
            symbol or "", _ANN_TYPES[source], keyword, limit, offset, date_from, date_to
        )
    except PSXError as e:
        return _err(e)
    return {"fetched_at": _now(), **res}


@mcp.tool(annotations=ro("Index Constituents"))
async def psx_index_constituents(
    index: Annotated[str, Field(description="Index code such as KSE100, KSE30, KMI30, KMIALLSHR, ALLSHR (psx_market_summary lists every index code)")] = "KSE100",
    sort_by: Annotated[Literal["weight", "points", "change_pct", "market_cap", "volume"], Field()] = "weight",
    limit: Annotated[int, Field(ge=1, le=500)] = 100,
) -> dict:
    """Members of a PSX index with index weight %, points contributed today, price
    change, volume, free float and market cap (PKR millions). Good for "who is
    moving the KSE-100 today"."""
    try:
        rows = await client().constituents(index)
    except PSXError as e:
        return _err(e)
    key = {"weight": "index_weight_pct", "points": "index_points", "change_pct": "change_pct", "market_cap": "market_cap_mn", "volume": "volume"}[sort_by]
    rows = sorted(rows, key=lambda r: abs(r[key] or 0) if sort_by == "points" else (r[key] or 0), reverse=True)
    return {"index": index.upper(), "fetched_at": _now(), "count": len(rows), "constituents": rows[:limit]}


@mcp.tool(annotations=ro("Sector Overview"))
async def psx_sectors() -> dict:
    """Sector-level view: advancers/decliners/unchanged, turnover and market cap
    (PKR billions) for every PSX sector, sorted by market cap."""
    try:
        rows = await client().sectorwise()
    except PSXError as e:
        return _err(e)
    rows = sorted(rows, key=lambda r: r["market_cap_bn"] or 0, reverse=True)
    return {"fetched_at": _now(), "sectors": rows}


@mcp.tool(annotations=ro("Ticker Search"))
async def psx_search(
    query: Annotated[str, Field(description="Company name or partial ticker, e.g. 'meezan', 'lucky', 'engro'")],
    include_debt: bool = False,
    limit: Annotated[int, Field(ge=1, le=50)] = 15,
) -> dict:
    """Find PSX tickers by company name or ticker fragment. Returns symbol, name,
    sector and whether it is an ETF or debt instrument."""
    try:
        syms = await client().symbols()
    except PSXError as e:
        return _err(e)
    q = query.lower().strip()
    hits = [
        s for s in syms
        if (include_debt or not s["is_debt"]) and (q in (s["symbol"] or "").lower() or q in (s["name"] or "").lower())
    ]
    hits.sort(key=lambda s: (s["symbol"].lower() != q, not s["symbol"].lower().startswith(q), s["symbol"]))
    return {"query": query, "count": len(hits), "results": hits[:limit]}


_SCREEN_SORT = {
    "market_cap": "market_cap_pkr",
    "pe": "pe_ttm",
    "dividend_yield": "dividend_yield_pct",
    "change_1y": "change_1y_pct",
    "change_today": "change_pct",
    "avg_volume": "avg_volume_30d",
    "price": "price",
}


async def _screener_with_sectors() -> list[dict]:
    rows = await client().screener()
    names = await client().sector_names()
    return [{**r, "sector": names.get(r["sector_code"], r["sector_code"])} for r in rows]


@mcp.tool(annotations=ro("Stock Screener"))
async def psx_screener(
    sector: Annotated[str | None, Field(description="Sector name, partial match: 'cement', 'bank', 'fertilizer', 'oil & gas exploration', 'technology'")] = None,
    index: Annotated[str | None, Field(description="Only members of this index, e.g. KSE100, KSE30, KMI30, KMIALLSHR")] = None,
    shariah_only: bool = False,
    min_pe: float | None = None,
    max_pe: float | None = None,
    min_dividend_yield: Annotated[float | None, Field(description="Percent, e.g. 8 for 8%")] = None,
    min_market_cap_bn: Annotated[float | None, Field(description="PKR billions")] = None,
    max_market_cap_bn: Annotated[float | None, Field(description="PKR billions")] = None,
    min_change_1y: Annotated[float | None, Field(description="Minimum 1-year return %")] = None,
    max_change_1y: Annotated[float | None, Field(description="Maximum 1-year return %")] = None,
    min_avg_volume: Annotated[float | None, Field(description="Minimum 30-day average daily volume (liquidity)")] = None,
    min_price: float | None = None,
    max_price: float | None = None,
    sort_by: Literal["market_cap", "pe", "dividend_yield", "change_1y", "change_today", "avg_volume", "price"] = "market_cap",
    ascending: bool = False,
    limit: Annotated[int, Field(ge=1, le=200)] = 25,
) -> dict:
    """Stock screener over every listed PSX equity using valuation data: market cap,
    P/E (TTM), dividend yield, 1-year return, free float and 30-day average volume.
    Example: cheap Shariah cement stocks = sector='cement', shariah_only=True,
    max_pe=8, sort_by='pe', ascending=True. Stocks with no P/E (losses or no
    earnings data) are excluded whenever a P/E filter or P/E sort is used."""
    try:
        rows = await _screener_with_sectors()
    except PSXError as e:
        return _err(e)

    def ok(r: dict) -> bool:
        if shariah_only and not r["shariah"]:
            return False
        if sector and sector.lower().rstrip("s") not in (r.get("sector") or "").lower():
            return False
        if index and index.upper() not in r["listed_in"]:
            return False
        pe = r["pe_ttm"]
        if (min_pe is not None or max_pe is not None or sort_by == "pe") and (pe is None or pe <= 0):
            return False
        checks = [
            (min_pe, pe, 1), (max_pe, pe, -1),
            (min_dividend_yield, r["dividend_yield_pct"], 1),
            (min_market_cap_bn, (r["market_cap_pkr"] or 0) / 1e9, 1),
            (max_market_cap_bn, (r["market_cap_pkr"] or 0) / 1e9, -1),
            (min_change_1y, r["change_1y_pct"], 1), (max_change_1y, r["change_1y_pct"], -1),
            (min_avg_volume, r["avg_volume_30d"], 1),
            (min_price, r["price"], 1), (max_price, r["price"], -1),
        ]
        for bound, val, direction in checks:
            if bound is None:
                continue
            if val is None or (direction == 1 and val < bound) or (direction == -1 and val > bound):
                return False
        return True

    hits = [r for r in rows if ok(r)]
    key = _SCREEN_SORT[sort_by]
    hits.sort(key=lambda r: (r[key] is None, (r[key] or 0) if ascending else -(r[key] or 0)))
    out = []
    for r in hits[:limit]:
        out.append({
            "symbol": r["symbol"], "sector": r["sector"], "price": r["price"], "change_pct": r["change_pct"],
            "market_cap_bn": round((r["market_cap_pkr"] or 0) / 1e9, 2), "pe_ttm": r["pe_ttm"],
            "dividend_yield_pct": r["dividend_yield_pct"], "change_1y_pct": r["change_1y_pct"],
            "avg_volume_30d": r["avg_volume_30d"], "free_float_shares": r["free_float_shares"], "shariah": r["shariah"],
        })
    return {"fetched_at": _now(), "matches": len(hits), "returned": len(out), "results": out}


@mcp.tool(annotations=ro("Compare Stocks"))
async def psx_compare(
    symbols: Annotated[list[str], Field(description="2-10 tickers to compare side by side", min_length=2, max_length=10)],
    include_technicals: Annotated[bool, Field(description="Add RSI, SMA200 position and 1m/3m returns (slower)")] = True,
) -> dict:
    """Side-by-side comparison: price, today's change, market cap, P/E, dividend
    yield, 1-year return, liquidity, Shariah status and (optionally) RSI, trend vs
    200-day average and 1m/3m returns."""
    try:
        scr = {r["symbol"]: r for r in await _screener_with_sectors()}
    except PSXError as e:
        return _err(e)
    rows, missing = [], []
    for s in symbols:
        s = s.upper().strip()
        r = scr.get(s)
        if r is None:
            missing.append(s)
            continue
        row = {
            "symbol": s, "sector": r["sector"], "price": r["price"], "change_pct": r["change_pct"],
            "market_cap_bn": round((r["market_cap_pkr"] or 0) / 1e9, 2), "pe_ttm": r["pe_ttm"],
            "dividend_yield_pct": r["dividend_yield_pct"], "change_1y_pct": r["change_1y_pct"],
            "avg_volume_30d": r["avg_volume_30d"], "shariah": r["shariah"],
        }
        if include_technicals:
            try:
                sm = analytics.summarize(await client().eod(s))
                row.update(rsi_14=sm.get("rsi_14"), price_vs_sma200=sm.get("price_vs_sma200"),
                           return_1m_pct=sm["returns_pct"].get("1m"), return_3m_pct=sm["returns_pct"].get("3m"),
                           pct_from_52w_high=sm.get("pct_from_52w_high"))
            except PSXError:
                pass
        rows.append(row)
    out: dict[str, Any] = {"fetched_at": _now(), "comparison": rows}
    if missing:
        out["not_found"] = missing
    return out


@mcp.tool(annotations=ro("Portfolio Valuation"))
async def psx_portfolio(
    holdings: Annotated[
        list[dict],
        Field(description="List of {symbol, quantity, avg_cost}. avg_cost is your average buy price per share (PKR); optional."),
    ],
) -> dict:
    """Value a portfolio at live PSX prices: market value, cost, unrealized P&L
    (PKR and %), today's P&L, weight of each holding, sector allocation and
    Shariah share. Nothing is stored; holdings are only used for this answer."""
    try:
        live = {r["symbol"]: r for r in await _mw_with_sectors()}
    except PSXError as e:
        return _err(e)
    positions, missing = [], []
    tot_val = tot_cost = tot_day = val_with_cost = 0.0
    for h in holdings:
        sym = str(h.get("symbol", "")).upper().strip()
        qty = float(h.get("quantity") or h.get("qty") or 0)
        cost = h.get("avg_cost") if h.get("avg_cost") is not None else h.get("cost")
        r = live.get(sym)
        price = r["current"] if r else None
        ldcp = r["ldcp"] if r else None
        sector = r.get("sector") if r else None
        shariah = r["shariah"] if r else None
        if r is None:
            try:
                c = await client().company(sym)
                price, ldcp, sector = c["price"], c["stats"].get("ldcp"), c["sector"]
            except PSXError:
                missing.append(sym)
                continue
        value = qty * (price or 0)
        day = qty * ((price or 0) - (ldcp or price or 0))
        pos = {"symbol": sym, "quantity": qty, "price": price, "market_value": round(value, 2),
               "day_pnl": round(day, 2), "day_change_pct": r["change_pct"] if r else None,
               "sector": sector, "shariah": shariah}
        if cost is not None:
            c_total = qty * float(cost)
            pos.update(avg_cost=float(cost), cost_value=round(c_total, 2),
                       unrealized_pnl=round(value - c_total, 2),
                       unrealized_pnl_pct=round((value - c_total) / c_total * 100, 2) if c_total else None)
            tot_cost += c_total
            val_with_cost += value
        tot_val += value
        tot_day += day
        positions.append(pos)
    sectors: dict[str, float] = {}
    for p in positions:
        p["weight_pct"] = round(p["market_value"] / tot_val * 100, 2) if tot_val else None
        sectors[p["sector"] or "UNKNOWN"] = sectors.get(p["sector"] or "UNKNOWN", 0) + p["market_value"]
    positions.sort(key=lambda p: p["market_value"], reverse=True)
    shariah_val = sum(p["market_value"] for p in positions if p["shariah"])
    summary = {
        "market_value": round(tot_val, 2),
        "day_pnl": round(tot_day, 2),
        "day_pnl_pct": round(tot_day / (tot_val - tot_day) * 100, 2) if tot_val - tot_day else None,
        "shariah_pct_of_value": round(shariah_val / tot_val * 100, 2) if tot_val else None,
    }
    if tot_cost:
        # P&L only over positions whose cost was given.
        summary.update(cost_value=round(tot_cost, 2), unrealized_pnl=round(val_with_cost - tot_cost, 2),
                       unrealized_pnl_pct=round((val_with_cost - tot_cost) / tot_cost * 100, 2))
    out: dict[str, Any] = {
        "fetched_at": _now(),
        "summary": summary,
        "sector_allocation_pct": {k: round(v / tot_val * 100, 2) for k, v in sorted(sectors.items(), key=lambda x: -x[1])} if tot_val else {},
        "positions": positions,
        "note": "Excludes brokerage, CVT and taxes; P&L is on price only.",
    }
    if missing:
        out["not_found"] = missing
    return out


@mcp.tool(annotations=ro("Recent Payouts"))
async def psx_recent_payouts(
    symbol: Annotated[str | None, Field(description="Filter by ticker; omit for the whole market")] = None,
    only_cash: Annotated[bool, Field(description="Only cash dividends")] = False,
    limit: Annotated[int, Field(ge=1, le=100)] = 25,
    offset: Annotated[int, Field(ge=0)] = 0,
) -> dict:
    """Latest dividend, bonus and right-share announcements across all PSX
    companies (newest first) with book-closure dates. Use for "which companies
    announced dividends this week" or "upcoming book closures"."""
    try:
        res = await client().payouts_feed(symbol or "", limit, offset)
    except PSXError as e:
        return _err(e)
    items = res["items"]
    if only_cash:
        items = [i for i in items if i["type"] == "cash dividend"]
    return {"fetched_at": _now(), "total": res["total"], "items": items,
            "note": "Payout % is on face value (usually PKR 10): 100% = PKR 10/share."}


@mcp.tool(annotations=ro("Corporate Calendar"))
async def psx_corporate_calendar(
    date_from: Annotated[str | None, Field(description="YYYY-MM-DD; default today")] = None,
    date_to: Annotated[str | None, Field(description="YYYY-MM-DD; default 30 days after date_from")] = None,
    symbol: Annotated[str | None, Field(description="Filter by ticker")] = None,
    meeting_type: Annotated[Literal["AGM", "EOGM", "ARM"] | None, Field(description="AGM annual general, EOGM extraordinary general, ARM annual review")] = None,
) -> dict:
    """Upcoming (or past) shareholder meetings from the PSX corporate calendar:
    AGMs, EOGMs and annual review meetings with date, time, city and period."""
    from datetime import date as _date, timedelta

    try:
        start = _date.fromisoformat(date_from) if date_from else datetime.now(PKT).date()
        end = _date.fromisoformat(date_to) if date_to else start + timedelta(days=30)
    except ValueError:
        return {"error": "Dates must be YYYY-MM-DD"}
    if (end - start).days > 120:
        end = start + timedelta(days=120)
    try:
        events = await client().calendar(start.isoformat(), end.isoformat())
    except PSXError as e:
        return _err(e)
    if symbol:
        events = [e for e in events if (e["symbol"] or "").upper() == symbol.upper().strip()]
    if meeting_type:
        events = [e for e in events if e["type"] == meeting_type]
    return {"fetched_at": _now(), "from": start.isoformat(), "to": end.isoformat(), "count": len(events), "events": events}


@mcp.tool(annotations=ro("Financial Reports"))
async def psx_financial_reports(
    symbol: Annotated[str, Field(description="PSX ticker")],
    report_type: Annotated[Literal["all", "annual", "quarterly"], Field()] = "all",
    limit: Annotated[int, Field(ge=1, le=100)] = 10,
) -> dict:
    """Links to a company's filed annual and quarterly financial report PDFs,
    newest period first. Open the PDF link to read the full statements."""
    try:
        rows = await client().reports(symbol)
    except PSXError as e:
        return _err(e)
    if report_type != "all":
        rows = [r for r in rows if r["type"].lower().startswith(report_type[:6])]
    return {"symbol": symbol.upper(), "fetched_at": _now(), "count": len(rows), "reports": rows[:limit]}


@mcp.tool(annotations=ro("Server Self-Test"))
async def psx_selftest() -> dict:
    """Health check: calls every PSX endpoint this server depends on and reports
    which ones parse correctly. Use when other tools return errors."""
    c = client()
    c.cache.clear()
    checks: dict[str, Any] = {}

    async def run(name, coro, describe):
        try:
            v = await coro
            checks[name] = {"ok": True, "sample": describe(v)}
        except Exception as e:  # noqa: BLE001
            checks[name] = {"ok": False, "error": str(e)[:300]}

    await run("token", c._refresh_token(force=True), lambda t: f"token {'present' if t else 'MISSING'}")
    await run("market_watch", c.market_watch(), lambda v: f"{len(v)} symbols")
    await run("indices", c.indices(), lambda v: [(i["index"], i["current"]) for i in v[:3]])
    await run("symbols", c.symbols(), lambda v: f"{len(v)} listed")
    await run("sectorwise", c.sectorwise(), lambda v: f"{len(v)} sectors")
    await run("company", c.company("MEBL"), lambda v: {"price": v["price"], "pe": v["stats"].get("pe_ttm"), "fin": list(v["financials"])})
    await run("eod", c.eod("MEBL"), lambda v: f"{len(v)} bars, last {v[-1]['date'] if v else None}")
    await run("intraday", c.intraday("MEBL"), lambda v: f"{len(v)} ticks")
    await run("payouts", c.payouts("MEBL"), lambda v: f"{len(v)} payouts")
    await run("announcements", c.announcements("MEBL", count=3), lambda v: f"{len(v['items'])} items of {v['total']}")
    await run("constituents", c.constituents("KSE100"), lambda v: f"{len(v)} members")
    await run("screener", c.screener(), lambda v: f"{len(v)} stocks, MEBL pe {next((r['pe_ttm'] for r in v if r['symbol']=='MEBL'), None)}")
    await run("payouts_feed", c.payouts_feed(count=3), lambda v: f"{len(v['items'])} items of {v['total']}")
    _today = datetime.now(PKT).date()
    await run("calendar", c.calendar(_today.isoformat(), (_today + timedelta(days=30)).isoformat()), lambda v: f"{len(v)} events")
    await run("reports", c.reports("LUCK"), lambda v: f"{len(v)} reports, newest {v[0]['period_ended'] if v else None}")
    await run("index_history", c.eod("KSE100"), lambda v: f"{len(v)} bars, last {v[-1]['close'] if v else None}")
    if not checks["token"]["ok"]:
        try:
            checks["diagnostics"] = {"ok": True, "sample": await c.probe()}
        except Exception as e:  # noqa: BLE001
            checks["diagnostics"] = {"ok": False, "error": repr(e)[:300]}
    ok = sum(1 for k, v in checks.items() if v["ok"] and k != "diagnostics")
    total = len([k for k in checks if k != "diagnostics"])
    return {"fetched_at": _now(), "passed": f"{ok}/{total}", "checks": checks}


# =============================================================================== ASGI app


class SecretPathMiddleware:
    """Only /<secret>/mcp reaches the MCP app; /health is public; all else 404.

    claude.ai custom connectors take a URL (no custom headers), so the secret
    lives in the path. Treat the full URL like a password."""

    def __init__(self, app, secret: str | None):
        self.app = app
        self.prefix = f"/{secret}" if secret else ""

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        path = scope.get("path", "")
        if path in ("/health", "/"):
            body = b'{"status":"ok","service":"psx-mcp"}'
            await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"application/json")]})
            await send({"type": "http.response.body", "body": body})
            return
        if self.prefix:
            if not (path == self.prefix + "/mcp" or path.startswith(self.prefix + "/mcp/")):
                await send({"type": "http.response.start", "status": 404, "headers": [(b"content-type", b"text/plain")]})
                await send({"type": "http.response.body", "body": b"Not found"})
                return
            scope = dict(scope)
            scope["path"] = path[len(self.prefix):]
            scope["raw_path"] = scope["path"].encode()
        return await self.app(scope, receive, send)


def build_app():
    secret = os.environ.get("MCP_SECRET", "").strip()
    if not secret and os.environ.get("ALLOW_NO_AUTH") != "1":
        raise SystemExit("Set MCP_SECRET (a long random string) or ALLOW_NO_AUTH=1 for local testing.")
    if secret and len(secret) < 16:
        raise SystemExit("MCP_SECRET must be at least 16 characters.")
    inner = mcp.streamable_http_app()
    return SecretPathMiddleware(inner, secret or None)


class PerRequestMCP:
    """Serverless-friendly MCP endpoint (Vercel and similar).

    The normal app starts one session manager in the ASGI lifespan, which
    serverless runtimes may never send. In stateless mode nothing needs to
    live between requests, so each request gets its own short-lived manager."""

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            while True:
                msg = await receive()
                if msg["type"] == "lifespan.startup":
                    await send({"type": "lifespan.startup.complete"})
                elif msg["type"] == "lifespan.shutdown":
                    await send({"type": "lifespan.shutdown.complete"})
                    return
        if scope["type"] != "http":
            return
        if scope["path"].rstrip("/") != "/mcp":
            await send({"type": "http.response.start", "status": 404, "headers": [(b"content-type", b"text/plain")]})
            await send({"type": "http.response.body", "body": b"Not found"})
            return
        from mcp.server.streamable_http_manager import StreamableHTTPSessionManager

        mgr = StreamableHTTPSessionManager(
            app=mcp._mcp_server,
            json_response=True,
            stateless=True,
            security_settings=TransportSecuritySettings(enable_dns_rebinding_protection=False),
        )
        async with mgr.run():
            await mgr.handle_request(scope, receive, send)


async def _misconfigured(scope, receive, send):
    if scope["type"] != "http":
        return
    await send({"type": "http.response.start", "status": 500, "headers": [(b"content-type", b"text/plain")]})
    await send({"type": "http.response.body", "body": b"MCP_SECRET env var is missing or shorter than 16 characters"})


def build_serverless_app():
    secret = os.environ.get("MCP_SECRET", "").strip()
    if len(secret) < 16:
        return _misconfigured
    return SecretPathMiddleware(PerRequestMCP(), secret)


def main():
    import uvicorn

    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run(build_app(), host="0.0.0.0", port=port, log_level="info", proxy_headers=True, forwarded_allow_ips="*")


if __name__ == "__main__":
    main()
