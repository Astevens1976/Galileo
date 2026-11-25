"""End-to-end tests of real on-chain execution.

Spins up an in-memory EVM, deploys the official Uniswap V2 factory/router/WETH
bytecode (PancakeSwap V2 is a fork of it) plus test tokens - a normal one, a
5%-tax one and a honeypot - and drives the actual LiveBroker / Engine code
against it with a freshly generated wallet key.
"""
import json
import os
import time

import pytest

web3 = pytest.importorskip("web3")
pytest.importorskip("eth_tester")

from eth_account import Account  # noqa: E402
from web3 import EthereumTesterProvider, Web3  # noqa: E402

from tradebot.broker import LiveBroker  # noqa: E402
from tradebot.config import Config  # noqa: E402
from tradebot.engine import Engine  # noqa: E402
from tradebot.evm import DexClient  # noqa: E402
from tradebot.models import SafetyReport, TokenSnapshot  # noqa: E402
from tradebot.selftest import run_selftest  # noqa: E402

FIX = os.path.join(os.path.dirname(__file__), "fixtures")
MAX = 2 ** 256 - 1
E18 = 10 ** 18


def _art(name):
    with open(os.path.join(FIX, name + ".json")) as f:
        return json.load(f)


class Chain:
    def __init__(self):
        self.w3 = w3 = Web3(EthereumTesterProvider())
        self.dep = w3.eth.accounts[0]
        self.factory = self.deploy("UniswapV2Factory", self.dep)
        self.weth = self.deploy("WETH9")
        self.router = self.deploy("UniswapV2Router02", self.factory.address, self.weth.address)
        self.usdt = self.deploy("TestToken", "Tether USD", "USDT", 18, 10 ** 12 * E18, 0, False)
        self.meme = self.deploy("TestToken", "Meme", "MEME", 9, 10 ** 18 * 10 ** 9, 0, False)
        self.tax = self.deploy("TestToken", "Taxed", "TAX", 18, 10 ** 12 * E18, 500, False)
        self.honey = self.deploy("TestToken", "Honey", "HONEY", 18, 10 ** 12 * E18, 0, True)
        self.tx(self.weth.functions.deposit(), value=5000 * E18)
        for t in (self.usdt, self.meme, self.tax, self.honey, self.weth):
            self.tx(t.functions.approve(self.router.address, MAX))
        self.add(self.usdt, self.weth, 600_000 * E18, 1000 * E18)          # WETH = $600
        self.add(self.weth, self.meme, 100 * E18, 10 ** 9 * 10 ** 9)        # MEME pairs with WETH only
        self.add(self.usdt, self.tax, 100_000 * E18, 10 ** 8 * E18)
        self.add(self.usdt, self.honey, 100_000 * E18, 10 ** 8 * E18)
        self.tx(self.honey.functions.setPair(
            self.factory.functions.getPair(self.usdt.address, self.honey.address).call()))

        self.bot = Account.create()
        w3.eth.send_transaction({"from": self.dep, "to": self.bot.address, "value": 10 * E18})
        self.tx(self.usdt.functions.transfer(self.bot.address, 1000 * E18))
        self.client = DexClient(w3, self.bot.key.hex(), self.router.address, self.usdt.address)

    def deploy(self, name, *args):
        a = _art(name)
        c = self.w3.eth.contract(abi=a["abi"], bytecode=a["bytecode"])
        r = self.w3.eth.wait_for_transaction_receipt(c.constructor(*args).transact({"from": self.dep}))
        return self.w3.eth.contract(address=r.contractAddress, abi=a["abi"])

    def tx(self, fn, value=0):
        return self.w3.eth.wait_for_transaction_receipt(fn.transact({"from": self.dep, "value": value}))

    def add(self, a, b, amt_a, amt_b):
        self.tx(self.router.functions.addLiquidity(a.address, b.address, amt_a, amt_b, 0, 0, self.dep, 2 ** 40))

    def swap_dep(self, path, amount_in):
        self.tx(self.router.functions.swapExactTokensForTokens(
            amount_in, 0, [p.address for p in path], self.dep, 2 ** 40))

    def price_usd(self, token, path_to_usdt):
        lot = 10 ** 6 * 10 ** token.functions.decimals().call()     # quote a big lot: tiny prices
        out = self.router.functions.getAmountsOut(lot, [p.address for p in path_to_usdt]).call()[-1]
        return out / E18 / 10 ** 6

    def snap(self, token, path_to_usdt, **kw):
        base = dict(address=token.address, symbol=token.functions.symbol().call(),
                    price_usd=self.price_usd(token, path_to_usdt), liquidity_usd=120_000,
                    market_cap_usd=500_000, age_min=30, buys_5m=50, sells_5m=10,
                    volume_5m_usd=5000, volume_1h_usd=20000, price_change_5m_pct=3)
        base.update(kw)
        return TokenSnapshot(**base)


