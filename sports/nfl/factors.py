# sports/nfl/factors.py — NFL scoring factors and SportModule implementation

from collections import defaultdict

import pandas as pd

from sports.base import SportModule
from sports.nfl import data as nfl
from sports.nfl.config import (
    WEIGHTS, PROP_TYPES, PROP_TYPE_LABELS, PRIZEPICKS_PROP_MAP,
    POSITION_OPTIONS, PRIZEPICKS_LEAGUE_ID, N_GAMES, BACKTEST_SEASONS,
    BACKTEST_WINDOW, SHORT_WEEK_REST, LONG_REST,
)
from markets.prizepicks import get_prizepicks_lines


def _clamp(value, low=0.0, high=1.0):
    return max(min(value, high), low)


def _ordinal(n):
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


# Typical target share for a starter at each position — the reference point the
# usage factor scores a player's actual share against.
_TARGET_SHARE_BASELINE = {"WR": 0.20, "TE": 0.16, "RB": 0.11}

_POSITION_MAP = {"QB": "QB", "RB": "RB", "FB": "RB", "HB": "RB", "WR": "WR", "TE": "TE"}


def normalize_position(position):
    return _POSITION_MAP.get(str(position).upper(), None)


# ── Individual factor scoring functions ──────────────────────────────────────
# Each takes the context dict built by NFLSport.build_context() and returns
# a float 0.0-1.0.

def score_form(ctx):
    stats = ctx.get("form_data")
    if not stats:
        return 0.5
    line = ctx["line"]
    avg_edge = (stats["avg"] - line) / line if line > 0 else 0
    boost = _clamp(avg_edge * 0.3, -0.1, 0.1)
    return _clamp(stats["hit_rate"] + boost)


def score_matchup(ctx):
    matchup = ctx.get("matchup_data")
    if not matchup or not matchup["reliable"] or matchup["avg"] is None:
        return 0.5
    avg, line = matchup["avg"], ctx["line"]
    if line <= 0:
        return 0.5
    edge = (avg - line) / line
    if edge >= 0:
        return min(0.5 + edge * 0.5, 0.95)
    return max(0.5 + edge * 0.5, 0.05)


def score_defense(ctx):
    dvp = ctx.get("defense_data")
    if not dvp:
        return 0.5

    # rank 1 allows the most to this position, so rank 1 -> 1.0
    total = dvp["total_teams"]
    rank_score = 1.0 - (dvp["rank"] - 1) / (total - 1) if total > 1 else 0.5

    league = dvp["league_avg"]
    edge = (dvp["allowed_avg"] - league) / league if league > 0 else 0
    edge_score = _clamp(0.5 + edge * 1.5)

    return rank_score * 0.6 + edge_score * 0.4


def score_usage(ctx):
    """Is this player actually getting the volume that drives this prop, lately?"""
    usage = ctx.get("usage_data")
    if not usage:
        return 0.5

    score = 0.5

    baseline = usage["volume_baseline"]
    if baseline > 0:
        trend = usage["volume_recent"] / baseline - 1
        score += _clamp(trend, -0.5, 0.5) * 0.4

    share_baseline = _TARGET_SHARE_BASELINE.get(ctx["position"])
    if share_baseline and usage["target_share"] > 0:
        score += _clamp((usage["target_share"] - share_baseline) / share_baseline, -1, 1) * 0.12

    if ctx["prop_type"] in ("receiving_yards", "rec+rush_yards") and usage["air_yards_share"] > 0:
        score += _clamp((usage["air_yards_share"] - 0.20) / 0.20, -1, 1) * 0.08

    if usage["snap_pct"] is not None:
        score += _clamp((usage["snap_pct"] - 0.70) / 0.30, -1, 1) * 0.10

    return _clamp(score, 0.05, 0.95)


def score_teammate_out(ctx):
    """
    A missing target hog is the strongest single-week usage signal in football.
    Prefer the player's own observed with/without split; when that sample is too
    thin (it usually is), fall back to how much target share is being vacated.
    """
    impact = ctx.get("teammate_data")
    if not impact:
        return 0.5

    line = ctx["line"]
    if impact["boost"] is not None and line > 0:
        return _clamp(0.5 + (impact["boost"] / line) * 1.2, 0.05, 0.95)

    vacated = impact.get("vacated_share")
    if vacated:
        return _clamp(0.5 + min(vacated, 0.35) * 0.8, 0.5, 0.85)
    return 0.5


