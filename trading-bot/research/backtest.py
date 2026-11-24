"""Brute-force strategy study on 5-minute candles, with honest out-of-sample scoring.

For every coin: 56 entry rules (12 families) x long/short x 126 exit settings
(take-profit, stop-loss, max hold) = ~14,000 strategies. Each is simulated one
position at a time, entering at the NEXT bar's open, paying fees, slippage and
funding. If a bar touches both the target and the stop, it counts as a loss.

The settings are CHOSEN on the first 75% of each coin's history (in-sample) and
JUDGED only on the last 25% (out-of-sample), which the choice never saw.

    python3 -m research.backtest --data-dir "/mnt/g/Adams Bot"
"""
from __future__ import annotations

import argparse
import json
import os
import time
from concurrent.futures import ProcessPoolExecutor

import numpy as np
from numba import njit

from .download import default_dir

# --- assumptions ------------------------------------------------------------
FEE_PER_SIDE = 0.00045      # Hyperliquid base taker fee 0.045% (entries, stops, time exits)
MAKER_FEE = 0.00015         # Hyperliquid base maker fee 0.015% (take-profit = resting limit order)
SLIP_PER_SIDE = 0.0002      # 0.02% slippage on every taker fill
TPS = np.array([0.5, 0.75, 1.0, 1.5, 2.0, 3.0])          # take-profit, x ATR(14)
SLS = np.array([0.75, 1.0, 1.5, 2.0, 3.0, 4.0, 6.0])     # stop-loss, x ATR(14)
HOLDS = np.array([12, 48, 144])                          # max hold: 1h, 4h, 12h (5m bars)
IS_FRACTION = 0.75
TREND_EMA = 576                                          # 2-day EMA as higher-timeframe trend filter

# selection rules (in-sample) and qualification rules (out-of-sample)
MIN_IS_TRADES = 60
MIN_OOS_TRADES = 30
MIN_WIN = 0.70


# --- indicators ---------------------------------------------------------------
@njit(cache=True)
def ema(x, n):
    out = np.empty_like(x)
    a = 2.0 / (n + 1)
    out[0] = x[0]
    for i in range(1, len(x)):
        out[i] = a * x[i] + (1 - a) * out[i - 1]
    return out


@njit(cache=True)
def rma(x, n):
    out = np.empty_like(x)
    out[0] = x[0]
    for i in range(1, len(x)):
        out[i] = (x[i] + (n - 1) * out[i - 1]) / n
    return out


def atr(h, l, c, n=14):
    pc = np.concatenate([[c[0]], c[:-1]])
    tr = np.maximum(h - l, np.maximum(np.abs(h - pc), np.abs(l - pc)))
    return rma(tr, n)


def rsi(c, n):
    d = np.diff(c, prepend=c[0])
    up = rma(np.maximum(d, 0.0), n)
    dn = rma(np.maximum(-d, 0.0), n)
    return 100 - 100 / (1 + up / np.maximum(dn, 1e-12))


def rolling(x, n, fn):
    from numpy.lib.stride_tricks import sliding_window_view
    out = np.full_like(x, np.nan)
    if len(x) >= n:
        out[n - 1:] = fn(sliding_window_view(x, n), axis=1)
    return out


def sma(x, n):
    cs = np.cumsum(np.insert(x, 0, 0.0))
    out = np.full_like(x, np.nan)
    out[n - 1:] = (cs[n:] - cs[:-n]) / n
    return out


def rstd(x, n):
    m = sma(x, n)
    m2 = sma(x * x, n)
    return np.sqrt(np.maximum(m2 - m * m, 0.0))


def cross_up(a, b):
    return np.concatenate([[False], (a[1:] > b[1:]) & (a[:-1] <= b[:-1])])


def cross_dn(a, b):
    return cross_up(b, a)


