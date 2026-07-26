"""Margin-aware fractional-Kelly position sizing.
Full Kelly is too aggressive for naked option selling; scale by IV/VIX regime.
A naked NIFTY strangle needs ~1.3L margin/lot; an iron condor ~15-20k/lot.
"""

# rough margin per lot (Rs) by symbol & structure
MARGIN = {
    "NIFTY":     {"naked": 130000, "condor": 18000},
    "BANKNIFTY": {"naked": 160000, "condor": 22000},
    "SENSEX":    {"naked": 120000, "condor": 16000},
}


def max_lots(symbol, structure, capital, util=0.6):
    """How many lots the capital can fund (using `util` fraction of capital)."""
    m = MARGIN.get(symbol, {}).get(structure)
    if not m:
        return 0
    return max(0, int((capital * util) // m))


def kelly_fraction(win_rate, avg_win, avg_loss, cap=0.25):
    """Fractional Kelly (capped). win_rate 0..1; avg_win/avg_loss in Rs (>0)."""
    if avg_loss <= 0:
        return 0.0
    b = avg_win / avg_loss
    f = (win_rate * (b + 1) - 1) / b
    f = max(0.0, f)
    return min(f, cap)   # never full Kelly


def sized_lots(symbol, structure, capital, win_rate=0.91, avg_win=3000, avg_loss=9000,
               iv_percentile=50):
    """Lots to trade: fractional-Kelly, scaled down when IV (risk) is high."""
    f = kelly_fraction(win_rate, avg_win, avg_loss)
    # scale down in high-IV (more tail risk) regimes
    iv_scale = 1.0 if iv_percentile < 60 else (0.6 if iv_percentile < 85 else 0.3)
    budget = capital * f * iv_scale
    m = MARGIN.get(symbol, {}).get(structure, 1e9)
    return max(0, int(budget // m)), round(f, 3), iv_scale
