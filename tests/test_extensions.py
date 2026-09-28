"""Watchlist, alerts, insider/corporate activity, why_moved and the
/api/cron/check-alerts endpoint: real MCP client -> uvicorn -> server, with a
mocked PSX portal (like test_server.py) plus a real, disposable Postgres.

Needs a reachable Postgres; set TEST_DATABASE_URL to run, e.g.:
    docker run --rm -d -p 5432:5432 -e POSTGRES_PASSWORD=postgres postgres:16
    TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/postgres pytest tests/test_extensions.py
"""
import asyncio, json, os, socket, threading, time
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl

import httpx
import psycopg
import pytest
import uvicorn
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from psx_mcp import db, server as S
from psx_mcp.client import PSXClient
from psx_mcp.db import MIGRATIONS_DIR

SECRET = "test-secret-0123456789"
CRON_SECRET = "test-cron-secret-xxxxxxxxxxxx"
TOKEN = "TESTTOKEN_abcdefghijklmnopqrstuvwxyz0123"

DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="set TEST_DATABASE_URL to run DB-backed tests")

SYMBOLS = [
    {"symbol": "MEBL", "name": "Meezan Bank Limited", "sectorName": "COMMERCIAL BANKS", "isETF": False, "isDebt": False},
    {"symbol": "LUCK", "name": "Lucky Cement Limited", "sectorName": "CEMENT", "isETF": False, "isDebt": False},
]
MARKET_WATCH_ROWS = [
    # symbol, current, change, change%, volume, open, high, low, ldcp
    ("MEBL", 350.0, 5.0, 1.45, 2_000_000, 345.0, 352.0, 344.0, 345.0),
    ("LUCK", 900.0, -10.0, -1.10, 500_000, 910.0, 912.0, 895.0, 910.0),
]
SCREENER_ROWS = {  # symbol -> avg_volume_30d
    "MEBL": 500_000,
    "LUCK": 400_000,
}

ANNOUNCEMENTS = [
    {"date": "Sep 22, 2026", "time": "1:59 PM", "symbol": "MEBL", "company": "Meezan Bank Limited",
     "title": "Transaction in Shares by a Director", "id": "300001"},
    {"date": "Sep 10, 2026", "time": "10:24 AM", "symbol": "MEBL", "company": "Meezan Bank Limited",
     "title": "Notice of Right Shares", "id": "300002"},
    {"date": "Sep 2, 2026", "time": "10:30 AM", "symbol": "MEBL", "company": "Meezan Bank Limited",
     "title": "Quarterly Financial Results", "id": "300003"},
    {"date": "Sep 20, 2026", "time": "11:00 AM", "symbol": "LUCK", "company": "Lucky Cement Limited",
     "title": "Board Meeting Notice", "id": "300004"},
]


def _ann_html(rows: list[dict]) -> str:
    trs = "".join(
        f'<tr><td>{r["date"]}</td><td>{r["time"]}</td>'
        f'<td><a href="/company/{r["symbol"]}">{r["symbol"]}</a></td>'
        f'<td>{r["company"]}</td><td>{r["title"]}</td>'
        f'<td><a href="/download/document/{r["id"]}.pdf">PDF</a></td></tr>'
        for r in rows
    )
    return (
        f'<div class="announcementsResults"><div>Showing 1 to {len(rows)} of {len(rows)} entries</div>'
        f'<table class="tbl"><thead><tr><th>DATE</th><th>TIME</th><th>SYMBOL</th>'
        f'<th>NAME</th><th>TITLE</th><th></th></tr></thead><tbody>{trs}</tbody></table></div>'
    )


def _market_watch_html() -> str:
    # Column order must match parse_market_watch exactly: symbol, sector, listed_in,
    # ldcp, open, high, low, current, change, change_pct, volume (11 <td>s).
    rows = "".join(
        f'<tr><td data-search="{s}"><a data-title="{s}"><strong>{s}</strong></a></td><td>SECT</td>'
        f'<td>ALLSHR</td><td>{ldcp}</td><td>{o}</td><td>{h}</td><td>{l}</td>'
        f'<td>{cur}</td><td>{chg}</td><td>{chgp}</td><td>{vol}</td></tr>'
        for s, cur, chg, chgp, vol, o, h, l, ldcp in MARKET_WATCH_ROWS
    )
    return (
        '<table class="tbl"><thead><tr><th>SYMBOL</th><th>SECTOR</th><th>LISTED IN</th><th>LDCP</th>'
        '<th>OPEN</th><th>HIGH</th><th>LOW</th><th>CURRENT</th><th>CHANGE</th><th>CHANGE (%)</th>'
        '<th>VOLUME</th></tr></thead>'
        f'<tbody>{rows}</tbody></table>'
    )


