# sports/mlb/factors.py — MLB scoring factors and SportModule implementation

from collections import defaultdict

import pandas as pd

from sports.base import SportModule
from sports.mlb import data as mlb
from sports.mlb.config import (
    WEIGHTS, PROP_TYPES, PROP_TYPE_LABELS, PRIZEPICKS_PROP_MAP, POSITION_OPTIONS,
    PRIZEPICKS_LEAGUE_ID, PITCHER_PROP_TYPES, SEASON, N_GAMES, BACKTEST_SEASONS,
    MIN_LINE,
)
from markets.prizepicks import get_prizepicks_lines


# League-average reference points every rate-stat comparison is scored against.
# Rough but stable modern-era values; the factors only ever use them to decide
# how far from average a pitcher or lineup sits, never as absolutes.
LEAGUE_REF = {
    "era":         4.10,
    "whip":        1.25,
    "opp_avg":     0.245,
    "k_rate":      0.22,
    "hr_per_9":    1.15,
    "xwoba_con":   0.370,
    "bullpen_era": 4.05,
}


def _clamp(x, lo=0.05, hi=0.95):
    return max(lo, min(hi, x))


def _ratio_score(value, ref, sensitivity=1.5, invert=False):
    """
    Scores a rate stat against its league reference. A value above the
    reference lands above 0.5 (or below it, with invert=True, for stats where
    "more" is bad for the prop). Returns None when the input is missing so
    callers can drop the component out of a weighted blend cleanly.
    """
    if value is None or not ref:
        return None
    edge = (value - ref) / ref
    if invert:
        edge = -edge
    return _clamp(0.5 + edge * sensitivity)


def _blend(components, weights):
    """Weighted mean over only the components that actually resolved."""
    usable = {k: v for k, v in components.items() if v is not None}
    total = sum(weights[k] for k in usable)
    if not total:
        return 0.5
    return sum(usable[k] * weights[k] for k in usable) / total


# ── Individual factor scoring functions ──────────────────────────────────────
# Each takes the context dict built by MLBSport.build_context() and returns
# a float 0.0-1.0.

def score_streak(ctx):
    stats = ctx.get("streak_data")
    if not stats:
        return 0.5
    hit_rate = stats["hit_rate"]
    line     = ctx["line"]
    avg_edge = (stats["avg"] - line) / line if line > 0 else 0
    boost    = min(avg_edge * 0.3, 0.1)
    return _clamp(hit_rate + boost, 0.0, 1.0)


# How much each component of the opposing starter's line matters, per prop.
# k_rate is inverted inside score_pitcher_matchup — a high-strikeout starter
# suppresses every batting prop.
_STARTER_COMPONENTS = {
    "hits":           {"opp_avg": 0.50, "whip": 0.30, "k_rate": 0.20},
    "total_bases":    {"opp_avg": 0.35, "hr_per_9": 0.35, "whip": 0.15, "k_rate": 0.15},
    "home_runs":      {"hr_per_9": 0.70, "opp_avg": 0.15, "k_rate": 0.15},
    "rbis":           {"era": 0.40, "whip": 0.35, "hr_per_9": 0.25},
    "runs":           {"era": 0.40, "whip": 0.40, "k_rate": 0.20},
    "hits+runs+rbis": {"opp_avg": 0.35, "whip": 0.30, "era": 0.20, "k_rate": 0.15},
}


def score_pitcher_matchup(ctx):
    """
    For batting props: how beatable the opposing starter is. For pitcher props:
    how strikeout-prone the opposing lineup is. This is baseball's single
    biggest per-game swing factor — the starter changes every night.
    """
    if ctx["is_pitcher_prop"]:
        lineup = ctx.get("opp_batting")
        league = ctx.get("league_batting")
        if not lineup or not league:
            return 0.5
        return _ratio_score(lineup.get("k_rate"), league.get("k_rate"), 2.5) or 0.5

    sp = ctx.get("opp_pitcher_stats")
    if not sp:
        return 0.5

    weights = _STARTER_COMPONENTS.get(ctx["prop_type"], _STARTER_COMPONENTS["hits"])
    components = {
        "era":      _ratio_score(sp.get("era"),      LEAGUE_REF["era"],      1.0),
        "whip":     _ratio_score(sp.get("whip"),     LEAGUE_REF["whip"],     1.8),
        "opp_avg":  _ratio_score(sp.get("opp_avg"),  LEAGUE_REF["opp_avg"],  1.8),
        "hr_per_9": _ratio_score(sp.get("hr_per_9"), LEAGUE_REF["hr_per_9"], 1.0),
        "k_rate":   _ratio_score(sp.get("k_rate"),   LEAGUE_REF["k_rate"],   1.5, invert=True),
    }
    return _blend({k: components[k] for k in weights}, weights)


