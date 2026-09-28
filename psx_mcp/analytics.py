"""Indicator math over daily closes (oldest-first lists)."""

from __future__ import annotations

from datetime import date, timedelta


def sma(values: list[float], n: int) -> float | None:
    if len(values) < n or n <= 0:
        return None
    return round(sum(values[-n:]) / n, 4)


def ema_series(values: list[float], n: int) -> list[float]:
    if len(values) < n:
        return []
    k = 2 / (n + 1)
    out = [sum(values[:n]) / n]
    for v in values[n:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def rsi(values: list[float], n: int = 14) -> float | None:
    """Wilder's RSI."""
    if len(values) < n + 1:
        return None
    gains, losses = [], []
    for a, b in zip(values[:-1], values[1:]):
        d = b - a
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    avg_g = sum(gains[:n]) / n
    avg_l = sum(losses[:n]) / n
    for g, l in zip(gains[n:], losses[n:]):
        avg_g = (avg_g * (n - 1) + g) / n
        avg_l = (avg_l * (n - 1) + l) / n
    if avg_l == 0:
        return 100.0
    rs = avg_g / avg_l
    return round(100 - 100 / (1 + rs), 2)


def macd(values: list[float]) -> dict | None:
    e12, e26 = ema_series(values, 12), ema_series(values, 26)
    if not e26:
        return None
    e12 = e12[-len(e26):]
    line = [a - b for a, b in zip(e12, e26)]
    sig = ema_series(line, 9)
    if not sig:
        return None
    return {"macd": round(line[-1], 4), "signal": round(sig[-1], 4), "histogram": round(line[-1] - sig[-1], 4)}


def pct_change(new: float | None, old: float | None) -> float | None:
    if new is None or old in (None, 0):
        return None
    return round((new - old) / old * 100, 2)


def close_on_or_before(bars: list[dict], target: date) -> float | None:
    val = None
    for b in bars:
        if date.fromisoformat(b["date"]) <= target:
            val = b["close"]
        else:
            break
    return val


def summarize(bars: list[dict]) -> dict:
    """Returns, moving averages, RSI, MACD, 52-week range, volatility."""
    if not bars:
        return {}
    closes = [b["close"] for b in bars]
    last = bars[-1]
    last_d = date.fromisoformat(last["date"])
    rets = {}
    for label, days in [("1w", 7), ("1m", 30), ("3m", 91), ("6m", 182), ("1y", 365), ("3y", 1095)]:
        rets[label] = pct_change(last["close"], close_on_or_before(bars, last_d - timedelta(days=days)))
    ytd_base = close_on_or_before(bars, date(last_d.year - 1, 12, 31))
    rets["ytd"] = pct_change(last["close"], ytd_base)

    yr = [b for b in bars if date.fromisoformat(b["date"]) > last_d - timedelta(days=365)]
    hi = max(yr, key=lambda b: b["close"]) if yr else None
    lo = min(yr, key=lambda b: b["close"]) if yr else None

    daily = [(b - a) / a for a, b in zip(closes[-61:-1], closes[-60:]) if a]
    vol = None
    if len(daily) > 5:
        m = sum(daily) / len(daily)
        vol = round((sum((x - m) ** 2 for x in daily) / (len(daily) - 1)) ** 0.5 * (245 ** 0.5) * 100, 2)

    vols = [b["volume"] for b in bars[-20:] if b.get("volume") is not None]
    s20, s50, s200 = sma(closes, 20), sma(closes, 50), sma(closes, 200)
    return {
        "last_close": last["close"],
        "last_date": last["date"],
        "returns_pct": rets,
        "high_52w": {"close": hi["close"], "date": hi["date"]} if hi else None,
        "low_52w": {"close": lo["close"], "date": lo["date"]} if lo else None,
        "pct_from_52w_high": pct_change(last["close"], hi["close"]) if hi else None,
        "sma": {"20": s20, "50": s50, "200": s200},
        "price_vs_sma200": ("above" if last["close"] > s200 else "below") if s200 else None,
        "rsi_14": rsi(closes, 14),
        "macd": macd(closes),
        "annualized_volatility_60d_pct": vol,
        "avg_volume_20d": int(sum(vols) / len(vols)) if vols else None,
    }
