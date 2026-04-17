# main.py — run the sports betting algorithm
#
# Modes:
#   python3 main.py live      — auto-pull today's PrizePicks slate and generate picks
#   python3 main.py manual    — use the manual SLATE defined below
#   python3 main.py backtest  — backtest the algorithm on historical data

import sys
from algorithm.picks import generate_picks, generate_parlays, display_picks, display_parlays

# ── MANUAL SLATE (used when running: python3 main.py manual) ─────────────────
MANUAL_SLATE = [
    {
        "player_name": "LeBron James",
        "opponent":    "Warriors",
        "prop_type":   "points",
        "line":        24.5,
        "is_home":     True,
        "is_b2b":      False,
        "position":    "SF",
    },
    {
        "player_name": "Stephen Curry",
        "opponent":    "Lakers",
        "prop_type":   "three_pointers_made",
        "line":        4.5,
        "is_home":     False,
        "is_b2b":      False,
        "position":    "PG",
    },
    {
        "player_name": "Nikola Jokic",
        "opponent":    "Celtics",
        "prop_type":   "pts+reb+ast",
        "line":        52.5,
        "is_home":     True,
        "is_b2b":      False,
        "position":    "C",
    },
]

# Players who are out tonight — maps player -> missing teammate name
MISSING_TEAMMATES = {
    # "Anthony Davis": "LeBron James",
}

# ── BACKTEST CONFIG ───────────────────────────────────────────────────────────
BACKTEST_SLATE = [
    {"player_name": "LeBron James",  "opponent": "Warriors", "prop_type": "points",   "position": "SF"},
    {"player_name": "Stephen Curry", "opponent": "Lakers",   "prop_type": "points",   "position": "PG"},
    {"player_name": "Nikola Jokic",  "opponent": "Celtics",  "prop_type": "rebounds", "position": "C"},
]
STARTING_BANKROLL = 1000.0

# ─────────────────────────────────────────────────────────────────────────────

def run_live():
    from data.schedule import build_auto_slate
    slate = build_auto_slate(MISSING_TEAMMATES)
    if not slate:
        print("No props found for today.")
        return
    picks   = generate_picks(slate, MISSING_TEAMMATES)
    display_picks(picks)
    parlays = generate_parlays(picks)
    display_parlays(parlays)


def run_manual():
    picks   = generate_picks(MANUAL_SLATE, MISSING_TEAMMATES)
    display_picks(picks)
    parlays = generate_parlays(picks)
    display_parlays(parlays)


def run_backtest():
    from backtest.simulator import backtest_slate
    backtest_slate(BACKTEST_SLATE, starting_bankroll=STARTING_BANKROLL)


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "manual"

    if mode == "live":
        run_live()
    elif mode == "manual":
        run_manual()
    elif mode == "backtest":
        run_backtest()
    else:
        print("Usage: python3 main.py [live | manual | backtest]")
