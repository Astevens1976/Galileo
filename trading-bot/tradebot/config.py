"""Bot configuration. Every number that changes behaviour lives here."""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, fields

MODES = ("sim", "paper", "live")


@dataclass
class Config:
    # sim   = synthetic market, fake money (works offline, fast-forward a night in seconds)
    # paper = REAL Solana market data (DexScreener + RugCheck), fake money
    # live  = REAL money, signed by your dedicated wallet (see README before using)
    mode: str = "sim"
    starting_balance_usdt: float = 1000.0   # paper/sim only; live uses your on-chain balance

    # --- chain / DEX -------------------------------------------------------------
    chain: str = "bsc"                    # DexScreener chain id: bsc | base | ethereum | arbitrum | solana
    allowed_dex_ids: list = field(default_factory=lambda: ["pancakeswap"])   # [] = any
    rpc_url: str = "https://bsc-dataseed.bnbchain.org"
    expected_chain_id: int = 56
    router_address: str = "0x10ED43C718714eb63d5aA57B78B54704E256024E"        # PancakeSwap V2
    quote_token_address: str = "0x55d398326f99059fF775485246999027B3197955"   # USDT (BEP-20)

    # --- live-trading guard rails ----------------------------------------------
    live_enabled: bool = False            # must ALSO be true in your config file to trade live
    max_live_trade_usd: float = 10.0
    max_live_daily_spend_usd: float = 50.0
    max_gas_gwei: float = 5.0
    min_gas_reserve_native: float = 0.005  # keep this much BNB for gas, always
    buy_slippage_pct: float = 8.0
    exit_slippage_pct: float = 20.0       # exits must go through in a falling market
    live_state_file: str = "live_state.json"
    kill_switch_file: str = "STOP"        # create this file to halt and sell everything

    # --- cheap pre-filters (no API cost) -------------------------------------
    min_liquidity_usd: float = 15_000
    max_market_cap_usd: float = 3_000_000
    min_age_min: float = 3
    max_age_min: float = 360
    min_volume_1h_usd: float = 10_000
    min_txns_5m: int = 20

    # --- signal agents --------------------------------------------------------
    min_buy_sell_ratio: float = 1.3      # whale watch: buys / sells over 5m
    min_volume_accel: float = 1.5        # sentiment proxy: 5m pace vs 1h average
    reevaluate_after_min: float = 5      # re-check a token that failed a soft signal

    # --- safety agents (fail closed) -----------------------------------------
    block_mint_authority: bool = True
    block_freeze_authority: bool = True
    block_lp_unlocked: bool = True
    max_top10_pct: float = 35
    max_tax_pct: float = 10               # EVM tokens can charge a tax on buys/sells
    min_lp_locked_pct: float = 50         # EVM: share of LP tokens locked or burned

    # --- sizing / portfolio risk ---------------------------------------------
    min_position_pct: float = 2.0
    max_position_pct: float = 4.0
    max_position_usd: float = 100.0
    min_trade_usd: float = 5.0
    max_pct_of_liquidity: float = 1.0    # never be more than 1% of the pool
    max_open_positions: int = 3
    max_daily_loss_pct: float = 15.0     # halt new entries for the rest of the day
    max_total_drawdown_pct: float = 40.0  # halt new entries for good

    # --- exits ------------------------------------------------------------------
    hard_stop_pct: float = 12.0
    trailing_stop_pct: float = 22.0
    trailing_activate_x: float = 1.3
    # [multiple, fraction of the *remaining* position to sell]
    take_profit_ladder: list = field(default_factory=lambda: [[2.0, 0.5], [5.0, 0.6]])
    time_stop_min: float = 45.0
    time_stop_min_x: float = 1.1
    rug_liquidity_usd: float = 2_000     # pool drained below this = treat as rug

    # --- costs (paper fills are deliberately pessimistic) ----------------------
    fee_pct: float = 1.0                 # DEX/aggregator fee per side
    network_fee_usd: float = 0.05        # priority fee + tx fee per side
    latency_slippage_pct: float = 1.0    # price moves against you while you land

    # --- runtime ---------------------------------------------------------------
    poll_seconds: float = 10.0           # paper mode API poll interval
    sim_tick_seconds: float = 15.0       # market time per sim tick
    sim_speed: float = 0.05              # real seconds per sim tick when dashboard is on
    sim_launches_per_min: float = 0.6
    seed: int | None = None
    dashboard_host: str = "127.0.0.1"
    dashboard_port: int = 8787
    log_dir: str = "logs"

    def validate(self) -> None:
        if self.mode not in MODES:
            raise ValueError(f"mode must be one of {MODES}, got {self.mode!r}")
        if not 0 < self.min_position_pct <= self.max_position_pct <= 25:
            raise ValueError("need 0 < min_position_pct <= max_position_pct <= 25")
        if self.starting_balance_usdt <= 0:
            raise ValueError("starting_balance_usdt must be positive")
        for lvl, frac in self.take_profit_ladder:
            if lvl <= 1 or not 0 < frac <= 1:
                raise ValueError(f"bad take_profit_ladder rung {[lvl, frac]}")

    def to_dict(self) -> dict:
        return asdict(self)


def load_config(path: str | None = None, **overrides) -> Config:
    data: dict = {}
    if path:
        with open(path) as f:
            data = json.load(f)
    data.update({k: v for k, v in overrides.items() if v is not None})
    known = {f.name for f in fields(Config)}
    unknown = set(data) - known
    if unknown:
        raise ValueError(f"unknown config keys: {sorted(unknown)}")
    cfg = Config(**data)
    cfg.validate()
    return cfg
