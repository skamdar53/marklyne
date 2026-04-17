# app.py — Streamlit dashboard for the NBA betting algorithm
# Run with: streamlit run app.py

import streamlit as st
import pandas as pd
import plotly.graph_objects as go
import plotly.express as px
import sys
import os
from config import EASY_THRESHOLD, MODERATE_THRESHOLD

sys.path.insert(0, os.path.dirname(__file__))

st.set_page_config(
    page_title="NBA Picks",
    page_icon="🏀",
    layout="wide",
)


# ── Shared Display Functions ──────────────────────────────────────────────────
def _display_picks(picks, parlays):
    """Renders picks and parlays in the dashboard."""
    if not picks:
        st.warning("No picks generated.")
        return

    # ── Filters ───────────────────────────────────────────────────────────────
    all_teams = sorted({p.get("team", "") for p in picks if p.get("team")})
    all_games = sorted({f"{p.get('team', '?')} vs {p['opponent']}" for p in picks if p.get("team")})

    fkey = st.session_state.get("filter_reset", 0)
    f1, f2, f3, f4, f5 = st.columns([2, 2, 2, 2, 1])
    filter_team   = f1.selectbox("Filter by Team",   ["All"] + all_teams,  key=f"ft_{fkey}")
    filter_game   = f2.selectbox("Filter by Game",   ["All"] + all_games,  key=f"fg_{fkey}")
    filter_bucket = f3.selectbox("Filter by Bucket", ["All", "Easy", "Moderate", "Aggressive"], key=f"fb_{fkey}")
    filter_prop   = f4.selectbox("Filter by Prop",   ["All", "points", "rebounds", "assists", "pts+reb+ast", "three_pointers_made"], key=f"fp_{fkey}")
    if f5.button("Reset", key=f"fr_{fkey}"):
        st.session_state["filter_reset"] = fkey + 1
        st.rerun()

    filtered = picks
    if filter_team   != "All": filtered = [p for p in filtered if p.get("team") == filter_team]
    if filter_game   != "All":
        home, _, away = filter_game.partition(" vs ")
        filtered = [p for p in filtered if p.get("team") in (home, away)]
    if filter_bucket != "All": filtered = [p for p in filtered if p["bucket"] == filter_bucket.lower()]
    if filter_prop   != "All": filtered = [p for p in filtered if p["prop"] == filter_prop]

    easy       = [p for p in filtered if p["bucket"] == "easy"]
    moderate   = [p for p in filtered if p["bucket"] == "moderate"]
    aggressive = [p for p in filtered if p["bucket"] == "aggressive"]
    skipped    = [p for p in filtered if p["bucket"] == "skip"]

    m1, m2, m3, m4, m5 = st.columns(5)
    m1.metric("Total Props",   len(filtered))
    m2.metric("🟢 Easy",       len(easy))
    m3.metric("🟡 Moderate",   len(moderate))
    m4.metric("🔴 Aggressive", len(aggressive))
    m5.metric("⚪ Skip",        len(skipped))

    # ── Parlays first ─────────────────────────────────────────────────────────
    if parlays:
        st.markdown("---")
        st.subheader("💰 Parlay Suggestions")
        cols = st.columns(len(parlays))
        for col, parlay in zip(cols, parlays):
            label = parlay.get("label", f"{parlay['size']}-leg")
            col.markdown(f"**{label}**")
            col.markdown(f"Combined: `{parlay['combined_confidence']*100:.1f}%`")
            for leg in parlay["legs"]:
                col.markdown(f"• {leg['player']} — {leg['prop']} {leg['line']} ({leg['confidence']*100:.1f}%)")

    # ── Picks by bucket ────────────────────────────────────────────────────────
    st.markdown("---")
    sentiment_icon = {"positive": "🟢", "negative": "🔴", "neutral": "🟡"}

    for bucket, group in [
        ("🟢 Easy", easy), ("🟡 Moderate", moderate),
        ("🔴 Aggressive", aggressive), ("⚪ Skip", skipped),
    ]:
        if not group:
            continue
        st.subheader(bucket)
        for p in group:
            with st.expander(f"**{p['player']}** — {p['prop']} {p['line']}+ vs {p['opponent']}  |  {p['confidence']*100:.1f}%"):
                reasons = p.get("reasons", [])
                if reasons:
                    for sentiment, text in reasons:
                        st.markdown(f"{sentiment_icon.get(sentiment, '•')} {text}")
                else:
                    st.write("No reasoning available.")

    # ── Confidence chart ───────────────────────────────────────────────────────
    st.markdown("---")
    st.subheader("Confidence Breakdown")
    rows = [{"Player": p["player"], "Prop": p["prop"],
             "Confidence": round(p["confidence"]*100, 1),
             "Bucket": p["bucket"].upper()} for p in filtered]
    df = pd.DataFrame(rows)
    fig = px.bar(
        df, x="Player", y="Confidence", color="Bucket",
        color_discrete_map={"EASY": "green", "MODERATE": "gold", "AGGRESSIVE": "red"},
        labels={"Confidence": "Confidence (%)"},
        title="Pick Confidence by Player",
        hover_data=["Prop"],
    )
    fig.add_hline(y=EASY_THRESHOLD*100,     line_dash="dash", line_color="green",  annotation_text=f"Easy ({EASY_THRESHOLD*100:.0f}%)")
    fig.add_hline(y=MODERATE_THRESHOLD*100, line_dash="dash", line_color="orange", annotation_text=f"Moderate ({MODERATE_THRESHOLD*100:.0f}%)")
    st.plotly_chart(fig, use_container_width=True)


