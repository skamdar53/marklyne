# markets/lines.py — build the live prop slate directly from Kalshi + Polymarket
#
# Replaces PrizePicks as the line source. Both platforms' player-prop markets
# (Kalshi's floor_strike, Polymarket's "O/U" questions) already state a
# threshold the same way PrizePicks states a line, so there's no need for a
# middleman prop book at all — and no DataDome to fight.
#
# This module only produces the raw slate (player_name, prop_type, line).
# Team/opponent/home-away/rest resolution stays inside each sport's own
# build_auto_slate(), exactly as when lines came from PrizePicks. The
# market_signal factor is unaffected: core.picks still calls
# markets.discrepancy.get_market_signal_score() for every slate entry,
# it's just now also describing the very market the line was sourced from.

from markets import kalshi, polymarket
from markets.discrepancy import line_tolerance, normalize_name, slate_start


def get_market_prop_lines(sport_key, prop_types):
    """
    Live player-prop lines for a sport, sourced from Kalshi + Polymarket and
    de-duplicated across platforms.

    prop_types: our normalized prop type names to keep (anything else either
                platform quotes is dropped — sport modules only know how to
                score the props they define factors for).

    Returns a list of dicts: {player_name, prop_type, line}. Where both
    platforms quote the same bet (threshold within tolerance), they're
    merged into one row using whichever platform carries more combined
    volume; different thresholds for the same player/stat are kept as
    separate rows, since they're genuinely different bets. Best-effort: a
    fetch failure on one platform just degrades to whatever the other one
    has, never raises.
    """
    sport = (sport_key or "").lower()
    wanted = set(prop_types or [])

    candidates = []  # (player_name, prop_type, threshold, volume)

    try:
        for row in kalshi.get_player_prop_markets(sport):
            if row.get("prop_type") not in wanted or row.get("threshold") is None:
                continue
            candidates.append((row["player_name"], row["prop_type"],
                                float(row["threshold"]), float(row.get("volume") or 0.0)))
    except Exception as e:
        print(f"Kalshi lines fetch error ({sport}): {e}")

    try:
        for row in polymarket.get_player_prop_markets(sport, slate_start()):
            if row.get("prop_type") not in wanted or row.get("threshold") is None:
                continue
            candidates.append((row["player_name"], row["prop_type"],
                                float(row["threshold"]), float(row.get("volume") or 0.0)))
    except Exception as e:
        print(f"Polymarket lines fetch error ({sport}): {e}")

    # Group by (normalized player name, prop type), then cluster thresholds
    # within tolerance so the same bet quoted on both platforms collapses to
    # one line instead of showing up twice.
    grouped = {}
    for name, prop_type, threshold, volume in candidates:
        key = (normalize_name(name), prop_type)
        grouped.setdefault(key, []).append(
            {"player_name": name, "threshold": threshold, "volume": volume})

    lines = []
    for (_, prop_type), rows in grouped.items():
        rows.sort(key=lambda r: r["threshold"])
        clusters = []
        for r in rows:
            for c in clusters:
                if abs(c["threshold"] - r["threshold"]) <= line_tolerance(c["threshold"]):
                    c["rows"].append(r)
                    break
            else:
                clusters.append({"threshold": r["threshold"], "rows": [r]})

        for c in clusters:
            # Canonical line = whichever platform's threshold carries the
            # most volume — the more-traded price is the better estimate of
            # where the real line sits.
            best = max(c["rows"], key=lambda r: r["volume"])
            lines.append({
                "player_name": best["player_name"],
                "prop_type":   prop_type,
                "line":        best["threshold"],
            })

    return lines
