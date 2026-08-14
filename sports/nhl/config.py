# sports/nhl/config.py — NHL-specific settings

# NHL season ids are "YYYYYYYY" (start year + end year), e.g. "20252026".
SEASON = "20252026"
GAME_TYPE = 2            # 2 = regular season, 3 = playoffs
N_GAMES = 10             # default lookback window for streak analysis
MIN_GAMES_VS_TEAM = 3    # minimum games vs a team to use matchup data

# Seasons pulled for backtesting, oldest first.
BACKTEST_SEASONS = ["20232024", "20242025", "20252026"]

# api-web.nhle.com game logs expose goals/assists/points/shots/powerPlayPoints
# for skaters and shotsAgainst/goalsAgainst for goalies. They do NOT expose
# blocked shots, so "blocked_shots" is deliberately left out — there's no
# per-game source for it short of pulling every boxscore individually.
PROP_TYPES = [
    "shots_on_goal",
    "points",
    "goals",
    "assists",
    "power_play_points",
    "saves",
]

PROP_TYPE_LABELS = {
    "shots_on_goal":     "SOG",
    "points":            "Points",
    "goals":             "Goals",
    "assists":           "Assists",
    "power_play_points": "PP Points",
    "saves":             "Saves",
}

# Prop types that only apply to goalies — scored with a separate factor path.
GOALIE_PROP_TYPES = {"saves"}

POSITION_OPTIONS = ["C", "LW", "RW", "D", "G"]

# Kalshi/Polymarket and MoneyPuck occasionally use short team codes that don't
# match the NHL's official tricodes.
TEAM_ABBR_ALIASES = {
    "la":  "LAK",
    "sj":  "SJS",
    "tb":  "TBL",
    "nj":  "NJD",
    "veg": "VGK",
    "vgs": "VGK",
    "lv":  "VGK",
    "was": "WSH",
    "wsh": "WSH",
    "cls": "CBJ",
    "clb": "CBJ",
    "mon": "MTL",
    "wpj": "WPG",
    "ana": "ANA",
    "utah": "UTA",
    "uta": "UTA",
    "ari": "ARI",
    "phx": "ARI",
}

# Scoring weights — must sum to 1.0. "market_signal" is populated from
# Kalshi/Polymarket cross-market data when available (markets/discrepancy.py);
# defaults to neutral (0.5) when no market data exists for a prop.
WEIGHTS = {
    "streak":        0.22,  # recent form vs the line
    "matchup":       0.12,  # historical performance vs this team
    "opp_defense":   0.16,  # opponent shot/goal suppression (or shot volume, for goalies)
    "goalie_edge":   0.11,  # opposing goalie save% / goals saved above expected
    "possession":    0.11,  # Corsi%/Fenwick% shot-attempt differential (MoneyPuck)
    "special_teams": 0.08,  # opponent PK% vs own PP%
    "home_away":     0.05,  # home/away split
    "rest":          0.05,  # days of rest / back to back
    "usage":         0.05,  # ice-time/usage bump when a linemate is out
    "market_signal": 0.05,  # Kalshi/Polymarket volume-weighted cross-market signal
}
