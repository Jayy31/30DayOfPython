"""Black-Scholes pricing, Greeks and implied volatility (European, index options).

Closed-form math with numpy/scipy; cross-checked against py_vollib in tests.
Theta is per calendar day; vega per 1 vol point (0.01).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime, time

from scipy.optimize import brentq
from scipy.stats import norm

from engine.core.models import OptionType
from engine.core.timeutils import IST, to_ist

RISK_FREE = 0.065
EXPIRY_CLOSE = time(15, 30)
YEAR_SECONDS = 365.0 * 24 * 3600
MIN_T = 1.0 / (365 * 24 * 60)  # one minute; avoids blow-ups at expiry


def time_to_expiry(now: datetime, expiry: date) -> float:
    """Years until 15:30 IST on expiry day (floored at one minute)."""
    end = datetime.combine(expiry, EXPIRY_CLOSE, tzinfo=IST)
    return max((end - to_ist(now)).total_seconds() / YEAR_SECONDS, MIN_T)


def _d1d2(s: float, k: float, t: float, iv: float, r: float) -> tuple[float, float]:
    vt = iv * math.sqrt(t)
    d1 = (math.log(s / k) + (r + 0.5 * iv * iv) * t) / vt
    return d1, d1 - vt


def price(s: float, k: float, t: float, iv: float, ot: OptionType, r: float = RISK_FREE) -> float:
    if t <= 0 or iv <= 0:
        return max(s - k, 0.0) if ot is OptionType.CE else max(k - s, 0.0)
    d1, d2 = _d1d2(s, k, t, iv, r)
    disc = math.exp(-r * t)
    if ot is OptionType.CE:
        return float(s * norm.cdf(d1) - k * disc * norm.cdf(d2))
    return float(k * disc * norm.cdf(-d2) - s * norm.cdf(-d1))


@dataclass(frozen=True)
class Greeks:
    iv: float
    delta: float
    gamma: float
    theta: float  # Rs per unit per calendar day
    vega: float   # Rs per unit per 1 vol point


def greeks(s: float, k: float, t: float, iv: float, ot: OptionType, r: float = RISK_FREE) -> Greeks:
    t = max(t, MIN_T)
    d1, d2 = _d1d2(s, k, t, iv, r)
    pdf = norm.pdf(d1)
    disc = math.exp(-r * t)
    gamma = pdf / (s * iv * math.sqrt(t))
    vega = s * pdf * math.sqrt(t) / 100
    common = -s * pdf * iv / (2 * math.sqrt(t))
    if ot is OptionType.CE:
        delta = norm.cdf(d1)
        theta = common - r * k * disc * norm.cdf(d2)
    else:
        delta = norm.cdf(d1) - 1
        theta = common + r * k * disc * norm.cdf(-d2)
    return Greeks(iv=float(iv), delta=float(delta), gamma=float(gamma), theta=float(theta) / 365, vega=float(vega))


def implied_vol(px: float, s: float, k: float, t: float, ot: OptionType, r: float = RISK_FREE) -> float | None:
    """IV in decimal, or None if the price is outside no-arbitrage bounds."""
    t = max(t, MIN_T)
    disc_k = k * math.exp(-r * t)
    lower = max(s - disc_k, 0.0) if ot is OptionType.CE else max(disc_k - s, 0.0)
    upper = s if ot is OptionType.CE else disc_k
    if not (lower < px < upper):
        return None
    try:
        return float(brentq(lambda v: price(s, k, t, v, ot, r) - px, 1e-4, 5.0, xtol=1e-7))
    except ValueError:
        return None


def greeks_from_price(px: float, s: float, k: float, t: float, ot: OptionType, r: float = RISK_FREE) -> Greeks | None:
    iv = implied_vol(px, s, k, t, ot, r)
    return None if iv is None else greeks(s, k, t, iv, ot, r)
