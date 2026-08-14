# sports/mlb/data.py — MLB data layer.
#
# Two live sources, both keyless:
#
#   * MLB Stats API (statsapi.mlb.com) — schedule, probable pitchers, rosters,
#     per-game logs, and every season/split aggregate. This is the backbone.
#   * Baseball Savant — Statcast quality-of-contact pulls (via pybaseball) and
#     the Statcast park-factor leaderboard.
#
# Deliberately NOT used: pybaseball's Baseball-Reference scrapers (Cloudflare-
# blocked) and pybaseball's FanGraphs scrapers — team_batting/team_pitching/
# pitching_stats all hit leaders-legacy.aspx, which now returns 403, so
# bullpen and team-batting aggregates come from the Stats API instead.

import json
import re
import unicodedata
from datetime import datetime, timedelta

import pandas as pd
import requests
import statsapi
import streamlit as st
from pybaseball import statcast_batter

from sports.mlb.config import (
    SEASON, N_GAMES, MIN_GAMES_VS_TEAM, STATCAST_WINDOW_DAYS, PITCHER_PROP_TYPES,
)

MLB_API = "https://statsapi.mlb.com/api/v1"
SAVANT_PARK_FACTORS = (
    "https://baseballsavant.mlb.com/leaderboard/statcast-park-factors"
    "?type=year&year={year}&batSide=&stat=index_wOBA&condition=All"
    "&rolling=&sortColumn=index_wOBA&sortDirection=desc&csv=true"
)
_UA = {"User-Agent": "Mozilla/5.0"}


def _api(path, **params):
    r = requests.get(f"{MLB_API}/{path}", params=params, headers=_UA, timeout=20)
    r.raise_for_status()
    return r.json()


def _splits(payload):
    """Flattens the Stats API's stats[].splits[] nesting into one list."""
    out = []
    for group in payload.get("stats", []):
        out.extend(group.get("splits", []))
    return out


def _num(value, default=None):
    """Stats API returns numbers as strings, and misses as '-.--' / '.---'."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


# ── Players / teams lookup ───────────────────────────────────────────────────

def _normalize(name):
    """Lowercase and strip accent marks so 'Suarez' matches 'Suárez'."""
    return unicodedata.normalize("NFD", name).encode("ascii", "ignore").decode().lower()


@st.cache_data(ttl=86400)
def _all_teams():
    return _api("teams", sportId=1)["teams"]


def get_player_id(name):
    """Returns the MLBAM player ID for a name. Case and accent insensitive."""
    matches = statsapi.lookup_player(name)
    if not matches:
        return None
    norm = _normalize(name)
    for p in matches:
        if _normalize(p["fullName"]) == norm:
            return p["id"]
    return matches[0]["id"]


@st.cache_data(ttl=86400)
def get_player_info(player_id):
    """Returns handedness and position for a player."""
    people = _api("people", personIds=player_id).get("people", [])
    if not people:
        return None
    p = people[0]
    position = p.get("primaryPosition", {}) or {}
    return {
        "name":       p.get("fullName", ""),
        "bat_side":   (p.get("batSide") or {}).get("code", "R"),
        "pitch_hand": (p.get("pitchHand") or {}).get("code", "R"),
        "position":   position.get("abbreviation", ""),
        "is_pitcher": position.get("type") == "Pitcher",
    }


@st.cache_data(ttl=86400)
def get_player_hands(player_ids):
    """Batch handedness lookup — one call for a whole slate's probable pitchers."""
    if not player_ids:
        return {}
    people = _api("people", personIds=",".join(str(i) for i in player_ids)).get("people", [])
    return {
        p["id"]: {
            "name":       p.get("fullName", ""),
            "bat_side":   (p.get("batSide") or {}).get("code", "R"),
            "pitch_hand": (p.get("pitchHand") or {}).get("code", "R"),
        }
        for p in people
    }


def get_team_id(team_name):
    """Returns the MLB team ID for a full name, nickname, or abbreviation."""
    team_name = team_name.strip().lower()
    for t in _all_teams():
        if (t["name"].lower() == team_name or
                t["abbreviation"].lower() == team_name or
                t["teamName"].lower() == team_name or
                t.get("clubName", "").lower() == team_name):
            return t["id"]
    return None