# Which per-PA rate carries the platoon signal for each batting prop.
_HAND_METRIC = {
    "hits":           "hits_pa",
    "total_bases":    "tb_pa",
    "home_runs":      "hr_pa",
    "rbis":           "ops",
    "runs":           "ops",
    "hits+runs+rbis": "ops",
}
MIN_SPLIT_PA = 40


def score_handedness(ctx):
    """
    The platoon matchup. For a batter, how their production against this
    starter's throwing hand compares to their own overall rate, refined by
    Statcast contact quality against that hand. For a pitcher, how badly the
    opposing lineup handles his hand.
    """
    if ctx["is_pitcher_prop"]:
        hand   = ctx.get("player_hand")
        splits = ctx.get("opp_team_hand_splits") or {}
        league = ctx.get("league_hand_ops") or {}
        vs = splits.get(hand)
        if not vs:
            return 0.5
        k_score   = _ratio_score(vs.get("k_rate"), LEAGUE_REF["k_rate"], 2.5)
        ops_score = _ratio_score(vs.get("ops"), league.get(hand), 2.0, invert=True)
        return _blend({"k": k_score, "ops": ops_score}, {"k": 0.6, "ops": 0.4})

    hand = ctx.get("opp_pitcher_hand")
    splits = ctx.get("batter_hand_splits") or {}
    vs    = splits.get(hand)
    other = splits.get("R" if hand == "L" else "L")

    score = 0.5
    metric = _HAND_METRIC.get(ctx["prop_type"], "ops")
    if vs and other and vs.get(metric) is not None and other.get(metric) is not None:
        if vs["pa"] >= MIN_SPLIT_PA:
            total_pa = vs["pa"] + other["pa"]
            baseline = (vs[metric] * vs["pa"] + other[metric] * other["pa"]) / total_pa
            score = _ratio_score(vs[metric], baseline, 1.2) or 0.5

    quality = ctx.get("statcast_quality")
    if quality and quality.get("xwoba_con") is not None:
        sc = _ratio_score(quality["xwoba_con"], LEAGUE_REF["xwoba_con"], 1.2)
        score = score * 0.7 + sc * 0.3

    return score


def score_bullpen(ctx):
    """
    For batting props: the opposing relief corps, which throws roughly a third
    of the innings a batter sees. For pitcher props: his own bullpen — a weak
    or overworked pen buys a starter a longer leash and more strikeouts.
    """
    if ctx["is_pitcher_prop"]:
        pen = ctx.get("own_bullpen")
        if not pen or pen.get("era") is None:
            return 0.5
        return _ratio_score(pen["era"], LEAGUE_REF["bullpen_era"], 0.8) or 0.5

    pen = ctx.get("opp_bullpen")
    if not pen or pen.get("rank") is None or pen.get("total_teams", 0) < 2:
        return 0.5
    # rank 1 is the league's best bullpen, i.e. the worst matchup for a batter
    weakness = (pen["rank"] - 1) / (pen["total_teams"] - 1)
    return _clamp(0.5 + (weakness - 0.5) * 0.8)


def score_park_factor(ctx):
    """
    Statcast park index for this stat at the stadium the game is actually in,
    centered on 100. This is also why there is no separate home/away factor —
    the venue effect is the park.
    """
    park = ctx.get("park_factor")
    if not park or park.get("index") is None:
        return 0.5
    return _clamp(0.5 + (park["index"] - 100) / 50)