def score_rest(ctx):
    rest_days = ctx.get("rest_days")
    if rest_days is None:
        return 0.5

    splits = ctx.get("rest_data")
    line = ctx["line"]
    if splits and splits["short_games"] >= 2 and line > 0:
        # rest_edge > 0 means the player produces less on short weeks
        rel = _clamp(splits["rest_edge"] / line, -0.6, 0.6)
        if rest_days <= SHORT_WEEK_REST:
            return _clamp(0.5 - rel * 0.5, 0.1, 0.9)
        # Not a short week, so the split only mildly nudges the normal-rest case
        return _clamp(0.5 + rel * 0.15, 0.35, 0.65)

    if rest_days <= SHORT_WEEK_REST:
        return 0.38
    if rest_days >= LONG_REST:
        return 0.58
    return 0.5


def score_home_away(ctx):
    splits = ctx.get("home_data")
    if not splits:
        return 0.5
    line = ctx["line"]
    if line <= 0:
        return 0.5
    rel = splits["home_away_edge"] / line
    if ctx["is_home"]:
        return _clamp(0.5 + rel * 0.8, 0.1, 0.9)
    return _clamp(0.5 - rel * 0.8, 0.1, 0.9)


def score_market_signal(ctx):
    """Cross-market (Kalshi/Polymarket) volume-weighted signal, neutral if unavailable."""
    return ctx.get("market_signal_score", 0.5)


FACTOR_FUNCS = {
    "form":          score_form,
    "defense":       score_defense,
    "usage":         score_usage,
    "matchup":       score_matchup,
    "teammate_out":  score_teammate_out,
    "rest":          score_rest,
    "home_away":     score_home_away,
    "market_signal": score_market_signal,
}


# ── Reasoning ─────────────────────────────────────────────────────────────────

