import pytest

from tradebot.__main__ import main
from tradebot.agents import LPAuditor, ProfitTaker, StopGuard
from tradebot.broker import LiveBroker, PaperBroker, pool_impact
from tradebot.config import Config, load_config
from tradebot.engine import Engine
from tradebot.feeds import SimFeed, parse_dexscreener_pairs, parse_rugcheck
from tradebot.models import SafetyReport, TokenSnapshot


def snap(price=1.0, liq=100_000, **kw):
    base = dict(address="A", symbol="TEST", price_usd=price, liquidity_usd=liq,
                market_cap_usd=500_000, age_min=30, buys_5m=60, sells_5m=30,
                volume_5m_usd=10_000, volume_1h_usd=40_000, price_change_5m_pct=5)
    base.update(kw)
    return TokenSnapshot(**base)


def test_round_trip_at_same_price_loses_money_to_costs():
    cfg = Config()
    b = PaperBroker(cfg)
    b.buy(snap(), 100, 0, "t")
    fill = b.sell("A", 1.0, snap(), 1)
    assert fill.closed and fill.pnl_usd < -2      # 2x 1% fee + 2x 1% latency + impact
    assert b.cash == pytest.approx(1000 + fill.pnl_usd)
    assert not b.positions


def test_price_impact_grows_with_size_vs_liquidity():
    assert pool_impact(100, 1_000_000) < 0.001
    assert pool_impact(10_000, 20_000) == pytest.approx(0.5)


def test_hard_stop_trailing_and_time_stop():
    cfg = Config()
    b = PaperBroker(cfg)
    b.buy(snap(), 50, 0, "t")
    p = b.positions["A"]
    g = StopGuard(cfg)
    assert g.check(p, snap(price=p.entry_price * 0.95), 60) is None
    assert "hard stop" in g.check(p, snap(price=p.entry_price * 0.87), 60)
    p.peak_price = p.entry_price * 1.5
    assert "trailing" in g.check(p, snap(price=p.peak_price * 0.77), 60)
    p.peak_price = p.entry_price
    assert "time stop" in g.check(p, snap(price=p.entry_price), 46 * 60)
    assert "liquidity" in g.check(p, snap(price=p.entry_price, liq=500), 60)


def test_profit_ladder_fires_each_rung_once():
    cfg = Config()
    b = PaperBroker(cfg)
    b.buy(snap(), 50, 0, "t")
    p = b.positions["A"]
    t = ProfitTaker(cfg)
    assert t.check(p, snap(price=p.entry_price * 1.9)) is None
    assert t.check(p, snap(price=p.entry_price * 2.1)) == (0.5, "TP 2x hit")
    assert t.check(p, snap(price=p.entry_price * 2.1)) is None
    assert t.check(p, snap(price=p.entry_price * 5.5))[0] == 0.6


def test_auditor_fails_closed_and_blocks_flags():
    a = LPAuditor(Config())
    assert not a.evaluate(snap(), SafetyReport(available=False))[0]
    assert not a.evaluate(snap(), SafetyReport(mint_authority=True))[0]
    assert not a.evaluate(snap(), SafetyReport(lp_unlocked=True))[0]
    assert a.evaluate(snap(), SafetyReport())[0]


def test_sim_engine_runs_and_respects_position_limit():
    cfg = Config(seed=3)
    e = Engine(cfg, SimFeed(cfg))
    for _ in range(1500):
        e.tick()
        assert len(e.broker.positions) <= cfg.max_open_positions
        assert e.broker.cash >= -1e-9
    s = e.state()
    assert s["stats"]["closed"] > 0
    assert s["equity"] > 0


def test_sim_is_reproducible_with_seed():
    def run():
        cfg = Config(seed=11)
        e = Engine(cfg, SimFeed(cfg))
        for _ in range(400):
            e.tick()
        return round(e.equity(), 6)
    assert run() == run()


def test_daily_loss_halt_stops_new_entries():
    cfg = Config(seed=1)
    e = Engine(cfg, SimFeed(cfg))
    e.tick()
    e.broker.cash = cfg.starting_balance_usdt * 0.5   # simulate a crash
    e.tick()
    assert e.halted
    before = e.stats["checked"]
    for _ in range(50):
        e.tick()
    assert e.stats["checked"] == before


def test_live_mode_is_locked(capsys):
    assert main(["--mode", "live", "--no-logs"]) == 2
    with pytest.raises(RuntimeError):
        LiveBroker(Config())


def test_config_rejects_unknown_keys():
    with pytest.raises(ValueError):
        load_config(None, not_a_setting=1)


def test_parse_dexscreener_picks_deepest_solana_pair():
    pairs = [
        {"chainId": "solana", "baseToken": {"address": "M1", "symbol": "AAA"}, "priceUsd": "0.001",
         "liquidity": {"usd": 5000}, "marketCap": 100000, "pairCreatedAt": 0,
         "txns": {"m5": {"buys": 10, "sells": 4}}, "volume": {"m5": 900, "h1": 5000}, "priceChange": {"m5": 3.5}},
        {"chainId": "solana", "baseToken": {"address": "M1", "symbol": "AAA"}, "priceUsd": "0.0011",
         "liquidity": {"usd": 50000}, "fdv": 110000, "pairCreatedAt": 60_000,
         "txns": {"m5": {"buys": 40, "sells": 20}}, "volume": {"m5": 9000, "h1": 30000}, "priceChange": {"m5": 7}},
        {"chainId": "base", "baseToken": {"address": "X"}, "priceUsd": "1"},
    ]
    out = parse_dexscreener_pairs(pairs, now=600)
    assert list(out) == ["M1"]
    s = out["M1"]
    assert s.liquidity_usd == 50000 and s.market_cap_usd == 110000 and s.buys_5m == 40
    assert s.age_min == pytest.approx(9)


def test_parse_rugcheck_flags():
    rep = parse_rugcheck({"risks": [
        {"name": "Freeze Authority still enabled", "level": "danger"},
        {"name": "Large Amount of LP Unlocked", "level": "danger"},
        {"name": "Top 10 holders high ownership", "level": "warn", "value": "41.5%"},
    ]})
    assert rep.freeze_authority and rep.lp_unlocked and not rep.mint_authority
    assert rep.top10_pct == pytest.approx(41.5)
    assert len(rep.dangers) == 2
