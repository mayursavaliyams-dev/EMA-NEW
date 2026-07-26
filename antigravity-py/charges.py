"""Indian F&O charges model — ALWAYS net out of P&L (never hidden).
Approximate index-options (NSE/BSE) charges for a round-trip per lot-leg.
"""

# rates (approx, 2026)
BROKERAGE_PER_ORDER = 20.0      # flat per executed order (buy/sell)
STT_SELL = 0.000625            # 0.0625% on sell premium turnover (options)
EXCH_TXN = 0.0003503           # ~0.03503% premium turnover (NSE options)
SEBI = 0.000001                # 0.0001%
STAMP_BUY = 0.00003            # 0.003% on buy
GST = 0.18                     # on (brokerage + exch txn + sebi)


def leg_charges(premium, qty, side):
    """Charges for ONE option leg execution. side = 'BUY' | 'SELL'."""
    turnover = premium * qty
    brokerage = BROKERAGE_PER_ORDER
    exch = EXCH_TXN * turnover
    sebi = SEBI * turnover
    stt = STT_SELL * turnover if side == "SELL" else 0.0
    stamp = STAMP_BUY * turnover if side == "BUY" else 0.0
    gst = GST * (brokerage + exch + sebi)
    return brokerage + exch + sebi + stt + stamp + gst


def round_trip_charges(entry_premium, exit_premium, qty, opened="SELL"):
    """Total charges for a full round trip (open + close) of one leg."""
    if opened == "SELL":   # sell to open, buy to close
        return leg_charges(entry_premium, qty, "SELL") + leg_charges(exit_premium, qty, "BUY")
    return leg_charges(entry_premium, qty, "BUY") + leg_charges(exit_premium, qty, "SELL")
