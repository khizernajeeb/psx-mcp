"""End-to-end: real MCP client -> uvicorn -> server -> mocked PSX portal."""
import asyncio, json, socket, threading, time

import httpx
import pytest
import uvicorn
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from conftest import fx
from psx_mcp import server as S
from psx_mcp.client import PSXClient

SECRET = "test-secret-0123456789"
TOKEN = "TESTTOKEN_abcdefghijklmnopqrstuvwxyz0123"
CALLS = []


def portal(request: httpx.Request) -> httpx.Response:
    """Mimics the real portal's gatekeeping: AJAX endpoints need both headers."""
    path = request.url.path
    CALLS.append(path)
    ajax_paths = ("/market-watch", "/symbols", "/timeseries/", "/announcements", "/company/payouts",
                  "/sector-summary/sectorwise", "/indices/", "/payouts", "/calendar",
                  "/company/reports/")
    is_ajax = any(path.startswith(p) for p in ajax_paths)
    if is_ajax:
        if request.headers.get("X-Requested-With") != "XMLHttpRequest":
            return httpx.Response(404, text="Not Found page")
        if request.headers.get("X-Req-Id") != TOKEN:
            return httpx.Response(403)
    routes = {
        "/": ("home.html", "text/html"),
        "/market-watch": ("market_watch.html", "text/html"),
        "/symbols": ("symbols.json", "application/json"),
        "/sector-summary/sectorwise": ("sectorwise.html", "text/html"),
        "/indices": ("indices.html", "text/html"),
        "/indices/KSE100": ("kse100.html", "text/html"),
        "/company/MEBL": ("company_MEBL.html", "text/html"),
        "/company/payouts": ("payouts.html", "text/html"),
        "/announcements": ("announcements.html", "text/html"),
        "/timeseries/eod/MEBL": ("eod_MEBL.json", "application/json"),
        "/timeseries/int/MEBL": ("int_MEBL.json", "application/json"),
        "/timeseries/eod/KSE100": ("eod_MEBL.json", "application/json"),
        "/screener": ("screener.html", "text/html"),
        "/payouts": ("payouts_feed.html", "text/html"),
        "/calendar": ("calendar.json", "application/json"),
        "/company/reports/LUCK": ("reports_LUCK.html", "text/html"),
    }
    if path in routes:
        name, ct = routes[path]
        return httpx.Response(200, text=fx(name), headers={"content-type": ct})
    return httpx.Response(404, text="Not Found page")


def _free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


@pytest.fixture(scope="module", params=["server", "serverless"])
def base_url(request):
    import os
    os.environ["MCP_SECRET"] = SECRET
    # This file only exercises PSX-facing tools, deliberately with no DB
    # configured -- unset DATABASE_URL/CRON_SECRET so its psx_selftest
    # assertion is deterministic regardless of what other test modules
    # (e.g. test_extensions.py) left in the environment.
    os.environ.pop("DATABASE_URL", None)
    os.environ.pop("CRON_SECRET", None)
    S._client = PSXClient(transport=httpx.MockTransport(portal), min_interval=0)
    port = _free_port()
    app = S.build_app() if request.param == "server" else S.build_serverless_app()
    cfg = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    srv = uvicorn.Server(cfg)
    t = threading.Thread(target=srv.run, daemon=True); t.start()
    for _ in range(100):
        if srv.started: break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    srv.should_exit = True; t.join(5)


async def _call(url, tool, args=None):
    async with streamablehttp_client(url) as (r, w, _):
        async with ClientSession(r, w) as s:
            await s.initialize()
            res = await s.call_tool(tool, args or {})
            assert not res.isError, res.content
            return json.loads(res.content[0].text)


def run(coro):
    return asyncio.run(coro)


def test_auth_paths(base_url):
    assert httpx.get(base_url + "/health").json()["status"] == "ok"
    assert httpx.post(base_url + "/mcp", json={}).status_code == 404
    assert httpx.post(base_url + "/wrong-secret-xxxxxxxx/mcp", json={}).status_code == 404


