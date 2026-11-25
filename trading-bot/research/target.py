"""Search for strategies that hit a specific account target over 20-trade runs.

Target (from Adam): >= 75% of trades win, and the account grows >= 50% within
20 trades, putting 30% of the account up as margin on each trade.

How it's measured, per coin and strategy:
  1. Wider exit grid than the main study (targets up to 8 ATR, stops up to
     12 ATR, holds up to 3 days), long and short, all 12 rule families.
  2. Keep strategies that win >= 75% in BOTH the training (older 75%) and the
     test (newest 25%) periods and make money in both.
  3. For each, take its real trade sequence and slide a 20-trade window across
     the TEST period. Account change per trade = 30% x leverage x trade return.
  4. Find the smallest leverage (up to Hyperliquid's max for that coin) at which
     the MEDIAN 20-trade window reaches +50%, then check that no stop in the
     whole history would have wiped out the margin at that leverage.

    python3 -m research.target --data-dir "/mnt/g/Adams Bot"
"""
from __future__ import annotations

import argparse
import csv
import json
import os
from concurrent.futures import ProcessPoolExecutor

import numpy as np
import pandas as pd
from numba import njit

from . import backtest as B
from .download import default_dir

# wider grid than the main study
B.TPS = np.array([1.0, 1.5, 2.0, 3.0, 4.0, 6.0, 8.0])
B.SLS = np.array([2.0, 3.0, 4.0, 6.0, 8.0, 12.0])
B.HOLDS = np.array([48, 144, 288, 864])          # 4h, 12h, 24h, 3 days

MARGIN = 0.30
WINDOW = 20
TARGET_WIN = 0.75
TARGET_GROWTH = 0.50
LIQUIDATION_LOSS = 0.90       # a move against you of 90% of margin = assume liquidated
LEVERAGES = [1, 2, 3, 5, 10, 15, 20, 25, 40, 50]


@njit(cache=True)
def trade_sequence(entry_bar, side, o, c, t, at_sig, tp_hit, sl_hit, tp, sl, H, ft, fcum, taker, maker):
    """Per-trade net returns for ONE exit setting, one position at a time.
    Returns (entry bar, net return, stop distance as a fraction of entry price)."""
    N = len(o)
    eb = np.empty(len(entry_bar), np.int64)
    rr = np.empty(len(entry_bar))
    sd = np.empty(len(entry_bar))
    k = 0
    free_from = 0
    for i in range(len(entry_bar)):
        e = entry_bar[i]
        if e - 1 < free_from or e >= N:
            continue
        ep = o[e]
        a = at_sig[i]
        tk = tp_hit[i]
        sk = sl_hit[i]
        if tk >= H:
            tk = -1
        if sk >= H:
            sk = -1
        exit_cost = taker
        if sk >= 0 and (tk < 0 or sk <= tk):
            off = sk
            stp = ep - side * sl * a
            px = min(o[e + off], stp) if side > 0 else max(o[e + off], stp)
        elif tk >= 0:
            off = tk
            px = ep + side * tp * a
            exit_cost = maker
        else:
            off = min(H - 1, N - 1 - e)
            px = c[e + off]
        x = e + off
        r = side * (px / ep - 1.0) - taker - exit_cost
        i0 = np.searchsorted(ft, t[e], side="right")
        i1 = np.searchsorted(ft, t[x], side="right")
        if i1 > i0:
            r -= side * (fcum[i1] - fcum[i0])
        eb[k] = e
        rr[k] = r
        # worst price move against the position (gap through the stop included)
        sd[k] = max(sl * a / ep, -side * (px / ep - 1.0))
        k += 1
        free_from = x + 1
    return eb[:k], rr[:k], sd[:k]


def windows(r: np.ndarray, lev: float) -> tuple[np.ndarray, np.ndarray]:
    """Growth and win rate of every run of WINDOW consecutive trades."""
    acct = np.maximum(MARGIN * lev * r, -MARGIN)          # can't lose more than the margin
    if len(r) < WINDOW:
        return np.zeros(0), np.zeros(0)
    lg = np.log1p(acct)
    cs = np.concatenate([[0.0], np.cumsum(lg)])
    growth = np.expm1(cs[WINDOW:] - cs[:-WINDOW])
    w = np.concatenate([[0], np.cumsum(r > 0)])
    win = (w[WINDOW:] - w[:-WINDOW]) / WINDOW
    return growth, win


