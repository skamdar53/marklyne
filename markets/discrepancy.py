# markets/discrepancy.py — turn prediction-market prices into a scoring signal
#
# Two entry points:
#
#   get_market_signal_score(...)   -> float 0.0-1.0, consumed by every sport
#                                     module's "market_signal" factor (weight
#                                     0.05, see core/config.MARKET_SIGNAL_WEIGHT).
#                                     0.5 means "no signal"; that's also every
#                                     sport module's default when we're silent.
#
#   get_platform_discrepancies(...) -> Kalshi vs Polymarket price gaps on the
#                                      same game, for the "Prediction Markets"
#                                      UI tab. Informational only, never fed
#                                      back into scoring.
#
# Best-effort throughout: any failure anywhere returns a neutral 0.5 / empty
# list rather than raising. Market data must never break a scoring run.

import unicodedata
from datetime import datetime, timedelta, timezone

import streamlit as st

from markets import kalshi, polymarket

# ── Blending constants ────────────────────────────────────────────────────
#
# VOLUME_HALF_WEIGHT: the traded volume at which we give a market half of the
# confidence we'd give an infinitely liquid one. Calibrated off the live
# distribution of Kalshi player-prop volumes (median traded contract volume
# sits around 150-2,400 depending on the stat, p90 around 1,300-12,700), so
# ~2,500 puts the inflection right around a genuinely active market.
VOLUME_HALF_WEIGHT = 2500.0

# How far a market's threshold may sit from our line before we stop believing
# it describes the same bet. Scales with the line so it works for both a 0.5
# home-run prop and a 275.5 passing-yards prop.
def line_tolerance(line):
    return max(1.0, abs(line) * 0.15)

# Widest bid/ask we'll treat as a real price. Past this the "market opinion"
# is mostly noise, so we shrink toward neutral.
MAX_CREDIBLE_SPREAD = 0.20

# How much of a game-outcome win probability's lean survives into the signal.
# This is a deliberately weak proxy — a team being favored says very little
# about one player's box score — so it can move the score at most +/-0.15.
GAME_PROXY_WEIGHT = 0.30

# Beyond this distance from a coin flip, a lopsided game starts implying
# garbage time / pulled starters, which cuts against the proxy. Damps the
# lean rather than reversing it.
BLOWOUT_ONSET = 0.30

# Never let a single 5%-weight factor return a hard 0 or 1.
SCORE_FLOOR = 0.02
SCORE_CEIL = 0.98

# Kalshi and Polymarket agree on team codes for 28 of 30 MLB teams; these are
# the exceptions plus the usual cross-league abbreviation variants. Values are
# the canonical code we normalize both platforms onto.
TEAM_CODE_ALIASES = {
    # MLB
    "ATH": "OAK", "AZ": "ARI", "CHW": "CWS", "SFG": "SF", "SDP": "SD",
    "TBR": "TB", "KCR": "KC", "WAS": "WSH", "NYA": "NYY", "NYN": "NYM",
    # NBA
    "GS": "GSW", "PHO": "PHX", "NO": "NOP", "SA": "SAS", "NY": "NYK",
    "BRK": "BKN", "UTAH": "UTA", "CHO": "CHA",
    # NFL
    "JAC": "JAX", "LAR": "LA", "WFT": "WAS", "OAK_NFL": "LV", "SD_NFL": "LAC",
    # NHL
    "TB_NHL": "TBL", "LA_NHL": "LAK", "SJ": "SJS", "NJ": "NJD",
    "VEG": "VGK", "WPG": "WPG", "MON": "MTL",
}


def _canon_code(code):
    """Normalize a team abbreviation onto a canonical code."""
    c = (code or "").strip().upper()
    return TEAM_CODE_ALIASES.get(c, c)


def normalize_name(name):
    """
    Fold a person/team name to a comparable form: strip accents, punctuation,
    case, and common generational suffixes. 'Ronald Acuña Jr.' -> 'ronald acuna'.
    """
    if not name:
        return ""
    text = unicodedata.normalize("NFKD", str(name))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = "".join(ch if ch.isalnum() or ch.isspace() else " " for ch in text)
    tokens = [t for t in text.lower().split() if t not in
              ("jr", "sr", "ii", "iii", "iv", "v")]
    return " ".join(tokens)


