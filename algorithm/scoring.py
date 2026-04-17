# algorithm/scoring.py — combines all data factors into a single confidence score

from data.matchups import get_player_vs_team, get_performance_without_teammate
from data.defense import (
    get_team_defensive_rating, get_team_pace, get_defense_vs_position,
    get_team_zone_defense, get_opponent_prop_stats,
)
from data.players import (
    get_player_id, get_streak_stats, get_home_away_splits,
    get_rest_stats, get_usage_stats, get_player_shot_profile,
)
from data.matchups import get_team_id
from config import WEIGHTS, N_GAMES


def score_streak(player_id, prop_type, line):
    """
    Score based on recent hit rate vs the line.
    Returns a float 0.0–1.0 representing confidence from streak data.
    """
    stats = get_streak_stats(player_id, prop_type, line)
    if not stats:
        return 0.5  # neutral if no data

    hit_rate = stats["hit_rate"]
    avg = stats["avg"]

    # Boost if average is well above the line
    avg_edge = (avg - line) / line if line > 0 else 0
    boost = min(avg_edge * 0.3, 0.1)  # cap boost at 10%

    return min(hit_rate + boost, 1.0)


def score_matchup(player_id, team_id, prop_type, line):
    """
    Score based on historical performance vs this specific team.
    Returns a float 0.0–1.0.
    """
    matchup = get_player_vs_team(player_id, team_id, prop_type)
    if not matchup or not matchup["reliable"] or matchup["avg"] is None:
        return 0.5  # neutral if not enough history

    avg = matchup["avg"]
    if avg > line:
        # Scale how far over the line they average
        edge = (avg - line) / line
        return min(0.5 + edge * 0.5, 0.95)
    else:
        edge = (line - avg) / line
        return max(0.5 - edge * 0.5, 0.05)


def score_defense(team_id, position, prop_type, line, player_id=None):
    """
    Score based on how easy/hard the opponent defense is for this prop.
    Combines overall defensive rating, pace, prop-specific opponent stats,
    and shot zone matching (perimeter vs interior tendency).
    Returns a float 0.0–1.0. Higher = weaker defense (easier to go over).
    """
    rating = get_team_defensive_rating(team_id)
    pace   = get_team_pace(team_id)

    if not rating or not pace:
        return 0.5

    # 1. Overall defensive rating (25%) — higher = worse defense = easier to go over
    def_score = (rating["def_rating"] - 110) / 20
    def_score = max(min(def_score + 0.5, 1.0), 0.0)

    # 2. Pace (15%) — more possessions = more counting stats
    pace_score = (pace["pace"] - 95) / 15
    pace_score = max(min(pace_score + 0.5, 1.0), 0.0)

    # 3. Prop-specific opponent stats (30%) — how much of this stat does team allow
    prop_score = 0.5
    opp_stats  = get_opponent_prop_stats(team_id)
    if opp_stats:
        rank_map = {
            "points":              "opp_pts_rank",
            "pts+reb+ast":         "opp_pts_rank",
            "rebounds":            "opp_reb_rank",
            "assists":             "opp_ast_rank",
            "three_pointers_made": "opp_pts_rank",
        }
        rank_key = rank_map.get(prop_type, "opp_pts_rank")
        rank     = opp_stats[rank_key]
        total    = opp_stats["total_teams"]
        # Rank 1 = most allowed = easiest = score 1.0; rank 30 = hardest = score 0.0
        prop_score = 1.0 - (rank - 1) / (total - 1)

    # 4. Shot zone match (30%) — only meaningful for points prop
    zone_score = 0.5
    if prop_type in ("points", "pts+reb+ast", "three_pointers_made") and player_id:
        zone_def     = get_team_zone_defense(team_id)
        shot_profile = get_player_shot_profile(player_id)
        if zone_def and shot_profile:
            perimeter = shot_profile["three_point_rate"]
            interior  = shot_profile["interior_scoring_share"]
            # Blend player tendencies with team zone weaknesses
            zone_boost = (perimeter * zone_def["perimeter_weakness"] +
                          interior  * zone_def["interior_weakness"])
            zone_score = max(min(0.5 + zone_boost * 2, 1.0), 0.0)

    return (def_score * 0.25) + (pace_score * 0.15) + (prop_score * 0.30) + (zone_score * 0.30)