def get_team_abbr(team_id):
    """Returns the abbreviation for a team ID."""
    for t in _all_teams():
        if t["id"] == team_id:
            return t["abbreviation"]
    return None


def build_team_lookup():
    """Maps abbreviation, nickname, and full name variations to abbreviation."""
    lookup = {}
    for t in _all_teams():
        lookup[t["abbreviation"].lower()] = t["abbreviation"]
        lookup[t["teamName"].lower()]     = t["abbreviation"]
        lookup[t["name"].lower()]         = t["abbreviation"]
    return lookup


# ── Game logs ─────────────────────────────────────────────────────────────────

_HITTING_FIELDS = [
    "hits", "homeRuns", "rbi", "runs", "totalBases", "doubles", "triples",
    "strikeOuts", "baseOnBalls", "atBats", "plateAppearances", "stolenBases",
]
_PITCHING_FIELDS = [
    "strikeOuts", "hits", "homeRuns", "baseOnBalls", "earnedRuns",
    "battersFaced", "outs", "gamesStarted",
]

_game_log_cache = {}


def stat_group(prop_type):
    return "pitching" if prop_type in PITCHER_PROP_TYPES else "hitting"


def seed_game_log_cache(player_id, logs, season=SEASON, group="hitting"):
    """
    Overrides the cache with provided logs (used in backtesting to inject only
    the games that had already happened at the time of the game being scored).
    The key must match what get_game_logs() reads or the seed is a no-op.
    """
    _game_log_cache[(player_id, season, group)] = logs


def _fetch_game_logs(player_id, season, group):
    payload = _api(
        f"people/{player_id}/stats",
        stats="gameLog", group=group, season=season,
    )
    fields = _PITCHING_FIELDS if group == "pitching" else _HITTING_FIELDS
    rows = []
    for split in _splits(payload):
        if split.get("gameType") != "R":
            continue
        stat = split.get("stat", {})
        row = {
            "game_date":   split.get("date"),
            "is_home":     bool(split.get("isHome")),
            "opponent_id": (split.get("opponent") or {}).get("id"),
            "team_id":     (split.get("team") or {}).get("id"),
        }
        for f in fields:
            row[f] = _num(stat.get(f), 0.0)
        if group == "pitching":
            row["inningsPitched"] = _num(stat.get("inningsPitched"), 0.0)
        rows.append(row)

    logs = pd.DataFrame(rows)
    if logs.empty:
        return logs
    logs["game_date"] = pd.to_datetime(logs["game_date"])
    return logs.sort_values("game_date").reset_index(drop=True)


def get_game_logs(player_id, n_games=N_GAMES, season=SEASON, group="hitting"):
    """
    Returns a DataFrame of the player's game logs for a season, oldest first.
    n_games trims to the most recent n. Cached per session.
    """
    key = (player_id, season, group)
    if key not in _game_log_cache:
        _game_log_cache[key] = _fetch_game_logs(player_id, season, group)
    logs = _game_log_cache[key]
    return logs.tail(n_games) if n_games and n_games < len(logs) else logs


def get_multi_season_logs(player_id, seasons, group="hitting"):
    """Pulls and concatenates game logs across multiple seasons, sorted by date."""
    frames = [_fetch_game_logs(player_id, s, group) for s in seasons]
    frames = [f for f in frames if not f.empty]
    if not frames:
        return None
    logs = pd.concat(frames, ignore_index=True)
    return logs.sort_values("game_date").reset_index(drop=True)


_PROP_COL_MAP = {
    "hits":               "hits",
    "total_bases":        "totalBases",
    "home_runs":          "homeRuns",
    "rbis":               "rbi",
    "runs":               "runs",
    "hits+runs+rbis":     None,
    "pitcher_strikeouts": "strikeOuts",
}


def _prop_column(logs, prop_type):
    col = _PROP_COL_MAP.get(prop_type)
    if col is None and prop_type == "hits+runs+rbis":
        logs = logs.copy()
        logs["HRR"] = logs["hits"] + logs["runs"] + logs["rbi"]
        col = "HRR"
    return logs, col


