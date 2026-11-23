import pytest

from tradebot.__main__ import main
from tradebot.agents import HolderMap, LPAuditor, ProfitTaker, RiskSizer, StopGuard
from tradebot.broker import PaperBroker, pool_impact
from tradebot.config import Config, load_config
from tradebot.engine import Engine
from tradebot.feeds import SimFeed, parse_dexscreener_pairs, parse_goplus, parse_rugcheck
from tradebot.models import SafetyReport, TokenSnapshot


def snap(price=1.0, liq=100_000, **kw):
    base = dict(address="A", symbol="TEST", price_usd=price, liquidity_usd=liq,
                market_cap_usd=500_000, age_min=30, buys_5m=60, sells_5m=30,
                volume_5m_usd=10_000, volume_1h_usd=40_000, price_change_5m_pct=5)
    base.update(kw)
    return TokenSnapshot(**base)


def test_round_trip_at_same_price_loses_money_to_costs():
    cfg = Config(starting_balance_usdt=1000)
    b = PaperBroker(cfg)
    b.buy(snap(), 100, 0, "t")
    fill = b.sell("A", 1.0, snap(), 1)
    assert fill.closed and fill.pnl_usd < -2      # 2x 0.5% fee + 2x 1% latency + impact
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
    assert g.check(p, snap(price=p.peak_price * 0.77), 60) is None          # -23%: inside the 25% trail
    assert "trailing" in g.check(p, snap(price=p.peak_price * 0.74), 60)
    p.peak_price = p.entry_price
    assert g.check(p, snap(price=p.entry_price), 24 * 60) is None
    assert "time stop" in g.check(p, snap(price=p.entry_price), 26 * 60)
    assert "liquidity" in g.check(p, snap(price=p.entry_price, liq=500), 60)
    p.entry_liquidity = 100_000
    assert "LP pulled" in g.check(p, snap(price=p.entry_price, liq=45_000), 60)
    p.creator_balance_at_entry = 1000.0
    assert g.check(p, snap(price=p.entry_price), 60, dev_balance=900.0) is None
    assert "dev sold" in g.check(p, snap(price=p.entry_price), 60, dev_balance=500.0)


def test_profit_ladder_fires_each_rung_once():
    cfg = Config()
    b = PaperBroker(cfg)
    b.buy(snap(), 50, 0, "t")
    p = b.positions["A"]
    p.initial_qty = p.qty
    t = ProfitTaker(cfg)
    assert t.check(p, snap(price=p.entry_price * 1.9)) is None
    assert t.check(p, snap(price=p.entry_price * 2.1))[0] == pytest.approx(0.4)   # 40% of original
    assert t.check(p, snap(price=p.entry_price * 2.1)) is None
    p.qty *= 0.6
    assert t.check(p, snap(price=p.entry_price * 4.5))[0] == pytest.approx(0.5)   # 30% of original = half of what's left
    assert t.check(p, snap(price=p.entry_price * 9)) is None                      # last 30% is the runner


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


def test_live_mode_refuses_unless_enabled_in_config(capsys):
    assert main(["--mode", "live", "--no-logs"]) == 2
    assert "live_enabled" in capsys.readouterr().out


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
    out = parse_dexscreener_pairs(pairs, now=600, chain="solana")
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


def test_parse_dexscreener_keeps_only_executable_bsc_pools():
    def pair(dex, labels, liq):
        return {"chainId": "bsc", "dexId": dex, "labels": labels, "pairAddress": f"P{liq}",
                "baseToken": {"address": "0xT", "symbol": "T"}, "quoteToken": {"address": "0xQ"},
                "priceUsd": "1", "liquidity": {"usd": liq}}
    pairs = [pair("pancakeswap", ["v3"], 900_000), pair("uniswap", [], 800_000),
             pair("pancakeswap", ["v2"], 50_000)]
    s = parse_dexscreener_pairs(pairs, 0, "bsc", ["pancakeswap"], skip_v3=True)["0xT"]
    assert s.pair_address == "P50000" and s.quote_address == "0xQ" and s.dex_id == "pancakeswap"


