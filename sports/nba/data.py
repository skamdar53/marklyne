# sports/nba/data.py — nba_api wrapper: player game logs, team stats, schedule
# (moved from the original data/players.py, data/defense.py, data/matchups.py,
# data/schedule.py — logic unchanged, just consolidated under sports/nba/)

import time
import unicodedata

import pandas as pd
import streamlit as st
from nba_api.stats.endpoints import (
    playergamelog,
    leaguedashteamstats,
    leaguedashptdefend,
    leaguedashplayerstats,
    scoreboardv2,
)
from nba_api.stats.static import players, teams
from datetime import datetime, timedelta

from sports.nba.config import SEASON, N_GAMES, MIN_GAMES_VS_TEAM


# ── Players / teams lookup ───────────────────────────────────────────────────

def _normalize(name):
    """Lowercase and strip accent marks so 'Doncic' matches 'Dončić'."""
    return unicodedata.normalize("NFD", name).encode("ascii", "ignore").decode().lower()


def get_player_id(name):
    """Returns the NBA player ID for a given name. Case insensitive, accent insensitive."""
    all_players = players.get_players()
    norm = _normalize(name)
    for p in all_players:
        if _normalize(p["full_name"]) == norm:
            return p["id"]
    for p in all_players:
        if norm in _normalize(p["full_name"]):
            return p["id"]
    return None


def get_team_id(team_name):
    """Returns the NBA team ID for a given team name or abbreviation."""
    all_teams = teams.get_teams()
    team_name = team_name.lower()
    for t in all_teams:
        if (t["full_name"].lower() == team_name or
                t["abbreviation"].lower() == team_name or
                t["nickname"].lower() == team_name):
            return t["id"]
    return None


def get_team_abbr(team_id):
    """Returns the abbreviation for a team ID."""
    for t in teams.get_teams():
        if t["id"] == team_id:
            return t["abbreviation"]
    return None


def get_player_team(player_id):
    """
    Returns a player's current team abbreviation, derived from their most
    recent game log's MATCHUP field ("LAL vs. GSW" / "LAL @ GSW" — the team
    is always the first token). Needed now that prop lines come from Kalshi/
    Polymarket, which don't hand us a team the way PrizePicks used to.
    """
    logs = get_game_logs(player_id, n_games=1)
    if logs.empty:
        return None
    return logs.iloc[0]["MATCHUP"].split(" ")[0]


# ── Game logs ─────────────────────────────────────────────────────────────────

_game_log_cache = {}


def seed_game_log_cache(player_id, logs):
    """Override the cache with provided logs (used in backtesting to inject historical data)."""
    _game_log_cache[player_id] = logs


def get_game_logs(player_id, n_games=N_GAMES):
    """
    Returns a DataFrame of the player's current-season game logs, cached per
    session by player_id. Backtesting overrides this cache via
    seed_game_log_cache() so scoring reads a historical-only window instead
    of live data — keep this keyed by player_id alone (not season) so that
    override actually takes effect; see sports/nba/factors.py backtest_games.
    """
    if player_id not in _game_log_cache:
        time.sleep(0.6)
        logs = playergamelog.PlayerGameLog(
            player_id=player_id,
            season=SEASON,
        ).get_data_frames()[0]
        _game_log_cache[player_id] = logs
    return _game_log_cache[player_id].head(n_games) if n_games < 82 else _game_log_cache[player_id]


def get_multi_season_logs(player_id, seasons):
    """Pulls and concatenates game logs across multiple seasons, sorted by date."""
    all_logs_list = []
    for s in seasons:
        time.sleep(0.6)
        try:
            df = playergamelog.PlayerGameLog(player_id=player_id, season=s).get_data_frames()[0]
            all_logs_list.append(df)
        except Exception:
            continue
    if not all_logs_list:
        return None
    all_logs = pd.concat(all_logs_list, ignore_index=True)
    all_logs["GAME_DATE"] = pd.to_datetime(all_logs["GAME_DATE"])
    return all_logs.sort_values("GAME_DATE").reset_index(drop=True)


_PROP_COL_MAP = {
    "points":              "PTS",
    "rebounds":            "REB",
    "assists":             "AST",
    "pts+reb+ast":         None,
    "three_pointers_made": "FG3M",
}


def _prop_column(logs, prop_type):
    col = _PROP_COL_MAP.get(prop_type)
    if col is None and prop_type == "pts+reb+ast":
        logs = logs.copy()
        logs["PRA"] = logs["PTS"] + logs["REB"] + logs["AST"]
        col = "PRA"
    return logs, col


def extract_stat(row, prop_type):
    """Extracts the actual stat value from a single game log row."""
    if prop_type == "pts+reb+ast":
        return row["PTS"] + row["REB"] + row["AST"]
    col = _PROP_COL_MAP.get(prop_type)
    return row[col] if col else None