def extract_stat(row, prop_type):
    """Extracts the actual stat value from a single game log row."""
    if prop_type == "hits+runs+rbis":
        return row["hits"] + row["runs"] + row["rbi"]
    col = _PROP_COL_MAP.get(prop_type)
    return row[col] if col else None


def get_streak_stats(player_id, prop_type, line, n_games=N_GAMES, season=SEASON):
    """
    Returns how often the player has exceeded the given line in their last
    n_games for the given prop type.
    """
    logs = get_game_logs(player_id, n_games, season, stat_group(prop_type))
    if logs.empty:
        return None
    logs, col = _prop_column(logs, prop_type)
    if col not in logs.columns:
        return None

    values = logs[col]
    over = int((values > line).sum())
    total = len(values)
    if total == 0:
        return None

    return {
        "hit_rate":   over / total,
        "avg":        float(values.mean()),
        "over_count": over,
        "games":      total,
    }


def get_player_vs_team(player_id, team_id, prop_type, season=SEASON):
    """Returns how a player historically performs against a specific team."""
    logs = get_game_logs(player_id, None, season, stat_group(prop_type))
    if logs.empty:
        return {"games": 0, "avg": None, "reliable": False}

    vs_team = logs[logs["opponent_id"] == team_id]
    if vs_team.empty:
        return {"games": 0, "avg": None, "reliable": False}

    vs_team, col = _prop_column(vs_team, prop_type)
    games = len(vs_team)
    return {
        "games":    games,
        "avg":      round(float(vs_team[col].mean()), 2),
        "reliable": games >= MIN_GAMES_VS_TEAM,
    }


def get_recent_pa_per_game(player_id, n_games=N_GAMES, season=SEASON):
    """Trailing plate appearances per game — a lineup-spot proxy when no lineup is posted."""
    logs = get_game_logs(player_id, n_games, season, "hitting")
    if logs.empty:
        return None
    played = logs[logs["plateAppearances"] > 0]
    if played.empty:
        return None
    return round(float(played["plateAppearances"].mean()), 2)


# ── Handedness splits ─────────────────────────────────────────────────────────

def _hand_rates(stat):
    pa = _num(stat.get("plateAppearances"), 0.0) or 0.0
    return {
        "pa":       pa,
        "ops":      _num(stat.get("ops")),
        "hits_pa":  (_num(stat.get("hits"), 0.0) / pa) if pa else None,
        "tb_pa":    (_num(stat.get("totalBases"), 0.0) / pa) if pa else None,
        "hr_pa":    (_num(stat.get("homeRuns"), 0.0) / pa) if pa else None,
        "k_rate":   (_num(stat.get("strikeOuts"), 0.0) / pa) if pa else None,
    }


@st.cache_data(ttl=3600)
def get_batter_hand_splits(player_id, season=SEASON):
    """
    Returns a batter's production split by the pitcher's throwing hand:
    {"L": {...}, "R": {...}} keyed by pitcher hand, plus per-PA rates.
    """
    payload = _api(
        f"people/{player_id}/stats",
        stats="statSplits", group="hitting", season=season, sitCodes="vl,vr",
    )
    out = {}
    for split in _splits(payload):
        code = (split.get("split") or {}).get("code")
        if code in ("vl", "vr"):
            out["L" if code == "vl" else "R"] = _hand_rates(split.get("stat", {}))
    return out or None


@st.cache_data(ttl=3600)
def get_league_team_hand_splits(season=SEASON):
    """
    Every team's offensive production vs LHP and RHP, in one call.
    Returns {team_id: {"L": {...}, "R": {...}}} plus a "_league" mean OPS per hand.
    """
    payload = _api(
        "teams/stats",
        stats="statSplits", group="hitting", season=season,
        sportId=1, sitCodes="vl,vr",
    )
    out = {}
    for split in _splits(payload):
        team_id = (split.get("team") or {}).get("id")
        code = (split.get("split") or {}).get("code")
        if not team_id or code not in ("vl", "vr"):
            continue
        out.setdefault(team_id, {})["L" if code == "vl" else "R"] = _hand_rates(split.get("stat", {}))

    league = {}
    for hand in ("L", "R"):
        vals = [t[hand]["ops"] for t in out.values() if t.get(hand, {}).get("ops") is not None]
        league[hand] = sum(vals) / len(vals) if vals else None
    out["_league"] = league
    return out


