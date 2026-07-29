"""Exit-decision regression tests for manage().

Covers the two bugs found in the full-code review:
  1. a stale WING (deep OTM, most likely leg to lose its quote) disarmed the 2x
     stop on a short leg whose own price was live;
  2. takeProfitPct = 0 made the take-profit condition read "gross >= 0", closing
     the position the moment it was a rupee in profit.

Isolated temp state; the live forward-test is untouched.
"""
import os
import json
import tempfile

os.environ["squareOffIST"] = "23:59"        # keep EOD out of the way

import config
from engines import strangle_engine as E

CE, PE, CW, PW = "K|CE", "K|PE", "K|CW", "K|PW"


class Conn:
    name = "fake"
    def __init__(self, px):
        self.px = px
    def ltp(self, keys):
        return {k: v for k, v in self.px.items() if k in keys and v is not None}
    def place_order(self, *a, **k):
        return {"status": "paper"}


def position(credit=5000):
    return {"id": 1, "symbol": "NIFTY", "structure": "condor", "status": "OPEN", "stale": False,
            "lots": 1, "qty": 65, "credit": credit, "mtm": 0.0, "max_loss": 20000,
            "entry_time": "2026-07-29T10:00:00", "expiry": "2026-08-06", "iv_pct": 70, "pop": 80,
            "legs": [
                {"side": "SELL", "opt": "CE", "strike": 24500, "key": CE, "entry": 100.0, "qty": 65},
                {"side": "SELL", "opt": "PE", "strike": 24000, "key": PE, "entry": 80.0, "qty": 65},
                {"side": "BUY", "opt": "CE", "strike": 24900, "key": CW, "entry": 20.0, "qty": 65},
                {"side": "BUY", "opt": "PE", "strike": 23600, "key": PW, "entry": 15.0, "qty": 65},
            ]}


def scenario(name, prices, expect, credit=5000, tp=None):
    if tp is None:
        os.environ.pop("takeProfitPct", None)
    else:
        os.environ["takeProfitPct"] = str(tp)
    st = E._blank()
    st.update({"positions": [position(credit)], "day": E._today()})
    fd, tmp = tempfile.mkstemp(suffix=".json"); os.close(fd)
    json.dump(st, open(tmp, "w"))
    E.STATE = tmp
    E.StrangleEngine(Conn(prices)).manage()
    out = json.load(open(tmp)); os.unlink(tmp)
    closed = out["closed"]
    got = (closed[0]["exit_reason"] if closed else "OPEN")
    ok = (expect in got) if expect != "OPEN" else (got == "OPEN")
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
    print(f"         expected {expect!r}  ->  got {got!r}")
    return ok


print("MANAGE() EXIT REGRESSION TESTS")
print(f"  config takeProfitPct default = {config.cfg('takeProfitPct')}, legStopMult = {config.cfg('legStopMult')}\n")

r = []
# 1. short CE at 2x, every leg quoted -> stop
r.append(scenario("short CE at 2x, all quotes live -> LEG STOP",
                  {CE: 200.0, PE: 70.0, CW: 25.0, PW: 12.0}, "LEG STOP"))

# 2. THE BUG: short CE at 2x but a WING has no quote -> stop must still fire
r.append(scenario("short CE at 2x, WING quote missing -> LEG STOP still fires",
                  {CE: 200.0, PE: 70.0, CW: None, PW: 12.0}, "LEG STOP"))

# 3. a SHORT leg has no quote -> risk unmeasurable, hold (no price-based exit)
r.append(scenario("SHORT leg quote missing -> withhold, stay OPEN",
                  {CE: None, PE: 70.0, CW: 25.0, PW: 12.0}, "OPEN"))

# 4. THE OTHER BUG: takeProfitPct = 0 must NOT close a barely-profitable position
r.append(scenario("takeProfitPct=0, tiny profit -> must stay OPEN",
                  {CE: 99.0, PE: 79.0, CW: 20.0, PW: 15.0}, "OPEN", tp=0))

# 5. take-profit at 50% of credit still works
#    credit 5000; need gross >= 2500 -> shorts decay by ~40 points total
r.append(scenario("50% of credit decayed -> TAKE PROFIT",
                  {CE: 70.0, PE: 55.0, CW: 20.0, PW: 15.0}, "TAKE PROFIT", tp=50))

print("\n" + ("ALL PASS ✓" if all(r) else "SOME FAILED ✗"))