def build_reasoning(ctx, scores):
    reasons = []
    prop_label = PROP_TYPE_LABELS.get(ctx["prop_type"], ctx["prop_type"])
    line = ctx["line"]
    opponent = ctx["entry"]["opponent"]
    player_name = ctx["entry"]["player_name"]

    form = ctx.get("form_data")
    if form:
        n, over, avg = form["games"], form["over_count"], form["avg"]
        if form["hit_rate"] >= 0.7:
            reasons.append(("positive", f"Cleared {prop_label} {line} in {over}/{n} of last {n} games (avg: {avg:.1f})"))
        elif form["hit_rate"] >= 0.5:
            reasons.append(("neutral", f"Cleared {prop_label} {line} in {over}/{n} of last {n} games (avg: {avg:.1f})"))
        else:
            reasons.append(("negative", f"Only cleared {prop_label} {line} in {over}/{n} of last {n} games (avg: {avg:.1f})"))

    dvp = ctx.get("defense_data")
    if dvp:
        rank, total, allowed = dvp["rank"], dvp["total_teams"], dvp["allowed_avg"]
        if rank <= total / 3:
            reasons.append(("positive", f"{opponent} allows {allowed:.1f} {prop_label} to {ctx['position']}s — {_ordinal(rank)} most in the league"))
        elif rank >= total * 2 / 3:
            reasons.append(("negative", f"{opponent} allows only {allowed:.1f} {prop_label} to {ctx['position']}s — {_ordinal(total - rank + 1)} stingiest"))
        else:
            reasons.append(("neutral", f"{opponent} is middle of the pack vs {ctx['position']}s ({allowed:.1f} {prop_label} allowed)"))

    usage = ctx.get("usage_data")
    if usage and usage["volume_baseline"] > 0:
        trend = usage["volume_recent"] / usage["volume_baseline"] - 1
        if trend >= 0.12:
            reasons.append(("positive", f"Workload trending up — {usage['volume_recent']:.1f} touches/targets a game over the last {usage['games']}, vs {usage['volume_baseline']:.1f} on the season"))
        elif trend <= -0.12:
            reasons.append(("negative", f"Workload trending down — {usage['volume_recent']:.1f} a game over the last {usage['games']}, vs {usage['volume_baseline']:.1f} on the season"))
        if ctx["position"] in _TARGET_SHARE_BASELINE and usage["target_share"] > 0:
            share_pct = usage["target_share"] * 100
            if usage["target_share"] >= _TARGET_SHARE_BASELINE[ctx["position"]] * 1.2:
                reasons.append(("positive", f"Commands {share_pct:.0f}% of the team's targets"))
            elif usage["target_share"] <= _TARGET_SHARE_BASELINE[ctx["position"]] * 0.7:
                reasons.append(("negative", f"Only a {share_pct:.0f}% target share"))

    matchup = ctx.get("matchup_data")
    if matchup and matchup["reliable"] and matchup["avg"] is not None:
        avg, n = matchup["avg"], matchup["games"]
        if avg > line:
            reasons.append(("positive", f"Averages {avg:.1f} {prop_label} vs {opponent} historically ({n} games)"))
        else:
            reasons.append(("negative", f"Only averages {avg:.1f} {prop_label} vs {opponent} historically ({n} games)"))

    missing_teammate = ctx.get("missing_teammate")
    if missing_teammate:
        impact = ctx.get("teammate_data") or {}
        usage_score = scores.get("teammate_out", 0.5)
        vacated = impact.get("vacated_share")
        if impact.get("boost") is not None and usage_score >= 0.6:
            reasons.append(("positive", f"{missing_teammate} is out — {player_name} averages {impact['boost']:+.1f} {prop_label} without him ({impact['sample_without']} games)"))
        elif impact.get("boost") is not None and usage_score <= 0.4:
            reasons.append(("negative", f"{missing_teammate} is out — {player_name} has actually produced less without him ({impact['sample_without']} games)"))
        elif vacated:
            reasons.append(("positive", f"{missing_teammate} is out — {vacated*100:.0f}% of the target share is up for grabs"))

    rest_days = ctx.get("rest_days")
    if rest_days is not None:
        rest_data = ctx.get("rest_data")
        if rest_days <= SHORT_WEEK_REST:
            if rest_data and rest_data["rest_edge"] > 0:
                reasons.append(("negative", f"Short week ({rest_days} days) — averages {rest_data['rest_edge']:.1f} fewer {prop_label} on short rest"))
            else:
                reasons.append(("neutral", f"Short week ({rest_days} days rest) — no clear historical drop-off"))
        elif rest_days >= LONG_REST:
            reasons.append(("positive", f"Extra rest ({rest_days} days) coming into this one"))

    home_data = ctx.get("home_data")
    if home_data and line > 0:
        edge = home_data["home_away_edge"]
        if abs(edge) / line >= 0.15:
            if ctx["is_home"]:
                sentiment = "positive" if edge > 0 else "negative"
                reasons.append((sentiment, f"At home, where he averages {abs(edge):.1f} {'more' if edge > 0 else 'fewer'} {prop_label} than on the road"))
            else:
                sentiment = "negative" if edge > 0 else "positive"
                reasons.append((sentiment, f"On the road, where he averages {abs(edge):.1f} {'fewer' if edge > 0 else 'more'} {prop_label} than at home"))

    market_score = scores.get("market_signal", 0.5)
    if abs(market_score - 0.5) >= 0.1:
        if market_score > 0.5:
            reasons.append(("positive", "Prediction markets (Kalshi/Polymarket) lean the same direction as this prop"))
        else:
            reasons.append(("negative", "Prediction markets (Kalshi/Polymarket) lean against this prop"))

    return reasons


# ── SportModule implementation ────────────────────────────────────────────────

