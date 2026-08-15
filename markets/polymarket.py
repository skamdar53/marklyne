# markets/polymarket.py — public read-only Polymarket client
#
# Two APIs, both plain public GET, no auth:
#   Gamma  (gamma-api.polymarket.com) — event/market metadata + prices
#   CLOB   (clob.polymarket.com)      — orderbook + historical price series
#
# Same best-effort contract as kalshi.py: never raise, return [] / {} on
# failure. Rate limits are generous (Gamma 4000/10s general, 300/10s on
# /markets; CLOB 9000/10s, 1000/10s on /prices-history) but we still back off.
#
# Shapes confirmed live against the API (Aug 2026):
#   /tags/slug/{slug} -> {"id": "100381", "label": "MLB", "slug": "mlb"}
#     sports=1  nba=745  mlb=100381  nfl=450  nhl=899
#   /events?tag_id=&active=true&closed=false&start_date_min=
#     event: {slug, title, startDate, teams:[{abbreviation, name, ordering}],
#             sport:{sport:"mlb",...}, volume, volume24hr, markets:[...]}
#     game slug   "mlb-tex-laa-2026-08-13"
#     props slug  "mlb-tex-laa-2026-08-13-player-props"
#   market: {sportsMarketType, question, groupItemTitle, outcomes,
#            outcomePrices, volume, volume24hr, clobTokenIds, ...}
#     moneyline -> outcomes are the two TEAM NAMES, outcomePrices parallel
#     player    -> sportsMarketType "baseball_player_home_runs",
#                  question "Corey Seager: Home Runs O/U 0.5",
#                  outcomes ["Over","Under"]
#   outcomes/outcomePrices/clobTokenIds arrive as JSON-encoded STRINGS.

import json
import re
import time

import requests
import streamlit as st

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"

HEADERS = {"User-Agent": "Mozilla/5.0"}
TIMEOUT = 15

# Slugs we resolve to numeric tag_ids at runtime (Gamma has no ?tag=<string>
# filter — you must resolve a real tag_id first).
SPORT_TAG_SLUGS = {
    "sports": "sports",
    "nba":    "nba",
    "mlb":    "mlb",
    "nfl":    "nfl",
    "nhl":    "nhl",
}

# Market types Polymarket uses for whole-game outcomes.
GAME_OUTCOME_TYPES = {"moneyline", "spreads", "totals"}

# sportsMarketType -> our normalized prop_type, for the types seen live.
# Anything unmapped falls back to _derive_prop_type() below.
PROP_TYPE_MAP = {
    "baseball_player_home_runs":  "home_runs",
    "baseball_player_strikeouts": "strikeouts",
    "baseball_player_hits":       "hits",
    "baseball_player_total_bases": "total_bases",
    "basketball_player_points":   "points",
    "basketball_player_rebounds": "rebounds",
    "basketball_player_assists":  "assists",
    "football_player_passing_yards":   "passing_yards",
    "football_player_rushing_yards":   "rushing_yards",
    "football_player_receiving_yards": "receiving_yards",
    "football_player_receptions":      "receptions",
    "hockey_player_points":  "points",
    "hockey_player_goals":   "goals",
    "hockey_player_assists": "assists",
    "hockey_player_saves":   "goalie_saves",
}


# ── HTTP ──────────────────────────────────────────────────────────────────

def _get(base, path, params=None, retries=2):
    """GET with backoff. Returns None on failure so callers can distinguish."""
    url = f"{base}{path}"
    for attempt in range(retries + 1):
        try:
            r = requests.get(url, params=params, headers=HEADERS, timeout=TIMEOUT)
            if r.status_code == 429 or r.status_code >= 500:
                if attempt < retries:
                    time.sleep(0.6 * (2 ** attempt))
                    continue
                print(f"Polymarket {path} HTTP {r.status_code}")
                return None
            r.raise_for_status()
            return r.json()
        except Exception as e:
            if attempt < retries:
                time.sleep(0.6 * (2 ** attempt))
                continue
            print(f"Polymarket fetch error ({path}): {e}")
            return None
    return None


