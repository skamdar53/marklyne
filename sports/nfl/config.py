# sports/nfl/config.py — NFL-specific settings

N_SEASONS = 3          # how many recent seasons to pull for splits/defense tables
N_GAMES = 6            # lookback window for recent form (NFL plays ~17 games/season)
MIN_GAMES_VS_TEAM = 2  # minimum games vs a team to trust matchup data
MIN_GAMES_WITHOUT_TEAMMATE = 2

# Days of rest thresholds, read off the schedule's home_rest/away_rest columns.
SHORT_WEEK_REST = 4    # Thursday game off a Sunday
LONG_REST = 10         # coming off a bye or a Thursday->following Sunday turnaround

BACKTEST_SEASONS = [2023, 2024, 2025]
# How much prior history the backtester exposes to the scorer for each game.
# Wider than N_GAMES on purpose: the form factor still only reads the last
# N_GAMES off it, but usage needs a season-length baseline to measure a recent
# workload trend against, and home/away and short-week splits need the volume.
BACKTEST_WINDOW = 17

PRIZEPICKS_LEAGUE_ID = 9

PROP_TYPES = [
    "pass_yards",
    "pass_completions",
    "pass_tds",
    "rush_yards",
    "receptions",
    "receiving_yards",
    "rec+rush_yards",
]

PROP_TYPE_LABELS = {
    "pass_yards":       "Pass Yds",
    "pass_completions": "Completions",
    "pass_tds":         "Pass TDs",
    "rush_yards":       "Rush Yds",
    "receptions":       "Receptions",
    "receiving_yards":  "Rec Yds",
    "rec+rush_yards":   "Rush+Rec Yds",
}

# Maps PrizePicks' raw stat_type (lowercased, spaces -> underscores) to our
# normalized prop type names.
#
# NOTE: PrizePicks' API is behind DataDome and returned 403 from this machine,
# so these raw keys could not be confirmed against a live NFL board. They're the
# lowercased/underscored forms of the board labels PrizePicks displays, plus the
# plausible spelling variants for each. Unrecognized stat_types are silently
# dropped by markets/prizepicks.py, so an unmapped variant costs us lines rather
# than producing bad data — worth re-checking during the season and adding any
# missing keys here.
PRIZEPICKS_PROP_MAP = {
    "pass_yards":              "pass_yards",
    "passing_yards":           "pass_yards",
    "pass_completions":        "pass_completions",
    "completions":             "pass_completions",
    "pass_tds":                "pass_tds",
    "passing_tds":             "pass_tds",
    "pass_touchdowns":         "pass_tds",
    "rush_yards":              "rush_yards",
    "rushing_yards":           "rush_yards",
    "receptions":              "receptions",
    "receiving_yards":         "receiving_yards",
    "rec_yards":               "receiving_yards",
    "rush+rec_yards":          "rec+rush_yards",
    "rec+rush_yards":          "rec+rush_yards",
    "rushing+receiving_yards": "rec+rush_yards",
}

POSITION_OPTIONS = ["QB", "RB", "WR", "TE"]

# Which volume stat actually drives each prop — used by the usage factor to ask
# "is this player getting more work lately?" rather than just "is he producing?"
PROP_VOLUME_COLS = {
    "pass_yards":       ["attempts"],
    "pass_completions": ["attempts"],
    "pass_tds":         ["attempts"],
    "rush_yards":       ["carries"],
    "receptions":       ["targets"],
    "receiving_yards":  ["targets"],
    "rec+rush_yards":   ["targets", "carries"],
}

# Scoring weights — must sum to 1.0. "market_signal" is populated from
# Kalshi/Polymarket cross-market data when available (markets/discrepancy.py);
# defaults to neutral (0.5) when no market data exists for a prop.
#
# Weighting differs from NBA on purpose: NFL usage (target share / air yards /
# snap share) is the single most predictive input for skill-position props and
# swings week to week, while head-to-head matchup history is thin (1-2 games a
# season) and home/away splits are small over a 17-game sample.
WEIGHTS = {
    "form":           0.24,  # recent form vs the line over the last N_GAMES
    "defense":        0.20,  # opponent defense vs this position for this stat
    "usage":          0.18,  # target/carry share, air yards share, snap share
    "matchup":        0.12,  # historical performance vs this opponent
    "teammate_out":   0.11,  # vacated target/carry share from a missing teammate
    "rest":           0.06,  # short week (Thursday) vs normal vs post-bye
    "home_away":      0.04,  # home/away split
    "market_signal":  0.05,  # Kalshi/Polymarket volume-weighted cross-market signal
}
