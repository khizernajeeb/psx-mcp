from conftest import fx
from psx_mcp import parsers as P


def test_token():
    assert P.extract_token(fx("home.html")) == "TESTTOKEN_abcdefghijklmnopqrstuvwxyz0123"
    assert P.extract_token("<html></html>") is None


def test_num_helpers():
    assert P.num("1,234.50") == 1234.5
    assert P.num("(-0.03%)") == -0.03
    assert P.num("Rs.547.94") == 547.94
    assert P.num("-") is None
    assert P.acct_num("(1.25)") == -1.25
    assert P.acct_num("(-1.25)") == -1.25
    assert P.num_range("493.31 — 602.93") == (493.31, 602.93)


def test_market_watch():
    rows = P.parse_market_watch(fx("market_watch.html"))
    assert len(rows) == 6
    t = rows[0]
    assert t["symbol"] == "TISL" and t["name"].startswith("Tasdeeq")
    assert t["current"] == 5.06 and t["change_pct"] == 10 and t["volume"] == 53237236
    assert t["shariah"] is True and t["sector_code"] == "0818"
    m = rows[1]
    assert m["change"] == -0.18 and "KSE100" in m["listed_in"]
    assert rows[2]["shariah"] is False


def test_sectorwise_and_symbols():
    s = P.parse_sectorwise(fx("sectorwise.html"))
    assert s[1] == {"sector_code": "0804", "sector": "CEMENT", "advancers": 12, "decliners": 3,
                    "unchanged": 1, "turnover": 9000000.0, "market_cap_bn": 1100.5}
    sy = P.parse_symbols(fx("symbols.json"))
    assert sy[0]["is_debt"] and sy[3]["is_etf"]


def test_indices_and_constituents():
    idx = P.parse_indices(fx("indices.html"))
    assert [i["index"] for i in idx] == ["KSE100", "KMI30"]
    assert idx[0]["current"] == 170466.28 and round(idx[0]["change_pct"], 2) == -0.18
    c = P.parse_index_constituents(fx("kse100.html"))
    assert c[1]["symbol"] == "MEBL" and c[1]["index_weight_pct"] == 5.9 and c[1]["market_cap_mn"] == 990180


def test_company():
    c = P.parse_company(fx("company_MEBL.html"), "mebl")
    assert c["name"] == "Meezan Bank Limited" and c["sector"] == "COMMERCIAL BANKS"
    assert c["price"] == 547.94 and c["change"] == -0.18 and c["change_pct"] == -0.03
    assert c["as_of"].startswith("Mon, Sep 28, 2026")
    st = c["stats"]
    assert st["pe_ttm"] == 10.78 and st["change_1y_pct"] == 33.17 and st["change_ytd_pct"] == 23.3
    assert st["range_52w"] == {"low": 410.0, "high": 605.0}
    assert st["circuit_breaker"]["high"] == 602.93 and st["volume"] == 82407 and st["ldcp"] == 548.12
    assert st["open"] == 548.12  # REG panel, not DFC
    pr = c["profile"]
    assert pr["key_people"][0] == {"name": "Dr. Syed Amir Ali", "role": "CEO"}
    assert "A.F. Ferguson" in pr["auditor"] and pr["fiscal_year_end"] == "December"
    eq = c["equity"]
    assert eq["free_float_pct"] == 24.91 and eq["shares"] == 1807096448 and eq["market_cap_pkr_000s"] == 990180427.72
    fin = c["financials"]
    assert fin["annual"]["periods"][0] == "2025"
    assert fin["annual"]["rows"]["EPS"]["2024"] == 56.12
    assert fin["quarterly"]["rows"]["EPS"]["Q2 2026"] == -1.25
    assert c["ratios"]["rows"]["EPS Growth (%)"]["2025"] == -1.39
    assert c["ratios"]["rows"]["PEG"]["2025"] is None
    ann = c["recent_announcements"]
    assert ann["financial_results"][0]["pdf"].endswith("/download/document/281000.pdf")
    assert ann["others"] == []


def test_payouts():
    p = P.parse_payouts(fx("payouts.html"))
    assert p[0]["payout_pct_of_face"] == 80 and p[0]["type"] == "cash dividend"
    assert p[0]["sequence"] == "interim ii" and p[0]["period_type"] == "half-year"
    assert p[0]["cash_per_share_pkr_if_face_10"] == 8.0
    assert p[2]["sequence"] == "final" and p[2]["period_type"] == "annual"
    assert p[3]["type"] == "bonus shares" and p[3]["cash_per_share_pkr_if_face_10"] is None


def test_announcements():
    a = P.parse_announcements(fx("announcements.html"))
    assert a["total"] == 732 and len(a["items"]) == 3
    it = a["items"][0]
    assert it["symbol"] == "MEBL" and it["time"] == "1:59 PM" and it["pdf"].endswith("283196.pdf")


def test_timeseries():
    eod = P.parse_timeseries(fx("eod_MEBL.json"), "eod")
    assert eod[0]["date"] < eod[-1]["date"] and eod[-1]["date"] == "2026-09-25" and eod[-1]["close"] == 548.12
    it = P.parse_timeseries(fx("int_MEBL.json"), "int")
    assert it[-1]["time"] == "2026-09-28 13:10" and it[-1]["price"] == 547.94