def get_streak_stats(player_id, prop_type, line, n_games=N_GAMES):
    """
    Returns how often the player has exceeded the given line in their
    last n_games for the given prop type.
    """
    logs = get_game_logs(player_id, n_games)
    logs, col = _prop_column(logs, prop_type)
    if col not in logs.columns:
        return None

    values = logs[col]
    over = (values > line).sum()
    total = len(values)

    return {
        "hit_rate":   over / total if total > 0 else 0,
        "avg":        values.mean(),
        "over_count": int(over),
        "games":      total,
    }


def get_home_away_splits(player_id, prop_type):
    """Returns home vs away averages for the given prop type this season."""
    logs = get_game_logs(player_id, n_games=82)
    logs, col = _prop_column(logs, prop_type)

    home = logs[logs["MATCHUP"].str.contains("vs.")][col].mean()
    away = logs[logs["MATCHUP"].str.contains("@")][col].mean()

    return {
        "home_avg":       round(home, 2),
        "away_avg":       round(away, 2),
        "home_away_edge": round(home - away, 2),
    }


def get_rest_stats(player_id, prop_type):
    """Returns performance on back-to-backs vs rested games."""
    logs = get_game_logs(player_id, n_games=82)
    logs, col = _prop_column(logs, prop_type)

    logs = logs.copy()
    logs["GAME_DATE"] = pd.to_datetime(logs["GAME_DATE"])
    logs = logs.sort_values("GAME_DATE")
    logs["DAYS_REST"] = logs["GAME_DATE"].diff().dt.days.fillna(3)

    b2b  = logs[logs["DAYS_REST"] <= 1][col].mean()
    rest = logs[logs["DAYS_REST"] >= 2][col].mean()

    return {
        "b2b_avg":    round(b2b, 2)  if not pd.isna(b2b)  else None,
        "rested_avg": round(rest, 2) if not pd.isna(rest) else None,
        "rest_edge":  round(rest - b2b, 2) if (not pd.isna(b2b) and not pd.isna(rest)) else None,
    }


def get_player_shot_profile(player_id):
    """Returns a player's scoring tendency: how much they rely on 3P vs interior."""
    logs = get_game_logs(player_id, n_games=82)
    if logs.empty:
        return None

    avg_fga  = logs["FGA"].mean()
    avg_fg3a = logs["FG3A"].mean()
    avg_fg3m = logs["FG3M"].mean()
    avg_pts  = logs["PTS"].mean()

    if avg_fga == 0 or avg_pts == 0:
        return None

    three_point_rate          = avg_fg3a / avg_fga
    three_point_scoring_share = min((avg_fg3m * 3) / avg_pts, 1.0)

    return {
        "three_point_rate":          round(three_point_rate, 3),
        "three_point_scoring_share": round(three_point_scoring_share, 3),
        "interior_scoring_share":    round(1 - three_point_scoring_share, 3),
    }


def get_player_vs_team(player_id, team_id, prop_type):
    """Returns how a player historically performs against a specific team."""
    abbr = get_team_abbr(team_id)
    if not abbr:
        return {"games": 0, "avg": None, "reliable": False}

    logs = get_game_logs(player_id, n_games=82)
    vs_team = logs[logs["MATCHUP"].str.contains(abbr, case=False, na=False)]
    if vs_team.empty:
        return {"games": 0, "avg": None, "reliable": False}

    vs_team, col = _prop_column(vs_team, prop_type)
    if col not in vs_team.columns:
        return {"games": 0, "avg": None, "reliable": False}

    games = len(vs_team)
    avg   = vs_team[col].mean()

    return {
        "games":    games,
        "avg":      round(avg, 2),
        "reliable": games >= MIN_GAMES_VS_TEAM,
    }


def get_performance_without_teammate(player_id, teammate_name, prop_type):
    """Compares a player's stats in games with vs without a key teammate."""
    teammate_id = get_player_id(teammate_name)
    if not teammate_id:
        return None

    player_logs   = get_game_logs(player_id, n_games=82).copy()
    teammate_logs = get_game_logs(teammate_id, n_games=82)

    player_logs, col = _prop_column(player_logs, prop_type)

    teammate_game_ids = set(teammate_logs["Game_ID"].values)
    player_logs["teammate_played"] = player_logs["Game_ID"].isin(teammate_game_ids)

    with_tm    = player_logs[player_logs["teammate_played"]][col].mean()
    without_tm = player_logs[~player_logs["teammate_played"]][col].mean()
    n_without  = int((~player_logs["teammate_played"]).sum())

    return {
        "with_avg":      round(with_tm, 2)    if not pd.isna(with_tm)    else None,
        "without_avg":   round(without_tm, 2) if not pd.isna(without_tm) else None,
        "boost":         round(without_tm - with_tm, 2) if (not pd.isna(with_tm) and not pd.isna(without_tm)) else None,
        "sample_without": n_without,
    }


