# data/defense.py — opponent defensive stats, pace, and positional defense

import pandas as pd
from nba_api.stats.endpoints import (
    leaguedashteamstats,
    leaguedashptdefend,
)
from config import SEASON
import time

_team_stats_cache = {}

def _get_league_team_stats(measure_type):
    """Fetches and caches league-wide team stats to avoid redundant API calls."""
    if measure_type not in _team_stats_cache:
        time.sleep(0.6)
        _team_stats_cache[measure_type] = leaguedashteamstats.LeagueDashTeamStats(
            season=SEASON,
            measure_type_detailed_defense=measure_type,
            per_mode_detailed="PerGame",
        ).get_data_frames()[0]
    return _team_stats_cache[measure_type]


def get_team_defensive_rating(team_id):
    """
    Returns a team's overall defensive rating this season.
    Lower = better defense.

    Returns a dict with:
        - def_rating: points allowed per 100 possessions
        - opp_pts_pg: opponent points per game
        - rank: defensive rank (1 = best defense)
    """
    stats    = _get_league_team_stats("Advanced")
    team_row = stats[stats["TEAM_ID"] == team_id]
    if team_row.empty:
        return None

    all_ratings = stats["DEF_RATING"].sort_values()
    rank = int(all_ratings.rank()[team_row.index[0]])

    return {
        "def_rating": round(float(team_row["DEF_RATING"].values[0]), 2),
        "rank":       rank,
        "total_teams": len(stats),
    }


def get_team_zone_defense(team_id):
    """
    Returns how well a team defends the perimeter vs interior.

    Returns a dict with:
        - perimeter_weakness: positive = weak 3P defense (good for perimeter scorers)
        - interior_weakness:  positive = weak 2P/paint defense (good for interior scorers)
        - opp_3p_pct: opponent 3P% allowed
        - opp_fg_pct: opponent overall FG% allowed
    """
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
    """
    Returns how many pts/reb/ast a team allows per game vs league average.
    Used for prop-specific defense scoring.

    Returns a dict with:
        - opp_pts_rank: 1 = most pts allowed (weakest), 30 = fewest (strongest)
        - opp_reb_rank, opp_ast_rank
        - opp_pts, opp_reb, opp_ast: raw values
    """
    stats    = _get_league_team_stats("Opponent")
    team_row = stats[stats["TEAM_ID"] == team_id]
    if team_row.empty:
        return None

    def rank_desc(col):
        # Rank descending: most allowed = rank 1 (easiest to bet over)
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
    """
    Returns a team's pace (possessions per 48 min) this season.
    Higher pace = more possessions = more counting stat opportunities.

    Returns a dict with:
        - pace: possessions per 48
        - rank: pace rank (1 = fastest)
    """
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


def get_defense_vs_position(team_id, position):
    """
    Returns how many points, rebounds, and assists a team allows
    to a specific position per game this season.

    position: "PG", "SG", "SF", "PF", "C"

    Returns a dict with:
        - pts_allowed: points allowed to position per game
        - reb_allowed: rebounds allowed
        - ast_allowed: assists allowed
    """
    time.sleep(0.6)

    position_map = {
        "PG": "Point Guard",
        "SG": "Shooting Guard",
        "SF": "Small Forward",
        "PF": "Power Forward",
        "C":  "Center",
    }
    pos_label = position_map.get(position.upper(), position)

    try:
        defend = leaguedashptdefend.LeagueDashPtDefend(
            season=SEASON,
            defense_category="Overall",
            per_mode_simple="PerGame",
        ).get_data_frames()[0]

        team_row = defend[defend["CLOSE_DEF_PERSON_ID"] == team_id]
        if team_row.empty:
            return None

        return {
            "pts_allowed": round(float(team_row["PTS"].values[0]), 2) if "PTS" in team_row else None,
        }
    except Exception:
        # Fall back to opponent dashboard if pt defend endpoint fails
        return _defense_vs_position_fallback(team_id, position)


def _defense_vs_position_fallback(team_id, position):
    """
    Fallback: uses LeagueDashTeamStats opponent stats as a proxy
    when position-level data isn't available.
    """
    time.sleep(0.6)
    stats = leaguedashteamstats.LeagueDashTeamStats(
        season=SEASON,
        measure_type_detailed_defense="Opponent",
    ).get_data_frames()[0]

    team_row = stats[stats["TEAM_ID"] == team_id]
    if team_row.empty:
        return None

    return {
        "opp_pts_pg":  round(float(team_row["OPP_PTS"].values[0]), 2),
        "opp_reb_pg":  round(float(team_row["OPP_REB"].values[0]), 2),
        "opp_ast_pg":  round(float(team_row["OPP_AST"].values[0]), 2),
        "note": "team-level fallback, position data unavailable",
    }
