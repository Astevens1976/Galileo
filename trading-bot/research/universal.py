"""One rule for every coin, plus a random-entry baseline.

Per-coin "best strategy" picking turned out to be mostly luck (it qualified LESS
often out-of-sample than a random 70% candidate). A single fixed rule applied to
every coin has no per-coin tuning to overfit, so it is the more honest test.

The baseline enters at random bars (same number of trades, same exits). If the
rule doesn't beat random entries, its profit is just the market drifting.

    python3 -m research.universal --data-dir "/mnt/g/Adams Bot"
"""
from __future__ import annotations

import argparse
import csv
import json
import os
from concurrent.futures import ProcessPoolExecutor

import numpy as np

from . import backtest as B
from .download import default_dir

RULE = {"family": "rsi_reversion", "params": "rsi14<20/>80", "side": "long", "tp": 2.0, "sl": 6.0, "hold": 144}


def simulate(entry_idx, side, z, at, tp, sl, hold, split_bar):
    o, h, l, c, t = z["o"], z["h"], z["l"], z["c"], z["t"]
    eb = (entry_idx + 1).astype(np.int64)
    tps, sls, holds = np.array([tp]), np.array([sl]), np.array([hold])
    tph, slh = B.first_hits(eb, side, o, h, l, at[entry_idx], tps, sls, hold)
    fcum = np.concatenate([[0.0], np.cumsum(z["fr"])])
    return B.run_configs(eb, side, o, c, t, at[entry_idx], tph, slh, tps, sls, holds, z["ft"], fcum, split_bar,
                         B.FEE_PER_SIDE + B.SLIP_PER_SIDE, B.MAKER_FEE)[0, 0, 0]


def coin(path: str, rule: dict, n_random: int = 20) -> dict:
    z = B.load_clean(path)
    o, h, l, c, qv, t = z["o"], z["h"], z["l"], z["c"], z["qv"], z["t"]
    at = B.atr(h, l, c)
    side = 1 if rule["side"] == "long" else -1
    for fam, p, ls, ss in B.signals(o, h, l, c, qv, at):
        if fam == rule["family"] and p == rule["params"]:
            sig = ls if side > 0 else ss
            break
    idx = np.flatnonzero(sig[:-1])
    idx = idx[idx >= B.TREND_EMA * 2]
    split = int(len(c) * B.IS_FRACTION)
    st = simulate(idx, side, z, at, rule["tp"], rule["sl"], rule["hold"], split)
    # random-entry baseline: same number of signals, random bars, same exits, averaged over n_random runs
    rng = np.random.default_rng(7)
    lo = B.TREND_EMA * 2
    base = np.zeros((2, 6))
    for _ in range(n_random):
        ridx = np.sort(rng.choice(np.arange(lo, len(c) - 1), size=min(len(idx), len(c) - 1 - lo), replace=False))
        base += simulate(ridx, side, z, at, rule["tp"], rule["sl"], rule["hold"], split)
    base /= n_random
    sym = os.path.basename(path)[:-4]
    fmt = lambda s: {"trades": int(s[0]), "win": s[1] / s[0] if s[0] else None, "return_pct": s[2] * 100,
                     "max_drawdown_pct": s[5] * 100}
    return {"symbol": sym, "first": str(np.datetime64(int(t[0]), "ms"))[:10],
            "split": str(np.datetime64(int(t[split]), "ms"))[:10],
            "train": fmt(st[0]), "test": fmt(st[1]), "random_train": fmt(base[0]), "random_test": fmt(base[1])}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data-dir", default=default_dir())
    ap.add_argument("--top", type=int, default=20)
    args = ap.parse_args(argv)
    hist = os.path.join(args.data_dir, "history")
    with open(os.path.join(hist, "universe.json")) as f:
        names = {u["symbol"]: u["coin"] for u in json.load(f)}
    files = sorted(os.path.join(hist, x) for x in os.listdir(hist) if x.endswith(".npz"))
    with ProcessPoolExecutor() as ex:
        rows = list(ex.map(coin, files, [RULE] * len(files)))
    for r in rows:
        r["coin"] = names.get(r["symbol"], r["symbol"])

    def agg(key):
        n = sum(r[key]["trades"] for r in rows)
        w = sum((r[key]["win"] or 0) * r[key]["trades"] for r in rows)
        ret = sum(r[key]["return_pct"] for r in rows)
        prof = sum(r[key]["return_pct"] > 0 for r in rows if r[key]["trades"] >= B.MIN_OOS_TRADES)
        return n, w / max(n, 1), ret / len(rows), prof

    lines = [f"Rule: {RULE}"]
    for key in ("train", "test", "random_train", "random_test"):
        n, w, avg, prof = agg(key)
        lines.append(f"{key:<13} trades {n:>6}  win {w:.1%}  avg return per coin {avg:+.1f}%  profitable coins {prof}")
    q = [r for r in rows if r["test"]["trades"] >= B.MIN_OOS_TRADES and (r["test"]["win"] or 0) >= B.MIN_WIN
         and r["test"]["return_pct"] > 0 and r["train"]["return_pct"] > 0]
    q.sort(key=lambda r: -r["test"]["return_pct"])
    lines.append(f"\nCoins where the rule won >=70% AND made money in BOTH training and test: {len(q)}")
    for i, r in enumerate(q[:args.top], 1):
        lines.append(f"{i:>2}. {r['coin']:<9} test {r['test']['trades']:>4} trades win {r['test']['win']:.0%} "
                     f"ret {r['test']['return_pct']:+6.1f}% dd {r['test']['max_drawdown_pct']:5.1f}% | "
                     f"train win {r['train']['win']:.0%} ret {r['train']['return_pct']:+6.1f}% | "
                     f"random-entry test ret {r['random_test']['return_pct']:+6.1f}%")
    out = os.path.join(args.data_dir, "research")
    os.makedirs(out, exist_ok=True)
    with open(os.path.join(out, "universal_rule.json"), "w") as f:
        json.dump({"rule": RULE, "rows": rows}, f, indent=2)
    with open(os.path.join(out, "universal_rule.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["coin", "symbol", "history_from", "test_from", "train_trades", "train_win", "train_return_pct",
                    "test_trades", "test_win", "test_return_pct", "test_max_drawdown_pct",
                    "random_entry_test_return_pct"])
        for r in sorted(rows, key=lambda r: -r["test"]["return_pct"]):
            w.writerow([r["coin"], r["symbol"], r["first"], r["split"], r["train"]["trades"], r["train"]["win"],
                        round(r["train"]["return_pct"], 2), r["test"]["trades"], r["test"]["win"],
                        round(r["test"]["return_pct"], 2), round(r["test"]["max_drawdown_pct"], 2),
                        round(r["random_test"]["return_pct"], 2)])
    with open(os.path.join(out, "universal_rule.md"), "w") as f:
        f.write("\n".join(lines) + "\n")
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
