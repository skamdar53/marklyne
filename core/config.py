# core/config.py — sport-agnostic settings shared across all sport modules

# Confidence thresholds for pick buckets (shared across sports so buckets
# mean the same thing everywhere in the UI)
EASY_THRESHOLD       = 0.62  # 62%+ confidence
MODERATE_THRESHOLD   = 0.56  # 56-62% confidence
AGGRESSIVE_THRESHOLD = 0.50  # 50-56% confidence

# PrizePicks payout multipliers (after the house cut)
SINGLE_PAYOUT  = 0.91
PARLAY_PAYOUTS = {2: 3.0, 3: 5.0, 4: 10.0, 5: 20.0, 6: 25.0}

# Bet sizing as % of current bankroll per bucket
BET_SIZE = {
    "easy":       0.03,
    "moderate":   0.02,
    "aggressive": 0.01,
}

# Weight given to the market-discrepancy/volume-weighting factor when
# Kalshi/Polymarket data is available for a prop. Each sport's own WEIGHTS
# dict reserves this much room under the "market_signal" key.
MARKET_SIGNAL_WEIGHT = 0.05