# ── Team defense / pace ───────────────────────────────────────────────────────

_team_stats_cache = {}


def _get_league_team_stats(measure_type):
    if measure_type not in _team_stats_cache:
        time.sleep(0.6)
        _team_stats_cache[measure_type] = leaguedashteamstats.LeagueDashTeamStats(
            season=SEASON,
            measure_type_detailed_defense=measure_type,
            per_mode_detailed="PerGame",
        ).get_data_frames()[0]
    return _team_stats_cache[measure_type]


def get_team_defensive_rating(team_id):
    stats    = _get_league_team_stats("Advanced")
    team_row = stats[stats["TEAM_ID"] == team_id]
    if team_row.empty:
        return None
    all_ratings = stats["DEF_RATING"].sort_values()
    rank = int(all_ratings.rank()[team_row.index[0]])
    return {
        "def_rating":  round(float(team_row["DEF_RATING"].values[0]), 2),
        "rank":        rank,
        "total_teams": len(stats),
    }


def get_team_zone_defense(team_id):
    stats    = _get_league_team_stats("Opponent")
    team_row = stats[stats["TEAM_ID"] == team_id]
    if team_row.empty:
        return None

    league_3p_pct = float(stats["OPP_FG3_PCT"].mean())
    league_fg_pct = float(stats["OPP_FG_PCT"].mean())
    opp_3p_pct    = float(team_row["OPP_FG3_PCT"].values[0])
    opp_fg_pct    = float(team_row["OPP_FG_PCT"].values[0])

    return {
        "perimeter_weakness": round((opp_3p_pct - league_3p_pct) / max(league_3p_pct, 0.001), 4),
        "interior_weakness":  round((opp_fg_pct  - league_fg_pct)  / max(league_fg_pct,  0.001), 4),
        "opp_3p_pct": round(opp_3p_pct, 4),
        "opp_fg_pct": round(opp_fg_pct, 4),
    }


def get_opponent_prop_stats(team_id):
    stats    = _get_league_team_stats("Opponent")
    team_row = stats[stats["TEAM_ID"] == team_id]
    if team_row.empty:
        return None

    def rank_desc(col):
        return int(stats[col].rank(ascending=False)[team_row.index[0]])

    return {
        "opp_pts":      round(float(team_row["OPP_PTS"].values[0]), 2),
        "opp_reb":      round(float(team_row["OPP_REB"].values[0]), 2),
        "opp_ast":      round(float(team_row["OPP_AST"].values[0]), 2),
        "opp_pts_rank": rank_desc("OPP_PTS"),
        "opp_reb_rank": rank_desc("OPP_REB"),
        "opp_ast_rank": rank_desc("OPP_AST"),
        "total_teams":  len(stats),
    }


def get_team_pace(team_id):
    stats    = _get_league_team_stats("Advanced")
    team_row = stats[stats["TEAM_ID"] == team_id]
    if team_row.empty:
        return None
    all_pace = stats["PACE"].sort_values(ascending=False)
    rank = int(all_pace.rank(ascending=False)[team_row.index[0]])
    return {
        "pace": round(float(team_row["PACE"].values[0]), 2),
        "rank": rank,
        "total_teams": len(stats),
    }


# ── Schedule ──────────────────────────────────────────────────────────────────

@st.cache_data(ttl=3600)
def get_yesterdays_teams():
    """Returns the set of team abbreviations that played yesterday (for B2B detection)."""
    time.sleep(0.6)
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%m/%d/%Y")
    try:
        board = scoreboardv2.ScoreboardV2(game_date=yesterday)
        games_df = board.get_data_frames()[0]
    except Exception:
        return set()

    all_teams = {t["id"]: t["abbreviation"] for t in teams.get_teams()}
    played = set()
    for _, row in games_df.iterrows():
        played.add(all_teams.get(int(row["HOME_TEAM_ID"]), ""))
        played.add(all_teams.get(int(row["VISITOR_TEAM_ID"]), ""))
    played.discard("")
    return played


@st.cache_data(ttl=3600)
def get_todays_games():
    """Returns today's NBA games with home/away team info."""
    time.sleep(0.6)
    today = datetime.now().strftime("%m/%d/%Y")
    try:
        board = scoreboardv2.ScoreboardV2(game_date=today)
        games_df = board.get_data_frames()[0]
    except Exception as e:
        print(f"NBA schedule fetch error: {e}")
        return []

    all_teams = {t["id"]: t["abbreviation"] for t in teams.get_teams()}

    games = []
    for _, row in games_df.iterrows():
        home_id = int(row["HOME_TEAM_ID"])
        away_id = int(row["VISITOR_TEAM_ID"])
        games.append({
            "home_team":    all_teams.get(home_id, ""),
            "away_team":    all_teams.get(away_id, ""),
            "home_team_id": home_id,
            "away_team_id": away_id,
        })
    return games
