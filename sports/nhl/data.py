# sports/nhl/data.py — NHL data layer.
#
# Two sources, both keyless:
#   * api-web.nhle.com/v1 + api.nhle.com/stats/rest/en — official (undocumented)
#     NHL endpoints: player game logs, schedule, team summary (PP%/PK%, shots
#     for/against per game). Game logs go back to 2007-08.
#   * moneypuck.com flat CSVs — shot-attempt share (Corsi%/Fenwick%), expected
#     goals share, and goalie goals-saved-above-expected, none of which the
#     official API exposes. MoneyPuck seasons are keyed by start year, so NHL
#     season "20242025" is MoneyPuck season "2024".

import io
import unicodedata
from datetime import datetime, timedelta

import pandas as pd
import requests
import streamlit as st

from sports.nhl.config import (
    SEASON, GAME_TYPE, N_GAMES, MIN_GAMES_VS_TEAM, TEAM_ABBR_ALIASES,
)

API_WEB   = "https://api-web.nhle.com/v1"
API_STATS = "https://api.nhle.com/stats/rest/en"
SEARCH    = "https://search.d3.nhle.com/api/v1/search/player"
MONEYPUCK = "https://moneypuck.com/moneypuck/playerData/seasonSummary"

_HEADERS = {"User-Agent": "Mozilla/5.0"}


def _get_json(url, params=None):
    r = requests.get(url, params=params, headers=_HEADERS, timeout=20)
    r.raise_for_status()
    return r.json()


def _normalize(name):
    """Lowercase and strip accent marks so 'Aho' matches 'Ähö'."""
    return unicodedata.normalize("NFD", name).encode("ascii", "ignore").decode().lower()


def _toi_to_minutes(toi):
    """NHL time-on-ice comes back as 'MM:SS'."""
    if not isinstance(toi, str) or ":" not in toi:
        return None
    mins, secs = toi.split(":")
    return int(mins) + int(secs) / 60


def _moneypuck_season(season):
    return season[:4]


# ── Players / teams lookup ───────────────────────────────────────────────────

_player_cache = {}


def get_player_info(name):
    """
    Resolves a player name to {"player_id", "position", "team"} via the NHL's
    own search service. Falls back to inactive players so backtests still work
    for retired/traded names.
    """
    norm = _normalize(name)
    if norm in _player_cache:
        return _player_cache[norm]

    result = None
    for active in ("true", "false"):
        try:
            hits = _get_json(SEARCH, {"culture": "en-us", "limit": 20, "q": name, "active": active})
        except Exception:
            hits = []
        exact = [h for h in hits if _normalize(h.get("name", "")) == norm]
        pool = exact or hits
        if pool:
            hit = pool[0]
            result = {
                "player_id": int(hit["playerId"]),
                "position":  hit.get("positionCode", ""),
                "team":      hit.get("teamAbbrev") or hit.get("lastTeamAbbrev") or "",
            }
            break

    _player_cache[norm] = result
    return result


def get_player_id(name):
    """Returns the NHL player ID for a given name, or None."""
    info = get_player_info(name)
    return info["player_id"] if info else None


@st.cache_data(ttl=86400)
def build_team_lookup():
    """Maps abbreviation, nickname and full name (all lowercased) to tricode."""
    lookup = dict(TEAM_ABBR_ALIASES)
    try:
        standings = _get_json(f"{API_WEB}/standings/now")["standings"]
    except Exception:
        standings = []

    for row in standings:
        abbr = row["teamAbbrev"]["default"]
        lookup[abbr.lower()] = abbr
        lookup[row["teamName"]["default"].lower()] = abbr
        lookup[row["teamCommonName"]["default"].lower()] = abbr
        lookup[row["placeName"]["default"].lower()] = abbr
    return lookup


def get_team_abbr(team_name):
    """Resolves any team string (tricode, nickname, city, full name) to a tricode."""
    if not team_name:
        return None
    key = _normalize(team_name).strip()
    lookup = build_team_lookup()
    return lookup.get(key)


@st.cache_data(ttl=86400)
def _team_id_to_abbr():
    """The team summary endpoint keys on teamId, so we need the id -> tricode map."""
    data = _get_json(f"{API_STATS}/team")["data"]
    return {int(t["id"]): t["triCode"] for t in data}


# ── Game logs ─────────────────────────────────────────────────────────────────

_game_log_cache = {}


def seed_game_log_cache(player_id, logs):
    """
    Override the cache with provided logs (used in backtesting to inject the
    only games that were knowable before a given date). Stored newest-first so
    get_game_logs()'s head(n) slicing means the same thing as it does for a
    live API pull.
    """
    _game_log_cache[player_id] = logs.sort_values("gameDate", ascending=False)