@njit(cache=True)
def supertrend_dir(h, l, c, at, m):
    n = len(c)
    d = np.ones(n)
    up = (h + l) / 2 - m * at
    dn = (h + l) / 2 + m * at
    fu, fd = up.copy(), dn.copy()
    for i in range(1, n):
        fu[i] = max(up[i], fu[i - 1]) if c[i - 1] > fu[i - 1] else up[i]
        fd[i] = min(dn[i], fd[i - 1]) if c[i - 1] < fd[i - 1] else dn[i]
        if d[i - 1] < 0 and c[i] > fd[i - 1]:
            d[i] = 1
        elif d[i - 1] > 0 and c[i] < fu[i - 1]:
            d[i] = -1
        else:
            d[i] = d[i - 1]
    return d


@njit(cache=True)
def streak(o, c):
    n = len(c)
    red = np.zeros(n)
    green = np.zeros(n)
    for i in range(n):
        if c[i] < o[i]:
            red[i] = (red[i - 1] if i else 0) + 1
        elif c[i] > o[i]:
            green[i] = (green[i - 1] if i else 0) + 1
    return red, green


def signals(o, h, l, c, qv, at):
    """Yield (family, params, long_signal, short_signal) boolean arrays (signal on bar close)."""
    trend = ema(c, TREND_EMA)
    up_trend, dn_trend = c > trend, c < trend
    for n in (7, 14):
        r = rsi(c, n)
        for lo in (20, 25, 30):
            yield "rsi_reversion", f"rsi{n}<{lo}/>{100 - lo}", r < lo, r > 100 - lo
            yield "rsi_reversion_trend", f"rsi{n}<{lo}/>{100 - lo} with 2d trend", (r < lo) & up_trend, (r > 100 - lo) & dn_trend
    m20, s20 = sma(c, 20), rstd(c, 20)
    for k in (2.0, 2.5, 3.0):
        yield "bollinger_reversion", f"bb20 k{k}", c < m20 - k * s20, c > m20 + k * s20
        yield "bollinger_reversion_trend", f"bb20 k{k} with 2d trend", (c < m20 - k * s20) & up_trend, (c > m20 + k * s20) & dn_trend
    for n in (48, 96, 288):
        m, s = sma(c, n), rstd(c, n)
        z = (c - m) / np.maximum(s, 1e-12)
        for zz in (2.0, 2.5, 3.0):
            yield "zscore_reversion", f"z{n} {zz}", z < -zz, z > zz
    ll = rolling(l, 14, np.min)
    hh = rolling(h, 14, np.max)
    k = 100 * (c - ll) / np.maximum(hh - ll, 1e-12)
    d = sma(np.nan_to_num(k, nan=50.0), 3)
    for lo in (10, 20):
        yield "stochastic_reversion", f"stoch14 {lo}/{100 - lo}", cross_up(k, d) & (k < lo), cross_dn(k, d) & (k > 100 - lo)
    red, green = streak(o, c)
    for n in (3, 4, 5, 6):
        yield "streak_reversal", f"{n} candles in a row", red == n, green == n
    for f, s in ((9, 21), (20, 50), (50, 200), (12, 96)):
        ef, es = ema(c, f), ema(c, s)
        yield "ema_cross", f"ema{f}/{s}", cross_up(ef, es), cross_dn(ef, es)
    macd = ema(c, 12) - ema(c, 26)
    sig = ema(macd, 9)
    yield "macd_cross", "12/26/9", cross_up(macd, sig), cross_dn(macd, sig)
    yield "macd_cross", "12/26/9 beyond zero line", cross_up(macd, sig) & (macd < 0), cross_dn(macd, sig) & (macd > 0)
    for n in (20, 55, 144, 288):
        ph = np.concatenate([[np.inf], rolling(h, n, np.max)[:-1]])
        pl = np.concatenate([[-np.inf], rolling(l, n, np.min)[:-1]])
        yield "donchian_breakout", f"{n} bars", cross_up(c, ph), cross_dn(c, pl)
    e20 = ema(c, 20)
    for kk in (1.5, 2.0, 2.5, 3.0):
        yield "keltner_breakout", f"ema20 +/-{kk}atr", cross_up(c, e20 + kk * at), cross_dn(c, e20 - kk * at)
    for m in (2.0, 3.0, 4.0):
        sd = supertrend_dir(h, l, c, at, m)
        flip_up = np.concatenate([[False], (sd[1:] > 0) & (sd[:-1] < 0)])
        flip_dn = np.concatenate([[False], (sd[1:] < 0) & (sd[:-1] > 0)])
        yield "supertrend", f"10 x{m}", flip_up, flip_dn
    vm = sma(qv, 48)
    for m in (3.0, 5.0, 8.0):
        spike = qv > m * np.nan_to_num(vm, nan=np.inf)
        yield "volume_spike_momentum", f"vol>{m}x avg", spike & (c > o), spike & (c < o)
        yield "volume_spike_fade", f"vol>{m}x avg", spike & (c < o), spike & (c > o)


