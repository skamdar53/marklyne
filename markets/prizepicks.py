# markets/prizepicks.py — fetch current PrizePicks lines for any league
#
# PrizePicks' unofficial API is protected by DataDome, which fingerprints
# TLS/HTTP client behavior and flags datacenter/cloud IPs far more
# aggressively than residential ones. Live pulls work when run locally;
# on a hosted deployment, use Manual Slate instead.

import requests
import streamlit as st

PRIZEPICKS_API = "https://api.prizepicks.com"


@st.cache_data(ttl=3600)
def get_leagues():
    """
    Fetches the current list of leagues live on PrizePicks' board.
    Returns a dict of {league_name: league_id}. Leagues only appear here
    while they have active projections, so don't cache this across days.
    """
    url = f"{PRIZEPICKS_API}/leagues"
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        r = requests.get(url, headers=headers, timeout=15)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        print(f"PrizePicks leagues fetch error: {e}")
        return {}

    return {
        item["attributes"]["name"]: int(item["id"])
        for item in data.get("data", [])
        if item.get("type") == "league"
    }


@st.cache_data(ttl=3600)
def get_prizepicks_lines(league_id, prop_map, valid_prop_types):
    """
    Fetches current player props from PrizePicks for a given league.

    league_id: PrizePicks numeric league id (NBA=7, MLB=2, NFL=9, NHL=8)
    prop_map: dict mapping PrizePicks' raw stat_type string (lowercased,
              spaces -> underscores) to our normalized prop_type name
    valid_prop_types: set/list of prop_type names we actually score

    Returns a list of dicts with:
        - player_name, position, team
        - prop_type, line, description
    """
    url = f"{PRIZEPICKS_API}/projections?league_id={league_id}&per_page=250"
    headers = {"User-Agent": "Mozilla/5.0"}

    try:
        r = requests.get(url, headers=headers, timeout=15)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        print(f"PrizePicks fetch error (league {league_id}): {e}")
        return []

    players_map = {}
    for item in data.get("included", []):
        if item["type"] == "new_player":
            pid = item["id"]
            attrs = item["attributes"]
            players_map[pid] = {
                "name":     attrs.get("name", ""),
                "team":     attrs.get("team", ""),
                "position": attrs.get("position", ""),
            }

    lines = []
    for proj in data.get("data", []):
        attrs = proj["attributes"]

        prop_type_raw = attrs.get("stat_type", "").lower().replace(" ", "_")
        prop_type = prop_map.get(prop_type_raw)
        if prop_type not in valid_prop_types:
            continue

        line = attrs.get("line_score")
        if line is None:
            continue

        player_rel = proj.get("relationships", {}).get("new_player", {})
        player_id  = player_rel.get("data", {}).get("id")
        player_info = players_map.get(player_id, {})

        lines.append({
            "player_name":  player_info.get("name", "Unknown"),
            "position":     player_info.get("position", ""),
            "team":         player_info.get("team", ""),
            "prop_type":    prop_type,
            "line":         float(line),
            "description":  attrs.get("description", ""),
            "_start_time":  attrs.get("start_time", ""),
        })

    return lines