def _fetch_game_log(player_id, season):
    raw = _get_json(f"{API_WEB}/player/{player_id}/game-log/{season}/{GAME_TYPE}")
    df = pd.DataFrame(raw.get("gameLog", []))
    if df.empty:
        return df
    df["gameDate"] = pd.to_datetime(df["gameDate"])
    df["toi_minutes"] = df["toi"].apply(_toi_to_minutes)
    return df


def get_game_logs(player_id, n_games=N_GAMES, season=SEASON):
    """
    Returns a DataFrame of the player's season game logs, newest first
    (the order api-web returns them in). Cached per session; backtests
    replace the cache entry via seed_game_log_cache().
    """
    if player_id in _game_log_cache:
        cached = _game_log_cache[player_id]
        return cached.head(n_games) if n_games < 82 else cached

    cache_key = (player_id, season)
    if cache_key not in _game_log_cache:
        _game_log_cache[cache_key] = _fetch_game_log(player_id, season)
    logs = _game_log_cache[cache_key]
    return logs.head(n_games) if n_games < 82 else logs


def get_multi_season_logs(player_id, seasons):
    """Pulls and concatenates game logs across multiple seasons, sorted by date."""
    frames = []
    for s in seasons:
        try:
            df = _fetch_game_log(player_id, s)
        except Exception:
            continue
        if not df.empty:
            df = df.copy()
            df["season"] = s
            frames.append(df)
    if not frames:
        return None
    all_logs = pd.concat(frames, ignore_index=True)
    return all_logs.sort_values("gameDate").reset_index(drop=True)


_PROP_COL_MAP = {
    "shots_on_goal":     "shots",
    "points":            "points",
    "goals":             "goals",
    "assists":           "assists",
    "power_play_points": "powerPlayPoints",
    "saves":             None,   # derived from shotsAgainst - goalsAgainst
}


def _prop_column(logs, prop_type):
    """Returns (logs, column_name) — adds the derived column for saves."""
    col = _PROP_COL_MAP.get(prop_type)
    if col is None and prop_type == "saves":
        if "shotsAgainst" not in logs.columns or "goalsAgainst" not in logs.columns:
            return logs, "saves"
        logs = logs.copy()
        logs["saves"] = logs["shotsAgainst"] - logs["goalsAgainst"]
        col = "saves"
    return logs, col


def extract_stat(row, prop_type):
    """Extracts the actual stat value from a single game log row."""
    if prop_type == "saves":
        if "shotsAgainst" not in row or pd.isna(row.get("shotsAgainst")):
            return None
        return row["shotsAgainst"] - row["goalsAgainst"]
    col = _PROP_COL_MAP.get(prop_type)
    if not col or col not in row:
        return None
    value = row[col]
    return None if pd.isna(value) else value


def get_streak_stats(player_id, prop_type, line, n_games=N_GAMES):
    """
    Returns how often the player has exceeded the given line in their
    last n_games for the given prop type.
    """
    logs = get_game_logs(player_id, n_games)
    if logs.empty:
        return None
    logs, col = _prop_column(logs, prop_type)
    if col not in logs.columns:
        return None

    values = logs[col].dropna()
    total = len(values)
    if total == 0:
        return None
    over = int((values > line).sum())

    return {
        "hit_rate":    over / total,
        "avg":         float(values.mean()),
        "over_count":  over,
        "games":       total,
        "toi_avg":     float(logs["toi_minutes"].dropna().mean()) if "toi_minutes" in logs else None,
    }


def get_home_away_splits(player_id, prop_type):
    """Returns home vs away averages for the given prop type."""
    logs = get_game_logs(player_id, n_games=82)
    if logs.empty:
        return None
    logs, col = _prop_column(logs, prop_type)
    if col not in logs.columns:
        return None

    home = logs[logs["homeRoadFlag"] == "H"][col].mean()
    away = logs[logs["homeRoadFlag"] == "R"][col].mean()
    if pd.isna(home) or pd.isna(away):
        return None

    return {
        "home_avg":       round(float(home), 2),
        "away_avg":       round(float(away), 2),
        "home_away_edge": round(float(home - away), 2),
    }


def get_rest_stats(player_id, prop_type):
    """Returns performance on back-to-backs vs rested games."""
    logs = get_game_logs(player_id, n_games=82)
    if logs.empty:
        return None
    logs, col = _prop_column(logs, prop_type)
    if col not in logs.columns:
        return None

    logs = logs.copy().sort_values("gameDate")
    logs["days_rest"] = logs["gameDate"].diff().dt.days.fillna(3)

    b2b  = logs[logs["days_rest"] <= 1][col].mean()
    rest = logs[logs["days_rest"] >= 2][col].mean()

    return {
        "b2b_avg":    round(float(b2b), 2)  if not pd.isna(b2b)  else None,
        "rested_avg": round(float(rest), 2) if not pd.isna(rest) else None,
        "rest_edge":  round(float(rest - b2b), 2) if (not pd.isna(b2b) and not pd.isna(rest)) else None,
    }