# --- simulation kernels -------------------------------------------------------
@njit(cache=True)
def first_hits(entry_bar, side, o, h, l, at_sig, tps, sls, hmax):
    """For each entry, the bar offset at which each TP / SL level is first touched (-1 = never)."""
    E, N = len(entry_bar), len(o)
    tp_hit = np.full((E, len(tps)), -1, np.int32)
    sl_hit = np.full((E, len(sls)), -1, np.int32)
    for i in range(E):
        e = entry_bar[i]
        ep = o[e]
        a = at_sig[i]
        for k in range(hmax):
            j = e + k
            if j >= N:
                break
            for t in range(len(tps)):
                if tp_hit[i, t] < 0:
                    tgt = ep + side * tps[t] * a
                    if (side > 0 and h[j] >= tgt) or (side < 0 and l[j] <= tgt):
                        tp_hit[i, t] = k
            for s in range(len(sls)):
                if sl_hit[i, s] < 0:
                    stp = ep - side * sls[s] * a
                    if (side > 0 and l[j] <= stp) or (side < 0 and h[j] >= stp):
                        sl_hit[i, s] = k
    return tp_hit, sl_hit


@njit(cache=True)
def run_configs(entry_bar, side, o, c, t, at_sig, tp_hit, sl_hit, tps, sls, holds,
                ft, fcum, split_bar, taker_cost, maker_cost):
    """Simulate every (tp, sl, hold) one trade at a time. Stats per [tp, sl, hold, segment]:
    0 trades, 1 wins, 2 sum return, 3 gross win, 4 gross loss, 5 max drawdown (sum of returns)."""
    N = len(o)
    out = np.zeros((len(tps), len(sls), len(holds), 2, 6))
    for ti in range(len(tps)):
        for si in range(len(sls)):
            for hi in range(len(holds)):
                H = holds[hi]
                free_from = 0
                eq0 = 0.0
                eq1 = 0.0
                pk0 = 0.0
                pk1 = 0.0
                for i in range(len(entry_bar)):
                    e = entry_bar[i]
                    if e - 1 < free_from or e >= N:
                        continue
                    ep = o[e]
                    a = at_sig[i]
                    tk = tp_hit[i, ti]
                    sk = sl_hit[i, si]
                    if tk >= H:
                        tk = -1
                    if sk >= H:
                        sk = -1
                    exit_cost = taker_cost
                    if sk >= 0 and (tk < 0 or sk <= tk):          # stop first (ties count as stop)
                        off = sk
                        stp = ep - side * sls[si] * a
                        px = min(o[e + off], stp) if side > 0 else max(o[e + off], stp)
                    elif tk >= 0:
                        off = tk
                        px = ep + side * tps[ti] * a
                        exit_cost = maker_cost
                    else:
                        off = min(H - 1, N - 1 - e)
                        px = c[e + off]
                    x = e + off
                    r = side * (px / ep - 1.0) - taker_cost - exit_cost
                    # funding paid by longs when the rate is positive, received by shorts
                    i0 = np.searchsorted(ft, t[e], side="right")
                    i1 = np.searchsorted(ft, t[x], side="right")
                    if i1 > i0:
                        r -= side * (fcum[i1] - fcum[i0])
                    seg = 0 if e < split_bar else 1
                    st = out[ti, si, hi, seg]
                    st[0] += 1
                    st[2] += r
                    if r > 0:
                        st[1] += 1
                        st[3] += r
                    else:
                        st[4] -= r
                    if seg == 0:
                        eq0 += r
                        pk0 = max(pk0, eq0)
                        st[5] = max(st[5], pk0 - eq0)
                    else:
                        eq1 += r
                        pk1 = max(pk1, eq1)
                        st[5] = max(st[5], pk1 - eq1)
                    free_from = x + 1
    return out