@pytest.fixture()
def chain():
    return Chain()


@pytest.fixture()
def cfg(tmp_path):
    return Config(mode="live", live_enabled=True, max_live_trade_usd=10, max_live_daily_spend_usd=100,
                  live_state_file=str(tmp_path / "state.json"), kill_switch_file=str(tmp_path / "STOP"))


def test_routes_only_through_existing_v2_pools(chain):
    c = chain.client
    assert c.path_for(chain.tax.address) == [chain.usdt.address, chain.tax.address]
    assert c.path_for(chain.meme.address) == [chain.usdt.address, chain.weth.address, chain.meme.address]
    assert c.path_for(Account.create().address) is None


def test_buy_partial_sell_full_sell_on_chain(chain, cfg):
    b = LiveBroker(cfg, chain.client)
    s = chain.snap(chain.meme, [chain.meme, chain.weth, chain.usdt])
    assert b.can_trade(s) == (True, "")
    usdt_before = chain.usdt.functions.balanceOf(chain.bot.address).call()

    fill = b.buy(s, 50, time.time(), "test")              # asks 50, capped to 10
    assert fill and fill.usd == pytest.approx(10)
    assert usdt_before - chain.usdt.functions.balanceOf(chain.bot.address).call() == 10 * E18
    held = chain.meme.functions.balanceOf(chain.bot.address).call()
    assert held > 0 and b.raw[chain.meme.address] == held
    assert chain.usdt.functions.allowance(chain.bot.address, chain.router.address).call() == 0  # exact approval
    # entry includes the test chain's (expensive) gas, so it sits a few % above market
    assert b.positions[chain.meme.address].entry_price == pytest.approx(s.price_usd, rel=0.06)

    part = b.sell(chain.meme.address, 0.5, s, time.time())
    assert part and not part.closed
    assert chain.meme.functions.balanceOf(chain.bot.address).call() == held - held // 2

    last = b.sell(chain.meme.address, 1.0, s, time.time())
    assert last.closed and chain.meme.address not in b.positions
    assert chain.meme.functions.balanceOf(chain.bot.address).call() == 0
    assert -1.0 < last.pnl_usd < 0      # 4 pool fees (2-hop route, both ways) + gas, nothing else
    assert b.total_fees_usd > 0


def test_tax_token_is_traded_with_tax_aware_min_out(chain, cfg):
    b = LiveBroker(cfg, chain.client)
    s = chain.snap(chain.tax, [chain.tax, chain.usdt])
    b.remember_safety(chain.tax.address, SafetyReport(buy_tax_pct=5, sell_tax_pct=5))
    assert b.can_trade(s)[0]
    assert b.buy(s, 10, time.time(), "t")
    fill = b.sell(chain.tax.address, 1.0, s, time.time())
    assert fill.closed and 0.85 < fill.multiple < 0.92   # two 5% taxes + fees


def test_slippage_guard_refuses_bad_fill_without_sending(chain, cfg):
    cfg.buy_slippage_pct = 1.0                           # tax not known -> 5% short -> must refuse
    b = LiveBroker(cfg, chain.client)
    s = chain.snap(chain.tax, [chain.tax, chain.usdt])
    assert b.can_trade(s)[0]
    gas_before = chain.client.gas_spent_wei
    assert b.buy(s, 10, time.time(), "t") is None
    assert "dry run failed" in b.last_error
    assert chain.tax.functions.balanceOf(chain.bot.address).call() == 0
    # only the exact approval was mined; the swap itself was never sent
    assert chain.client.gas_spent_wei - gas_before < 100_000 * chain.w3.eth.gas_price


def test_honeypot_sell_is_caught_by_dry_run(chain, cfg):
    b = LiveBroker(cfg, chain.client)
    s = chain.snap(chain.honey, [chain.honey, chain.usdt])
    assert b.can_trade(s)[0]
    assert b.buy(s, 10, time.time(), "t")
    gas_before = chain.client.gas_spent_wei
    assert b.sell(chain.honey.address, 1.0, s, time.time()) is None
    assert "dry run failed" in b.last_error
    assert b.positions[chain.honey.address].sell_failures == 1
    assert chain.client.gas_spent_wei - gas_before < 100_000 * chain.w3.eth.gas_price


