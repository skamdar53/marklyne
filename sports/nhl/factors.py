# sports/nhl/factors.py — NHL scoring factors and SportModule implementation
#
# Skaters and goalies share the same factor names but take different paths
# inside most of them: for a skater, a weak opposing defense/goalie is good for
# the over, while for a goalie facing a high-volume offense is what drives
# saves. ctx["is_goalie"] is the switch.

import pandas as pd

from sports.base import SportModule
from sports.nhl import data as nhl
from sports.nhl.config import (
    WEIGHTS, PROP_TYPES, PROP_TYPE_LABELS, GOALIE_PROP_TYPES,
    POSITION_OPTIONS, N_GAMES, SEASON, BACKTEST_SEASONS,
)
from markets.lines import get_market_prop_lines


def _clamp(value, low=0.05, high=0.95):
    return max(low, min(value, high))


def _rank_score(rank, total):
    """Rank 1 (the most of whatever was ranked) maps to 1.0."""
    if total <= 1:
        return 0.5
    return 1.0 - (rank - 1) / (total - 1)


# ── Individual factor scoring functions ──────────────────────────────────────
# Each takes the context dict built by NHLSport.build_context() and returns
# a float 0.0-1.0.

def score_streak(ctx):
    stats = nhl.get_streak_stats(ctx["player_id"], ctx["prop_type"], ctx["line"])
    if not stats:
        return 0.5
    line = ctx["line"]
    avg_edge = (stats["avg"] - line) / line if line > 0 else 0
    boost = min(avg_edge * 0.3, 0.1)
    return min(stats["hit_rate"] + boost, 1.0)


def score_matchup(ctx):
    matchup = nhl.get_player_vs_team(ctx["player_id"], ctx["opp_abbr"], ctx["prop_type"])
    if not matchup or not matchup["reliable"] or matchup["avg"] is None:
        return 0.5
    avg, line = matchup["avg"], ctx["line"]
    if line <= 0:
        return 0.5
    if avg > line:
        return min(0.5 + (avg - line) / line * 0.5, 0.95)
    return max(0.5 - (line - avg) / line * 0.5, 0.05)


# How much of the opponent-defense score comes from shot volume vs goal
# prevention, per prop. Shot props barely care how many goals a team allows.
_DEFENSE_SHOT_SHARE = {
    "shots_on_goal":     0.80,
    "points":            0.35,
    "goals":             0.20,
    "assists":           0.35,
    "power_play_points": 0.25,
}


def score_opp_defense(ctx):
    opp = nhl.get_team_stats(ctx["opp_abbr"], ctx["season"])
    if not opp:
        return 0.5
    total = opp["total_teams"]

    # Goalie saves are a volume stat: what matters is how many shots the
    # opponent generates, not how well they defend.
    if ctx["is_goalie"]:
        return _rank_score(opp["shots_for_rank"], total)

    shots_score = _rank_score(opp["shots_against_rank"], total)
    goals_score = _rank_score(opp["goals_against_rank"], total)
    share = _DEFENSE_SHOT_SHARE.get(ctx["prop_type"], 0.4)
    return shots_score * share + goals_score * (1 - share)


def score_goalie_edge(ctx):
    baseline = nhl.get_league_goalie_baseline(ctx["season"])

    if ctx["is_goalie"]:
        quality = nhl.get_goalie_quality(ctx["player_id"], ctx["season"])
        if not quality:
            return 0.5
        # A goalie who stops a higher share of what he faces banks saves
        # instead of goals against on the same workload.
        return _clamp(0.5 + (quality["save_pct"] - baseline) * 12, 0.15, 0.85)

    # Shots on goal are recorded whether or not they're stopped, so the
    # opposing goalie is irrelevant to that prop.
    if ctx["prop_type"] == "shots_on_goal":
        return 0.5

    quality = nhl.get_starting_goalie_quality(ctx["opp_abbr"], ctx["season"])
    if not quality:
        return 0.5
    save_edge = (baseline - quality["save_pct"]) * 15
    gsax_edge = -quality["gsax_per_60"] * 0.15
    return _clamp(0.5 + save_edge + gsax_edge, 0.15, 0.85)


def score_possession(ctx):
    team = nhl.get_team_possession(ctx["team_abbr"], ctx["season"])
    opp  = nhl.get_team_possession(ctx["opp_abbr"], ctx["season"])
    if not team or not opp:
        return 0.5

    corsi_edge   = team["corsi_pct"] - opp["corsi_pct"]
    fenwick_edge = team["fenwick_pct"] - opp["fenwick_pct"]
    edge = corsi_edge * 0.6 + fenwick_edge * 0.4

    # Being out-attempted is bad for a skater's counting stats and good for a
    # goalie's save total.
    if ctx["is_goalie"]:
        edge = -edge
    return _clamp(0.5 + edge * 4, 0.10, 0.90)


