# markets/kalshi.py — public read-only Kalshi client
#
# Kalshi's v2 trade API serves market data with no auth at all (GET endpoints
# are `security: []`; auth is only needed to actually place orders). Both
# api.elections.kalshi.com and external-api.kalshi.com front the same v2 API
# and serve every category, sports included.
#
# Everything here is best-effort: if Kalshi is down, rate-limiting us, or has
# renamed a series, we return an empty list rather than blowing up a scoring
# run. Market data is a 5% weight, not a hard dependency.
#
# Naming conventions confirmed live against the API (Aug 2026):
#   Game outcome:  KXMLBGAME / KXNBAGAME / KXNFLGAME / KXNHLGAME
#     event_ticker  KXMLBGAME-26AUG161605COLSF   (date + away/home codes)
#     ticker        KXMLBGAME-26AUG161605COLSF-SF  (suffix = team code)
#     yes_sub_title "San Francisco"  (city, sometimes truncated: "Los Angeles D")
#   Player props:  KXNBAPTS / KXMLBTB / KXNFLPASSYDS / KXNHLSAVES / ...
#     ticker        KXMLBTB-26AUG131930PHIMIN-MINLKEASCHALL15-5
#     yes_sub_title "Luke Keaschall: 5+"
#     floor_strike  4.5   with strike_type "greater" -> "5+" -> an over-4.5 line
#   Prices are dollar-denominated STRINGS ("0.5700"), volumes too ("29017.06").

import re
import time

import requests
import streamlit as st

KALSHI_API = "https://api.elections.kalshi.com/trade-api/v2"

HEADERS = {"User-Agent": "Mozilla/5.0"}
TIMEOUT = 15

# Our sport keys -> Kalshi's per-game outcome series ticker.
GAME_SERIES = {
    "nba": "KXNBAGAME",
    "mlb": "KXMLBGAME",
    "nfl": "KXNFLGAME",
    "nhl": "KXNHLGAME",
}

# Our normalized prop_type names -> Kalshi player-prop series tickers.
# Kalshi runs a separate series per stat, so a "player props for this sport"
# fetch is really N series fetches. Keys are deliberately generous (several
# aliases per stat) because each sport module names its prop types itself and
# those modules are being written in parallel by other agents.
PROP_SERIES = {
    "nba": {
        "points":                "KXNBAPTS",
        "rebounds":              "KXNBAREB",
        "assists":               "KXNBAAST",
        "three_pointers_made":   "KXNBA3PT",
        "threes":                "KXNBA3PT",
        "blocks":                "KXNBABLK",
        "steals":                "KXNBASTL",
        "free_throws_made":      "KXNBAFTM",
        "pts+reb+ast":           "KXNBAPRA",
        "pts+reb":               "KXNBAPR",
        "pts+ast":               "KXNBAPA",
        "reb+ast":               "KXNBARA",
        "stl+blk":               "KXNBASTOCK",
    },
    "mlb": {
        "hits":                  "KXMLBHIT",
        "home_runs":             "KXMLBHR",
        "strikeouts":            "KXMLBKS",
        "pitcher_strikeouts":    "KXMLBKS",
        "total_bases":           "KXMLBTB",
        "rbis":                  "KXMLBRBI",
        "runs_batted_in":        "KXMLBRBI",
        "hits+runs+rbis":        "KXMLBHRR",
        "stolen_bases":          "KXMLBSB",
        "outs_recorded":         "KXMLBOUTS",
        "pitching_outs":         "KXMLBOUTS",
    },
    "nfl": {
        "passing_yards":         "KXNFLPASSYDS",
        "passing_tds":           "KXNFLPASSTDS",
        "passing_touchdowns":    "KXNFLPASSTDS",
        "passing_attempts":      "KXNFLPASSATT",
        "passing_completions":   "KXNFLPASSCOMP",
        "interceptions":         "KXNFLPASSINT",
        "rushing_yards":         "KXNFLRSHYDS",
        "rushing_attempts":      "KXNFLRSHATT",
        "receiving_yards":       "KXNFLRECYDS",
        "receptions":            "KXNFLREC",
        "anytime_td":            "KXNFLANYTD",
        "fantasy_points":        "KXNFLFFPTS",
    },
    "nhl": {
        "points":                "KXNHLPTS",
        "assists":               "KXNHLAST",
        "goals":                 "KXNHLGOAL",
        "goalie_saves":          "KXNHLSAVES",
        "saves":                 "KXNHLSAVES",
        "shots_on_goal":         "KXNHLPTS",
    },
}


