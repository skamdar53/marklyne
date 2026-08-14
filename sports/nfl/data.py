# sports/nfl/data.py — nflreadpy (nflverse) wrapper: weekly player stats,
# schedules, snap counts, plus ESPN's public scoreboard for the live slate.
#
# nflverse ships static Parquet files off GitHub — no key, no rate limit — so
# everything here is a plain read plus a local cache. ESPN's hidden site API is
# only used for the current week's fixtures, since nflverse's schedule file
# covers the regular season only and lags during the preseason.

import unicodedata
from datetime import datetime, timedelta

import nflreadpy as nfl
import pandas as pd
import requests
import streamlit as st
from nflreadpy.config import CacheMode, update_config

from sports.nfl.config import (
    N_SEASONS, N_GAMES, MIN_GAMES_VS_TEAM, MIN_GAMES_WITHOUT_TEAMMATE,
    SHORT_WEEK_REST, PROP_VOLUME_COLS,
)

# The nflverse Parquet files are immutable per release, so keep them on disk
# instead of re-downloading a few hundred MB every session.
update_config(cache_mode=CacheMode.FILESYSTEM)

ESPN_API = "https://site.api.espn.com/apis/site/v2/sports/football/nfl"


# ── Players / teams lookup ───────────────────────────────────────────────────

def _normalize(name):
    """Lowercase and strip accent marks/punctuation so 'DK Metcalf' matches 'D.K. Metcalf'."""
    stripped = unicodedata.normalize("NFD", name).encode("ascii", "ignore").decode()
    return "".join(c for c in stripped if c.isalnum() or c == " ").lower().strip()


_players_cache = {}


def _players():
    if "df" not in _players_cache:
        _players_cache["df"] = nfl.load_players().to_pandas()
    return _players_cache["df"]


def get_player_id(name):
    """Returns the nflverse gsis_id for a player name. Case/accent/punctuation insensitive."""
    df = _players()
    norm = _normalize(name)

    exact = df[df["display_name"].fillna("").map(_normalize) == norm]
    if not exact.empty:
        # Prefer an active player when a name collides across eras
        active = exact[exact["status"] == "ACT"]
        row = active.iloc[0] if not active.empty else exact.iloc[0]
        return row["gsis_id"]

    partial = df[df["display_name"].fillna("").map(_normalize).str.contains(norm, regex=False)]
    if not partial.empty:
        active = partial[partial["status"] == "ACT"]
        row = active.iloc[0] if not active.empty else partial.iloc[0]
        return row["gsis_id"]
    return None


def get_player_position(player_id):
    df = _players()
    row = df[df["gsis_id"] == player_id]
    return row.iloc[0]["position"] if not row.empty else None


def get_player_pfr_id(player_id):
    df = _players()
    row = df[df["gsis_id"] == player_id]
    return row.iloc[0]["pfr_id"] if not row.empty else None


_teams_cache = {}

# nflverse uses LA/WAS/JAX/LV; PrizePicks and ESPN use some of the older or
# longer abbreviations, and relocated franchises still show up in old data.
_TEAM_ALIASES = {
    "lar": "LA", "stl": "LA", "rams": "LA",
    "lac": "LAC", "sd": "LAC", "sdg": "LAC",
    "lv": "LV", "lvr": "LV", "oak": "LV", "rai": "LV",
    "wsh": "WAS", "was": "WAS", "wft": "WAS",
    "jac": "JAX", "jax": "JAX",
    "gnb": "GB", "kan": "KC", "nwe": "NE", "nor": "NO",
    "sfo": "SF", "tam": "TB", "nyg": "NYG", "nyj": "NYJ",
    "ari": "ARI", "crd": "ARI", "rav": "BAL", "clt": "IND",
    "htx": "HOU", "oti": "TEN", "bal": "BAL",
}


def build_team_lookup():
    """Maps abbreviation, nickname and full-name variations to nflverse's abbreviation."""
    if "lookup" not in _teams_cache:
        lookup = dict(_TEAM_ALIASES)
        for _, t in nfl.load_teams().to_pandas().iterrows():
            abbr = t["team_abbr"]
            lookup[abbr.lower()] = abbr
            lookup[str(t["team_nick"]).lower()] = abbr
            lookup[str(t["team_name"]).lower()] = abbr
        # load_teams carries historical franchises too; the aliases above win
        lookup.update(_TEAM_ALIASES)
        _teams_cache["lookup"] = lookup
    return _teams_cache["lookup"]


def get_team_abbr(team_name):
    """Normalizes any team name/abbreviation to nflverse's abbreviation, or None."""
    if not team_name:
        return None
    return build_team_lookup().get(str(team_name).strip().lower())


# ── Seasons ───────────────────────────────────────────────────────────────────

