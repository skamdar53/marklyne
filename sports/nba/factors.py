# sports/nba/factors.py — NBA scoring factors and SportModule implementation
# (scoring math moved from the original algorithm/scoring.py, unchanged)

from collections import defaultdict

import pandas as pd

from sports.base import SportModule
from sports.nba import data as nba
from sports.nba.config import (
    WEIGHTS, PROP_TYPES, PROP_TYPE_LABELS, PRIZEPICKS_PROP_MAP,
    POSITION_OPTIONS, PRIZEPICKS_LEAGUE_ID, N_GAMES,
)
from markets.prizepicks import get_prizepicks_lines


# ── Individual factor scoring functions ──────────────────────────────────────
# Each takes the context dict built by NBASport.build_context() and returns
# a float 0.0-1.0.

def score_streak(ctx):
    stats = nba.get_streak_stats(ctx["player_id"], ctx["prop_type"], ctx["line"])
    if not stats:
        return 0.5
    hit_rate = stats["hit_rate"]
    avg      = stats["avg"]
    line     = ctx["line"]
    avg_edge = (avg - line) / line if line > 0 else 0
    boost    = min(avg_edge * 0.3, 0.1)
    return min(hit_rate + boost, 1.0)


def score_matchup(ctx):
    matchup = nba.get_player_vs_team(ctx["player_id"], ctx["team_id"], ctx["prop_type"])
    if not matchup or not matchup["reliable"] or matchup["avg"] is None:
        return 0.5
    avg  = matchup["avg"]
    line = ctx["line"]
    if avg > line:
        edge = (avg - line) / line
        return min(0.5 + edge * 0.5, 0.95)
    edge = (line - avg) / line
    return max(0.5 - edge * 0.5, 0.05)


def score_defense(ctx):
    team_id   = ctx["team_id"]
    prop_type = ctx["prop_type"]
    player_id = ctx["player_id"]

    rating = nba.get_team_defensive_rating(team_id)
    pace   = nba.get_team_pace(team_id)
    if not rating or not pace:
        return 0.5

    def_score = max(min((rating["def_rating"] - 110) / 20 + 0.5, 1.0), 0.0)
    pace_score = max(min((pace["pace"] - 95) / 15 + 0.5, 1.0), 0.0)

    prop_score = 0.5
    opp_stats  = nba.get_opponent_prop_stats(team_id)
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
        prop_score = 1.0 - (rank - 1) / (total - 1)

    zone_score = 0.5
    if prop_type in ("points", "pts+reb+ast", "three_pointers_made") and player_id:
        zone_def     = nba.get_team_zone_defense(team_id)
        shot_profile = nba.get_player_shot_profile(player_id)
        if zone_def and shot_profile:
            perimeter = shot_profile["three_point_rate"]
            interior  = shot_profile["interior_scoring_share"]
            zone_boost = (perimeter * zone_def["perimeter_weakness"] +
                          interior  * zone_def["interior_weakness"])
            zone_score = max(min(0.5 + zone_boost * 2, 1.0), 0.0)

    return (def_score * 0.25) + (pace_score * 0.15) + (prop_score * 0.30) + (zone_score * 0.30)


def score_home_away(ctx):
    splits = nba.get_home_away_splits(ctx["player_id"], ctx["prop_type"])
    if not splits:
        return 0.5
    edge = splits["home_away_edge"]
    if ctx["is_home"]:
        return min(0.5 + edge / 10, 0.9)
    return max(0.5 - edge / 10, 0.1)


def score_rest(ctx):
    rest = nba.get_rest_stats(ctx["player_id"], ctx["prop_type"])
    if not rest or rest["rest_edge"] is None:
        return 0.5
    edge = rest["rest_edge"]
    if ctx["is_b2b"]:
        return max(0.5 - (edge / 10), 0.1)
    return min(0.5 + (edge / 10), 0.9)


def score_usage(ctx):
    missing_teammate = ctx.get("missing_teammate")
    if not missing_teammate:
        return 0.5
    impact = nba.get_performance_without_teammate(ctx["player_id"], missing_teammate, ctx["prop_type"])
    if not impact or impact["boost"] is None:
        return 0.5
    return min(0.5 + impact["boost"] / 15, 0.95)


def score_market_signal(ctx):
    """Cross-market (Kalshi/Polymarket) volume-weighted signal, neutral if unavailable."""
    return ctx.get("market_signal_score", 0.5)


FACTOR_FUNCS = {
    "streak":        score_streak,
    "matchup":       score_matchup,
    "defense":       score_defense,
    "home_away":     score_home_away,
    "rest":          score_rest,
    "usage":         score_usage,
    "market_signal": score_market_signal,
}


# ── Reasoning ─────────────────────────────────────────────────────────────────

