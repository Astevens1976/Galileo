"""CLI.

  python -m tradebot                                 # sim market + dashboard
  python -m tradebot --headless --hours 8 --seed 1   # fast-forward one night, print summary
  python -m tradebot --mode paper                    # REAL market data, fake money
"""
from __future__ import annotations

import argparse
import sys
import threading

from .config import load_config
from .dashboard import start_dashboard
from .engine import Engine, run_forever
from .feeds import DexScreenerFeed, SimFeed
from .journal import Journal

LIVE_LOCKED = """Live trading is locked in this version.

Run it with fake money first:
  --mode sim     synthetic market, fast-forward many nights to sanity-check rules
  --mode paper   real Solana market data, fake USDT balance, real-time

Real-money execution gets added only once paper mode has shown a positive
result over several weeks of real market data, net of fees and slippage."""


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="tradebot", description="Meme-coin paper-trading bot")
    ap.add_argument("--config", help="JSON file overriding defaults (see config.example.json)")
    ap.add_argument("--mode", choices=["sim", "paper", "live"])
    ap.add_argument("--balance", type=float, dest="starting_balance_usdt")
    ap.add_argument("--seed", type=int)
    ap.add_argument("--port", type=int, dest="dashboard_port")
    ap.add_argument("--speed", type=float, dest="sim_speed", help="real seconds per sim tick")
    ap.add_argument("--headless", action="store_true", help="no dashboard")
    ap.add_argument("--hours", type=float, help="sim: market hours to run, then stop")
    ap.add_argument("--no-logs", action="store_true")
    args = ap.parse_args(argv)

    cfg = load_config(args.config, mode=args.mode, starting_balance_usdt=args.starting_balance_usdt,
                      seed=args.seed, dashboard_port=args.dashboard_port, sim_speed=args.sim_speed)
    if cfg.mode == "live":
        print(LIVE_LOCKED)
        return 2

    feed = SimFeed(cfg) if cfg.mode == "sim" else DexScreenerFeed(cfg)
    engine = Engine(cfg, feed, journal=Journal(None if args.no_logs else cfg.log_dir))
    print(f"mode={cfg.mode} feed={feed.name} balance={cfg.starting_balance_usdt:,.2f} USDT (fake)")

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
        print("running - Ctrl+C to stop")
        run_forever(engine, interval, stop)
    except KeyboardInterrupt:
        pass
    return _finish(engine)


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
    return 0


if __name__ == "__main__":
    sys.exit(main())