# Special teams swing power-play production far more than even-strength props.
_SPECIAL_TEAMS_SCALE = {
    "power_play_points": 6.0,
    "goals":             3.0,
    "points":            3.0,
    "assists":           2.5,
    "shots_on_goal":     1.5,
}


def score_special_teams(ctx):
    opp = nhl.get_team_stats(ctx["opp_abbr"], ctx["season"])
    if not opp:
        return 0.5

    if ctx["is_goalie"]:
        # A dangerous opposing power play means more time defending and more
        # shots faced.
        return _clamp(0.5 + (opp["power_play_pct"] - opp["league_power_play"]) * 3, 0.2, 0.8)

    own = nhl.get_team_stats(ctx["team_abbr"], ctx["season"]) if ctx["team_abbr"] else None
    pk_edge = opp["league_penalty_kill"] - opp["penalty_kill_pct"]
    pp_edge = (own["power_play_pct"] - own["league_power_play"]) if own else 0.0

    scale = _SPECIAL_TEAMS_SCALE.get(ctx["prop_type"], 2.0)
    return _clamp(0.5 + (pk_edge * 0.6 + pp_edge * 0.4) * scale, 0.10, 0.90)


def _relative_edge(edge, level):
    """
    NHL counting stats are small and vary wildly in scale (0.6 goals vs 28
    saves), so splits are scored as a fraction of the player's own output
    rather than in raw units.
    """
    if not level or level <= 0:
        return 0.0
    return edge / level


def score_home_away(ctx):
    splits = ctx.get("home_data")
    if not splits or splits.get("home_away_edge") is None:
        return 0.5
    level = (splits["home_avg"] + splits["away_avg"]) / 2
    rel = _relative_edge(splits["home_away_edge"], level)
    if ctx["is_home"]:
        return _clamp(0.5 + rel * 0.5, 0.15, 0.85)
    return _clamp(0.5 - rel * 0.5, 0.15, 0.85)


def score_rest(ctx):
    rest = ctx.get("rest_data")
    if not rest or rest.get("rest_edge") is None:
        return 0.5
    rel = _relative_edge(rest["rest_edge"], rest["rested_avg"])
    if ctx["is_b2b"]:
        return _clamp(0.5 - rel * 0.5, 0.15, 0.85)
    return _clamp(0.5 + rel * 0.5, 0.15, 0.85)


def score_usage(ctx):
    missing = ctx.get("missing_teammate")
    if not missing:
        return 0.5
    impact = nhl.get_performance_without_teammate(ctx["player_id"], missing, ctx["prop_type"])
    if not impact or impact["boost"] is None or impact["sample_without"] < 3:
        return 0.5
    rel = _relative_edge(impact["boost"], impact["with_avg"])
    return _clamp(0.5 + rel * 0.5, 0.15, 0.95)


def score_market_signal(ctx):
    """Cross-market (Kalshi/Polymarket) volume-weighted signal, neutral if unavailable."""
    return ctx.get("market_signal_score", 0.5)


FACTOR_FUNCS = {
    "streak":        score_streak,
    "matchup":       score_matchup,
    "opp_defense":   score_opp_defense,
    "goalie_edge":   score_goalie_edge,
    "possession":    score_possession,
    "special_teams": score_special_teams,
    "home_away":     score_home_away,
    "rest":          score_rest,
    "usage":         score_usage,
    "market_signal": score_market_signal,
}


# ── Reasoning ─────────────────────────────────────────────────────────────────

