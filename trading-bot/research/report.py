"""Turn backtest results into the final ranking (CSV + Markdown).

Only coins that passed the Binance-vs-Hyperliquid price check are ranked.
Each coin's chosen strategy is also re-run on Hyperliquid's own most recent
5m candles (~17 days) as a sanity check: too short to prove anything, but it
catches strategies that only "work" on Binance prices.

    python3 -m research.report --data-dir "/mnt/g/Adams Bot"
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import time

import numpy as np

from . import backtest as B
from .download import default_dir


def run_one(arrs: dict, family: str, params: str, side: str, tp: float, sl: float, hold: int) -> dict:
    """Simulate one strategy over the whole array (no in/out split)."""
    o, h, l, c, qv, t = arrs["o"], arrs["h"], arrs["l"], arrs["c"], arrs["qv"], arrs["t"]
    at = B.atr(h, l, c)
    sd = 1 if side == "long" else -1
    for fam, p, ls, ss in B.signals(o, h, l, c, qv, at):
        if fam == family and p == params:
            sig = ls if sd > 0 else ss
            break
    else:
        return {"n": 0}
    idx = np.flatnonzero(sig[:-1])
    idx = idx[idx >= min(B.TREND_EMA, len(c) // 4)]
    if not len(idx):
        return {"n": 0}
    eb = (idx + 1).astype(np.int64)
    tps, sls, holds = np.array([tp]), np.array([sl]), np.array([hold])
    tph, slh = B.first_hits(eb, sd, o, h, l, at[idx], tps, sls, hold)
    st = B.run_configs(eb, sd, o, c, t, at[idx], tph, slh, tps, sls, holds,
                       np.zeros(0, np.int64), np.zeros(1), len(c) + 1,
                       B.FEE_PER_SIDE + B.SLIP_PER_SIDE, B.MAKER_FEE)[0, 0, 0, 0]
    n = int(st[0])
    return {"n": n, "win": st[1] / n if n else 0.0, "ret": st[2]}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data-dir", default=default_dir())
    ap.add_argument("--top", type=int, default=20)
    args = ap.parse_args(argv)
    hist = os.path.join(args.data_dir, "history")
    rdir = os.path.join(args.data_dir, "research")
    with open(os.path.join(rdir, "results.json")) as f:
        res = json.load(f)
    with open(os.path.join(hist, "verified.json")) as f:
        ver = {v["symbol"]: v for v in json.load(f)}

    rows, rejected = [], []
    for r in res["results"]:
        v = ver.get(r["symbol"])
        if not v or not v["match"]:
            rejected.append((r["symbol"], (v or {}).get("why", "not verified")))
            continue
        b = r["best"]
        row = {"coin": v["coin"], "symbol": r["symbol"], "history": f"{r['first']} .. {r['last']}",
               "test_period": f"{r['split']} .. {r['last']}", "tested": r["strategies_tested"],
               "is_70pct": r["is_70pct_count"], "candidates": r["candidates"], "qualifies": r["qualifies"]}
        if b:
            row.update({
                "strategy": f"{b['family']} ({b['params']})", "side": b["side"],
                "take_profit_atr": b["tp_atr"], "stop_loss_atr": b["sl_atr"],
                "max_hold_min": int(b["hold_bars"]) * 5,
                "train_trades": int(b["is_n"]), "train_win": b["is_win"], "train_return_pct": b["is_ret"] * 100,
                "test_trades": int(b["oos_n"]), "test_win": b["oos_win"], "test_return_pct": b["oos_ret"] * 100,
                "test_profit_factor": b["oos_pf"], "test_max_drawdown_pct": b["oos_dd"] * 100,
            })
            hp = os.path.join(hist, "hyperliquid", f"{v['coin']}-5m.npz")
            if os.path.exists(hp):
                hl = run_one(dict(np.load(hp)), b["family"], b["params"], b["side"], b["tp_atr"], b["sl_atr"],
                             int(b["hold_bars"]))
                row.update({"hl_recent_trades": hl["n"], "hl_recent_win": hl.get("win"),
                            "hl_recent_return_pct": hl.get("ret", 0) * 100})
        rows.append(row)

    ranked = sorted([r for r in rows if r["qualifies"]], key=lambda r: (-r["test_return_pct"]))
    near = sorted([r for r in rows if not r["qualifies"] and r.get("strategy")],
                  key=lambda r: -(r.get("test_win") or 0))

    fields = sorted({k for r in rows for k in r}, key=lambda k: list(rows[0]).index(k) if k in rows[0] else 99)
    with open(os.path.join(rdir, "all_coins.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)

    def pct(x):
        return "–" if x is None else f"{x:.0%}"

    lines = [f"# 5-minute strategy study: {time.strftime('%Y-%m-%d')}", "",
             f"Coins studied: {len(rows)} (dropped {len(rejected)} whose Binance prices didn't match Hyperliquid). "
             f"Strategies tested per coin: ~{rows[0]['tested'] if rows else 0:,}.", "",
             "A coin qualifies only if its best strategy, chosen on the older 75% of its history, then won "
             f">= {B.MIN_WIN:.0%} of at least {B.MIN_OOS_TRADES} trades AND made money on the newest 25% it never saw "
             "(after fees, slippage and funding).", "",
             f"## Qualifying coins: {len(ranked)}", ""]
    hdr = "| # | Coin | Strategy | Side | TP/SL (ATR) | Max hold | Test trades | Test win | Test return | HL last ~17d |"
    lines += [hdr, "|" + "---|" * 10]
    for i, r in enumerate(ranked[:args.top], 1):
        hl = (f"{r.get('hl_recent_trades', 0)} trades, {pct(r.get('hl_recent_win'))}"
              if r.get("hl_recent_trades") else "no trades")
        lines.append(f"| {i} | {r['coin']} | {r['strategy']} | {r['side']} | {r['take_profit_atr']}/{r['stop_loss_atr']} | "
                     f"{r['max_hold_min']} min | {r['test_trades']} | {pct(r['test_win'])} | "
                     f"{r['test_return_pct']:+.1f}% | {hl} |")
    lines += ["", "## Closest that did not qualify", "", hdr, "|" + "---|" * 10]
    for i, r in enumerate(near[:15], 1):
        hl = (f"{r.get('hl_recent_trades', 0)} trades, {pct(r.get('hl_recent_win'))}"
              if r.get("hl_recent_trades") else "no trades")
        lines.append(f"| {i} | {r['coin']} | {r['strategy']} | {r['side']} | {r['take_profit_atr']}/{r['stop_loss_atr']} | "
                     f"{r['max_hold_min']} min | {r['test_trades']} | {pct(r['test_win'])} | "
                     f"{r['test_return_pct']:+.1f}% | {hl} |")
    no_cand = [r["coin"] for r in rows if not r.get("strategy")]
    lines += ["", f"Coins with no 70%-and-profitable strategy even in training: {', '.join(no_cand) or 'none'}", "",
              "Dropped (different asset under the same ticker): " +
              (", ".join(f"{s} ({w})" for s, w in rejected) or "none")]
    with open(os.path.join(rdir, "report.md"), "w") as f:
        f.write("\n".join(lines) + "\n")
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
