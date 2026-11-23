"""On-chain execution for EVM chains (BNB Chain by default) through a
Uniswap-V2-style router (PancakeSwap V2 on BNB Chain).

The bot signs with the private key of a DEDICATED wallet account (create a new
account in MetaMask, fund it with your trading budget only, export its key).
No exchange login, no custody: the key never leaves your computer.

Safety built in here:
  * chain id is checked against the config before anything is sent
  * every swap is gas-estimated first; a swap that would revert is never sent
  * min-out (slippage) protection on every swap, adjusted for token taxes
  * exact-amount approvals only (no unlimited allowances)
  * gas price cap
"""
from __future__ import annotations

import os
import time

ERC20_ABI = [
    {"name": "balanceOf", "type": "function", "stateMutability": "view",
     "inputs": [{"name": "a", "type": "address"}], "outputs": [{"name": "", "type": "uint256"}]},
    {"name": "decimals", "type": "function", "stateMutability": "view",
     "inputs": [], "outputs": [{"name": "", "type": "uint8"}]},
    {"name": "symbol", "type": "function", "stateMutability": "view",
     "inputs": [], "outputs": [{"name": "", "type": "string"}]},
    {"name": "allowance", "type": "function", "stateMutability": "view",
     "inputs": [{"name": "o", "type": "address"}, {"name": "s", "type": "address"}],
     "outputs": [{"name": "", "type": "uint256"}]},
    {"name": "approve", "type": "function", "stateMutability": "nonpayable",
     "inputs": [{"name": "s", "type": "address"}, {"name": "v", "type": "uint256"}],
     "outputs": [{"name": "", "type": "bool"}]},
    {"name": "deposit", "type": "function", "stateMutability": "payable", "inputs": [], "outputs": []},
]
ROUTER_ABI = [
    {"name": "factory", "type": "function", "stateMutability": "view", "inputs": [],
     "outputs": [{"name": "", "type": "address"}]},
    {"name": "WETH", "type": "function", "stateMutability": "view", "inputs": [],
     "outputs": [{"name": "", "type": "address"}]},
    {"name": "getAmountsOut", "type": "function", "stateMutability": "view",
     "inputs": [{"name": "amountIn", "type": "uint256"}, {"name": "path", "type": "address[]"}],
     "outputs": [{"name": "", "type": "uint256[]"}]},
    {"name": "swapExactTokensForTokensSupportingFeeOnTransferTokens", "type": "function",
     "stateMutability": "nonpayable",
     "inputs": [{"name": "amountIn", "type": "uint256"}, {"name": "amountOutMin", "type": "uint256"},
                {"name": "path", "type": "address[]"}, {"name": "to", "type": "address"},
                {"name": "deadline", "type": "uint256"}], "outputs": []},
]
FACTORY_ABI = [
    {"name": "getPair", "type": "function", "stateMutability": "view",
     "inputs": [{"name": "a", "type": "address"}, {"name": "b", "type": "address"}],
     "outputs": [{"name": "", "type": "address"}]},
    {"name": "allPairsLength", "type": "function", "stateMutability": "view", "inputs": [],
     "outputs": [{"name": "", "type": "uint256"}]},
    {"name": "allPairs", "type": "function", "stateMutability": "view",
     "inputs": [{"name": "i", "type": "uint256"}], "outputs": [{"name": "", "type": "address"}]},
]
PAIR_ABI = [
    {"name": "token0", "type": "function", "stateMutability": "view", "inputs": [],
     "outputs": [{"name": "", "type": "address"}]},
    {"name": "token1", "type": "function", "stateMutability": "view", "inputs": [],
     "outputs": [{"name": "", "type": "address"}]},
    {"name": "getReserves", "type": "function", "stateMutability": "view", "inputs": [],
     "outputs": [{"name": "r0", "type": "uint112"}, {"name": "r1", "type": "uint112"},
                 {"name": "t", "type": "uint32"}]},
]
ZERO = "0x0000000000000000000000000000000000000000"


class SwapError(Exception):
    pass


def load_private_key(env_file: str = ".env") -> str:
    """BOT_PRIVATE_KEY from the environment, or from a local .env file."""
    key = os.environ.get("BOT_PRIVATE_KEY", "").strip()
    if not key and os.path.exists(env_file):
        with open(env_file) as f:
            for line in f:
                name, _, value = line.strip().partition("=")
                if name.strip() == "BOT_PRIVATE_KEY":
                    key = value.strip().strip('"').strip("'")
    if not key:
        raise SystemExit("No BOT_PRIVATE_KEY found (environment variable or .env file). See README.")
    return key