def _screener_html() -> str:
    rows = "".join(
        f'<tr><td>{s}</td><td>SECTOR</td><td>KSE100</td><td>10,000</td><td>100</td><td>1</td>'
        f'<td>10</td><td>8</td><td>1</td><td>5</td><td>{avg}</td></tr>'
        for s, avg in SCREENER_ROWS.items()
    )
    return (
        '<table class="tbl"><thead><tr><th>SYMBOL</th><th>SECTOR</th><th>LISTED IN</th><th>MARKET CAP</th>'
        '<th>PRICE</th><th>CHANGE %</th><th>1-YEAR CH %</th><th>PE (TTM)</th><th>DIVIDEND YIELD %</th>'
        '<th>FREE FLOAT</th><th>30D VOLUME AVG</th></tr></thead>'
        f'<tbody>{rows}</tbody></table>'
    )


EOD_CLOSES = {
    "MEBL": [(1, 340.0), (15, 352.0), (22, 350.0)],
    "LUCK": [(1, 890.0), (15, 905.0), (22, 900.0)],
}


def _eod_json(symbol: str) -> str:
    rows = EOD_CLOSES.get(symbol, EOD_CLOSES["MEBL"])
    data = [
        [int(datetime(2026, 9, day, tzinfo=timezone.utc).timestamp()), close, 100000 + day, close - 1]
        for day, close in rows
    ]
    return json.dumps({"status": 1, "data": data})


CALLS: list[tuple[str, dict]] = []


def portal(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    ajax_paths = ("/market-watch", "/symbols", "/announcements", "/timeseries/")
    is_ajax = any(path.startswith(p) for p in ajax_paths)
    if is_ajax:
        if request.headers.get("X-Requested-With") != "XMLHttpRequest":
            return httpx.Response(404, text="Not Found page")
        if request.headers.get("X-Req-Id") != TOKEN:
            return httpx.Response(403)
    if path == "/":
        return httpx.Response(200, text=f'<html><script>window.__ps={{"_k":"{TOKEN}"}}</script></html>')
    if path == "/market-watch":
        return httpx.Response(200, text=_market_watch_html())
    if path == "/symbols":
        return httpx.Response(200, json=SYMBOLS)
    if path == "/screener":
        return httpx.Response(200, text=_screener_html())
    if path.startswith("/timeseries/eod/"):
        sym = path.rsplit("/", 1)[-1]
        return httpx.Response(200, text=_eod_json(sym))
    if path == "/announcements":
        form = dict(parse_qsl(request.content.decode()))
        CALLS.append(("announcements", form))
        symbol = form.get("symbol", "")
        query = (form.get("query") or "").lower()
        rows = [
            r for r in ANNOUNCEMENTS
            if (not symbol or r["symbol"] == symbol) and (not query or query in r["title"].lower())
        ]
        return httpx.Response(200, text=_ann_html(rows))
    return httpx.Response(404, text="Not Found page")


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _sync_conn():
    return psycopg.connect(DATABASE_URL, autocommit=True)


@pytest.fixture(scope="module", autouse=True)
def _migrated_db():
    """Run migrations with a plain sync connection -- deliberately NOT using
    db.py's async singleton here, since that's shared global state the live
    server thread (a different thread/event loop, below) also uses; mixing
    the two from different threads/loops would corrupt psycopg's async state."""
    with _sync_conn() as conn, conn.cursor() as cur:
        cur.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())")
        cur.execute("SELECT version FROM schema_migrations")
        applied = {r[0] for r in cur.fetchall()}
        for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
            if path.name in applied:
                continue
            cur.execute(path.read_text())
            cur.execute("INSERT INTO schema_migrations (version) VALUES (%s)", (path.name,))
    yield


@pytest.fixture(autouse=True)
def _clean_tables():
    with _sync_conn() as conn, conn.cursor() as cur:
        cur.execute("TRUNCATE watchlist, alerts, alert_events, last_seen_announcement RESTART IDENTITY CASCADE")
        cur.execute("UPDATE cron_state SET last_run_at = NULL, last_alerts_evaluated = NULL, last_events_created = NULL, last_error = NULL WHERE id = 1")
    CALLS.clear()
    yield


