#!/usr/bin/env python3
"""WikiMasters auto pack opener. One account, opens a pack as soon as one is available.

Secrets: ~/.secrets/wikimasters (WM_REFRESH_TOKEN) and ~/.secrets/wikimasters.session.json.
Log: ~/wikimasters-bot/pulls.jsonl (one line per opened pack).
"""
import base64, datetime as dt, json, os, random, sys, time, urllib.error, urllib.request

SITE = "https://www.wiki-masters.com"
SB = "https://cyrxjeppjqsxxjayfrur.supabase.co"
REF = "cyrxjeppjqsxxjayfrur"
ANON = os.environ.get("WM_ANON_KEY", "")  # public anon key, set in the service env
SESSION = os.path.expanduser("~/.secrets/wikimasters.session.json")
LOG = os.path.expanduser("~/wikimasters-bot/pulls.jsonl")
UA = "Mozilla/5.0 (X11; Linux x86_64) wmbot (personal, one account)"


def log(*a):
    print(dt.datetime.now().strftime("%m-%d %H:%M:%S"), *a, flush=True)


def save(s):
    tmp = SESSION + ".tmp"
    with open(tmp, "w") as f:
        json.dump(s, f)
    os.chmod(tmp, 0o600)
    os.replace(tmp, SESSION)


def refresh(s):
    req = urllib.request.Request(
        f"{SB}/auth/v1/token?grant_type=refresh_token", method="POST",
        data=json.dumps({"refresh_token": s["refresh_token"]}).encode(),
        headers={"apikey": ANON, "Content-Type": "application/json"})
    d = json.load(urllib.request.urlopen(req, timeout=20))
    s = {k: d[k] for k in ("access_token", "refresh_token", "expires_at")}
    save(s)
    log("session refreshed")
    return s


def cookie(s):
    v = "base64-" + base64.urlsafe_b64encode(json.dumps(
        {**s, "token_type": "bearer", "expires_in": 3600}).encode()).decode().rstrip("=")
    return "; ".join(f"sb-{REF}-auth-token.{i}={v[j:j + 3180]}"
                     for i, j in enumerate(range(0, len(v), 3180)))


def until(iso):
    t = dt.datetime.fromisoformat(iso.replace("Z", "+00:00"))
    return max(5.0, (t - dt.datetime.now(dt.timezone.utc)).total_seconds())


def main():
    s = json.load(open(SESSION))
    auth_fail = 0
    while True:
        if s["expires_at"] - time.time() < 600:
            try:
                s = refresh(s)
            except Exception as e:
                log("refresh failed:", e)
                time.sleep(300)
                continue
        req = urllib.request.Request(f"{SITE}/api/packs/open", method="POST", headers={
            "Cookie": cookie(s), "User-Agent": UA, "Origin": SITE, "Referer": f"{SITE}/pulls"})
        try:
            d = json.load(urllib.request.urlopen(req, timeout=30))
            auth_fail = 0
            cards = [{"r": c["rarity"], "t": c["wikipedia_title"], "atk": c.get("atk"), "def": c.get("def")}
                     for c in d["cards"]]
            with open(LOG, "a") as f:
                f.write(json.dumps({"at": dt.datetime.now(dt.timezone.utc).isoformat(), "cards": cards}) + "\n")
            log("opened:", ", ".join(f'{c["r"]} {c["t"]}' for c in cards), "| left", d.get("packs_remaining"))
            if d.get("packs_remaining", 0) > 0:
                time.sleep(random.uniform(20, 40))
                continue
            wait = until(d["packs_last_regen_at"]) + 600 if d.get("packs_last_regen_at") else 600
            time.sleep(min(max(wait, 30), 660) + random.uniform(5, 30))
        except urllib.error.HTTPError as e:
            body = e.read()[:400]
            try:
                j = json.loads(body)
            except Exception:
                j = {}
            if e.code == 429 or j.get("rate_limited"):
                w = until(j["retry_after"]) if j.get("retry_after") else 120
                if j.get("rate_limit_daily"):
                    log("daily limit reached, sleeping", int(w), "s")
                time.sleep(w + random.uniform(3, 15))
            elif e.code in (401, 403):
                auth_fail += 1
                log("auth error", e.code, body)
                if auth_fail >= 3:
                    log("stopping: auth keeps failing, new refresh token needed")
                    sys.exit(2)
                s = refresh(s)
            else:
                log("http", e.code, body)
                time.sleep(120)
        except Exception as e:
            log("error", e)
            time.sleep(120)


if __name__ == "__main__":
    main()
