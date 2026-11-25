"""On-chain self-test: one small buy and sell-back with your bot wallet.

Testnet (default): uses free test BNB, wraps a little into WBNB, finds a
PancakeSwap testnet pool, buys and sells back. Proves that your key, RPC,
approvals, swaps and receipts all work, without real money.

Mainnet: swaps $1 of USDT into WBNB and back (a deep, safe pool). Costs a few
cents of fees and gas.
"""
from __future__ import annotations

from .config import Config
from .evm import DexClient, SwapError


def run_selftest(cfg: Config, private_key: str, amount: float | None, token: str | None,
                 mainnet: bool = False, client: DexClient | None = None) -> int:
    c = client or DexClient.connect(cfg.rpc_url, private_key, cfg.router_address,
                                    cfg.quote_token_address, cfg.expected_chain_id, cfg.max_gas_gwei)
    amount = amount or (1.0 if mainnet else 0.001)
    qsym = c.erc20(c.quote).functions.symbol().call()
    print(f"wallet {c.address} · chain {c.w3.eth.chain_id}")
    print(f"balances: {c.native_balance():.5f} native · {c.quote_balance():.6f} {qsym}")
    qdec = c.decimals(c.quote)
    amount_raw = int(amount * 10 ** qdec)

    if c.native_balance() < 0.001:
        print("FAIL: wallet has no native coin for gas. " +
              ("Get free test BNB from the BNB Chain testnet faucet." if not mainnet else "Add a little BNB."))
        return 1
    if c.balance(c.quote) < amount_raw:
        if c.quote == c.wnative:
            print(f"wrapping {amount} native -> {qsym}")
            c.wrap_native(amount_raw)
        else:
            print(f"FAIL: need {amount} {qsym} in the wallet")
            return 1

    if token is None:
        token = c.wnative if mainnet and c.quote != c.wnative else c.find_test_pair(c.quote)
    if token is None:
        print(f"FAIL: no pool with {qsym} found among recent pairs; pass --token")
        return 1
    path = c.path_for(token)
    if not path:
        print(f"FAIL: no V2 route {qsym} -> {token}")
        return 1
    tsym = c.erc20(token).functions.symbol().call()
    try:
        got, tx1 = c.swap(path, amount_raw, slippage_pct=5)
        print(f"BUY  {amount} {qsym} -> {got / 10 ** c.decimals(token):.6g} {tsym}  tx {tx1}")
        back, tx2 = c.swap(list(reversed(path)), got, slippage_pct=5)
        print(f"SELL {got / 10 ** c.decimals(token):.6g} {tsym} -> {back / 10 ** qdec:.6g} {qsym}  tx {tx2}")
    except SwapError as e:
        print(f"FAIL: {e}")
        return 1
    print(f"round-trip cost: {(amount_raw - back) / 10 ** qdec:.6g} {qsym} in pool fees/impact "
          f"+ {c.gas_spent_wei / 1e18:.6f} native in gas")
    print("PASS: wallet, approvals, swaps and receipts all work.")
    return 0
