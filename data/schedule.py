# data/schedule.py — fetch today's NBA games and build auto slate from PrizePicks

import requests
import streamlit as st
from datetime import datetime, timedelta
from nba_api.stats.endpoints import scoreboardv2
from nba_api.stats.static import teams as nba_teams
from data.lines import get_prizepicks_lines
import time


@st.cache_data(ttl=3600)
def get_yesterdays_teams():
    """
    Returns the set of team abbreviations that played yesterday (for B2B detection).
    """
    time.sleep(0.6)
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%m/%d/%Y")
    try:
        board = scoreboardv2.ScoreboardV2(game_date=yesterday)
        games_df = board.get_data_frames()[0]
    except Exception:
        return set()

    all_teams = {t["id"]: t["abbreviation"] for t in nba_teams.get_teams()}
    played = set()
    for _, row in games_df.iterrows():
        played.add(all_teams.get(int(row["HOME_TEAM_ID"]), ""))
        played.add(all_teams.get(int(row["VISITOR_TEAM_ID"]), ""))
    played.discard("")
    return played


@st.cache_data(ttl=3600)
def get_todays_games():
    """
    Returns a list of today's NBA games with home/away team info.

    Returns list of dicts with:
        - home_team: team abbreviation
        - away_team: team abbreviation
        - home_team_id
        - away_team_id
    """
    time.sleep(0.6)
    today = datetime.now().strftime("%m/%d/%Y")
    try:
        board = scoreboardv2.ScoreboardV2(game_date=today)
        games_df = board.get_data_frames()[0]
    except Exception as e:
        print(f"Schedule fetch error: {e}")
        return []

    all_teams = {t["id"]: t["abbreviation"] for t in nba_teams.get_teams()}

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


def _build_team_lookup():
    """Maps team abbreviation and nickname variations to standard abbreviation."""
    lookup = {}
    for t in nba_teams.get_teams():
        lookup[t["abbreviation"].lower()]  = t["abbreviation"]
        lookup[t["nickname"].lower()]      = t["abbreviation"]
        lookup[t["full_name"].lower()]     = t["abbreviation"]
    return lookup


def build_auto_slate(missing_teammates=None, lines=None):
    """
    Pulls today's PrizePicks lines and enriches them with
    home/away info from the NBA schedule.

    Returns a list of game dicts ready to pass into generate_picks().
    """
    if missing_teammates is None:
        missing_teammates = {}

    games            = get_todays_games()
    team_lookup      = _build_team_lookup()
    played_yesterday = get_yesterdays_teams()

    # Build set of teams playing today
    playing_today = set()
    home_teams    = set()
    for g in games:
        playing_today.add(g["home_team"])
        playing_today.add(g["away_team"])
        home_teams.add(g["home_team"])

    # Build opponent map: team -> opponent
    opponent_map = {}
    for g in games:
        opponent_map[g["home_team"]] = g["away_team"]
        opponent_map[g["away_team"]] = g["home_team"]

    if lines is None:
        lines = get_prizepicks_lines()

    # Group by (player, prop_type) and pick the median line value
    from collections import defaultdict
    grouped = defaultdict(list)
    meta = {}

    for line in lines:
        team_raw  = line.get("team", "").strip()
        team_abbr = team_lookup.get(team_raw.lower())

        if not team_abbr or team_abbr not in playing_today:
            continue

        opponent = opponent_map.get(team_abbr, "")
        if not opponent:
            continue

        key = (line["player_name"], line["prop_type"])
        grouped[key].append(line["line"])
        if key not in meta:
            meta[key] = {
                "player_name": line["player_name"],
                "team":        team_abbr,
                "opponent":    opponent,
                "prop_type":   line["prop_type"],
                "is_home":     team_abbr in home_teams,
                "is_b2b":      team_abbr in played_yesterday,
                "position":    line.get("position", "SF"),
            }

    slate = []
    for key, line_values in grouped.items():
        line_values.sort()
        median_line = line_values[len(line_values) // 2]  # pick middle value
        entry = dict(meta[key])
        entry["line"] = median_line
        slate.append(entry)

    slate = sorted(slate, key=lambda x: x["player_name"])
    return slate
