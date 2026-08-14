# core/scoring.py — sport-agnostic confidence scoring engine
#
# This module knows nothing about basketball, baseball, football, or hockey.
# Each sport module (sports/nba, sports/mlb, sports/nfl, sports/nhl) supplies
# its own WEIGHTS dict and FACTOR_FUNCS mapping (factor name -> function that
# takes a context dict and returns a float 0.0-1.0); this engine just combines
# them into a single confidence score. Swapping sports never touches this file.

from core.config import EASY_THRESHOLD, MODERATE_THRESHOLD, AGGRESSIVE_THRESHOLD


def categorize_pick(confidence):
    """Buckets a confidence score into easy/moderate/aggressive/skip."""
    if confidence >= EASY_THRESHOLD:
        return "easy"
    elif confidence >= MODERATE_THRESHOLD:
        return "moderate"
    elif confidence >= AGGRESSIVE_THRESHOLD:
        return "aggressive"
    return "skip"


def calculate_confidence(sport_module, entry):
    """
    Master scoring function, generic across sports.

    sport_module: a SportModule instance (see sports/base.py)
    entry: dict describing the prop to score — player_name, opponent,
           prop_type, line, plus whatever sport-specific context keys
           that sport's build_context() expects (is_home, is_b2b,
           position, missing_teammate, etc.)

    Returns a dict with confidence, breakdown, and reasons — same shape
    regardless of sport, since the UI renders it generically.
    """
    context = sport_module.build_context(entry)
    if context is None:
        return {"error": f"Could not resolve entry for {sport_module.label}: {entry.get('player_name')}"}
    if context.get("error"):
        return {"error": context["error"]}

    def _safe(fn, fallback=0.5):
        # A pandas .mean()/.std() on an empty or all-NaN slice returns NaN
        # rather than raising, so it slips past a bare try/except and would
        # otherwise corrupt the whole weighted sum silently. Catch it here
        # once so no individual sport's factor functions have to guard for it.
        try:
            value = fn(context)
        except Exception:
            return fallback
        try:
            if value != value:  # NaN != NaN is the cheapest isnan check
                return fallback
        except Exception:
            return fallback
        return value

    scores = {}
    for factor_name in sport_module.weights:
        fn = sport_module.factor_funcs[factor_name]
        scores[factor_name] = _safe(fn)

    confidence = sum(sport_module.weights[k] * scores[k] for k in sport_module.weights)
    reasons = sport_module.build_reasoning(context, scores)

    return {
        "player":     entry.get("player_name"),
        "opponent":   entry.get("opponent"),
        "prop":       entry.get("prop_type"),
        "line":       entry.get("line"),
        "confidence": round(confidence, 4),
        "breakdown":  {k: round(v, 4) for k, v in scores.items()},
        "reasons":    reasons,
    }