def get_player_vs_team(player_id, opp_abbr, prop_type):
    """Returns how a player historically performs against a specific team."""
    empty = {"games": 0, "avg": None, "reliable": False}
    if not opp_abbr:
        return empty

    logs = get_game_logs(player_id, n_games=82)
    if logs.empty or "opponentAbbrev" not in logs.columns:
        return empty

    vs_team = logs[logs["opponentAbbrev"].str.upper() == opp_abbr.upper()]
    if vs_team.empty:
        return empty

    vs_team, col = _prop_column(vs_team, prop_type)
    if col not in vs_team.columns:
        return empty

    values = vs_team[col].dropna()
    if values.empty:
        return empty

    return {
        "games":    len(values),
        "avg":      round(float(values.mean()), 2),
        "reliable": len(values) >= MIN_GAMES_VS_TEAM,
    }


def get_performance_without_teammate(player_id, teammate_name, prop_type):
    """
    Compares a player's production in games with vs without a linemate, matched
    on gameId. A top-line center missing genuinely shifts a winger's ice time.
    """
    teammate_id = get_player_id(teammate_name)
    if not teammate_id:
        return None

    player_logs = get_game_logs(player_id, n_games=82)
    teammate_logs = get_game_logs(teammate_id, n_games=82)
    if player_logs.empty or teammate_logs.empty:
        return None

    player_logs, col = _prop_column(player_logs, prop_type)
    if col not in player_logs.columns:
        return None

    player_logs = player_logs.copy()
    played = set(teammate_logs["gameId"].values)
    player_logs["teammate_played"] = player_logs["gameId"].isin(played)

    with_tm    = player_logs[player_logs["teammate_played"]][col].mean()
    without_tm = player_logs[~player_logs["teammate_played"]][col].mean()

    return {
        "with_avg":       round(float(with_tm), 2)    if not pd.isna(with_tm)    else None,
        "without_avg":    round(float(without_tm), 2) if not pd.isna(without_tm) else None,
        "boost":          round(float(without_tm - with_tm), 2)
                          if (not pd.isna(with_tm) and not pd.isna(without_tm)) else None,
        "sample_without": int((~player_logs["teammate_played"]).sum()),
    }


# ── Team stats: special teams, shot volume, shot suppression ──────────────────

_team_summary_cache = {}


def _get_team_summary(season):
    if season not in _team_summary_cache:
        data = _get_json(
            f"{API_STATS}/team/summary",
            {"cayenneExp": f"seasonId={season} and gameTypeId={GAME_TYPE}"},
        )["data"]
        df = pd.DataFrame(data)
        df["abbrev"] = df["teamId"].map(_team_id_to_abbr())
        _team_summary_cache[season] = df
    return _team_summary_cache[season]


def get_team_stats(team_abbr, season=SEASON):
    """
    Season-level team rates plus league ranks. Ranks are 1 = most, so
    goals_against_rank 1 means "allows the most goals" (softest matchup).
    """
    df = _get_team_summary(season)
    row = df[df["abbrev"] == team_abbr]
    if row.empty:
        return None

    i = row.index[0]

    def rank_desc(col):
        return int(df[col].rank(ascending=False, method="min")[i])

    return {
        "power_play_pct":        float(row["powerPlayPct"].values[0]),
        "penalty_kill_pct":      float(row["penaltyKillPct"].values[0]),
        "shots_for_per_game":    float(row["shotsForPerGame"].values[0]),
        "shots_against_per_game": float(row["shotsAgainstPerGame"].values[0]),
        "goals_for_per_game":    float(row["goalsForPerGame"].values[0]),
        "goals_against_per_game": float(row["goalsAgainstPerGame"].values[0]),
        "shots_against_rank":    rank_desc("shotsAgainstPerGame"),
        "shots_for_rank":        rank_desc("shotsForPerGame"),
        "goals_against_rank":    rank_desc("goalsAgainstPerGame"),
        "power_play_rank":       rank_desc("powerPlayPct"),
        "penalty_kill_rank":     rank_desc("penaltyKillPct"),
        "league_penalty_kill":   float(df["penaltyKillPct"].mean()),
        "league_power_play":     float(df["powerPlayPct"].mean()),
        "league_shots_against":  float(df["shotsAgainstPerGame"].mean()),
        "league_shots_for":      float(df["shotsForPerGame"].mean()),
        "total_teams":           len(df),
    }