def score_home_away(player_id, prop_type, is_home):
    """
    Score based on home/away splits.
    Returns a float 0.0–1.0.
    """
    splits = get_home_away_splits(player_id, prop_type)
    if not splits:
        return 0.5

    edge = splits["home_away_edge"]
    if is_home:
        # Positive edge means better at home
        return min(0.5 + edge / 10, 0.9)
    else:
        return max(0.5 - edge / 10, 0.1)


def score_rest(player_id, prop_type, is_b2b):
    """
    Score based on rest situation.
    Returns a float 0.0–1.0. Lower on b2b if player has strong rest edge.
    """
    rest = get_rest_stats(player_id, prop_type)
    if not rest or rest["rest_edge"] is None:
        return 0.5

    edge = rest["rest_edge"]
    if is_b2b:
        # Penalize if player is significantly worse on b2b
        return max(0.5 - (edge / 10), 0.1)
    else:
        return min(0.5 + (edge / 10), 0.9)


def score_usage(player_id, prop_type, missing_teammate=None):
    """
    Score based on usage rate impact when a key teammate is missing.
    Returns a float 0.0–1.0.
    """
    if not missing_teammate:
        return 0.5  # neutral — no teammate impact

    impact = get_performance_without_teammate(player_id, missing_teammate, prop_type)
    if not impact or impact["boost"] is None:
        return 0.5

    boost = impact["boost"]
    # Positive boost = player does better without the teammate
    return min(0.5 + boost / 15, 0.95)


def calculate_confidence(
    player_name,
    opponent_team,
    prop_type,
    line,
    is_home=True,
    is_b2b=False,
    missing_teammate=None,
    position="SF",
):
    """
    Master scoring function. Combines all factors using config weights
    into a single confidence score.

    Returns a dict with:
        - confidence: float 0.0–1.0
        - breakdown: dict of individual scores
        - factors: raw data used
    """
    player_id = get_player_id(player_name)
    team_id   = get_team_id(opponent_team)

    if not player_id:
        return {"error": f"Player not found: {player_name}"}
    if not team_id:
        return {"error": f"Team not found: {opponent_team}"}

    # Collect raw data for reasoning
    try:
        streak_data = get_streak_stats(player_id, prop_type, line)
    except Exception:
        streak_data = None
    try:
        matchup_data = get_player_vs_team(player_id, team_id, prop_type) if team_id else None
    except Exception:
        matchup_data = None
    try:
        rest_data = get_rest_stats(player_id, prop_type)
    except Exception:
        rest_data = None
    try:
        home_data = get_home_away_splits(player_id, prop_type)
    except Exception:
        home_data = None

    def _safe(fn, *args, fallback=0.5):
        try:
            return fn(*args)
        except Exception:
            return fallback

    scores = {
        "streak":        _safe(score_streak,    player_id, prop_type, line),
        "matchup":       _safe(score_matchup,   player_id, team_id, prop_type, line),
        "defense":       _safe(score_defense,   team_id, position, prop_type, line, player_id),
        "home_away":     _safe(score_home_away, player_id, prop_type, is_home),
        "rest":          _safe(score_rest,      player_id, prop_type, is_b2b),
        "usage":         _safe(score_usage,     player_id, prop_type, missing_teammate),
        "line_movement": 0.5,
    }

    confidence = sum(WEIGHTS[k] * scores[k] for k in WEIGHTS)
    reasons    = _build_reasoning(
        player_name, opponent_team, prop_type, line, is_home, is_b2b,
        missing_teammate, scores, streak_data, matchup_data, rest_data, home_data,
    )

    return {
        "player":     player_name,
        "opponent":   opponent_team,
        "prop":       prop_type,
        "line":       line,
        "confidence": round(confidence, 4),
        "breakdown":  {k: round(v, 4) for k, v in scores.items()},
        "reasons":    reasons,
    }


