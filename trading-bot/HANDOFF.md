# Handoff: Adams Bot (from cloud session, Nov 22–25 2026)

Read this first, then continue the work in `G:\Adams Bot`.

## Who / constraints (from Adam)
- Trades **crypto only**, through a **MetaMask wallet on decentralized exchanges**. **Never** sign up for or log into a centralized exchange with a username/password.
- Start with **fake/test money**, real money (USDT) only later.
- **Save all historical and future data to `G:\Adams Bot`** (WSL path: `/mnt/g/Adams Bot`).
- Runs Windows with **WSL Ubuntu 24.04**, WSL user `adam`. Bot checked out at `~/Galileo/trading-bot` in WSL.
- Chose: BNB Chain + USDT for the meme-coin bot; separate MetaMask account whose key the bot uses (auto-trading); position size **20–40% of equity per trade** (aggressive, his choice); merged the "Grok Bot" prompt rules.
- Explain things in plain language; he is not a developer. Give copy-paste commands one line at a time (pasting several lines at once caused problems before).

## Code
- GitHub: `Astevens1976/Galileo`, branch `claude/automated-trading-bot-testnet-thvq6g`, draft PR #1. Everything is under `trading-bot/`.
- `tradebot/`: the meme-coin DEX bot (10 agents, dashboard at http://127.0.0.1:8787).
  - Modes: `sim` (synthetic), `paper` (real BNB Chain data via DexScreener + GeckoTerminal + GoPlus, fake money), `live` (real money via PancakeSwap V2; off unless `live_enabled: true` in config **and** typing `TRADE`).
  - Live safety: $50/buy and $300/day caps, swaps dry-run before sending, exact approvals, BNB gas reserve, `STOP` file = sell all + halt, state persisted.
  - `--data-dir "/mnt/g/Adams Bot"` (or env `ADAMS_BOT_DIR`) puts logs/trades/live state on G:.
- `research/`: 5-minute strategy study (see below).
- Tests: `python3 -m pytest -q` (34 pass; `tests/test_live_chain.py` runs the real live code against official Uniswap V2 bytecode + normal/tax/honeypot tokens on an in-memory EVM).

## Bot status
- Paper mode ran on Adam's PC against the real market: connections work. Discovery was too thin with only DexScreener (2 coins/hour); added GeckoTerminal `new_pools`/`trending_pools` (paced 30s/120s with backoff after HTTP 429). After that: ~100 coins/1.5h, a handful passing the size filters, none bought yet (rules are strict: 5x volume spike + 70% buys). He was going to leave it running overnight and paste the recap.
- If it never trades after a few nights, discuss loosening `min_volume_spike` (5 → ~3) or `min_volume_5m_usd` (25k → ~10k). That's his call (more trades = more risk).
- Not yet done: MetaMask bot account + `.env`, testnet `--selftest`, bridging his USDT (currently on **Arbitrum**) to **BNB Chain**, mainnet `$1` selftest, live.

## Research (Hyperliquid, 5-minute, 63 months)
Data: Binance USD-M futures archive (USDT) for the 60 most-traded Hyperliquid perps, Aug 2021 to Oct 2026, + funding. Hyperliquid itself only serves ~17 days of 5m candles; all 60 coins' recent prices match Hyperliquid within ~0.1%. Binance's LITUSDT was a different project before Dec 2025, so history before a coin's last zero-volume day is dropped. Raw history ~1.3 GB, re-download to G: with:
```
cd ~/Galileo/trading-bot && source .venv/bin/activate && pip install -r research/requirements.txt
python3 -m research.download --data-dir "/mnt/g/Adams Bot" --top 60
python3 -m research.verify   --data-dir "/mnt/g/Adams Bot"
python3 -m research.backtest --data-dir "/mnt/g/Adams Bot"
python3 -m research.report   --data-dir "/mnt/g/Adams Bot"
python3 -m research.universal --data-dir "/mnt/g/Adams Bot"
python3 -m research.target   --data-dir "/mnt/g/Adams Bot"
```
Assumptions: Hyperliquid taker 0.045% + 0.02% slippage on entries/stops, maker 0.015% on take-profits, funding included, entry at next bar open, same-bar TP+SL counts as a loss. Chosen on first 75% of each coin's history, judged on the last 25%.

Findings:
1. ~14,000 strategies/coin (12 families × long/short × TP/SL/hold grid). Lots hit 70% win rate, but almost all by geometry (small target, wide stop). Random entries with the same exits also win ~72% and lose −36%/coin.
2. Picking a best strategy per coin was overfitting: it qualified out-of-sample less often (21%) than a random 70% candidate (30%). Don't trust `report.md`'s 12-coin list.
3. **One rule for all coins held up:** RSI(14) on 5m < 20 → long; TP +2×ATR(14), SL −6×ATR(14), close after 12h. Test: 72% win overall, +5.4% per coin average, beats random by ~40 points; **19 coins** win ≥70% and profit in both periods: ZEC, VVV, kLUNC, TRUMP, SUI, XRP, ZRO, FET, ICP, BABY, ETHFI, XPL, LIT, ADA, ASTER, ONDO, WLFI, MON, kSHIB (`research/universal_rule.csv`).
4. Latest ask (in progress): **≥75% win, ≥+50% account growth within 20 trades, 30% of account as margin per trade, long or short, stop set from history.** `research/target.py` widens exits (TP ≤ 8 ATR, SL ≤ 12 ATR, hold ≤ 3 days) and finds the leverage needed for the median 20-trade window to reach +50%, then flags liquidation risk. Early result: many strategies win 75–95% (e.g. ZEC RSI long TP3/SL12 ATR, 24h: 82% win, ~+1%/trade), but reaching +50%/20 trades needs ~10x leverage, and at that leverage historical worst moves would liquidate the margin. Of the first 21 coins, **none** reach the target without liquidation risk. See `research/target_search.csv` when complete.

## Open items / next steps
1. Finish target search results and explain honestly (likely: target only reachable with leverage that has historically liquidated).
2. Offer: paper-trade the universal RSI rule on Hyperliquid prices for the 19 coins (fake money), saving to G:.
3. Keep the BNB meme bot paper run going; review recap.
4. Hyperliquid margin is USDC, not USDT; Hyperliquid connects to MetaMask directly (fits his DEX-only rule).
