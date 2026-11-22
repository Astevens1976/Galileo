# Meme-coin trading bot (paper trading first)

A multi-agent Solana meme-coin bot modeled on the "Grok Bot Terminal" style
dashboards going around social media. **It trades fake USDT only.** Real-money
execution is deliberately locked (see [Going live](#going-live)).

![dashboard](docs-dashboard.png)

## Read this first

- The viral videos are labeled *"Contains AI-generated media"*. $50 to $2,833
  overnight (56x) is not a realistic expectation; treat those clips as ads.
- Most new meme coins go to zero, many are deliberate rugs, and you are
  competing with faster bots. Fees, price impact and landing latency eat a
  large share of every trade. This bot charges all of those in paper mode on
  purpose.
- Results in `sim` mode come from a **synthetic market I wrote**. They test
  that the machinery works. They say nothing about whether the strategy makes
  money in the real market. Only `paper` mode (real data) can start to answer
  that, and only over weeks.

## The 10 agents

| Agent | Job |
|---|---|
| Launch Radar | Finds new Solana tokens (DexScreener latest profiles / boosts) |
| Whale Watch | Buy vs sell flow over 5 min; needs buys/sells >= 1.3 |
| Sentiment | Volume acceleration (5m pace vs 1h avg). Stands in for social hype: there's no free X/Telegram feed |
| LP Auditor | RugCheck: blocks mint authority, freeze authority, unlocked LP, any "danger" risk. **Fails closed** if the audit can't be fetched |
| Holder Map | Blocks if top-10 holders own > 35% |
| Risk Sizer | 2–4% of equity (less when volatile), capped at $100 and 1% of pool liquidity |
| Sniper | Executes the entry |
| Profit Taker | Ladder: sell 50% at 2x, 60% of the rest at 5x, keep a moonbag |
| Stop Guard | Hard stop -12%, trailing -22% from peak (after 1.3x), time stop after 45m flat, exits on pulled liquidity |
| Journal | Writes every event and fill to `logs/` |

Portfolio guards: max 3 open positions, **halt new entries after -15% in a
day**, and halt for good after -40% from the start.

## Run it

Needs Python 3.10+. No packages to install.

```bash
cd trading-bot

# 1. Synthetic market + dashboard at http://127.0.0.1:8787
python3 -m tradebot

# 2. Fast-forward one simulated night and print a summary
python3 -m tradebot --headless --hours 8 --seed 1

# 3. REAL Solana market data, fake money, real time (run this for weeks)
python3 -m tradebot --mode paper --balance 1000
```

Change any setting by copying `config.example.json` to `config.json`, editing
it, and passing `--config config.json`.

Each session writes `logs/session-*/events.jsonl`, `trades.csv` and
`summary.md`. That's the "reads it at 7am" part.

Tests: `python3 -m pytest -q`

## Modes

| Mode | Market data | Money |
|---|---|---|
| `sim` | synthetic, seeded, offline | fake |
| `paper` | real: DexScreener + RugCheck public APIs | fake |
| `live` | real | real. **Locked**, exits with an explanation |

Paper fills use a constant-product pool model: you pay 1% fee per side,
$0.05 network fee per side, 1% landing slippage, plus price impact based on
your size vs pool liquidity. A round trip at an unchanged price loses about 4%.

## Going live

Before any real USDT is involved:

1. Run `paper` mode for at least 2–4 weeks. Look at `trades.csv`: is it
   profitable **after costs**, and not just because of one lucky runner?
2. If yes: use a brand-new wallet that only holds money you can lose entirely.
   Start with a small amount.
3. Live execution would add: a USDT -> token swap via a Solana aggregator,
   transaction simulation before sending, hard per-trade and per-day USD caps,
   and a kill switch. Keys come from an environment variable and never get
   committed.

That last step isn't built yet on purpose. It should only be built once the
paper results justify it.