# --- per-coin study -----------------------------------------------------------
def study_symbol(path: str) -> dict:
    z = np.load(path)
    t, o, h, l, c, qv = z["t"], z["o"], z["h"], z["l"], z["c"], z["qv"]
    ft, fr = z["ft"], z["fr"]
    fcum = np.concatenate([[0.0], np.cumsum(fr)])
    sym = os.path.basename(path)[:-4]
    N = len(c)
    at = atr(h, l, c)
    split_bar = int(N * IS_FRACTION)
    taker_cost = FEE_PER_SIDE + SLIP_PER_SIDE
    warm = TREND_EMA * 2
    rows = []
    for fam, params, long_sig, short_sig in signals(o, h, l, c, qv, at):
        for side, sig in ((1, long_sig), (-1, short_sig)):
            idx = np.flatnonzero(sig[:-1])
            idx = idx[idx >= warm]
            if len(idx) < MIN_IS_TRADES:
                continue
            entry_bar = (idx + 1).astype(np.int64)
            at_sig = at[idx]
            tp_hit, sl_hit = first_hits(entry_bar, side, o, h, l, at_sig, TPS, SLS, int(HOLDS.max()))
            stats = run_configs(entry_bar, side, o, c, t, at_sig, tp_hit, sl_hit, TPS, SLS, HOLDS,
                                ft, fcum, split_bar, taker_cost, MAKER_FEE)
            for ti in range(len(TPS)):
                for si in range(len(SLS)):
                    for hi in range(len(HOLDS)):
                        s0, s1 = stats[ti, si, hi]
                        rows.append((fam, params, "long" if side > 0 else "short", TPS[ti], SLS[si],
                                     int(HOLDS[hi]), *s0, *s1))
    cols = ["family", "params", "side", "tp_atr", "sl_atr", "hold_bars",
            "is_n", "is_wins", "is_ret", "is_gw", "is_gl", "is_dd",
            "oos_n", "oos_wins", "oos_ret", "oos_gw", "oos_gl", "oos_dd"]
    first = time.strftime("%Y-%m-%d", time.gmtime(t[0] / 1000))
    split = time.strftime("%Y-%m-%d", time.gmtime(t[split_bar] / 1000))
    last = time.strftime("%Y-%m-%d", time.gmtime(t[-1] / 1000))
    return {"symbol": sym, "bars": N, "first": first, "split": split, "last": last, "cols": cols, "rows": rows}