def current_season():
    return nfl.get_current_season()


def recent_seasons(n=N_SEASONS, through=None):
    """The n most recent seasons ending at `through` (defaults to the current season)."""
    end = through if through is not None else current_season()
    return list(range(end - n + 1, end + 1))


# ── Raw frames ────────────────────────────────────────────────────────────────

_stats_cache = {}
_schedule_cache = {}
_snaps_cache = {}


def load_stats(seasons):
    """Weekly player stats (REG + POST) for the given seasons, sorted chronologically."""
    key = tuple(sorted(seasons))
    if key not in _stats_cache:
        df = nfl.load_player_stats(seasons=list(key)).to_pandas()
        df = df.sort_values(["season", "week"]).reset_index(drop=True)
        _stats_cache[key] = df
    return _stats_cache[key]


def load_schedule(seasons):
    """Game-level schedule rows (includes home_rest/away_rest in days)."""
    key = tuple(sorted(seasons))
    if key not in _schedule_cache:
        _schedule_cache[key] = nfl.load_schedules(seasons=list(key)).to_pandas()
    return _schedule_cache[key]


def load_snaps(seasons):
    key = tuple(sorted(seasons))
    if key not in _snaps_cache:
        try:
            _snaps_cache[key] = nfl.load_snap_counts(seasons=list(key)).to_pandas()
        except Exception:
            _snaps_cache[key] = pd.DataFrame()
    return _snaps_cache[key]


# ── Game logs ─────────────────────────────────────────────────────────────────

_game_log_cache = {}


def seed_game_log_cache(player_id, logs):
    """Override the cache with provided logs (used in backtesting to inject historical data)."""
    _game_log_cache[player_id] = logs


def clear_game_log_cache():
    _game_log_cache.clear()


def _attach_game_context(logs, seasons):
    """Adds game_date, is_home and rest_days to a player's weekly rows from the schedule."""
    sched = load_schedule(seasons)[
        ["game_id", "gameday", "weekday", "home_team", "away_team", "home_rest", "away_rest"]
    ]
    merged = logs.merge(sched, on="game_id", how="left")
    merged["is_home"] = merged["team"] == merged["home_team"]
    merged["rest_days"] = merged["away_rest"].where(~merged["is_home"], merged["home_rest"])
    merged["game_date"] = merged["gameday"]
    return merged


def get_game_log(player_id, seasons=None):
    """
    Returns the player's weekly stat rows across `seasons`, oldest first, with
    game_date/is_home/rest_days attached. Cached per player.
    """
    if player_id in _game_log_cache:
        return _game_log_cache[player_id]

    seasons = seasons or recent_seasons()
    stats = load_stats(seasons)
    logs = stats[stats["player_id"] == player_id]
    if logs.empty:
        _game_log_cache[player_id] = logs
        return logs

    logs = _attach_game_context(logs, seasons).sort_values(["season", "week"]).reset_index(drop=True)
    _game_log_cache[player_id] = logs
    return logs


_PROP_COL_MAP = {
    "pass_yards":       ["passing_yards"],
    "pass_completions": ["completions"],
    "pass_tds":         ["passing_tds"],
    "rush_yards":       ["rushing_yards"],
    "receptions":       ["receptions"],
    "receiving_yards":  ["receiving_yards"],
    "rec+rush_yards":   ["receiving_yards", "rushing_yards"],
}


def prop_series(logs, prop_type):
    """Returns the prop's value per row as a Series (summing components for combo props)."""
    cols = _PROP_COL_MAP.get(prop_type)
    if not cols or logs.empty:
        return None
    total = None
    for col in cols:
        values = logs[col].fillna(0)
        total = values if total is None else total + values
    return total


def extract_stat(row, prop_type):
    """Extracts the actual stat value from a single weekly row."""
    cols = _PROP_COL_MAP.get(prop_type)
    if not cols:
        return None
    total = 0.0
    for col in cols:
        value = row.get(col)
        total += 0.0 if pd.isna(value) else float(value)
    return total


# ── Player-level splits ───────────────────────────────────────────────────────

def get_form_stats(player_id, prop_type, line, n_games=N_GAMES):
    """How often the player cleared the line in their last n_games, plus the average."""
    logs = get_game_log(player_id).tail(n_games)
    values = prop_series(logs, prop_type)
    if values is None or values.empty:
        return None
    over = int((values > line).sum())
    total = len(values)
    return {
        "hit_rate":   over / total,
        "avg":        float(values.mean()),
        "over_count": over,
        "games":      total,
    }


