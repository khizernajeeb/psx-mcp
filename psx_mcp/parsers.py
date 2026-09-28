"""Pure parsing functions: PSX Data Portal HTML/JSON -> plain Python dicts.

Every function here takes raw text and returns data, with no network access,
so they can be unit-tested against saved fixtures. The Data Portal layout was
mapped in Sep 2026; if PSX changes its markup, fixes belong in this file.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from bs4 import BeautifulSoup, Tag

PKT = timezone(timedelta(hours=5), name="PKT")
BASE_URL = "https://dps.psx.com.pk"

_NUM_RE = re.compile(r"-?\d[\d,]*(?:\.\d+)?")


# --------------------------------------------------------------------------- helpers


def num(text: Any) -> float | None:
    """'1,234.50' -> 1234.5, '(-0.03%)' -> -0.03, 'Rs.547.94' -> 547.94, '' -> None."""
    if text is None:
        return None
    if isinstance(text, (int, float)):
        return float(text)
    s = str(text).replace("−", "-").strip()
    if not s or s in {"-", "--", "N/A", "n/a"}:
        return None
    m = _NUM_RE.search(s.replace(" ", ""))
    if not m:
        return None
    try:
        return float(m.group(0).replace(",", ""))
    except ValueError:
        return None


def acct_num(text: str) -> float | None:
    """Accounting style used in financial tables: '(1.25)' means -1.25."""
    t = (text or "").strip()
    v = num(t)
    if v is not None and t.startswith("(") and t.endswith(")") and v > 0:
        return -v
    return v


def num_range(text: str | None) -> tuple[float | None, float | None]:
    """'493.31 — 602.93' -> (493.31, 602.93)."""
    if not text:
        return None, None
    parts = [p for p in re.split(r"\s*[—–]\s*|\s+-\s+", text) if p.strip()]
    if len(parts) >= 2:
        return num(parts[0]), num(parts[1])
    return num(text), None


def _txt(el: Tag | None) -> str:
    if el is None:
        return ""
    return re.sub(r"\s+", " ", el.get_text(" ", strip=True)).strip()


def _cell_value(td: Tag) -> float | None:
    """Prefer the machine-readable data-order attribute, fall back to text."""
    if td.has_attr("data-order"):
        v = num(td["data-order"])
        if v is not None:
            return v
    return num(_txt(td))


def _soup(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "lxml")


def _abs(href: str | None) -> str | None:
    if not href or href.startswith("javascript"):
        return None
    return href if href.startswith("http") else BASE_URL + href


def ts_to_pkt(ts: float) -> datetime:
    return datetime.fromtimestamp(ts, tz=PKT)


def _snake(label: str) -> str:
    s = re.sub(r"[*^]+", "", label).strip().lower()
    s = s.replace("%", "pct").replace("(000's)", "000s")
    s = re.sub(r"[^a-z0-9]+", "_", s).strip("_")
    return s


def _table_rows(table: Tag) -> list[Tag]:
    body = table.find("tbody")
    rows = (body or table).find_all("tr", recursive=False)
    return [r for r in rows if r.find("td")]


def _headers(table: Tag) -> list[str]:
    thead = table.find("thead")
    if thead is None:
        return []
    return [_txt(th) for th in thead.find_all("th")]


# --------------------------------------------------------------------------- token


def extract_token(page_html: str) -> str | None:
    """The portal embeds `window.__ps = {..., "_k": "<token>"}`; AJAX calls must
    send it back as the X-Req-Id header."""
    m = re.search(r"window\.__ps\s*=\s*(\{.*?\})\s*;?\s*</script>", page_html, re.S)
    if not m:
        m = re.search(r"window\.__ps\s*=\s*(\{[^<]*?\})", page_html, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(1)).get("_k")
    except (json.JSONDecodeError, AttributeError):
        k = re.search(r'"_k"\s*:\s*"([^"]+)"', m.group(1))
        return k.group(1) if k else None


# --------------------------------------------------------------------------- market watch


def parse_market_watch(html: str) -> list[dict]:
    """/market-watch -> one dict per listed, traded symbol (regular board)."""
    soup = _soup(html)
    table = soup.find("table")
    if table is None:
        return []
    out = []
    for tr in _table_rows(table):
        tds = tr.find_all("td", recursive=False)
        if len(tds) < 11:
            continue
        sym_td = tds[0]
        a = sym_td.find("a")
        symbol = (sym_td.get("data-search") or _txt(sym_td)).strip().upper()
        listed_in = [x for x in _txt(tds[2]).replace(" ", "").split(",") if x]
        out.append(
            {
                "symbol": symbol,
                "name": a.get("data-title") if a else None,
                "sector_code": _txt(tds[1]),
                "listed_in": listed_in,
                "shariah": any(x.startswith("KMI") for x in listed_in),
                "ldcp": _cell_value(tds[3]),
                "open": _cell_value(tds[4]),
                "high": _cell_value(tds[5]),
                "low": _cell_value(tds[6]),
                "current": _cell_value(tds[7]),
                "change": _cell_value(tds[8]),
                "change_pct": _cell_value(tds[9]),
                "volume": _cell_value(tds[10]),
            }
        )
    return out


# --------------------------------------------------------------------------- symbols / sectors


def parse_symbols(text: str) -> list[dict]:
    data = json.loads(text)
    return [
        {
            "symbol": d.get("symbol"),
            "name": d.get("name"),
            "sector": d.get("sectorName"),
            "is_etf": bool(d.get("isETF")),
            "is_debt": bool(d.get("isDebt")),
        }
        for d in data
    ]


def parse_sectorwise(html: str) -> list[dict]:
    """/sector-summary/sectorwise -> sector code, name, breadth, turnover, mcap."""
    soup = _soup(html)
    table = soup.find("table")
    if table is None:
        return []
    out = []
    for tr in _table_rows(table):
        tds = tr.find_all("td", recursive=False)
        if len(tds) < 7:
            continue
        out.append(
            {
                "sector_code": _txt(tds[0]),
                "sector": re.sub(r"\s+", " ", _txt(tds[1])),
                "advancers": int(num(_txt(tds[2])) or 0),
                "decliners": int(num(_txt(tds[3])) or 0),
                "unchanged": int(num(_txt(tds[4])) or 0),
                "turnover": _cell_value(tds[5]),
                "market_cap_bn": _cell_value(tds[6]),
            }
        )
    return out


# --------------------------------------------------------------------------- indices


def parse_indices(html: str) -> list[dict]:
    """/indices -> index level table (KSE100, KMI30, ALLSHR ...)."""
    soup = _soup(html)
    out = []
    for table in soup.find_all("table"):
        hdr = [h.lower() for h in _headers(table)]
        if not hdr or hdr[0] != "index":
            continue
        for tr in _table_rows(table):
            tds = tr.find_all("td", recursive=False)
            if len(tds) < 6:
                continue
            out.append(
                {
                    "index": _txt(tds[0]),
                    "high": _cell_value(tds[1]),
                    "low": _cell_value(tds[2]),
                    "current": _cell_value(tds[3]),
                    "change": _cell_value(tds[4]),
                    "change_pct": _cell_value(tds[5]),
                }
            )
    return out


def parse_index_constituents(html: str) -> list[dict]:
    """/indices/<CODE> -> constituents with weights, points, free float, mcap."""
    soup = _soup(html)
    table = soup.find("table")
    if table is None:
        return []
    hdr = [_snake(h) for h in _headers(table)]
    if hdr and "symbol" not in hdr[0]:
        # Layout changed; surface that instead of returning misaligned data.
        raise ValueError(f"Unexpected constituents header: {hdr}")
    out = []
    for tr in _table_rows(table):
        tds = tr.find_all("td", recursive=False)
        if len(tds) < 11:
            continue
        out.append(
            {
                "symbol": _txt(tds[0]),
                "name": _txt(tds[1]),
                "ldcp": _cell_value(tds[2]),
                "current": _cell_value(tds[3]),
                "change": _cell_value(tds[4]),
                "change_pct": _cell_value(tds[5]),
                "index_weight_pct": _cell_value(tds[6]),
                "index_points": _cell_value(tds[7]),
                "volume": _cell_value(tds[8]),
                "free_float_mn": _cell_value(tds[9]),
                "market_cap_mn": _cell_value(tds[10]),
            }
        )
    return out


# --------------------------------------------------------------------------- company page


def _label_value_pairs(scope: Tag) -> list[tuple[str, Tag]]:
    pairs = []
    for lab in scope.select(".stats_label"):
        val = lab.find_next_sibling(class_="stats_value")
        if val is None and lab.parent is not None:
            val = lab.parent.select_one(".stats_value")
        if val is not None:
            pairs.append((_txt(lab), val))
    return pairs


def _generic_table(table: Tag) -> dict:
    """Row-label tables (financials/ratios): first column = metric name."""
    periods = [h for h in _headers(table)][1:]
    rows = {}
    for tr in _table_rows(table):
        cells = tr.find_all(["td", "th"], recursive=False)
        if not cells:
            continue
        label = _txt(cells[0])
        rows[label] = {p: acct_num(_txt(c)) for p, c in zip(periods, cells[1:])}
    return {"periods": periods, "rows": rows}


def _doc_table(table: Tag) -> list[dict]:
    """Date | Title | Document tables inside company announcements."""
    out = []
    for tr in _table_rows(table):
        tds = tr.find_all("td", recursive=False)
        if len(tds) < 2:
            continue
        links = [_abs(a.get("href")) for a in tr.find_all("a")]
        links = [l for l in links if l]
        out.append({"date": _txt(tds[0]), "title": _txt(tds[1]), "pdf": links[0] if links else None})
    return out


def parse_company(html: str, symbol: str) -> dict:
    soup = _soup(html)
    quote = soup.select_one("#quote") or soup
    result: dict[str, Any] = {
        "symbol": symbol.upper(),
        "name": _txt(quote.select_one(".quote__name")) or None,
        "sector": _txt(quote.select_one(".quote__sector")) or None,
        "price": num(_txt(quote.select_one(".quote__close"))),
        "change": num(_txt(quote.select_one(".change__value"))),
        "change_pct": num(_txt(quote.select_one(".change__percent"))),
        "as_of": re.sub(r"^[\^\s]*As of\s*", "", _txt(quote.select_one(".quote__date"))) or None,
        "url": f"{BASE_URL}/company/{symbol.upper()}",
    }

    # Regular-market stats panel (first panel named REG, else first panel).
    panel = quote.select_one('.tabs__panel[data-name="REG"]') or quote.select_one(".tabs__panel")
    stats: dict[str, Any] = {}
    if panel is not None:
        for label, val_el in _label_value_pairs(panel):
            if not label:
                continue
            key = _snake(label)
            text = _txt(val_el)
            if "range" in key or "circuit" in key:
                lo, hi = num_range(text)
                stats[key] = {"low": lo, "high": hi}
            else:
                stats[key] = num(text)
    # Rename the noisy labels to stable keys.
    rename = {
        "p_e_ratio_ttm": "pe_ttm",
        "1_year_change": "change_1y_pct",
        "ytd_change": "change_ytd_pct",
        "52_week_range": "range_52w",
        "day_range": "day_range",
        "circuit_breaker": "circuit_breaker",
    }
    result["stats"] = {rename.get(k, k): v for k, v in stats.items()}

    # Profile: description, people, address, website, registrar, auditor, FY end.
    profile: dict[str, Any] = {}
    prof = soup.select_one("#profile")
    if prof is not None:
        for label, val_el in _label_value_pairs(prof):
            key = _snake(label)
            if not key:
                continue
            if val_el.find("table") is not None or key == "key_people":
                people = []
                for tr in val_el.find_all("tr"):
                    tds = tr.find_all("td")
                    if len(tds) >= 2:
                        people.append({"name": _txt(tds[0]), "role": _txt(tds[1])})
                profile[key] = people
            else:
                profile[key] = _txt(val_el)
        if "key_people" not in profile:
            t = prof.find("table")
            if t is not None:
                profile["key_people"] = [
                    {"name": _txt(tds[0]), "role": _txt(tds[1])}
                    for tds in (tr.find_all("td") for tr in t.find_all("tr"))
                    if len(tds) >= 2
                ]
    result["profile"] = profile

    # Equity: market cap (PKR '000), shares, free float (shares and %).
    equity: dict[str, Any] = {}
    eq = soup.select_one("#equity")
    if eq is not None:
        for label, val_el in _label_value_pairs(eq):
            key = _snake(label)
            text = _txt(val_el)
            if key == "free_float" and "%" in text:
                equity["free_float_pct"] = num(text)
            elif key == "free_float":
                equity["free_float_shares"] = num(text)
            elif key.startswith("market_cap"):
                equity["market_cap_pkr_000s"] = num(text)
            else:
                equity[key] = num(text)
    result["equity"] = equity

    # Financials: Annual + Quarterly panels.
    fin: dict[str, Any] = {}
    fsec = soup.select_one("#financials")
    if fsec is not None:
        panels = fsec.select(".tabs__panel")
        if panels:
            for p in panels:
                t = p.find("table")
                if t is not None:
                    fin[(p.get("data-name") or "table").lower()] = _generic_table(t)
        else:
            for i, t in enumerate(fsec.find_all("table")):
                fin[f"table_{i}"] = _generic_table(t)
    result["financials"] = fin

    rsec = soup.select_one("#ratios")
    result["ratios"] = _generic_table(rsec.find("table")) if rsec is not None and rsec.find("table") else {}

    ann: dict[str, list] = {}
    asec = soup.select_one("#announcements")
    if asec is not None:
        for p in asec.select(".tabs__panel"):
            t = p.find("table")
            if t is not None:
                ann[_snake(p.get("data-name") or "other")] = _doc_table(t)
    result["recent_announcements"] = ann

    if not result["name"] and result["price"] is None:
        raise ValueError(f"Company page for {symbol} did not contain a quote (unknown symbol or layout change)")
    return result


# --------------------------------------------------------------------------- payouts


def parse_payouts(html: str) -> list[dict]:
    """POST /company/payouts -> dividend/bonus history."""
    soup = _soup(html)
    table = soup.find("table")
    if table is None:
        return []
    out = []
    for tr in _table_rows(table):
        tds = tr.find_all("td", recursive=False)
        if len(tds) < 4:
            continue
        details = _txt(tds[2])
        out.append(
            {
                "announced": _txt(tds[0]),
                "period": _txt(tds[1]),
                "details": details,
                "book_closure": _txt(tds[3]),
                **_decode_payout(details, _txt(tds[1])),
            }
        )
    return out


_PERIOD = {"YR": "annual", "HYR": "half-year", "IQ": "Q1", "IIQ": "Q2", "IIIQ": "Q3", "Q": "quarter"}
_KIND = {"D": "cash dividend", "B": "bonus shares", "R": "right shares"}


def _decode_payout(details: str, period: str) -> dict:
    """'75%(i) (D)' -> 75% of face value, interim #1, cash dividend.
    PSX payout % is on face value (usually PKR 10), so 75% = PKR 7.50/share."""
    pct = num(details.split("(")[0]) if details else None
    kind_m = re.search(r"\(([DBR])\)", details or "")
    marker_m = re.search(r"%?\s*\(([ivxF]+)\)", details or "")
    per_m = re.search(r"\(([A-Z]+)\)", period or "")
    kind = _KIND.get(kind_m.group(1)) if kind_m else None
    return {
        "payout_pct_of_face": pct,
        "type": kind,
        "sequence": ("final" if marker_m and marker_m.group(1) == "F" else f"interim {marker_m.group(1)}")
        if marker_m
        else None,
        "period_type": _PERIOD.get(per_m.group(1), per_m.group(1)) if per_m else None,
        "cash_per_share_pkr_if_face_10": round(pct / 10, 4) if pct is not None and kind == "cash dividend" else None,
    }


# --------------------------------------------------------------------------- announcements


def parse_announcements(html: str) -> dict:
    """POST /announcements -> list + total count."""
    soup = _soup(html)
    table = soup.find("table")
    items = []
    if table is not None:
        hdr = [h.lower() for h in _headers(table)]
        for tr in _table_rows(table):
            tds = tr.find_all("td", recursive=False)
            if not tds:
                continue
            row = {h or f"col{i}": _txt(td) for i, (h, td) in enumerate(zip(hdr, tds))}
            pdfs = [_abs(a.get("href")) for a in tr.find_all("a")]
            pdfs = [p for p in pdfs if p and "/download/" in p]
            items.append(
                {
                    "date": row.get("date"),
                    "time": row.get("time"),
                    "symbol": row.get("symbol"),
                    "company": row.get("name"),
                    "title": row.get("title"),
                    "pdf": pdfs[0] if pdfs else None,
                }
            )
    total = None
    m = re.search(r"of\s+([\d,]+)\s+entries", soup.get_text(" "))
    if m:
        total = int(m.group(1).replace(",", ""))
    return {"total": total, "items": items}


# --------------------------------------------------------------------------- time series


def parse_timeseries(text: str, kind: str) -> list[dict]:
    """/timeseries/eod/<SYM>: [ts, close, volume, open] newest-first.
    /timeseries/int/<SYM>: [ts, price, volume] newest-first.
    Returns oldest-first."""
    payload = json.loads(text)
    if isinstance(payload, dict):
        if payload.get("status") not in (1, "1", True, None):
            raise ValueError(f"PSX timeseries error: {payload.get('message')}")
        rows = payload.get("data") or []
    else:
        rows = payload
    out = []
    for r in rows:
        if not r or len(r) < 2:
            continue
        dt = ts_to_pkt(r[0])
        vol = int(r[2]) if len(r) > 2 and r[2] is not None else None
        if kind == "eod":
            item = {
                "date": dt.date().isoformat(),
                "open": float(r[3]) if len(r) > 3 and r[3] is not None else None,
                "close": float(r[1]),
                "volume": vol,
            }
        else:
            item = {"time": dt.strftime("%Y-%m-%d %H:%M"), "price": float(r[1]), "volume": vol}
        out.append(item)
    out.sort(key=lambda x: x.get("date") or x.get("time"))
    return out