def analyse(path: str, max_lev: int) -> dict:
    res = B.study_symbol(path)
    df = pd.DataFrame(res["rows"], columns=res["cols"])
    df["is_win"] = df.is_wins / df.is_n.clip(lower=1)
    df["oos_win"] = df.oos_wins / df.oos_n.clip(lower=1)
    cand = df[(df.is_n >= 40) & (df.oos_n >= WINDOW) & (df.is_win >= TARGET_WIN) & (df.oos_win >= TARGET_WIN)
              & (df.is_ret > 0) & (df.oos_ret > 0)].copy()
    cand["oos_avg"] = cand.oos_ret / cand.oos_n
    cand = cand.sort_values("oos_avg", ascending=False).head(40)
    out = {"symbol": res["symbol"], "max_leverage": max_lev, "tested": len(df),
           "win75_both_periods": int(len(cand)), "results": []}
    if not len(cand):
        return out
    z = B.load_clean(path)
    o, h, l, c, qv, t = z["o"], z["h"], z["l"], z["c"], z["qv"], z["t"]
    at = B.atr(h, l, c)
    fcum = np.concatenate([[0.0], np.cumsum(z["fr"])])
    split_bar = int(len(c) * B.IS_FRACTION)
    sigs = {(f, p): (ls, ss) for f, p, ls, ss in B.signals(o, h, l, c, qv, at)}
    for _, row in cand.iterrows():
        side = 1 if row.side == "long" else -1
        sig = sigs[(row.family, row.params)][0 if side > 0 else 1]
        idx = np.flatnonzero(sig[:-1])
        idx = idx[idx >= B.TREND_EMA * 2]
        ebar = (idx + 1).astype(np.int64)
        tph, slh = B.first_hits(ebar, side, o, h, l, at[idx], np.array([row.tp_atr]), np.array([row.sl_atr]),
                                int(row.hold_bars))
        eb, r, sd = trade_sequence(ebar, side, o, c, t, at[idx], tph[:, 0], slh[:, 0], row.tp_atr, row.sl_atr,
                                   int(row.hold_bars), z["ft"], fcum, B.FEE_PER_SIDE + B.SLIP_PER_SIDE, B.MAKER_FEE)
        test = r[eb >= split_bar]
        worst_move = float(sd.max()) if len(sd) else 0.0
        best = None
        for lev in [x for x in LEVERAGES if x <= max_lev]:
            g, w = windows(test, lev)
            if not len(g):
                break
            med = float(np.median(g))
            if med >= TARGET_GROWTH:
                hit = (g >= TARGET_GROWTH) & (w >= TARGET_WIN)
                best = {"leverage": lev, "median_20trade_growth_pct": med * 100,
                        "share_of_windows_hitting_target": float(hit.mean()),
                        "worst_20trade_growth_pct": float(g.min()) * 100,
                        "liquidation_risk": bool(lev * worst_move >= LIQUIDATION_LOSS),
                        "worst_move_against_pct": worst_move * 100}
                break
        g1, w1 = windows(test, 1)
        out["results"].append({
            "strategy": f"{row.family} ({row.params})", "side": row.side, "tp_atr": float(row.tp_atr),
            "sl_atr": float(row.sl_atr), "max_hold_hours": int(row.hold_bars) * 5 / 60,
            "train_trades": int(row.is_n), "train_win": float(row.is_win),
            "test_trades": int(len(test)), "test_win": float((test > 0).mean()) if len(test) else 0.0,
            "avg_trade_pct": float(test.mean() * 100) if len(test) else 0.0,
            "median_20trade_growth_at_1x_pct": float(np.median(g1) * 100) if len(g1) else None,
            "needed": best})
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data-dir", default=default_dir())
    ap.add_argument("--workers", type=int, default=os.cpu_count() or 2)
    args = ap.parse_args(argv)
    hist = os.path.join(args.data_dir, "history")
    with open(os.path.join(hist, "hyperliquid_meta.json")) as f:
        maxlev = {u["name"]: int(u.get("maxLeverage", 1)) for u in json.load(f)["meta"]["universe"]}
    with open(os.path.join(hist, "universe.json")) as f:
        uni = {u["symbol"]: u["coin"] for u in json.load(f)}
    files = sorted(x for x in os.listdir(hist) if x.endswith(".npz"))
    paths = [os.path.join(hist, x) for x in files]
    levs = [maxlev.get(uni.get(x[:-4], ""), 1) for x in files]
    rows = []
    with ProcessPoolExecutor(args.workers) as ex:
        for res in ex.map(analyse, paths, levs):
            res["coin"] = uni.get(res["symbol"], res["symbol"])
            rows.append(res)
            ok = [r for r in res["results"] if r["needed"] and not r["needed"]["liquidation_risk"]]
            print(f"{res['coin']:>9} max {res['max_leverage']:>2}x | 75%-win in both periods: {res['win75_both_periods']:>3} "
                  f"| reach +50%/20 trades without liquidation risk: {len(ok)}", flush=True)
    out = os.path.join(args.data_dir, "research")
    os.makedirs(out, exist_ok=True)
    with open(os.path.join(out, "target_search.json"), "w") as f:
        json.dump(rows, f, indent=2)
    flat = []
    for res in rows:
        for r in res["results"]:
            n = r["needed"] or {}
            flat.append({"coin": res["coin"], "max_leverage": res["max_leverage"], **{k: v for k, v in r.items() if k != "needed"},
                         **{f"needed_{k}": v for k, v in n.items()}})
    if flat:
        with open(os.path.join(out, "target_search.csv"), "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(flat[0].keys()) + [k for k in
                               ["needed_leverage", "needed_median_20trade_growth_pct", "needed_share_of_windows_hitting_target",
                                "needed_worst_20trade_growth_pct", "needed_liquidation_risk", "needed_worst_move_against_pct"]
                               if k not in flat[0]])
            w.writeheader()
            w.writerows(flat)
    print(f"saved {out}/target_search.json and .csv")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