def _display_backtest(bankroll):
    """Renders backtest results and bankroll chart."""
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Final Balance",  f"${bankroll.balance:,.2f}",
              delta=f"${bankroll.balance - bankroll.start:+,.2f}")
    m2.metric("ROI",            f"{bankroll.roi()}%")
    m3.metric("Win Rate",       f"{bankroll.win_rate()}%")
    m4.metric("Bets Placed",    bankroll.wins + bankroll.losses)

    if not bankroll.history:
        st.warning("No bets placed — try a player with more games vs this opponent.")
        return

    balances = [bankroll.start] + [b["balance"] for b in bankroll.history]
    fig = go.Figure()
    fig.add_trace(go.Scatter(
        y=balances,
        mode="lines+markers",
        name="Bankroll",
        line=dict(color="green" if bankroll.roi() >= 0 else "red", width=2),
    ))
    fig.add_hline(y=bankroll.start, line_dash="dash",
                  line_color="gray", annotation_text="Starting balance")
    fig.update_layout(
        title="Bankroll Over Time",
        xaxis_title="Bet #",
        yaxis_title="Balance ($)",
        hovermode="x unified",
    )
    st.plotly_chart(fig, use_container_width=True)

    st.subheader("Bet History")
    df = pd.DataFrame(bankroll.history)[[
        "label", "bucket", "confidence", "bet_size", "hit", "outcome", "balance"
    ]]
    df["confidence"] = df["confidence"].apply(lambda x: f"{x*100:.1f}%" if x else "parlay")
    st.dataframe(df, use_container_width=True, hide_index=True)

    st.subheader("Bet Reasoning")
    sentiment_icon = {"positive": "🟢", "negative": "🔴", "neutral": "🟡"}
    for bet in bankroll.history:
        result_icon = "✅" if bet["hit"] else "❌"
        label = f"{result_icon} {bet['label']} — {bet['outcome']}"
        with st.expander(label):
            reasons = bet.get("reasons", [])
            if reasons:
                for sentiment, text in reasons:
                    icon = sentiment_icon.get(sentiment, "•")
                    st.markdown(f"{icon} {text}")
            else:
                st.write("No reasoning available.")


# ── Sidebar ───────────────────────────────────────────────────────────────────
st.sidebar.title("🏀 NBA Picks Algorithm")
st.sidebar.markdown(
    "An algorithm that scores NBA player props using 7 data factors and recommends "
    "the best bets and parlays for the day."
)
st.sidebar.markdown("---")
mode = st.sidebar.radio("Mode", ["Today's Picks (Live)", "Manual Slate", "Backtest"])
st.sidebar.markdown("---")
st.sidebar.markdown("**Bet Sizing**")
starting_bankroll = st.sidebar.number_input("Starting Bankroll ($)", value=1000, step=100)
st.sidebar.markdown("---")
with st.sidebar.expander("How it works"):
    st.markdown("""
**7 scoring factors (0–100% each):**

| Factor | Weight |
|--------|--------|
| Recent streak vs line | 25% |
| Historical matchup vs team | 20% |
| Opponent defense + pace + zone | 20% |
| Home / Away split | 10% |
| Rest / Back-to-back | 10% |
| Teammate availability | 10% |
| Line movement | 5% |

**Confidence buckets:**
- 🟢 **Easy** — 62%+ (safest)
- 🟡 **Moderate** — 56–62%
- 🔴 **Aggressive** — 50–56%
- ⚪ **Skip** — below 50%
    """)
st.sidebar.markdown("---")
st.sidebar.markdown("**Modes:**")
st.sidebar.markdown("- **Today's Picks** — pulls live PrizePicks lines automatically")
st.sidebar.markdown("- **Manual Slate** — enter your own players and props")
st.sidebar.markdown("- **Backtest** — test the algorithm against historical games")

