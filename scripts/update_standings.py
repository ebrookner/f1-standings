#!/usr/bin/env python3
"""
Fetch live F1 driver-standings from the Jolpica F1 API (Ergast-compatible
successor) and regenerate data.json for the standings chart.

The chart (index.html) fetches ./data.json at load time. A scheduled GitHub
Action runs this script weekly and commits data.json only if it changed, which
auto-redeploys the GitHub Pages site.

Design notes:
  * Jolpica is a small, volunteer-run free service. We pace requests with a
    short delay and keep the total number of calls minimal.
  * The per-round `points` field in driverstandings is the official cumulative
    total *after that round*, already combining race + sprint points, so there
    is no need to add sprint results separately.
  * The top-6 set is recomputed from live data every run (never hardcoded),
    because the 2026 leaderboard shifts as the season progresses.
  * On any HTTP error or unexpected response shape we fail loudly (non-zero
    exit) and do NOT write a partial data.json — the workflow must not commit
    bad data.
"""

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

BASE = "https://api.jolpi.ca/ergast/f1"
SEASONS = [2025, 2026]
TOP_N = 6
REQUEST_DELAY = 0.25  # seconds between API calls — be a courteous citizen
TIMEOUT = 30

# Round labels use the circuit's familiar short name (keyed by Jolpica/Ergast
# circuitId) rather than the country — so the two Italian races read "Imola" and
# "Monza", the three US races read "Miami" / "COTA" / "Las Vegas", etc. Any
# circuitId not listed here falls back to the locality (see label_for_race).
CIRCUIT_LABELS = {
    "albert_park": "Melbourne",
    "shanghai": "Shanghai",
    "suzuka": "Suzuka",
    "bahrain": "Bahrain",
    "jeddah": "Jeddah",
    "miami": "Miami",
    "imola": "Imola",
    "monaco": "Monaco",
    "catalunya": "Barcelona",
    "villeneuve": "Montreal",
    "red_bull_ring": "Red Bull Ring",
    "silverstone": "Silverstone",
    "spa": "Spa",
    "hungaroring": "Hungaroring",
    "zandvoort": "Zandvoort",
    "monza": "Monza",
    "madring": "Madrid",
    "baku": "Baku",
    "marina_bay": "Marina Bay",
    "americas": "COTA",
    "rodriguez": "Mexico City",
    "interlagos": "Interlagos",
    "vegas": "Las Vegas",
    "losail": "Lusail",
    "yas_marina": "Yas Marina",
}

# The API returns some drivers with a longer given name than the label used in
# the chart's curated color map (index.html). Normalize to the short display
# name so the legend/tooltip read cleanly and the curated color is applied.
DRIVER_NAME_OVERRIDES = {
    "Andrea Kimi Antonelli": "Kimi Antonelli",
}


class UpdateError(Exception):
    """Raised on any unrecoverable problem — triggers a loud, non-zero exit."""


def get_json(url):
    """GET a Jolpica endpoint and return parsed JSON, failing loudly on error."""
    try:
        resp = requests.get(url, timeout=TIMEOUT, headers={"User-Agent": "f1-standings-updater"})
    except requests.RequestException as exc:
        raise UpdateError(f"Request failed for {url}: {exc}") from exc
    if resp.status_code != 200:
        raise UpdateError(f"HTTP {resp.status_code} for {url}: {resp.text[:200]}")
    try:
        return resp.json()
    except ValueError as exc:
        raise UpdateError(f"Non-JSON response from {url}: {resp.text[:200]}") from exc


def label_for_race(race):
    """Familiar short circuit name for a race (e.g. 'Monza', 'Imola')."""
    try:
        circuit = race["Circuit"]
        circuit_id = circuit["circuitId"]
    except (KeyError, TypeError) as exc:
        raise UpdateError(f"Race missing Circuit.circuitId: {race}") from exc
    if circuit_id in CIRCUIT_LABELS:
        return CIRCUIT_LABELS[circuit_id]
    # Fallback for a circuit we haven't curated yet: use the locality.
    try:
        return circuit["Location"]["locality"]
    except (KeyError, TypeError) as exc:
        raise UpdateError(f"Race missing Circuit.Location.locality: {race}") from exc


