"""Vectorised Black-Scholes implied volatility + greeks (numpy) — 25,000 strikes in ~50 ms.

Same model and conventions as greeks.py (r = 6.5 %, IV in %, theta per day, vega per 1 vol pt),
so the tracker and the strangle dashboard agree.
"""
import numpy as np
from math import sqrt, pi

R = 0.065
_SQ2 = sqrt(2.0)


def _ncdf(x):
    # erf via numpy-friendly approximation (Abramowitz-Stegun 7.1.26, |err| < 1.5e-7)
    s = np.sign(x)
    a = np.abs(x) / _SQ2
    t = 1.0 / (1.0 + 0.3275911 * a)
    y = 1.0 - (((((1.061405429 * t - 1.453152027) * t) + 1.421413741) * t - 0.284496736) * t + 0.254829592) * t * np.exp(-a * a)
    return 0.5 * (1.0 + s * y)


def _npdf(x):
    return np.exp(-0.5 * x * x) / sqrt(2.0 * pi)


def _price(S, K, T, sig, call):
    sq = np.sqrt(T)
    d1 = (np.log(S / K) + (R + 0.5 * sig * sig) * T) / (sig * sq)
    d2 = d1 - sig * sq
    c = S * _ncdf(d1) - K * np.exp(-R * T) * _ncdf(d2)
    p = K * np.exp(-R * T) * _ncdf(-d2) - S * _ncdf(-d1)
    return np.where(call, c, p)


def iv_greeks(S, K, T, price, call, iters=48):
    """Arrays in -> dict of arrays out: iv (%), delta, gamma, theta (/day), vega (/1 vol pt).
    Entries with no valid IV (price below intrinsic, T<=0, …) come back as NaN."""
    S, K, T, price = (np.asarray(a, dtype=float) for a in (S, K, T, price))
    call = np.asarray(call, dtype=bool)
    T = np.maximum(T, 1e-6)
    intrinsic = np.where(call, np.maximum(S - K * np.exp(-R * T), 0), np.maximum(K * np.exp(-R * T) - S, 0))
    ok = (S > 0) & (K > 0) & (price > 0) & (price > intrinsic + 1e-9)
    lo = np.full(S.shape, 1e-3)
    hi = np.full(S.shape, 5.0)
    for _ in range(iters):
        mid = 0.5 * (lo + hi)
        over = _price(S, K, T, mid, call) > price
        hi = np.where(over, mid, hi)
        lo = np.where(over, lo, mid)
    iv = 0.5 * (lo + hi)
    sq = np.sqrt(T)
    d1 = (np.log(S / K) + (R + 0.5 * iv * iv) * T) / (iv * sq)
    d2 = d1 - iv * sq
    delta = np.where(call, _ncdf(d1), _ncdf(d1) - 1)
    gamma = _npdf(d1) / (S * iv * sq)
    vega = S * _npdf(d1) * sq / 100.0
    theta = (-(S * _npdf(d1) * iv) / (2 * sq)
             - R * K * np.exp(-R * T) * np.where(call, _ncdf(d2), _ncdf(-d2)) * np.where(call, 1, -1)) / 365.0
    nan = np.nan
    return {"iv": np.where(ok, np.round(iv * 100, 2), nan), "delta": np.where(ok, np.round(delta, 3), nan),
            "gamma": np.where(ok, np.round(gamma, 5), nan), "theta": np.where(ok, np.round(theta, 2), nan),
            "vega": np.where(ok, np.round(vega, 2), nan)}
