# data/matchups.py — historical player vs team matchup data and teammate impact

import pandas as pd
from nba_api.stats.endpoints import playergamelog
from nba_api.stats.static import teams
from config import SEASON, MIN_GAMES_VS_TEAM
import time


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


def get_player_vs_team(player_id, team_id, prop_type):
    """
    Returns how a player historically performs against a specific team
    by filtering their game logs for matchups vs that team.

    Returns a dict with:
        - games: number of games in the sample
        - avg: average vs this team for the prop
        - reliable: bool — False if sample size < MIN_GAMES_VS_TEAM
    """
    abbr = get_team_abbr(team_id)
    if not abbr:
        return {"games": 0, "avg": None, "reliable": False}

    from data.players import get_game_logs
    logs = get_game_logs(player_id, n_games=82)

    # Filter to games vs this opponent
    vs_team = logs[logs["MATCHUP"].str.contains(abbr, case=False, na=False)]

    if vs_team.empty:
        return {"games": 0, "avg": None, "reliable": False}

    col_map = {
        "points":              "PTS",
        "rebounds":            "REB",
        "assists":             "AST",
        "pts+reb+ast":         None,
        "three_pointers_made": "FG3M",
    }
    col = col_map.get(prop_type)
    if col is None and prop_type == "pts+reb+ast":
        vs_team = vs_team.copy()
        vs_team["PRA"] = vs_team["PTS"] + vs_team["REB"] + vs_team["AST"]
        col = "PRA"

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
    """
    Compares a player's stats in games where a key teammate played
    vs games where they didn't, using game logs this season.

    Returns a dict with:
        - with_avg: average with teammate
        - without_avg: average without teammate
        - boost: without_avg - with_avg (positive = better without)
        - sample_without: number of games without teammate
    """
    from data.players import get_player_id, get_game_logs

    teammate_id = get_player_id(teammate_name)
    if not teammate_id:
        return None

    player_logs   = get_game_logs(player_id, n_games=82)
    teammate_logs = get_game_logs(teammate_id, n_games=82)

    col_map = {
        "points":              "PTS",
        "rebounds":            "REB",
        "assists":             "AST",
        "pts+reb+ast":         None,
        "three_pointers_made": "FG3M",
    }
    col = col_map.get(prop_type)
    if col is None and prop_type == "pts+reb+ast":
        player_logs["PRA"] = player_logs["PTS"] + player_logs["REB"] + player_logs["AST"]
        col = "PRA"

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