def completed_rounds(season, today):
    """Return races with date <= today (UTC), sorted by round number."""
    data = get_json(f"{BASE}/{season}/races.json")
    try:
        races = data["MRData"]["RaceTable"]["Races"]
    except (KeyError, TypeError) as exc:
        raise UpdateError(f"Unexpected races.json shape for {season}: {exc}") from exc

    done = []
    for race in races:
        date_str = race.get("date")
        if not date_str:
            raise UpdateError(f"Race missing date in {season}: {race}")
        try:
            race_date = datetime.strptime(date_str, "%Y-%m-%d").date()
        except ValueError as exc:
            raise UpdateError(f"Bad date '{date_str}' in {season}: {exc}") from exc
        if race_date <= today:
            done.append(race)

    done.sort(key=lambda r: int(r["round"]))
    return done


def standings_after_round(season, rnd):
    """Return {driver_full_name: cumulative_points_int} after the given round."""
    data = get_json(f"{BASE}/{season}/{rnd}/driverstandings.json")
    try:
        lists = data["MRData"]["StandingsTable"]["StandingsLists"]
    except (KeyError, TypeError) as exc:
        raise UpdateError(f"Unexpected driverstandings shape {season}/{rnd}: {exc}") from exc
    if not lists:
        raise UpdateError(f"No StandingsLists for {season}/{rnd}")

    out = {}
    for entry in lists[0].get("DriverStandings", []):
        driver = entry.get("Driver", {})
        name = f"{driver.get('givenName', '').strip()} {driver.get('familyName', '').strip()}".strip()
        if not name:
            raise UpdateError(f"Standings entry missing driver name in {season}/{rnd}")
        name = DRIVER_NAME_OVERRIDES.get(name, name)
        try:
            points = int(round(float(entry["points"])))
        except (KeyError, ValueError, TypeError) as exc:
            raise UpdateError(f"Bad points for {name} in {season}/{rnd}: {exc}") from exc
        out[name] = points
    if not out:
        raise UpdateError(f"Empty driver standings for {season}/{rnd}")
    return out


def build_season(season, today):
    """
    Return (labels, series, final_standings) for a season.

    labels: list of round labels (short country names), in round order
    series: {driver_name: [cumulative pts after each round]} for the top-6
            drivers as of the latest completed round
    final_standings: [(name, points), ...] descending, all drivers, latest round
    """
    races = completed_rounds(season, today)
    if not races:
        # Season not started yet (no race dates <= today).
        return [], {}, []

    labels = []
    per_round = []  # list of {name: points} dicts, one per completed round
    for race in races:
        rnd = race["round"]
        labels.append(label_for_race(race))
        per_round.append(standings_after_round(season, rnd))
        time.sleep(REQUEST_DELAY)

    # Top-6 determined by the LATEST completed round's standings.
    latest = per_round[-1]
    final_sorted = sorted(latest.items(), key=lambda kv: kv[1], reverse=True)
    top6 = [name for name, _ in final_sorted[:TOP_N]]

    # Cumulative points per round for each of the top-6 drivers. If a driver is
    # somehow absent from an earlier round's standings, treat it as 0.
    series = {name: [rnd_map.get(name, 0) for rnd_map in per_round] for name in top6}

    return labels, series, final_sorted


def main():
    today = datetime.now(timezone.utc).date()
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    labels25, series25, final25 = build_season(2025, today)
    labels26, series26, final26 = build_season(2026, today)

    if not series25:
        raise UpdateError("2025 season produced no data — refusing to write data.json")

    # meta strings
    champ_name, champ_pts = final25[0]
    meta25 = f"Champion: {champ_name}, {champ_pts} pts"

    if series26 and labels26:
        leader_name, leader_pts = final26[0]
        meta26 = f"Leader through {labels26[-1]}: {leader_name}, {leader_pts} pts"
    else:
        meta26 = "Season not yet started"

    data = {
        "rounds25": labels25,
        "series25": series25,
        "rounds26": labels26,
        "series26": series26,
        "meta25": meta25,
        "meta26": meta26,
        "total_rounds_2026": 24,
        "last_updated": now_iso,
    }

    out_path = Path(__file__).resolve().parent.parent / "data.json"
    out_path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"Wrote {out_path}")
    print(f"  2025: {len(labels25)} rounds, top-6 {list(series25.keys())}")
    print(f"  2026: {len(labels26)} rounds, top-6 {list(series26.keys())}")


if __name__ == "__main__":
    try:
        main()
    except UpdateError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