def test_list_tools(base_url):
    async def go():
        async with streamablehttp_client(f"{base_url}/{SECRET}/mcp") as (r, w, _):
            async with ClientSession(r, w) as s:
                init = await s.initialize()
                tools = await s.list_tools()
                return init, [t.name for t in tools.tools]
    init, names = run(go())
    assert init.serverInfo.name == "PSX"
    assert {"psx_market_summary", "psx_quote", "psx_top_movers", "psx_company", "psx_price_history",
            "psx_dividends", "psx_announcements", "psx_index_constituents", "psx_sectors", "psx_search",
            "psx_selftest"} <= set(names)


def U(base):
    return f"{base}/{SECRET}/mcp"


def test_market_summary(base_url):
    d = run(_call(U(base_url), "psx_market_summary"))
    assert d["indices"][0]["index"] == "KSE100"
    assert d["breadth"] == {"symbols_traded": 5, "advancers": 3, "decliners": 2, "unchanged": 0,
                            "total_volume": 53237236 + 82407 + 9780700 + 1883930 + 1200000}
    assert d["top_gainers"][0]["symbol"] == "TISL" and d["top_losers"][0]["symbol"] == "FPJM"
    assert "ZZZ" not in [g["symbol"] for g in d["top_gainers"] + d["top_losers"]]
    assert d["most_active"][0]["sector"] == "MISCELLANEOUS"


def test_quote_and_movers(base_url):
    d = run(_call(U(base_url), "psx_quote", {"symbols": ["mebl", "luck", "NOPE"]}))
    assert [q["symbol"] for q in d["quotes"]] == ["MEBL", "LUCK"] and d["not_found"] == ["NOPE"]
    assert d["quotes"][0]["sector"] == "COMMERCIAL BANKS" and d["quotes"][0]["shariah"] is True
    g = run(_call(U(base_url), "psx_top_movers", {"category": "gainers", "shariah_only": True}))
    assert [r["symbol"] for r in g["results"]] == ["TISL", "LUCK"]
    c = run(_call(U(base_url), "psx_top_movers", {"category": "active", "sector": "cement"}))
    assert [r["symbol"] for r in c["results"]] == ["LUCK"]
    v = run(_call(U(base_url), "psx_top_movers", {"category": "value", "limit": 2, "min_price": 10}))
    assert v["results"][0]["symbol"] == "LUCK"


def test_company_history_dividends(base_url):
    c = run(_call(U(base_url), "psx_company", {"symbol": "MEBL", "sections": ["quote", "equity"]}))
    assert c["stats"]["pe_ttm"] == 10.78 and "financials" not in c and c["equity"]["free_float_pct"] == 24.91
    h = run(_call(U(base_url), "psx_price_history", {"symbol": "MEBL", "period": "1m"}))
    sm = h["summary"]
    assert sm["last_close"] == 548.12 and 0 <= sm["rsi_14"] <= 100 and sm["sma"]["200"] is not None
    assert h["interval"] == "daily" and 15 <= len(h["bars"]) <= 25
    w = run(_call(U(base_url), "psx_price_history", {"symbol": "MEBL", "period": "max", "include_bars": True}))
    assert w["interval"] == "weekly"
    i = run(_call(U(base_url), "psx_price_history", {"symbol": "MEBL", "interval": "intraday"}))
    assert i["ticks"] == 60 and i["last"]["price"] == 547.94
    dv = run(_call(U(base_url), "psx_dividends", {"symbol": "MEBL"}))
    assert dv["payouts"][1]["cash_per_share_pkr_if_face_10"] == 7.5


def test_misc_tools(base_url):
    a = run(_call(U(base_url), "psx_announcements", {"symbol": "MEBL", "limit": 3}))
    assert a["total"] == 732
    k = run(_call(U(base_url), "psx_index_constituents", {"sort_by": "points", "limit": 2}))
    assert [r["symbol"] for r in k["constituents"]] == ["LUCK", "MEBL"]
    s = run(_call(U(base_url), "psx_sectors"))
    assert s["sectors"][0]["sector"] == "COMMERCIAL BANKS"
    q = run(_call(U(base_url), "psx_search", {"query": "bank"}))
    assert [r["symbol"] for r in q["results"]] == ["BAHL", "MEBL"]
    e = run(_call(U(base_url), "psx_company", {"symbol": "XXXX"}))
    assert "error" in e
    st = run(_call(U(base_url), "psx_selftest"))
    # 16 PSX checks pass; the 6 DB/cron checks fail gracefully since no
    # DATABASE_URL is configured in this file (see tests/test_extensions.py
    # for those, against a real Postgres).
    assert st["passed"] == "16/22", st
    assert st["checks"]["db_connectivity"]["ok"] is False