def get_player_vs_team(player_id, opponent_abbr, prop_type):
    """How the player has historically performed against a specific opponent."""
    logs = get_game_log(player_id)
    if logs.empty:
        return {"games": 0, "avg": None, "reliable": False}

    vs_team = logs[logs["opponent_team"] == opponent_abbr]
    values = prop_series(vs_team, prop_type)
    if values is None or values.empty:
        return {"games": 0, "avg": None, "reliable": False}

    return {
        "games":    len(values),
        "avg":      round(float(values.mean()), 2),
        "reliable": len(values) >= MIN_GAMES_VS_TEAM,
    }


def get_home_away_splits(player_id, prop_type):
    """Home vs away averages for the given prop type."""
    logs = get_game_log(player_id)
    values = prop_series(logs, prop_type)
    if values is None or values.empty:
        return None

    home = values[logs["is_home"].fillna(False)].mean()
    away = values[~logs["is_home"].fillna(False)].mean()
    if pd.isna(home) or pd.isna(away):
        return None

    return {
        "home_avg":       round(float(home), 2),
        "away_avg":       round(float(away), 2),
        "home_away_edge": round(float(home - away), 2),
    }


def get_rest_splits(player_id, prop_type):
    """Performance on short weeks (Thursday games) vs normal rest."""
    logs = get_game_log(player_id)
    values = prop_series(logs, prop_type)
    if values is None or values.empty or "rest_days" not in logs.columns:
        return None

    rest = logs["rest_days"]
    short = values[rest <= SHORT_WEEK_REST]
    normal = values[rest > SHORT_WEEK_REST]
    if short.empty or normal.empty:
        return None

    return {
        "short_week_avg": round(float(short.mean()), 2),
        "normal_avg":     round(float(normal.mean()), 2),
        "rest_edge":      round(float(normal.mean() - short.mean()), 2),
        "short_games":    len(short),
    }


def get_usage_profile(player_id, prop_type, n_games=N_GAMES):
    """
    Recent workload: how the volume stat that drives this prop (targets, carries,
    pass attempts) has trended, plus target share, air yards share and snap share.
    """
    logs = get_game_log(player_id)
    if logs.empty:
        return None

    recent = logs.tail(n_games)
    volume_cols = PROP_VOLUME_COLS.get(prop_type, ["targets"])

    def _volume(frame):
        total = 0.0
        for col in volume_cols:
            total += float(frame[col].fillna(0).mean())
        return total

    volume_recent = _volume(recent)
    volume_baseline = _volume(logs)

    target_share = float(recent["target_share"].fillna(0).mean())
    air_yards_share = float(recent["air_yards_share"].fillna(0).mean())

    return {
        "volume_recent":   round(volume_recent, 2),
        "volume_baseline": round(volume_baseline, 2),
        "target_share":    round(target_share, 4),
        "air_yards_share": round(air_yards_share, 4),
        "snap_pct":        get_snap_share(player_id, recent["game_id"].tolist(), recent["season"].tolist()),
        "games":           len(recent),
    }


def get_snap_share(player_id, game_ids, seasons):
    """
    Mean offensive snap % across the given game_ids. Restricting to game_ids the
    caller already has keeps this no-lookahead safe during backtests.
    """
    if not game_ids or not seasons:
        return None
    pfr_id = get_player_pfr_id(player_id)
    if not pfr_id:
        return None

    snaps = load_snaps(sorted(set(int(s) for s in seasons)))
    if snaps.empty:
        return None

    rows = snaps[(snaps["pfr_player_id"] == pfr_id) & (snaps["game_id"].isin(game_ids))]
    if rows.empty:
        return None
    pct = rows["offense_pct"].dropna().mean()
    return round(float(pct), 3) if not pd.isna(pct) else None


def get_teammate_out_impact(player_id, teammate_name, prop_type):
    """
    Compares the player's production in games the teammate played vs sat, and
    reports how much target share that teammate normally occupies — the vacated
    share is the real mechanism behind a WR2/RB2 stepping up.
    """
    teammate_id = get_player_id(teammate_name)
    if not teammate_id:
        return None

    logs = get_game_log(player_id)
    teammate_logs = get_game_log(teammate_id)
    values = prop_series(logs, prop_type)
    if values is None or values.empty:
        return None

    teammate_games = set(teammate_logs["game_id"]) if not teammate_logs.empty else set()
    played = logs["game_id"].isin(teammate_games)

    with_tm = values[played].mean()
    without_tm = values[~played].mean()
    n_without = int((~played).sum())

    vacated_share = None
    if not teammate_logs.empty:
        share = teammate_logs.tail(N_GAMES)["target_share"].fillna(0).mean()
        vacated_share = round(float(share), 4)

    boost = None
    if n_without >= MIN_GAMES_WITHOUT_TEAMMATE and not pd.isna(with_tm) and not pd.isna(without_tm):
        boost = round(float(without_tm - with_tm), 2)

    return {
        "with_avg":       round(float(with_tm), 2) if not pd.isna(with_tm) else None,
        "without_avg":    round(float(without_tm), 2) if not pd.isna(without_tm) else None,
        "boost":          boost,
        "sample_without": n_without,
        "vacated_share":  vacated_share,
    }


