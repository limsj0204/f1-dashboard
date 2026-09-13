"""
Auto-updates the pure numeric/structural data in index.html (standings, lap-by-lap
replay data, qualifying results, grid positions, calendar bookkeeping) from
Jolpica/Ergast + FastF1, whenever a new session has finished.

Deliberately does NOT touch editorial content: NEWS_ITEMS, NEWS_UPDATED_AT, or
NEXT_RACE_PREVIEW (circuit facts / Pirelli notes / diagram). Those need a web
search + human judgment and stay on the manual "ask Claude" path.

Safe to run repeatedly (idempotent) - skips anything already embedded, and skips
anything whose upstream data isn't published yet rather than failing loudly. A
failure on one round/session is logged and skipped rather than aborting the run.
"""
import json
import os
import re
import time
import urllib.request
from datetime import datetime, timezone

SEASON = 2026
MIN_ROUND = 12  # first round we have detailed replay data for - never backfill earlier rounds
REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INDEX_PATH = os.path.join(REPO_DIR, "index.html")
CACHE_DIR = os.path.join(REPO_DIR, ".ff1_cache")

ERGAST_BASE = "https://api.jolpi.ca/ergast/f1"
UA = "Mozilla/5.0"

SESSION_FIELD_MAP = [
    ("FirstPractice", "fp1", "FP1"),
    ("SecondPractice", "fp2", "FP2"),
    ("ThirdPractice", "fp3", "FP3"),
]


# ---------- HTTP ----------

def http_get_json(url, tries=3, timeout=20):
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            if attempt == tries - 1:
                print(f"  ! request failed: {url} ({e})")
                return None
            time.sleep(3)
    return None


# ---------- const block extraction/replacement (brace-matching, not fragile regex) ----------

def find_const_span(html, name):
    marker = f"const {name} = "
    idx = html.find(marker)
    if idx == -1:
        return None
    start = idx + len(marker)
    opener = html[start]
    if opener not in "{[":
        # numeric/simple literal const, e.g. `const COMPLETED_ROUNDS = 13;`
        end = html.index(";", start)
        return start, end
    closer = "}" if opener == "{" else "]"
    depth = 0
    in_str = False
    str_char = ""
    escape = False
    i = start
    while i < len(html):
        c = html[i]
        if in_str:
            if escape:
                escape = False
            elif c == "\\":
                escape = True
            elif c == str_char:
                in_str = False
        else:
            if c == '"' or c == "'":
                in_str = True
                str_char = c
            elif c == opener:
                depth += 1
            elif c == closer:
                depth -= 1
                if depth == 0:
                    return start, i + 1
        i += 1
    raise ValueError(f"unbalanced braces for const {name}")


_BARE_KEY_RE = re.compile(r'([{,]\s*)([A-Za-z_$][A-Za-z0-9_$]*|\d+)(\s*:)')


def _js_object_literal_to_json(text):
    """Our consts are hand-written JS object literals with unquoted keys
    (e.g. `{pos:1,code:"ANT"}`) and occasional whole-line `//` comments,
    not strict JSON. Strip full-line comments, then quote bare keys so
    json.loads can parse it; leaves already-quoted keys and string
    values untouched (the regex only matches right after `{`/`,`)."""
    lines = [ln for ln in text.split("\n") if not ln.strip().startswith("//")]
    text = "\n".join(lines)
    return _BARE_KEY_RE.sub(lambda m: f'{m.group(1)}"{m.group(2)}"{m.group(3)}', text)


def get_const(html, name):
    span = find_const_span(html, name)
    if span is None:
        return None
    start, end = span
    text = html[start:end]
    if text and text[0] in "{[":
        text = _js_object_literal_to_json(text)
    return json.loads(text)


def set_const(html, name, value):
    span = find_const_span(html, name)
    if span is None:
        raise ValueError(f"const {name} not found")
    start, end = span
    new_text = json.dumps(value, separators=(",", ":"), ensure_ascii=False) if not isinstance(value, (int, float)) else str(value)
    return html[:start] + new_text + html[end:]


# ---------- time helpers ----------

def parse_iso(date_str, time_str):
    return datetime.fromisoformat(f"{date_str}T{time_str.replace('Z', '+00:00')}")


# ---------- FastF1 ----------