def build_reasoning(ctx, scores):
    reasons = []
    prop_label = PROP_TYPE_LABELS.get(ctx["prop_type"], ctx["prop_type"])
    line       = ctx["line"]
    opponent   = ctx["entry"]["opponent"]
    player     = ctx["entry"]["player_name"]

    streak = ctx.get("streak_data")
    if streak:
        n, avg, hit_rate = streak["games"], streak["avg"], streak["hit_rate"]
        over = streak["over_count"]
        if hit_rate >= 0.7:
            reasons.append(("positive", f"Hit {prop_label} {line}+ in {over}/{n} of last {n} games (avg: {avg:.1f})"))
        elif hit_rate >= 0.5:
            reasons.append(("neutral", f"Hit {prop_label} {line}+ in {over}/{n} of last {n} games (avg: {avg:.1f})"))
        else:
            reasons.append(("negative", f"Only hit {prop_label} {line}+ in {over}/{n} of last {n} games (avg: {avg:.1f})"))
        if streak.get("toi_avg"):
            reasons.append(("neutral", f"Averaging {streak['toi_avg']:.1f} min of ice time over that stretch"))

    matchup = ctx.get("matchup_data")
    if matchup and matchup.get("reliable") and matchup.get("avg") is not None:
        avg, n = matchup["avg"], matchup["games"]
        if avg > line:
            reasons.append(("positive", f"Averages {avg:.1f} {prop_label} vs {opponent} historically ({n} games)"))
        else:
            reasons.append(("negative", f"Only averages {avg:.1f} {prop_label} vs {opponent} historically ({n} games)"))

    opp_stats = ctx.get("opp_stats")
    if opp_stats:
        total = opp_stats["total_teams"]
        if ctx["is_goalie"]:
            rank = opp_stats["shots_for_rank"]
            if rank <= 10:
                reasons.append(("positive", f"{opponent} is {rank}/{total} in shots generated ({opp_stats['shots_for_per_game']:.1f}/gm) — heavy workload"))
            elif rank >= total - 9:
                reasons.append(("negative", f"{opponent} is a low-volume offense ({opp_stats['shots_for_per_game']:.1f} shots/gm) — fewer save chances"))
        else:
            rank = opp_stats["shots_against_rank"]
            if rank <= 10:
                reasons.append(("positive", f"{opponent} allows {opp_stats['shots_against_per_game']:.1f} shots/gm ({rank}/{total} most) — high-event matchup"))
            elif rank >= total - 9:
                reasons.append(("negative", f"{opponent} suppresses shots well ({opp_stats['shots_against_per_game']:.1f}/gm) — tough matchup"))

    goalie_score = scores.get("goalie_edge", 0.5)
    opp_goalie = ctx.get("opp_goalie")
    if not ctx["is_goalie"] and opp_goalie and ctx["prop_type"] != "shots_on_goal":
        if goalie_score >= 0.6:
            reasons.append(("positive", f"{opp_goalie.get('name', 'Opposing goalie')} is below league average ({opp_goalie['save_pct']:.3f} SV%, {opp_goalie['gsax']:+.1f} GSAx)"))
        elif goalie_score <= 0.4:
            reasons.append(("negative", f"{opp_goalie.get('name', 'Opposing goalie')} is stingy ({opp_goalie['save_pct']:.3f} SV%, {opp_goalie['gsax']:+.1f} GSAx)"))

    possession_score = scores.get("possession", 0.5)
    team_poss, opp_poss = ctx.get("team_possession"), ctx.get("opp_possession")
    if team_poss and opp_poss:
        if possession_score >= 0.62:
            if ctx["is_goalie"]:
                reasons.append(("positive", f"Gets out-attempted on balance ({team_poss['corsi_pct']:.1%} Corsi vs {opp_poss['corsi_pct']:.1%}) — shots pile up"))
            else:
                reasons.append(("positive", f"Owns the shot-attempt edge ({team_poss['corsi_pct']:.1%} Corsi vs {opp_poss['corsi_pct']:.1%})"))
        elif possession_score <= 0.38:
            if ctx["is_goalie"]:
                reasons.append(("negative", f"Team drives play ({team_poss['corsi_pct']:.1%} Corsi vs {opp_poss['corsi_pct']:.1%}) — light workload expected"))
            else:
                reasons.append(("negative", f"Cedes the shot-attempt battle ({team_poss['corsi_pct']:.1%} Corsi vs {opp_poss['corsi_pct']:.1%})"))

    st_score = scores.get("special_teams", 0.5)
    if opp_stats and not ctx["is_goalie"]:
        if st_score >= 0.6:
            reasons.append(("positive", f"{opponent}'s penalty kill is leaky ({opp_stats['penalty_kill_pct']:.1%})"))
        elif st_score <= 0.4:
            reasons.append(("negative", f"{opponent}'s penalty kill is strong ({opp_stats['penalty_kill_pct']:.1%})"))

    home = ctx.get("home_data")
    if home and home.get("home_away_edge") is not None and abs(home["home_away_edge"]) >= 0.2:
        edge = home["home_away_edge"]
        if ctx["is_home"] and edge > 0:
            reasons.append(("positive", f"Averages {edge:.1f} more {prop_label} at home"))
        elif not ctx["is_home"] and edge < 0:
            reasons.append(("positive", f"Averages {abs(edge):.1f} more {prop_label} on the road"))
        elif ctx["is_home"] and edge < 0:
            reasons.append(("negative", f"Produces {abs(edge):.1f} more {prop_label} on the road"))
        else:
            reasons.append(("negative", f"Averages {edge:.1f} fewer {prop_label} away vs home"))

    if ctx["is_b2b"]:
        rest = ctx.get("rest_data")
        if rest and rest.get("rest_edge") and rest["rest_edge"] > 0:
            reasons.append(("negative", f"Back-to-back — averages {rest['rest_edge']:.1f} fewer {prop_label} on zero rest"))
        else:
            reasons.append(("neutral", "Back-to-back game — no meaningful drop off historically"))

    missing = ctx.get("missing_teammate")
    if missing:
        usage_score = scores.get("usage", 0.5)
        if usage_score >= 0.6:
            reasons.append(("positive", f"{missing} is out — {player} historically sees a usage bump"))
        elif usage_score <= 0.4:
            reasons.append(("negative", f"{missing} is out — {player} loses a play driver on his line"))

    market_score = scores.get("market_signal", 0.5)
    if abs(market_score - 0.5) >= 0.1:
        if market_score > 0.5:
            reasons.append(("positive", "Prediction markets (Kalshi/Polymarket) lean the same direction as this prop"))
        else:
            reasons.append(("negative", "Prediction markets (Kalshi/Polymarket) lean against this prop"))

    return reasons


