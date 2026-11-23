from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class TokenSnapshot:
    address: str
    symbol: str
    price_usd: float
    liquidity_usd: float
    market_cap_usd: float
    age_min: float
    buys_5m: int = 0
    sells_5m: int = 0
    volume_5m_usd: float = 0.0
    volume_1h_usd: float = 0.0
    price_change_5m_pct: float = 0.0
    pair_address: str = ""
    quote_address: str = ""
    dex_id: str = ""


@dataclass
class SafetyReport:
    available: bool = True
    mint_authority: bool | None = None
    freeze_authority: bool | None = None
    lp_unlocked: bool | None = None
    top10_pct: float | None = None
    lp_locked_pct: float | None = None
    buy_tax_pct: float | None = None
    sell_tax_pct: float | None = None
    dangers: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass
class Position:
    address: str
    symbol: str
    qty: float
    entry_price: float          # effective price incl. impact + slippage
    cost_usd: float             # total USDT spent incl. fees
    opened_at: float
    reason: str
    peak_price: float = 0.0
    last_price: float = 0.0
    proceeds_usd: float = 0.0   # USDT received from partial sells so far
    ladder_hits: int = 0
    missing_ticks: int = 0
    sell_failures: int = 0


@dataclass
class Fill:
    side: str                   # buy | sell
    address: str
    symbol: str
    qty: float
    price: float
    usd: float                  # spent (buy) or received (sell), after fees
    fee_usd: float
    impact_pct: float
    ts: float
    closed: bool = False
    pnl_usd: float | None = None
    multiple: float | None = None
