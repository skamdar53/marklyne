# data/players.py — player game logs, streaks, splits, and usage analysis

import unicodedata
import pandas as pd
from nba_api.stats.endpoints import (
    playergamelog,
    commonallplayers,
    leaguedashplayerstats,
)
from nba_api.stats.static import players
from config import SEASON, N_GAMES
import time


def _normalize(name):
    """Lowercase and strip accent marks so 'Doncic' matches 'Dončić'."""
    return unicodedata.normalize("NFD", name).encode("ascii", "ignore").decode().lower()


def get_player_id(name):
    """Returns the NBA player ID for a given name. Case insensitive, accent insensitive."""
    all_players = players.get_players()
    norm = _normalize(name)
    # Exact match (normalized)
    for p in all_players:
        if _normalize(p["full_name"]) == norm:
            return p["id"]
    # Substring fallback
    for p in all_players:
        if norm in _normalize(p["full_name"]):
            return p["id"]
    return None


_game_log_cache = {}

def get_game_logs(player_id, n_games=N_GAMES):
    """Returns a DataFrame of the player's full season game logs. Cached per session."""
    if player_id not in _game_log_cache:
        time.sleep(0.6)
        logs = playergamelog.PlayerGameLog(
            player_id=player_id,
            season=SEASON,
        ).get_data_frames()[0]
        _game_log_cache[player_id] = logs
    return _game_log_cache[player_id].head(n_games) if n_games < 82 else _game_log_cache[player_id]


def get_streak_stats(player_id, prop_type, line, n_games=N_GAMES):
    """
    Returns how often the player has exceeded the given line
    in their last n_games for the given prop type.

    Returns a dict with:
        - hit_rate: float (0.0 - 1.0)
        - avg: average value over the window
        - over_count: number of games over the line
        - games: total games in window
    """
    logs = get_game_logs(player_id, n_games)

    col_map = {
        "points":               "PTS",
        "rebounds":             "REB",
        "assists":              "AST",
        "pts+reb+ast":          None,
        "three_pointers_made":  "FG3M",
    }

    col = col_map.get(prop_type)
    if col is None and prop_type == "pts+reb+ast":
        logs["PRA"] = logs["PTS"] + logs["REB"] + logs["AST"]
        col = "PRA"

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
    """
    Returns home vs away averages for the given prop type this season.

    Returns a dict with:
        - home_avg: float
        - away_avg: float
        - home_away_edge: positive means better at home
    """
    logs = get_game_logs(player_id, n_games=82)  # full season, uses cache

    col_map = {
        "points":              "PTS",
        "rebounds":            "REB",
        "assists":             "AST",
        "pts+reb+ast":         None,
        "three_pointers_made": "FG3M",
    }
    col = col_map.get(prop_type)
    if col is None and prop_type == "pts+reb+ast":
        logs["PRA"] = logs["PTS"] + logs["REB"] + logs["AST"]
        col = "PRA"

    home = logs[logs["MATCHUP"].str.contains("vs.")][col].mean()
    away = logs[logs["MATCHUP"].str.contains("@")][col].mean()

    return {
        "home_avg":      round(home, 2),
        "away_avg":      round(away, 2),
        "home_away_edge": round(home - away, 2),
    }


def get_rest_stats(player_id, prop_type):
    """
    Returns performance on back-to-backs vs rested games.

    Returns a dict with:
        - b2b_avg: average on back to backs
        - rested_avg: average with 2+ days rest
        - rest_edge: rested_avg - b2b_avg
    """
    logs = get_game_logs(player_id, n_games=82)  # full season, uses cache

    col_map = {
        "points":              "PTS",
        "rebounds":            "REB",
        "assists":             "AST",
        "pts+reb+ast":         None,
        "three_pointers_made": "FG3M",
    }
    col = col_map.get(prop_type)
    if col is None and prop_type == "pts+reb+ast":
        logs["PRA"] = logs["PTS"] + logs["REB"] + logs["AST"]
        col = "PRA"

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
    """
    Returns a player's scoring tendency: how much they rely on 3P vs interior.
    Computed from season game logs.

    Returns a dict with:
        - three_point_rate:          FG3A / FGA  (0 = never shoots 3s, 1 = all 3s)
        - three_point_scoring_share: % of points from 3s
        - interior_scoring_share:    % of points NOT from 3s (paint + mid)
    """
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
    three_point_scoring_share = (avg_fg3m * 3) / avg_pts
    three_point_scoring_share = min(three_point_scoring_share, 1.0)

    return {
        "three_point_rate":          round(three_point_rate, 3),
        "three_point_scoring_share": round(three_point_scoring_share, 3),
        "interior_scoring_share":    round(1 - three_point_scoring_share, 3),
    }


def get_usage_stats(player_id):
    """
    Returns the player's current season usage rate and points per game.
    Useful for modeling how usage shifts when teammates are out.
    """
    time.sleep(0.6)
    stats = leaguedashplayerstats.LeagueDashPlayerStats(
        season=SEASON,
    ).get_data_frames()[0]

    player_row = stats[stats["PLAYER_ID"] == player_id]
    if player_row.empty:
        return None

    return {
        "usg_pct": round(float(player_row["USG_PCT"].values[0]), 4),
        "ppg":     round(float(player_row["PTS"].values[0]), 2),
        "rpg":     round(float(player_row["REB"].values[0]), 2),
        "apg":     round(float(player_row["AST"].values[0]), 2),
    }
