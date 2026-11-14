"""Currency helpers. Rates are always 'units of the account's base currency per 1 unit of X'."""


def symbol_currency(sym):
    """RY.TO / ABC.V trade in CAD; everything else is treated as a US listing in USD."""
    return "CAD" if sym.strip().upper().endswith((".TO", ".V")) else "USD"


def cross(base, cur, usdcad):
    """Convert using a USD/CAD rate (CAD per 1 USD). Returns base-currency units per 1 `cur`."""
    if cur == base:
        return 1.0
    if not usdcad:
        return None
    if base == "CAD" and cur == "USD":
        return usdcad
    if base == "USD" and cur == "CAD":
        return 1 / usdcad
    return None