def score_lineup_spot(ctx):
    """
    Batting-order position drives plate appearances, and plate appearances drive
    every counting stat. Uses the posted lineup when there is one, otherwise the
    player's trailing PA per game, which encodes the same thing with a lag.
    """
    if ctx["is_pitcher_prop"]:
        return 0.5

    slot = ctx.get("lineup_spot")
    if slot:
        return _clamp(0.5 + (4.5 - slot) * 0.04)

    pa = ctx.get("pa_per_game")
    if pa:
        return _clamp(0.5 + (pa - 4.0) * 0.15)
    return 0.5


def score_rest(ctx):
    """
    Days of rest. Heavily a pitcher factor — a starter on short rest gets pulled
    early and never reaches his strikeout number. Batters play daily, so an off
    day is worth only a nudge.
    """
    days = ctx.get("days_rest")
    if days is None:
        return 0.5

    if ctx["is_pitcher_prop"]:
        if days <= 3:
            return 0.30   # short rest — shorter leash
        if days <= 5:
            return 0.58   # normal turn through the rotation
        if days <= 7:
            return 0.52
        return 0.45       # long layoff, usually a pitch limit coming back
    return 0.55 if days >= 2 else 0.50


def score_market_signal(ctx):
    """Cross-market (Kalshi/Polymarket) volume-weighted signal, neutral if unavailable."""
    return ctx.get("market_signal_score", 0.5)


FACTOR_FUNCS = {
    "streak":          score_streak,
    "pitcher_matchup": score_pitcher_matchup,
    "handedness":      score_handedness,
    "bullpen":         score_bullpen,
    "park_factor":     score_park_factor,
    "lineup_spot":     score_lineup_spot,
    "rest":            score_rest,
    "market_signal":   score_market_signal,
}


# ── Reasoning ─────────────────────────────────────────────────────────────────

_HAND_LABEL = {"L": "LHP", "R": "RHP"}


