"""Our own persisted data: watchlist, alerts, and the alert-evaluation job.

Thin DB-backed logic, called by the thin @mcp.tool wrappers in server.py --
the same split as client.py (PSX data) vs server.py (tool wrappers). Talking
to PSX itself is delegated to the PSXClient instance the caller passes in
(server.py's shared `client()` singleton), so this module never imports
server.py and stays free of circular imports.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta
from typing import Any

from psycopg.types.json import Jsonb

from . import db, telegram
from .client import PSXClient, PSXError
from .parsers import PKT

ALERT_TYPES = ("price_above", "price_below", "volume_spike", "new_announcement")
INSIDER_KEYWORDS = ("transaction in shares", "director", "sponsor", "buy back", "buyback")
CORPORATE_KEYWORDS = ("right shares", "material information", "merger", "amalgamation", "board meeting")


# =============================================================================== symbols


async def validate_symbol(c: PSXClient, symbol: str) -> dict:
    """Raise ValueError with suggestions if `symbol` isn't a real PSX ticker."""
    sym = symbol.upper().strip()
    syms = await c.symbols()
    for s in syms:
        if s["symbol"] == sym:
            return s
    hits = [s for s in syms if sym in s["symbol"] or sym in (s["name"] or "").upper()][:5]
    hint = f"Did you mean: {', '.join(h['symbol'] for h in hits)}?" if hits else "Use psx_search to find the right ticker."
    raise ValueError(f"Unknown PSX symbol {sym!r}. {hint}")


# =============================================================================== watchlist


async def watchlist_add(
    c: PSXClient, symbol: str, target_buy: float | None, target_sell: float | None,
    reason: str | None, priority: str, notes: str | None, tags: list[str] | None,
) -> dict:
    await validate_symbol(c, symbol)
    sym = symbol.upper().strip()
    return await db.fetchrow(
        """
        INSERT INTO watchlist (symbol, target_buy, target_sell, reason, priority, notes, tags)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (symbol) DO UPDATE SET
            target_buy = EXCLUDED.target_buy, target_sell = EXCLUDED.target_sell,
            reason = EXCLUDED.reason, priority = EXCLUDED.priority,
            notes = EXCLUDED.notes, tags = EXCLUDED.tags, updated_at = now()
        RETURNING *
        """,
        (sym, target_buy, target_sell, reason, priority, notes, tags or []),
    )


async def watchlist_update(symbol: str, **fields: Any) -> dict:
    """Only columns present in `fields` with a non-None value are changed."""
    sym = symbol.upper().strip()
    existing = await db.fetchrow("SELECT symbol FROM watchlist WHERE symbol = %s", (sym,))
    if not existing:
        raise ValueError(f"{sym} is not on the watchlist. Use watchlist_add first.")
    sets, params = [], []
    for col in ("target_buy", "target_sell", "reason", "priority", "notes", "tags", "status"):
        val = fields.get(col)
        if val is not None:
            sets.append(f"{col} = %s")
            params.append(val)
    if not sets:
        return await db.fetchrow("SELECT * FROM watchlist WHERE symbol = %s", (sym,))
    sets.append("updated_at = now()")
    params.append(sym)
    return await db.fetchrow(f"UPDATE watchlist SET {', '.join(sets)} WHERE symbol = %s RETURNING *", tuple(params))


async def watchlist_remove(symbol: str) -> bool:
    n = await db.execute("DELETE FROM watchlist WHERE symbol = %s", (symbol.upper().strip(),))
    return n > 0


async def watchlist_get(priority: str | None, status: str | None, tag: str | None) -> list[dict]:
    clauses, params = [], []
    if priority:
        clauses.append("priority = %s")
        params.append(priority)
    if status:
        clauses.append("status = %s")
        params.append(status)
    if tag:
        clauses.append("%s = ANY(tags)")
        params.append(tag)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    order = "ORDER BY CASE priority WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END, symbol"
    return await db.fetch(f"SELECT * FROM watchlist {where} {order}", tuple(params))


async def watchlist_symbols() -> list[str]:
    return [r["symbol"] for r in await db.fetch("SELECT symbol FROM watchlist")]


# =============================================================================== alerts


