# backtest/simulator.py — sport-agnostic historical backtesting engine
#
# Each sport module owns its own no-lookahead logic (what "3 seasons back" or
# "a trailing 10-game window" means is sport-specific — NFL has 17 games a
# season, MLB has 162). This file just drains whatever stream of historical
# games a sport module yields and turns it into fake-money bankroll results.

from backtest.bankroll import Bankroll


def backtest_player(sport_module, player_name, opponent_team, prop_type, position=None, starting_bankroll=1000.0):
    """
    Backtests sport_module's algorithm for a single player/prop combination
    by draining sport_module.backtest_games(...), which yields one historical
    game at a time using only data available before that game.
    """
    bankroll = Bankroll(starting_balance=starting_bankroll)
    debug_lines = []
    found_any = False

    for game in sport_module.backtest_games(player_name, opponent_team, prop_type, position=position):
        found_any = True
        game_date = game.get("game_date", "?")

        if "skip_reason" in game:
            debug_lines.append(f"- {game_date}: skipped — {game['skip_reason']}")
            continue

        confidence = game["confidence"]
        actual     = game["actual"]
        hit        = game["hit"]
        line       = game["line"]

        bucket = (
            "easy" if confidence >= 0.62 else
            "moderate" if confidence >= 0.56 else
            "aggressive" if confidence >= 0.50 else "skip"
        )
        debug_lines.append(
            f"- {game_date}: line={line}, actual={actual}, conf={confidence*100:.1f}% [{bucket}], hit={hit}"
        )

        bankroll.place_bet(
            confidence=confidence,
            hit=hit,
            label=f"{player_name} {prop_type} {line} vs {opponent_team} (actual: {actual})",
            reasons=game.get("reasons", []),
            force=True,
        )

    if not found_any:
        debug_lines.append(f"No historical games found for {player_name} vs {opponent_team}.")

    bankroll.debug_log = "\n".join(debug_lines)
    return bankroll


def backtest_slate(sport_module, slate, starting_bankroll=1000.0):
    """
    Backtests a full slate of player/prop combinations for one sport.

    slate: list of dicts with player_name, opponent, prop_type, position
    """
    combined = Bankroll(starting_balance=starting_bankroll)

    for entry in slate:
        print(f"\nBacktesting {entry['player_name']} — {entry['prop_type']} vs {entry['opponent']}...")
        result = backtest_player(
            sport_module,
            player_name=entry["player_name"],
            opponent_team=entry["opponent"],
            prop_type=entry["prop_type"],
            position=entry.get("position"),
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
