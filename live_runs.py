"""
LIVE RUN WATCHER - pings Telegram when a team goes on a scoring run, so you can FADE it on the live spread.
  Euroleague: official live play-by-play (free, every basket with its clock time)
  ACB:        The Odds API live scores (only if the feed covers ACB - see --test), ~1 unit per check

Runs on GitHub (live.yml) during game windows. On your laptop:
  python live_runs.py   (or F5 in IDLE) checks the feeds: today's games, a replay of an old game, ACB on the odds feed
  python live_runs.py euroleague      watch tonight's Euroleague games (prints + Telegram)
"""
import os, sys, time, json, requests
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

# ------------------------------------------------------------------ settings
RUNS = [(10, 0), (11, 1), (12, 2), (13, 3), (14, 4), (15, 5)]   # (runner pts, opponent pts) - any one triggers ONE ping
LATEST_ELAPSED = 1500                   # only ping up to Q3 5:00 left (25 of 40 min played) - first half / early Q3
ACB_LATEST_WALL_MIN = 75                # ACB feed has no game clock: ~75 min after tip-off is roughly early Q3
POLL_SECONDS = 10                       # Euroleague feed (free)
ODDS_POLL_SECONDS = 45                  # ACB via The Odds API (costs 1 unit per check)
MAX_HOURS = 5.6                         # GitHub stops jobs at 6h
ACB_ODDS_KEY = "basketball_spain_acb"   # sport key on the odds feed - --test confirms the real one
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

# ------------------------------------------------------------------ ACB (via The Odds API live scores)
OH = {"X-RapidAPI-Key": ODDS_API_KEY or "", "X-RapidAPI-Host": "odds.p.rapidapi.com"}

def watch_acb(league="ACB"):
    start = time.time(); end = start + MAX_HOURS * 3600
    det, names, done, seen_live = {}, {}, set(), False
    while time.time() < end:
        r = requests.get(f"https://odds.p.rapidapi.com/v4/sports/{ACB_ODDS_KEY}/scores", headers=OH, timeout=30)
        if r.status_code != 200:
            print(f"ACB scores [{r.status_code}] {r.text[:150]}"); return
        live = 0
        for g in r.json() or []:
            ko = datetime.fromisoformat(g["commence_time"].replace("Z", "+00:00"))
            if ko > datetime.now(timezone.utc) or g["id"] in done: continue
            sc = {s["name"]: int(s["score"]) for s in (g.get("scores") or []) if s.get("score") not in (None, "")}
            h, a = g["home_team"], g["away_team"]
            if g.get("completed") or (datetime.now(timezone.utc) - ko).total_seconds() > ACB_LATEST_WALL_MIN * 60:
                done.add(g["id"]); continue
            if h not in sc or a not in sc: continue
            live += 1
            el = (datetime.now(timezone.utc) - ko).total_seconds() * 2400 / 6600   # rough game clock from wall time
            d = det.setdefault(g["id"], Detector()); names[g["id"]] = (h, a)
            if not d.ev or (sc[h], sc[a]) != tuple(d.ev[-1][1:]):
                for run in d.update(d.ev + [(el, sc[h], sc[a])]):
                    if (datetime.now(timezone.utc) - ko).total_seconds() <= ACB_LATEST_WALL_MIN * 60:
                        ping(league, h, a, run, 600)
        seen_live = seen_live or live > 0
        if seen_live and live == 0:
            print("No ACB games live any more."); return
        if not seen_live and time.time() - start > 3 * 3600:
            print("No ACB games went live."); return
        time.sleep(ODDS_POLL_SECONDS)

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
    print("\n3) Basketball leagues on your odds feed (free call, no quota)")
    try:
        r = requests.get("https://odds.p.rapidapi.com/v4/sports", headers=OH, params={"all": "true"}, timeout=30)
        for s in r.json():
            if s["key"].startswith("basketball"): print(f"   {s['key']:32s} {s['title']}  active={s['active']}")
    except Exception as e:
        print("   FAILED:", e)

if __name__ == "__main__":
    if len(sys.argv) == 1: sys.argv.append("--test")      # plain F5 in IDLE = test mode
    if "--test" in sys.argv: test()
    elif len(sys.argv) > 1 and sys.argv[1] == "acb": watch_acb()
    else: watch_euroleague()
    if "--test" in sys.argv:
        input("\nPaste everything back to Claude. Press Enter to close...")