def names_match(a, b):
    """
    Same player? Exact normalized match, or last name plus first initial —
    which covers 'C. Seager' / 'Corey Seager' and most feed disagreements
    without collapsing distinct players onto each other.
    """
    na, nb = normalize_name(a), normalize_name(b)
    if not na or not nb:
        return False
    if na == nb:
        return True

    ta, tb = na.split(), nb.split()
    if len(ta) < 2 or len(tb) < 2:
        return False
    return ta[-1] == tb[-1] and ta[0][:1] == tb[0][:1]


def slate_start():
    """
    Oldest game date we care about, as "YYYY-MM-DD": yesterday, so games that
    started late last night and haven't settled yet still count. Day
    granularity keeps the @st.cache_data key stable within a day.
    """
    return (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")


# ── Confidence components ─────────────────────────────────────────────────

def _volume_confidence(volume):
    """
    Saturating volume weight in [0, 1): vol / (vol + VOLUME_HALF_WEIGHT).

    This is the core "volume as confidence" mechanism. An untraded market
    (volume 0) contributes nothing and leaves the score at neutral 0.5; a
    heavily traded one pulls the score most of the way to its implied
    probability. There's no cliff — confidence rises smoothly with liquidity.
    """
    v = max(0.0, float(volume or 0.0))
    return v / (v + VOLUME_HALF_WEIGHT)


def _threshold_confidence(threshold, line):
    """
    Linear decay to 0 as the market's threshold drifts from our line. A
    Kalshi '5+ total bases' market is only evidence about a 4.5 PrizePicks
    line to the extent the two describe the same bet.
    """
    tol = line_tolerance(line)
    gap = abs(float(threshold) - float(line))
    if gap >= tol:
        return 0.0
    return 1.0 - (gap / tol)


def _spread_confidence(spread):
    """Wide or one-sided books are unreliable prices; floor at 0.2, not 0."""
    s = float(spread if spread is not None else 1.0)
    if s <= 0:
        return 1.0
    return max(0.2, min(1.0, 1.0 - (s / MAX_CREDIBLE_SPREAD)))


# ── (a) player-prop derived signal ────────────────────────────────────────

def _best_prop_match(candidates, player_name, prop_type, line):
    """
    Pick the market that best describes our exact bet: right player, right
    stat, threshold closest to our line and inside tolerance.
    """
    best = None
    best_gap = None
    for c in candidates:
        if prop_type and c.get("prop_type") and \
                str(c["prop_type"]).lower() != str(prop_type).lower():
            continue
        if not names_match(c.get("player_name"), player_name):
            continue

        threshold = c.get("threshold")
        if threshold is None:
            continue
        gap = abs(float(threshold) - float(line))
        if gap >= line_tolerance(line):
            continue
        if best_gap is None or gap < best_gap:
            best, best_gap = c, gap

    return best


def _prop_signal(market, line):
    """
    Signal and confidence from one player-prop market.

        lean       = implied_prob(over) - 0.5      in [-0.5, +0.5]
        confidence = volume_conf * threshold_conf * spread_conf
        signal     = 0.5 + lean * confidence

    Returns (signal, confidence). Because |lean| <= 0.5 and confidence <= 1,
    the signal stays inside [0, 1] by construction.
    """
    prob = market.get("implied_prob")
    if prob is None:
        return None, 0.0

    lean = float(prob) - 0.5
    confidence = (
        _volume_confidence(market.get("volume"))
        * _threshold_confidence(market.get("threshold"), line)
        * _spread_confidence(market.get("spread"))
    )
    return 0.5 + lean * confidence, confidence


# ── (b) game-outcome proxy signal ─────────────────────────────────────────

def _game_proxy_signal(win_prob, volume):
    """
    Weak fallback when no player-prop market exists.

        lean    = (win_prob - 0.5), damped once the game gets lopsided past
                  BLOWOUT_ONSET (a projected blowout means garbage time and
                  pulled starters, which cuts against the proxy)
        signal  = 0.5 + lean * GAME_PROXY_WEIGHT * volume_conf

    Capped at +/-0.15 by GAME_PROXY_WEIGHT, so a favored team nudges its
    players' props modestly toward the over and never dominates the factor.
    """
    if win_prob is None:
        return None, 0.0

    lean = float(win_prob) - 0.5
    excess = abs(lean) - BLOWOUT_ONSET
    if excess > 0:
        # Halve the lean at the extreme end of the range.
        lean *= 1.0 - min(1.0, excess / (0.5 - BLOWOUT_ONSET)) * 0.5

    vol_conf = _volume_confidence(volume)
    return 0.5 + lean * GAME_PROXY_WEIGHT * vol_conf, GAME_PROXY_WEIGHT * vol_conf


def _find_team_game_market(rows, team_abbr, opponent_abbr):
    """Locate this team's game-outcome row, preferring a matching opponent."""
    team = _canon_code(team_abbr)
    opp = _canon_code(opponent_abbr)
    if not team:
        return None

    fallback = None
    for r in rows:
        if _canon_code(r.get("team_code")) != team:
            continue
        if opp and _canon_code(r.get("opponent_code")) == opp:
            return r
        if fallback is None:
            fallback = r
    return fallback


# ── Public: the scoring signal ────────────────────────────────────────────

@st.cache_data(ttl=300)
def get_market_signal_score(sport_key, player_name, team_abbr, opponent_abbr,
                            prop_type, line):
    """
    Prediction-market signal for one prop, as a float in 0.0-1.0.

    0.5 = neutral / no signal. >0.5 = markets lean toward the over. <0.5 =
    markets lean under. Sport modules attach this to a slate entry as
    "market_signal_score" before scoring.

    Priority order:

      (a) A player-prop market for this player/stat with a threshold close to
          `line`. Signal = 0.5 + (implied_over_prob - 0.5) * confidence, where
          confidence multiplies three independent discounts:
            - volume:    vol / (vol + 2500)  — the "volume as confidence"
                         mechanism; untraded markets stay at neutral, heavily
                         traded ones pull the score most of the way to the
                         market's own probability
            - threshold: linear decay to 0 as the market's threshold drifts
                         from our line (tolerance = max(1.0, 0.15 * line))
            - spread:    1 - spread/0.20, floored at 0.2, so wide/one-sided
                         books count for less
          When both Kalshi and Polymarket quote the same prop, the two signals
          are averaged weighted by each one's confidence — the more liquid,
          better-matched book dominates.

      (b) Otherwise the team's game-outcome win probability, as a much weaker
          proxy: 0.5 + (win_prob - 0.5) * 0.30 * volume_conf, damped once the
          game is projected past ~80/20 (blowout risk cuts both ways). Bounded
          to roughly [0.35, 0.65].

      (c) No market data for the game at all -> exactly 0.5.
    """
    try:
        line = float(line)
    except (TypeError, ValueError):
        return 0.5

    sport = (sport_key or "").lower()

    # ── (a) player props, Kalshi and Polymarket ──
    signals = []  # (signal, confidence)

    try:
        k_props = kalshi.get_player_prop_markets(sport, prop_type)
        k_match = _best_prop_match(k_props, player_name, prop_type, line)
        if k_match:
            sig, conf = _prop_signal(k_match, line)
            if sig is not None and conf > 0:
                signals.append((sig, conf))
    except Exception as e:
        print(f"Kalshi prop signal error ({player_name} {prop_type}): {e}")

    try:
        p_props = polymarket.get_player_prop_markets(sport, slate_start())
        p_match = _best_prop_match(p_props, player_name, prop_type, line)
        if p_match:
            # Polymarket Gamma exposes no bid/ask, so treat the quoted
            # probability as a tight book and let volume do the discounting.
            p_match = dict(p_match, spread=0.0)
            sig, conf = _prop_signal(p_match, line)
            if sig is not None and conf > 0:
                signals.append((sig, conf))
    except Exception as e:
        print(f"Polymarket prop signal error ({player_name} {prop_type}): {e}")

    if signals:
        total_conf = sum(c for _, c in signals)
        if total_conf > 0:
            blended = sum(s * c for s, c in signals) / total_conf
            return max(SCORE_FLOOR, min(SCORE_CEIL, blended))

    # ── (b) game-outcome proxy ──
    best = None  # (signal, confidence)

    for source, fetch in (
        ("kalshi", lambda: kalshi.get_game_outcome_markets(sport)),
        ("polymarket", lambda: polymarket.get_game_outcome_markets(sport, slate_start())),
    ):
        try:
            row = _find_team_game_market(fetch(), team_abbr, opponent_abbr)
        except Exception as e:
            print(f"{source} game signal error ({team_abbr}): {e}")
            continue
        if not row:
            continue

        sig, conf = _game_proxy_signal(row.get("implied_prob"), row.get("volume"))
        if sig is not None and (best is None or conf > best[1]):
            best = (sig, conf)

    if best and best[1] > 0:
        return max(SCORE_FLOOR, min(SCORE_CEIL, best[0]))

    # ── (c) nothing at all ──
    return 0.5


# ── Public: cross-platform comparison (UI only) ───────────────────────────

def _game_key(row):
    """Order-independent key for a game: the pair of canonical team codes."""
    a = _canon_code(row.get("team_code"))
    b = _canon_code(row.get("opponent_code"))
    if not a or not b:
        return None
    return tuple(sorted((a, b)))


@st.cache_data(ttl=300)
def get_platform_discrepancies(sport_key):
    """
    Games priced on BOTH Kalshi and Polymarket, with each platform's implied
    probability for the same outcome and the gap between them.

    Matching is by canonical team-abbreviation pair. The two platforms agree
    on codes for the large majority of teams (28/30 in MLB), and
    TEAM_CODE_ALIASES patches the known exceptions — but this is inherently
    fuzzy and will miss some games, especially around doubleheaders (both
    platforms key a game by its teams, not its start time, in this view).

    Returns a list of dicts sorted by |gap| descending:
        {game, outcome_label, kalshi_prob, polymarket_prob, gap,
         kalshi_volume, polymarket_volume}

    Informational only — nothing here feeds back into scoring.
    """
    sport = (sport_key or "").lower()

    try:
        k_rows = kalshi.get_game_outcome_markets(sport)
    except Exception as e:
        print(f"Kalshi discrepancy fetch error ({sport}): {e}")
        k_rows = []

    try:
        p_rows = polymarket.get_game_outcome_markets(sport, slate_start())
    except Exception as e:
        print(f"Polymarket discrepancy fetch error ({sport}): {e}")
        p_rows = []

    if not k_rows or not p_rows:
        return []

    # Index each platform by (game, team) so we compare like with like.
    k_index = {}
    for r in k_rows:
        key = _game_key(r)
        if key:
            k_index[(key, _canon_code(r.get("team_code")))] = r

    out = []
    seen = set()
    for p in p_rows:
        key = _game_key(p)
        if not key:
            continue
        team = _canon_code(p.get("team_code"))
        k = k_index.get((key, team))
        if not k:
            continue

        pair = (key, team)
        if pair in seen:
            continue
        seen.add(pair)

        k_prob = k.get("implied_prob")
        p_prob = p.get("implied_prob")
        if k_prob is None or p_prob is None:
            continue

        away, home = key
        out.append({
            "game":              p.get("game") or k.get("game") or f"{away} vs {home}",
            "outcome_label":     f"{p.get('team') or team} to win",
            "kalshi_prob":       round(float(k_prob), 4),
            "polymarket_prob":   round(float(p_prob), 4),
            "gap":               round(abs(float(k_prob) - float(p_prob)), 4),
            "kalshi_volume":     float(k.get("volume") or 0.0),
            "polymarket_volume": float(p.get("volume") or 0.0),
            "market_kind":       "game",
        })

    out.sort(key=lambda r: r["gap"], reverse=True)
    return out


def _prop_platform_discrepancies(sport):
    """
    Player-prop markets priced on BOTH platforms at (near) the same
    threshold, with each platform's implied probability and the gap. Same
    shape as get_platform_discrepancies() (tagged market_kind="prop" instead
    of "game") so the UI can list them together.
    """
    try:
        k_props = kalshi.get_player_prop_markets(sport)
    except Exception as e:
        print(f"Kalshi prop discrepancy fetch error ({sport}): {e}")
        k_props = []

    try:
        p_props = polymarket.get_player_prop_markets(sport, slate_start())
    except Exception as e:
        print(f"Polymarket prop discrepancy fetch error ({sport}): {e}")
        p_props = []

    if not k_props or not p_props:
        return []

    out = []
    seen = set()
    for k in k_props:
        threshold = k.get("threshold")
        if threshold is None:
            continue
        match = _best_prop_match(p_props, k.get("player_name"), k.get("prop_type"), threshold)
        if not match:
            continue

        dedupe_key = (normalize_name(k.get("player_name")), k.get("prop_type"), round(float(threshold), 1))
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)

        k_prob, p_prob = k.get("implied_prob"), match.get("implied_prob")
        if k_prob is None or p_prob is None:
            continue

        out.append({
            "game":              k.get("title") or match.get("question") or "",
            "outcome_label":     f"{k.get('player_name')} {k.get('prop_type')} over {threshold}",
            "kalshi_prob":       round(float(k_prob), 4),
            "polymarket_prob":   round(float(p_prob), 4),
            "gap":               round(abs(float(k_prob) - float(p_prob)), 4),
            "kalshi_volume":     float(k.get("volume") or 0.0),
            "polymarket_volume": float(match.get("volume") or 0.0),
            "market_kind":       "prop",
        })

    return out


