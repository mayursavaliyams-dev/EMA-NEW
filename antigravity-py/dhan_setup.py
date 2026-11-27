"""Save the Dhan access token to dhan.json (one question, no technical steps).

Run by DHAN_TOKEN.bat. Asks for the token, checks it with Dhan's /profile
(a read-only REST call, no WebSocket), reads the client id from Dhan's
answer and writes dhan.json next to this file. Never prints the token.
"""
import json
import os
import sys
import time

import httpx

HERE = os.path.dirname(os.path.abspath(__file__))
PATH = os.path.join(HERE, "dhan.json")
PROFILE = "https://api.dhan.co/v2/profile"


def check(token, http=None):
    """Return (client_id, None) when Dhan accepts the token, else (None, reason)."""
    http = http or httpx.Client(timeout=20)
    try:
        r = http.get(PROFILE, headers={"access-token": token, "Accept": "application/json"})
    except Exception as e:
        return None, f"internet / Dhan sathe connect na thayu ({type(e).__name__})"
    if r.status_code != 200:
        return None, f"Dhan e token na swikaryo (HTTP {r.status_code}) - token juno ke khoto chhe"
    try:
        cid = str(r.json().get("dhanClientId") or "")
    except Exception:
        cid = ""
    return (cid, None) if cid else (None, "Dhan no jawab samjayo nahi")


def save(token, client_id, path=PATH):
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"client_id": client_id, "access_token": token, "saved_at": time.time()}, f)


def renew_quiet():
    """TRACKER_START: renew the saved token if it is older than 20 h. Never asks anything."""
    if not os.path.exists(PATH):
        return 0
    sys.path.insert(0, HERE)
    from trackers.providers import DhanProvider
    d = DhanProvider(1)
    age = d.token_age_h(PATH)
    if age is not None and age < 20:
        print(f"  Dhan token OK ({age:.0f} kalak juno).")
        return 0
    ok, msg = d.renew(PATH)
    if ok:
        print("  [OK] Dhan token potani rite renew thayo.")
        return 0
    print(f"  [!] Dhan token renew na thayo ({msg}). DHAN_TOKEN.bat chalavi navo token nakho.")
    return 2


def main():
    print()
    print("  Dhan web (web.dhan.co) > Profile > 'Access DhanHQ APIs' ma jao,")
    print("  'Generate Access Token' dabavo ane token copy karo.")
    print()
    token = input("  Token ahi paste karo (Right-click) ane Enter dabavo: ").strip().strip('"')
    if len(token) < 50:
        print("\n  [X] Token barabar paste thayo nathi. Fari try karo.")
        return 1
    cid, err = check(token)
    if err:
        print(f"\n  [X] {err}.")
        return 1
    save(token, cid)
    print("\n  [OK] Dhan token save thai gayo. Have TRACKER_START.bat chalavo.")
    return 0


if __name__ == "__main__":
    sys.exit(renew_quiet() if "--renew" in sys.argv else main())