async def alert_set(c: PSXClient, symbol: str, type: str, threshold: float | None) -> dict:
    if type not in ALERT_TYPES:
        raise ValueError(f"type must be one of {ALERT_TYPES}")
    if type != "new_announcement" and threshold is None:
        raise ValueError(f"threshold is required for alert type {type!r}")
    await validate_symbol(c, symbol)
    sym = symbol.upper().strip()
    return await db.fetchrow(
        """
        INSERT INTO alerts (symbol, type, threshold, status)
        VALUES (%s, %s, %s, 'active')
        ON CONFLICT (symbol, type) DO UPDATE SET
            threshold = EXCLUDED.threshold, status = 'active', updated_at = now()
        RETURNING *
        """,
        (sym, type, threshold),
    )


async def alert_list(symbol: str | None, status: str | None) -> list[dict]:
    clauses, params = [], []
    if symbol:
        clauses.append("symbol = %s")
        params.append(symbol.upper().strip())
    if status:
        clauses.append("status = %s")
        params.append(status)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    return await db.fetch(f"SELECT * FROM alerts {where} ORDER BY symbol, type", tuple(params))


async def alert_events_since(since: datetime, limit: int = 200) -> list[dict]:
    """`since` must be an actual datetime, not an ISO string: psycopg adapts a
    Python str to a text parameter, and Postgres has no implicit text ->
    timestamptz cast for a bound parameter in a comparison."""
    return await db.fetch(
        "SELECT * FROM alert_events WHERE triggered_at >= %s ORDER BY triggered_at DESC LIMIT %s",
        (since, limit),
    )


# =============================================================================== last-seen announcements


def _announcement_surrogate_id(item: dict) -> str:
    """PSX gives announcements no ID; the PDF link is the closest stable one."""
    if item.get("pdf"):
        return item["pdf"]
    raw = "|".join(str(item.get(k) or "") for k in ("symbol", "date", "time", "title"))
    return hashlib.sha1(raw.encode()).hexdigest()


async def last_seen_get(symbol: str) -> str | None:
    row = await db.fetchrow("SELECT last_id FROM last_seen_announcement WHERE symbol = %s", (symbol,))
    return row["last_id"] if row else None


async def last_seen_set(symbol: str, last_id: str) -> None:
    await db.execute(
        """
        INSERT INTO last_seen_announcement (symbol, last_id, last_checked_at)
        VALUES (%s, %s, now())
        ON CONFLICT (symbol) DO UPDATE SET last_id = EXCLUDED.last_id, last_checked_at = now()
        """,
        (symbol, last_id),
    )


# =============================================================================== insider / corporate keyword search


async def resolve_symbols(explicit: list[str] | None) -> list[str]:
    """Explicit symbols win; otherwise fall back to the watchlist.

    Phase 1 has no persisted portfolio (psx_portfolio takes holdings as a
    call-time argument and stores nothing), so "portfolio + watchlist" from
    the spec reduces to "watchlist" until a portfolio table exists."""
    if explicit:
        return [s.upper().strip() for s in explicit]
    return await watchlist_symbols()


def _ann_sort_key(item: dict) -> datetime:
    """parse_announcements gives dates/times as PSX's raw display text (e.g.
    'Sep 22, 2026' / '1:59 PM'), not ISO -- sorting those strings lexically
    would not sort chronologically, so parse them properly here."""
    try:
        dt = datetime.strptime(item.get("date") or "", "%b %d, %Y")
    except ValueError:
        return datetime.min
    try:
        tm = datetime.strptime(item.get("time") or "", "%I:%M %p").time()
        dt = dt.replace(hour=tm.hour, minute=tm.minute)
    except ValueError:
        pass
    return dt


async def _keyword_announcements(c: PSXClient, symbol: str | None, keywords: tuple[str, ...], days: int) -> list[dict]:
    date_from = (datetime.now(PKT).date() - timedelta(days=days)).isoformat()
    seen: dict[str, dict] = {}
    for kw in keywords:
        try:
            res = await c.announcements(symbol=symbol or "", keyword=kw, count=30, date_from=date_from)
        except PSXError:
            continue
        for item in res.get("items", []):
            seen[_announcement_surrogate_id(item)] = item
    return sorted(seen.values(), key=_ann_sort_key, reverse=True)