def _jlist(value):
    """
    outcomes / outcomePrices / clobTokenIds come back as JSON-encoded strings
    ('["Yes", "No"]') rather than real arrays. Tolerate both.
    """
    if isinstance(value, list):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else []
        except Exception:
            return []
    return []


def _f(value, default=0.0):
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


# ── Discovery ─────────────────────────────────────────────────────────────

@st.cache_data(ttl=3600)
def get_sports_tags():
    """
    Resolve our sport keys to Polymarket numeric tag_ids by hitting
    /tags/slug/{slug} for each. Returns {sport_key: tag_id_int}, skipping
    anything that fails to resolve.

    Live values: sports=1, nba=745, mlb=100381, nfl=450, nhl=899 — but we
    resolve rather than hardcode, since Polymarket does reshuffle tags.
    """
    tags = {}
    for key, slug in SPORT_TAG_SLUGS.items():
        data = _get(GAMMA_API, f"/tags/slug/{slug}")
        if not isinstance(data, dict):
            continue
        try:
            tags[key] = int(data.get("id"))
        except (TypeError, ValueError):
            continue
    return tags


# Game events carry the game's calendar date in their slug:
#   "mlb-tex-laa-2026-08-13", "mlb-tex-laa-2026-08-13-player-props"
_SLUG_DATE_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")


def slug_game_date(slug):
    """
    The game's calendar date (YYYY-MM-DD) parsed out of an event slug, or "".

    This matters more than it looks: Gamma's `startDate` field is when the
    event was CREATED (~6 days before first pitch), not when the game is
    played, so filtering on start_date_min silently drops most of the live
    slate. The slug is the reliable source for the actual game date.
    """
    m = _SLUG_DATE_RE.search(slug or "")
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else ""


@st.cache_data(ttl=300)
def get_events(sport_key, on_or_after=None, limit=500):
    """
    Active, unclosed Gamma game events for a sport, with their nested markets.

    on_or_after: "YYYY-MM-DD"; keeps only events whose slug game date is on or
                 after this. Filters out both season futures (no date in the
                 slug at all) and stale events that were never resolved and
                 still report active=true months later.

    Deliberately does NOT use Gamma's start_date_min parameter — see
    slug_game_date() for why that filter is a trap.
    """
    tag_id = get_sports_tags().get((sport_key or "").lower())
    if not tag_id:
        return []

    page = 100
    out = []
    offset = 0
    while offset < limit:
        data = _get(GAMMA_API, "/events", {
            "tag_id": tag_id,
            "active": "true",
            "closed": "false",
            "limit": page,
            "offset": offset,
        })
        if not isinstance(data, list) or not data:
            break

        for e in data:
            game_date = slug_game_date(e.get("slug"))
            if not game_date:
                continue  # futures / awards / next-team markets
            if on_or_after and game_date < on_or_after:
                continue
            e = dict(e, _game_date=game_date)
            out.append(e)

        if len(data) < page:
            break
        offset += page

    return out


def _teams(event):
    """[(abbreviation_upper, full_name, 'home'|'away'), ...] for a game event."""
    return [
        (
            (t.get("abbreviation") or "").upper(),
            t.get("name") or "",
            t.get("ordering") or "",
        )
        for t in (event.get("teams") or [])
    ]


# ── Game outcome markets ──────────────────────────────────────────────────

