# config.py — central settings for the sports betting algorithm

SEASON = "2025-26"
N_GAMES = 10          # default lookback window for streak analysis
MIN_GAMES_VS_TEAM = 3 # minimum games vs a team to use matchup data

# Confidence thresholds for pick buckets
EASY_THRESHOLD       = 0.62  # 62%+ confidence
MODERATE_THRESHOLD   = 0.56  # 56–62% confidence
AGGRESSIVE_THRESHOLD = 0.50  # 50–56% confidence

# Scoring weights — must sum to 1.0
WEIGHTS = {
    "streak":       0.25,  # recent form vs the line
    "matchup":      0.20,  # historical performance vs this team
    "defense":      0.20,  # opponent defensive rating vs position
    "home_away":    0.10,  # home/away split
    "rest":         0.10,  # days of rest / back to back
    "usage":        0.10,  # usage rate impact from teammate availability
    "line_movement": 0.05, # sharp money signal
}

# PrizePicks NBA league ID
PRIZEPICKS_NBA_ID = 7

# Prop types we analyze
PROP_TYPES = [
    "points",
    "rebounds",
    "assists",
    "pts+reb+ast",
    "three_pointers_made",
]