async def keyword_activity(c: PSXClient, symbols: list[str] | None, days: int, keywords: tuple[str, ...]) -> dict:
    syms = await resolve_symbols(symbols)
    if syms:
        items: list[dict] = []
        for s in syms:
            items.extend(await _keyword_announcements(c, s, keywords, days))
        items.sort(key=_ann_sort_key, reverse=True)
    else:
        items = await _keyword_announcements(c, None, keywords, days)
    return {"days": days, "symbols": syms or None, "count": len(items), "items": items}


# =============================================================================== alert evaluation (the cron job)


async def _fire(alert: dict, message: str, payload: dict) -> None:
    await db.execute(
        "INSERT INTO alert_events (alert_id, symbol, type, message, payload) VALUES (%s, %s, %s, %s, %s)",
        (alert["id"], alert["symbol"], alert["type"], message, Jsonb(payload)),
    )
    await telegram.send_message(f"[PSX Alert] {message}")


async def evaluate_alerts(c: PSXClient) -> dict:
    """Called every ~15 min (during market hours) by /api/cron/check-alerts."""
    alerts = await db.fetch("SELECT * FROM alerts WHERE status = 'active'")
    events_created = 0
    errors: list[str] = []
    mw_cache: list[dict] | None = None

    async def mw_row(symbol: str) -> dict | None:
        nonlocal mw_cache
        if mw_cache is None:
            mw_cache = await c.market_watch()
        return next((r for r in mw_cache if r["symbol"] == symbol), None)

    for alert in alerts:
        sym, typ = alert["symbol"], alert["type"]
        try:
            if typ in ("price_above", "price_below"):
                row = await mw_row(sym)
                price = row["current"] if row else None
                threshold = float(alert["threshold"]) if alert["threshold"] is not None else None
                if price is None or threshold is None:
                    continue
                hit = (typ == "price_above" and price >= threshold) or (typ == "price_below" and price <= threshold)
                if hit:
                    verb = "crossed above" if typ == "price_above" else "dropped below"
                    await _fire(alert, f"{sym} {verb} {threshold}: now {price}", {"price": price, "threshold": threshold})
                    await db.execute("UPDATE alerts SET status = 'triggered', last_fired_at = now() WHERE id = %s", (alert["id"],))
                    events_created += 1

            elif typ == "volume_spike":
                rows = await c.screener()
                r = next((x for x in rows if x["symbol"] == sym), None)
                avg_vol = r["avg_volume_30d"] if r else None
                mw = await mw_row(sym)
                volume = mw["volume"] if mw else None
                multiplier = float(alert["threshold"]) if alert["threshold"] is not None else None
                if not avg_vol or volume is None or multiplier is None:
                    continue
                already_today = alert["last_fired_at"] and alert["last_fired_at"].astimezone(PKT).date() == datetime.now(PKT).date()
                if volume > multiplier * avg_vol and not already_today:
                    ratio = volume / avg_vol
                    await _fire(alert, f"{sym} volume {volume:,} is {ratio:.1f}x its 30-day average", {"volume": volume, "avg_volume_30d": avg_vol})
                    await db.execute("UPDATE alerts SET last_fired_at = now() WHERE id = %s", (alert["id"],))
                    events_created += 1

            elif typ == "new_announcement":
                res = await c.announcements(symbol=sym, count=5)
                items = res.get("items", [])
                if items:
                    newest = _announcement_surrogate_id(items[0])
                    prev = await last_seen_get(sym)
                    if prev is None:
                        await last_seen_set(sym, newest)  # first run: baseline only, don't replay history
                    elif newest != prev:
                        new_items = []
                        for it in items:
                            if _announcement_surrogate_id(it) == prev:
                                break
                            new_items.append(it)
                        headline = new_items[0]["title"] if new_items else items[0]["title"]
                        await _fire(alert, f"{sym}: {len(new_items) or 1} new announcement(s), latest: {headline}", {"items": new_items or items[:1]})
                        await last_seen_set(sym, newest)
                        events_created += 1
        except Exception as e:  # noqa: BLE001
            errors.append(f"{sym}/{typ}: {e}")
        finally:
            await db.execute("UPDATE alerts SET last_checked_at = now() WHERE id = %s", (alert["id"],))

    await db.execute(
        "UPDATE cron_state SET last_run_at = now(), last_alerts_evaluated = %s, last_events_created = %s, last_error = %s WHERE id = 1",
        (len(alerts), events_created, "; ".join(errors[:5]) or None),
    )
    return {"evaluated": len(alerts), "events_created": events_created, "errors": errors}