def ff1_fetch(year, rnd, session_type):
    import fastf1
    os.makedirs(CACHE_DIR, exist_ok=True)
    fastf1.Cache.enable_cache(CACHE_DIR)
    try:
        session = fastf1.get_session(year, rnd, session_type)
        session.load(laps=True, telemetry=False, weather=False, messages=False)
        laps = session.laps
    except Exception as e:
        print(f"  ! FastF1 data not ready for R{rnd} {session_type}: {e}")
        return None
    if laps is None or len(laps) == 0:
        return None

    def is_sc(ts):
        if ts is None or (isinstance(ts, float)):
            return 0
        return 1 if any(c in str(ts) for c in ("4", "6", "7")) else 0

    drivers = {}
    for abbr, grp in laps.groupby("Driver"):
        grp = grp.sort_values("LapNumber")
        team = grp["Team"].iloc[0] if "Team" in grp.columns else None
        rows = []
        for _, r in grp.iterrows():
            lt = r["LapTime"]
            row = [
                int(r["LapNumber"]) if r["LapNumber"] == r["LapNumber"] else None,
                round(lt.total_seconds(), 3) if lt == lt else None,
                (r["Compound"] or "UNKNOWN")[0] if isinstance(r["Compound"], str) else "U",
                int(r["Stint"]) if r["Stint"] == r["Stint"] else None,
                int(r["TyreLife"]) if r["TyreLife"] == r["TyreLife"] else None,
                1 if r["PitInTime"] == r["PitInTime"] else 0,
                is_sc(r.get("TrackStatus")),
                0 if r.get("FreshTyre") is False else 1,
            ]
            if session_type == "R":
                row.append(int(r["Position"]) if r["Position"] == r["Position"] else None)
            rows.append(row)
        drivers[abbr] = {"team": team, "laps": rows}

    if len(drivers) == 0:
        return None
    return {"year": year, "round": rnd, "raceName": session.event["EventName"], "drivers": drivers}


# ---------- Ergast ----------

def ergast_qualifying(rnd):
    d = http_get_json(f"{ERGAST_BASE}/{SEASON}/{rnd}/qualifying/?limit=30")
    if not d:
        return None
    races = d["MRData"]["RaceTable"]["Races"]
    if not races:
        return None
    rows = []
    for r in races[0]["QualifyingResults"]:
        rows.append([int(r["position"]), r["Driver"]["code"], r.get("Q1", ""), r.get("Q2", ""), r.get("Q3", "")])
    return rows


def ergast_results(rnd):
    d = http_get_json(f"{ERGAST_BASE}/{SEASON}/{rnd}/results/?limit=30")
    if not d:
        return None
    races = d["MRData"]["RaceTable"]["Races"]
    if not races:
        return None
    return races[0]["Results"]


def ergast_driver_standings_current():
    d = http_get_json(f"{ERGAST_BASE}/current/driverstandings/?limit=30")
    if not d:
        return None
    lst = d["MRData"]["StandingsTable"]["StandingsLists"]
    if not lst:
        return None
    return lst[0]["DriverStandings"]


def ergast_constructor_standings_current():
    d = http_get_json(f"{ERGAST_BASE}/current/constructorstandings/?limit=15")
    if not d:
        return None
    lst = d["MRData"]["StandingsTable"]["StandingsLists"]
    if not lst:
        return None
    return lst[0]["ConstructorStandings"]


def ergast_driver_standings_after_round(rnd):
    d = http_get_json(f"{ERGAST_BASE}/{SEASON}/{rnd}/driverstandings/?limit=30")
    if not d:
        return None
    lst = d["MRData"]["StandingsTable"]["StandingsLists"]
    if not lst:
        return None
    return {e["Driver"]["code"]: int(e["points"]) for e in lst[0]["DriverStandings"]}


def ergast_schedule():
    d = http_get_json(f"{ERGAST_BASE}/{SEASON}.json?limit=40")
    if not d:
        return []
    return d["MRData"]["RaceTable"]["Races"]


def short_gp_name(race_name):
    return race_name.replace("Grand Prix", "GP").strip()


# ---------- main orchestration ----------

