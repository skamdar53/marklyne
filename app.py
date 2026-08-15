# app.py — Streamlit dashboard for Marklyne
# Run with: streamlit run app.py

import sys
import os

import streamlit as st
import pandas as pd
import plotly.graph_objects as go
import plotly.express as px

sys.path.insert(0, os.path.dirname(__file__))

from core.config import EASY_THRESHOLD, MODERATE_THRESHOLD
from core.picks import generate_picks, generate_parlays
from sports.registry import SPORTS, get_sport

st.set_page_config(
    page_title="Marklyne",
    page_icon="⚖️",
    layout="wide",
)


# ── Ticker bar ──────────────────────────────────────────────────────────────
def _ticker_track_html(items, label, accent):
    """One scrolling strip. Items are duplicated once so the CSS loop has no
    visible seam; probability is colored green/red of the 50% line so the
    strip reads at a glance the way a real stock ticker does."""
    if not items:
        spans = f'<span class="tick-item">no live {label.lower()} data right now</span>'
    else:
        parts = []
        for it in items:
            pct = it["implied_prob"] * 100
            color = "#2ecc71" if pct >= 50 else "#ff5c5c"
            parts.append(
                f'<span class="tick-item">{it["label"]} '
                f'<b style="color:{color}">{pct:.0f}%</b></span>'
                f'<span class="tick-sep">·</span>'
            )
        spans = "".join(parts) * 2

    return f"""
    <div class="ticker-row">
      <div class="ticker-label" style="background:{accent}">{label}</div>
      <div class="ticker-track"><div class="ticker-scroll">{spans}</div></div>
    </div>
    """


def _render_ticker(sport_keys):
    from markets.discrepancy import get_ticker_items

    try:
        kalshi_items = get_ticker_items("kalshi", sport_keys)
    except Exception:
        kalshi_items = []
    try:
        poly_items = get_ticker_items("polymarket", sport_keys)
    except Exception:
        poly_items = []

    st.markdown(
        """
        <style>
        .ticker-row { display: flex; align-items: stretch; border-radius: 6px;
                      overflow: hidden; margin-bottom: 6px; font-family: 'SF Mono', Consolas, monospace; }
        .ticker-label { flex: 0 0 auto; padding: 6px 14px; color: #0b0b0b; font-weight: 700;
                         font-size: 0.8rem; letter-spacing: 0.08em; display: flex; align-items: center; }
        .ticker-track { flex: 1 1 auto; background: #111318; overflow: hidden; white-space: nowrap; }
        .ticker-scroll { display: inline-block; padding: 6px 0; animation: ticker-scroll 55s linear infinite; }
        .ticker-row:hover .ticker-scroll { animation-play-state: paused; }
        .tick-item { color: #e8e8e8; font-size: 0.82rem; padding: 0 10px; }
        .tick-sep { color: #4a4a4a; }
        @keyframes ticker-scroll { 0% { transform: translateX(0); } 100% { transform: translateX(-50%); } }
        </style>
        """,
        unsafe_allow_html=True,
    )
    st.markdown(_ticker_track_html(kalshi_items, "KALSHI", "#ffd23f"), unsafe_allow_html=True)
    st.markdown(_ticker_track_html(poly_items, "POLYMARKET", "#5ac8fa"), unsafe_allow_html=True)


# ── Shared Display Functions ──────────────────────────────────────────────────
def _display_picks(sport, picks, parlays):
    """Renders picks and parlays in the dashboard."""
    if not picks:
        st.warning("No picks generated.")
        return

    all_teams = sorted({p.get("team", "") for p in picks if p.get("team")})
    all_games = sorted({f"{p.get('team', '?')} vs {p['opponent']}" for p in picks if p.get("team")})
    prop_options = ["All"] + sport.prop_types

    fkey = st.session_state.get("filter_reset", 0)
    f1, f2, f3, f4, f5 = st.columns([2, 2, 2, 2, 1])
    filter_team   = f1.selectbox("Filter by Team",   ["All"] + all_teams,  key=f"ft_{fkey}")
    filter_game   = f2.selectbox("Filter by Game",   ["All"] + all_games,  key=f"fg_{fkey}")
    filter_bucket = f3.selectbox("Filter by Bucket", ["All", "Easy", "Moderate", "Aggressive"], key=f"fb_{fkey}")
    filter_prop   = f4.selectbox("Filter by Prop",   prop_options, key=f"fp_{fkey}")
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
        title=f"{sport.label} Pick Confidence by Player",
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
        st.warning("No bets placed — see debug log below for why each game was skipped.")
        debug = getattr(bankroll, "debug_log", None)
        if debug:
            with st.expander("Debug log"):
                st.markdown(debug)
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