def summarize(res: dict) -> dict:
    """Pick the best in-sample strategy that meets the bar, then report how it did out-of-sample."""
    import pandas as pd
    df = pd.DataFrame(res["rows"], columns=res["cols"])
    df["is_win"] = df.is_wins / df.is_n.clip(lower=1)
    df["oos_win"] = df.oos_wins / df.oos_n.clip(lower=1)
    df["is_pf"] = df.is_gw / df.is_gl.replace(0, np.nan)
    df["oos_pf"] = df.oos_gw / df.oos_gl.replace(0, np.nan)
    df["is_avg"] = df.is_ret / df.is_n.clip(lower=1)
    df["oos_avg"] = df.oos_ret / df.oos_n.clip(lower=1)
    out = {k: res[k] for k in ("symbol", "bars", "first", "split", "last")}
    out["strategies_tested"] = len(df)
    out["is_70pct_count"] = int(((df.is_win >= MIN_WIN) & (df.is_n >= MIN_IS_TRADES)).sum())
    out["is_70pct_profitable_count"] = int(((df.is_win >= MIN_WIN) & (df.is_n >= MIN_IS_TRADES) & (df.is_ret > 0)).sum())
    cand = df[(df.is_n >= MIN_IS_TRADES) & (df.is_win >= MIN_WIN) & (df.is_ret > 0) & (df.is_pf > 1.1)]
    out["candidates"] = len(cand)
    if len(cand):
        # robust in-sample score: average edge x sqrt(trades) (favours many trades with a real edge)
        best = cand.assign(score=cand.is_avg * np.sqrt(cand.is_n)).sort_values("score", ascending=False).iloc[0]
        out["best"] = {k: (v.item() if hasattr(v, "item") else v) for k, v in best.items()}
        b = out["best"]
        out["qualifies"] = bool(b["oos_n"] >= MIN_OOS_TRADES and b["oos_win"] >= MIN_WIN and b["oos_ret"] > 0)
    else:
        out["best"] = None
        out["qualifies"] = False
    # also keep the top 25 in-sample candidates for the report
    top = cand.assign(score=cand.is_avg * np.sqrt(cand.is_n)).sort_values("score", ascending=False).head(25)
    out["top_candidates"] = json.loads(top.to_json(orient="records"))
    return out


def _work(path):
    t0 = time.time()
    r = summarize(study_symbol(path))
    r["seconds"] = round(time.time() - t0, 1)
    return r


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data-dir", default=default_dir())
    ap.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) - 0))
    ap.add_argument("--only", nargs="*", help="symbols to run (default: all downloaded)")
    args = ap.parse_args(argv)
    hist = os.path.join(args.data_dir, "history")
    files = sorted(f for f in os.listdir(hist) if f.endswith(".npz"))
    if args.only:
        files = [f for f in files if f[:-4] in args.only]
    outdir = os.path.join(args.data_dir, "research")
    os.makedirs(outdir, exist_ok=True)
    results = []
    with ProcessPoolExecutor(args.workers) as ex:
        for r in ex.map(_work, [os.path.join(hist, f) for f in files]):
            b = r["best"]
            msg = (f"{r['symbol']:<14} {r['first']}..{r['last']} tested {r['strategies_tested']:>6} "
                   f"70%-in-sample {r['is_70pct_count']:>5} candidates {r['candidates']:>4} ")
            if b:
                msg += (f"best {b['family']} {b['params']} {b['side']} tp{b['tp_atr']} sl{b['sl_atr']} h{b['hold_bars']} | "
                        f"IS win {b['is_win']:.0%} n{int(b['is_n'])} | OOS win {b['oos_win']:.0%} n{int(b['oos_n'])} "
                        f"ret {b['oos_ret'] * 100:+.1f}% {'QUALIFIES' if r['qualifies'] else ''}")
            print(msg + f" ({r['seconds']}s)", flush=True)
            results.append(r)
    path = os.path.join(outdir, "results.json")
    with open(path, "w") as f:
        json.dump({"assumptions": {"taker_fee": FEE_PER_SIDE, "maker_fee_take_profit": MAKER_FEE,
                                   "slippage_per_taker_fill": SLIP_PER_SIDE,
                                   "tp_atr": TPS.tolist(), "sl_atr": SLS.tolist(), "hold_bars": HOLDS.tolist(),
                                   "in_sample_fraction": IS_FRACTION, "min_win": MIN_WIN,
                                   "min_is_trades": MIN_IS_TRADES, "min_oos_trades": MIN_OOS_TRADES},
                   "results": results}, f)
    print(f"saved {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