# ── Public: ambiguous / high-conviction markets (UI only) ─────────────────

# Within this many points of a coin flip counts as "too close to call".
TOSSUP_BAND = 0.07

# Roughly 2x VOLUME_HALF_WEIGHT — a market with real conviction behind it,
# not just a couple of opening trades. Same floor for both toss-ups (real
# money still piling into a genuine coin flip) and disagreements (both sides
# of a cross-platform gap need to be well-capitalized, not just noise on a
# thin book).
HIGH_VOLUME_FLOOR = 5000.0


@st.cache_data(ttl=300)
def get_ambiguous_markets(sport_key, scope="sport"):
    """
    Two cuts of "interesting" markets for the Ambiguous Markets UI tab:

      toss_ups: priced within TOSSUP_BAND of 50/50 (genuine outcome
                uncertainty) yet still cleared HIGH_VOLUME_FLOOR in trading
                volume — real conviction behind a bet nobody's sure of.
      disagreements: get_platform_discrepancies() + the player-prop
                version, filtered to pairs where BOTH platforms cleared
                HIGH_VOLUME_FLOOR — two well-capitalized markets actively
                pricing the same outcome differently, sorted by |gap|.

    scope: "sport" (default) — game-outcome and player-prop markets for
           `sport_key` only, same as before.
           "all" — toss_ups broadens to Kalshi/Polymarket markets across
           EVERY category (politics, economics, entertainment, crypto,
           etc. — see kalshi.GENERAL_CATEGORIES / polymarket.get_all_markets),
           not just the four sports this app scores. disagreements stays
           sport-scoped even in "all" mode: reliably matching the SAME
           market across two platforms needs a structured join key (team
           codes + game date, or player + stat + threshold) that general
           markets don't have — fuzzy title matching alone is unreliable
           enough it isn't worth shipping, so cross-platform disagreement
           detection stays limited to sports.

    Returns {"toss_ups": [...], "disagreements": [...]}. Informational only.
    """
    sport = (sport_key or "").lower()

    sources = []
    for label, fetch in (
        ("kalshi",     lambda: [dict(r, market_kind="game") for r in kalshi.get_game_outcome_markets(sport)]),
        ("kalshi",     lambda: [dict(r, market_kind="prop") for r in kalshi.get_player_prop_markets(sport)]),
        ("polymarket", lambda: [dict(r, market_kind="game") for r in polymarket.get_game_outcome_markets(sport, slate_start())]),
        ("polymarket", lambda: [dict(r, market_kind="prop") for r in polymarket.get_player_prop_markets(sport, slate_start())]),
    ):
        try:
            sources.extend(dict(r, source=label) for r in fetch())
        except Exception as e:
            print(f"ambiguous markets fetch error ({label}, {sport}): {e}")

    if scope == "all":
        for label, fetch in (
            ("kalshi",     lambda: [dict(r, market_kind="general") for r in kalshi.get_all_markets()]),
            ("polymarket", lambda: [dict(r, market_kind="general") for r in polymarket.get_all_markets()]),
        ):
            try:
                sources.extend(dict(r, source=label) for r in fetch())
            except Exception as e:
                print(f"ambiguous markets fetch error ({label}, general): {e}")

    toss_ups = []
    for r in sources:
        prob = r.get("implied_prob")
        volume = float(r.get("volume") or 0.0)
        if prob is None or volume < HIGH_VOLUME_FLOOR or abs(float(prob) - 0.5) > TOSSUP_BAND:
            continue

        kind = r["market_kind"]
        if kind == "prop":
            label = f"{r.get('player_name', '?')} {r.get('prop_type', '')} over {r.get('threshold', '')}".strip()
            game = r.get("title") or r.get("question") or ""
        elif kind == "general":
            label = r.get("label", "?")
            game = r.get("category", "")
        else:
            label = f"{r.get('team', '?')} to win"
            game = r.get("game", "")

        toss_ups.append({
            "source":       r["source"],
            "market_kind":  kind,
            "label":        label,
            "game":         game,
            "implied_prob": round(float(prob), 4),
            "volume":       volume,
        })

    toss_ups.sort(key=lambda r: r["volume"], reverse=True)

    disagreements = [
        row for row in (get_platform_discrepancies(sport) + _prop_platform_discrepancies(sport))
        if row["kalshi_volume"] >= HIGH_VOLUME_FLOOR and row["polymarket_volume"] >= HIGH_VOLUME_FLOOR
    ]
    disagreements.sort(key=lambda r: r["gap"], reverse=True)

    return {"toss_ups": toss_ups, "disagreements": disagreements}