# ── SportModule implementation ────────────────────────────────────────────────

class NHLSport(SportModule):
    key = "nhl"
    label = "NHL"
    icon = "🏒"

    prop_types = PROP_TYPES
    prop_type_labels = PROP_TYPE_LABELS
    position_options = POSITION_OPTIONS

    weights = WEIGHTS
    factor_funcs = FACTOR_FUNCS

    def build_context(self, entry):
        player_name = entry.get("player_name")
        opponent    = entry.get("opponent")
        prop_type   = entry.get("prop_type")

        info = nhl.get_player_info(player_name)
        if not info:
            return {"error": f"Player not found: {player_name}"}

        opp_abbr = nhl.get_team_abbr(opponent)
        if not opp_abbr:
            return {"error": f"Team not found: {opponent}"}

        player_id = info["player_id"]
        team_abbr = nhl.get_team_abbr(entry.get("team")) or info["team"] or None
        position  = entry.get("position") or info["position"] or "C"
        season    = entry.get("season", SEASON)
        is_goalie = prop_type in GOALIE_PROP_TYPES or position.upper() == "G"

        def _safe(fn, *args):
            try:
                return fn(*args)
            except Exception:
                return None

        return {
            "entry":               entry,
            "player_id":           player_id,
            "position":            position,
            "team_abbr":           team_abbr,
            "opp_abbr":            opp_abbr,
            "prop_type":           prop_type,
            "line":                entry.get("line"),
            "season":              season,
            "is_goalie":           is_goalie,
            "is_home":             entry.get("is_home", True),
            "is_b2b":              entry.get("is_b2b", False),
            "missing_teammate":    entry.get("missing_teammate"),
            "market_signal_score": entry.get("market_signal_score", 0.5),
            "streak_data":         _safe(nhl.get_streak_stats, player_id, prop_type, entry.get("line")),
            "matchup_data":        _safe(nhl.get_player_vs_team, player_id, opp_abbr, prop_type),
            "rest_data":           _safe(nhl.get_rest_stats, player_id, prop_type),
            "home_data":           _safe(nhl.get_home_away_splits, player_id, prop_type),
            "opp_stats":           _safe(nhl.get_team_stats, opp_abbr, season),
            "team_possession":     _safe(nhl.get_team_possession, team_abbr, season),
            "opp_possession":      _safe(nhl.get_team_possession, opp_abbr, season),
            "opp_goalie":          _safe(nhl.get_starting_goalie_quality, opp_abbr, season),
        }

    def build_reasoning(self, context, scores):
        return build_reasoning(context, scores)

    def get_todays_games(self):
        return nhl.get_todays_games()

    def build_auto_slate(self, missing_teammates=None, lines=None):
        if missing_teammates is None:
            missing_teammates = {}

        games            = nhl.get_todays_games()
        played_yesterday = nhl.get_yesterdays_teams()

        playing_today = set()
        home_teams    = set()
        opponent_map  = {}
        for g in games:
            playing_today.update((g["home_team"], g["away_team"]))
            home_teams.add(g["home_team"])
            opponent_map[g["home_team"]] = g["away_team"]
            opponent_map[g["away_team"]] = g["home_team"]

        if lines is None:
            lines = get_market_prop_lines("nhl", PROP_TYPES)

        # Kalshi/Polymarket prop rows carry neither a team nor a position the
        # way PrizePicks' did, so resolve both per player and cache them —
        # one player usually shows up across several props.
        player_cache = {}

        slate = []
        for line in lines:
            player_name = line["player_name"]
            if player_name not in player_cache:
                info = nhl.get_player_info(player_name)
                player_cache[player_name] = {
                    "team":     nhl.get_player_team(player_name),
                    "position": (info or {}).get("position", ""),
                }
            resolved = player_cache[player_name]
            team_abbr = resolved["team"]

            if not team_abbr or team_abbr not in playing_today:
                continue

            opponent = opponent_map.get(team_abbr, "")
            if not opponent:
                continue

            # No de-duping pass here: markets/lines.py already collapses the
            # same bet quoted on both platforms, and the distinct thresholds
            # it does keep are genuinely different bets, not noise to median
            # away the way duplicate PrizePicks rows were.
            slate.append({
                "player_name": player_name,
                "team":        team_abbr,
                "opponent":    opponent,
                "prop_type":   line["prop_type"],
                "line":        line["line"],
                "is_home":     team_abbr in home_teams,
                "is_b2b":      team_abbr in played_yesterday,
                "position":    resolved["position"],
            })

        return sorted(slate, key=lambda x: x["player_name"])

    def backtest_games(self, player_name, opponent_team, prop_type, position=None):
        info = nhl.get_player_info(player_name)
        if not info:
            yield {"game_date": "?", "skip_reason": f"player not found: {player_name}"}
            return

        player_id = info["player_id"]
        opp_abbr  = nhl.get_team_abbr(opponent_team) or opponent_team.upper()

        all_logs = nhl.get_multi_season_logs(player_id, BACKTEST_SEASONS)
        if all_logs is None:
            yield {"game_date": "?", "skip_reason": f"no game logs found for {player_name}"}
            return

        opp_logs = all_logs[all_logs["opponentAbbrev"].str.upper() == opp_abbr]
        if opp_logs.empty:
            yield {"game_date": "?", "skip_reason": f"no games found vs {opponent_team}"}
            return

        try:
            for idx, game_row in opp_logs.iterrows():
                game_date = game_row["gameDate"].strftime("%Y-%m-%d")
                prior = all_logs[all_logs.index < idx]
                logs_before = prior.tail(N_GAMES)
                if len(logs_before) < 3:
                    yield {"game_date": game_date, "skip_reason": f"not enough prior games ({len(logs_before)})"}
                    continue

                values = [nhl.extract_stat(row, prop_type) for _, row in logs_before.iterrows()]
                values = [v for v in values if v is not None and not pd.isna(v)]
                if not values:
                    yield {"game_date": game_date, "skip_reason": "could not infer line"}
                    continue

                # Sportsbook proxy: trailing average snapped to the nearest
                # half-integer, matching how prop lines are actually posted —
                # with hockey's small counting stats a whole-number line would
                # turn frequent pushes into recorded misses.
                avg = sum(values) / len(values)
                line = max(round(avg - 0.5) + 0.5, 0.5)

                nhl.seed_game_log_cache(player_id, logs_before)

                days_rest = (game_row["gameDate"] - prior.iloc[-1]["gameDate"]).days
                entry = {
                    "player_name": player_name,
                    "team":        game_row.get("teamAbbrev"),
                    "opponent":    opponent_team,
                    "prop_type":   prop_type,
                    "line":        line,
                    "position":    position or info["position"] or "C",
                    "is_home":     game_row.get("homeRoadFlag") == "H",
                    "is_b2b":      days_rest <= 1,
                    # Team/goalie factors are pinned to the season the game was
                    # played in — season-level aggregates rather than to-date
                    # ones, but they never reach forward into a later season.
                    "season":      game_row.get("season", SEASON),
                }

                result = calculate_confidence(self, entry)
                if "error" in result:
                    yield {"game_date": game_date, "skip_reason": result["error"]}
                    continue

                actual = nhl.extract_stat(game_row, prop_type)
                if actual is None or pd.isna(actual):
                    yield {"game_date": game_date, "skip_reason": "no actual stat"}
                    continue

                yield {
                    "game_date":  game_date,
                    "line":       line,
                    "actual":     float(actual),
                    "hit":        bool(actual > line),
                    "confidence": result["confidence"],
                    "reasons":    result.get("reasons", []),
                }
        finally:
            # Drop the seeded window so live scoring after a backtest doesn't
            # keep reading a truncated, historical game log.
            nhl._game_log_cache.pop(player_id, None)


# Imported at the bottom to avoid a circular import at module load time
# (core.scoring imports nothing from sports, so this is safe).
from core.scoring import calculate_confidence  # noqa: E402
