# sports/nba/config.py — NBA-specific settings

SEASON = "2025-26"
N_GAMES = 10          # default lookback window for streak analysis
MIN_GAMES_VS_TEAM = 3 # minimum games vs a team to use matchup data

PRIZEPICKS_LEAGUE_ID = 7

PROP_TYPES = [
    "points",
    "rebounds",
    "assists",
    "pts+reb+ast",
    "three_pointers_made",
]

PROP_TYPE_LABELS = {
    "points":               "Points",
    "rebounds":             "Rebounds",
    "assists":              "Assists",
    "pts+reb+ast":          "PRA",
    "three_pointers_made":  "3PM",
}

# Maps PrizePicks' raw stat_type (lowercased, spaces -> underscores) to our
# normalized prop type names
PRIZEPICKS_PROP_MAP = {
    "points":                   "points",
    "rebounds":                 "rebounds",
    "assists":                  "assists",
    "points+rebounds+assists":  "pts+reb+ast",
    "3-pt_made":                "three_pointers_made",
    "3_pointers_made":          "three_pointers_made",
}

POSITION_OPTIONS = ["PG", "SG", "SF", "PF", "C"]

# Scoring weights — must sum to 1.0. "market_signal" is populated from
# Kalshi/Polymarket cross-market data when available (markets/discrepancy.py);
# defaults to neutral (0.5) when no market data exists for a prop.
WEIGHTS = {
    "streak":         0.25,  # recent form vs the line
    "matchup":        0.20,  # historical performance vs this team
    "defense":        0.20,  # opponent defensive rating vs position + shot zone
    "home_away":      0.10,  # home/away split
    "rest":           0.10,  # days of rest / back to back
    "usage":          0.10,  # usage rate impact from teammate availability
    "market_signal":  0.05,  # Kalshi/Polymarket volume-weighted cross-market signal
}
