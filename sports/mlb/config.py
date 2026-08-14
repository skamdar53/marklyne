# sports/mlb/config.py — MLB-specific settings

SEASON = 2026
N_GAMES = 15           # default lookback window for streak analysis (baseball is
                       # noisier per-game than basketball, so the window is wider)
MIN_GAMES_VS_TEAM = 5  # minimum games vs a team to use matchup data
STATCAST_WINDOW_DAYS = 45  # trailing window for Statcast quality-of-contact pulls

BACKTEST_SEASONS = [2025, 2024, 2023]

PROP_TYPES = [
    "hits",
    "total_bases",
    "home_runs",
    "rbis",
    "runs",
    "hits+runs+rbis",
    "pitcher_strikeouts",
]

PROP_TYPE_LABELS = {
    "hits":               "Hits",
    "total_bases":        "Total Bases",
    "home_runs":          "Home Runs",
    "rbis":               "RBIs",
    "runs":               "Runs",
    "hits+runs+rbis":     "H+R+RBI",
    "pitcher_strikeouts": "Pitcher Ks",
}

# Props thrown by a pitcher rather than produced by a batter. These read the
# "pitching" stat group out of the MLB Stats API instead of "hitting", and
# several factors flip meaning for them (see factors.py).
PITCHER_PROP_TYPES = {"pitcher_strikeouts"}

# Nothing in build_context branches on a defensive position — the pitcher/batter
# split is driven by prop_type via PITCHER_PROP_TYPES — so the manual-slate UI
# only needs the two buckets that actually change how a prop is scored.
POSITION_OPTIONS = ["Batter", "Pitcher"]

# Synthetic backtest lines are floored here so that inferring a line from a
# trailing average never produces an unbeatable 0.0 on a low-count prop (no
# sportsbook posts a home-run line below 0.5 either).
MIN_LINE = 0.5

# Scoring weights — must sum to 1.0. "market_signal" is populated from
# Kalshi/Polymarket cross-market data when available (markets/discrepancy.py);
# defaults to neutral (0.5) when no market data exists for a prop.
#
# There is deliberately no standalone home/away factor: in baseball the venue
# effect is the park itself, and "park_factor" already resolves to whichever
# stadium the game is actually in, so a separate home/away split would double
# count it. There is likewise no NBA-style "usage" factor — a teammate sitting
# does not redistribute plate appearances the way it redistributes shots. The
# MLB-native equivalent is "lineup_spot", which tracks batting-order position
# (and therefore PA volume).
WEIGHTS = {
    "streak":          0.25,  # recent form vs the line
    "pitcher_matchup": 0.20,  # opposing starter's quality (or, for pitcher props,
                              # the opposing lineup's strikeout tendency)
    "handedness":      0.15,  # platoon edge — batter vs pitcher-hand splits
    "bullpen":         0.10,  # opposing team's relief corps quality
    "park_factor":     0.10,  # Statcast park factors for this stat at this venue
    "lineup_spot":     0.08,  # batting order slot / trailing plate appearances
    "rest":            0.07,  # pitcher days between starts, batter team rest
    "market_signal":   0.05,  # Kalshi/Polymarket volume-weighted cross-market signal
}