def _display_market_discrepancies(sport_key):
    """Renders cross-market (Kalshi vs Polymarket) discrepancy tab."""
    from markets.discrepancy import get_platform_discrepancies

    st.markdown(
        "Compares implied win probability for the same games priced independently on **Kalshi** "
        "and **Polymarket**. A large gap means two real-money markets disagree on the same outcome. "
        "This view is informational; it doesn't feed into the confidence scores above (that happens "
        "via the `market_signal` factor, weighted by trading volume, folded directly into each prop's "
        "score — and for Today's Picks, these two platforms *are* the line source, not just a signal)."
    )

    with st.spinner("Pulling live Kalshi and Polymarket data..."):
        try:
            rows = get_platform_discrepancies(sport_key)
        except Exception as e:
            st.error(f"Could not pull prediction market data: {e}")
            return

    if not rows:
        st.info("No overlapping markets found on both platforms for this sport right now.")
        return

    df = pd.DataFrame(rows)
    df["kalshi_prob"]     = (df["kalshi_prob"] * 100).round(1)
    df["polymarket_prob"] = (df["polymarket_prob"] * 100).round(1)
    df["gap"]             = (df["gap"] * 100).round(1)
    df = df.rename(columns={
        "game": "Game", "outcome_label": "Outcome",
        "kalshi_prob": "Kalshi %", "polymarket_prob": "Polymarket %", "gap": "Gap (pts)",
        "kalshi_volume": "Kalshi Volume", "polymarket_volume": "Polymarket Volume",
    })
    st.dataframe(df, use_container_width=True, hide_index=True)


def _display_ambiguous_markets(sport_key):
    """Renders the toss-up / high-conviction-disagreement tab."""
    from markets.discrepancy import get_ambiguous_markets, HIGH_VOLUME_FLOOR, TOSSUP_BAND

    scope_choice = st.radio(
        "Scope",
        [f"This sport ({SPORTS[sport_key].label})", "Everything — all Kalshi/Polymarket categories"],
        horizontal=True,
    )
    scope = "all" if scope_choice.startswith("Everything") else "sport"

    st.markdown(
        f"Two cuts of the most **interesting** live markets "
        f"(volume floor: **{HIGH_VOLUME_FLOOR:,.0f}** contracts, so this is real "
        f"conviction, not noise on a thin book):"
    )
    if scope == "all":
        st.caption(
            "Toss-ups below span politics, economics, entertainment, crypto, and more — not just "
            "sports. High-conviction disagreements still require matching the same market across "
            "both platforms, which only works reliably for sports right now (see Prediction Markets "
            f"tab), so that section stays scoped to {SPORTS[sport_key].label}."
        )

    with st.spinner("Scanning live Kalshi and Polymarket markets" +
                     (" across every category — this can take a bit longer..." if scope == "all" else "...")):
        try:
            result = get_ambiguous_markets(sport_key, scope=scope)
        except Exception as e:
            st.error(f"Could not pull prediction market data: {e}")
            return

    st.subheader(f"🎲 Toss-ups — within {TOSSUP_BAND*100:.0f} pts of 50/50")
    st.caption("Genuine outcome uncertainty that still has heavy money behind it.")
    toss_ups = result.get("toss_ups", [])
    if not toss_ups:
        st.info("No high-volume toss-ups right now.")
    else:
        df = pd.DataFrame(toss_ups)
        df["implied_prob"] = (df["implied_prob"] * 100).round(1)
        df = df.rename(columns={
            "source": "Platform", "market_kind": "Type", "label": "Market",
            "game": "Game / Category", "implied_prob": "Implied %", "volume": "Volume",
        })
        st.dataframe(df, use_container_width=True, hide_index=True)

    st.markdown("---")
    st.subheader(f"⚡ High-conviction disagreements — {SPORTS[sport_key].label}")
    st.caption("Both platforms are heavily traded on the same outcome, and still don't agree.")
    disagreements = result.get("disagreements", [])
    if not disagreements:
        st.info("No high-volume cross-platform disagreements right now.")
    else:
        df = pd.DataFrame(disagreements)
        df["kalshi_prob"]     = (df["kalshi_prob"] * 100).round(1)
        df["polymarket_prob"] = (df["polymarket_prob"] * 100).round(1)
        df["gap"]             = (df["gap"] * 100).round(1)
        df = df.rename(columns={
            "game": "Game", "outcome_label": "Outcome", "market_kind": "Type",
            "kalshi_prob": "Kalshi %", "polymarket_prob": "Polymarket %", "gap": "Gap (pts)",
            "kalshi_volume": "Kalshi Volume", "polymarket_volume": "Polymarket Volume",
        })
        st.dataframe(df, use_container_width=True, hide_index=True)