def _build_reasoning(
    player_name, opponent_team, prop_type, line, is_home, is_b2b,
    missing_teammate, scores, streak_data, matchup_data, rest_data, home_data,
):
    """
    Builds a list of plain-English reasons explaining why a bet looks good or bad.
    Each reason is a tuple of (sentiment, text) where sentiment is 'positive',
    'negative', or 'neutral'.
    """
    reasons = []
    prop_label = prop_type.replace("_", " ").replace("pts+reb+ast", "PRA")

    # Streak
    if streak_data:
        hit_rate = streak_data["hit_rate"]
        avg      = streak_data["avg"]
        n        = streak_data["games"]
        if hit_rate >= 0.7:
            reasons.append(("positive",
                f"Hit {prop_label} {line}+ in {int(hit_rate*n)}/{n} of last {n} games "
                f"(avg: {avg:.1f})"))
        elif hit_rate >= 0.5:
            reasons.append(("neutral",
                f"Hit {prop_label} {line}+ in {int(hit_rate*n)}/{n} of last {n} games "
                f"(avg: {avg:.1f})"))
        else:
            reasons.append(("negative",
                f"Only hit {prop_label} {line}+ in {int(hit_rate*n)}/{n} of last {n} games "
                f"(avg: {avg:.1f})"))

    # Matchup vs this team
    if matchup_data and matchup_data.get("reliable") and matchup_data.get("avg") is not None:
        avg = matchup_data["avg"]
        n   = matchup_data["games"]
        if avg > line:
            reasons.append(("positive",
                f"Averages {avg:.1f} {prop_label} vs {opponent_team} historically ({n} games)"))
        else:
            reasons.append(("negative",
                f"Only averages {avg:.1f} {prop_label} vs {opponent_team} historically ({n} games)"))

    # Defense
    def_score = scores.get("defense", 0.5)
    if def_score >= 0.65:
        reasons.append(("positive",
            f"{opponent_team} has a weak defense — favorable for counting stats"))
    elif def_score <= 0.35:
        reasons.append(("negative",
            f"{opponent_team} has a strong defense — harder to go over"))

    # Home/away
    if home_data and home_data.get("home_away_edge") is not None:
        edge = home_data["home_away_edge"]
        if abs(edge) >= 2.0:
            if is_home and edge > 0:
                reasons.append(("positive",
                    f"Averages {edge:.1f} more {prop_label} at home"))
            elif not is_home and edge < 0:
                reasons.append(("positive",
                    f"Performs similarly on the road for this prop"))
            elif is_home and edge < 0:
                reasons.append(("negative",
                    f"Performs {abs(edge):.1f} better {prop_label} on the road"))
            elif not is_home and edge > 0:
                reasons.append(("negative",
                    f"Averages {edge:.1f} fewer {prop_label} away vs home"))

    # Rest
    if is_b2b:
        if rest_data and rest_data.get("rest_edge") and rest_data["rest_edge"] >= 3.0:
            reasons.append(("negative",
                f"Back-to-back game — averages {rest_data['rest_edge']:.1f} fewer {prop_label} on b2b"))
        else:
            reasons.append(("neutral", "Back-to-back game — minimal rest impact historically"))

    # Missing teammate
    if missing_teammate:
        usage_score = scores.get("usage", 0.5)
        if usage_score >= 0.65:
            reasons.append(("positive",
                f"{missing_teammate} is out — {player_name} historically sees a usage boost"))
        elif usage_score <= 0.4:
            reasons.append(("negative",
                f"{missing_teammate} is out — minimal or negative impact on {player_name}'s stats"))

    return reasons
