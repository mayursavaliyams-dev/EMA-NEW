"""Copy the live bot's fresh Dhan token into the tracker's dhan.json.

Reads C:\\Expiry-Friday-5x\\data\\dhan-token.json (the bot's own store, written by its
login) and the client id from the bot's .env (DHAN_CLIENT_ID) or the token's
dhanClientId claim. Writes dhan.json only when the bot's token expires later than
the one already there, atomically (tmp file + os.replace).

Never prints or logs a token or client id. One line per run goes to
data/dhan_refresh.log: time, exp, updated/unchanged/error.
It never logs in, and never uses the PIN or TOTP secret.
"""
import base64
import datetime
import json
import os
import sys

BOT = os.environ.get("BOT_DIR", r"C:\Expiry-Friday-5x")
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # antigravity-py
OUT = os.path.join(HERE, "dhan.json")
LOG = os.path.join(HERE, "data", "dhan_refresh.log")
IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))


def jwt_claims(tok):
    parts = str(tok or "").split(".")
    if len(parts) != 3:
        return {}
    try:
        return json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
    except Exception:
        return {}


def env_vals(path):
    d = {}
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    d[k.strip()] = v.strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    return d


def fmt(exp):
    return datetime.datetime.fromtimestamp(exp, IST).strftime("%Y-%m-%d %H:%M IST") if exp else "none"


def log(msg):
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    line = f"{datetime.datetime.now(IST):%Y-%m-%d %H:%M:%S} IST  {msg}"
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")
    print(line)


def main():
    env = env_vals(os.path.join(BOT, ".env"))
    data_dir = env.get("DATA_DIR") or "."
    base = data_dir if os.path.isabs(data_dir) else os.path.join(BOT, data_dir)
    cands = [os.path.join(base, "dhan-token.json"), os.path.join(BOT, "data", "dhan-token.json")]
    src = next((c for c in cands if os.path.isfile(c)), None)
    if not src:
        log("error: bot token store not found")
        return 2
    try:
        with open(src, encoding="utf-8") as f:
            tok = (json.load(f) or {}).get("token")
    except Exception as e:
        log(f"error: cannot read bot token store ({type(e).__name__})")
        return 2
    if not tok:
        log("error: bot token store has no token")
        return 2

    claims = jwt_claims(tok)
    new_exp = claims.get("exp")
    cid = str(claims.get("dhanClientId") or env.get("DHAN_CLIENT_ID") or "")
    if not cid:
        log("error: no client id")
        return 2

    old_exp, old_tok = None, None
    try:
        with open(OUT, encoding="utf-8") as f:
            cur = json.load(f)
        old_tok = cur.get("access_token")
        old_exp = jwt_claims(old_tok).get("exp")
    except Exception:
        pass

    now = datetime.datetime.now(datetime.timezone.utc).timestamp()
    if new_exp and new_exp <= now:
        log(f"unchanged: bot token already expired (exp {fmt(new_exp)}); bot has not logged in yet")
        return 1
    if old_tok == tok or (old_exp and new_exp and new_exp <= old_exp):
        log(f"unchanged: dhan.json exp {fmt(old_exp)} (bot exp {fmt(new_exp)})")
        return 0

    tmp = OUT + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"client_id": cid, "access_token": tok}, f)
    os.replace(tmp, OUT)
    log(f"updated: dhan.json exp {fmt(old_exp)} -> {fmt(new_exp)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