# ── Ticker ────────────────────────────────────────────────────────────────────
_render_ticker(list(SPORTS.keys()))

# ── Sidebar ───────────────────────────────────────────────────────────────────
st.sidebar.title("⚖️ Marklyne")
st.sidebar.markdown(
    "A multi-sport player-prop scoring engine built directly on live Kalshi and "
    "Polymarket prediction markets — no third-party prop book in the loop."
)
st.sidebar.markdown("---")

sport_key = st.sidebar.selectbox(
    "Sport",
    list(SPORTS.keys()),
    format_func=lambda k: f"{SPORTS[k].icon} {SPORTS[k].label}",
)
sport = get_sport(sport_key)

mode = st.sidebar.radio(
    "Mode",
    ["Today's Picks (Live)", "Manual Slate", "Backtest", "Prediction Markets", "Ambiguous Markets"],
)
st.sidebar.markdown("---")
st.sidebar.markdown("**Bet Sizing**")
starting_bankroll = st.sidebar.number_input("Starting Bankroll ($)", value=1000, step=100)
st.sidebar.markdown("---")
with st.sidebar.expander("How it works"):
    weight_rows = "\n".join(f"| {k.replace('_', ' ').title()} | {v*100:.0f}% |" for k, v in sport.weights.items())
    st.markdown(f"""
**{sport.label} scoring factors (0-100% each):**

| Factor | Weight |
|--------|--------|
{weight_rows}

**Confidence buckets:**
- 🟢 **Easy** — 62%+ (safest)
- 🟡 **Moderate** — 56-62%
- 🔴 **Aggressive** — 50-56%
- ⚪ **Skip** — below 50%
    """)
st.sidebar.markdown("---")
st.sidebar.markdown("**Modes:**")
st.sidebar.markdown("- **Today's Picks** — live prop lines pulled directly from Kalshi/Polymarket")
st.sidebar.markdown("- **📋 Manual Slate** — enter any player + line and get a full score instantly")
st.sidebar.markdown("- **Backtest** — test the algorithm against historical games")
st.sidebar.markdown("- **Prediction Markets** — Kalshi vs Polymarket implied-probability discrepancies")
st.sidebar.markdown("- **Ambiguous Markets** — high-volume toss-ups and cross-platform disagreements")

# ── Today's Picks ─────────────────────────────────────────────────────────────
if mode == "Today's Picks (Live)":
    st.title(f"{sport.icon} Today's {sport.label} Picks")

    st.info(
        "Lines are pulled directly from live Kalshi and Polymarket player-prop markets — "
        "no third-party prop book, so no anti-bot blocking to fight. Coverage depends on what's "
        "actively trading right now: deepest for in-season sports, thin or empty out of season. "
        "If a sport comes back empty, try **Manual Slate** instead.",
        icon="ℹ️",
    )

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

        with st.spinner(f"Fetching {sport.label} schedule and live market lines..."):
            slate = sport.build_auto_slate(missing)

        if not slate:
            st.warning(
                f"No props found for {sport.label} right now — Kalshi/Polymarket don't have "
                "active player-prop markets for this sport at the moment. Try the "
                "**Manual Slate** tab to score specific props manually."
            )
        else:
            with st.spinner(f"Scoring {len(slate)} props..."):
                picks   = generate_picks(sport, slate, missing)
                parlays = generate_parlays(picks)
            st.session_state["live_picks"]   = picks
            st.session_state["live_parlays"] = parlays

    if st.session_state.get("live_picks"):
        _display_picks(sport, st.session_state["live_picks"], st.session_state.get("live_parlays", []))

