"""Market data feeds.

SimFeed       - synthetic Solana meme-coin market. Offline, seeded, fast.

DexScreenerFeed - real data on any DexScreener chain (prices, liquidity, flow)
                  + safety checks: RugCheck on Solana, GoPlus on EVM chains.

Both expose the same interface:
    advance()                -> move the clock (sim) / no-op (live data)
    now()                    -> unix seconds
    discover()               -> list of newly seen token addresses
    snapshot(addresses)      -> {address: TokenSnapshot}
    safety(address)          -> SafetyReport
"""
from __future__ import annotations

import json
import math
import random
import re
import time
import urllib.error
import urllib.request
from collections import deque

from .config import Config
from .models import SafetyReport, TokenSnapshot


class FeedError(Exception):
    pass


# ============================================================================
# Simulated market
# ============================================================================

_SYLLABLES = ["ZOR", "BLIP", "GRUN", "TAKO", "MIM", "FLOP", "WUBA", "KRAB", "NOOD",
              "SNEK", "PUFF", "GLOB", "YAP", "BORK", "DIZ", "LUMP", "QUAK", "FIZ",
              "MOOP", "ZAZA", "HONK", "PIX", "GOOB", "DRIP"]
_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


class _Rolling:
    """Running sum over the last `n` ticks."""

    def __init__(self, n: int):
        self.q: deque = deque()
        self.n = n
        self.total = 0.0

    def push(self, v: float) -> None:
        self.q.append(v)
        self.total += v
        if len(self.q) > self.n:
            self.total -= self.q.popleft()


class _SimToken:
    # fate: what actually happens to the token (hidden from the bot)
    FATES = (("rug", 45), ("fade", 42), ("pump", 11), ("moon", 2))

    def __init__(self, rng: random.Random, born: float, tick_s: float):
        self.rng = rng
        self.born = born
        self.address = "".join(rng.choice(_B58) for _ in range(44))
        self.symbol = rng.choice(_SYLLABLES) + rng.choice(_SYLLABLES + [""])
        self.fate = rng.choices([f for f, _ in self.FATES], [w for _, w in self.FATES])[0]
        mcap0 = rng.uniform(20e3, 150e3)
        self.supply = 1e9
        self.p0 = self.price = mcap0 / self.supply
        self.liq0 = mcap0 * rng.uniform(0.15, 0.3)
        self.liq_mult = 1.0
        self.activity = rng.uniform(0.5, 2.0)
        self.avg_trade = rng.uniform(60, 350)
        self.rugged = False

        u = rng.uniform
        if self.fate == "moon":
            self.run_min, self.run_drift, self.after_drift = u(30, 180), u(0.015, 0.04), -0.02
        elif self.fate == "pump":
            self.run_min, self.run_drift, self.after_drift = u(8, 60), u(0.006, 0.02), -0.025
        elif self.fate == "fade":
            self.run_min, self.run_drift, self.after_drift = u(3, 20), 0.01, -u(0.004, 0.015)
        else:  # rug: looks like a pump, then the pool gets pulled
            self.run_min, self.run_drift, self.after_drift = u(5, 40), u(0.005, 0.02), -0.01
        self.rug_at = self.run_min + u(0, 20)

        # Safety flags correlate with fate, but imperfectly - some rugs look clean.
        p_flag = {"rug": 0.65, "fade": 0.3, "pump": 0.12, "moon": 0.1}[self.fate]
        flags = rng.sample(["mint", "freeze", "lp"], rng.randint(1, 2)) if rng.random() < p_flag else []
        self.safety = SafetyReport(
            mint_authority="mint" in flags,
            freeze_authority="freeze" in flags,
            lp_unlocked="lp" in flags,
            top10_pct=rng.uniform(30, 70) if self.fate == "rug" else rng.uniform(12, 40),
        )
        for f, label in (("mint", "Mint authority still enabled"),
                         ("freeze", "Freeze authority still enabled"),
                         ("lp", "Large amount of LP unlocked")):
            if f in flags:
                self.safety.dangers.append(label)

        per5 = max(1, int(300 / tick_s))
        per60 = max(1, int(3600 / tick_s))
        self.buys5, self.sells5, self.vol5 = _Rolling(per5), _Rolling(per5), _Rolling(per5)
        self.vol60 = _Rolling(per60)
        self.prices = deque(maxlen=per5 + 1)
        self.prices.append(self.price)

    def drift(self, age_min: float) -> float:
        return self.run_drift if age_min < self.run_min else self.after_drift

    def step(self, now: float, dt_s: float) -> None:
        rng = self.rng
        age = (now - self.born) / 60
        dt_m = dt_s / 60
        d = self.drift(age)
        logret = d * dt_m + rng.gauss(0, 0.035 * math.sqrt(dt_m / 0.25))
        if self.fate == "rug" and not self.rugged and age >= self.rug_at:
            self.rugged = True
            pull = rng.uniform(0.01, 0.08)
            logret += math.log(pull)
            self.liq_mult *= pull
        if rng.random() < 0.002 * (dt_s / 15):        # random whale / dev dump
            logret += math.log(rng.uniform(0.6, 0.85))
        self.price *= math.exp(logret)

        perceived = d + rng.gauss(0, 0.04)             # flow is a noisy hint, not the truth
        buy_share = min(0.95, max(0.05, 1 / (1 + math.exp(-perceived * 60))))
        rate = self.activity * 8 * math.exp(-age / 240) * (1 + 30 * abs(d)) * (dt_s / 15)
        if self.rugged:
            buy_share, rate = 0.2, rate * 0.1
        n = max(0, int(rng.gauss(rate, math.sqrt(rate)) + 0.5))
        buys = min(n, max(0, round(n * buy_share + rng.gauss(0, math.sqrt(n * buy_share * (1 - buy_share) + 1e-9)))))
        vol = n * self.avg_trade * rng.uniform(0.6, 1.4)
        self.buys5.push(buys)
        self.sells5.push(n - buys)
        self.vol5.push(vol)
        self.vol60.push(vol)
        self.prices.append(self.price)

    def snapshot(self, now: float) -> TokenSnapshot:
        p_old = self.prices[0]
        return TokenSnapshot(
            address=self.address, symbol=self.symbol, price_usd=self.price,
            liquidity_usd=self.liq0 * math.sqrt(self.price / self.p0) * self.liq_mult,
            market_cap_usd=self.price * self.supply, age_min=(now - self.born) / 60,
            buys_5m=int(self.buys5.total), sells_5m=int(self.sells5.total),
            volume_5m_usd=self.vol5.total, volume_1h_usd=self.vol60.total,
            price_change_5m_pct=(self.price / p_old - 1) * 100 if p_old else 0.0,
        )


