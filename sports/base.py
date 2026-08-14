# sports/base.py — the contract every sport module implements.
#
# core/scoring.py, core/picks.py, backtest/simulator.py, and app.py are all
# written against this interface and never import a specific sport directly
# (except sports/registry.py, which wires the four together). Adding a new
# sport means writing one new package under sports/ that satisfies this.

from abc import ABC, abstractmethod


class SportModule(ABC):
    # ── Identity ──────────────────────────────────────────────────────────
    key: str                    # "nba", "mlb", "nfl", "nhl" — used as a dict key everywhere
    label: str                  # "NBA", "MLB", "NFL", "NHL" — display name
    icon: str                   # emoji for the UI

    # ── PrizePicks integration ───────────────────────────────────────────
    prizepicks_league_id: int
    prop_types: list           # our normalized prop type names for this sport
    prop_type_labels: dict     # prop_type -> human label, e.g. "pts+reb+ast" -> "PRA"

    # ── Manual slate UI ──────────────────────────────────────────────────
    position_options: list     # e.g. ["PG","SG","SF","PF","C"] for NBA, [] if not applicable

    # ── Scoring ───────────────────────────────────────────────────────────
    weights: dict               # factor_name -> weight (float), must sum to ~1.0
    factor_funcs: dict          # factor_name -> function(context) -> float 0.0-1.0

    @abstractmethod
    def build_context(self, entry):
        """
        Resolves a raw slate entry (player_name, opponent, prop_type, line,
        plus sport-specific keys like is_home/is_b2b/position/missing_teammate)
        into a fully-loaded context dict that factor_funcs can read from.

        Returns None or a dict with an "error" key if the entry can't be
        resolved (e.g. unknown player/team) — core.scoring treats either as
        a scoring failure and skips the prop.
        """
        raise NotImplementedError

    @abstractmethod
    def build_reasoning(self, context, scores):
        """
        Returns a list of (sentiment, text) tuples ("positive"/"negative"/
        "neutral") explaining why a prop scored the way it did, for display
        in the UI's expander.
        """
        raise NotImplementedError

    @abstractmethod
    def get_todays_games(self):
        """
        Returns today's scheduled games for this sport as a list of dicts:
        {home_team, away_team, home_team_id, away_team_id} (team abbreviations).
        """
        raise NotImplementedError

    @abstractmethod
    def build_auto_slate(self, missing_teammates=None, lines=None):
        """
        Pulls current PrizePicks lines for this sport (or uses the ones
        passed in) and enriches them with schedule context (home/away,
        rest/back-to-back) to build a slate ready for core.picks.generate_picks.
        """
        raise NotImplementedError

    @abstractmethod
    def backtest_games(self, player_name, opponent_team, prop_type, position=None):
        """
        Generator that yields one dict per historical game played against
        opponent_team, using ONLY data available before that game (no
        lookahead). Each dict is either:

          {"game_date": ..., "skip_reason": "<why this game was skipped>"}

        or a fully scored game:

          {"game_date": ..., "line": float, "actual": float, "hit": bool,
           "confidence": float, "reasons": [(sentiment, text), ...]}

        backtest/simulator.py consumes this stream directly and feeds it
        into a Bankroll — it has no sport-specific knowledge itself.
        """
        raise NotImplementedError