def test_parse_goplus():
    clean = {"result": {"0xabc": {
        "is_open_source": "1", "is_honeypot": "0", "buy_tax": "0.03", "sell_tax": "0.05",
        "lp_holders": [{"address": "0x000000000000000000000000000000000000dead", "percent": "0.6"},
                       {"address": "0x1", "percent": "0.3", "is_locked": 1}],
        "holders": [{"address": "0xpair", "percent": "0.5", "is_contract": 1},
                    {"address": "0xw1", "percent": "0.08"}, {"address": "0xw2", "percent": "0.04"}]}}}
    r = parse_goplus(clean, "0xABC")
    assert r.available and not r.dangers
    assert r.buy_tax_pct == pytest.approx(3) and r.sell_tax_pct == pytest.approx(5)
    assert r.lp_locked_pct == pytest.approx(90) and r.top10_pct == pytest.approx(12)

    bad = {"result": {"0xabc": {"is_open_source": "0", "is_honeypot": "1", "buy_tax": "", "sell_tax": "0.4"}}}
    r = parse_goplus(bad, "0xabc")
    assert {"honeypot: cannot sell", "source code not verified", "buy tax unknown"} <= set(r.dangers)
    assert not LPAuditor(Config()).evaluate(snap(), r)[0]
    assert not parse_goplus({"result": {}}, "0xabc").available


def test_auditor_blocks_high_tax_and_unlocked_lp():
    a = LPAuditor(Config())
    assert "tax" in a.evaluate(snap(), SafetyReport(buy_tax_pct=2, sell_tax_pct=15))[1]
    assert "of LP burned/locked" in a.evaluate(snap(), SafetyReport(lp_locked_pct=20))[1]
    assert a.evaluate(snap(), SafetyReport(buy_tax_pct=2, sell_tax_pct=3, lp_locked_pct=95))[0]


def test_holder_map_rules():
    h = HolderMap(Config())
    assert "holders" in h.evaluate(snap(), SafetyReport(holder_count=120))[1]
    assert "dev wallet" in h.evaluate(snap(), SafetyReport(creator_pct=8))[1]
    assert "top 10" in h.evaluate(snap(), SafetyReport(top10_pct=40))[1]
    assert h.evaluate(snap(), SafetyReport(holder_count=900, creator_pct=2, top10_pct=20))[0]


def test_sizer_uses_20_to_40_pct_and_halves_after_two_losses():
    r = RiskSizer(Config())
    calm, wild = snap(price_change_5m_pct=0), snap(price_change_5m_pct=60)
    assert r.size(calm, 100, 100)[0] == pytest.approx(40)
    assert r.size(wild, 100, 100)[0] == pytest.approx(20)
    assert r.size(calm, 100, 100, loss_streak=2)[0] == pytest.approx(20)
    assert r.size(calm, 100, 100, loss_streak=1)[0] == pytest.approx(40)


def test_goplus_lp_lock_duration_dev_and_holders():
    now = 1_700_000_000
    data = {"result": {"0xabc": {
        "is_open_source": "1", "buy_tax": "0", "sell_tax": "0", "holder_count": "812",
        "creator_percent": "0.031", "creator_address": "0xDEV",
        "lp_holders": [
            {"address": "0x1", "percent": "0.5", "is_locked": 1,
             "locked_detail": [{"end_time": str(now + 7 * 86400)}]},          # only a week
            {"address": "0x2", "percent": "0.3", "is_locked": 1,
             "locked_detail": [{"end_time": "2030-01-01T00:00:00+00:00"}]}]}}}
    r = parse_goplus(data, "0xabc", now=now, min_lock_days=30)
    assert r.holder_count == 812 and r.creator_pct == pytest.approx(3.1) and r.creator_address == "0xDEV"
    assert r.lp_locked_pct == pytest.approx(80) and r.lp_locked_long_pct == pytest.approx(30)
    assert "burned/locked 30d+" in LPAuditor(Config()).evaluate(snap(), r)[1]


def test_no_chasing_and_two_signals_required():
    cfg = Config(min_liquidity_usd=0, min_volume_5m_usd=0, min_txns_5m=0, min_age_min=0)
    e = Engine(cfg, SimFeed(cfg))
    chased = snap(buys_5m=90, sells_5m=10, volume_5m_usd=50_000, volume_1h_usd=60_000,
                  price_change_1h_pct=400, price_change_5m_pct=2)
    e._evaluate(chased, 0)
    assert e.pipeline[-1]["verdict"] == "watching" and "no chase" in e.pipeline[-1]["why"]
    one_signal = snap(address="B", buys_5m=90, sells_5m=10, volume_5m_usd=5_000, volume_1h_usd=60_000)
    e._evaluate(one_signal, 0)
    assert e.pipeline[-1]["verdict"] == "watching" and not e.broker.positions


def test_drawdown_from_day_peak_pauses_entries():
    cfg = Config(seed=1)
    e = Engine(cfg, SimFeed(cfg))
    e.tick()
    e.broker.cash = cfg.starting_balance_usdt * 1.5      # new high for the day
    e.tick()
    e.broker.cash = cfg.starting_balance_usdt * 1.0      # -33% from that high
    e.tick()
    assert e.halted and "today's peak" in e.halt_reason