class SimFeed:
    name = "sim"

    def __init__(self, cfg: Config, start: float = 1_700_000_000.0):
        self.cfg = cfg
        self.rng = random.Random(cfg.seed)
        self.clock = start
        self.tokens: dict[str, _SimToken] = {}
        self._new: list[str] = []

    def now(self) -> float:
        return self.clock

    def advance(self) -> None:
        dt = self.cfg.sim_tick_seconds
        self.clock += dt
        lam = self.cfg.sim_launches_per_min * dt / 60
        n_new, p, limit = 0, self.rng.random(), math.exp(-lam)   # Poisson(lam), Knuth
        while p > limit:
            n_new += 1
            p *= self.rng.random()
        for _ in range(n_new):
            t = _SimToken(self.rng, self.clock, dt)
            self.tokens[t.address] = t
            self._new.append(t.address)
        cutoff = self.clock - 8 * 3600
        for addr in [a for a, t in self.tokens.items() if t.born < cutoff]:
            del self.tokens[addr]
        for t in self.tokens.values():
            t.step(self.clock, dt)

    def discover(self) -> list[str]:
        new, self._new = self._new, []
        return new

    def snapshot(self, addresses) -> dict[str, TokenSnapshot]:
        return {a: self.tokens[a].snapshot(self.clock) for a in addresses if a in self.tokens}

    def safety(self, address: str) -> SafetyReport:
        t = self.tokens.get(address)
        return t.safety if t else SafetyReport(available=False)


# ============================================================================
# Real market data: DexScreener + RugCheck (Solana) / GoPlus (EVM). No API keys.
# ============================================================================

