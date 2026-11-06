"""Price, market cap and liquidity of an Elysium token, from on-chain quotes (dex.py, same as the trade bot)."""
from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass

import config as C
from dex import Dex, NoPool, Route, Token
from evm import Rpc, RpcError
import links

log = logging.getLogger("market")


@dataclass
class Snap:
    token: Token
    route: Route | None
    price_hype: float        # HYPE per whole token
    mc_hype: float
    liq_hype: float          # HYPE in the pool, 0 if unknown
    hype_usd: float
    pad: str | None

    @property
    def price_usd(self) -> float:
        return self.price_hype * self.hype_usd

    @property
    def mc(self) -> float:
        return self.mc_hype * self.hype_usd

    @property
    def liq(self) -> float:
        return self.liq_hype * self.hype_usd


# HYPE/USD is shared by every Market in the process (front + background): one fetch, one cache.
# Several sources in turn — if api.hyperliquid.xyz doesn't answer from the server, MC used to become 0 and the
# tracker backed off every token (no MC/dip alerts for hours). The last good price is kept (and saved to disk),
# a stale price is far better than none.
_PRICE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".hype_usd")
_USD = {"v": 0.0, "at": -1e9, "tried": -1e9, "src": "", "fails": 0, "err": ""}
_USD_LOCK: asyncio.Lock | None = None

try:
    with open(_PRICE_FILE) as _f:
        _USD["v"], _USD["at"] = float(_f.read().split()[0]), -1e9  # known but "old": refreshed at once
except Exception:
    pass


async def _hl(http):
    r = await http.post(C.HYPE_USD_API, json={"type": "allMids"}, timeout=8)
    r.raise_for_status()
    return float(r.json()["HYPE"])


async def _coingecko(http):
    r = await http.get("https://api.coingecko.com/api/v3/simple/price",
                       params={"ids": "hyperliquid", "vs_currencies": "usd"}, timeout=8)
    r.raise_for_status()
    return float(r.json()["hyperliquid"]["usd"])


async def _okx(http):
    r = await http.get("https://www.okx.com/api/v5/market/ticker", params={"instId": "HYPE-USDT"}, timeout=8)
    r.raise_for_status()
    return float(r.json()["data"][0]["last"])


async def _bybit(http):
    r = await http.get("https://api.bybit.com/v5/market/tickers", params={"category": "spot", "symbol": "HYPEUSDT"},
                       timeout=8)
    r.raise_for_status()
    return float(r.json()["result"]["list"][0]["lastPrice"])


async def _kucoin(http):
    r = await http.get("https://api.kucoin.com/api/v1/market/orderbook/level1", params={"symbol": "HYPE-USDT"},
                       timeout=8)
    r.raise_for_status()
    return float(r.json()["data"]["price"])


_SOURCES = [("hyperliquid", _hl), ("coingecko", _coingecko), ("okx", _okx), ("bybit", _bybit), ("kucoin", _kucoin)]


def usd_status() -> dict:
    return dict(_USD)


async def fetch_hype_usd(http) -> float:
    """Fresh (≤60 s) price if any source answers, else the last good one; 0 only if never known."""
    global _USD_LOCK
    now = time.monotonic()
    if _USD["v"] and now - _USD["at"] < 60:
        return _USD["v"]
    if now - _USD["tried"] < 15:               # all sources just failed: don't hammer them from every check
        return _USD["v"]
    if _USD_LOCK is None:
        _USD_LOCK = asyncio.Lock()
    async with _USD_LOCK:                      # one fetch at a time; the others take its result
        now = time.monotonic()
        if (_USD["v"] and now - _USD["at"] < 60) or now - _USD["tried"] < 15:
            return _USD["v"]
        _USD["tried"] = now
        errs = []
        for name, fn in _SOURCES:
            try:
                v = await fn(http)
                if v > 0:
                    if _USD["fails"]:
                        log.info("HYPE/USD back via %s after %d failed rounds: %.3f", name, _USD["fails"], v)
                    _USD.update(v=v, at=time.monotonic(), src=name, fails=0, err="")
                    try:
                        with open(_PRICE_FILE, "w") as f:
                            f.write(f"{v}")
                    except OSError:
                        pass
                    return v
            except Exception as ex:
                errs.append(f"{name}: {type(ex).__name__} {ex}"[:120])
        _USD["fails"] += 1
        _USD["err"] = "; ".join(errs)
        if _USD["fails"] == 1 or _USD["fails"] % 20 == 0:
            log.warning("HYPE/USD: all sources failed (%d rounds), keeping %.3f — %s",
                        _USD["fails"], _USD["v"], _USD["err"])
        return _USD["v"]


class Market:
    def __init__(self, rpc: Rpc, dex: Dex):
        self.rpc, self.dex = rpc, dex

    async def hype_usd(self) -> float:
        return await fetch_hype_usd(self.rpc.http)

    async def snap(self, token: str, venue: str = "") -> Snap:
        """Raises NoPool if the address isn't a token or has no HYPE pool."""
        tok, hu = await asyncio.gather(self.dex.token(token), self.hype_usd())
        route = await self.dex.route(token, prefer=venue)
        price = route.price(tok.decimals)
        supply = tok.supply / 10 ** tok.decimals if tok.decimals is not None else 0
        liq = (route.liquidity or 0) / 1e18
        return Snap(tok, route, price, price * supply, liq, hu, links.pad_of_route(route))

    async def try_snap(self, token: str, retries: int = 1, venue: str = "") -> Snap | None:
        """None if no pool answers. A busy public node can make one venue's quote fail, so 'no pool' is retried."""
        for i in range(retries + 1):
            try:
                return await self.snap(token, venue)
            except (NoPool, RpcError, IndexError, ValueError) as ex:
                if i == retries:
                    log.info("no pool %s: %s", token, ex)
                    return None
                await asyncio.sleep(1.5)
            except Exception as ex:
                log.info("snap %s: %s", token, ex)
                return None
        return None