# ── MoneyPuck: possession and goalie quality ──────────────────────────────────

_moneypuck_cache = {}


def _moneypuck_csv(kind, season):
    """
    kind is one of teams / skaters / goalies. Fetched through requests rather
    than pd.read_csv(url) because Cloudflare 403s urllib's default user agent.
    """
    key = (kind, season)
    if key not in _moneypuck_cache:
        url = f"{MONEYPUCK}/{_moneypuck_season(season)}/regular/{kind}.csv"
        r = requests.get(url, headers=_HEADERS, timeout=60)
        r.raise_for_status()
        _moneypuck_cache[key] = pd.read_csv(io.StringIO(r.text))
    return _moneypuck_cache[key]


def get_team_possession(team_abbr, season=SEASON):
    """
    All-situations shot-attempt share from MoneyPuck. corsi_pct counts every
    attempt, fenwick_pct excludes blocks, xg_pct weights by shot quality —
    together they're the closest NHL analogue to pace + efficiency.
    """
    df = _moneypuck_csv("teams", season)
    row = df[(df["team"] == team_abbr) & (df["situation"] == "all")]
    if row.empty:
        return None
    return {
        "corsi_pct":    float(row["corsiPercentage"].values[0]),
        "fenwick_pct":  float(row["fenwickPercentage"].values[0]),
        "xg_pct":       float(row["xGoalsPercentage"].values[0]),
        "sog_against":  float(row["shotsOnGoalAgainst"].values[0]),
        "games_played": int(row["games_played"].values[0]),
    }


def _goalie_row_stats(row):
    """MoneyPuck goalie rows: 'goals'/'xGoals' are against, icetime is seconds."""
    shots  = float(row["ongoal"])
    goals  = float(row["goals"])
    icetime = float(row["icetime"])
    if shots <= 0 or icetime <= 0:
        return None
    gsax = float(row["xGoals"]) - goals
    return {
        "save_pct":    round(1 - goals / shots, 4),
        "gsax":        round(gsax, 2),
        "gsax_per_60": round(gsax / (icetime / 3600), 3),
        "shots_faced": shots,
        "games":       int(row["games_played"]),
    }


def get_goalie_quality(player_id, season=SEASON):
    """Save% and goals-saved-above-expected for a specific goalie."""
    df = _moneypuck_csv("goalies", season)
    row = df[(df["playerId"] == player_id) & (df["situation"] == "all")]
    if row.empty:
        return None
    return _goalie_row_stats(row.iloc[0])


def get_starting_goalie_quality(team_abbr, season=SEASON):
    """
    Quality of a team's most-used goalie. We don't know the confirmed starter
    at slate-build time, so the workhorse is the best available proxy.
    """
    df = _moneypuck_csv("goalies", season)
    rows = df[(df["team"] == team_abbr) & (df["situation"] == "all")]
    if rows.empty:
        return None
    starter = rows.sort_values("icetime", ascending=False).iloc[0]
    stats = _goalie_row_stats(starter)
    if stats:
        stats["name"] = starter["name"]
    return stats


def get_league_goalie_baseline(season=SEASON):
    """League-average save% across every goalie with real workload."""
    df = _moneypuck_csv("goalies", season)
    rows = df[(df["situation"] == "all") & (df["ongoal"] >= 200)]
    if rows.empty:
        return 0.905
    return float(1 - rows["goals"].sum() / rows["ongoal"].sum())


# ── Schedule ──────────────────────────────────────────────────────────────────

def _games_on(date_str):
    """schedule/{date} returns the whole week containing that date."""
    try:
        week = _get_json(f"{API_WEB}/schedule/{date_str}")["gameWeek"]
    except Exception as e:
        print(f"NHL schedule fetch error ({date_str}): {e}")
        return []
    for day in week:
        if day.get("date") == date_str:
            return day.get("games", [])
    return []


@st.cache_data(ttl=3600)
def get_yesterdays_teams():
    """Returns the set of team abbreviations that played yesterday (for B2B detection)."""
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    played = set()
    for g in _games_on(yesterday):
        played.add(g["homeTeam"]["abbrev"])
        played.add(g["awayTeam"]["abbrev"])
    return played


@st.cache_data(ttl=3600)
def get_todays_games():
    """Returns today's NHL games with home/away team info."""
    today = datetime.now().strftime("%Y-%m-%d")
    return [
        {
            "home_team":    g["homeTeam"]["abbrev"],
            "away_team":    g["awayTeam"]["abbrev"],
            "home_team_id": int(g["homeTeam"]["id"]),
            "away_team_id": int(g["awayTeam"]["id"]),
        }
        for g in _games_on(today)
    ]