@st.cache_data(ttl=300)
def get_game_outcome_markets(sport_key, on_or_after=None):
    """
    Active game-outcome (moneyline) markets for a sport.

    Polymarket runs one market per game whose `outcomes` are the two team
    names and whose `outcomePrices` are the parallel implied probabilities,
    so we split it into one row per team to match kalshi.py's shape.

    Returns a list of dicts:
        {slug, game, game_date, team, team_code, opponent_code, implied_prob,
         volume, volume_24h, market_type, clob_token_id, start_date}

    Empty for out-of-season sports (confirmed today: NBA and NHL have only
    futures markets live, no game events).
    """
    rows = []

    for event in get_events(sport_key, on_or_after=on_or_after):
        teams = _teams(event)
        codes = [c for c, _, _ in teams]

        for m in (event.get("markets") or []):
            if m.get("sportsMarketType") != "moneyline":
                continue

            outcomes = _jlist(m.get("outcomes"))
            prices = _jlist(m.get("outcomePrices"))
            tokens = _jlist(m.get("clobTokenIds"))
            if len(outcomes) < 2 or len(prices) < len(outcomes):
                continue

            for i, outcome in enumerate(outcomes):
                code = _match_code(outcome, teams)
                others = [c for c in codes if c and c != code]
                rows.append({
                    "slug":          event.get("slug", ""),
                    "game":          event.get("title", ""),
                    "game_date":     event.get("_game_date", ""),
                    "team":          outcome,
                    "team_code":     code,
                    "opponent_code": others[0] if others else "",
                    "implied_prob":  _f(prices[i]),
                    "volume":        _f(m.get("volume")),
                    "volume_24h":    _f(m.get("volume24hr")),
                    "market_type":   "moneyline",
                    "clob_token_id": tokens[i] if i < len(tokens) else "",
                    "start_date":    event.get("startDate", ""),
                })

    return rows


def _match_code(outcome_name, teams):
    """Map an outcome label ('New York Yankees') back to its team code."""
    label = (outcome_name or "").strip().lower()
    for code, name, _ in teams:
        if name and name.strip().lower() == label:
            return code
    # Fall back to nickname overlap ("Yankees" in "New York Yankees").
    for code, name, _ in teams:
        if name and label and (label in name.lower() or name.lower() in label):
            return code
    return ""


# ── Player prop markets ───────────────────────────────────────────────────

# "Corey Seager: Home Runs O/U 0.5" -> player, stat label, line
_PROP_RE = re.compile(r"^(?P<player>[^:]+):\s*(?P<stat>.+?)\s*O/U\s*(?P<line>[\d.]+)\s*$", re.I)


def _derive_prop_type(sports_market_type, stat_label):
    """Normalize an unmapped sportsMarketType, else fall back to the label."""
    if sports_market_type in PROP_TYPE_MAP:
        return PROP_TYPE_MAP[sports_market_type]
    if sports_market_type:
        # "baseball_player_home_runs" -> "home_runs"
        tail = re.sub(r"^[a-z]+_player_", "", sports_market_type)
        if tail and tail != sports_market_type:
            return tail
    return (stat_label or "").strip().lower().replace(" ", "_")