class DexClient:
    def __init__(self, w3, private_key: str, router: str, quote_token: str,
                 max_gas_gwei: float = 5.0, receipt_timeout: int = 120):
        from eth_account import Account
        self.w3 = w3
        self.acct = Account.from_key(private_key)
        self.address = self.acct.address
        cs = w3.to_checksum_address
        self.router = w3.eth.contract(address=cs(router), abi=ROUTER_ABI)
        self.factory = w3.eth.contract(address=self.router.functions.factory().call(), abi=FACTORY_ABI)
        self.wnative = self.router.functions.WETH().call()
        self.quote = self.wnative if quote_token.upper() == "WNATIVE" else cs(quote_token)
        self.max_gas_wei = int(max_gas_gwei * 1e9)
        self.receipt_timeout = receipt_timeout
        self._dec: dict[str, int] = {}
        self.gas_spent_wei = 0

    @classmethod
    def connect(cls, rpc_url: str, private_key: str, router: str, quote_token: str,
                expected_chain_id: int, max_gas_gwei: float = 5.0) -> "DexClient":
        from web3 import Web3
        from web3.middleware import ExtraDataToPOAMiddleware
        w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 20}))
        w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
        cid = w3.eth.chain_id
        if cid != expected_chain_id:
            raise SystemExit(f"RPC is chain {cid}, config expects {expected_chain_id}. Refusing to trade.")
        return cls(w3, private_key, router, quote_token, max_gas_gwei)

    # ------------------------------------------------------------- reads
    def erc20(self, token: str):
        return self.w3.eth.contract(address=self.w3.to_checksum_address(token), abi=ERC20_ABI)

    def decimals(self, token: str) -> int:
        token = self.w3.to_checksum_address(token)
        if token not in self._dec:
            self._dec[token] = self.erc20(token).functions.decimals().call()
        return self._dec[token]

    def balance(self, token: str) -> int:
        return self.erc20(token).functions.balanceOf(self.address).call()

    def quote_balance(self) -> float:
        return self.balance(self.quote) / 10 ** self.decimals(self.quote)

    def native_balance(self) -> float:
        return self.w3.eth.get_balance(self.address) / 1e18

    def native_price_in_quote(self) -> float:
        if self.wnative == self.quote:
            return 1.0
        try:
            out = self.router.functions.getAmountsOut(10 ** 18, [self.wnative, self.quote]).call()
            return out[-1] / 10 ** self.decimals(self.quote)
        except Exception:
            return 0.0

    def path_for(self, token: str, pair_quote: str | None = None) -> list[str] | None:
        """Route quote -> token through a V2 pool that actually exists on this router's factory."""
        cs = self.w3.to_checksum_address
        token = cs(token)
        candidates = [[self.quote, token]]
        if self.wnative != self.quote:
            candidates.append([self.quote, self.wnative, token])
        if pair_quote and cs(pair_quote) not in (self.quote, self.wnative):
            candidates.append([self.quote, cs(pair_quote), token])
        for path in candidates:
            if all(self.factory.functions.getPair(a, b).call() != ZERO for a, b in zip(path, path[1:])):
                return path
        return None

    # ------------------------------------------------------------ writes
    def _send(self, fn, value: int = 0) -> dict:
        gas_price = min(self.w3.eth.gas_price, self.max_gas_wei)
        tx = {"from": self.address, "value": value, "chainId": self.w3.eth.chain_id}
        try:
            gas = fn.estimate_gas(tx)          # doubles as a dry run: reverts are caught here
        except Exception as e:
            raise SwapError(f"dry run failed, not sending: {str(e)[:160]}") from e
        tx.update(gas=int(gas * 1.25), gasPrice=gas_price,
                  nonce=self.w3.eth.get_transaction_count(self.address, "pending"))
        signed = self.acct.sign_transaction(fn.build_transaction(tx))
        h = self.w3.eth.send_raw_transaction(signed.raw_transaction)
        r = self.w3.eth.wait_for_transaction_receipt(h, timeout=self.receipt_timeout)
        self.gas_spent_wei += r["gasUsed"] * r.get("effectiveGasPrice", gas_price)
        if r["status"] != 1:
            raise SwapError(f"transaction {h.hex()} reverted")
        return r

    def _approve_exact(self, token: str, amount: int) -> None:
        c = self.erc20(token)
        if c.functions.allowance(self.address, self.router.address).call() < amount:
            self._send(c.functions.approve(self.router.address, amount))

    def wrap_native(self, wei: int) -> None:
        self._send(self.erc20(self.wnative).functions.deposit(), value=wei)

    def swap(self, path: list[str], amount_in: int, slippage_pct: float, tax_pct: float = 0.0) -> tuple[int, str]:
        """Exact-in swap along path. Returns (amount received, tx hash)."""
        if amount_in <= 0:
            raise SwapError("amount is zero")
        expected = self.router.functions.getAmountsOut(amount_in, path).call()[-1]
        min_out = int(expected * (1 - tax_pct / 100) * (1 - slippage_pct / 100))
        if min_out <= 0:
            raise SwapError("quote is zero - no liquidity")
        self._approve_exact(path[0], amount_in)
        out_token = self.erc20(path[-1])
        before = out_token.functions.balanceOf(self.address).call()
        r = self._send(self.router.functions.swapExactTokensForTokensSupportingFeeOnTransferTokens(
            amount_in, min_out, path, self.address, int(time.time()) + 300))
        received = out_token.functions.balanceOf(self.address).call() - before
        return received, r["transactionHash"].hex()

    # ------------------------------------------------------------ helpers
    def find_test_pair(self, base: str, lookback: int = 300, min_base_reserve: int = 10 ** 15) -> str | None:
        """Most recent pool pairing `base` with something, with some liquidity (for self-tests)."""
        base = self.w3.to_checksum_address(base)
        n = self.factory.functions.allPairsLength().call()
        for i in range(n - 1, max(-1, n - 1 - lookback), -1):
            pair = self.w3.eth.contract(address=self.factory.functions.allPairs(i).call(), abi=PAIR_ABI)
            t0, t1 = pair.functions.token0().call(), pair.functions.token1().call()
            if base not in (t0, t1):
                continue
            r0, r1, _ = pair.functions.getReserves().call()
            if (r0 if t0 == base else r1) >= min_base_reserve:
                return t1 if t0 == base else t0
        return None