def main():
    with open(INDEX_PATH, encoding="utf-8") as f:
        html = f.read()

    state = dict(
        race_meta=get_const(html, "RACE_META"),
        lap_races=get_const(html, "LAP_RACES"),
        quali_results=get_const(html, "QUALI_RESULTS"),
        grid_positions=get_const(html, "GRID_POSITIONS"),
        gp_names=get_const(html, "GP_NAMES"),
        driver_standings=get_const(html, "driverStandings"),
        team_standings=get_const(html, "teamStandings"),
        points_progress=get_const(html, "POINTS_PROGRESS"),
        completed_races=get_const(html, "completedRaces"),
        upcoming_races=get_const(html, "upcomingRaces"),
        next_race_sessions=get_const(html, "nextRaceSessions"),
        completed_rounds=get_const(html, "COMPLETED_ROUNDS"),
        last_race_result=get_const(html, "lastRaceResult"),
        last_race_fastest_lap=get_const(html, "lastRaceFastestLap"),
    )

    schedule = ergast_schedule()
    if not schedule:
        print("Could not fetch season schedule; aborting.")
        return

    now = datetime.now(timezone.utc)
    changed = False

    for race in schedule:
        rnd = int(race["round"])
        if rnd < MIN_ROUND:
            continue  # never backfill rounds before we started tracking replay data
        try:
            if process_round(race, rnd, now, state):
                changed = True
        except Exception as e:
            print(f"  ! unexpected error processing R{rnd}, skipping this round: {e}")

    if not changed:
        print("Nothing new.")
        return

    # rebuild nextRaceSessions from the new first upcoming race
    upcoming_races = state["upcoming_races"]
    if upcoming_races:
        nxt = next((r for r in schedule if int(r["round"]) == upcoming_races[0][0]), None)
        if nxt:
            def sess_iso(field):
                return f"{nxt[field]['date']}T{nxt[field]['time']}"
            next_race_sessions = state["next_race_sessions"]
            next_race_sessions.clear()
            if "FirstPractice" in nxt:
                next_race_sessions["1차 연습"] = sess_iso("FirstPractice")
            if "SecondPractice" in nxt:
                next_race_sessions["2차 연습"] = sess_iso("SecondPractice")
            if "ThirdPractice" in nxt:
                next_race_sessions["3차 연습"] = sess_iso("ThirdPractice")
            if "Qualifying" in nxt:
                next_race_sessions["예선"] = sess_iso("Qualifying")
            next_race_sessions["결승"] = f"{nxt['date']}T{nxt['time']}"

    name_map = {
        "RACE_META": "race_meta", "LAP_RACES": "lap_races", "QUALI_RESULTS": "quali_results",
        "GRID_POSITIONS": "grid_positions", "GP_NAMES": "gp_names", "driverStandings": "driver_standings",
        "teamStandings": "team_standings", "POINTS_PROGRESS": "points_progress",
        "completedRaces": "completed_races", "upcomingRaces": "upcoming_races",
        "nextRaceSessions": "next_race_sessions", "COMPLETED_ROUNDS": "completed_rounds",
        "lastRaceResult": "last_race_result", "lastRaceFastestLap": "last_race_fastest_lap",
    }
    for const_name, key in name_map.items():
        html = set_const(html, const_name, state[key])

    with open(INDEX_PATH, "w", encoding="utf-8") as f:
        f.write(html)
    print("index.html updated.")