class _Http:
    def __init__(self, min_interval_s: float = 1.1, timeout: float = 10):
        self.min_interval = min_interval_s
        self.timeout = timeout
        self._last: dict[str, float] = {}

    def get_json(self, url: str):
        host = url.split("/")[2]
        wait = self._last.get(host, 0) + self.min_interval - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self._last[host] = time.monotonic()
        req = urllib.request.Request(url, headers={"User-Agent": "paper-trading-bot/0.1",
                                                   "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return json.loads(r.read().decode())
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as e:
            raise FeedError(f"GET {url}: {e}") from e


def _f(v, default=0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def parse_dexscreener_pairs(pairs, now: float, chain: str = "solana",
                            allowed_dex=()) -> dict[str, TokenSnapshot]:
    """Best-liquidity tradable pair per base token -> TokenSnapshot.

    allowed_dex limits to DEXes we can execute on (e.g. pancakeswap); V3/V4
    pools are skipped because execution goes through a V2 router."""
    best: dict[str, tuple[float, dict]] = {}
    for p in pairs or []:
        if p.get("chainId") != chain:
            continue
        if allowed_dex and p.get("dexId") not in allowed_dex:
            continue
        if {"v3", "v4"} & {str(x).lower() for x in (p.get("labels") or [])}:
            continue
        addr = (p.get("baseToken") or {}).get("address")
        liq = _f((p.get("liquidity") or {}).get("usd"))
        if addr and (addr not in best or liq > best[addr][0]):
            best[addr] = (liq, p)
    out = {}
    for addr, (liq, p) in best.items():
        created = p.get("pairCreatedAt")
        m5 = (p.get("txns") or {}).get("m5") or {}
        vol = p.get("volume") or {}
        out[addr] = TokenSnapshot(
            address=addr,
            symbol=(p.get("baseToken") or {}).get("symbol") or addr[:6],
            price_usd=_f(p.get("priceUsd")),
            liquidity_usd=liq,
            market_cap_usd=_f(p.get("marketCap")) or _f(p.get("fdv")),
            age_min=(now * 1000 - created) / 60000 if created else 1e9,
            buys_5m=int(_f(m5.get("buys"))),
            sells_5m=int(_f(m5.get("sells"))),
            volume_5m_usd=_f(vol.get("m5")),
            volume_1h_usd=_f(vol.get("h1")),
            price_change_5m_pct=_f((p.get("priceChange") or {}).get("m5")),
            pair_address=p.get("pairAddress") or "",
            quote_address=(p.get("quoteToken") or {}).get("address") or "",
            dex_id=p.get("dexId") or "",
        )
    return out


_DEAD = {"0x0000000000000000000000000000000000000000", "0x000000000000000000000000000000000000dead"}


def parse_goplus(data: dict, address: str) -> SafetyReport:
    """GoPlus token security (EVM chains). Unknown answers count against the token."""
    res = (data or {}).get("result") or {}
    t = res.get(address.lower()) or (next(iter(res.values())) if res else None)
    if not t:
        return SafetyReport(available=False)

    def flag(k):
        return str(t.get(k, "0")) == "1"

    rep = SafetyReport(mint_authority=flag("is_mintable"), freeze_authority=flag("transfer_pausable"),
                       lp_unlocked=False)
    checks = [
        ("is_honeypot", "honeypot: cannot sell"),
        ("cannot_sell_all", "cannot sell full balance"),
        ("hidden_owner", "hidden owner"),
        ("can_take_back_ownership", "owner can reclaim ownership"),
        ("owner_change_balance", "owner can change balances"),
        ("selfdestruct", "contract can self-destruct"),
        ("slippage_modifiable", "owner can change the tax"),
        ("personal_slippage_modifiable", "owner can set per-wallet tax"),
        ("is_proxy", "upgradeable proxy contract"),
    ]
    for key, label in checks:
        if flag(key):
            rep.dangers.append(label)
    if str(t.get("is_open_source", "0")) != "1":
        rep.dangers.append("source code not verified")
    for key, label in (("is_blacklisted", "has blacklist"), ("trading_cooldown", "trading cooldown"),
                       ("external_call", "external calls")):
        if flag(key):
            rep.warnings.append(label)
    for key, attr in (("buy_tax", "buy_tax_pct"), ("sell_tax", "sell_tax_pct")):
        v = t.get(key)
        if v in (None, ""):
            rep.dangers.append(f"{key.replace('_', ' ')} unknown")
        else:
            setattr(rep, attr, _f(v) * 100)
    lp = t.get("lp_holders") or []
    if lp:
        rep.lp_locked_pct = 100 * sum(_f(h.get("percent")) for h in lp
                                      if str(h.get("is_locked")) == "1" or str(h.get("address", "")).lower() in _DEAD)
    holders = [h for h in (t.get("holders") or [])
               if str(h.get("is_contract")) != "1" and str(h.get("is_locked")) != "1"
               and str(h.get("address", "")).lower() not in _DEAD]
    if holders:
        rep.top10_pct = 100 * sum(_f(h.get("percent")) for h in holders[:10])
    return rep


def parse_rugcheck(data: dict) -> SafetyReport:
    rep = SafetyReport(mint_authority=False, freeze_authority=False, lp_unlocked=False)
    for r in (data or {}).get("risks") or []:
        name = str(r.get("name", ""))
        low = name.lower()
        level = str(r.get("level", "")).lower()
        if "mint authority" in low:
            rep.mint_authority = True
        if "freeze authority" in low:
            rep.freeze_authority = True
        if "lp" in low and "unlocked" in low:
            rep.lp_unlocked = True
        if "top 10" in low:
            m = re.search(r"([\d.]+)\s*%", str(r.get("value", "")))
            if m:
                rep.top10_pct = float(m.group(1))
        (rep.dangers if level == "danger" else rep.warnings).append(name)
    if rep.top10_pct is None and data and "topHolders" in data:
        rep.top10_pct = sum(_f(h.get("pct")) for h in (data["topHolders"] or [])[:10])
    return rep


class DexScreenerFeed:
    DEX = "https://api.dexscreener.com"
    RUG = "https://api.rugcheck.xyz/v1/tokens/{}/report/summary"
    GOPLUS = "https://api.gopluslabs.io/api/v1/token_security/{}?contract_addresses={}"
    GOPLUS_CHAIN = {"bsc": 56, "ethereum": 1, "base": 8453, "arbitrum": 42161, "polygon": 137}

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.chain = cfg.chain
        self.name = f"dexscreener/{cfg.chain}"
        if self.chain != "solana" and self.chain not in self.GOPLUS_CHAIN:
            raise ValueError(f"no safety-check source for chain {self.chain!r}")
        self.http = _Http()
        self._seen: set[str] = set()
        self._safety: dict[str, tuple[float, SafetyReport]] = {}

    def now(self) -> float:
        return time.time()

    def advance(self) -> None:
        pass

    def discover(self) -> list[str]:
        new = []
        for path in ("/token-profiles/latest/v1", "/token-boosts/latest/v1"):
            data = self.http.get_json(self.DEX + path)
            for item in data if isinstance(data, list) else []:
                addr = item.get("tokenAddress")
                if item.get("chainId") == self.chain and addr and addr not in self._seen:
                    self._seen.add(addr)
                    new.append(addr)
        return new

    def snapshot(self, addresses) -> dict[str, TokenSnapshot]:
        addresses = list(addresses)
        out: dict[str, TokenSnapshot] = {}
        for i in range(0, len(addresses), 30):
            chunk = ",".join(addresses[i:i + 30])
            data = self.http.get_json(f"{self.DEX}/tokens/v1/{self.chain}/{chunk}")
            pairs = data.get("pairs") if isinstance(data, dict) else data
            out.update(parse_dexscreener_pairs(pairs, self.now(), self.chain, self.cfg.allowed_dex_ids))
        return out

    def safety(self, address: str) -> SafetyReport:
        hit = self._safety.get(address)
        if hit and time.time() - hit[0] < 600:
            return hit[1]
        try:
            if self.chain == "solana":
                rep = parse_rugcheck(self.http.get_json(self.RUG.format(address)))
            else:
                url = self.GOPLUS.format(self.GOPLUS_CHAIN[self.chain], address)
                rep = parse_goplus(self.http.get_json(url), address)
        except FeedError:
            rep = SafetyReport(available=False)   # fail closed: auditor will block
        self._safety[address] = (time.time(), rep)
        return rep
