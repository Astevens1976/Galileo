"""CLI.

  python -m tradebot                                   # sim market + dashboard
  python -m tradebot --headless --hours 8 --seed 1     # fast-forward one simulated night
  python -m tradebot --mode paper                      # REAL market data, fake money
  python -m tradebot --selftest --network testnet      # real swaps on BNB testnet (free test coins)
  python -m tradebot --mode live --config config.json  # REAL money (read README first)
"""
from __future__ import annotations

import argparse
import os
import sys
import threading

from .config import load_config
from .dashboard import start_dashboard
from .engine import Engine, run_forever
from .feeds import DexScreenerFeed, SimFeed
from .journal import Journal

TESTNET = dict(rpc_url="https://data-seed-prebsc-1-s1.bnbchain.org:8545", expected_chain_id=97,
               router_address="0xD99D1c33F9fC3444f8101754aBC46c52416550D1",   # PancakeSwap V2 testnet
               quote_token_address="WNATIVE")

LIVE_DISABLED = """Live trading is off.

To turn it on, put  "live_enabled": true  in your config file and pass it with
--config. Read the "Going live" section of the README first: use a NEW wallet
account holding only money you can afford to lose."""


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="tradebot", description="DEX meme-coin trading bot")
    ap.add_argument("--config", help="JSON file overriding defaults (see config.example.json)")
    ap.add_argument("--mode", choices=["sim", "paper", "live"])
    ap.add_argument("--chain", choices=["bsc", "solana"], help="bsc (default) | solana (paper only)")
    ap.add_argument("--balance", type=float, dest="starting_balance_usdt")
    ap.add_argument("--seed", type=int)
    ap.add_argument("--port", type=int, dest="dashboard_port")
    ap.add_argument("--speed", type=float, dest="sim_speed", help="real seconds per sim tick")
    ap.add_argument("--headless", action="store_true", help="no dashboard")
    ap.add_argument("--hours", type=float, help="sim: market hours to run, then stop")
    ap.add_argument("--no-logs", action="store_true")
    ap.add_argument("--data-dir", default=os.environ.get("ADAMS_BOT_DIR"),
                    help='folder for logs, trades and live state, e.g. "/mnt/g/Adams Bot" '
                         "(default: $ADAMS_BOT_DIR, else this folder)")
    ap.add_argument("--env-file", default=".env", help="file holding BOT_PRIVATE_KEY")
    ap.add_argument("--yes", action="store_true", help="live: skip the typed confirmation")
    ap.add_argument("--selftest", action="store_true", help="buy and sell back a small amount on-chain")
    ap.add_argument("--network", choices=["testnet", "mainnet"], default="testnet")
    ap.add_argument("--amount", type=float, help="selftest amount in quote token")
    ap.add_argument("--token", help="selftest token address (default: auto)")
    args = ap.parse_args(argv)

    cfg = load_config(args.config, mode=args.mode, chain=args.chain,
                      starting_balance_usdt=args.starting_balance_usdt, seed=args.seed,
                      dashboard_port=args.dashboard_port, sim_speed=args.sim_speed)

    if args.data_dir:
        os.makedirs(args.data_dir, exist_ok=True)
        cfg.log_dir = os.path.join(args.data_dir, "logs")
        if not os.path.isabs(cfg.live_state_file):
            cfg.live_state_file = os.path.join(args.data_dir, cfg.live_state_file)
        print(f"data folder: {args.data_dir}")

    if args.selftest:
        from .evm import load_private_key
        from .selftest import run_selftest
        if args.network == "testnet":
            for k, v in TESTNET.items():
                setattr(cfg, k, v)
        return run_selftest(cfg, load_private_key(args.env_file), args.amount, args.token,
                            mainnet=args.network == "mainnet")

    broker = None
    if cfg.mode == "live":
        broker = _live_broker(cfg, args)
        if broker is None:
            return 2

    feed = SimFeed(cfg) if cfg.mode == "sim" else DexScreenerFeed(cfg)
    engine = Engine(cfg, feed, broker=broker, journal=Journal(None if args.no_logs else cfg.log_dir))
    money = "REAL" if cfg.mode == "live" else "fake"
    print(f"mode={cfg.mode} feed={feed.name} balance={engine.equity():,.2f} USDT ({money})")

    if cfg.mode == "sim" and args.headless:
        ticks = int((args.hours or 8) * 3600 / cfg.sim_tick_seconds)
        for _ in range(ticks):
            engine.tick()
        return _finish(engine)

    if not args.headless:
        url = start_dashboard(engine, cfg.dashboard_host, cfg.dashboard_port)
        print(f"dashboard: {url}")

    interval = cfg.sim_speed if cfg.mode == "sim" else cfg.poll_seconds
    stop = threading.Event()
    if args.hours and cfg.mode == "sim":
        limit = engine.started_at + args.hours * 3600
        threading.Thread(target=_stop_at, args=(engine, limit, stop), daemon=True).start()
    try:
        print(f"running - Ctrl+C to stop" + (f" · create a file named '{cfg.kill_switch_file}' to sell everything"
                                             if cfg.mode == "live" else ""))
        run_forever(engine, interval, stop)
    except KeyboardInterrupt:
        pass
    return _finish(engine)


def _live_broker(cfg, args):
    if not cfg.live_enabled:
        print(LIVE_DISABLED)
        return None
    if cfg.chain == "solana":
        print("Live execution supports EVM chains (MetaMask-compatible) only. Set chain to bsc.")
        return None
    from .broker import LiveBroker
    from .evm import DexClient, load_private_key
    client = DexClient.connect(cfg.rpc_url, load_private_key(args.env_file), cfg.router_address,
                               cfg.quote_token_address, cfg.expected_chain_id, cfg.max_gas_gwei)
    usdt, bnb = client.quote_balance(), client.native_balance()
    print(f"""
  LIVE TRADING - REAL MONEY
  wallet        {client.address}
  chain id      {cfg.expected_chain_id}  rpc {cfg.rpc_url}
  balances      {usdt:,.2f} USDT · {bnb:.4f} BNB (gas)
  per trade     max ${cfg.max_live_trade_usd:,.2f}
  per day       max ${cfg.max_live_daily_spend_usd:,.2f} of buys
  stop / trail  -{cfg.hard_stop_pct:g}% / -{cfg.trailing_stop_pct:g}% from peak
  kill switch   create a file named '{cfg.kill_switch_file}' in this folder
""")
    if bnb < cfg.min_gas_reserve_native:
        print(f"Wallet needs at least {cfg.min_gas_reserve_native} BNB for gas. Stopping.")
        return None
    if not args.yes and input("Type TRADE to start with real money: ").strip() != "TRADE":
        print("Not confirmed. Nothing was traded.")
        return None
    broker = LiveBroker(cfg, client)
    cfg.starting_balance_usdt = max(broker.equity({}), 0.01)   # drawdown limits use real balance
    return broker


def _stop_at(engine: Engine, limit: float, stop: threading.Event) -> None:
    while not stop.wait(0.5):
        if engine.feed.now() >= limit:
            stop.set()


def _finish(engine: Engine) -> int:
    text = engine.summary()
    print("\n" + text)
    path = engine.journal.write_summary(text)
    if path:
        print(f"logs: {engine.journal.dir}")
    if engine.broker.positions and getattr(engine.broker, "live", False):
        print(f"NOTE: {len(engine.broker.positions)} live position(s) still open in your wallet. "
              "They are saved and will be managed again when you restart.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
