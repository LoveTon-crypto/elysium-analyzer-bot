"""🐳 Big buys: every swap on Elysium (Uniswap v4 PoolManagers — Kleos, ElysPump, Boxa, Montra, ELYXA … — and v3
pools) is checked; buys of at least BIGBUY_HYPE HYPE go to the 'bigbuy' topic.

Swap events are only a cheap pre-filter. The buy itself is confirmed from the transaction: HYPE paid = native HYPE
sent with the tx (or wrapped HYPE the buyer transferred), token = what the buyer received in that tx.
"""
from __future__ import annotations

import asyncio
import logging
import time

import config as C
import links
import scanner
from evm import RpcError
from links import e, usd
from swaps import TRANSFER, V2_SWAP, V3_SWAP, V4_SWAP, TOKEN0, TOKEN1

log = logging.getLogger("bigbuys")

MIN_HYPE = float(C.env("BIGBUY_HYPE", "15"))
POLL = float(C.env("BIGBUY_POLL", "3"))
CANON_WHYPE = "0xba876237b0f021a6c6d852d6c26e107997cc5e81"


def _int(word: str) -> int:
    v = int(word, 16)
    return v - (1 << 256) if v >= 1 << 255 else v


class BigBuys:
    def __init__(self, store, rpc, dex, market, post):
        self.store, self.rpc, self.dex, self.market, self.post = store, rpc, dex, market, post
        self.cursor = 0
        self.whype: set[str] = {CANON_WHYPE}
        self._pools: dict[str, tuple[str, str]] = {}     # v3 pool → (token0, token1)
        self._done: dict[str, float] = {}                # tx hash → seen
        self.sem = asyncio.Semaphore(6)
        self.bot_username = ""
        self.stats = {"posted": 0, "checked": 0, "last": 0.0}
        self.tracker = None       # tracker.Tracker: a big buy puts the token on its hot list
        self.book = None          # tops.VolumeBook: every swap is also booked as volume

    async def run(self, app):
        self.bot_username = app.bot.username
        self.whype |= {v.whype.lower() for v in self.dex.venues if v.whype}
        await asyncio.sleep(8)
        thr = int(MIN_HYPE * 1e18 * 0.9)
        while True:
            try:
                head = int(await self.rpc.req("eth_blockNumber"), 16)
                if not self.cursor or head - self.cursor > 3000:
                    self.cursor = head
                if head > self.cursor:
                    frm, to = self.cursor + 1, min(head, self.cursor + 2000)
                    logs = await self.rpc.req("eth_getLogs", [{"fromBlock": hex(frm), "toBlock": hex(to),
                                                               "topics": [[V4_SWAP, V3_SWAP, V2_SWAP]]}])
                    cands = set()
                    for lg in logs or []:
                        if await self._big(lg, thr):
                            cands.add(lg["transactionHash"])
                    if self.book is not None:
                        await self.book.add(logs or [], self._pools)
                    for h in cands:
                        if h not in self._done:
                            self._done[h] = time.monotonic()
                            asyncio.get_running_loop().create_task(self._check(h))
                    self.cursor = to
                    if len(self._done) > 20000:
                        now = time.monotonic()
                        self._done = {k: v for k, v in self._done.items() if now - v < 3600}
                    if to < head:
                        continue
            except Exception as ex:
                log.warning("bigbuys: %s", ex)
                await asyncio.sleep(3)
            await asyncio.sleep(POLL)

    async def _big(self, lg: dict, thr: int) -> bool:
        """Cheap pre-filter: could this swap have moved ≥ the threshold in HYPE?"""
        d = (lg.get("data") or "")[2:]
        try:
            if lg["topics"][0] == V4_SWAP:     # native HYPE is always currency0 of its pools
                return abs(_int(d[:64])) >= thr
            pool = lg["address"].lower()
            if pool not in self._pools:
                t0, t1 = await asyncio.gather(self.rpc.call(pool, TOKEN0), self.rpc.call(pool, TOKEN1))
                self._pools[pool] = ("0x" + t0[-40:].lower(), "0x" + t1[-40:].lower())
            t0, t1 = self._pools[pool]
            side = 0 if t0 in self.whype else 1 if t1 in self.whype else -1
            if side < 0:
                return False
            if lg["topics"][0] == V2_SWAP:      # amount0In, amount1In, amount0Out, amount1Out
                return int(d[side * 64:(side + 1) * 64], 16) >= thr
            return abs(_int(d[side * 64:(side + 1) * 64])) >= thr
        except (RpcError, IndexError, ValueError, KeyError):
            return False
        return False

    async def _check(self, txh: str):
        async with self.sem:
            try:
                tx, rc = await asyncio.gather(self.rpc.req("eth_getTransactionByHash", [txh]),
                                              self.rpc.req("eth_getTransactionReceipt", [txh]))
                if not tx or not rc or int(rc.get("status", "0x1"), 16) != 1:
                    return
                self.stats["checked"] += 1
                buyer = (tx.get("from") or "").lower()
                paid = int(tx.get("value") or "0x0", 16)
                got: dict[str, int] = {}
                for x in rc.get("logs") or []:
                    t = x.get("topics") or []
                    if len(t) != 3 or t[0] != TRANSFER:
                        continue
                    addr = x["address"].lower()
                    frm, to_a = "0x" + t[1][-40:], "0x" + t[2][-40:]
                    v = int(x.get("data") or "0x0", 16)
                    if addr in self.whype:
                        if frm == buyer:
                            paid += v
                    elif to_a == buyer:
                        got[addr] = got.get(addr, 0) + v
                if paid < MIN_HYPE * 1e18 or not got:
                    return
                token = max(got, key=got.get)   # the coin the buyer received
                await self._alert(token, buyer, paid, got[token], txh)
            except Exception as ex:
                log.info("bigbuy %s: %s", txh, ex)

    async def _alert(self, token: str, buyer: str, paid: int, amount: int, txh: str):
        snap = await self.market.try_snap(token)
        if not snap:
            return
        row = self.store.deploy(token)
        hype = paid / 1e18
        usd_v = hype * snap.hype_usd
        t = snap.token
        held_pct = amount / t.supply * 100 if t.supply else 0
        whales = "🐳" * min(5, max(1, int(hype // MIN_HYPE)))
        head = [f"{whales} <b>BIG BUY {hype:,.2f} HYPE</b>" + (f" ({usd(usd_v)})" if usd_v else ""), ""]
        extra = [f'👤 Buyer <a href="{links.addr_link(buyer)}">{links.short(buyer)}</a> · got {held_pct:.2f}% of supply',
                 f'🔗 <a href="{C.EXPLORER}/tx/{txh}">Transaction</a>']
        text = "\n".join(head + scanner.card_lines(snap, row, extra))
        if self.tracker is not None:
            self.tracker.nudge(token)
        await self.post("bigbuy", text, None, scanner.alert_keyboard(token, self.bot_username, snap.pad or ""))
        self.stats["posted"] += 1
        self.stats["last"] = time.time()
        log.info("BIG BUY %.2f HYPE %s by %s", hype, token, buyer)
