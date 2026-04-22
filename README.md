# NBA Picks Algorithm

A data-driven NBA player prop betting algorithm that pulls live lines from PrizePicks, scores them across 7 statistical factors, and generates ranked picks and parlay suggestions for the day.

## How the Algorithm Works

Each prop is scored 0–100% across 7 independent factors, then combined into a single confidence score using weighted averaging:

| Factor | Weight | Data Source |
|--------|--------|-------------|
| Recent streak vs the line | 25% | Last 10 game logs |
| Historical matchup vs this team | 20% | Player game logs filtered by opponent |
| Opponent defense + pace + shot zone | 20% | NBA advanced team stats |
| Home / Away split | 10% | Season game logs |
| Rest / Back-to-back penalty | 10% | Schedule + game date diffs |
| Teammate availability boost | 10% | Games with/without teammate |
| Line movement signal | 5% | Placeholder (future: sharp money data) |

### Confidence Buckets
- 🟢 **Easy** — 62%+ confidence (recommended for singles + parlays)
- 🟡 **Moderate** — 56–62% (viable, slightly more risk)
- 🔴 **Aggressive** — 50–56% (high risk, small bet sizing)
- ⚪ **Skip** — below 50% (not recommended)

### Defense Scoring (most complex factor)
Goes beyond simple defensive rating — combines:
- Overall defensive rating vs league average
- Team pace (possessions per game)
- Prop-specific opponent stats (pts/reb/ast allowed vs league avg)
- Shot zone matching: player's 3P tendency vs team's perimeter defense weakness, and interior scoring tendency vs team's paint defense

## Modes

### Today's Picks (Live)
Pulls all current NBA props from PrizePicks, matches to today's NBA schedule (including home/away and back-to-back detection), scores every prop, and surfaces the best bets grouped by confidence tier. Generates 2-leg, 3-leg, and 4-leg parlay suggestions automatically.

### Manual Slate
Enter any player, opponent, prop, and line manually. Useful for testing specific matchups or when PrizePicks hasn't posted lines yet.

### Backtest
Test the algorithm's historical accuracy for a specific player/prop combination. Pulls 3 seasons of game logs and, for each historical game vs the chosen opponent, seeds the algorithm with only the data that was available at that point in time (last 10 games before tip-off). Compares predicted confidence to actual results and tracks fake bankroll performance with realistic PrizePicks payout multipliers. Every game in the sample is shown — confidence drives bet sizing, not whether a bet is placed.

## Tech Stack
- **Python** — core algorithm
- **nba_api** — NBA player game logs, team stats, schedule
- **PrizePicks** (unofficial API) — live player prop lines
- **Streamlit** — interactive dashboard
- **Plotly** — confidence charts and bankroll visualization

## Running Locally

```bash
pip install -r requirements.txt
streamlit run app.py
```

## Limitations
- PrizePicks live pull works locally but is blocked on the hosted version (cloud datacenter IPs are blocked by their API) — use Manual Slate on the deployed app
- nba_api has rate limits; first load takes 1–3 minutes as player data is fetched and cached
- Line movement factor is currently neutral (0.5) — sharp money data would improve accuracy
- Position-level defensive data sometimes falls back to team-level stats when nba_api endpoint is unavailable