def test_daily_spend_cap(chain, cfg):
    cfg.max_live_daily_spend_usd = 15
    b = LiveBroker(cfg, chain.client)
    toks = [(chain.meme, [chain.meme, chain.weth, chain.usdt]), (chain.tax, [chain.tax, chain.usdt]),
            (chain.weth, [chain.weth, chain.usdt])]
    spent = []
    for tok, path in toks:
        s = chain.snap(tok, path)
        b.remember_safety(tok.address, SafetyReport(buy_tax_pct=5 if tok is chain.tax else 0))
        assert b.can_trade(s)[0]
        f = b.buy(s, 10, time.time(), "t")
        spent.append(f.usd if f else None)
    assert spent == [10, 5, None]
    assert "daily" in b.last_error


def test_positions_survive_restart(chain, cfg):
    b = LiveBroker(cfg, chain.client)
    s = chain.snap(chain.meme, [chain.meme, chain.weth, chain.usdt])
    b.can_trade(s)
    b.buy(s, 10, time.time(), "t")
    again = LiveBroker(cfg, chain.client)
    assert chain.meme.address in again.positions
    assert again.raw[chain.meme.address] == b.raw[chain.meme.address]
    assert again.day_spent == 10
    assert again.sell(chain.meme.address, 1.0, s, time.time()).closed


class ChainFeed:
    """Feed whose prices come from the on-chain pools."""
    name = "testchain"

    def __init__(self, chain):
        self.c = chain

    def now(self):
        return time.time()

    def advance(self):
        pass

    def discover(self):
        return [self.c.meme.address]

    def snapshot(self, addrs):
        return {self.c.meme.address: self.c.snap(self.c.meme, [self.c.meme, self.c.weth, self.c.usdt])}

    def safety(self, addr):
        return SafetyReport(mint_authority=False, freeze_authority=False, lp_unlocked=False, top10_pct=10)


def _engine(chain, cfg):
    for k in ("min_liquidity_usd", "min_age_min", "min_volume_5m_usd", "min_txns_5m",
              "min_buy_share", "min_volume_spike"):
        setattr(cfg, k, 0)
    b = LiveBroker(cfg, chain.client)
    cfg.starting_balance_usdt = b.equity({})
    return Engine(cfg, ChainFeed(chain), broker=b)


def test_engine_end_to_end_buy_take_profit_then_trailing_stop(chain, cfg):
    cfg.entry_parts = 1
    e = _engine(chain, cfg)
    e.tick()
    assert chain.meme.address in e.broker.positions, e.state()["pipeline"]
    pos = e.broker.positions[chain.meme.address]

    chain.swap_dep([chain.usdt, chain.weth, chain.meme], 40_000 * E18)    # someone pumps it >2x
    e.tick()
    assert pos.ladder_hits == 1 and chain.meme.address in e.broker.positions

    held = chain.meme.functions.balanceOf(chain.dep).call()
    chain.swap_dep([chain.meme, chain.weth, chain.usdt], held // 1000)   # and dumps it
    e.tick()
    assert chain.meme.address not in e.broker.positions
    st = e.state()["stats"]
    assert st["trades"] == 1 and st["wins"] == 1
    assert chain.meme.functions.balanceOf(chain.bot.address).call() == 0


def test_kill_switch_sells_everything_and_halts(chain, cfg):
    e = _engine(chain, cfg)
    e.tick()
    assert e.broker.positions
    open(cfg.kill_switch_file, "w").close()
    e.tick()
    assert not e.broker.positions and e.halted
    assert chain.meme.functions.balanceOf(chain.bot.address).call() == 0


def test_selftest_round_trip(chain, cfg, capsys):
    assert run_selftest(cfg, "", 5.0, chain.meme.address, client=chain.client) == 0
    assert "PASS" in capsys.readouterr().out


def test_entry_is_split_into_two_buys(chain, cfg):
    e = _engine(chain, cfg)            # entry_parts = 2 by default
    e.tick()
    first = e.broker.raw[chain.meme.address]
    assert e.broker.positions[chain.meme.address].parts_left == 1
    e.tick()                            # price unchanged -> second part goes in
    pos = e.broker.positions[chain.meme.address]
    assert pos.parts_left == 0
    assert e.broker.raw[chain.meme.address] == pytest.approx(2 * first, rel=0.01)
    assert chain.meme.functions.balanceOf(chain.bot.address).call() == e.broker.raw[chain.meme.address]
    assert pos.initial_qty == pytest.approx(pos.qty)