# ── Today's Picks ─────────────────────────────────────────────────────────────
if mode == "Today's Picks (Live)":
    st.title("🏀 Today's NBA Picks")

    missing_raw = st.text_area(
        "Missing Teammates (optional)",
        placeholder="Anthony Davis: LeBron James\nKlay Thompson: Stephen Curry",
        height=80,
    )

    if st.button("🔄 Pull Today's Lines", type="primary"):
        missing = {}
        for line in missing_raw.strip().splitlines():
            if ":" in line:
                player, teammate = line.split(":", 1)
                missing[player.strip()] = teammate.strip()

        with st.spinner("Fetching schedule and PrizePicks lines..."):
            from data.schedule import build_auto_slate
            from data.lines import get_prizepicks_lines
            raw_lines = get_prizepicks_lines()
            slate = build_auto_slate(missing, lines=raw_lines)

        if not raw_lines:
            st.error(
                "Could not fetch PrizePicks lines. Their API may be temporarily unavailable "
                "or lines haven't been posted yet (usually up by 10am ET on game days). "
                "Try the **Manual Slate** tab to score specific props manually."
            )
        elif not slate:
            st.warning("No props found for today — there may be no NBA games scheduled.")
        else:
            with st.spinner(f"Scoring {len(slate)} props..."):
                from algorithm.picks import generate_picks, generate_parlays
                picks   = generate_picks(slate, missing)
                parlays = generate_parlays(picks)
            st.session_state["live_picks"]   = picks
            st.session_state["live_parlays"] = parlays

    if st.session_state.get("live_picks"):
        _display_picks(st.session_state["live_picks"], st.session_state.get("live_parlays", []))

# ── Manual Slate ──────────────────────────────────────────────────────────────
elif mode == "Manual Slate":
    st.title("📋 Manual Slate")
    st.markdown("Add your own props below.")

    PROP_OPTIONS = ["points", "rebounds", "assists", "pts+reb+ast", "three_pointers_made"]
    POS_OPTIONS  = ["PG", "SG", "SF", "PF", "C"]

    slate = []
    for i in range(st.session_state.get("num_rows", 3)):
        c1, c2, c3, c4, c5, c6, c7 = st.columns([2, 2, 2, 1, 1, 1, 1])
        if i == 0:
            c1.markdown("**Player**");   c2.markdown("**Opponent**"); c3.markdown("**Prop**")
            c4.markdown("**Line**");     c5.markdown("**Pos**");      c6.markdown("**Home?**"); c7.markdown("**B2B?**")
        player   = c1.text_input("", key=f"player_{i}", placeholder="LeBron James", label_visibility="collapsed")
        opponent = c2.text_input("", key=f"opp_{i}",    placeholder="Warriors",     label_visibility="collapsed")
        prop     = c3.selectbox("",  PROP_OPTIONS, key=f"prop_{i}",                 label_visibility="collapsed")
        line     = c4.number_input("", key=f"line_{i}", value=20.0, step=0.5,       label_visibility="collapsed")
        position = c5.selectbox("",  POS_OPTIONS,  key=f"pos_{i}",                 label_visibility="collapsed")
        is_home  = c6.checkbox("",   key=f"home_{i}",                               label_visibility="collapsed")
        is_b2b   = c7.checkbox("",   key=f"b2b_{i}",                                label_visibility="collapsed")

        if player and opponent:
            slate.append({
                "player_name": player, "opponent": opponent,
                "prop_type": prop,     "line": line,
                "is_home": is_home,    "is_b2b": is_b2b, "position": position,
            })

    col_add, col_run = st.columns([1, 5])
    if col_add.button("+ Add Row"):
        st.session_state.num_rows = st.session_state.get("num_rows", 3) + 1
        st.rerun()

    if col_run.button("▶ Generate Picks", type="primary") and slate:
        with st.spinner("Scoring props..."):
            from algorithm.picks import generate_picks, generate_parlays
            picks   = generate_picks(slate)
            parlays = generate_parlays(picks)
        st.session_state["manual_picks"]   = picks
        st.session_state["manual_parlays"] = parlays

    if st.session_state.get("manual_picks"):
        _display_picks(st.session_state["manual_picks"], st.session_state.get("manual_parlays", []))

# ── Backtest ──────────────────────────────────────────────────────────────────
elif mode == "Backtest":
    st.title("📊 Backtest")
    st.markdown("Test the algorithm against historical games.")

    col1, col2, col3 = st.columns(3)
    player_name = col1.text_input("Player", value="LeBron James")
    opponent    = col2.text_input("Opponent Team", value="Warriors")
    prop_type   = col3.selectbox("Prop Type",
                                 ["points", "rebounds", "assists",
                                  "pts+reb+ast", "three_pointers_made"])
    position    = st.selectbox("Position", ["PG", "SG", "SF", "PF", "C"], index=2)

    if st.button("▶ Run Backtest", type="primary"):
        with st.spinner(f"Backtesting {player_name} vs {opponent}..."):
            from backtest.simulator import backtest_player
            bankroll = backtest_player(
                player_name=player_name,
                opponent_team=opponent,
                prop_type=prop_type,
                position=position,
                starting_bankroll=float(starting_bankroll),
            )

        if bankroll:
            st.session_state["backtest_bankroll"] = bankroll
        else:
            st.error("No data found. Try a different player or opponent.")

    if st.session_state.get("backtest_bankroll"):
        _display_backtest(st.session_state["backtest_bankroll"])