def build_reasoning(ctx, scores):
    reasons = []
    prop_label = PROP_TYPE_LABELS.get(ctx["prop_type"], ctx["prop_type"])
    line     = ctx["line"]
    entry    = ctx["entry"]
    opponent = entry.get("opponent", "the opponent")
    player   = entry.get("player_name", "Player")

    streak = ctx.get("streak_data")
    if streak:
        n, avg = streak["games"], streak["avg"]
        over = streak["over_count"]
        if streak["hit_rate"] >= 0.7:
            reasons.append(("positive", f"Cleared {prop_label} {line} in {over}/{n} of the last {n} games (avg: {avg:.2f})"))
        elif streak["hit_rate"] >= 0.5:
            reasons.append(("neutral", f"Cleared {prop_label} {line} in {over}/{n} of the last {n} games (avg: {avg:.2f})"))
        else:
            reasons.append(("negative", f"Only cleared {prop_label} {line} in {over}/{n} of the last {n} games (avg: {avg:.2f})"))

    sp = ctx.get("opp_pitcher_stats")
    if sp and not ctx["is_pitcher_prop"]:
        name = ctx.get("opp_pitcher_name") or "the opposing starter"
        matchup_score = scores.get("pitcher_matchup", 0.5)
        detail = []
        if sp.get("era") is not None:
            detail.append(f"{sp['era']:.2f} ERA")
        if sp.get("whip") is not None:
            detail.append(f"{sp['whip']:.2f} WHIP")
        if sp.get("k_rate") is not None:
            detail.append(f"{sp['k_rate']*100:.0f}% K rate")
        summary = ", ".join(detail)
        if matchup_score >= 0.6:
            reasons.append(("positive", f"Faces {name} ({summary}) — a beatable starter"))
        elif matchup_score <= 0.4:
            reasons.append(("negative", f"Faces {name} ({summary}) — a tough draw"))
        else:
            reasons.append(("neutral", f"Faces {name} ({summary})"))

    if ctx["is_pitcher_prop"]:
        lineup = ctx.get("opp_batting")
        league = ctx.get("league_batting")
        if lineup and league and lineup.get("k_rate") and league.get("k_rate"):
            if lineup["k_rate"] > league["k_rate"]:
                reasons.append(("positive", f"{opponent} strike out {lineup['k_rate']*100:.1f}% of the time, above the {league['k_rate']*100:.1f}% league rate"))
            else:
                reasons.append(("negative", f"{opponent} strike out just {lineup['k_rate']*100:.1f}% of the time, below the {league['k_rate']*100:.1f}% league rate"))

    hand_score = scores.get("handedness", 0.5)
    hand = ctx.get("opp_pitcher_hand") if not ctx["is_pitcher_prop"] else ctx.get("player_hand")
    if hand and abs(hand_score - 0.5) >= 0.06:
        hand_label = _HAND_LABEL.get(hand, hand)
        if ctx["is_pitcher_prop"]:
            side = "struggles against" if hand_score > 0.5 else "handles"
            reasons.append((
                "positive" if hand_score > 0.5 else "negative",
                f"{opponent}'s lineup {side} {hand_label}",
            ))
        else:
            side = "hits" if hand_score > 0.5 else "struggles against"
            reasons.append((
                "positive" if hand_score > 0.5 else "negative",
                f"{player} {side} {hand_label} relative to his own baseline",
            ))

    quality = ctx.get("statcast_quality")
    if quality and quality.get("xwoba_con") is not None:
        if quality["xwoba_con"] >= LEAGUE_REF["xwoba_con"] * 1.1:
            reasons.append(("positive", f"Statcast backs it up — {quality['xwoba_con']:.3f} xwOBA on contact, {quality['hard_hit_rate']*100:.0f}% hard-hit over {quality['batted_balls']} batted balls"))
        elif quality["xwoba_con"] <= LEAGUE_REF["xwoba_con"] * 0.9:
            reasons.append(("negative", f"Weak contact lately — {quality['xwoba_con']:.3f} xwOBA on contact over {quality['batted_balls']} batted balls"))

    pen = ctx.get("opp_bullpen")
    if pen and pen.get("rank") and not ctx["is_pitcher_prop"]:
        if scores.get("bullpen", 0.5) >= 0.6:
            reasons.append(("positive", f"{opponent}'s bullpen ranks {pen['rank']}/{pen['total_teams']} ({pen['era']:.2f} relief ERA) — soft late innings"))
        elif scores.get("bullpen", 0.5) <= 0.4:
            reasons.append(("negative", f"{opponent}'s bullpen ranks {pen['rank']}/{pen['total_teams']} ({pen['era']:.2f} relief ERA) — hard to score on late"))

    park = ctx.get("park_factor")
    if park and park.get("index") is not None and abs(park["index"] - 100) >= 4:
        direction = "boosts" if park["index"] > 100 else "suppresses"
        sentiment = "positive" if park["index"] > 100 else "negative"
        reasons.append((sentiment, f"{park['venue']} {direction} {prop_label} ({park['index']:.0f} park index)"))

    slot = ctx.get("lineup_spot")
    if slot:
        if slot <= 3:
            reasons.append(("positive", f"Batting {slot}{'st' if slot == 1 else 'nd' if slot == 2 else 'rd'} — top of the order, most plate appearances"))
        elif slot >= 7:
            reasons.append(("negative", f"Batting {slot}th — bottom of the order, fewest plate appearances"))

    if ctx["is_pitcher_prop"] and ctx.get("days_rest") is not None:
        days = ctx["days_rest"]
        if days <= 3:
            reasons.append(("negative", f"Only {days} days rest — expect a shorter outing"))
        elif days >= 8:
            reasons.append(("negative", f"{days} days since his last outing — likely on a pitch limit"))

    vs_team = ctx.get("matchup_data")
    if vs_team and vs_team.get("reliable") and vs_team.get("avg") is not None:
        if vs_team["avg"] > line:
            reasons.append(("positive", f"Averages {vs_team['avg']:.2f} {prop_label} vs {opponent} ({vs_team['games']} games)"))
        else:
            reasons.append(("negative", f"Averages only {vs_team['avg']:.2f} {prop_label} vs {opponent} ({vs_team['games']} games)"))

    market_score = scores.get("market_signal", 0.5)
    if abs(market_score - 0.5) >= 0.1:
        if market_score > 0.5:
            reasons.append(("positive", "Prediction markets (Kalshi/Polymarket) lean the same direction as this prop"))
        else:
            reasons.append(("negative", "Prediction markets (Kalshi/Polymarket) lean against this prop"))

    return reasons


