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

NAMES = {"sma_cross": "SMA crossover", "rsi_reversion": "RSI mean reversion", "breakout": "Breakout"}


# ---------- explanations: the exact numbers behind a signal, for approval requests ----------
def _explain_sma(closes, s):
    f_now, s_now = sma(closes, s.fast), sma(closes, s.slow)
    f_prev, s_prev = sma(closes[:-1], s.fast), sma(closes[:-1], s.slow)
    if None in (f_now, s_now, f_prev, s_prev):
        return []
    direction = "above" if f_now > s_now else "below"
    return [f"The {s.fast}-bar average ({f_now:.2f}) just crossed {direction} the {s.slow}-bar average ({s_now:.2f}); "
            f"one bar earlier they were {f_prev:.2f} and {s_prev:.2f}."]


def _explain_rsi(closes, s):
    r = rsi(closes, s.rsi_period)
    if r is None:
        return []
    if r < s.rsi_low:
        return [f"RSI({s.rsi_period}) is {r:.1f}, below the buy level of {s.rsi_low:g}: the price has fallen quickly "
                f"and the strategy bets on a bounce."]
    return [f"RSI({s.rsi_period}) is {r:.1f}, above the sell level of {s.rsi_high:g}: the price has risen quickly "
            f"and the strategy expects it to cool off."]


def _explain_breakout(closes, s):
    n = s.breakout_lookback
    if len(closes) < n + 1:
        return []
    window = closes[-n - 1:-1]
    hi, lo = max(window), min(window)
    if closes[-1] > hi:
        return [f"The price ({closes[-1]:.2f}) broke above the highest close of the previous {n} bars ({hi:.2f})."]
    return [f"The price ({closes[-1]:.2f}) fell below the lowest close of the previous {n} bars ({lo:.2f})."]


_EXPLAIN = {"sma_cross": _explain_sma, "rsi_reversion": _explain_rsi, "breakout": _explain_breakout}


def explain(name, closes, s):
    """Plain-language lines describing why `name` fired on these closes."""
    fn = _EXPLAIN.get(name)
    return fn(closes, s) if fn else []
