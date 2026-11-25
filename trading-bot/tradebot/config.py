"""Bot configuration. Every number that changes behaviour lives here.

Defaults follow the "overnight fresh-meme" rule set (scanner needs 2 signals,
rug check must pass, 20-40% sizing, 2-part entries, 40/30/30 profit ladder,
-12% hard stop, 25 min time stop, -30% from the day's peak pauses trading)
and trade on BNB Chain with USDT through PancakeSwap V2.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, fields

MODES = ("sim", "paper", "live")

# Chain-specific settings. Picking a chain fills these in unless your config
# file sets them explicitly.
CHAIN_PRESETS = {
    "bsc": dict(
        rpc_url="https://bsc-dataseed.bnbchain.org", expected_chain_id=56, native_symbol="BNB",
        quote_token_address="0x55d398326f99059fF775485246999027B3197955",   # USDT (BEP-20)
        router_address="0x10ED43C718714eb63d5aA57B78B54704E256024E",        # PancakeSwap V2
        allowed_dex_ids=["pancakeswap"], max_gas_gwei=5.0, min_gas_reserve_native=0.005),
    "solana": dict(allowed_dex_ids=[], native_symbol="SOL"),   # paper trading only
}


@dataclass
class Config:
    # sim   = synthetic market, fake money (works offline, fast-forward a night in seconds)
    # paper = REAL market data (DexScreener + GoPlus/RugCheck), fake money
    # live  = REAL money, signed by your dedicated wallet (see README before using)
    mode: str = "sim"
    starting_balance_usdt: float = 100.0   # paper/sim only; live uses your on-chain balance

    # --- chain / execution (see CHAIN_PRESETS) ------------------------------------
    chain: str = "bsc"
    allowed_dex_ids: list = field(default_factory=lambda: ["pancakeswap"])
    rpc_url: str = "https://bsc-dataseed.bnbchain.org"
    expected_chain_id: int = 56
    native_symbol: str = "BNB"
    quote_token_address: str = "0x55d398326f99059fF775485246999027B3197955"   # USDT (BEP-20)
    router_address: str = "0x10ED43C718714eb63d5aA57B78B54704E256024E"        # PancakeSwap V2

    # --- live-trading guard rails ----------------------------------------------
    live_enabled: bool = False            # must ALSO be true in your config file to trade live
    max_live_trade_usd: float = 50.0      # hard ceiling per buy, whatever the % sizing says
    max_live_daily_spend_usd: float = 300.0
    max_gas_gwei: float = 5.0
    min_gas_reserve_native: float = 0.005  # always keep this much BNB for gas
    buy_slippage_pct: float = 2.5
    take_profit_slippage_pct: float = 2.5
    exit_slippage_pct: float = 15.0       # stop-loss exits must go through in a falling market
    live_state_file: str = "live_state.json"
    kill_switch_file: str = "STOP"        # create this file to halt and sell everything

    # --- universe: cheap pre-filters (no API cost) ------------------------------
    min_liquidity_usd: float = 40_000
    min_volume_5m_usd: float = 25_000
    max_market_cap_usd: float = 10_000_000
    min_age_min: float = 3
    max_age_min: float = 360              # launched or migrated in the last 6 hours
    min_txns_5m: int = 20

    # --- scanner signals (flags only; entry needs min_signals of them) -----------
    min_buy_share: float = 0.70           # buys as a share of 5m transactions
    min_volume_spike: float = 5.0         # 5m volume pace vs 1h average
    min_signals: int = 2
    max_chase_1h_pct: float = 300         # skip if up this much in 1h...
    chase_pullback_pct: float = 10        # ...unless it has pulled back at least this much in 5m
    reevaluate_after_min: float = 5

    # --- rug check (fail closed: nothing executes without a PASS) --------------
    block_mint_authority: bool = True
    block_freeze_authority: bool = True
    block_lp_unlocked: bool = True
    min_lp_locked_pct: float = 50         # share of LP burned or locked...
    min_lp_lock_days: float = 30          # ...for at least this long
    max_top10_pct: float = 35
    max_creator_pct: float = 5            # dev wallet share of supply
    min_holders: int = 300
    max_tax_pct: float = 10

    # --- sizer -------------------------------------------------------------------
    min_position_pct: float = 20.0
    max_position_pct: float = 40.0
    max_position_usd: float = 1_000_000.0  # paper cap; live is capped by max_live_trade_usd
    min_trade_usd: float = 1.0
    max_pct_of_liquidity: float = 1.0     # never be more than 1% of the pool
    max_open_positions: int = 2
    loss_streak_len: int = 2              # after this many losses in a row...
    loss_streak_size_mult: float = 0.5    # ...size is multiplied by this until a win
    entry_parts: int = 2                  # split each entry into this many buys

    # --- portfolio stops ---------------------------------------------------------
    max_drawdown_from_day_peak_pct: float = 30.0   # pause new entries for the rest of the UTC day
    max_total_drawdown_pct: float = 60.0           # pause new entries for good

    # --- executor exits ----------------------------------------------------------
    hard_stop_pct: float = 12.0
    trailing_stop_pct: float = 25.0
    trailing_activate_x: float = 1.3
    # [multiple, fraction of the ORIGINAL position to sell]; the rest is the runner
    take_profit_ladder: list = field(default_factory=lambda: [[2.0, 0.4], [4.0, 0.3]])
    time_stop_min: float = 25.0
    time_stop_min_x: float = 1.1
    dev_sell_exit_pct: float = 20.0       # exit if the dev wallet sells this much of its bag
    liquidity_drop_exit_pct: float = 50.0  # exit if pool liquidity falls this much since entry
    rug_liquidity_usd: float = 2_000

    # --- paper-fill costs (deliberately pessimistic) -----------------------------
    fee_pct: float = 0.5                  # PancakeSwap V2 0.25% per side + buffer
    network_fee_usd: float = 0.05
    latency_slippage_pct: float = 1.0

    # --- runtime ---------------------------------------------------------------
    poll_seconds: float = 10.0
    sim_tick_seconds: float = 15.0
    sim_speed: float = 0.05
    sim_launches_per_min: float = 0.6
    seed: int | None = None
    dashboard_host: str = "127.0.0.1"
    dashboard_port: int = 8787
    log_dir: str = "logs"

    def validate(self) -> None:
        if self.mode not in MODES:
            raise ValueError(f"mode must be one of {MODES}, got {self.mode!r}")
        if not 0 < self.min_position_pct <= self.max_position_pct <= 100:
            raise ValueError("need 0 < min_position_pct <= max_position_pct <= 100")
        if self.starting_balance_usdt <= 0:
            raise ValueError("starting_balance_usdt must be positive")
        if sum(f for _, f in self.take_profit_ladder) > 1:
            raise ValueError("take_profit_ladder fractions add up to more than 100%")
        for lvl, frac in self.take_profit_ladder:
            if lvl <= 1 or not 0 < frac <= 1:
                raise ValueError(f"bad take_profit_ladder rung {[lvl, frac]}")
        if self.entry_parts < 1:
            raise ValueError("entry_parts must be >= 1")

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
    chain = data.get("chain", Config.chain)
    if chain not in CHAIN_PRESETS:
        raise ValueError(f"chain must be one of {sorted(CHAIN_PRESETS)}")
    cfg = Config(**{**CHAIN_PRESETS[chain], **data})
    cfg.validate()
    return cfg
