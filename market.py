"""Price, market cap and liquidity of an Elysium token, from on-chain quotes (dex.py, same as the trade bot)."""
from __future__ import annotations

import asyncio
import logging
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


class Market:
    def __init__(self, rpc: Rpc, dex: Dex):
        self.rpc, self.dex = rpc, dex
        self._usd = (0.0, 0.0)

    async def hype_usd(self) -> float:
        if self._usd[0] and time.monotonic() - self._usd[1] < 60:
            return self._usd[0]
        try:
            r = await self.rpc.http.post(C.HYPE_USD_API, json={"type": "allMids"}, timeout=8)
            self._usd = (float(r.json()["HYPE"]), time.monotonic())
        except Exception as ex:
            log.warning("HYPE/USD: %s", ex)
        return self._usd[0]

    async def snap(self, token: str) -> Snap:
        """Raises NoPool if the address isn't a token or has no HYPE pool."""
        tok, hu = await asyncio.gather(self.dex.token(token), self.hype_usd())
        route = await self.dex.route(token)
        price = route.price(tok.decimals)
        supply = tok.supply / 10 ** tok.decimals if tok.decimals is not None else 0
        liq = (route.liquidity or 0) / 1e18
        return Snap(tok, route, price, price * supply, liq, hu, links.pad_of(route.venue))

    async def try_snap(self, token: str, retries: int = 1) -> Snap | None:
        """None if no pool answers. A busy public node can make one venue's quote fail, so 'no pool' is retried."""
        for i in range(retries + 1):
            try:
                return await self.snap(token)
            except (NoPool, RpcError, IndexError, ValueError) as ex:
                if i == retries:
                    log.info("no pool %s: %s", token, ex)
                    return None
                await asyncio.sleep(1.5)
        except Exception as ex:
            log.info("snap %s: %s", token, ex)
            return None