def build_reasoning(ctx, scores):
    reasons = []
    prop_label = PROP_TYPE_LABELS.get(ctx["prop_type"], ctx["prop_type"])
    line = ctx["line"]

    streak_data = ctx.get("streak_data")
    if streak_data:
        hit_rate, avg, n = streak_data["hit_rate"], streak_data["avg"], streak_data["games"]
        if hit_rate >= 0.7:
            reasons.append(("positive", f"Hit {prop_label} {line}+ in {int(hit_rate*n)}/{n} of last {n} games (avg: {avg:.1f})"))
        elif hit_rate >= 0.5:
            reasons.append(("neutral", f"Hit {prop_label} {line}+ in {int(hit_rate*n)}/{n} of last {n} games (avg: {avg:.1f})"))
        else:
            reasons.append(("negative", f"Only hit {prop_label} {line}+ in {int(hit_rate*n)}/{n} of last {n} games (avg: {avg:.1f})"))

    matchup_data = ctx.get("matchup_data")
    if matchup_data and matchup_data.get("reliable") and matchup_data.get("avg") is not None:
        avg, n = matchup_data["avg"], matchup_data["games"]
        opponent = ctx["entry"]["opponent"]
        if avg > line:
            reasons.append(("positive", f"Averages {avg:.1f} {prop_label} vs {opponent} historically ({n} games)"))
        else:
            reasons.append(("negative", f"Only averages {avg:.1f} {prop_label} vs {opponent} historically ({n} games)"))

    def_score = scores.get("defense", 0.5)
    opponent = ctx["entry"]["opponent"]
    if def_score >= 0.65:
        reasons.append(("positive", f"{opponent} has a weak defense — favorable for counting stats"))
    elif def_score <= 0.35:
        reasons.append(("negative", f"{opponent} has a strong defense — harder to go over"))

    home_data = ctx.get("home_data")
    if home_data and home_data.get("home_away_edge") is not None:
        edge = home_data["home_away_edge"]
        is_home = ctx["is_home"]
        player_name = ctx["entry"]["player_name"]
        if abs(edge) >= 2.0:
            if is_home and edge > 0:
                reasons.append(("positive", f"Averages {edge:.1f} more {prop_label} at home"))
            elif not is_home and edge < 0:
                reasons.append(("positive", "Performs similarly on the road for this prop"))
            elif is_home and edge < 0:
                reasons.append(("negative", f"Performs {abs(edge):.1f} better {prop_label} on the road"))
            elif not is_home and edge > 0:
                reasons.append(("negative", f"Averages {edge:.1f} fewer {prop_label} away vs home"))

    if ctx["is_b2b"]:
        rest_data = ctx.get("rest_data")
        if rest_data and rest_data.get("rest_edge") and rest_data["rest_edge"] >= 3.0:
            reasons.append(("negative", f"Back-to-back game — averages {rest_data['rest_edge']:.1f} fewer {prop_label} on b2b"))
        else:
            reasons.append(("neutral", "Back-to-back game — minimal rest impact historically"))

    missing_teammate = ctx.get("missing_teammate")
    if missing_teammate:
        usage_score = scores.get("usage", 0.5)
        player_name = ctx["entry"]["player_name"]
        if usage_score >= 0.65:
            reasons.append(("positive", f"{missing_teammate} is out — {player_name} historically sees a usage boost"))
        elif usage_score <= 0.4:
            reasons.append(("negative", f"{missing_teammate} is out — minimal or negative impact on {player_name}'s stats"))

    market_score = scores.get("market_signal", 0.5)
    if abs(market_score - 0.5) >= 0.1:
        if market_score > 0.5:
            reasons.append(("positive", "Prediction markets (Kalshi/Polymarket) lean the same direction as this prop"))
        else:
            reasons.append(("negative", "Prediction markets (Kalshi/Polymarket) lean against this prop"))

    return reasons


# ── SportModule implementation ────────────────────────────────────────────────

