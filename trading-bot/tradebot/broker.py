"""Order execution.

PaperBroker - fake money. Fills against a constant-product pool model so size,
              thin liquidity, fees, token taxes and landing latency all cost
              money the way they would on-chain. Errs on the pessimistic side.
LiveBroker  - real money. Swaps on-chain through a V2 router (PancakeSwap on
              BNB Chain) with your dedicated wallet, behind hard USD caps.
Both expose the same interface, so the strategy can't tell them apart.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import asdict

from .config import Config
from .models import Fill, Position, SafetyReport, TokenSnapshot


def pool_impact(trade_usd: float, liquidity_usd: float) -> float:
    """Fractional price impact of a trade on an x*y=k pool (liquidity = both sides)."""
    side = max(liquidity_usd / 2.0, 1.0)
    return trade_usd / (side + trade_usd)


def _close(positions: dict, pos: Position, fill: Fill) -> None:
    del positions[pos.address]
    fill.closed = True
    fill.pnl_usd = pos.proceeds_usd - pos.cost_usd
    fill.multiple = pos.proceeds_usd / pos.cost_usd if pos.cost_usd else 0.0


class PaperBroker:
    live = False

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.cash = cfg.starting_balance_usdt
        self.positions: dict[str, Position] = {}
        self.taxes: dict[str, tuple[float, float]] = {}
        self.total_fees_usd = 0.0
        self.total_impact_usd = 0.0
        self.last_error = ""

    def can_trade(self, snap: TokenSnapshot) -> tuple[bool, str]:
        return True, ""

    def remember_safety(self, address: str, r: SafetyReport) -> None:
        self.taxes[address] = (r.buy_tax_pct or 0.0, r.sell_tax_pct or 0.0)

    def buy(self, snap: TokenSnapshot, usd: float, ts: float, reason: str) -> Fill | None:
        usd = min(usd, self.cash)
        fee = usd * self.cfg.fee_pct / 100 + self.cfg.network_fee_usd
        net = usd - fee
        if net <= 0 or snap.price_usd <= 0 or snap.address in self.positions:
            return None
        impact = pool_impact(net, snap.liquidity_usd)
        price = snap.price_usd * (1 + impact) * (1 + self.cfg.latency_slippage_pct / 100)
        qty = net / price * (1 - self.taxes.get(snap.address, (0, 0))[0] / 100)
        self.cash -= usd
        self.total_fees_usd += fee
        self.total_impact_usd += net - qty * snap.price_usd
        self.positions[snap.address] = Position(
            address=snap.address, symbol=snap.symbol, qty=qty, entry_price=usd / qty,
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
        gross = qty * price * (1 - self.taxes.get(address, (0, 0))[1] / 100)
        fee = min(gross * self.cfg.fee_pct / 100 + self.cfg.network_fee_usd, gross)
        proceeds = gross - fee
        self.cash += proceeds
        self.total_fees_usd += fee
        self.total_impact_usd += mark_value - gross
        pos.qty -= qty
        pos.proceeds_usd += proceeds
        fill = Fill("sell", address, pos.symbol, qty, price, proceeds, fee, impact * 100, ts)
        if fraction >= 1.0 or pos.qty * snap.price_usd < 0.01:
            _close(self.positions, pos, fill)
        return fill

    def write_off(self, address: str, ts: float) -> Fill | None:
        """Token vanished (rug / delisted): position is worth nothing."""
        pos = self.positions.get(address)
        if pos is None:
            return None
        fill = Fill("sell", address, pos.symbol, pos.qty, 0.0, 0.0, 0.0, 100.0, ts)
        pos.qty = 0
        _close(self.positions, pos, fill)
        return fill

    def equity(self, marks: dict[str, float]) -> float:
        held = sum(p.qty * marks.get(a, p.last_price) for a, p in self.positions.items())
        return self.cash + held


class LiveBroker:
    """Real swaps from your dedicated wallet. Every buy is capped by
    max_live_trade_usd and the day's total by max_live_daily_spend_usd."""
    live = True

    def __init__(self, cfg: Config, client):
        from .evm import SwapError
        self._SwapError = SwapError
        self.cfg = cfg
        self.client = client
        self.positions: dict[str, Position] = {}
        self.raw: dict[str, int] = {}             # token units actually held, per position
        self.paths: dict[str, list[str]] = {}
        self.taxes: dict[str, tuple[float, float]] = {}
        self.total_fees_usd = 0.0                 # gas, in USD
        self.total_impact_usd = 0.0
        self.day = None
        self.day_spent = 0.0
        self.last_error = ""
        self._cash = 0.0
        self._cash_at = 0.0
        self.native_px = client.native_price_in_quote()
        self._load()

    # ------------------------------------------------------------ state
    @property
    def cash(self) -> float:
        if time.time() - self._cash_at > 30:
            self.refresh()
        return self._cash

    def refresh(self) -> None:
        self._cash = self.client.quote_balance()
        self._cash_at = time.time()

    def _save(self) -> None:
        data = {"day": self.day, "day_spent": self.day_spent,
                "positions": [{**asdict(p), "raw": str(self.raw.get(a, 0)), "path": self.paths.get(a)}
                              for a, p in self.positions.items()]}
        tmp = self.cfg.live_state_file + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, self.cfg.live_state_file)

    def _load(self) -> None:
        if not os.path.exists(self.cfg.live_state_file):
            return
        with open(self.cfg.live_state_file) as f:
            data = json.load(f)
        self.day, self.day_spent = data.get("day"), data.get("day_spent", 0.0)
        for d in data.get("positions", []):
            raw, path = int(d.pop("raw")), d.pop("path")
            p = Position(**d)
            self.positions[p.address] = p
            self.raw[p.address] = raw
            self.paths[p.address] = path

    def _gas_usd(self, wei_before: int) -> float:
        return (self.client.gas_spent_wei - wei_before) / 1e18 * self.native_px

    # ------------------------------------------------------------ trading
    def can_trade(self, snap: TokenSnapshot) -> tuple[bool, str]:
        if self.client.native_balance() < self.cfg.min_gas_reserve_native:
            return False, "not enough BNB for gas"
        if snap.address not in self.paths:
            path = self.client.path_for(snap.address, snap.quote_address or None)
            if not path:
                return False, "no V2 pool on router"
            self.paths[snap.address] = path
        return True, ""

    def remember_safety(self, address: str, r: SafetyReport) -> None:
        self.taxes[address] = (r.buy_tax_pct or 0.0, r.sell_tax_pct or 0.0)

    def buy(self, snap: TokenSnapshot, usd: float, ts: float, reason: str) -> Fill | None:
        day = time.strftime("%Y-%m-%d", time.gmtime())
        if day != self.day:
            self.day, self.day_spent = day, 0.0
        usd = min(usd, self.cfg.max_live_trade_usd,
                  self.cfg.max_live_daily_spend_usd - self.day_spent, self.cash)
        self.last_error = ""
        if snap.address in self.positions:
            return None
        if usd < self.cfg.min_trade_usd:
            cap_left = self.cfg.max_live_daily_spend_usd - self.day_spent
            self.last_error = ("daily live spend cap reached" if cap_left < self.cfg.min_trade_usd
                               else "not enough USDT in wallet")
            return None
        path = self.paths.get(snap.address)
        if not path:
            return None
        qdec = self.client.decimals(path[0])
        gas0 = self.client.gas_spent_wei
        try:
            got, tx = self.client.swap(path, int(usd * 10 ** qdec), self.cfg.buy_slippage_pct,
                                       self.taxes.get(snap.address, (0, 0))[0])
        except self._SwapError as e:
            self.last_error = str(e)
            return None
        if got <= 0:
            self.last_error = "swap returned no tokens"
            return None
        gas = self._gas_usd(gas0)
        qty = got / 10 ** self.client.decimals(snap.address)
        entry = usd / qty
        self.day_spent += usd
        self.total_fees_usd += gas
        self.total_impact_usd += max(0.0, usd - qty * snap.price_usd)
        self.positions[snap.address] = Position(
            address=snap.address, symbol=snap.symbol, qty=qty, entry_price=entry,
            cost_usd=usd + gas, opened_at=ts, reason=f"{reason} · tx {tx}",
            peak_price=snap.price_usd, last_price=snap.price_usd)
        self.raw[snap.address] = got
        self.refresh()
        self._save()
        return Fill("buy", snap.address, snap.symbol, qty, entry, usd, gas,
                    (entry / snap.price_usd - 1) * 100 if snap.price_usd else 0.0, ts)

    def sell(self, address: str, fraction: float, snap: TokenSnapshot | None, ts: float) -> Fill | None:
        pos = self.positions.get(address)
        if pos is None:
            return None
        held = min(self.raw.get(address, 0), self.client.balance(address))
        amount = held if fraction >= 1.0 else int(held * fraction)
        path = list(reversed(self.paths[address]))
        gas0 = self.client.gas_spent_wei
        try:
            got, tx = self.client.swap(path, amount, self.cfg.exit_slippage_pct,
                                       self.taxes.get(address, (0, 0))[1])
        except self._SwapError as e:
            pos.sell_failures += 1
            self.last_error = str(e)
            return None
        gas = self._gas_usd(gas0)
        qdec = self.client.decimals(path[-1])
        proceeds = got / 10 ** qdec
        qty = amount / 10 ** self.client.decimals(address)
        self.raw[address] = held - amount
        pos.qty = self.raw[address] / 10 ** self.client.decimals(address)
        pos.proceeds_usd += proceeds
        pos.cost_usd += gas
        self.total_fees_usd += gas
        if snap is not None:
            self.total_impact_usd += max(0.0, qty * snap.price_usd - proceeds)
        fill = Fill("sell", address, pos.symbol, qty, proceeds / qty if qty else 0.0, proceeds, gas,
                    0.0, ts)
        if fraction >= 1.0 or self.raw[address] == 0:
            _close(self.positions, pos, fill)
            self.raw.pop(address, None)
        self.refresh()
        self._save()
        return fill

    def write_off(self, address: str, ts: float) -> Fill | None:
        """Price feed lost the token: try to dump it on-chain anyway; keep it if that fails."""
        return self.sell(address, 1.0, None, ts)

    def equity(self, marks: dict[str, float]) -> float:
        held = sum(p.qty * marks.get(a, p.last_price) for a, p in self.positions.items())
        return self.cash + held
