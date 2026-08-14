# Marklyne

A multi-sport player-prop scoring engine built directly on live Kalshi and Polymarket
prediction markets — no third-party prop book in the loop — with no-lookahead historical
backtesting for every model.

Covers **NBA, MLB, NFL, and NHL**. Each sport gets its own tailored, independently
researched factor model — not one generic model reused across sports — combined into a
single 0-100% confidence score per prop, with a volume-weighted cross-market signal
folded in wherever Kalshi and/or Polymarket price the same outcome.

## How it works

Every sport shares one scoring engine (`core/scoring.py`) that combines a sport's own
weighted factors into a single confidence score. What differs is the factor set itself:

| Sport | Factors | Data sources |
|-------|---------|--------------|
| **NBA** | recent streak, matchup history, opponent defense + pace + shot-zone match, home/away, rest/b2b, teammate-out usage boost, market signal | `nba_api` |
| **MLB** | recent streak, batter-vs-pitcher handedness split, pitcher matchup, bullpen strength, park factor, lineup spot, rest, market signal | `pybaseball` (Statcast/Baseball Savant), MLB Stats API |
| **NFL** | recent form, target share/usage, defense-vs-position, matchup history, teammate-out usage boost, rest/short-week, home/away, market signal | `nflreadpy` (nflverse), ESPN |
| **NHL** | streak, matchup, opponent defense, goalie edge, possession (Corsi/Fenwick), special teams, home/away, rest, usage, market signal | official NHL API (`api-web.nhle.com`), MoneyPuck |

Each prop lands in a confidence bucket:
- 🟢 **Easy** — 62%+ (recommended for singles + parlays)
- 🟡 **Moderate** — 56-62% (viable, slightly more risk)
- 🔴 **Aggressive** — 50-56% (high risk, small bet sizing)
- ⚪ **Skip** — below 50% (not recommended)

## Kalshi + Polymarket: the line source, not just a signal

Kalshi and Polymarket both run live sports prediction markets with public,
unauthenticated read APIs — and both already state player-prop bets as a threshold the
same way a sportsbook states a line (Kalshi's `floor_strike` markets, Polymarket's O/U
questions), so there's no need for a middleman prop book at all. `markets/lines.py`
pulls current player-prop markets from both platforms directly and de-duplicates them
into the live slate for **Today's Picks** — no PrizePicks, no anti-bot fight.

The same data feeds three more things:

1. **A scoring factor.** Wherever Kalshi or Polymarket price a prop at (or near) the
   line being scored, its implied probability is folded into that prop's confidence
   score, weighted by trading volume — a heavily-traded market pulls the signal further
   from neutral than a thin one.
2. **Prediction Markets tab.** Game by game, how far Kalshi's and Polymarket's
   independently-priced implied win probabilities diverge from each other.
3. **Ambiguous Markets tab.** Two more cuts across every live market (game outcomes and
   player props, both platforms): **toss-ups** — priced within 7 points of a coin flip
   yet still clearing a real volume floor (genuine uncertainty with real money behind
   it) — and **high-conviction disagreements** — cross-platform gaps where *both* sides
   are heavily traded, not just noise on a thin book.

Coverage varies by sport and by day (these are live, actively-traded markets, not a
fixed dataset) — MLB and NFL currently have the deepest player-prop coverage; NBA and
NHL prop markets pick back up in-season. When no market data exists for a given prop,
the signal defaults to neutral and every other factor is reweighted as if it weren't
there.

## Modes

- **Today's Picks (Live)** — pulls current player-prop lines straight from Kalshi and
  Polymarket for the selected sport, matches them to the schedule (home/away,
  rest/back-to-back), scores every prop, and surfaces the best bets by confidence tier
  with 2/3/4-leg parlay suggestions.
- **Manual Slate** — enter any player, opponent, prop, and line by hand. Useful for
  testing specific matchups or scoring a prop that isn't trading yet.
- **Backtest** — replays the algorithm against historical games for a player/opponent/
  prop combination. At each historical game, the model is seeded with only the data
  that existed before that game (no lookahead) and its predicted confidence is compared
  against the actual result, with a simulated bankroll tracked using realistic payout
  multipliers.
- **Prediction Markets** — the Kalshi vs. Polymarket discrepancy view described above.
- **Ambiguous Markets** — the toss-up / high-conviction-disagreement view above.

A scrolling ticker bar across the top of the app shows a live, volume-sorted feed of
Kalshi and Polymarket prices across all four sports at once.

## Tech stack

- **Python** — core algorithm, sport-agnostic scoring engine + one module per sport
- **nba_api**, **pybaseball**, **nflreadpy**, official **NHL API** — per-sport stat data
- **Kalshi**, **Polymarket** (public REST APIs) — prop lines, prices, and volume
- **Streamlit** — interactive dashboard
- **Plotly** — confidence charts and bankroll visualization

## Running locally

```bash
pip install -r requirements.txt
streamlit run app.py
```

CLI mode:

```bash
python3 main.py live --sport nba       # or mlb / nfl / nhl
python3 main.py manual --sport nba
python3 main.py backtest --sport nba
```

## Architecture

```
core/            sport-agnostic scoring engine, pick/parlay generation, shared config
sports/
  base.py        the SportModule interface every sport implements
  registry.py    wires all four sports together
  nba/ mlb/ nfl/ nhl/   data.py (stat sources) + factors.py (scoring + reasoning) + config.py
markets/
  kalshi.py      public Kalshi market data client
  polymarket.py  public Polymarket (Gamma + CLOB) market data client
  lines.py       de-duplicated live prop lines sourced from both platforms
  discrepancy.py cross-market scoring signal, discrepancy view, ambiguous-markets view, ticker feed
backtest/        no-lookahead simulator + fake-bankroll tracker (sport-agnostic)
app.py           Streamlit dashboard
main.py          CLI entry point
```

Adding a fifth sport means writing one new `sports/<sport>/` package against the
`SportModule` interface — nothing else changes.

## Limitations

- Kalshi and Polymarket player-prop coverage is live-market-dependent, not fixed — it's
  deepest for in-season sports with active trading volume and can be sparse or empty
  out of season.
- `nba_api` and the NHL/MLB/NFL data sources are all unofficial or undocumented (no
  vendor SLA) — expect occasional upstream schema changes.
- Cross-platform matching (team codes, player names, game dates) is inherently fuzzy —
  it's patched for known naming differences but will miss some pairings, especially
  around doubleheaders and midseason trades.
- Position-level defensive data sometimes falls back to team-level stats when a sport's
  primary endpoint is unavailable.