@pytest.fixture(scope="module")
def base_url():
    os.environ["MCP_SECRET"] = SECRET
    os.environ["CRON_SECRET"] = CRON_SECRET
    os.environ["DATABASE_URL"] = DATABASE_URL
    os.environ.pop("TELEGRAM_BOT_TOKEN", None)
    os.environ.pop("TELEGRAM_CHAT_ID", None)
    S._client = PSXClient(transport=httpx.MockTransport(portal), min_interval=0)
    port = _free_port()
    # build_serverless_app(), not build_app(): FastMCP's streamable_http_app()
    # session manager is a one-shot resource on the shared `mcp` object, so a
    # second full uvicorn lifespan in the same process (test_server.py also
    # exercises build_app()) breaks it. PerRequestMCP creates a fresh manager
    # per request instead, and it's what Vercel (our actual target) uses anyway.
    app = S.build_serverless_app()
    cfg = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    srv = uvicorn.Server(cfg)
    t = threading.Thread(target=srv.run, daemon=True)
    t.start()
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    srv.should_exit = True
    t.join(5)


def U(base: str) -> str:
    return f"{base}/{SECRET}/mcp"


async def _call(url, tool, args=None):
    async with streamablehttp_client(url) as (r, w, _):
        async with ClientSession(r, w) as s:
            await s.initialize()
            res = await s.call_tool(tool, args or {})
            assert not res.isError, res.content
            return json.loads(res.content[0].text)


def run(coro):
    return asyncio.run(coro)


# =============================================================================== watchlist


def test_watchlist_crud(base_url):
    added = run(_call(U(base_url), "watchlist_add", {"symbol": "mebl", "target_buy": 300, "priority": "high", "tags": ["bank"]}))
    assert added["symbol"] == "MEBL" and added["target_buy"] == 300 and added["priority"] == "high" and added["tags"] == ["bank"]

    bad = run(_call(U(base_url), "watchlist_add", {"symbol": "NOPE"}))
    assert "error" in bad

    got = run(_call(U(base_url), "watchlist_get", {}))
    assert got["count"] == 1 and got["watchlist"][0]["symbol"] == "MEBL"

    updated = run(_call(U(base_url), "watchlist_update", {"symbol": "MEBL", "status": "paused", "notes": "waiting for dip"}))
    assert updated["status"] == "paused" and updated["notes"] == "waiting for dip" and updated["target_buy"] == 300

    missing = run(_call(U(base_url), "watchlist_update", {"symbol": "LUCK", "notes": "x"}))
    assert "error" in missing

    filtered = run(_call(U(base_url), "watchlist_get", {"status": "paused"}))
    assert filtered["count"] == 1
    filtered_none = run(_call(U(base_url), "watchlist_get", {"status": "watching"}))
    assert filtered_none["count"] == 0

    removed = run(_call(U(base_url), "watchlist_remove", {"symbol": "mebl"}))
    assert removed["removed"] is True
    removed_again = run(_call(U(base_url), "watchlist_remove", {"symbol": "mebl"}))
    assert removed_again["removed"] is False


# =============================================================================== insider / corporate


def test_insider_and_corporate_activity_explicit_symbols(base_url):
    d = run(_call(U(base_url), "insider_activity", {"symbols": ["MEBL"]}))
    titles = [i["title"] for i in d["items"]]
    assert any("Transaction in Shares" in t for t in titles)
    assert not any("Quarterly Financial Results" in t for t in titles)  # not an insider keyword

    c = run(_call(U(base_url), "corporate_actions", {"symbols": ["MEBL", "LUCK"]}))
    titles = [i["title"] for i in c["items"]]
    assert any("Right Shares" in t for t in titles) and any("Board Meeting" in t for t in titles)


def test_insider_activity_defaults_to_watchlist(base_url):
    empty = run(_call(U(base_url), "insider_activity", {}))
    assert empty["symbols"] is None  # no watchlist entries yet -> market-wide

    run(_call(U(base_url), "watchlist_add", {"symbol": "MEBL"}))
    d = run(_call(U(base_url), "insider_activity", {}))
    assert d["symbols"] == ["MEBL"]
    assert any("Transaction in Shares" in i["title"] for i in d["items"])


# =============================================================================== alerts