# ── SportModule implementation ────────────────────────────────────────────────

class MLBSport(SportModule):
    key = "mlb"
    label = "MLB"
    icon = "⚾"

    prizepicks_league_id = PRIZEPICKS_LEAGUE_ID
    prop_types = PROP_TYPES
    prop_type_labels = PROP_TYPE_LABELS
    position_options = POSITION_OPTIONS

    weights = WEIGHTS
    factor_funcs = FACTOR_FUNCS

    def build_context(self, entry):
        player_name = entry.get("player_name")
        opponent    = entry.get("opponent")
        prop_type   = entry.get("prop_type")
        line        = entry.get("line")

        player_id = mlb.get_player_id(player_name)
        if not player_id:
            return {"error": f"Player not found: {player_name}"}
        opp_team_id = mlb.get_team_id(opponent) if opponent else None
        if not opp_team_id:
            return {"error": f"Team not found: {opponent}"}

        # season / as_of_date let a backtest pin every season-level aggregate to
        # the year of the game being scored instead of the live season.
        season    = entry.get("season", SEASON)
        as_of     = entry.get("as_of_date")
        is_live   = as_of is None
        is_pitcher_prop = prop_type in PITCHER_PROP_TYPES
        group     = mlb.stat_group(prop_type)

        info = mlb.get_player_info(player_id) or {}
        player_hand = info.get("pitch_hand" if is_pitcher_prop else "bat_side", "R")

        is_home = entry.get("is_home", True)
        own_team_id = mlb.get_team_id(entry["team"]) if entry.get("team") else None
        venue_team_id = own_team_id if is_home else opp_team_id

        opp_pitcher_id   = entry.get("opp_pitcher_id")
        opp_pitcher_hand = entry.get("opp_pitcher_hand")
        opp_pitcher_name = entry.get("opp_pitcher_name")
        if not opp_pitcher_id and is_live and not is_pitcher_prop:
            probable = self._probable_against(own_team_id, opp_team_id)
            if probable:
                opp_pitcher_id   = probable["id"]
                opp_pitcher_hand = probable["hand"]
                opp_pitcher_name = probable["name"]

        def _safe(fn, *args, **kwargs):
            try:
                return fn(*args, **kwargs)
            except Exception:
                return None

        ctx = {
            "entry":            entry,
            "player_id":        player_id,
            "player_hand":      player_hand,
            "team_id":          opp_team_id,
            "own_team_id":      own_team_id,
            "prop_type":        prop_type,
            "line":             line,
            "is_home":          is_home,
            "is_pitcher_prop":  is_pitcher_prop,
            "season":           season,
            "position":         entry.get("position", "Pitcher" if is_pitcher_prop else "Batter"),
            "opp_pitcher_id":   opp_pitcher_id,
            "opp_pitcher_hand": opp_pitcher_hand,
            "opp_pitcher_name": opp_pitcher_name,
            "market_signal_score": entry.get("market_signal_score", 0.5),

            "streak_data":   _safe(mlb.get_streak_stats, player_id, prop_type, line, N_GAMES, season),
            "matchup_data":  _safe(mlb.get_player_vs_team, player_id, opp_team_id, prop_type, season),
            "park_factor":   _safe(mlb.get_park_factor, venue_team_id, prop_type, season) if venue_team_id else None,
        }

        if is_pitcher_prop:
            league_batting = _safe(mlb.get_league_team_batting, season) or {}
            ctx["opp_batting"]    = league_batting.get(opp_team_id)
            ctx["league_batting"] = league_batting.get("_league")
            hand_splits = _safe(mlb.get_league_team_hand_splits, season) or {}
            ctx["opp_team_hand_splits"] = hand_splits.get(opp_team_id)
            ctx["league_hand_ops"]      = hand_splits.get("_league")
            ctx["own_bullpen"]  = _safe(mlb.get_bullpen_strength, own_team_id, season) if own_team_id else None
            ctx["days_rest"]    = _safe(mlb.get_pitcher_days_rest, player_id, as_of, season)
        else:
            ctx["opp_pitcher_stats"] = _safe(mlb.get_pitcher_season_stats, opp_pitcher_id, season) if opp_pitcher_id else None
            ctx["batter_hand_splits"] = _safe(mlb.get_batter_hand_splits, player_id, season)
            ctx["opp_bullpen"] = _safe(mlb.get_bullpen_strength, opp_team_id, season)
            ctx["pa_per_game"] = _safe(mlb.get_recent_pa_per_game, player_id, N_GAMES, season)
            ctx["lineup_spot"] = (mlb.get_todays_lineup_spots() or {}).get(player_id) if is_live else None
            ctx["days_rest"]   = entry.get("days_rest") or self._days_rest_from_logs(player_id, group, season, as_of)
            # Statcast pulls are the slowest call in the module, so they are
            # opt-out — backtests turn them off rather than paying it per game.
            # With no known starter the hand filter drops and this becomes an
            # overall contact-quality read, which still beats a flat 0.5.
            if entry.get("use_statcast", True):
                ctx["statcast_quality"] = _safe(mlb.get_statcast_quality, player_id, opp_pitcher_hand, as_of)

        return ctx

    @staticmethod
    def _probable_against(own_team_id, opp_team_id):
        """Finds today's opposing starter for a team without one supplied by the slate."""
        for game in mlb.get_todays_games():
            if game["home_team_id"] == opp_team_id and (own_team_id is None or game["away_team_id"] == own_team_id):
                return game["home_probable"]
            if game["away_team_id"] == opp_team_id and (own_team_id is None or game["home_team_id"] == own_team_id):
                return game["away_probable"]
        return None

    @staticmethod
    def _days_rest_from_logs(player_id, group, season, as_of):
        """Days since the player's previous game, read off the same trailing window."""
        if not as_of:
            return None
        logs = mlb.get_game_logs(player_id, None, season, group)
        if logs is None or logs.empty:
            return None
        return int((pd.to_datetime(as_of) - logs["game_date"].iloc[-1]).days)

    def build_reasoning(self, context, scores):
        return build_reasoning(context, scores)

    def get_todays_games(self):
        return mlb.get_todays_games()

    def build_auto_slate(self, missing_teammates=None, lines=None):
        games       = mlb.get_todays_games()
        team_lookup = mlb.build_team_lookup()
        rest_days   = mlb.get_team_rest_days()

        playing_today = set()
        home_teams    = set()
        opponent_map  = {}
        starter_map   = {}   # team abbr -> the starter that team's hitters face

        for g in games:
            home, away = g["home_team"], g["away_team"]
            playing_today.update((home, away))
            home_teams.add(home)
            opponent_map[home] = away
            opponent_map[away] = home
            starter_map[home] = g["away_probable"]
            starter_map[away] = g["home_probable"]

        if lines is None:
            lines = get_prizepicks_lines(PRIZEPICKS_LEAGUE_ID, PRIZEPICKS_PROP_MAP, PROP_TYPES)

        grouped = defaultdict(list)
        meta = {}

        for line in lines:
            team_abbr = team_lookup.get(line.get("team", "").strip().lower())
            if not team_abbr or team_abbr not in playing_today:
                continue

            opponent = opponent_map.get(team_abbr)
            if not opponent:
                continue

            key = (line["player_name"], line["prop_type"])
            grouped[key].append(line["line"])
            if key not in meta:
                starter = starter_map.get(team_abbr) or {}
                meta[key] = {
                    "player_name":      line["player_name"],
                    "team":             team_abbr,
                    "opponent":         opponent,
                    "prop_type":        line["prop_type"],
                    "is_home":          team_abbr in home_teams,
                    "position":         "Pitcher" if line["prop_type"] in PITCHER_PROP_TYPES else "Batter",
                    "days_rest":        rest_days.get(team_abbr),
                    "opp_pitcher_id":   starter.get("id"),
                    "opp_pitcher_hand": starter.get("hand"),
                    "opp_pitcher_name": starter.get("name"),
                    "season":           SEASON,
                }

        slate = []
        for key, line_values in grouped.items():
            line_values.sort()
            entry = dict(meta[key])
            entry["line"] = line_values[len(line_values) // 2]
            slate.append(entry)

        return sorted(slate, key=lambda x: x["player_name"])

    def backtest_games(self, player_name, opponent_team, prop_type, position=None):
        player_id = mlb.get_player_id(player_name)
        if not player_id:
            yield {"game_date": "?", "skip_reason": f"player not found: {player_name}"}
            return

        opp_team_id = mlb.get_team_id(opponent_team)
        if not opp_team_id:
            yield {"game_date": "?", "skip_reason": f"team not found: {opponent_team}"}
            return

        group = mlb.stat_group(prop_type)
        all_logs = mlb.get_multi_season_logs(player_id, BACKTEST_SEASONS, group)
        if all_logs is None:
            yield {"game_date": "?", "skip_reason": f"no game logs found for {player_name}"}
            return

        opp_logs = all_logs[all_logs["opponent_id"] == opp_team_id]
        if opp_logs.empty:
            yield {"game_date": "?", "skip_reason": f"no games found vs {opponent_team}"}
            return

        starter_maps = {}

        for idx, game_row in opp_logs.iterrows():
            game_date = game_row["game_date"]
            date_str  = game_date.strftime("%Y-%m-%d")
            season    = int(game_date.year)

            # Everything downstream sees only these games. The cache is keyed
            # (player_id, season, group), which is exactly what get_game_logs
            # reads, so no factor can reach past this point in time.
            logs_before = all_logs[all_logs.index < idx].tail(N_GAMES)
            if len(logs_before) < 5:
                yield {"game_date": date_str, "skip_reason": f"not enough prior games ({len(logs_before)})"}
                continue

            values = [mlb.extract_stat(row, prop_type) for _, row in logs_before.iterrows()]
            values = [v for v in values if v is not None]
            if not values:
                yield {"game_date": date_str, "skip_reason": "could not infer line"}
                continue
            line = max(round((sum(values) / len(values)) * 2) / 2, MIN_LINE)

            mlb.seed_game_log_cache(player_id, logs_before, season, group)

            own_team_id = int(game_row["team_id"])
            if (own_team_id, season) not in starter_maps:
                starter_maps[(own_team_id, season)] = mlb.get_opposing_starters(own_team_id, season)
            starter = starter_maps[(own_team_id, season)].get(date_str) or {}

            entry = {
                "player_name":      player_name,
                "team":             mlb.get_team_abbr(own_team_id),
                "opponent":         opponent_team,
                "prop_type":        prop_type,
                "line":             line,
                "is_home":          bool(game_row["is_home"]),
                "position":         position or ("Pitcher" if prop_type in PITCHER_PROP_TYPES else "Batter"),
                "season":           season,
                "as_of_date":       date_str,
                "use_statcast":     False,
                "opp_pitcher_id":   starter.get("id"),
                "opp_pitcher_hand": starter.get("hand"),
                "opp_pitcher_name": starter.get("name"),
            }

            result = calculate_confidence(self, entry)
            if "error" in result:
                yield {"game_date": date_str, "skip_reason": result["error"]}
                continue

            actual = mlb.extract_stat(game_row, prop_type)
            if actual is None:
                yield {"game_date": date_str, "skip_reason": "no actual stat"}
                continue

            yield {
                "game_date":  date_str,
                "line":       line,
                "actual":     float(actual),
                "hit":        bool(actual > line),
                "confidence": result["confidence"],
                "reasons":    result.get("reasons", []),
            }


# Imported at the bottom to avoid a circular import at module load time
# (core.scoring imports nothing from sports, so this is safe).
from core.scoring import calculate_confidence  # noqa: E402