class NFLSport(SportModule):
    key = "nfl"
    label = "NFL"
    icon = "🏈"

    prizepicks_league_id = PRIZEPICKS_LEAGUE_ID
    prop_types = PROP_TYPES
    prop_type_labels = PROP_TYPE_LABELS
    position_options = POSITION_OPTIONS

    weights = WEIGHTS
    factor_funcs = FACTOR_FUNCS

    def build_context(self, entry):
        player_name = entry.get("player_name")
        opponent    = entry.get("opponent")

        player_id = nfl.get_player_id(player_name)
        opp_abbr  = nfl.get_team_abbr(opponent)

        if not player_id:
            return {"error": f"Player not found: {player_name}"}
        if not opp_abbr:
            return {"error": f"Team not found: {opponent}"}

        prop_type = entry.get("prop_type")
        line      = entry.get("line")

        # stat_seasons lets the backtester restrict every league-wide table to
        # seasons that had already been played at the time of the game.
        stat_seasons = entry.get("stat_seasons") or nfl.recent_seasons()
        as_of = entry.get("as_of")

        logs = nfl.get_game_log(player_id, stat_seasons)
        if logs.empty:
            return {"error": f"No game log for {player_name} in {stat_seasons}"}

        position = (normalize_position(entry.get("position"))
                    or normalize_position(nfl.get_player_position(player_id))
                    or "WR")

        def _safe(fn):
            try:
                return fn()
            except Exception:
                return None

        form_data     = _safe(lambda: nfl.get_form_stats(player_id, prop_type, line))
        matchup_data  = _safe(lambda: nfl.get_player_vs_team(player_id, opp_abbr, prop_type))
        home_data     = _safe(lambda: nfl.get_home_away_splits(player_id, prop_type))
        rest_data     = _safe(lambda: nfl.get_rest_splits(player_id, prop_type))
        usage_data    = _safe(lambda: nfl.get_usage_profile(player_id, prop_type))
        defense_data  = _safe(lambda: nfl.get_defense_vs_position(
            opp_abbr, position, prop_type, seasons=stat_seasons, as_of=as_of))

        # The manual-slate UI only offers NBA's back-to-back checkbox; for NFL
        # the equivalent squeezed-schedule case is a short week.
        rest_days = entry.get("rest_days")
        if rest_days is None:
            rest_days = SHORT_WEEK_REST if entry.get("is_b2b") else 7

        missing_teammate = entry.get("missing_teammate")
        teammate_data = None
        if missing_teammate:
            teammate_data = _safe(lambda: nfl.get_teammate_out_impact(player_id, missing_teammate, prop_type))

        return {
            "entry":               entry,
            "player_id":           player_id,
            "opponent_abbr":       opp_abbr,
            "prop_type":           prop_type,
            "line":                line,
            "position":            position,
            "is_home":             entry.get("is_home", True),
            "rest_days":           rest_days,
            "missing_teammate":    missing_teammate,
            "market_signal_score": entry.get("market_signal_score", 0.5),
            "stat_seasons":        stat_seasons,
            "as_of":               as_of,
            "form_data":           form_data,
            "matchup_data":        matchup_data,
            "home_data":           home_data,
            "rest_data":           rest_data,
            "usage_data":          usage_data,
            "defense_data":        defense_data,
            "teammate_data":       teammate_data,
        }

    def build_reasoning(self, context, scores):
        return build_reasoning(context, scores)

    def get_todays_games(self):
        """NFL is weekly — 'today's games' means this week's slate."""
        return nfl.get_this_weeks_games()

    def build_auto_slate(self, missing_teammates=None, lines=None):
        if missing_teammates is None:
            missing_teammates = {}

        games       = nfl.get_this_weeks_games()
        team_lookup = nfl.build_team_lookup()

        playing_this_week = set()
        home_teams        = set()
        opponent_map      = {}
        rest_map          = {}
        week = season = None

        for g in games:
            playing_this_week.update([g["home_team"], g["away_team"]])
            home_teams.add(g["home_team"])
            opponent_map[g["home_team"]] = g["away_team"]
            opponent_map[g["away_team"]] = g["home_team"]
            rest_map[g["home_team"]] = g["home_rest"]
            rest_map[g["away_team"]] = g["away_rest"]
            week, season = g["week"], g["season"]

        if lines is None:
            lines = get_prizepicks_lines(PRIZEPICKS_LEAGUE_ID, PRIZEPICKS_PROP_MAP, PROP_TYPES)

        grouped = defaultdict(list)
        meta = {}

        for line in lines:
            team_abbr = team_lookup.get(line.get("team", "").strip().lower())
            if not team_abbr or team_abbr not in playing_this_week:
                continue

            opponent = opponent_map.get(team_abbr)
            if not opponent:
                continue

            key = (line["player_name"], line["prop_type"])
            grouped[key].append(line["line"])
            if key not in meta:
                rest_days = rest_map.get(team_abbr, 7)
                meta[key] = {
                    "player_name":   line["player_name"],
                    "team":          team_abbr,
                    "opponent":      opponent,
                    "prop_type":     line["prop_type"],
                    "is_home":       team_abbr in home_teams,
                    "position":      normalize_position(line.get("position")) or "WR",
                    "rest_days":     rest_days,
                    "is_short_week": rest_days <= SHORT_WEEK_REST,
                    "week":          week,
                    "season":        season,
                }

        slate = []
        for key, line_values in grouped.items():
            line_values.sort()
            median_line = line_values[len(line_values) // 2]
            entry = dict(meta[key])
            entry["line"] = median_line
            entry["missing_teammate"] = missing_teammates.get(entry["player_name"])
            slate.append(entry)

        return sorted(slate, key=lambda x: x["player_name"])

    def backtest_games(self, player_name, opponent_team, prop_type, position=None):
        player_id = nfl.get_player_id(player_name)
        if not player_id:
            yield {"game_date": "?", "skip_reason": f"player not found: {player_name}"}
            return

        opp_abbr = nfl.get_team_abbr(opponent_team)
        if not opp_abbr:
            yield {"game_date": "?", "skip_reason": f"team not found: {opponent_team}"}
            return

        # Start clean so a live-scoring cache entry can't leak into the window,
        # and hold the full history separately from the seeded window.
        nfl.clear_game_log_cache()
        all_logs = nfl.get_game_log(player_id, BACKTEST_SEASONS).copy()
        if all_logs.empty:
            yield {"game_date": "?", "skip_reason": f"no game logs found for {player_name}"}
            return

        opp_logs = all_logs[all_logs["opponent_team"] == opp_abbr]
        if opp_logs.empty:
            yield {"game_date": "?", "skip_reason": f"no games found vs {opponent_team}"}
            return

        for idx, game_row in opp_logs.iterrows():
            game_date = game_row.get("game_date")
            if pd.isna(game_date):
                game_date = f"{game_row['season']} W{game_row['week']}"

            logs_before = all_logs[all_logs.index < idx].tail(BACKTEST_WINDOW)
            if len(logs_before) < 3:
                yield {"game_date": game_date, "skip_reason": f"not enough prior games ({len(logs_before)})"}
                continue

            values = nfl.prop_series(logs_before.tail(N_GAMES), prop_type)
            if values is None or values.empty:
                yield {"game_date": game_date, "skip_reason": "could not infer line"}
                continue
            line = round(float(values.mean()) * 2) / 2
            if line <= 0:
                yield {"game_date": game_date, "skip_reason": "inferred line of 0"}
                continue

            # Everything the scorer sees is restricted to before this game.
            nfl.seed_game_log_cache(player_id, logs_before)
            season, week = int(game_row["season"]), int(game_row["week"])

            entry = {
                "player_name":  player_name,
                "opponent":     opp_abbr,
                "prop_type":    prop_type,
                "line":         line,
                "position":     position or game_row.get("position"),
                "is_home":      bool(game_row.get("is_home", True)),
                "rest_days":    int(game_row["rest_days"]) if not pd.isna(game_row.get("rest_days")) else 7,
                "as_of":        (season, week),
                "stat_seasons": [s for s in BACKTEST_SEASONS if s <= season],
            }
            result = calculate_confidence(self, entry)
            if "error" in result:
                yield {"game_date": game_date, "skip_reason": result["error"]}
                continue

            actual = nfl.extract_stat(game_row, prop_type)
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

        nfl.clear_game_log_cache()


# Imported at the bottom to avoid a circular import at module load time
# (core.scoring imports nothing from sports, so this is safe).
from core.scoring import calculate_confidence  # noqa: E402
