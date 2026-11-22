from __future__ import annotations

import csv
import json
import os
import time

from .models import Fill


class Journal:
    """Append-only session logs: events.jsonl (everything) and trades.csv (fills)."""

    def __init__(self, log_dir: str | None):
        self.lines = 0
        self.dir = None
        if log_dir:
            self.dir = os.path.join(log_dir, time.strftime("session-%Y%m%d-%H%M%S"))
            os.makedirs(self.dir, exist_ok=True)
            with open(self._p("trades.csv"), "w", newline="") as f:
                csv.writer(f).writerow(["ts", "side", "symbol", "address", "qty", "price",
                                        "usd", "fee_usd", "impact_pct", "closed", "pnl_usd",
                                        "multiple", "reason"])

    def _p(self, name: str) -> str:
        return os.path.join(self.dir, name)

    def event(self, ev: dict) -> None:
        self.lines += 1
        if self.dir:
            with open(self._p("events.jsonl"), "a") as f:
                f.write(json.dumps(ev) + "\n")

    def fill(self, fill: Fill, reason: str) -> None:
        if self.dir:
            with open(self._p("trades.csv"), "a", newline="") as f:
                csv.writer(f).writerow([
                    f"{fill.ts:.0f}", fill.side, fill.symbol, fill.address, f"{fill.qty:.6g}",
                    f"{fill.price:.6g}", f"{fill.usd:.4f}", f"{fill.fee_usd:.4f}",
                    f"{fill.impact_pct:.3f}", fill.closed,
                    "" if fill.pnl_usd is None else f"{fill.pnl_usd:.4f}",
                    "" if fill.multiple is None else f"{fill.multiple:.3f}", reason])

    def write_summary(self, text: str) -> str | None:
        if not self.dir:
            return None
        path = self._p("summary.md")
        with open(path, "w") as f:
            f.write(text)
        return path