@st.cache_data(ttl=300)
def get_player_prop_markets(sport_key, on_or_after=None):
    """
    Active player-prop markets for a sport, best effort.

    Confirmed live for MLB today (`baseball_player_home_runs`,
    `baseball_player_strikeouts`); returns [] cleanly for sports with none.
    Nothing is hardcoded about which sports have props — we just report what
    the API returns.

    Returns a list of dicts:
        {slug, game, game_date, player_name, prop_type, threshold,
         side ("over"), implied_prob, volume, volume_24h, market_type,
         clob_token_id, team_codes, question}

    `implied_prob` is the probability of the OVER.
    """
    rows = []

    for event in get_events(sport_key, on_or_after=on_or_after):
        teams = _teams(event)
        codes = [c for c, _, _ in teams if c]

        for m in (event.get("markets") or []):
            market_type = m.get("sportsMarketType") or ""
            if not market_type or market_type in GAME_OUTCOME_TYPES:
                continue
            if "player" not in market_type:
                continue

            question = m.get("question") or m.get("groupItemTitle") or ""
            match = _PROP_RE.match(question.strip())
            if not match:
                continue

            outcomes = _jlist(m.get("outcomes"))
            prices = _jlist(m.get("outcomePrices"))
            tokens = _jlist(m.get("clobTokenIds"))
            if len(outcomes) < 2 or len(prices) < 2:
                continue

            # Find the "Over" leg; Polymarket lists it first, but don't assume.
            over_idx = 0
            for i, outcome in enumerate(outcomes):
                if (outcome or "").strip().lower() == "over":
                    over_idx = i
                    break

            try:
                threshold = float(match.group("line"))
            except ValueError:
                continue

            rows.append({
                "slug":          event.get("slug", ""),
                "game":          event.get("title", ""),
                "game_date":     event.get("_game_date", ""),
                "player_name":   match.group("player").strip(),
                "prop_type":     _derive_prop_type(market_type, match.group("stat")),
                "threshold":     threshold,
                "side":          "over",
                "implied_prob":  _f(prices[over_idx]),
                "volume":        _f(m.get("volume")),
                "volume_24h":    _f(m.get("volume24hr")),
                "market_type":   market_type,
                "clob_token_id": tokens[over_idx] if over_idx < len(tokens) else "",
                "team_codes":    codes,
                "question":      question,
            })

    return rows


# ── CLOB price detail ─────────────────────────────────────────────────────

@st.cache_data(ttl=300)
def get_price(clob_token_id, side="BUY"):
    """Current CLOB price for one outcome token. Returns None on failure."""
    if not clob_token_id:
        return None
    data = _get(CLOB_API, "/price", {"token_id": clob_token_id, "side": side})
    if not isinstance(data, dict):
        return None
    price = data.get("price")
    return _f(price, None) if price is not None else None


@st.cache_data(ttl=300)
def get_price_history(clob_token_id, interval="1d", fidelity=60):
    """
    Historical price series for one outcome token as [{t, p}, ...].
    Useful for spotting a line that's been moving. [] on failure.
    """
    if not clob_token_id:
        return []
    data = _get(CLOB_API, "/prices-history", {
        "market": clob_token_id,
        "interval": interval,
        "fidelity": fidelity,
    })
    if not isinstance(data, dict):
        return []
    return data.get("history", []) or []


# ── Everything, not just sports ─────────────────────────────────────────────

@st.cache_data(ttl=300)
def get_open_events(limit=1500):
    """Active Gamma events across EVERY category (no tag_id filter) — politics,
    economics, culture, science, sports, all of it."""
    page = 100
    out = []
    offset = 0
    while offset < limit:
        data = _get(GAMMA_API, "/events", {
            "active": "true",
            "closed": "false",
            "limit": page,
            "offset": offset,
        })
        if not isinstance(data, list) or not data:
            break
        out.extend(data)
        if len(data) < page:
            break
        offset += page
    return out[:limit]


@st.cache_data(ttl=300)
def get_all_markets(limit=1500):
    """
    Broad, normalized sample of currently open Polymarket binary (Yes/No)
    markets across EVERY category — not just the four sports we score. For
    the "all markets" scope of the Ambiguous Markets tab. Multi-outcome
    events (more than 2 outcomes) are skipped: "implied probability of Yes"
    isn't well-defined for them.

    Returns [{label, implied_prob, volume, slug}, ...].
    """
    rows = []
    for event in get_open_events(limit):
        for m in (event.get("markets") or []):
            outcomes = _jlist(m.get("outcomes"))
            prices = _jlist(m.get("outcomePrices"))
            if len(outcomes) != 2 or len(prices) != 2:
                continue

            yes_idx = 0
            for i, o in enumerate(outcomes):
                if (o or "").strip().lower() == "yes":
                    yes_idx = i
                    break

            rows.append({
                "label":        m.get("question") or event.get("title", ""),
                "implied_prob": _f(prices[yes_idx]),
                "volume":       _f(m.get("volume")),
                "slug":         event.get("slug", ""),
            })
    return rows
