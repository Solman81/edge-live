"""
LIVE RUN WATCHER - pings Telegram when a team goes on a scoring run, so you can FADE it on the live spread.
  Euroleague: official live play-by-play (free, every basket with its clock time)
  ACB:        official acb.com live shot data (free; needs the site key - Supabase setting acb_apikey)

Runs on GitHub (live.yml) during game windows. On your laptop:
  python live_runs.py   (or F5 in IDLE) checks the feeds: today's games, a replay of an old game, ACB on the odds feed
  python live_runs.py euroleague      watch tonight's Euroleague games (prints + Telegram)
"""
import os, sys, time, json, requests
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

# ------------------------------------------------------------------ settings
RUNS = [(12, 0)]                       # 12 unanswered points only (changed 10 Nov)
LATEST_ELAPSED = 1500                   # only ping up to Q3 5:00 left (25 of 40 min played) - first half / early Q3
POLL_SECONDS = 10                       # Euroleague feed (free)
MAX_HOURS = 5.8                         # GitHub stops jobs at 6h
try:
    MAD = ZoneInfo("Europe/Madrid")
except Exception:                       # Windows without tzdata: use the computer's own clock (Spain)
    MAD = datetime.now().astimezone().tzinfo
UA = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}

def cfg(name):
    v = os.environ.get(name)
    if v: return v
    try:
        import edge_config; return getattr(edge_config, name, None)
    except ImportError:
        return None

TG_TOKEN = cfg("TELEGRAM_TOKEN"); ODDS_API_KEY = cfg("ODDS_API_KEY")
SB_URL = cfg("SUPABASE_URL"); SB_SECRET = cfg("SUPABASE_SECRET")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
if not CHAT_ID and SB_URL and SB_SECRET:
    try:
        r = requests.get(f"{SB_URL}/rest/v1/settings", headers={"apikey": SB_SECRET},
                         params={"select": "value", "key": "eq.telegram_chat_id"}, timeout=20)
        CHAT_ID = str(r.json()[0]["value"]).strip('"')
    except Exception as e:
        print("Couldn't read the Telegram chat id:", e)