class NBASport(SportModule):
    key = "nba"
    label = "NBA"
    icon = "🏀"

    prizepicks_league_id = PRIZEPICKS_LEAGUE_ID
    prop_types = PROP_TYPES
    prop_type_labels = PROP_TYPE_LABELS
    position_options = POSITION_OPTIONS

    weights = WEIGHTS
    factor_funcs = FACTOR_FUNCS

    def build_context(self, entry):
        player_name = entry.get("player_name")
        opponent    = entry.get("opponent")

        player_id = nba.get_player_id(player_name)
        team_id   = nba.get_team_id(opponent)

        if not player_id:
            return {"error": f"Player not found: {player_name}"}
        if not team_id:
            return {"error": f"Team not found: {opponent}"}

        prop_type = entry.get("prop_type")
        try:
            streak_data = nba.get_streak_stats(player_id, prop_type, entry.get("line"))
        except Exception:
            streak_data = None
        try:
            matchup_data = nba.get_player_vs_team(player_id, team_id, prop_type)
        except Exception:
            matchup_data = None
        try:
            rest_data = nba.get_rest_stats(player_id, prop_type)
        except Exception:
            rest_data = None
        try:
            home_data = nba.get_home_away_splits(player_id, prop_type)
        except Exception:
            home_data = None

        return {
            "entry":              entry,
            "player_id":          player_id,
            "team_id":            team_id,
            "prop_type":          prop_type,
            "line":               entry.get("line"),
            "is_home":            entry.get("is_home", True),
            "is_b2b":             entry.get("is_b2b", False),
            "position":           entry.get("position", "SF"),
            "missing_teammate":   entry.get("missing_teammate"),
            "market_signal_score": entry.get("market_signal_score", 0.5),
            "streak_data":        streak_data,
            "matchup_data":       matchup_data,
            "rest_data":          rest_data,
            "home_data":          home_data,
        }

    def build_reasoning(self, context, scores):
        return build_reasoning(context, scores)

    def get_todays_games(self):
        return nba.get_todays_games()

    def build_auto_slate(self, missing_teammates=None, lines=None):
        if missing_teammates is None:
            missing_teammates = {}

        games            = nba.get_todays_games()
        team_lookup      = nba.build_team_lookup()
        played_yesterday = nba.get_yesterdays_teams()

        playing_today = set()
        home_teams    = set()
        for g in games:
            playing_today.add(g["home_team"])
            playing_today.add(g["away_team"])
            home_teams.add(g["home_team"])

        opponent_map = {}
        for g in games:
            opponent_map[g["home_team"]] = g["away_team"]
            opponent_map[g["away_team"]] = g["home_team"]

        if lines is None:
            lines = get_prizepicks_lines(PRIZEPICKS_LEAGUE_ID, PRIZEPICKS_PROP_MAP, PROP_TYPES)

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
            median_line = line_values[len(line_values) // 2]
            entry = dict(meta[key])
            entry["line"] = median_line
            slate.append(entry)

        return sorted(slate, key=lambda x: x["player_name"])

    def backtest_games(self, player_name, opponent_team, prop_type, position=None):
        player_id = nba.get_player_id(player_name)
        team_id   = nba.get_team_id(opponent_team)

        if not player_id:
            yield {"game_date": "?", "skip_reason": f"player not found: {player_name}"}
            return

        seasons = ["2024-25", "2023-24", "2022-23"]
        all_logs = nba.get_multi_season_logs(player_id, seasons)
        if all_logs is None:
            yield {"game_date": "?", "skip_reason": f"no game logs found for {player_name}"}
            return

        opp_abbr = nba.get_team_abbr(team_id) if team_id else opponent_team
        opp_logs = all_logs[all_logs["MATCHUP"].str.contains(opp_abbr, case=False, na=False)]
        if opp_logs.empty:
            yield {"game_date": "?", "skip_reason": f"no games found vs {opponent_team}"}
            return

        try:
            for idx, game_row in opp_logs.iterrows():
                game_date = game_row.get("GAME_DATE", "?")
                logs_before = all_logs[all_logs.index < idx].tail(N_GAMES)
                if len(logs_before) < 3:
                    yield {"game_date": game_date, "skip_reason": f"not enough prior games ({len(logs_before)})"}
                    continue

                values = [nba.extract_stat(row, prop_type) for _, row in logs_before.iterrows()]
                values = [v for v in values if v is not None]
                if not values:
                    yield {"game_date": game_date, "skip_reason": "could not infer line"}
                    continue
                line = round((sum(values) / len(values)) * 2) / 2

                nba.seed_game_log_cache(player_id, logs_before)

                entry = {
                    "player_name": player_name,
                    "opponent":    opponent_team,
                    "prop_type":   prop_type,
                    "line":        line,
                    "position":    position or "SF",
                }
                result = calculate_confidence(self, entry)
                if "error" in result:
                    yield {"game_date": game_date, "skip_reason": result["error"]}
                    continue

                actual = nba.extract_stat(game_row, prop_type)
                if actual is None:
                    yield {"game_date": game_date, "skip_reason": "no actual stat"}
                    continue

                yield {
                    "game_date":  game_date,
                    "line":       line,
                    "actual":     actual,
                    "hit":        actual > line,
                    "confidence": result["confidence"],
                    "reasons":    result.get("reasons", []),
                }
        finally:
            # Un-seed so a backtest run doesn't leak stale historical data
            # into subsequent live scoring calls for this player.
            nba._game_log_cache.pop(player_id, None)


# Imported at the bottom to avoid a circular import at module load time
# (core.scoring imports nothing from sports, so this is safe).
from core.scoring import calculate_confidence  # noqa: E402