# ── Manual Slate ──────────────────────────────────────────────────────────────
elif mode == "Manual Slate":
    st.title("📋 Manual Slate")
    st.markdown(f"Add your own {sport.label} props below.")

    pos_options = sport.position_options or [""]
    n_cols = 7 if sport.position_options else 6

    slate = []
    for i in range(st.session_state.get("num_rows", 3)):
        widths = [2, 2, 2, 1, 1, 1, 1] if sport.position_options else [2, 2, 2, 1, 1, 1]
        cols = st.columns(widths)
        if i == 0:
            headers = ["**Player**", "**Opponent**", "**Prop**", "**Line**"]
            if sport.position_options:
                headers.append("**Pos**")
            headers += ["**Home?**", "**B2B?**"]
            for c, h in zip(cols, headers):
                c.markdown(h)

        c_iter = iter(cols)
        player   = next(c_iter).text_input("", key=f"player_{i}", placeholder="Player name", label_visibility="collapsed")
        opponent = next(c_iter).text_input("", key=f"opp_{i}",    placeholder="Opponent",    label_visibility="collapsed")
        prop     = next(c_iter).selectbox("",  sport.prop_types,  key=f"prop_{i}",            label_visibility="collapsed")
        line     = next(c_iter).number_input("", key=f"line_{i}", value=20.0, step=0.5,       label_visibility="collapsed")
        position = next(c_iter).selectbox("",  pos_options, key=f"pos_{i}", label_visibility="collapsed") if sport.position_options else None
        is_home  = next(c_iter).checkbox("",   key=f"home_{i}",                               label_visibility="collapsed")
        is_b2b   = next(c_iter).checkbox("",   key=f"b2b_{i}",                                label_visibility="collapsed")

        if player and opponent:
            entry = {
                "player_name": player, "opponent": opponent,
                "prop_type": prop,     "line": line,
                "is_home": is_home,    "is_b2b": is_b2b,
            }
            if position:
                entry["position"] = position
            slate.append(entry)

    col_add, col_run = st.columns([1, 5])
    if col_add.button("+ Add Row"):
        st.session_state.num_rows = st.session_state.get("num_rows", 3) + 1
        st.rerun()

    if col_run.button("▶ Generate Picks", type="primary") and slate:
        with st.spinner("Scoring props..."):
            picks   = generate_picks(sport, slate)
            parlays = generate_parlays(picks)
        st.session_state["manual_picks"]   = picks
        st.session_state["manual_parlays"] = parlays

    if st.session_state.get("manual_picks"):
        _display_picks(sport, st.session_state["manual_picks"], st.session_state.get("manual_parlays", []))

# ── Backtest ──────────────────────────────────────────────────────────────────
elif mode == "Backtest":
    st.title("📊 Backtest")
    st.markdown(f"Test the {sport.label} algorithm against historical games.")

    col1, col2, col3 = st.columns(3)
    player_name = col1.text_input("Player", value="")
    opponent    = col2.text_input("Opponent Team", value="")
    prop_type   = col3.selectbox("Prop Type", sport.prop_types)
    position    = st.selectbox("Position", sport.position_options) if sport.position_options else None

    if st.button("▶ Run Backtest", type="primary") and player_name and opponent:
        with st.spinner(f"Backtesting {player_name} vs {opponent}..."):
            from backtest.simulator import backtest_player
            bankroll = backtest_player(
                sport,
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

# ── Prediction Markets ────────────────────────────────────────────────────────
elif mode == "Prediction Markets":
    st.title("🔀 Prediction Markets")
    st.markdown(f"Kalshi vs Polymarket discrepancies for {sport.label}.")
    _display_market_discrepancies(sport_key)

# ── Ambiguous Markets ─────────────────────────────────────────────────────────
elif mode == "Ambiguous Markets":
    st.title("🎲 Ambiguous Markets")
    st.markdown(f"Toss-ups and high-conviction disagreements for {sport.label}.")
    _display_ambiguous_markets(sport_key)