def process_round(race, rnd, now, state):
    """Fetch and merge anything new for one GP weekend. Returns True if
    anything in `state` changed. Raises on unexpected failures - the caller
    catches per-round so one bad round doesn't abort the whole run."""
    race_meta = state["race_meta"]
    lap_races = state["lap_races"]
    quali_results = state["quali_results"]
    grid_positions = state["grid_positions"]
    gp_names = state["gp_names"]
    driver_standings = state["driver_standings"]
    team_standings = state["team_standings"]
    points_progress = state["points_progress"]
    completed_races = state["completed_races"]
    upcoming_races = state["upcoming_races"]
    last_race_result = state["last_race_result"]
    last_race_fastest_lap = state["last_race_fastest_lap"]

    race_dt = parse_iso(race["date"], race["time"])
    changed = False

    def ensure_gp_name():
        if rnd not in gp_names:
            gp_names[rnd] = short_gp_name(race["raceName"]) + f" ({race['Circuit']['Location']['locality']})"

    # -- practice sessions --
    for field, suffix, label in SESSION_FIELD_MAP:
        if field not in race:
            continue
        sess_dt = parse_iso(race[field]["date"], race[field]["time"])
        if sess_dt > now:
            continue
        race_key = f"r{rnd}{suffix}"
        if race_key in race_meta:
            continue  # already have it
        print(f"Fetching R{rnd} {label} ...")
        data = ff1_fetch(2026, rnd, label)
        if not data:
            print(f"  (no data yet for R{rnd} {label}, will retry later)")
            continue
        order = sorted(
            data["drivers"].items(),
            key=lambda kv: min([l[1] for l in kv[1]["laps"] if l[1] is not None], default=9999),
        )
        finish_order = [code for code, _ in order]
        lap_races[race_key] = data
        race_meta[race_key] = {
            "round": rnd, "session": label, "type": "practice",
            "finishOrder": finish_order, "defaultOn": finish_order[:4],
        }
        ensure_gp_name()
        changed = True
        print(f"  OK - {len(finish_order)} drivers")

    # -- qualifying --
    quali_dt = parse_iso(race["Qualifying"]["date"], race["Qualifying"]["time"]) if "Qualifying" in race else None
    race_key = f"r{rnd}"

    if quali_dt and quali_dt <= now and race_key not in race_meta:
        print(f"Fetching R{rnd} qualifying ...")
        rows = ergast_qualifying(rnd)
        if rows:
            quali_results[race_key] = rows
            finish_order = [r[1] for r in rows]
            race_meta[race_key] = {
                "round": rnd, "session": "레이스", "type": "race", "qualiOnly": True,
                "finishOrder": finish_order, "defaultOn": [],
            }
            lap_races[race_key] = {"year": 2026, "round": rnd, "raceName": race["raceName"], "drivers": {}}
            ensure_gp_name()
            changed = True
            print(f"  OK - pole: {rows[0][1]}")
        else:
            print("  (qualifying not published yet)")

    # -- race results --
    is_qualionly = race_meta.get(race_key, {}).get("qualiOnly")
    if race_dt <= now and (race_key not in race_meta or is_qualionly):
        print(f"Fetching R{rnd} race results ...")
        results = ergast_results(rnd)
        if results:
            results_sorted = sorted(results, key=lambda r: int(r["position"]))
            finish_order = [r["Driver"]["code"] for r in results_sorted]
            grid_positions[race_key] = {
                r["Driver"]["code"]: int(r["grid"]) for r in results if r.get("grid") not in (None, "0")
            }
            race_meta[race_key] = {
                "round": rnd, "session": "레이스", "type": "race",
                "finishOrder": finish_order, "defaultOn": finish_order[:3],
            }

            top5 = results_sorted[:5]
            new_last_result = []
            for r in top5:
                time_field = (r.get("Time") or {}).get("time")
                gap = "우승" if r["position"] == "1" else (time_field or "–")
                new_last_result.append({
                    "pos": int(r["position"]), "code": r["Driver"]["code"],
                    "name": f'{r["Driver"]["givenName"]} {r["Driver"]["familyName"]}',
                    "team": r["Constructor"]["constructorId"], "timeOrGap": gap,
                })
            last_race_result[:] = new_last_result

            fl_candidates = [r for r in results if (r.get("FastestLap") or {}).get("rank") == "1"]
            if fl_candidates:
                fl = fl_candidates[0]
                last_race_fastest_lap.clear()
                last_race_fastest_lap.update({
                    "code": fl["Driver"]["code"], "name": f'{fl["Driver"]["givenName"]} {fl["Driver"]["familyName"]}',
                    "time": fl["FastestLap"]["Time"]["time"], "lap": int(fl["FastestLap"]["lap"]),
                })

            winner = results_sorted[0]
            pole_code = quali_results.get(race_key, [[None, finish_order[0]]])[0][1]
            pole_row = next((r for r in results if r["Driver"]["code"] == pole_code), winner)
            completed_races[:] = [cr for cr in completed_races if cr[0] != rnd]
            completed_races.append([
                rnd, short_gp_name(race["raceName"]), race["Circuit"]["Location"]["locality"],
                race["Circuit"]["Location"]["country"], race["date"],
                winner["Driver"]["code"], f'{winner["Driver"]["givenName"]} {winner["Driver"]["familyName"]}',
                winner["Constructor"]["constructorId"], pole_code,
                f'{pole_row["Driver"]["givenName"]} {pole_row["Driver"]["familyName"]}',
            ])
            completed_races.sort(key=lambda r: r[0])
            upcoming_races[:] = [ur for ur in upcoming_races if ur[0] != rnd]
            state["completed_rounds"] = max(state["completed_rounds"], rnd)

            ds = ergast_driver_standings_current()
            if ds:
                driver_standings[:] = [{
                    "pos": int(e["position"]), "code": e["Driver"]["code"],
                    "name": f'{e["Driver"]["givenName"]} {e["Driver"]["familyName"]}',
                    "team": e["Constructors"][-1]["constructorId"],
                    "points": int(e["points"]), "wins": int(e["wins"]),
                } for e in ds]
            cs = ergast_constructor_standings_current()
            if cs:
                team_standings[:] = [{
                    "pos": int(e["position"]), "team": e["Constructor"]["constructorId"],
                    "points": int(e["points"]), "wins": int(e["wins"]),
                } for e in cs]
            round_totals = ergast_driver_standings_after_round(rnd)
            if round_totals and rnd not in points_progress["rounds"]:
                points_progress["rounds"].append(rnd)
                for code, arr in points_progress["points"].items():
                    arr.append(round_totals.get(code, arr[-1]))

            ensure_gp_name()
            changed = True
            print(f"  OK - winner: {winner['Driver']['code']}")
        else:
            print("  (race results not published yet)")

    # -- replace race-lap placeholder with real FastF1 data once available --
    if race_key in lap_races and not lap_races[race_key]["drivers"]:
        print(f"Fetching R{rnd} race lap data ...")
        data = ff1_fetch(2026, rnd, "R")
        if data:
            lap_races[race_key] = data
            changed = True
            print(f"  OK - {len(data['drivers'])} drivers")
        else:
            print("  (race lap data not ready yet)")

    return changed


if __name__ == "__main__":
    main()
