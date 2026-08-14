# core/picks.py — generate picks and parlay suggestions, sport-agnostic

from core.scoring import calculate_confidence, categorize_pick


def _attach_market_signal(sport_module, entry):
    """
    Best-effort enrichment: looks up a volume-weighted Kalshi/Polymarket signal
    for this prop and attaches it as market_signal_score, which each sport's
    "market_signal" factor reads (defaults to neutral 0.5 if unavailable).
    Failures here should never block scoring — market data is a nice-to-have.
    """
    if "market_signal_score" in entry:
        return entry
    try:
        from markets.discrepancy import get_market_signal_score
        entry = dict(entry)
        entry["market_signal_score"] = get_market_signal_score(
            sport_module.key,
            entry.get("player_name"),
            entry.get("team"),
            entry.get("opponent"),
            entry.get("prop_type"),
            entry.get("line"),
        )
    except Exception:
        pass
    return entry


def generate_picks(sport_module, game_slate, missing_teammates=None):
    """
    Generates confidence-scored picks for a slate of props in a single sport.

    sport_module: a SportModule instance
    game_slate: list of dicts (see that sport's build_context() for required keys)
    missing_teammates: dict mapping player_name -> teammate_name who is out
                        (only meaningful for sports whose factor set uses it)

    Returns a sorted list of pick dicts with confidence and bucket.
    """
    if missing_teammates is None:
        missing_teammates = {}

    picks = []
    for entry in game_slate:
        player = entry.get("player_name")
        entry = dict(entry)
        entry.setdefault("missing_teammate", missing_teammates.get(player))
        entry = _attach_market_signal(sport_module, entry)

        result = calculate_confidence(sport_module, entry)

        if "error" in result:
            print(f"  Skipping {player}: {result['error']}")
            continue

        result["bucket"] = categorize_pick(result["confidence"])
        result["team"]   = entry.get("team", "")
        result["sport"]  = sport_module.key
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

    Works across sports transparently since it only reads confidence/bucket.
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
    """Pretty prints picks grouped by bucket (CLI use)."""
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
    """Pretty prints parlay suggestions (CLI use)."""
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