def test_token_refresh_on_403():
    """Stale token -> 403 -> client refreshes from homepage and succeeds."""
    async def go():
        c = PSXClient(transport=httpx.MockTransport(portal), min_interval=0)
        c._token, c._token_at = "stale", time.monotonic()
        rows = await c.market_watch()
        await c.aclose()
        return rows
    assert len(run(go())) == 6


def test_client_survives_new_event_loops():
    """Serverless hosts may use a fresh loop per request; cached client must cope."""
    c = PSXClient(transport=httpx.MockTransport(portal), min_interval=0)
    assert len(asyncio.run(c.market_watch())) == 6
    c.cache.clear()
    assert len(asyncio.run(c.market_watch())) == 6
    assert asyncio.run(c.company("MEBL"))["price"] == 547.94


def test_serverless_requires_secret(monkeypatch):
    monkeypatch.setenv("MCP_SECRET", "short")
    assert S.build_serverless_app() is S._misconfigured


def test_screener_compare_portfolio(base_url):
    d = run(_call(U(base_url), "psx_screener", {"sector": "cement", "max_pe": 10, "sort_by": "pe", "ascending": True}))
    assert [r["symbol"] for r in d["results"]] == ["LUCK"]  # DGKC pe 12 filtered, LOSS has no pe
    d = run(_call(U(base_url), "psx_screener", {"index": "KSE100", "min_dividend_yield": 2}))
    assert [r["symbol"] for r in d["results"]] == ["MEBL", "LUCK"] and d["results"][0]["market_cap_bn"] == 990.18
    d = run(_call(U(base_url), "psx_screener", {"shariah_only": True, "sector": "cement"}))
    assert [r["symbol"] for r in d["results"]] == ["LUCK", "LOSS"]
    c = run(_call(U(base_url), "psx_compare", {"symbols": ["MEBL", "LUCK", "NOPE"]}))
    assert [r["symbol"] for r in c["comparison"]] == ["MEBL", "LUCK"] and c["not_found"] == ["NOPE"]
    assert "rsi_14" in c["comparison"][0]
    p = run(_call(U(base_url), "psx_portfolio", {"holdings": [
        {"symbol": "MEBL", "quantity": 100, "avg_cost": 500},
        {"symbol": "LUCK", "quantity": 10, "avg_cost": 400},
        {"symbol": "WTL", "quantity": 1000}]}))
    sm = p["summary"]
    assert sm["market_value"] == round(100 * 547.94 + 10 * 450.5 + 1000 * 1.05, 2)
    assert sm["cost_value"] == 54000 and sm["unrealized_pnl"] == round(100 * 47.94 + 10 * 50.5, 2)
    assert p["positions"][0]["symbol"] == "MEBL" and p["positions"][0]["unrealized_pnl"] == 4794.0
    assert round(sum(p["sector_allocation_pct"].values())) == 100


def test_payouts_calendar_reports_index(base_url):
    f = run(_call(U(base_url), "psx_recent_payouts", {"only_cash": True}))
    assert f["total"] == 682 and [i["symbol"] for i in f["items"]] == ["STJT", "SAPT"]
    assert f["items"][0]["cash_per_share_pkr_if_face_10"] == 11.5 and f["items"][0]["sequence"] == "final"
    cal = run(_call(U(base_url), "psx_corporate_calendar", {"date_from": "2026-10-01", "date_to": "2026-10-31"}))
    assert [e["symbol"] for e in cal["events"]] == ["LUCK", "SLCL", "GWLC"]
    cal = run(_call(U(base_url), "psx_corporate_calendar", {"date_from": "2026-10-01", "meeting_type": "AGM"}))
    assert cal["count"] == 2
    r = run(_call(U(base_url), "psx_financial_reports", {"symbol": "LUCK", "report_type": "annual"}))
    assert r["count"] == 1 and r["reports"][0]["pdf"].startswith("https://financials.psx.com.pk/")
    r = run(_call(U(base_url), "psx_financial_reports", {"symbol": "LUCK"}))
    assert r["reports"][0]["period_ended"] == "2026-03-31"
    h = run(_call(U(base_url), "psx_price_history", {"symbol": "KSE100", "include_bars": False}))
    assert h["summary"]["last_close"] == 548.12
