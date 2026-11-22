"""Order execution.

PaperBroker fills against a constant-product pool model so that size, thin
liquidity, fees and landing latency all cost you money the way they would
on-chain. It never sees the price *after* your own trade, so it errs on the
pessimistic side - a strategy that only works with free fills is not a strategy.
"""
from __future__ import annotations

from .config import Config
from .models import Fill, Position, TokenSnapshot


def pool_impact(trade_usd: float, liquidity_usd: float) -> float:
    """Fractional price impact of a trade on an x*y=k pool (liquidity = both sides)."""
    side = max(liquidity_usd / 2.0, 1.0)
    return trade_usd / (side + trade_usd)


class PaperBroker:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.cash = cfg.starting_balance_usdt
        self.positions: dict[str, Position] = {}
        self.total_fees_usd = 0.0
        self.total_impact_usd = 0.0

    def buy(self, snap: TokenSnapshot, usd: float, ts: float, reason: str) -> Fill | None:
        usd = min(usd, self.cash)
        fee = usd * self.cfg.fee_pct / 100 + self.cfg.network_fee_usd
        net = usd - fee
        if net <= 0 or snap.price_usd <= 0 or snap.address in self.positions:
            return None
        impact = pool_impact(net, snap.liquidity_usd)
        price = snap.price_usd * (1 + impact) * (1 + self.cfg.latency_slippage_pct / 100)
        qty = net / price
        self.cash -= usd
        self.total_fees_usd += fee
        self.total_impact_usd += net - qty * snap.price_usd
        self.positions[snap.address] = Position(
            address=snap.address, symbol=snap.symbol, qty=qty, entry_price=price,
            cost_usd=usd, opened_at=ts, reason=reason,
            peak_price=snap.price_usd, last_price=snap.price_usd,
        )
        return Fill("buy", snap.address, snap.symbol, qty, price, usd, fee, impact * 100, ts)

    def sell(self, address: str, fraction: float, snap: TokenSnapshot, ts: float) -> Fill | None:
        pos = self.positions.get(address)
        if pos is None:
            return None
        fraction = min(max(fraction, 0.0), 1.0)
        qty = pos.qty * fraction
        mark_value = qty * snap.price_usd
        impact = pool_impact(mark_value, snap.liquidity_usd)
        price = snap.price_usd * (1 - impact) * (1 - self.cfg.latency_slippage_pct / 100)
        gross = qty * price
        fee = min(gross * self.cfg.fee_pct / 100 + self.cfg.network_fee_usd, gross)
        proceeds = gross - fee
        self.cash += proceeds
        self.total_fees_usd += fee
        self.total_impact_usd += mark_value - gross
        pos.qty -= qty
        pos.proceeds_usd += proceeds
        fill = Fill("sell", address, pos.symbol, qty, price, proceeds, fee, impact * 100, ts)
        if fraction >= 1.0 or pos.qty * snap.price_usd < 0.01:
            self._close(pos, fill)
        return fill

    def write_off(self, address: str, ts: float) -> Fill | None:
        """Token vanished (rug / delisted): position is worth nothing."""
        pos = self.positions.get(address)
        if pos is None:
            return None
        fill = Fill("sell", address, pos.symbol, pos.qty, 0.0, 0.0, 0.0, 100.0, ts)
        pos.qty = 0
        self._close(pos, fill)
        return fill

    def _close(self, pos: Position, fill: Fill) -> None:
        del self.positions[pos.address]
        fill.closed = True
        fill.pnl_usd = pos.proceeds_usd - pos.cost_usd
        fill.multiple = pos.proceeds_usd / pos.cost_usd if pos.cost_usd else 0.0

    def equity(self, marks: dict[str, float]) -> float:
        held = sum(p.qty * marks.get(a, p.last_price) for a, p in self.positions.items())
        return self.cash + held


class LiveBroker:
    """Placeholder. Real-money execution is intentionally not wired up yet.

    It will be added only after the paper results justify it, and will need:
    a dedicated hot wallet holding only what you can afford to lose, a swap
    route USDT -> token via an aggregator, transaction simulation before send,
    hard per-trade and per-day USD caps, and a manual kill switch.
    """

    def __init__(self, cfg: Config):
        raise RuntimeError(
            "Live trading is locked in this version. Run --mode sim or --mode paper "
            "until the paper results over several weeks justify real money."
        )
