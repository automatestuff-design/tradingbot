"""Indicators and signal functions. Each strategy takes a list of closes and returns BUY, SELL or None."""
from typing import Optional

BUY, SELL = "BUY", "SELL"


def sma(values, n):
    if len(values) < n:
        return None
    return sum(values[-n:]) / n


def sma_series(values, n):
    out, s = [], 0.0
    for i, v in enumerate(values):
        s += v
        if i >= n:
            s -= values[i - n]
        out.append(s / n if i >= n - 1 else None)
    return out


def rsi(values, n=14):
    if len(values) < n + 1:
        return None
    gains = losses = 0.0
    for a, b in zip(values[-n - 1:-1], values[-n:]):
        d = b - a
        gains += max(d, 0)
        losses += max(-d, 0)
    if losses == 0:
        return 100.0
    rs = (gains / n) / (losses / n)
    return 100 - 100 / (1 + rs)


def sma_cross(closes, s) -> Optional[str]:
    if len(closes) < s.slow + 1:
        return None
    f_now, s_now = sma(closes, s.fast), sma(closes, s.slow)
    f_prev, s_prev = sma(closes[:-1], s.fast), sma(closes[:-1], s.slow)
    if f_prev <= s_prev and f_now > s_now:
        return BUY
    if f_prev >= s_prev and f_now < s_now:
        return SELL
    return None


def rsi_reversion(closes, s) -> Optional[str]:
    r = rsi(closes, s.rsi_period)
    if r is None:
        return None
    if r < s.rsi_low:
        return BUY
    if r > s.rsi_high:
        return SELL
    return None


def breakout(closes, s) -> Optional[str]:
    n = s.breakout_lookback
    if len(closes) < n + 1:
        return None
    window = closes[-n - 1:-1]
    if closes[-1] > max(window):
        return BUY
    if closes[-1] < min(window):
        return SELL
    return None


STRATEGIES = {"sma_cross": sma_cross, "rsi_reversion": rsi_reversion, "breakout": breakout}