# ── Public: ticker feed (UI only) ─────────────────────────────────────────

@st.cache_data(ttl=300)
def get_ticker_items(source, sport_keys, limit=40):
    """
    A flat, volume-sorted feed of live markets across every sport for one
    platform ("kalshi" or "polymarket") — game outcomes and player props
    mixed together, for the scrolling ticker bar. Always something moving
    even when a given sport is out of season, since it pulls from all four.

    Returns a list of dicts: {label, implied_prob, volume}, highest volume
    first, capped at `limit`. [] on total failure — the ticker just hides.
    """
    client = kalshi if source == "kalshi" else polymarket
    items = []

    for sport in sport_keys:
        try:
            for r in client.get_game_outcome_markets(sport):
                if r.get("implied_prob") is None:
                    continue
                items.append({
                    "label":        f"{sport.upper()} · {r.get('team', '?')} ML",
                    "implied_prob": float(r["implied_prob"]),
                    "volume":       float(r.get("volume") or 0.0),
                })
        except Exception as e:
            print(f"ticker fetch error ({source}, {sport}, game): {e}")

        try:
            props = client.get_player_prop_markets(sport) if source == "kalshi" \
                else client.get_player_prop_markets(sport, slate_start())
            for r in props:
                if r.get("implied_prob") is None:
                    continue
                items.append({
                    "label":        f"{sport.upper()} · {r.get('player_name', '?')} "
                                     f"{r.get('prop_type', '')} o{r.get('threshold', '')}",
                    "implied_prob": float(r["implied_prob"]),
                    "volume":       float(r.get("volume") or 0.0),
                })
        except Exception as e:
            print(f"ticker fetch error ({source}, {sport}, prop): {e}")

    items.sort(key=lambda r: r["volume"], reverse=True)
    return items[:limit]
