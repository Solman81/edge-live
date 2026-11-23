"""
EDGE AUTO RESULTS - settles your open bets from final scores. Started by Supabase:
  python results.py settle    every 2 hours, silent: writes W / L / P to the dashboard (tagged "auto")
  python results.py summary   once a day: Telegram summary of the last 24h + anything that needs checking by hand
Matches each bet to its game by the odds-feed event id (no name guessing). Anything it can't settle safely
is left open for you and listed once in the morning summary.
Secrets: ODDS_API_KEY, SUPABASE_URL, SUPABASE_SECRET, EDGE_TELEGRAM_TOKEN (your main Edge bot)
"""
import os, sys, html, requests
from datetime import datetime, timezone, timedelta
from collections import defaultdict

ODDS_API_KEY = os.environ["ODDS_API_KEY"]
SB = os.environ["SUPABASE_URL"] + "/rest/v1"
SH = {"apikey": os.environ["SUPABASE_SECRET"], "Content-Type": "application/json"}
TG_TOKEN = os.environ.get("EDGE_TELEGRAM_TOKEN") or os.environ.get("TELEGRAM_TOKEN")
OH = {"X-RapidAPI-Key": ODDS_API_KEY, "X-RapidAPI-Host": "odds.p.rapidapi.com"}
MIN_AGE_H = 2          # only look at games that started at least 2 hours ago
FEED_DAYS = 3          # the scores feed only goes back 3 days

def db_get(table, **p):
    r = requests.get(f"{SB}/{table}", headers=SH, params=p, timeout=30); r.raise_for_status(); return r.json()

def db_patch(table, filters, data):
    r = requests.patch(f"{SB}/{table}", headers=SH, params=filters, json=data, timeout=30); r.raise_for_status()

def setting(key, default=None):
    rows = db_get("settings", select="value", key=f"eq.{key}")
    return rows[0]["value"] if rows else default

def settle(bet, ev):
    """('W'|'L'|'P', home_score, away_score)  or  (None, reason)"""
    if not ev.get("completed"):
        return None, "not finished"
    sc = {s["name"]: s["score"] for s in (ev.get("scores") or [])}
    h, a = ev["home_team"], ev["away_team"]
    if h not in sc or a not in sc:
        return None, "no final score"
    try:
        hs, as_ = float(sc[h]), float(sc[a])
    except (TypeError, ValueError):
        return None, "score not a number"
    mk, out = bet["market"], bet["outcome"]
    line = None if bet.get("line") is None else float(bet["line"])
    sk = bet.get("sport_key") or ""
    if sk.startswith("tennis") and mk != "h2h":
        return None, "tennis games/handicap - feed only has sets"
    if sk.startswith("icehockey") and mk != "h2h" and abs(hs - as_) == 1:
        return None, "one-goal hockey game - may have gone to OT (check the regulation score)"
    if mk == "h2h":
        if out.lower() == "draw": return ("W" if hs == as_ else "L"), hs, as_
        if out == h: return ("W" if hs > as_ else "L"), hs, as_
        if out == a: return ("W" if as_ > hs else "L"), hs, as_
        return None, "team name mismatch"
    if mk == "totals" and line is not None:
        tot = hs + as_
        if tot == line: return "P", hs, as_
        if out.lower().startswith("over"):  return ("W" if tot > line else "L"), hs, as_
        if out.lower().startswith("under"): return ("W" if tot < line else "L"), hs, as_
        return None, "over/under not recognised"
    if mk == "spreads" and line is not None:
        if out == h:   m = hs + line - as_
        elif out == a: m = as_ + line - hs
        else: return None, "team name mismatch"
        return ("P" if m == 0 else "W" if m > 0 else "L"), hs, as_
    return None, "market not supported"

def run_settle():
    now = datetime.now(timezone.utc)
    bets = db_get("bets", select="*", result="is.null",
                  commence_time=f"lt.{(now - timedelta(hours=MIN_AGE_H)).isoformat()}")
    bets = [b for b in bets if b.get("event_id") and b.get("sport_key")]
    print(f"Open bets old enough to check: {len(bets)}")
    by_sport = defaultdict(list)
    for b in bets: by_sport[b["sport_key"]].append(b)
    n_ok = 0
    for sport, bl in by_sport.items():
        r = requests.get(f"https://odds.p.rapidapi.com/v4/sports/{sport}/scores", headers=OH,
                         params={"daysFrom": FEED_DAYS, "dateFormat": "iso"}, timeout=30)
        if r.status_code != 200:
            print(f"  {sport}: scores [{r.status_code}] {r.text[:120]}"); continue
        print(f"  {sport}: {len(bl)} bet(s)  (quota left: {r.headers.get('x-requests-remaining', '?')})")
        events = {e["id"]: e for e in r.json() or []}
        for b in bl:
            ev = events.get(b["event_id"])
            age = now - datetime.fromisoformat(b["commence_time"].replace("Z", "+00:00"))
            res = settle(b, ev) if ev else (None, "game not found in scores feed")
            if res[0] is None:
                late = (res[1] == "game not found in scores feed" and age > timedelta(days=FEED_DAYS - 0.25)) or \
                       (res[1] not in ("not finished", "game not found in scores feed")) or \
                       (res[1] == "not finished" and age > timedelta(hours=10))
                if late and "[check]" not in (b.get("notes") or ""):
                    db_patch("bets", {"id": f"eq.{b['id']}"},
                             {"notes": ((b.get("notes") or "") + f" [check] {res[1]}").strip()})
                continue
            result, hs, as_ = res
            db_patch("bets", {"id": f"eq.{b['id']}", "result": "is.null"},
                     {"result": result, "home_score": hs, "away_score": as_,
                      "settled_at": now.isoformat(), "settled_by": "auto"})
            n_ok += 1
            print(f"    {result}  {b['home']} {hs:g}-{as_:g} {b['away']}  | {b['outcome']} {b.get('line') or ''}")
    print(f"Settled {n_ok}.")

def run_summary():
    chat = setting("telegram_chat_id")
    if not (TG_TOKEN and chat):
        print("No Telegram token/chat id - summary skipped"); return
    since = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    done = db_get("bets", select="*", settled_at=f"gte.{since}", result="not.is.null")
    check = [b for b in db_get("bets", select="*", result="is.null") if "[check]" in (b.get("notes") or "")]
    e = html.escape
    eur = lambda b: float(b.get("profit") or 0) * float(b.get("fx") or 1)
    lines = []
    if done:
        tot = sum(eur(b) for b in done)
        w = sum(b["result"] == "W" for b in done); l = sum(b["result"] == "L" for b in done); p = len(done) - w - l
        sign = "+" if tot >= 0 else "−"
        lines.append(f"\U0001F4CA <b>Last 24h:</b> {len(done)} settled · {w}W {l}L" + (f" {p}P" if p else "") +
                     f" · <b>{sign}€{abs(tot):.2f}</b>")
    else:
        lines.append("\U0001F4CA <b>Last 24h:</b> nothing settled")
    if check:
        lines.append(f"\n<b>Needs checking</b> ({len(check)}) — set these by hand on the dashboard:")
        for b in sorted(check, key=lambda b: b["commence_time"])[:20]:
            why = (b.get("notes") or "").split("[check]", 1)[-1].strip()
            lines.append(f"• {e(b['home'])} v {e(b['away'])} · {e(b['outcome'])} — {e(why)}")
    requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                  json={"chat_id": chat, "text": "\n".join(lines), "parse_mode": "HTML"}, timeout=20)
    print("\n".join(lines))

if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "settle"
    run_summary() if mode == "summary" else run_settle()
