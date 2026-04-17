# algorithm/picks.py — generate picks and parlay suggestions

from algorithm.scoring import calculate_confidence
from data.lines import get_prizepicks_lines
from config import EASY_THRESHOLD, MODERATE_THRESHOLD, AGGRESSIVE_THRESHOLD


def categorize_pick(confidence):
    """Buckets a confidence score into easy/moderate/aggressive/skip."""
    if confidence >= EASY_THRESHOLD:
        return "easy"
    elif confidence >= MODERATE_THRESHOLD:
        return "moderate"
    elif confidence >= AGGRESSIVE_THRESHOLD:
        return "aggressive"
    else:
        return "skip"


def generate_picks(game_slate, missing_teammates=None):
    """
    Generates confidence-scored picks for a list of games.

    game_slate: list of dicts with keys:
        - player_name
        - opponent
        - prop_type
        - line
        - is_home
        - is_b2b
        - position

    missing_teammates: dict mapping player_name -> teammate_name who is out

    Returns a sorted list of pick dicts with confidence and bucket.
    """
    if missing_teammates is None:
        missing_teammates = {}

    picks = []
    for game in game_slate:
        player  = game["player_name"]
        missing = missing_teammates.get(player)

        result = calculate_confidence(
            player_name=player,
            opponent_team=game["opponent"],
            prop_type=game["prop_type"],
            line=game["line"],
            is_home=game.get("is_home", True),
            is_b2b=game.get("is_b2b", False),
            missing_teammate=missing,
            position=game.get("position", "SF"),
        )

        if "error" in result:
            print(f"  Skipping {player}: {result['error']}")
            continue

        result["bucket"] = categorize_pick(result["confidence"])
        result["team"]   = game.get("team", "")
        picks.append(result)

    return sorted(picks, key=lambda x: x["confidence"], reverse=True)


def _make_parlay(legs, label):
    combined = 1.0
    for p in legs:
        combined *= p["confidence"]
    return {
        "legs":                legs,
        "combined_confidence": round(combined, 4),
        "size":                len(legs),
        "label":               label,
    }


def generate_parlays(picks):
    """
    Generates three parlay tiers from the scored picks:
      - Safe (2-leg):       top 2 Easy picks
      - Standard (3-leg):   top 3 Easy + Moderate picks
      - Aggressive (4-leg): top 4 picks from any non-skip bucket

    Returns list of parlay dicts.
    """
    easy      = [p for p in picks if p["bucket"] == "easy"]
    em        = [p for p in picks if p["bucket"] in ("easy", "moderate")]
    any_valid = [p for p in picks if p["bucket"] != "skip"]

    parlays = []

    if len(easy) >= 2:
        parlays.append(_make_parlay(easy[:2], "2-leg safe (Easy only)"))

    if len(em) >= 3:
        parlays.append(_make_parlay(em[:3], "3-leg standard (Easy + Moderate)"))

    if len(any_valid) >= 4:
        parlays.append(_make_parlay(any_valid[:4], "4-leg aggressive"))

    return parlays


def display_picks(picks):
    """Pretty prints picks grouped by bucket."""
    buckets = {"easy": [], "moderate": [], "aggressive": [], "skip": []}
    for p in picks:
        buckets[p["bucket"]].append(p)

    for bucket in ["easy", "moderate", "aggressive"]:
        group = buckets[bucket]
        if not group:
            continue
        print(f"\n{'='*40}")
        print(f"  {bucket.upper()} PICKS ({len(group)})")
        print(f"{'='*40}")
        for p in group:
            print(f"  {p['player']} — {p['prop']} {p['line']} "
                  f"({p['confidence']*100:.1f}% confidence)")
            print(f"    vs {p['opponent']}")
            print(f"    Breakdown: " +
                  ", ".join(f"{k}: {v*100:.0f}%" for k, v in p["breakdown"].items()))


def display_parlays(parlays):
    """Pretty prints parlay suggestions."""
    if not parlays:
        print("\nNo parlay suggestions.")
        return
    print(f"\n{'='*40}")
    print("  PARLAY SUGGESTIONS")
    print(f"{'='*40}")
    for i, parlay in enumerate(parlays, 1):
        label = parlay.get("label", f"{parlay['size']}-leg parlay")
        print(f"\n  Parlay {i} — {label} "
              f"({parlay['combined_confidence']*100:.1f}% combined)")
        for leg in parlay["legs"]:
            print(f"    • {leg['player']} {leg['prop']} {leg['line']} "
                  f"({leg['confidence']*100:.1f}%)")
