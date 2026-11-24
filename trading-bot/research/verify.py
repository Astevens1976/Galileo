"""Check every downloaded coin really is the same asset on Binance and Hyperliquid,
and save Hyperliquid's own recent 5m candles (the last ~5000) for a live cross-check.

Two different projects can share a ticker (e.g. Binance LITUSDT is not
Hyperliquid's LIT). A coin passes if its Binance and Hyperliquid daily closes
over the last month agree within 3% on median.

    python3 -m research.verify --data-dir "/mnt/g/Adams Bot"
"""
from __future__ import annotations

import argparse
import json
import os
import time

import numpy as np

from .download import default_dir, hl_post


def hl_candles(coin: str, interval: str, start_ms: int, end_ms: int) -> np.ndarray:
    rows = hl_post({"type": "candleSnapshot",
                    "req": {"coin": coin, "interval": interval, "startTime": start_ms, "endTime": end_ms}})
    a = np.array([[r["t"], float(r["o"]), float(r["h"]), float(r["l"]), float(r["c"]), float(r["v"]) * float(r["c"])]
                  for r in rows]) if rows else np.zeros((0, 6))
    return a


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data-dir", default=default_dir())
    args = ap.parse_args(argv)
    hist = os.path.join(args.data_dir, "history")
    hl_dir = os.path.join(hist, "hyperliquid")
    os.makedirs(hl_dir, exist_ok=True)
    with open(os.path.join(hist, "universe.json")) as f:
        uni = json.load(f)
    now = int(time.time() * 1000)
    out = []
    for u in uni:
        coin, sym = u["coin"], u["symbol"]
        path = os.path.join(hist, f"{sym}.npz")
        if not os.path.exists(path):
            continue
        z = np.load(path)
        t, c = z["t"], z["c"]
        # Binance daily closes (from 5m) for the last 30 days of the archive
        end = int(t[-1])
        start = end - 30 * 86400_000
        day = (t // 86400_000)
        mask = t >= start
        b_days, idx = np.unique(day[mask], return_index=True)
        b_close = {int(d): c[mask][np.flatnonzero(day[mask] == d)[-1]] for d in b_days}
        try:
            hl = hl_candles(coin, "1d", start, end + 86400_000)
            time.sleep(0.3)
        except Exception as e:      # noqa: BLE001
            out.append({**u, "match": False, "why": f"hyperliquid error: {e}"})
            continue
        ratios = [hl[i, 4] / b_close[int(hl[i, 0] // 86400_000)] for i in range(len(hl))
                  if int(hl[i, 0] // 86400_000) in b_close]
        med = float(np.median(ratios)) if ratios else float("nan")
        ok = bool(ratios) and abs(med - 1) < 0.03
        rec = {**u, "price_ratio_hl_vs_binance": med, "days_compared": len(ratios), "match": ok}
        if ok:
            recent = hl_candles(coin, "5m", now - 5000 * 300_000, now)
            np.savez_compressed(os.path.join(hl_dir, f"{coin}-5m.npz"), t=recent[:, 0].astype(np.int64),
                                o=recent[:, 1], h=recent[:, 2], l=recent[:, 3], c=recent[:, 4], qv=recent[:, 5])
            rec["hl_recent_bars"] = len(recent)
            time.sleep(0.3)
        else:
            rec["why"] = f"prices differ (Hyperliquid/Binance = {med:.3f})" if ratios else "no overlapping days"
        out.append(rec)
        print(f"{coin:>9} {sym:<14} ratio {med:8.4f} over {len(ratios):>2} days  {'OK' if ok else 'MISMATCH: ' + rec['why']}",
              flush=True)
    with open(os.path.join(hist, "verified.json"), "w") as f:
        json.dump(out, f, indent=2)
    good = [r["coin"] for r in out if r["match"]]
    print(f"{len(good)}/{len(out)} coins verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