def test_alert_set_list_and_validation(base_url):
    bad_type = run(_call(U(base_url), "alert_set", {"symbol": "MEBL", "type": "price_above"}))
    assert "error" in bad_type  # threshold required

    a = run(_call(U(base_url), "alert_set", {"symbol": "mebl", "type": "price_above", "threshold": 340}))
    assert a["symbol"] == "MEBL" and a["type"] == "price_above" and a["threshold"] == 340 and a["status"] == "active"

    updated = run(_call(U(base_url), "alert_set", {"symbol": "mebl", "type": "price_above", "threshold": 360}))
    assert updated["threshold"] == 360  # upsert, not a duplicate

    run(_call(U(base_url), "alert_set", {"symbol": "LUCK", "type": "new_announcement"}))
    listed = run(_call(U(base_url), "alert_list", {}))
    assert listed["count"] == 2
    only_mebl = run(_call(U(base_url), "alert_list", {"symbol": "mebl"}))
    assert only_mebl["count"] == 1


def test_cron_endpoint_auth_and_evaluation(base_url):
    r = httpx.post(f"{base_url}/api/cron/check-alerts")
    assert r.status_code == 401
    r = httpx.post(f"{base_url}/api/cron/check-alerts", headers={"X-Cron-Secret": "wrong"})
    assert r.status_code == 401
    r = httpx.get(f"{base_url}/api/cron/check-alerts", headers={"X-Cron-Secret": CRON_SECRET})
    assert r.status_code == 405

    # price_above threshold (345) is below MEBL's mocked current price (350): should fire once.
    run(_call(U(base_url), "alert_set", {"symbol": "MEBL", "type": "price_above", "threshold": 345}))
    r = httpx.post(f"{base_url}/api/cron/check-alerts", headers={"X-Cron-Secret": CRON_SECRET})
    assert r.status_code == 200
    summary = r.json()
    assert summary["evaluated"] == 1 and summary["events_created"] == 1

    events = run(_call(U(base_url), "get_alerts", {"since": "2020-01-01T00:00:00+00:00"}))
    assert events["count"] == 1 and events["events"][0]["symbol"] == "MEBL"

    alerts = run(_call(U(base_url), "alert_list", {}))
    assert alerts["alerts"][0]["status"] == "triggered"  # one-shot: auto-disabled after firing

    # Firing again should be a no-op (already triggered, not active).
    r2 = httpx.post(f"{base_url}/api/cron/check-alerts", headers={"X-Cron-Secret": CRON_SECRET})
    assert r2.json()["evaluated"] == 0


def test_new_announcement_alert_baselines_then_fires(base_url):
    run(_call(U(base_url), "alert_set", {"symbol": "LUCK", "type": "new_announcement"}))
    r1 = httpx.post(f"{base_url}/api/cron/check-alerts", headers={"X-Cron-Secret": CRON_SECRET})
    assert r1.json()["events_created"] == 0  # first run only baselines last_seen

    r2 = httpx.post(f"{base_url}/api/cron/check-alerts", headers={"X-Cron-Secret": CRON_SECRET})
    assert r2.json()["events_created"] == 0  # nothing new since baseline


# =============================================================================== why_moved


def test_why_moved(base_url):
    d = run(_call(U(base_url), "why_moved", {"symbol": "MEBL", "date": "2026-09-22"}))
    assert d["symbol"] == "MEBL" and d["price"]["close"] == 350.0 and d["price"]["prev_close"] == 352.0
    assert any("Transaction in Shares" in a["title"] for a in d["announcements"])

    missing = run(_call(U(base_url), "why_moved", {"symbol": "MEBL", "date": "2099-01-01"}))
    assert "error" in missing


# =============================================================================== psx_selftest


def test_selftest_reports_db_and_cron(base_url):
    d = run(_call(U(base_url), "psx_selftest", {}))
    assert d["checks"]["db_connectivity"]["ok"] is True
    assert d["checks"]["table:watchlist"]["ok"] is True
    assert d["checks"]["cron"]["ok"] is True
    assert d["checks"]["cron"]["sample"]["status"] == "cron has never run"

    httpx.post(f"{base_url}/api/cron/check-alerts", headers={"X-Cron-Secret": CRON_SECRET})
    d2 = run(_call(U(base_url), "psx_selftest", {}))
    assert "last_run_minutes_ago" in d2["checks"]["cron"]["sample"]