# ── Opposing pitching: starters and bullpens ──────────────────────────────────

@st.cache_data(ttl=3600)
def get_pitcher_season_stats(pitcher_id, season=SEASON):
    """Season-to-date rate stats for a starting pitcher."""
    payload = _api(
        f"people/{pitcher_id}/stats",
        stats="season", group="pitching", season=season,
    )
    splits = _splits(payload)
    if not splits:
        return None
    stat = splits[0].get("stat", {})
    bf = _num(stat.get("battersFaced"), 0.0) or 0.0
    outs = _num(stat.get("outs"), 0.0) or 0.0
    starts = _num(stat.get("gamesStarted"), 0.0) or 0.0

    return {
        "era":        _num(stat.get("era")),
        "whip":       _num(stat.get("whip")),
        "opp_avg":    _num(stat.get("avg")),
        "k_rate":     (_num(stat.get("strikeOuts"), 0.0) / bf) if bf else None,
        "bb_rate":    (_num(stat.get("baseOnBalls"), 0.0) / bf) if bf else None,
        "hr_per_9":   (_num(stat.get("homeRuns"), 0.0) * 27 / outs) if outs else None,
        "ip_per_start": (outs / 3 / starts) if starts else None,
        "starts":     int(starts),
    }


@st.cache_data(ttl=3600)
def get_league_bullpens(season=SEASON):
    """
    Every team's relief-pitching-only line, in one call (sitCode 'rp'), with a
    league-relative rank. Rank 1 = best bullpen (lowest ERA).
    """
    payload = _api(
        "teams/stats",
        stats="statSplits", group="pitching", season=season,
        sportId=1, sitCodes="rp",
    )
    pens = {}
    for split in _splits(payload):
        team_id = (split.get("team") or {}).get("id")
        if not team_id:
            continue
        stat = split.get("stat", {})
        bf = _num(stat.get("battersFaced"), 0.0) or 0.0
        pens[team_id] = {
            "era":    _num(stat.get("era")),
            "whip":   _num(stat.get("whip")),
            "k_rate": (_num(stat.get("strikeOuts"), 0.0) / bf) if bf else None,
            "bf":     bf,
        }

    ranked = sorted((t for t in pens if pens[t]["era"] is not None),
                    key=lambda t: pens[t]["era"])
    for i, team_id in enumerate(ranked, 1):
        pens[team_id]["rank"] = i
    for team_id in pens:
        pens[team_id].setdefault("rank", None)
        pens[team_id]["total_teams"] = len(ranked)
    return pens


def get_bullpen_strength(team_id, season=SEASON):
    return get_league_bullpens(season).get(team_id)


@st.cache_data(ttl=3600)
def get_league_team_batting(season=SEASON):
    """
    Every team's offensive season line, in one call — used to judge how
    strikeout-prone an opposing lineup is for pitcher props.
    """
    payload = _api("teams/stats", stats="season", group="hitting",
                   season=season, sportId=1)
    teams = {}
    for split in _splits(payload):
        team_id = (split.get("team") or {}).get("id")
        if not team_id:
            continue
        stat = split.get("stat", {})
        pa = _num(stat.get("plateAppearances"), 0.0) or 0.0
        games = _num(stat.get("gamesPlayed"), 0.0) or 0.0
        teams[team_id] = {
            "k_rate":         (_num(stat.get("strikeOuts"), 0.0) / pa) if pa else None,
            "ops":            _num(stat.get("ops")),
            "runs_per_game":  (_num(stat.get("runs"), 0.0) / games) if games else None,
        }

    k_rates = [t["k_rate"] for t in teams.values() if t["k_rate"] is not None]
    ops_all = [t["ops"] for t in teams.values() if t["ops"] is not None]
    teams["_league"] = {
        "k_rate": sum(k_rates) / len(k_rates) if k_rates else None,
        "ops":    sum(ops_all) / len(ops_all) if ops_all else None,
    }
    return teams


def get_team_batting_profile(team_id, season=SEASON):
    return get_league_team_batting(season).get(team_id)


# ── Park factors ──────────────────────────────────────────────────────────────

