"""Download 5-minute price history + funding rates for Hyperliquid coins.

Source: Binance USD-M futures public archive (data.binance.vision), quoted in
USDT, which has years of clean 5m history. Hyperliquid itself only serves the
last ~5000 candles, so it is used for the coin list and a recent cross-check.

    python3 -m research.download --data-dir "/mnt/g/Adams Bot" --top 60

Everything is saved under <data-dir>/history:
    hyperliquid_meta.json          coin list + 24h volume when downloaded
    universe.json                  coins chosen and their Binance symbols
    raw/<SYMBOL>/...zip            original monthly archive files (kept)
    <SYMBOL>.npz                   compact arrays used by the backtester
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import os
import time
import urllib.error
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor

import numpy as np

BASE = "https://data.binance.vision/data/futures/um/monthly"
HL = "https://api.hyperliquid.xyz/info"


def default_dir() -> str:
    return os.environ.get("ADAMS_BOT_DIR", os.path.expanduser("~/adams-bot-data"))


def hl_post(body: dict):
    req = urllib.request.Request(HL, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def fetch(url: str, dest: str | None = None, tries: int = 4) -> bytes | None:
    """GET with retries. None on 404. Caches to dest if given."""
    if dest and os.path.exists(dest) and os.path.getsize(dest) > 0:
        with open(dest, "rb") as f:
            return f.read()
    for i in range(tries):
        try:
            with urllib.request.urlopen(url, timeout=60) as r:
                data = r.read()
            if dest:
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                with open(dest + ".part", "wb") as f:
                    f.write(data)
                os.replace(dest + ".part", dest)
            return data
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            time.sleep(2 ** i)
        except (urllib.error.URLError, TimeoutError, OSError):
            time.sleep(2 ** i)
    raise RuntimeError(f"failed: {url}")


def binance_symbol(hl_name: str) -> str:
    # Hyperliquid prices some small coins per 1000 (kPEPE) like Binance's 1000PEPEUSDT
    if hl_name.startswith("k") and hl_name[1:].isupper():
        return "1000" + hl_name[1:] + "USDT"
    return hl_name + "USDT"


def months(start: str, end: str) -> list[str]:
    y, m = map(int, start.split("-"))
    ey, em = map(int, end.split("-"))
    out = []
    while (y, m) <= (ey, em):
        out.append(f"{y:04d}-{m:02d}")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def read_zip_csv(data: bytes) -> list[list[str]]:
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        with z.open(z.namelist()[0]) as f:
            rows = list(csv.reader(io.TextIOWrapper(f)))
    return [r for r in rows if r and r[0][:1].isdigit()]      # drop header if present


def pick_universe(top: int, probe_month: str, history: str) -> list[dict]:
    meta, ctxs = hl_post({"type": "metaAndAssetCtxs"})
    with open(os.path.join(history, "hyperliquid_meta.json"), "w") as f:
        json.dump({"downloaded_at": time.time(), "meta": meta, "ctxs": ctxs}, f)
    coins = [(u["name"], float(c.get("dayNtlVlm") or 0)) for u, c in zip(meta["universe"], ctxs)
             if not u.get("isDelisted")]
    coins.sort(key=lambda x: -x[1])

    def probe(item):
        name, vol = item
        sym = binance_symbol(name)
        url = f"{BASE}/klines/{sym}/5m/{sym}-5m-{probe_month}.zip"
        req = urllib.request.Request(url, method="HEAD")
        try:
            urllib.request.urlopen(req, timeout=30)
            return {"coin": name, "symbol": sym, "hl_day_volume": vol}
        except urllib.error.HTTPError:
            return None

    with ThreadPoolExecutor(8) as ex:
        found = [r for r in ex.map(probe, coins) if r]
    return found[:top]


def download_symbol(sym: str, month_list: list[str], history: str) -> dict:
    raw = os.path.join(history, "raw", sym)
    rows, funding = [], []
    for mo in month_list:
        k = fetch(f"{BASE}/klines/{sym}/5m/{sym}-5m-{mo}.zip", os.path.join(raw, f"{sym}-5m-{mo}.zip"))
        if k:
            rows += read_zip_csv(k)
        fr = fetch(f"{BASE}/fundingRate/{sym}/{sym}-fundingRate-{mo}.zip",
                   os.path.join(raw, f"{sym}-fundingRate-{mo}.zip"))
        if fr:
            funding += read_zip_csv(fr)
    if not rows:
        return {"symbol": sym, "bars": 0}
    a = np.array([[float(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[7])] for r in rows])
    a = a[np.argsort(a[:, 0], kind="stable")]
    a = a[np.concatenate([[True], np.diff(a[:, 0]) > 0])]          # drop duplicate timestamps
    f = np.array([[float(r[0]), float(r[2])] for r in funding]) if funding else np.zeros((0, 2))
    if len(f):
        f = f[np.argsort(f[:, 0])]
    np.savez_compressed(os.path.join(history, f"{sym}.npz"), t=a[:, 0].astype(np.int64), o=a[:, 1], h=a[:, 2],
                        l=a[:, 3], c=a[:, 4], qv=a[:, 5], ft=f[:, 0].astype(np.int64), fr=f[:, 1])
    first = time.strftime("%Y-%m-%d", time.gmtime(a[0, 0] / 1000))
    last = time.strftime("%Y-%m-%d", time.gmtime(a[-1, 0] / 1000))
    return {"symbol": sym, "bars": len(a), "first": first, "last": last, "funding_points": len(f)}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data-dir", default=default_dir())
    ap.add_argument("--top", type=int, default=60, help="most-traded Hyperliquid coins to keep")
    ap.add_argument("--start", default="2021-08", help="first month (63 months before Nov 2026)")
    ap.add_argument("--end", default="2026-10", help="last complete month")
    ap.add_argument("--workers", type=int, default=6)
    args = ap.parse_args(argv)

    history = os.path.join(args.data_dir, "history")
    os.makedirs(history, exist_ok=True)
    uni = pick_universe(args.top, args.end, history)
    print(f"{len(uni)} coins: {' '.join(u['coin'] for u in uni)}")
    month_list = months(args.start, args.end)
    done = 0

    def job(u):
        info = download_symbol(u["symbol"], month_list, history)
        return {**u, **info}

    results = []
    with ThreadPoolExecutor(args.workers) as ex:
        for r in ex.map(job, uni):
            done += 1
            results.append(r)
            print(f"[{done}/{len(uni)}] {r['coin']:>8} {r['symbol']:<14} {r['bars']:>7} bars "
                  f"{r.get('first', '-')} .. {r.get('last', '-')}", flush=True)
    with open(os.path.join(history, "universe.json"), "w") as f:
        json.dump(results, f, indent=2)
    print(f"saved to {history}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