# ── HTTP ──────────────────────────────────────────────────────────────────

def _get(path, params=None, retries=2):
    """
    GET a Kalshi v2 endpoint. Unauthenticated traffic is IP-throttled at
    roughly "Basic" tier, so back off and retry on 429 / 5xx. Returns {} on
    any failure — callers treat that as "no market data".
    """
    url = f"{KALSHI_API}{path}"
    for attempt in range(retries + 1):
        try:
            r = requests.get(url, params=params, headers=HEADERS, timeout=TIMEOUT)
            if r.status_code == 429 or r.status_code >= 500:
                if attempt < retries:
                    time.sleep(0.6 * (2 ** attempt))
                    continue
                print(f"Kalshi {path} HTTP {r.status_code}")
                return {}
            r.raise_for_status()
            return r.json()
        except Exception as e:
            if attempt < retries:
                time.sleep(0.6 * (2 ** attempt))
                continue
            print(f"Kalshi fetch error ({path}): {e}")
            return {}
    return {}


def _f(value, default=0.0):
    """Kalshi returns prices/volumes as strings ('0.5700', '29017.06')."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def implied_prob(market):
    """
    Best available implied probability (0-1) that this market resolves YES.

    Prefer the midpoint of the live bid/ask — that's the market's current
    opinion. Fall back to last traded price when the book is one-sided or
    empty. Returns None when there's no usable price at all.
    """
    bid = _f(market.get("yes_bid_dollars"))
    ask = _f(market.get("yes_ask_dollars"))
    last = _f(market.get("last_price_dollars"))

    if bid > 0 and ask > 0 and ask >= bid:
        return (bid + ask) / 2.0
    if last > 0:
        return last
    if ask > 0:          # only an offer resting: it's an upper bound, use it
        return ask
    if bid > 0:
        return bid
    return None


# Event tickers embed the game's date and start time in ET:
#   KXMLBGAME-26AUG161605COLSF  ->  Aug 16 2026, 4:05pm ET, COL at SF
_EVENT_DATE_RE = re.compile(r"-(\d{2})([A-Z]{3})(\d{2})")

_MONTHS = {m: i + 1 for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN",
     "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"])}


def event_game_date(event_ticker):
    """
    Game date as "YYYY-MM-DD" parsed from an event ticker, or "".

    Gives us a shared key with Polymarket's slug date, so cross-platform
    matching can pin a specific game rather than just a matchup — which
    matters for series (the same two teams play 3-4 days running) and
    doubleheaders.
    """
    m = _EVENT_DATE_RE.search(event_ticker or "")
    if not m:
        return ""
    month = _MONTHS.get(m.group(2))
    if not month:
        return ""
    return f"20{m.group(1)}-{month:02d}-{int(m.group(3)):02d}"


def spread(market):
    """Bid/ask spread in probability units. Wide spread = untrustworthy price."""
    bid = _f(market.get("yes_bid_dollars"))
    ask = _f(market.get("yes_ask_dollars"))
    if bid > 0 and ask > 0 and ask >= bid:
        return ask - bid
    return 1.0  # no two-sided book


# ── Discovery ─────────────────────────────────────────────────────────────

@st.cache_data(ttl=3600)
def get_sports_series(category="Sports"):
    """
    All Kalshi series in a category (default "Sports" — must match Kalshi's
    exact category spelling, e.g. "Science and Technology"), as a list of
    dicts: {ticker, title, category, frequency}

    Kalshi's category values are title-cased multi-word strings ("Science
    and Technology") — plain .capitalize() mangles those (lowercases every
    word but the first), which sends the server a category that matches
    nothing and can come back with "series": null rather than []. Send the
    category through unmodified and let the client-side filter (case
    insensitive) be the real safety net either way.
    """
    data = _get("/series", {"category": category})
    series = (data.get("series") or []) if isinstance(data, dict) else []

    want = category.lower()
    return [
        {
            "ticker":    s.get("ticker", ""),
            "title":     s.get("title", ""),
            "category":  s.get("category", ""),
            "frequency": s.get("frequency", ""),
        }
        for s in series
        if (s.get("category") or "").lower() == want
    ]


@st.cache_data(ttl=300)
def get_series_markets(series_ticker, status="open", max_markets=1000):
    """
    All markets in one series, following the cursor until exhausted.
    Returns the raw Kalshi market dicts (or [] on failure / unknown series).
    """
    if not series_ticker:
        return []

    out = []
    cursor = None
    while len(out) < max_markets:
        params = {"series_ticker": series_ticker, "status": status, "limit": 200}
        if cursor:
            params["cursor"] = cursor

        data = _get("/markets", params)
        markets = data.get("markets", []) if isinstance(data, dict) else []
        if not markets:
            break

        out.extend(markets)
        cursor = data.get("cursor")
        if not cursor:
            break

    return out[:max_markets]


# ── Game outcome markets ──────────────────────────────────────────────────

@st.cache_data(ttl=300)
def get_game_outcome_markets(sport_key):
    """
    Active per-game moneyline-equivalent markets for a sport.

    Kalshi lists one binary market per team ("Will Seattle win the Dallas vs
    Seattle game?"), two markets per event, so we return one row per team and
    tag each with its opponent (derived by pairing on event_ticker).

    Returns a list of dicts:
        {ticker, event_ticker, game_date, game, team, team_code,
         opponent_code, implied_prob, volume, volume_24h, open_interest,
         spread, close_time}

    Empty list if the sport is out of season (confirmed: NBA/NHL return
    nothing in August) or on any API failure.
    """
    series = GAME_SERIES.get((sport_key or "").lower())
    if not series:
        return []

    markets = get_series_markets(series)

    # Pair up the two sides of each game so every row knows its opponent.
    by_event = {}
    for m in markets:
        by_event.setdefault(m.get("event_ticker", ""), []).append(m)

    rows = []
    for event_ticker, sides in by_event.items():
        codes = [s.get("ticker", "").split("-")[-1].upper() for s in sides]

        for m in sides:
            code = m.get("ticker", "").split("-")[-1].upper()
            others = [c for c in codes if c != code]
            prob = implied_prob(m)
            if prob is None:
                continue

            rows.append({
                "ticker":        m.get("ticker", ""),
                "event_ticker":  event_ticker,
                "game_date":     event_game_date(event_ticker),
                "game":          m.get("title", ""),
                "team":          m.get("yes_sub_title", ""),
                "team_code":     code,
                "opponent_code": others[0] if others else "",
                "implied_prob":  prob,
                "volume":        _f(m.get("volume_fp")),
                "volume_24h":    _f(m.get("volume_24h_fp")),
                "open_interest": _f(m.get("open_interest_fp")),
                "spread":        spread(m),
                "close_time":    m.get("close_time", ""),
            })

    return rows


# ── Player prop markets ───────────────────────────────────────────────────

def _parse_player(market):
    """
    Pull the player's name out of a prop market. Kalshi formats the subtitle
    as "Luke Keaschall: 5+", so everything before the colon is the name.
    """
    label = market.get("yes_sub_title") or market.get("title") or ""
    name = label.split(":")[0].strip()
    return name


def _parse_threshold(market):
    """
    The prop's threshold as an over/under line.

    strike_type "greater" with floor_strike 4.5 means the market pays YES on
    5+, i.e. exactly an "over 4.5" line — floor_strike maps 1:1 onto how
    PrizePicks states a line, which is what makes this comparison possible.
    """
    for key in ("floor_strike", "cap_strike", "strike"):
        value = market.get(key)
        if value is not None:
            try:
                return float(value)
            except (TypeError, ValueError):
                continue
    return None


@st.cache_data(ttl=300)
def get_player_prop_markets(sport_key, prop_type=None):
    """
    Active player-prop markets for a sport.

    prop_type: our normalized prop name (e.g. "points"). If given, only that
               stat's series is fetched — much cheaper, and what
               discrepancy.py actually wants. If omitted, every mapped stat
               series for the sport is fetched.

    Returns a list of dicts:
        {ticker, event_ticker, game_date, player_name, prop_type,
         series_ticker, threshold, side ("over"), implied_prob, volume,
         volume_24h, open_interest, spread, title}

    `implied_prob` is the market's probability of going OVER `threshold`.

    Returns [] gracefully when the sport has no props live — confirmed today
    for NBA and NHL (offseason). Nothing here assumes a given sport does or
    doesn't have props; it just reports what the API returns.
    """
    stat_map = PROP_SERIES.get((sport_key or "").lower(), {})
    if not stat_map:
        return []

    if prop_type:
        series_ticker = stat_map.get(str(prop_type).lower())
        if not series_ticker:
            return []
        wanted = {series_ticker: str(prop_type).lower()}
    else:
        # De-dupe: several aliases point at the same series ticker.
        wanted = {}
        for name, ticker in stat_map.items():
            wanted.setdefault(ticker, name)

    rows = []
    for series_ticker, stat_name in wanted.items():
        for m in get_series_markets(series_ticker):
            prob = implied_prob(m)
            threshold = _parse_threshold(m)
            if prob is None or threshold is None:
                continue

            player = _parse_player(m)
            if not player:
                continue

            rows.append({
                "ticker":        m.get("ticker", ""),
                "event_ticker":  m.get("event_ticker", ""),
                "game_date":     event_game_date(m.get("event_ticker", "")),
                "player_name":   player,
                "prop_type":     stat_name,
                "series_ticker": series_ticker,
                "threshold":     threshold,
                "side":          "over",
                "implied_prob":  prob,
                "volume":        _f(m.get("volume_fp")),
                "volume_24h":    _f(m.get("volume_24h_fp")),
                "open_interest": _f(m.get("open_interest_fp")),
                "spread":        spread(m),
                "title":         m.get("title", ""),
            })

    return rows


@st.cache_data(ttl=300)
def get_orderbook(ticker):
    """Raw orderbook for one market. Returns {} on failure."""
    if not ticker:
        return {}
    data = _get(f"/markets/{ticker}/orderbook")
    return data.get("orderbook", {}) if isinstance(data, dict) else {}


# ── Everything, not just sports ─────────────────────────────────────────────
#
# A blind, unfiltered pull of /markets?status=open turns out to be almost
# entirely "MVE" (multi-value-event) contracts — auto-generated parlay-style
# combo products that get created constantly and crowd out everything else
# once you page more than a couple hundred deep (confirmed live: 3,000
# markets fetched, 0 were a normal single-outcome market). Their tickers
# also don't parse as clean Yes/No bets ("yes Boston,yes Chicago WS,...").
# So instead of paging /markets directly, this samples series from named
# non-sports categories and pulls markets from each — same structured
# approach as get_sports_series(), just for the categories people actually
# want in a "surprise me" feed.

# A representative slice of Kalshi's non-sports categories (confirmed live
# via /series: Politics has 2,173 series, Entertainment 2,500, Economics
# 622, etc. — orders of magnitude too many to enumerate fully, so this only
# samples the first SERIES_PER_CATEGORY of each).
GENERAL_CATEGORIES = ["Politics", "Economics", "Financials", "Entertainment",
                       "Crypto", "Science and Technology", "World", "Elections"]
SERIES_PER_CATEGORY = 15
MARKETS_PER_SERIES = 10


@st.cache_data(ttl=300)
def get_all_markets(categories=None):
    """
    Broad, normalized sample of currently open Kalshi markets across several
    non-sports categories (politics, economics, culture, and more) — for the
    "all markets" scope of the Ambiguous Markets tab. Not exhaustive (there
    are tens of thousands of series across these categories); a real sample
    of legitimate single-outcome markets, not the exotic combo products a
    blind /markets pull surfaces.

    Returns [{label, implied_prob, volume, ticker, category}, ...].
    """
    cats = categories or GENERAL_CATEGORIES
    rows = []

    for category in cats:
        try:
            series_list = get_sports_series(category)[:SERIES_PER_CATEGORY]
        except Exception as e:
            print(f"Kalshi category fetch error ({category}): {e}")
            continue

        for s in series_list:
            ticker = s.get("ticker")
            if not ticker:
                continue
            try:
                markets = get_series_markets(ticker, max_markets=MARKETS_PER_SERIES)
            except Exception as e:
                print(f"Kalshi series markets error ({ticker}): {e}")
                continue

            for m in markets:
                prob = implied_prob(m)
                if prob is None:
                    continue
                rows.append({
                    "label":        m.get("title") or m.get("yes_sub_title") or m.get("ticker", ""),
                    "implied_prob": prob,
                    "volume":       _f(m.get("volume_fp")),
                    "ticker":       m.get("ticker", ""),
                    "category":     category,
                })

    return rows
