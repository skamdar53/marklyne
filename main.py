# main.py — run Arbiter's picks/backtest engine from the CLI
#
# Usage:
#   python3 main.py live      --sport nba   — auto-pull today's PrizePicks slate and generate picks
#   python3 main.py manual    --sport nba   — use the manual SLATE defined below
#   python3 main.py backtest  --sport nba   — backtest the algorithm on historical data

import argparse

from core.picks import generate_picks, generate_parlays, display_picks, display_parlays
from sports.registry import get_sport, SPORTS

# ── MANUAL SLATE (used when running: python3 main.py manual) ─────────────────
MANUAL_SLATES = {
    "nba": [
        {"player_name": "LeBron James",  "opponent": "Warriors", "prop_type": "points",              "line": 24.5, "is_home": True,  "is_b2b": False, "position": "SF"},
        {"player_name": "Stephen Curry", "opponent": "Lakers",   "prop_type": "three_pointers_made",  "line": 4.5,  "is_home": False, "is_b2b": False, "position": "PG"},
        {"player_name": "Nikola Jokic",  "opponent": "Celtics",  "prop_type": "pts+reb+ast",          "line": 52.5, "is_home": True,  "is_b2b": False, "position": "C"},
    ],
}

# Players who are out tonight — maps player -> missing teammate name
MISSING_TEAMMATES = {
    # "Anthony Davis": "LeBron James",
}

# ── BACKTEST CONFIG ───────────────────────────────────────────────────────────
BACKTEST_SLATES = {
    "nba": [
        {"player_name": "LeBron James",  "opponent": "Warriors", "prop_type": "points",   "position": "SF"},
        {"player_name": "Stephen Curry", "opponent": "Lakers",   "prop_type": "points",   "position": "PG"},
        {"player_name": "Nikola Jokic",  "opponent": "Celtics",  "prop_type": "rebounds", "position": "C"},
    ],
}
STARTING_BANKROLL = 1000.0

# ─────────────────────────────────────────────────────────────────────────────


def run_live(sport_key):
    sport = get_sport(sport_key)
    slate = sport.build_auto_slate(MISSING_TEAMMATES)
    if not slate:
        print(f"No props found for {sport.label} right now.")
        return
    picks   = generate_picks(sport, slate, MISSING_TEAMMATES)
    display_picks(picks)
    parlays = generate_parlays(picks)
    display_parlays(parlays)


def run_manual(sport_key):
    sport = get_sport(sport_key)
    slate = MANUAL_SLATES.get(sport_key, [])
    if not slate:
        print(f"No manual slate configured for {sport.label} — edit MANUAL_SLATES in main.py.")
        return
    picks   = generate_picks(sport, slate, MISSING_TEAMMATES)
    display_picks(picks)
    parlays = generate_parlays(picks)
    display_parlays(parlays)


def run_backtest(sport_key):
    from backtest.simulator import backtest_slate
    sport = get_sport(sport_key)
    slate = BACKTEST_SLATES.get(sport_key, [])
    if not slate:
        print(f"No backtest slate configured for {sport.label} — edit BACKTEST_SLATES in main.py.")
        return
    backtest_slate(sport, slate, starting_bankroll=STARTING_BANKROLL)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Arbiter — multi-sport player-prop scoring engine")
    parser.add_argument("mode", choices=["live", "manual", "backtest"], nargs="?", default="manual")
    parser.add_argument("--sport", choices=list(SPORTS.keys()), default="nba")
    args = parser.parse_args()

    if args.mode == "live":
        run_live(args.sport)
    elif args.mode == "manual":
        run_manual(args.sport)
    elif args.mode == "backtest":
        run_backtest(args.sport)
