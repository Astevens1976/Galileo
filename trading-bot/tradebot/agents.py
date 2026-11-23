"""The ten agents. Each is small, does one job, and explains its decision."""
from __future__ import annotations

from .config import Config
from .models import Position, SafetyReport, TokenSnapshot


class Agent:
    name = ""
    badge = ""
    blurb = ""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.note = ""
        self.active = False

    def touch(self, note: str) -> None:
        self.note = note
        self.active = True

    def info(self) -> dict:
        return {"name": self.name, "badge": self.badge, "blurb": self.blurb,
                "note": self.note, "active": self.active}


class LaunchRadar(Agent):
    name, badge, blurb = "Launch Radar", "SCAN", "new launches (DexScreener)"


class WhaleWatch(Agent):
    name, badge, blurb = "Whale Watch", "TRACK", "buy vs sell flow, last 5m"

    def evaluate(self, s: TokenSnapshot) -> tuple[bool, str]:
        ratio = s.buys_5m / max(s.sells_5m, 1)
        share = s.buys_5m / max(s.buys_5m + s.sells_5m, 1)
        ok = ratio >= self.cfg.min_buy_sell_ratio
        why = f"buys {share:.0%} of flow ({s.buys_5m}/{s.sells_5m})"
        self.touch(f"${s.symbol}: {why}")
        return ok, why


class Sentiment(Agent):
    # No social API here: volume acceleration is the honest stand-in for "hype".
    name, badge, blurb = "Sentiment", "READ", "volume acceleration (hype proxy)"

    def evaluate(self, s: TokenSnapshot) -> tuple[bool, str]:
        hour_avg_5m = s.volume_1h_usd / 12 if s.volume_1h_usd else 0
        accel = s.volume_5m_usd / hour_avg_5m if hour_avg_5m else 0
        ok = accel >= self.cfg.min_volume_accel
        why = f"volume {accel:.1f}x 1h avg"
        self.touch(f"${s.symbol}: {why}")
        return ok, why


class LPAuditor(Agent):
    name, badge, blurb = "LP Auditor", "AUDIT", "honeypot / tax / LP lock / mint"

    def evaluate(self, s: TokenSnapshot, r: SafetyReport) -> tuple[bool, str]:
        c = self.cfg
        if not r.available:
            why = "audit unavailable (fail closed)"
        elif c.block_mint_authority and r.mint_authority:
            why = "mint authority enabled"
        elif c.block_freeze_authority and r.freeze_authority:
            why = "freeze authority enabled"
        elif c.block_lp_unlocked and r.lp_unlocked:
            why = "LP unlocked"
        elif c.block_lp_unlocked and r.lp_locked_pct is not None and r.lp_locked_pct < c.min_lp_locked_pct:
            why = f"only {r.lp_locked_pct:.0f}% of LP locked"
        elif max(r.buy_tax_pct or 0, r.sell_tax_pct or 0) > c.max_tax_pct:
            why = f"tax {r.buy_tax_pct or 0:.0f}% buy / {r.sell_tax_pct or 0:.0f}% sell"
        elif r.dangers:
            why = r.dangers[0]
        else:
            self.touch(f"passed ${s.symbol}")
            return True, "audit passed"
        self.touch(f"blocked ${s.symbol}: {why}")
        return False, why


class HolderMap(Agent):
    name, badge, blurb = "Holder Map", "MAP", "top-10 holder concentration"

    def evaluate(self, s: TokenSnapshot, r: SafetyReport) -> tuple[bool, str]:
        if r.top10_pct is not None and r.top10_pct > self.cfg.max_top10_pct:
            why = f"top 10 hold {r.top10_pct:.0f}%"
            self.touch(f"blocked ${s.symbol}: {why}")
            return False, why
        why = "top 10 n/a" if r.top10_pct is None else f"top 10 hold {r.top10_pct:.0f}%"
        self.touch(f"${s.symbol}: {why}")
        return True, why


class RiskSizer(Agent):
    name, badge, blurb = "Risk Sizer", "SIZE", "size by volatility"

    def size(self, s: TokenSnapshot, equity: float, cash: float) -> tuple[float, str]:
        c = self.cfg
        vol = min(abs(s.price_change_5m_pct), 50) / 50          # 0 calm .. 1 wild
        pct = c.max_position_pct - vol * (c.max_position_pct - c.min_position_pct)
        usd = min(equity * pct / 100, c.max_position_usd, cash,
                  s.liquidity_usd * c.max_pct_of_liquidity / 100)
        self.touch(f"{pct:.1f}% of ${equity:,.0f} -> ${usd:,.2f}")
        return usd, f"{pct:.1f}% of equity"


class Sniper(Agent):
    name, badge, blurb = "Sniper", "LIVE", "executes entries"


class ProfitTaker(Agent):
    name, badge, blurb = "Profit Taker", "LADDER", "take-profit ladder + moonbag"

    def check(self, p: Position, s: TokenSnapshot) -> tuple[float, str] | None:
        ladder = self.cfg.take_profit_ladder
        if p.ladder_hits < len(ladder):
            lvl, frac = ladder[p.ladder_hits]
            if s.price_usd >= p.entry_price * lvl:
                p.ladder_hits += 1
                self.touch(f"sold {frac:.0%} ${p.symbol} at {lvl:g}x")
                return frac, f"TP {lvl:g}x hit"
        return None


class StopGuard(Agent):
    name, badge, blurb = "Stop Guard", "ARMED", "hard stop, trailing, time stop"

    def check(self, p: Position, s: TokenSnapshot, now: float) -> str | None:
        c = self.cfg
        x = s.price_usd / p.entry_price
        reason = None
        if s.liquidity_usd < c.rug_liquidity_usd:
            reason = "liquidity pulled"
        elif x <= 1 - c.hard_stop_pct / 100:
            reason = f"hard stop -{c.hard_stop_pct:g}% (at {x - 1:+.0%})"
        elif (p.peak_price >= p.entry_price * c.trailing_activate_x
              and s.price_usd <= p.peak_price * (1 - c.trailing_stop_pct / 100)):
            reason = f"trailing stop -{c.trailing_stop_pct:g}% from peak"
        elif (now - p.opened_at) / 60 >= c.time_stop_min and x < c.time_stop_min_x and p.ladder_hits == 0:
            reason = f"time stop: flat after {c.time_stop_min:g}m"
        if reason:
            self.touch(f"stopped ${p.symbol}: {reason}")
        return reason


class JournalAgent(Agent):
    name, badge, blurb = "Journal", "WRITE", "logs every fill"
