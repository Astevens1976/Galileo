# DEX trading bot: MetaMask wallet, no exchange accounts

A multi-agent meme-coin trading bot, modeled on the "Grok Bot Terminal"
dashboards on social media. It trades **only on decentralized exchanges**
(PancakeSwap on BNB Chain by default) **straight from a MetaMask wallet
account**. No exchange sign-up, username or password, and nobody holds your
funds but you.

![dashboard](docs-dashboard.png)

## Read this first

- The viral videos are labeled *"Contains AI-generated media"*. $50 to $2,833
  overnight is not a realistic expectation.
- Most new meme coins go to zero, many are built to steal from buyers, and
  professional bots are faster than this one. **Only trade money you can lose
  completely.**
- The safety checks (honeypot, tax, mint, locked liquidity, holder
  concentration) catch a lot, not everything. Scams that pass every check exist.
- Slippage tolerance (8% on buys) leaves room for "sandwich" bots to take a
  cut of some trades. Lower `buy_slippage_pct` to reduce that; more buys will
  then fail.

## How it works: 10 agents

| Agent | Job |
|---|---|
| Launch Radar | Finds new tokens on DexScreener |
| Whale Watch | Buy vs sell flow over 5 min (buys/sells >= 1.3) |
| Sentiment | Volume acceleration (5m pace vs 1h average): a stand-in for hype, since there is no free X/Telegram feed |
| LP Auditor | GoPlus security scan: blocks honeypots, tax > 10%, owner-changeable tax, hidden owner, mintable, pausable, unverified source, < 50% of liquidity locked. **Blocks if the scan can't be fetched** |
| Holder Map | Blocks if top-10 wallets own > 35% |
| Risk Sizer | 2–4% of balance per trade (less when volatile) |
| Sniper | Executes the buy |
| Profit Taker | Sells 50% at 2x and 60% of the rest at 5x, then rides the remainder |
| Stop Guard | -12% hard stop, -22% trailing stop after 1.3x, exits after 45 min going nowhere, exits if liquidity is pulled |
| Journal | Logs every decision and trade to `logs/` |

Portfolio limits: max 3 open positions, no new buys after -15% in a day,
none at all after -40% overall.

### Live-trading protections

- Off unless `"live_enabled": true` is in your config file **and** you type
  `TRADE` at startup.
- **Max $10 per trade and $50 of buys per day** by default (`max_live_trade_usd`,
  `max_live_daily_spend_usd`).
- Every swap is dry-run first. A swap that would fail (a honeypot, or a price
  that moved too far) is never sent, so it costs no gas.
- Exact-amount token approvals only. It never grants unlimited spending.
- Checks that it's connected to the right chain (56 = BNB Chain) before
  sending anything. Gas price capped at 5 gwei.
- Keeps some BNB in reserve for gas so it can always sell.
- **Kill switch:** create a file named `STOP` in the bot folder. It sells
  everything and stops buying.
- Open positions are saved to `live_state.json` and picked up again after a
  restart.

## Setup

You need Python 3.10 or newer.

```bash
cd trading-bot
pip install -r requirements.txt
```

### Step 1: Practice with fake money (no wallet needed)

```bash
python3 -m tradebot                          # simulated market, dashboard at http://127.0.0.1:8787
python3 -m tradebot --mode paper             # REAL BNB Chain market data, fake $1,000
```

Run `--mode paper` for **2–4 weeks**. Each session writes
`logs/session-*/summary.md` and `trades.csv`. Only continue if it's
profitable after costs, and not just because of one lucky trade.

### Step 2: Make a separate bot account in MetaMask

1. In MetaMask, open the account menu and choose **Add account**. Name it
   "Trading bot". Never use your main account.
2. On the new account, open **Account details**, then **Show private key**,
   enter your MetaMask password, and copy the key.
3. In the `trading-bot` folder, create a file named `.env` containing one line:
   ```
   BOT_PRIVATE_KEY=0xPASTE_YOUR_KEY_HERE
   ```
   That file is git-ignored. Never share it, paste it into a website or chat,
   or commit it. Anyone with this key controls that account.

The bot account stays a normal MetaMask account. You can see its balances and
send funds back to your main account any time.

### Step 3: Test real swaps with free test coins (BNB Chain testnet)

1. In MetaMask, turn on **Show test networks** and select **BNB Chain Testnet**.
   If it isn't listed, add it manually: chain ID `97`, RPC
   `https://data-seed-prebsc-1-s1.bnbchain.org:8545`, symbol `tBNB`.
2. Get free tBNB for the bot account from the BNB Chain testnet faucet.
3. Run:
   ```bash
   python3 -m tradebot --selftest
   ```
   It wraps 0.001 tBNB, buys a token on PancakeSwap testnet, sells it back,
   and prints `PASS` with the transaction hashes. You can look them up on
   testnet.bscscan.com.

### Step 4: Real money, small

1. Send the bot account **USDT on BNB Chain (BEP-20)** plus about **0.02 BNB**
   for gas. USDT on Ethereum (ERC-20) is a different token and won't show up.
   Use MetaMask's Bridge feature if your USDT is on another chain.
2. Run a $1 round trip (USDT to WBNB and back) to confirm everything:
   ```bash
   python3 -m tradebot --selftest --network mainnet
   ```
3. Start live trading:
   ```bash
   cp config.live.example.json config.json      # edit the limits if you like
   python3 -m tradebot --mode live --config config.json
   ```
   Keep the computer awake and online. To stop buying and sell everything,
   create a file named `STOP` in the folder (or press Ctrl+C: open positions
   stay in the wallet and are managed again on restart).

## Settings

Every setting is in `config.example.json`. Copy what you want to change into
`config.json` and pass `--config config.json`. Other EVM chains work by
changing `chain`, `rpc_url`, `expected_chain_id`, `router_address` and
`quote_token_address` (any Uniswap-V2-style router).

## Testing

```bash
pip install -r requirements-dev.txt
python3 -m pytest -q
```

`tests/test_live_chain.py` starts a private in-memory blockchain, deploys the
official Uniswap V2 contracts (PancakeSwap V2 is a fork of them) and three
test tokens (normal, 5% tax, honeypot). It then runs the real live-trading
code against them: buys, partial and full sells, the take-profit ladder,
trailing stop, kill switch, daily cap, slippage refusal, honeypot detection
and restart recovery.

What hasn't been checked against the real network yet: the BNB Chain
endpoints, DexScreener and GoPlus responses, and the PancakeSwap testnet
router address. That's what `--selftest` and `--mode paper` are for, and why
they come before real money.

`trades.csv` records every fill. Keep it for your tax records.
