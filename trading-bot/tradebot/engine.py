"""Main loop: RADAR -> FLOW/HYPE -> AUDIT -> HOLDERS -> SIZE -> SNIPE, then TAKE/STOP."""
from __future__ import annotations

import os
import threading
import time
from collections import deque

from . import agents as A
from .broker import PaperBroker
from .config import Config
from .feeds import FeedError
from .journal import Journal
from .models import Fill, SafetyReport, TokenSnapshot

MAX_WATCH = 400


class Engine:
    def __init__(self, cfg: Config, feed, broker=None, journal: Journal | None = None):
        self.cfg = cfg
        self.feed = feed
        self.broker = broker or PaperBroker(cfg)
        self.journal = journal or Journal(None)
        self.radar = A.LaunchRadar(cfg)
        self.whale = A.WhaleWatch(cfg)
        self.sentiment = A.Sentiment(cfg)
        self.lp = A.LPAuditor(cfg)
        self.holders = A.HolderMap(cfg)
        self.sizer = A.RiskSizer(cfg)
        self.sniper = A.Sniper(cfg)
        self.taker = A.ProfitTaker(cfg)
        self.stop = A.StopGuard(cfg)
        self.journal_agent = A.JournalAgent(cfg)
        self.agents = [self.radar, self.whale, self.sentiment, self.lp, self.holders,
                       self.sizer, self.sniper, self.taker, self.stop, self.journal_agent]

        self.watch: dict[str, float] = {}          # address -> first seen
        self.next_eval: dict[str, float] = {}
        self.done: set[str] = set()                 # blocked or already traded
        self.marks: dict[str, float] = {}
        self.launches_seen = 0
        self.stats = dict(checked=0, blocked=0, trades=0, wins=0, losses=0,
                          best_x=0.0, best_symbol="", worst_x=None, worst_symbol="",
                          realized_pnl=0.0, gross_win=0.0, gross_loss=0.0, last_realized=None)
        self.events: deque = deque(maxlen=200)
        self.pipeline: deque = deque(maxlen=8)
        self.equity_curve: list[tuple[float, float]] = []
        self.halted = False
        self.halt_reason = ""
        self.peak_equity = cfg.starting_balance_usdt
        self.day = None
        self.day_start_equity = cfg.starting_balance_usdt
        self.started_at = feed.now()
        self.ticks = 0
        self.headline = "starting"
        self.errors = 0
        self.lock = threading.Lock()

    # ------------------------------------------------------------------ loop
    def tick(self) -> None:
        with self.lock:
            self._tick()

    def _tick(self) -> None:
        self.feed.advance()
        now = self.feed.now()
        for a in self.agents:
            a.active = False

        try:
            new = self.feed.discover()
        except FeedError as e:
            new = []
            self._error(now, e)
        for addr in new:
            self.watch.setdefault(addr, now)
        self.launches_seen += len(new)
        if new:
            self.radar.touch(f"{len(new)} new · watching {len(self.watch)}")

        wanted = set(self.watch) | set(self.broker.positions)
        try:
            snaps = self.feed.snapshot(wanted)
        except FeedError as e:
            self._error(now, e)
            snaps = {}
        for a, s in snaps.items():
            self.marks[a] = s.price_usd

        self._manage_positions(snaps, now)
        self._kill_switch(now, snaps)
        self._risk_guard(now)
        if not self.halted:
            self._scan(snaps, now)
        self._prune(snaps, now)

        eq = self.equity()
        self.peak_equity = max(self.peak_equity, eq)
        self.equity_curve.append((now, eq))
        if len(self.equity_curve) > 3000:
            self.equity_curve = self.equity_curve[::2]
        self.ticks += 1

    # ------------------------------------------------------------ positions
    def _manage_positions(self, snaps: dict[str, TokenSnapshot], now: float) -> None:
        for addr, pos in list(self.broker.positions.items()):
            s = snaps.get(addr)
            if s is None:
                pos.missing_ticks += 1
                if pos.missing_ticks >= 6:
                    fill = self.broker.write_off(addr, now)
                    self._record(fill, "token vanished from feed: exited", "stop") if fill else self._sell_failed(addr, now)
                continue
            pos.missing_ticks = 0
            pos.last_price = s.price_usd
            pos.peak_price = max(pos.peak_price, s.price_usd)
            reason = self.stop.check(pos, s, now)
            if reason:
                fill = self.broker.sell(addr, 1.0, s, now)
                self._record(fill, reason, "stop") if fill else self._sell_failed(addr, now)
                continue
            tp = self.taker.check(pos, s)
            if tp:
                frac, reason = tp
                fill = self.broker.sell(addr, frac, s, now)
                if fill:
                    self._record(fill, reason, "sell")
                else:
                    pos.ladder_hits -= 1          # retry this rung next tick
                    self._sell_failed(addr, now)

    # --------------------------------------------------------------- entries
    def _scan(self, snaps: dict[str, TokenSnapshot], now: float) -> None:
        cands = [s for a, s in snaps.items()
                 if a in self.watch and a not in self.done and a not in self.broker.positions
                 and self.next_eval.get(a, 0) <= now]
        cands.sort(key=lambda s: s.volume_5m_usd, reverse=True)
        for s in cands:
            if len(self.broker.positions) >= self.cfg.max_open_positions:
                self.sniper.note = f"max {self.cfg.max_open_positions} positions open · waiting"
                return
            self._evaluate(s, now)

    def _prefilter(self, s: TokenSnapshot) -> bool:
        c = self.cfg
        return (s.price_usd > 0 and s.liquidity_usd >= c.min_liquidity_usd
                and 0 < s.market_cap_usd <= c.max_market_cap_usd
                and c.min_age_min <= s.age_min <= c.max_age_min
                and s.volume_1h_usd >= c.min_volume_1h_usd
                and s.buys_5m + s.sells_5m >= c.min_txns_5m)

    def _evaluate(self, s: TokenSnapshot, now: float) -> None:
        a = s.address
        if not self._prefilter(s):
            self.next_eval[a] = now + 60
            return
        self.stats["checked"] += 1
        for agent in (self.whale, self.sentiment):
            ok, why = agent.evaluate(s)
            if not ok:
                self.next_eval[a] = now + self.cfg.reevaluate_after_min * 60
                self._pipe(s, "watching", why)
                return
        try:
            report = self.feed.safety(a)
        except FeedError:
            report = SafetyReport(available=False)
        self.broker.remember_safety(a, report)
        for agent in (self.lp, self.holders):
            ok, why = agent.evaluate(s, report)
            if not ok:
                self.done.add(a)
                self.stats["blocked"] += 1
                self._pipe(s, "blocked", why)
                self._event(now, "blocked", s.symbol, why)
                return
        flow = f"buys {s.buys_5m / max(s.buys_5m + s.sells_5m, 1):.0%} of flow · vol {s.volume_5m_usd / max(s.volume_1h_usd / 12, 1):.1f}x"
        usd, size_note = self.sizer.size(s, self.equity(), self.broker.cash)
        if usd < self.cfg.min_trade_usd:
            self.next_eval[a] = now + self.cfg.reevaluate_after_min * 60
            self._pipe(s, "skipped", f"size ${usd:.2f} too small")
            return
        ok, why = self.broker.can_trade(s)
        if not ok:
            self.next_eval[a] = now + self.cfg.reevaluate_after_min * 60
            self._pipe(s, "skipped", why)
            return
        fill = self.broker.buy(s, usd, now, flow)
        if not fill and self.broker.last_error.startswith("daily"):
            self._halt(now, "daily live spend cap reached")   # resets at the next UTC day
            return
        if not fill and self.broker.last_error:
            self.done.add(a)
            self._pipe(s, "skipped", self.broker.last_error[:80])
            self._event(now, "error", s.symbol, f"buy not executed: {self.broker.last_error}")
        if fill:
            self.done.add(a)
            self.sniper.touch(f"BUY ${s.symbol} · impact {fill.impact_pct:.2f}%")
            self._pipe(s, "bought", flow)
            self._record(fill, f"{flow} · {size_note}", "buy")

    def _kill_switch(self, now: float, snaps) -> None:
        path = self.cfg.kill_switch_file
        if not path or not os.path.exists(path):
            return
        if not self.halted or not self.halt_reason.startswith("kill switch"):
            self._halt(now, f"kill switch file '{path}' found: selling everything")
        for addr in list(self.broker.positions):
            s = snaps.get(addr)
            fill = self.broker.sell(addr, 1.0, s, now) if s else self.broker.write_off(addr, now)
            self._record(fill, "kill switch", "stop") if fill else self._sell_failed(addr, now)

    def _sell_failed(self, addr: str, now: float) -> None:
        pos = self.broker.positions.get(addr)
        if pos and pos.sell_failures in (1, 5, 20, 100):     # don't spam the log
            self._event(now, "error", pos.symbol, f"sell failed ({pos.sell_failures}x): {self.broker.last_error}")

    # ------------------------------------------------------------ risk guard
    def _risk_guard(self, now: float) -> None:
        eq = self.equity()
        day = int(now // 86400)
        if day != self.day:
            self.day, self.day_start_equity = day, eq
            if self.halted and self.halt_reason.startswith("daily"):
                self.halted, self.halt_reason = False, ""
                self._event(now, "info", "", "new day: daily loss limit reset")
        if self.halted:
            return
        if eq <= self.cfg.starting_balance_usdt * (1 - self.cfg.max_total_drawdown_pct / 100):
            self._halt(now, f"total drawdown limit -{self.cfg.max_total_drawdown_pct:g}% hit")
        elif eq <= self.day_start_equity * (1 - self.cfg.max_daily_loss_pct / 100):
            self._halt(now, f"daily loss limit -{self.cfg.max_daily_loss_pct:g}% hit")

    def _halt(self, now: float, reason: str) -> None:
        self.halted, self.halt_reason = True, reason
        self.headline = f"HALTED · {reason}"
        self._event(now, "halt", "", reason + " · no new entries")

    def _prune(self, snaps, now: float) -> None:
        for a in list(self.watch):
            s = snaps.get(a)
            too_old = s is not None and s.age_min > self.cfg.max_age_min
            stale = s is None and now - self.watch[a] > 1800
            if a not in self.broker.positions and (a in self.done or too_old or stale):
                del self.watch[a]
                self.next_eval.pop(a, None)
        if len(self.watch) > MAX_WATCH:
            for a, _ in sorted(self.watch.items(), key=lambda kv: kv[1])[:len(self.watch) - MAX_WATCH]:
                if a not in self.broker.positions:
                    del self.watch[a]

    # --------------------------------------------------------------- records
    def _record(self, fill: Fill | None, reason: str, kind: str) -> None:
        if fill is None:
            return
        self.journal.fill(fill, reason)
        self.journal_agent.touch(f"{fill.side} ${fill.symbol} logged")
        ev = self._event(fill.ts, kind, fill.symbol, reason, usd=fill.usd, fee=fill.fee_usd)
        if kind == "buy":
            self.headline = f"SNIPER · BUY ${fill.symbol} ${fill.usd:,.2f}"
            return
        if fill.closed:
            st = self.stats
            st["trades"] += 1
            st["realized_pnl"] += fill.pnl_usd
            st["last_realized"] = fill.pnl_usd
            if fill.pnl_usd > 0:
                st["wins"] += 1
                st["gross_win"] += fill.pnl_usd
            else:
                st["losses"] += 1
                st["gross_loss"] += -fill.pnl_usd
            if fill.multiple > st["best_x"]:
                st["best_x"], st["best_symbol"] = fill.multiple, fill.symbol
            if st["worst_x"] is None or fill.multiple < st["worst_x"]:
                st["worst_x"], st["worst_symbol"] = fill.multiple, fill.symbol
            ev["pnl"], ev["multiple"] = fill.pnl_usd, fill.multiple
        agent = "STOP GUARD · STOPPED" if kind == "stop" else "PROFIT TAKER · SOLD"
        self.headline = f"{agent} ${fill.symbol}" + (f" {fill.multiple:.2f}x" if fill.closed else "")

    def _event(self, ts: float, kind: str, symbol: str, reason: str, **extra) -> dict:
        ev = {"ts": ts, "kind": kind, "symbol": symbol, "reason": reason, **extra}
        self.events.append(ev)
        self.journal.event(ev)
        return ev

    def _pipe(self, s: TokenSnapshot, verdict: str, why: str) -> None:
        self.pipeline.append({"symbol": s.symbol, "price": s.price_usd,
                              "change5m": s.price_change_5m_pct, "verdict": verdict, "why": why})

    def _error(self, now: float, e: Exception) -> None:
        self.errors += 1
        self._event(now, "error", "", str(e)[:200])

    # ----------------------------------------------------------------- views
    def equity(self) -> float:
        return self.broker.equity(self.marks)

    def state(self) -> dict:
        with self.lock:
            st = self.stats
            closed = st["wins"] + st["losses"]
            positions = [{
                "symbol": p.symbol, "address": p.address, "entry": p.entry_price,
                "mark": p.last_price, "x": p.last_price / p.entry_price if p.entry_price else 0,
                "cost": p.cost_usd, "value": p.qty * p.last_price, "proceeds": p.proceeds_usd,
                "age_min": (self.feed.now() - p.opened_at) / 60, "ladder": p.ladder_hits,
                "reason": p.reason,
            } for p in self.broker.positions.values()]
            step = max(1, len(self.equity_curve) // 400)
            return {
                "mode": self.cfg.mode, "feed": self.feed.name, "now": self.feed.now(),
                "started_at": self.started_at, "ticks": self.ticks, "headline": self.headline,
                "halted": self.halted, "halt_reason": self.halt_reason,
                "start_balance": self.cfg.starting_balance_usdt, "equity": self.equity(),
                "cash": self.broker.cash, "fees": self.broker.total_fees_usd,
                "impact": self.broker.total_impact_usd, "launches": self.launches_seen,
                "watching": len(self.watch), "errors": self.errors,
                "stats": {**st, "closed": closed, "win_rate": st["wins"] / closed if closed else None},
                "agents": [a.info() for a in self.agents],
                "positions": positions, "pipeline": list(self.pipeline),
                "events": list(self.events)[-40:],
                "equity_curve": self.equity_curve[::step],
                "journal_lines": self.journal.lines,
                "live": getattr(self.broker, "live", False),
                "wallet": getattr(getattr(self.broker, "client", None), "address", ""),
            }

    def summary(self) -> str:
        st = self.stats
        closed = st["wins"] + st["losses"]
        eq = self.equity()
        start = self.cfg.starting_balance_usdt
        hours = (self.feed.now() - self.started_at) / 3600
        avg_w = st["gross_win"] / st["wins"] if st["wins"] else 0
        avg_l = st["gross_loss"] / st["losses"] if st["losses"] else 0
        pf = st["gross_win"] / st["gross_loss"] if st["gross_loss"] else float("inf")
        lines = [
            f"# Session summary ({self.cfg.mode} mode, {self.feed.name} feed)",
            "",
            f"- Market time covered: {hours:.1f} h",
            f"- Equity: ${start:,.2f} -> ${eq:,.2f} ({(eq / start - 1) * 100:+.1f}%)",
            f"- Max drawdown from peak during run: see equity curve; peak ${self.peak_equity:,.2f}",
            f"- Closed trades: {closed} (wins {st['wins']}, losses {st['losses']}"
            + (f", win rate {st['wins'] / closed:.0%})" if closed else ")"),
            f"- Avg win ${avg_w:,.2f} · avg loss ${avg_l:,.2f} · profit factor {pf:.2f}",
            f"- Best {st['best_x']:.2f}x ${st['best_symbol']}"
            + (f" · worst {st['worst_x']:.2f}x ${st['worst_symbol']}" if st["worst_x"] is not None else ""),
            f"- Open positions: {len(self.broker.positions)}",
            f"- Launches seen {self.launches_seen} · checked {st['checked']} · blocked by audit {st['blocked']}",
            f"- Paid in fees ${self.broker.total_fees_usd:,.2f} · lost to price impact/slippage ${self.broker.total_impact_usd:,.2f}",
            f"- Halted: {self.halt_reason or 'no'} · feed errors: {self.errors}",
        ]
        return "\n".join(lines) + "\n"


def run_forever(engine: Engine, interval_s: float, stop: threading.Event | None = None) -> None:
    stop = stop or threading.Event()
    while not stop.is_set():
        t0 = time.monotonic()
        engine.tick()
        stop.wait(max(0.0, interval_s - (time.monotonic() - t0)))
