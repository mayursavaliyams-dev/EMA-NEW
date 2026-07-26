"""Black-Scholes greeks + implied vol (pure Python, no scipy needed)."""
import math
from datetime import datetime


def _ncdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _npdf(x):
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def bs_price(S, K, T, r, sigma, is_call):
    if sigma <= 0 or T <= 0:
        return max(0.0, (S - K) if is_call else (K - S))
    d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    if is_call:
        return S * _ncdf(d1) - K * math.exp(-r * T) * _ncdf(d2)
    return K * math.exp(-r * T) * _ncdf(-d2) - S * _ncdf(-d1)


def greeks(spot, strike, expiry_date, side, price, r=0.065):
    try:
        S, K, mkt = float(spot), float(strike), float(price)
        if S <= 0 or K <= 0 or mkt <= 0:
            return {}
        exp_dt = datetime.strptime(expiry_date, "%Y-%m-%d").replace(hour=15, minute=30)
        T = max((exp_dt - datetime.now()).total_seconds() / (365.0 * 24 * 3600), 1e-6)
        is_call = (side == "CE")
        lo, hi = 0.001, 5.0
        for _ in range(60):
            mid = (lo + hi) / 2
            if bs_price(S, K, T, r, mid, is_call) > mkt:
                hi = mid
            else:
                lo = mid
        iv = (lo + hi) / 2
        d1 = (math.log(S / K) + (r + 0.5 * iv * iv) * T) / (iv * math.sqrt(T))
        d2 = d1 - iv * math.sqrt(T)
        delta = _ncdf(d1) if is_call else _ncdf(d1) - 1
        gamma = _npdf(d1) / (S * iv * math.sqrt(T))
        vega = S * _npdf(d1) * math.sqrt(T) / 100.0
        theta = (-(S * _npdf(d1) * iv) / (2 * math.sqrt(T))
                 - r * K * math.exp(-r * T) * (_ncdf(d2) if is_call else _ncdf(-d2)) * (1 if is_call else -1)) / 365.0
        # PoP (prob option expires OTM = stays short side's favour) via N(d2)
        pop = (1 - _ncdf(d2)) if is_call else _ncdf(d2)
        return {"iv": round(iv * 100, 2), "delta": round(delta, 3), "gamma": round(gamma, 5),
                "theta": round(theta, 2), "vega": round(vega, 2), "pop": round(pop * 100, 1)}
    except Exception:
        return {}


def breakeven_pop_strangle(spot, ce_strike, pe_strike, credit, ce_iv, pe_iv, expiry_date, r=0.065):
    """Approx probability the spot stays between the two breakevens at expiry."""
    try:
        exp_dt = datetime.strptime(expiry_date, "%Y-%m-%d").replace(hour=15, minute=30)
        T = max((exp_dt - datetime.now()).total_seconds() / (365.0 * 24 * 3600), 1e-6)
        iv = ((ce_iv + pe_iv) / 2) / 100.0
        be_up = ce_strike + credit
        be_dn = pe_strike - credit
        sd = spot * iv * math.sqrt(T)
        if sd <= 0:
            return 0.0
        p_up = _ncdf((math.log(be_up / spot)) / (iv * math.sqrt(T)))
        p_dn = _ncdf((math.log(be_dn / spot)) / (iv * math.sqrt(T)))
        return round((p_up - p_dn) * 100, 1)
    except Exception:
        return 0.0
