# backtest/simulator.py — historical backtesting engine
# Pulls past game logs, simulates what our algorithm would have predicted,
# compares to actual results, and tracks fake bankroll performance.

import pandas as pd
from nba_api.stats.endpoints import playergamelog
from data.players import get_player_id, get_streak_stats, seed_game_log_cache
from data.matchups import get_team_id, get_team_abbr
from algorithm.scoring import calculate_confidence
from backtest.bankroll import Bankroll
from config import SEASON
import time


def _get_actual_stat(row, prop_type):
    """Extracts the actual stat value from a game log row."""
    if prop_type == "points":
        return row["PTS"]
    elif prop_type == "rebounds":
        return row["REB"]
    elif prop_type == "assists":
        return row["AST"]
    elif prop_type == "pts+reb+ast":
        return row["PTS"] + row["REB"] + row["AST"]
    elif prop_type == "three_pointers_made":
        return row["FG3M"]
    return None


def _infer_line(logs_before, prop_type):
    """
    Simulates what a sportsbook line might be:
    uses the player's rolling 10-game average as the proxy line.
    """
    values = []
    for _, row in logs_before.iterrows():
        val = _get_actual_stat(row, prop_type)
        if val is not None:
            values.append(val)
    if not values:
        return None
    avg = sum(values) / len(values)
    # Round to nearest 0.5 like real lines
    return round(avg * 2) / 2


def backtest_player(
    player_name,
    opponent_team,
    prop_type,
    position="SF",
    n_games_back=20,
    starting_bankroll=1000.0,
):
    """
    Backtests the algorithm for a single player/prop combination.

    Iterates through past games, runs scoring on data available
    BEFORE each game, then checks the actual result.

    Returns a Bankroll object with full history.
    """
    player_id = get_player_id(player_name)
    team_id   = get_team_id(opponent_team)

    if not player_id:
        print(f"Player not found: {player_name}")
        return None

    # Pull multiple seasons for a larger sample
    seasons = ["2024-25", "2023-24", "2022-23"]
    all_logs_list = []
    for s in seasons:
        time.sleep(0.6)
        try:
            df = playergamelog.PlayerGameLog(
                player_id=player_id, season=s
            ).get_data_frames()[0]
            all_logs_list.append(df)
        except Exception:
            continue
    if not all_logs_list:
        print(f"No game logs found for {player_name}")
        return None
    import pandas as pd
    all_logs = pd.concat(all_logs_list, ignore_index=True)

    all_logs = all_logs.sort_values("GAME_DATE").reset_index(drop=True)

    # Convert team name to abbreviation for matchup filtering
    team_id  = get_team_id(opponent_team)
    opp_abbr = get_team_abbr(team_id) if team_id else opponent_team

    # Filter to games vs the specified opponent
    opp_logs = all_logs[
        all_logs["MATCHUP"].str.contains(opp_abbr, case=False, na=False)
    ]

    if opp_logs.empty:
        print(f"No games found vs {opponent_team} for {player_name}")
        return None

    bankroll = Bankroll(starting_balance=starting_bankroll)
    debug_lines = []
    debug_lines.append(f"Found **{len(opp_logs)}** games vs {opponent_team} across 3 seasons.")

    for idx, game_row in opp_logs.iterrows():
        game_date = game_row.get("GAME_DATE", "?")

        # Only use data from BEFORE this game
        logs_before = all_logs[all_logs.index < idx].tail(10)
        if len(logs_before) < 3:
            debug_lines.append(f"- {game_date}: skipped — not enough prior games ({len(logs_before)})")
            continue

        # Infer what the line would have been
        line = _infer_line(logs_before, prop_type)
        if not line:
            debug_lines.append(f"- {game_date}: skipped — could not infer line")
            continue

        # Seed the cache with historical data available before this game
        # so scoring functions use the player's form at that point in time
        seed_game_log_cache(player_id, logs_before)

        # Run our algorithm
        try:
            result = calculate_confidence(
                player_name=player_name,
                opponent_team=opponent_team,
                prop_type=prop_type,
                line=line,
                position=position,
            )
        except Exception as e:
            debug_lines.append(f"- {game_date}: skipped — scoring error: {e}")
            continue

        if "error" in result:
            debug_lines.append(f"- {game_date}: skipped — {result['error']}")
            continue

        confidence = result["confidence"]
        actual     = _get_actual_stat(game_row, prop_type)
        hit        = actual > line if actual is not None else None

        if hit is None:
            debug_lines.append(f"- {game_date}: skipped — no actual stat")
            continue

        bucket = "easy" if confidence >= 0.62 else "moderate" if confidence >= 0.56 else "aggressive" if confidence >= 0.50 else "skip"
        debug_lines.append(
            f"- {game_date}: line={line}, actual={actual}, conf={confidence*100:.1f}% [{bucket}], hit={hit}"
        )

        # Place bet and record result (force=True shows all games, not just high-confidence)
        bankroll.place_bet(
            confidence=confidence,
            hit=hit,
            label=f"{player_name} {prop_type} {line} vs {opponent_team} "
                  f"(actual: {actual})",
            reasons=result.get("reasons", []),
            force=True,
        )

    bankroll.debug_log = "\n".join(debug_lines)
    return bankroll


def backtest_slate(slate, starting_bankroll=1000.0):
    """
    Backtests a full slate of player/prop combinations.

    slate: list of dicts with player_name, opponent, prop_type, position

    Returns a combined Bankroll with all results.
    """
    combined = Bankroll(starting_balance=starting_bankroll)

    for entry in slate:
        print(f"\nBacktesting {entry['player_name']} — {entry['prop_type']} vs {entry['opponent']}...")
        result = backtest_player(
            player_name=entry["player_name"],
            opponent_team=entry["opponent"],
            prop_type=entry["prop_type"],
            position=entry.get("position", "SF"),
            starting_bankroll=starting_bankroll,
        )
        if result:
            combined.merge(result)
            result.summary()

    print("\n" + "="*50)
    print("COMBINED BACKTEST RESULTS")
    print("="*50)
    combined.summary()
    return combined
