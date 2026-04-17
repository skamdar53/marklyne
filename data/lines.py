# data/lines.py — fetch current PrizePicks lines and detect line movement

import requests
import pandas as pd
import streamlit as st
from config import PRIZEPICKS_NBA_ID, PROP_TYPES


@st.cache_data(ttl=3600)
def get_prizepicks_lines():
    """
    Fetches current NBA player props from PrizePicks unofficial API.

    Returns a list of dicts with:
        - player_name
        - prop_type
        - line
        - team
        - opponent
        - is_home
    """
    url = f"https://api.prizepicks.com/projections?league_id={PRIZEPICKS_NBA_ID}&per_page=250"
    headers = {"User-Agent": "Mozilla/5.0"}

    try:
        r = requests.get(url, headers=headers, timeout=15)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        print(f"PrizePicks fetch error: {e}")
        return []

    # Build player lookup from included data
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

        # Normalize prop type to our standard names
        prop_map = {
            "points":                   "points",
            "rebounds":                 "rebounds",
            "assists":                  "assists",
            "points+rebounds+assists":  "pts+reb+ast",
            "3-pt_made":                "three_pointers_made",
            "3_pointers_made":          "three_pointers_made",
        }
        prop_type = prop_map.get(prop_type_raw)
        if prop_type not in PROP_TYPES:
            continue

        line = attrs.get("line_score")
        if line is None:
            continue

        # Get player info
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


def get_high_volume_lines(top_n=30):
    """
    Returns the top N lines sorted by PrizePicks flash/featured status
    as a proxy for volume — featured lines are the highest action props.
    """
    lines = get_prizepicks_lines()
    # PrizePicks doesn't expose volume directly — featured/goblin/demon
    # lines are their highest volume. For now return all and let the
    # scoring algorithm filter by confidence.
    return lines[:top_n] if len(lines) > top_n else lines


def display_lines(lines):
    """Pretty prints a list of lines."""
    if not lines:
        print("No lines found.")
        return
    for l in lines:
        print(f"{l['player_name']} ({l['team']}) — {l['prop_type']}: {l['line']}")