@st.cache_data(ttl=86400)
def get_park_factors(year=SEASON):
    """
    Statcast park factors keyed by the venue's home team ID. Each index is
    centered on 100 (100 = league neutral, 113 = 13% more of that event).

    Savant serves this leaderboard as HTML with the rows embedded in a
    `var data = [...]` blob — the documented `csv=true` parameter no longer
    returns raw CSV. Falls back to the prior year early in a season, before
    the current year has accumulated rows.
    """
    for candidate in (year, year - 1):
        try:
            html = requests.get(SAVANT_PARK_FACTORS.format(year=candidate),
                                headers=_UA, timeout=30).text
        except Exception:
            continue
        match = re.search(r"var data = (\[.*?\]);", html, re.S)
        if not match:
            continue
        rows = json.loads(match.group(1))
        if not rows:
            continue
        return {
            int(r["main_team_id"]): {
                "venue":  r.get("venue_name", ""),
                "runs":   _num(r.get("index_runs"), 100.0),
                "hits":   _num(r.get("index_hits"), 100.0),
                "hr":     _num(r.get("index_hr"), 100.0),
                "so":     _num(r.get("index_so"), 100.0),
                "woba":   _num(r.get("index_woba"), 100.0),
                "year_range": r.get("year_range", ""),
            }
            for r in rows if r.get("main_team_id")
        }
    return {}


_PARK_INDEX_FOR_PROP = {
    "hits":               "hits",
    "total_bases":        "woba",
    "home_runs":          "hr",
    "rbis":               "runs",
    "runs":               "runs",
    "hits+runs+rbis":     "runs",
    "pitcher_strikeouts": "so",
}


def get_park_factor(venue_team_id, prop_type, year=SEASON):
    """Returns the park index relevant to a prop at the home team's stadium."""
    park = get_park_factors(year).get(venue_team_id)
    if not park:
        return None
    return {
        "index": park[_PARK_INDEX_FOR_PROP.get(prop_type, "runs")],
        "venue": park["venue"],
    }


# ── Statcast quality of contact ───────────────────────────────────────────────