def tg(text):
    print(text.replace("<b>", "").replace("</b>", ""), flush=True)
    if TG_TOKEN and CHAT_ID and "--test" not in sys.argv:
        try:
            requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                          json={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"}, timeout=20)
        except Exception as e:
            print("Telegram failed:", e)

# ------------------------------------------------------------------ run detector (same rule as the backtest)
class Detector:
    """Feed it score states (elapsed_seconds, home_pts, away_pts) in order. Returns new runs."""
    def __init__(self):
        self.ev = []; self.last = {0: -1, 1: -1}
    def update(self, states):
        new = []
        for st in states[len(self.ev):]:
            self.ev.append(st); j = len(self.ev) - 1
            if j == 0: continue
            hs, as_ = self.ev[j][1], self.ev[j][2]
            ph, pa = self.ev[j - 1][1], self.ev[j - 1][2]
            for side in (0, 1):
                my_now, op_now = (hs, as_) if side == 0 else (as_, hs)
                my_prev = ph if side == 0 else pa
                if my_now <= my_prev: continue                       # only when this team just scored
                for X, Y in RUNS:
                    hit = None
                    for i in range(j - 1, max(self.last[side], j - 60) - 1, -1):
                        my_i, op_i = (self.ev[i][1], self.ev[i][2]) if side == 0 else (self.ev[i][2], self.ev[i][1])
                        if op_now - op_i > Y: break
                        if my_now - my_i >= X: hit = i; break
                    if hit is not None:
                        my_i, op_i = (self.ev[hit][1], self.ev[hit][2]) if side == 0 else (self.ev[hit][2], self.ev[hit][1])
                        new.append({"side": side, "run": f"{my_now - my_i}-{op_now - op_i}",
                                    "secs": self.ev[j][0] - self.ev[min(hit + 1, j)][0], "at": self.ev[j]})
                        self.last[side] = j
                        break
        return new

def clock_txt(elapsed, qlen, nq=4, otlen=300):
    q = int(elapsed // qlen) + 1 if elapsed < nq * qlen else nq + 1 + int((elapsed - nq * qlen) // otlen)
    left = (q * qlen - elapsed) if q <= nq else (nq * qlen + (q - nq) * otlen - elapsed)
    return (f"Q{q}" if q <= nq else f"OT{q - nq}") + f" {int(left // 60)}:{int(left % 60):02d}"

def ping(league, home, away, r, qlen):
    runner, other = (home, away) if r["side"] == 0 else (away, home)
    t, hs, as_ = r["at"]
    m, s = divmod(int(r["secs"]), 60)
    tg(f"\U0001F3C0 <b>RUN {r['run']} {runner}</b> ({league}, took {m}:{s:02d})\n"
       f"{home} {hs:g} - {as_:g} {away} · {clock_txt(t, qlen)}\n"
       f"→ <b>FADE {runner}</b>: back {other} on the live spread (€1)")

# ------------------------------------------------------------------ EUROLEAGUE (official live feed)
def season_code(comp="E"):
    now = datetime.now(MAD); y = now.year if now.month >= 8 else now.year - 1
    return f"{comp}{y}"

def el_games_today(comp="E", day=None):
    """Today's games from the official schedule. Returns [(gamecode, home, away, start_text)]."""
    day = day or datetime.now(MAD).date()
    sc = season_code(comp)
    url = f"https://api-live.euroleague.net/v2/competitions/{comp}/seasons/{sc}/games"
    r = requests.get(url, headers=UA, timeout=30); r.raise_for_status()
    data = r.json(); items = data.get("data", data) if isinstance(data, dict) else data
    out = []
    for g in items:
        raw = g.get("utcDate") or g.get("date") or ""
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if dt.tzinfo is None: dt = dt.replace(tzinfo=MAD)        # schedule dates are local if no zone
            d = dt.astimezone(MAD)
        except ValueError:
            continue
        if d.date() != day: continue
        home = (g.get("local") or {}).get("club", {}).get("name") or "Home"
        away = (g.get("road") or {}).get("club", {}).get("name") or "Away"
        out.append((g.get("gameCode"), home, away, d.strftime("%H:%M")))
    return sorted(out, key=lambda x: x[3])

def el_states(comp, gamecode, season=None):
    """Score states from the live play-by-play: (elapsed_s, home, away). Plus finished flag."""
    r = requests.get("https://live.euroleague.net/api/PlaybyPlay", headers=UA, timeout=30,
                     params={"gamecode": gamecode, "seasoncode": season or season_code(comp)})
    if r.status_code != 200 or not r.text.strip(): return [], False
    data = r.json()
    states, last, done = [], (0, 0), False
    for qi, key in enumerate(["FirstQuarter", "SecondQuarter", "ThirdQuarter", "ForthQuarter", "ExtraTime"]):
        for p in data.get(key) or []:
            if (p.get("PLAYTYPE") or "").strip() == "EG": done = True
            a, b = p.get("POINTS_A"), p.get("POINTS_B")
            if a is None or b is None: continue
            sc = (int(a), int(b))
            if sc == last: continue
            mt = (p.get("MARKERTIME") or "").strip()
            try:
                mm, ss = mt.split(":")[:2]; left = int(mm) * 60 + int(float(ss))
            except ValueError:
                left = 0
            if qi < 4: el = qi * 600 + 600 - left
            else:      el = 2400 + 300 - left     # (all extra time lumped together - fine for alerts)
            states.append((el, sc[0], sc[1])); last = sc
    return states, done

def watch_euroleague(comp="E", league="Euroleague"):
    games = el_games_today(comp)
    if not games:
        print(f"No {league} games today."); return
    print(f"{league} today: " + ", ".join(f"{h} v {a} {t}" for _, h, a, t in games))
    det = {g[0]: Detector() for g in games}; done = set(); primed = set()
    end = time.time() + MAX_HOURS * 3600
    while time.time() < end and len(done) < len(games):
        for code, home, away, _ in games:
            if code in done: continue
            try:
                states, fin = el_states(comp, code)
            except Exception as e:
                print(f"  {home} v {away}: feed error {e}"); continue
            runs = det[code].update(states)
            if code not in primed:              # first look at a game already under way: learn the history, don't ping old runs
                primed.add(code)
                if runs: print(f"  {home} v {away}: joined at {states[-1][1]}-{states[-1][2]}, {len(runs)} earlier run(s) skipped")
                runs = [r for r in runs if r["at"] == states[-1]]   # ...unless the run ended on the very latest basket
            for r in runs:
                if r["at"][0] <= LATEST_ELAPSED: ping(league, home, away, r, 600)
            if states and states[-1][0] > LATEST_ELAPSED and not fin:
                done.add(code); print(f"  {home} v {away}: past the cut-off, stopped watching"); continue
            if fin:
                done.add(code); print(f"  final: {home} {states[-1][1]}-{states[-1][2]} {away}" if states else f"  final: {home} v {away}")
        time.sleep(POLL_SECONDS)

# ------------------------------------------------------------------ ACB (official acb.com live data)
ACB = "https://api2.acb.com/api/matchdata"
ACB_KEY_DEFAULT = "0dd94928-6f57-4c08-a3bd-b1b2f092976e"   # copied from live.acb.com - can be overridden in Supabase settings (acb_apikey)

def sb_setting(key, default=None):
    try:
        r = requests.get(f"{SB_URL}/rest/v1/settings", headers={"apikey": SB_SECRET},
                         params={"select": "value", "key": f"eq.{key}"}, timeout=20)
        v = r.json()[0]["value"]; return v if v not in (None, "") else default
    except Exception:
        return default

def sb_save(key, value):
    try:
        requests.post(f"{SB_URL}/rest/v1/settings", params={"on_conflict": "key"}, timeout=20,
                      headers={"apikey": SB_SECRET, "Content-Type": "application/json",
                               "Prefer": "resolution=merge-duplicates"}, json=[{"key": key, "value": value}])
    except Exception as e:
        print("couldn't save", key, e)

def acb_get(path, **params):
    h = {**UA, "x-apikey": str(sb_setting("acb_apikey", ACB_KEY_DEFAULT)).strip('"'),
         "Referer": "https://live.acb.com/", "Origin": "https://live.acb.com"}
    r = requests.get(f"{ACB}/{path}", headers=h, params=params, timeout=30)
    if r.status_code in (401, 403):
        tg("⚠️ ACB watcher: acb.com refused the key - grab a new x-apikey from live.acb.com (F12 → Network).")
        raise SystemExit(f"ACB key refused [{r.status_code}]")
    r.raise_for_status(); return r.json()

def acb_round_games(day=None):
    """Games of the round containing `day` (Spain time). Walks forward from the last known match id."""
    day = day or datetime.now(MAD).date()
    seed = int(sb_setting("acb_seed_match", 105432))
    for _ in range(8):
        ml = acb_get("Menu/matchlist", matchId=seed, format="round", delay=45)
        ms = ml.get("matches") or []
        if not ms: break
        def start(m): return datetime.fromisoformat((m.get("delayedStartDateTime") or m["startDateTime"]).replace("Z", "+00:00")).astimezone(MAD)
        last_day = max(start(m).date() for m in ms)
        if last_day >= day:
            sb_save("acb_seed_match", min(m["id"] for m in ms))
            return [(m["id"], m["homeTeam"]["shortName"], m["awayTeam"]["shortName"], start(m), m.get("matchStatus"))
                    for m in sorted(ms, key=start)]
        seed = max(m["id"] for m in ms) + 1           # next round
    return []

def acb_states(match_id):
    d = acb_get("MatchShots/match-shots", matchId=match_id)
    def el(s):
        left = s["minute"] * 60 + s["second"]
        return (s["quarter"] - 1) * 600 + 600 - left if s["quarter"] <= 4 else 2400 + (s["quarter"] - 5) * 300 + 300 - left
    states, last = [], (0, 0)
    for sh in sorted(d.get("shotPoints") or [], key=lambda s: (el(s), s["id"])):
        sc = (sh["scoreHome"], sh["scoreAway"])
        if sc != last: states.append((el(sh), sc[0], sc[1])); last = sc
    return states, bool(d.get("matchFinished"))

def watch_acb(window="00:00-23:59", league="ACB"):
    lo, hi = window.split("-")
    today = datetime.now(MAD).date()
    games = [g for g in acb_round_games(today) if g[3].date() == today and lo <= g[3].strftime("%H:%M") <= hi]
    if not games:
        print(f"No ACB games today between {lo} and {hi}."); return
    print(f"ACB today ({lo}-{hi}): " + ", ".join(f"{h} v {a} {t:%H:%M}" for _, h, a, t, _ in games))
    det = {g[0]: Detector() for g in games}; done, primed = set(), set()
    end = time.time() + MAX_HOURS * 3600
    while time.time() < end and len(done) < len(games):
        for mid, home, away, ko, _ in games:
            if mid in done or datetime.now(MAD) < ko - timedelta(minutes=2): continue
            try:
                states, fin = acb_states(mid)
            except SystemExit:
                raise
            except Exception as e:
                print(f"  {home} v {away}: feed error {e}"); continue
            runs = det[mid].update(states)
            if mid not in primed:
                primed.add(mid)
                if runs: print(f"  {home} v {away}: joined at {states[-1][1]}-{states[-1][2]}, {len(runs)} earlier run(s) skipped")
                runs = [r for r in runs if states and r["at"] == states[-1]]
            for r in runs:
                if r["at"][0] <= LATEST_ELAPSED: ping(league, home, away, r, 600)
            if fin or (states and states[-1][0] > LATEST_ELAPSED):
                done.add(mid); print(f"  {home} v {away}: " + ("final" if fin else "past the cut-off") + ", stopped watching")
        time.sleep(POLL_SECONDS)

# ------------------------------------------------------------------ laptop test
def test():
    print("1) Euroleague schedule for today and the next 7 days")
    try:
        for k in range(8):
            day = datetime.now(MAD).date() + timedelta(days=k)
            g = el_games_today("E", day)
            if g: print(f"   {day}: " + ", ".join(f"{h} v {a} {t} (game {c})" for c, h, a, t in g))
        print("   (season code used:", season_code("E") + ")")
    except Exception as e:
        print("   FAILED:", e)
        try:
            r = requests.get(f"https://api-live.euroleague.net/v2/competitions/E/seasons/{season_code('E')}/games", headers=UA, timeout=30)
            print("   raw:", r.status_code, r.text[:500])
        except Exception as e2: print("   raw failed:", e2)
    print("\n2) Replay of last season's game 1 through the run detector")
    try:
        prev = f"E{int(season_code('E')[1:]) - 1}"
        states, fin = el_states("E", 1, prev)
        print(f"   {len(states)} scoring moments, finished={fin}, final {states[-1][1]}-{states[-1][2]}" if states else "   no data")
        d = Detector()
        for r in d.update(states):
            if r["at"][0] <= LATEST_ELAPSED: ping("Euroleague replay", "HOME", "AWAY", r, 600)
            else: print(f"   (after the cut-off, no ping: {r['run']} at {clock_txt(r['at'][0], 600)})")
    except Exception as e:
        print("   FAILED:", e)
    print("\n3) ACB: this round's games from acb.com")
    try:
        for i, h, a, t, st in acb_round_games(): print(f"   {t:%a %d %b %H:%M}  {h} v {a}  ({st})")
    except BaseException as e:
        print("   FAILED:", e)

if __name__ == "__main__":
    if len(sys.argv) == 1: sys.argv.append("--test")      # plain F5 in IDLE = test mode
    if "--test" in sys.argv: test()
    elif len(sys.argv) > 1 and sys.argv[1] == "acb": watch_acb(sys.argv[2] if len(sys.argv) > 2 and sys.argv[2] else "00:00-23:59")
    elif len(sys.argv) > 1 and sys.argv[1] == "acbtest":
        g = acb_round_games(); print("ACB round:", *[f"  {t:%a %d %b %H:%M}  {h} v {a}  ({st}, id {i})" for i, h, a, t, st in g], sep="\n")
        st, fin = acb_states(105432); print(f"Replay Barca v Breogan: {len(st)} scores, finished={fin}, final {st[-1][1]}-{st[-1][2]}")
        input("\nPaste this to Claude. Press Enter to close...")
    else: watch_euroleague()
    if "--test" in sys.argv:
        input("\nPaste everything back to Claude. Press Enter to close...")
