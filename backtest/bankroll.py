# backtest/bankroll.py — fake money tracker with bet sizing strategies

from config import EASY_THRESHOLD, MODERATE_THRESHOLD, AGGRESSIVE_THRESHOLD

# PrizePicks payout multipliers (after the house cut)
# Single pick pays ~0.91x (like -110 in traditional betting)
# Parlays pay fixed multipliers
SINGLE_PAYOUT  = 0.91
PARLAY_PAYOUTS = {2: 3.0, 3: 5.0, 4: 10.0, 5: 20.0, 6: 25.0}

# Bet sizing as % of current bankroll per bucket
BET_SIZE = {
    "easy":       0.03,   # 3% of bankroll
    "moderate":   0.02,   # 2% of bankroll
    "aggressive": 0.01,   # 1% of bankroll
}


class Bankroll:
    """
    Tracks a fake bankroll across a series of bets.
    Supports flat betting, percentage betting, and parlay simulation.
    """

    def __init__(self, starting_balance=1000.0):
        self.balance      = starting_balance
        self.start        = starting_balance
        self.history      = []   # list of bet records
        self.wins         = 0
        self.losses       = 0
        self.skipped      = 0

    def _get_bucket(self, confidence):
        if confidence >= EASY_THRESHOLD:
            return "easy"
        elif confidence >= MODERATE_THRESHOLD:
            return "moderate"
        elif confidence >= AGGRESSIVE_THRESHOLD:
            return "aggressive"
        return "skip"

    def place_bet(self, confidence, hit, label="", reasons=None, force=False):
        """
        Places a single bet based on confidence level.
        Uses percentage-of-bankroll sizing.

        confidence: float 0.0–1.0
        hit: bool — did the player go over the line?
        label: description for logging
        force: if True, bet even on skip-tier picks (used in backtesting)
        """
        bucket = self._get_bucket(confidence)
        if bucket == "skip":
            if not force:
                self.skipped += 1
                return
            # In forced mode, use minimum sizing for low-confidence picks
            bucket = "aggressive"

        bet_pct  = BET_SIZE[bucket]
        bet_size = round(self.balance * bet_pct, 2)

        if hit:
            profit = round(bet_size * SINGLE_PAYOUT, 2)
            self.balance += profit
            self.wins    += 1
            outcome = f"+${profit}"
        else:
            self.balance -= bet_size
            self.losses  += 1
            outcome = f"-${bet_size}"

        self.history.append({
            "label":      label,
            "bucket":     bucket,
            "confidence": confidence,
            "bet_size":   bet_size,
            "hit":        hit,
            "outcome":    outcome,
            "balance":    round(self.balance, 2),
            "reasons":    reasons or [],
        })

    def place_parlay(self, legs, hits):
        """
        Simulates a parlay bet.

        legs: list of (confidence, label) tuples
        hits: list of bools — did each leg hit?

        Uses 1% of bankroll as the parlay stake regardless of bucket.
        """
        n = len(legs)
        if n < 2 or n > 6:
            return

        payout_mult = PARLAY_PAYOUTS.get(n, 1.0)
        stake = round(self.balance * 0.01, 2)
        all_hit = all(hits)

        if all_hit:
            profit = round(stake * payout_mult, 2)
            self.balance += profit
            self.wins    += 1
            outcome = f"+${profit} ({n}-leg parlay)"
        else:
            self.balance -= stake
            self.losses  += 1
            outcome = f"-${stake} ({n}-leg parlay, missed)"

        labels = " + ".join(l for _, l in legs)
        self.history.append({
            "label":      labels,
            "bucket":     "parlay",
            "confidence": None,
            "bet_size":   stake,
            "hit":        all_hit,
            "outcome":    outcome,
            "balance":    round(self.balance, 2),
        })

    def merge(self, other):
        """Merges another Bankroll's history into this one (for combined results)."""
        self.history  += other.history
        self.wins     += other.wins
        self.losses   += other.losses
        self.skipped  += other.skipped
        # Recalculate balance from merged history
        self.balance = self.start
        for bet in self.history:
            amount = float(bet["outcome"].replace("+$", "").replace("-$", ""))
            if bet["outcome"].startswith("+"):
                self.balance += amount
            else:
                self.balance -= amount

    def roi(self):
        """Returns return on investment as a percentage."""
        return round(((self.balance - self.start) / self.start) * 100, 2)

    def win_rate(self):
        total = self.wins + self.losses
        return round(self.wins / total * 100, 2) if total > 0 else 0

    def summary(self):
        """Prints a full bankroll summary."""
        total = self.wins + self.losses
        print(f"\n  Starting balance:  ${self.start:,.2f}")
        print(f"  Final balance:     ${self.balance:,.2f}")
        print(f"  Net profit/loss:   ${self.balance - self.start:+,.2f}")
        print(f"  ROI:               {self.roi()}%")
        print(f"  Win rate:          {self.win_rate()}% ({self.wins}W / {self.losses}L)")
        print(f"  Bets placed:       {total}")
        print(f"  Skipped (low conf):{self.skipped}")

    def print_history(self):
        """Prints every bet in the history."""
        print(f"\n{'─'*70}")
        for bet in self.history:
            conf = f"{bet['confidence']*100:.1f}%" if bet["confidence"] else "parlay"
            print(f"  [{bet['bucket']:10}] {conf:6} | {bet['outcome']:12} | "
                  f"Balance: ${bet['balance']:,.2f} | {bet['label']}")
        print(f"{'─'*70}")
