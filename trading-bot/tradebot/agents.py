"""The agents. Each is small, does one job, and explains its decision."""
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
    name, badge, blurb = "Launch Radar", "SCAN", "new pairs (DexScreener) · flags only, never trades"


class WhaleWatch(Agent):
    name, badge, blurb = "Whale Watch", "TRACK", "buy pressure: buys as share of 5m flow"

    def evaluate(self, s: TokenSnapshot) -> tuple[bool, str]:
        share = s.buys_5m / max(s.buys_5m + s.sells_5m, 1)
        ok = share >= self.cfg.min_buy_share
        why = f"buys {share:.0%} of flow"
        self.touch(f"${s.symbol}: {why}")
        return ok, why


class Sentiment(Agent):
    # No social API here: a volume spike is the honest stand-in for "hype".
    name, badge, blurb = "Sentiment", "READ", "volume spike vs 1h average"

    def evaluate(self, s: TokenSnapshot) -> tuple[bool, str]:
        hour_avg_5m = s.volume_1h_usd / 12 if s.volume_1h_usd else 0
        spike = s.volume_5m_usd / hour_avg_5m if hour_avg_5m else 0
        ok = spike >= self.cfg.min_volume_spike
        why = f"vol {spike:.1f}x 1h avg"
        self.touch(f"${s.symbol}: {why}")
        return ok, why


class LPAuditor(Agent):
    name, badge, blurb = "LP Auditor", "AUDIT", "honeypot / tax / LP lock / mint"

    def evaluate(self, s: TokenSnapshot, r: SafetyReport) -> tuple[bool, str]:
        c = self.cfg
        lp_long = r.lp_locked_long_pct if r.lp_locked_long_pct is not None else r.lp_locked_pct
        if not r.available:
            why = "audit unavailable (fail closed)"
        elif c.block_mint_authority and r.mint_authority:
            why = "mint authority enabled"
        elif c.block_freeze_authority and r.freeze_authority:
            why = "freeze authority enabled"
        elif c.block_lp_unlocked and r.lp_unlocked:
            why = "LP unlocked"
        elif c.block_lp_unlocked and lp_long is not None and lp_long < c.min_lp_locked_pct:
            why = f"only {lp_long:.0f}% of LP burned/locked {c.min_lp_lock_days:g}d+"
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
    name, badge, blurb = "Holder Map", "MAP", "holders, top-10 and dev wallet share"

    def evaluate(self, s: TokenSnapshot, r: SafetyReport) -> tuple[bool, str]:
        c = self.cfg
        why = None
        if r.top10_pct is not None and r.top10_pct > c.max_top10_pct:
            why = f"top 10 hold {r.top10_pct:.0f}%"
        elif r.creator_pct is not None and r.creator_pct > c.max_creator_pct:
            why = f"dev wallet holds {r.creator_pct:.1f}%"
        elif r.holder_count is not None and r.holder_count < c.min_holders:
            why = f"only {r.holder_count} holders"
        if why:
            self.touch(f"blocked ${s.symbol}: {why}")
            return False, why
        parts = []
        if r.holder_count is not None:
            parts.append(f"{r.holder_count} holders")
        if r.top10_pct is not None:
            parts.append(f"top 10 {r.top10_pct:.0f}%")
        why = " · ".join(parts) or "holder data n/a"
        self.touch(f"${s.symbol}: {why}")
        return True, why


class RiskSizer(Agent):
    name, badge, blurb = "Risk Sizer", "SIZE", "% of equity, smaller when volatile or losing"

    def size(self, s: TokenSnapshot, equity: float, cash: float, loss_streak: int = 0) -> tuple[float, str]:
        c = self.cfg
        vol = min(abs(s.price_change_5m_pct), 50) / 50          # 0 calm .. 1 wild
        pct = c.max_position_pct - vol * (c.max_position_pct - c.min_position_pct)
        note = f"{pct:.0f}% of equity"
        if loss_streak >= c.loss_streak_len:
            pct *= c.loss_streak_size_mult
            note += f" x{c.loss_streak_size_mult:g} after {loss_streak} losses"
        usd = min(equity * pct / 100, c.max_position_usd, cash,
                  s.liquidity_usd * c.max_pct_of_liquidity / 100)
        self.touch(f"{note} -> ${usd:,.2f}")
        return usd, note


class Sniper(Agent):
    name, badge, blurb = "Sniper", "LIVE", "executes entries in parts"


class ProfitTaker(Agent):
    name, badge, blurb = "Profit Taker", "LADDER", "40% at 2x · 30% at 4x · 30% runs"

    def check(self, p: Position, s: TokenSnapshot) -> tuple[float, str] | None:
        """Returns (fraction of what's LEFT to sell, reason). Ladder rungs are % of the original."""
        ladder = self.cfg.take_profit_ladder
        if p.ladder_hits < len(ladder) and p.qty > 0:
            lvl, frac = ladder[p.ladder_hits]
            if s.price_usd >= p.entry_price * lvl:
                p.ladder_hits += 1
                base = p.initial_qty or p.qty
                of_left = min(1.0, frac * base / p.qty)
                self.touch(f"sold {frac:.0%} of ${p.symbol} at {lvl:g}x")
                return of_left, f"TP {lvl:g}x: sold {frac:.0%} of position"
        return None


class StopGuard(Agent):
    name, badge, blurb = "Stop Guard", "ARMED", "hard / trailing / time stop, dev sells, LP pulled"

    def check(self, p: Position, s: TokenSnapshot, now: float, dev_balance: float | None = None) -> str | None:
        c = self.cfg
        x = s.price_usd / p.entry_price
        reason = None
        dev_sold = (dev_balance is not None and p.creator_balance_at_entry
                    and dev_balance < p.creator_balance_at_entry * (1 - c.dev_sell_exit_pct / 100))
        if s.liquidity_usd < c.rug_liquidity_usd:
            reason = "liquidity pulled"
        elif p.entry_liquidity and s.liquidity_usd < p.entry_liquidity * (1 - c.liquidity_drop_exit_pct / 100):
            reason = f"LP pulled: liquidity -{(1 - s.liquidity_usd / p.entry_liquidity) * 100:.0f}% since entry"
        elif dev_sold:
            reason = f"dev sold {(1 - dev_balance / p.creator_balance_at_entry) * 100:.0f}% of bag: instant exit"
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
    name, badge, blurb = "Journal", "WRITE", "logs every decision with a reason"