# ── Defense vs position ───────────────────────────────────────────────────────
# There's no free API for this, so it's derived: sum every player at a position's
# production against each defense, per game, then average and rank the defenses.

_dvp_cache = {}


def get_defense_vs_position(opponent_abbr, position, prop_type, seasons=None, as_of=None):
    """
    What `opponent_abbr` allows to `position` for this prop, per game, ranked
    against the rest of the league. rank 1 = allows the most (weakest defense).

    as_of: optional (season, week) tuple — only games strictly before it are
    used, which is what keeps backtests honest.
    """
    seasons = tuple(sorted(seasons or recent_seasons()))
    key = (seasons, position, prop_type, as_of)

    if key not in _dvp_cache:
        stats = load_stats(seasons)
        rows = stats[stats["position"] == position]
        if as_of:
            season, week = as_of
            rows = rows[(rows["season"] < season) |
                        ((rows["season"] == season) & (rows["week"] < week))]

        values = prop_series(rows, prop_type)
        if values is None or values.empty:
            _dvp_cache[key] = None
        else:
            rows = rows.assign(_stat=values)
            per_game = rows.groupby(["opponent_team", "season", "week"])["_stat"].sum().reset_index()
            _dvp_cache[key] = per_game.groupby("opponent_team")["_stat"].mean()

    allowed = _dvp_cache[key]
    if allowed is None or len(allowed) < 2 or opponent_abbr not in allowed.index:
        return None

    ranks = allowed.rank(ascending=False)
    return {
        "allowed_avg":  round(float(allowed[opponent_abbr]), 2),
        "league_avg":   round(float(allowed.mean()), 2),
        "rank":         int(ranks[opponent_abbr]),
        "total_teams":  int(len(allowed)),
    }


# ── Schedule ──────────────────────────────────────────────────────────────────

@st.cache_data(ttl=3600)
def get_week_schedule():
    """
    This week's games from nflverse, with days of rest per side. Returns the
    next upcoming week during the season and an empty list in the offseason
    (nflverse's schedule file covers the regular season only).
    """
    season = current_season()
    sched = load_schedule([season]).dropna(subset=["gameday"]).copy()
    if sched.empty:
        return []

    sched["_date"] = pd.to_datetime(sched["gameday"])
    sched = sched.sort_values("_date")

    cutoff = pd.Timestamp(datetime.now().date()) - timedelta(days=1)
    upcoming = sched[sched["_date"] >= cutoff]
    if upcoming.empty:
        return []

    week = int(upcoming.iloc[0]["week"])
    games = []
    for _, row in sched[sched["week"] == week].iterrows():
        games.append({
            "home_team":    row["home_team"],
            "away_team":    row["away_team"],
            "home_team_id": row["home_team"],
            "away_team_id": row["away_team"],
            "season":       int(row["season"]),
            "week":         week,
            "gameday":      row["gameday"],
            "weekday":      row["weekday"],
            "home_rest":    int(row["home_rest"]) if not pd.isna(row["home_rest"]) else 7,
            "away_rest":    int(row["away_rest"]) if not pd.isna(row["away_rest"]) else 7,
        })
    return games


@st.cache_data(ttl=3600)
def get_espn_games():
    """
    Current scoreboard from ESPN's public site API — the fallback when nflverse's
    schedule has nothing for this week (preseason, or a file that hasn't shipped
    yet). No rest data, so callers default to a normal week.
    """
    try:
        r = requests.get(f"{ESPN_API}/scoreboard", timeout=15)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        print(f"ESPN schedule fetch error: {e}")
        return []

    games = []
    for event in data.get("events", []):
        competitors = event.get("competitions", [{}])[0].get("competitors", [])
        sides = {c.get("homeAway"): c.get("team", {}).get("abbreviation", "") for c in competitors}
        home = get_team_abbr(sides.get("home"))
        away = get_team_abbr(sides.get("away"))
        if not home or not away:
            continue
        games.append({
            "home_team":    home,
            "away_team":    away,
            "home_team_id": home,
            "away_team_id": away,
            "season":       int(data.get("season", {}).get("year", current_season())),
            "week":         int(data.get("week", {}).get("number", 0)),
            "gameday":      event.get("date", "")[:10],
            "weekday":      "",
            "home_rest":    7,
            "away_rest":    7,
        })
    return games


def get_this_weeks_games():
    """This week's fixtures — nflverse first, ESPN as the live fallback."""
    return get_week_schedule() or get_espn_games()