@st.cache_data(ttl=3600)
def get_statcast_quality(player_id, pitcher_hand=None, end_date=None,
                         days=STATCAST_WINDOW_DAYS):
    """
    Trailing-window Statcast batted-ball quality, optionally restricted to one
    pitcher hand. Expected wOBA on contact and hard-hit rate lead raw results,
    so this catches a hot or cold batter before the box score does.
    """
    end = pd.to_datetime(end_date) if end_date else datetime.now()
    start = end - timedelta(days=days)
    try:
        df = statcast_batter(start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"), player_id)
    except Exception:
        return None
    if df is None or df.empty:
        return None

    if pitcher_hand:
        df = df[df["p_throws"] == pitcher_hand]
    contact = df[df["launch_speed"].notna()]
    if len(contact) < 15:
        return None

    xwoba = contact["estimated_woba_using_speedangle"].dropna()
    return {
        "xwoba_con":     round(float(xwoba.mean()), 3) if len(xwoba) else None,
        "hard_hit_rate": round(float((contact["launch_speed"] >= 95).mean()), 3),
        "batted_balls":  len(contact),
    }


# ── Schedule ──────────────────────────────────────────────────────────────────

@st.cache_data(ttl=3600)
def get_todays_games():
    """
    Today's MLB games with home/away teams, venue, and probable starters
    (including each starter's throwing hand).
    """
    today = datetime.now().strftime("%Y-%m-%d")
    try:
        payload = _api("schedule", sportId=1, date=today,
                       hydrate="probablePitcher,team,venue")
    except Exception as e:
        print(f"MLB schedule fetch error: {e}")
        return []

    raw = []
    for date in payload.get("dates", []):
        raw.extend(date.get("games", []))

    pitcher_ids = []
    for g in raw:
        for side in ("home", "away"):
            pid = (g["teams"][side].get("probablePitcher") or {}).get("id")
            if pid:
                pitcher_ids.append(pid)
    hands = get_player_hands(tuple(sorted(set(pitcher_ids))))

    abbrs = {t["id"]: t["abbreviation"] for t in _all_teams()}

    def _probable(side_data):
        pp = side_data.get("probablePitcher") or {}
        if not pp.get("id"):
            return None
        return {
            "id":   pp["id"],
            "name": pp.get("fullName", ""),
            "hand": hands.get(pp["id"], {}).get("pitch_hand", "R"),
        }

    games = []
    for g in raw:
        if g.get("gameType") != "R":
            continue
        home, away = g["teams"]["home"], g["teams"]["away"]
        home_id, away_id = home["team"]["id"], away["team"]["id"]
        games.append({
            "game_pk":        g.get("gamePk"),
            "home_team":      abbrs.get(home_id, ""),
            "away_team":      abbrs.get(away_id, ""),
            "home_team_id":   home_id,
            "away_team_id":   away_id,
            "venue":          (g.get("venue") or {}).get("name", ""),
            "home_probable":  _probable(home),
            "away_probable":  _probable(away),
        })
    return games


@st.cache_data(ttl=3600)
def get_team_rest_days():
    """
    Days since each team last played, keyed by abbreviation. In baseball this is
    almost always 1 (teams play nearly every day); 2+ means an off day.
    """
    today = datetime.now().date()
    try:
        payload = _api("schedule", sportId=1,
                       startDate=(today - timedelta(days=8)).strftime("%Y-%m-%d"),
                       endDate=(today - timedelta(days=1)).strftime("%Y-%m-%d"))
    except Exception:
        return {}

    abbrs = {t["id"]: t["abbreviation"] for t in _all_teams()}
    last_played = {}
    for date in payload.get("dates", []):
        day = datetime.strptime(date["date"], "%Y-%m-%d").date()
        for g in date.get("games", []):
            if g.get("status", {}).get("abstractGameState") != "Final":
                continue
            for side in ("home", "away"):
                abbr = abbrs.get(g["teams"][side]["team"]["id"])
                if abbr and (abbr not in last_played or day > last_played[abbr]):
                    last_played[abbr] = day
    return {abbr: (today - day).days for abbr, day in last_played.items()}


def get_pitcher_days_rest(pitcher_id, as_of=None, season=SEASON):
    """Days between a pitcher's last outing and the game being scored."""
    logs = get_game_logs(pitcher_id, None, season, "pitching")
    if logs.empty:
        return None
    as_of = pd.to_datetime(as_of) if as_of else pd.Timestamp(datetime.now().date())
    prior = logs[logs["game_date"] < as_of]
    if prior.empty:
        return None
    return int((as_of - prior["game_date"].iloc[-1]).days)


@st.cache_data(ttl=1800)
def get_todays_lineup_spots():
    """
    Batting-order slot for every player in a posted lineup today, keyed by
    player ID. Lineups only appear an hour or two before first pitch, so this
    is frequently empty or partial — callers fall back to trailing PA volume.
    """
    spots = {}
    for game in get_todays_games():
        try:
            box = _api(f"game/{game['game_pk']}/boxscore")
        except Exception:
            continue
        for side in ("home", "away"):
            for player in box.get("teams", {}).get(side, {}).get("players", {}).values():
                order = player.get("battingOrder")
                if order:
                    spots[player["person"]["id"]] = int(order) // 100
    return spots


@st.cache_data(ttl=86400)
def get_opposing_starters(team_id, season):
    """
    Maps each of a team's game dates in a season to the opposing starting
    pitcher, so a backtest can score the pitcher matchup a batter actually
    faced instead of falling back to neutral. Returns
    {date_string: {"id", "name", "hand", "is_home"}}.
    """
    try:
        payload = _api("schedule", sportId=1, teamId=team_id, season=season,
                       gameType="R", hydrate="probablePitcher")
    except Exception:
        return {}

    starters = {}
    pending = []
    for date in payload.get("dates", []):
        for g in date.get("games", []):
            home_id = g["teams"]["home"]["team"]["id"]
            side = "away" if home_id == team_id else "home"
            pp = (g["teams"][side].get("probablePitcher") or {})
            if not pp.get("id"):
                continue
            starters[date["date"]] = {
                "id":      pp["id"],
                "name":    pp.get("fullName", ""),
                "hand":    None,
                "is_home": home_id == team_id,
            }
            pending.append(pp["id"])

    hands = get_player_hands(tuple(sorted(set(pending))))
    for entry in starters.values():
        entry["hand"] = hands.get(entry["id"], {}).get("pitch_hand", "R")
    return starters
