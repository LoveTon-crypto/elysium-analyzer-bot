"""Price history from the token's past swaps on-chain → 15m candles right away, no indexer needed.

The pool is found from the token's own transfers: the receipt of a recent trade holds the pool's Swap event —
Uniswap v4 (PoolManager, shared by Kleos v5, ElysPump, Boxa, Montra …), v3 or v2. Then all Swap events of that
pool are read with eth_getLogs, price = HYPE amount / token amount of each swap.
Pads with their own bonding curves (Obol, ELYXA, Kleos v4) emit other events — there the chart comes from the
prices the bot records itself (extdata.py).
"""
from __future__ import annotations

import asyncio
import logging
import time

import logscan
from evm import RpcError

log = logging.getLogger("swaps")

try:
    from eth_utils import keccak

    def _topic(sig: str) -> str:
        return "0x" + keccak(text=sig).hex().removeprefix("0x")
except Exception:  # pragma: no cover
    keccak = None

    def _topic(sig: str) -> str:
        return {"Swap(bytes32,address,int128,int128,uint160,uint128,int24,uint24)":
                "0x40e9cecb9f5f1f1c5b9c97dec2917b7ee92e57ba5563708daca94dd84ad7112f",
                "Swap(address,address,int256,int256,uint160,uint128,int24)":
                "0xc42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67",
                "Swap(address,uint256,uint256,uint256,uint256,address)":
                "0xd78ad95fa46c994b6551d0da85fc275fe613ce37657fb8d5e3d130840159d822",
                "Transfer(address,address,uint256)":
                "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"}[sig]

V4_SWAP = _topic("Swap(bytes32,address,int128,int128,uint160,uint128,int24,uint24)")
V3_SWAP = _topic("Swap(address,address,int256,int256,uint160,uint128,int24)")
V2_SWAP = _topic("Swap(address,uint256,uint256,uint256,uint256,address)")
TRANSFER = _topic("Transfer(address,address,uint256)")
TOKEN0, TOKEN1 = "0x0dfe1681", "0xd21220a7"
WINDOW = 96 * 900          # 24h of 15m candles


def _int(word: str, signed: bool) -> int:
    v = int(word, 16)
    return v - (1 << 256) if signed and v >= 1 << 255 else v


def _words(data: str) -> list[str]:
    d = data[2:] if data.startswith("0x") else data
    return [d[i:i + 64] for i in range(0, len(d), 64)]


class SwapHistory:
    def __init__(self, rpc):
        self.rpc = rpc
        self._pool: dict[str, tuple | None] = {}   # token → (kind, address, topic1|None, hype_is_0) or None
        self._pts: dict[str, list[tuple[int, float]]] = {}   # token → [(block, price HYPE)]
        self._last: dict[str, int] = {}
        self._bt: tuple | None = None   # (block, ts, seconds per block, monotonic) anchor
        self._locks: dict[str, asyncio.Lock] = {}

    async def _logs(self, flt: dict, frm: int, to: int, max_calls: int = 0) -> list[dict]:
        return await logscan.get_logs(self.rpc, flt, frm, to)

    async def block_time(self, head: int) -> tuple[int, float, float]:
        """(head, its timestamp, seconds per block); the rate is re-measured every 10 minutes."""
        if self._bt and time.monotonic() - self._bt[3] < 600:
            hb, ts, spb, _ = self._bt
            return head, ts + (head - hb) * spb, spb
        b1, b0 = await asyncio.gather(self.rpc.req("eth_getBlockByNumber", [hex(head), False]),
                                      self.rpc.req("eth_getBlockByNumber", [hex(max(1, head - 100_000)), False]))
        t1, t0 = int(b1["timestamp"], 16), int(b0["timestamp"], 16)
        spb = max(0.01, (t1 - t0) / max(1, head - max(1, head - 100_000)))
        self._bt = (head, t1, spb, time.monotonic())
        return head, t1, spb

    async def _find_pool(self, token: str, head: int, spb: float):
        """Pool of the token from the Swap event in the receipt of its latest transfer."""
        span = int(WINDOW * 3 / spb)
        logs, _ = await logscan.scan_back(self.rpc, {"address": token, "topics": [TRANSFER]}, head,
                                          lambda ls: len(ls) >= 3, span)
        v4_ids: dict[tuple[str, str], int] = {}
        for lg in reversed(logs[-8:]):
            try:
                rc = await self.rpc.req("eth_getTransactionReceipt", [lg["transactionHash"]])
            except RpcError:
                continue
            for x in (rc or {}).get("logs") or []:
                t = x.get("topics") or []
                if not t:
                    continue
                if t[0] == V4_SWAP and len(t) >= 2:
                    k = (x["address"].lower(), t[1])
                    v4_ids[k] = v4_ids.get(k, 0) + 1
                elif t[0] in (V3_SWAP, V2_SWAP):
                    pool = x["address"].lower()
                    try:
                        t0, t1 = await asyncio.gather(self.rpc.call(pool, TOKEN0), self.rpc.call(pool, TOKEN1))
                    except RpcError:
                        continue
                    t0, t1 = "0x" + t0[-40:].lower(), "0x" + t1[-40:].lower()
                    if token in (t0, t1):
                        return ("v3" if t[0] == V3_SWAP else "v2", pool, None, t1 == token)
        if v4_ids:   # the pool our token's trades go through most often
            (mgr, pid), _ = max(v4_ids.items(), key=lambda kv: kv[1])
            return ("v4", mgr, pid, True)
        return None

    @staticmethod
    def _price(kind: str, data: str, hype0: bool, dec: int) -> float:
        w = _words(data)
        try:
            if kind == "v4":
                a0, a1 = abs(_int(w[0], True)), abs(_int(w[1], True))
            elif kind == "v3":
                a0, a1 = abs(_int(w[0], True)), abs(_int(w[1], True))
            else:  # v2: amount0In, amount1In, amount0Out, amount1Out
                a0, a1 = int(w[0], 16) + int(w[2], 16), int(w[1], 16) + int(w[3], 16)
        except (IndexError, ValueError):
            return 0.0
        hype, tok = (a0, a1) if hype0 else (a1, a0)
        if not hype or not tok:
            return 0.0
        return (hype / 1e18) / (tok / 10 ** dec)

    async def candles(self, token: str, decimals: int, hype_usd: float) -> list[tuple]:
        """[(ts, o, h, l, c, 0)] in USD for the last 24h; [] if the pool's swaps can't be read."""
        token = token.lower()
        lock = self._locks.setdefault(token, asyncio.Lock())
        async with lock:
            try:
                head = int(await self.rpc.req("eth_blockNumber"), 16)
                hb, hts, spb = await self.block_time(head)
                if token not in self._pool:
                    self._pool[token] = await self._find_pool(token, head, spb)
                pool = self._pool[token]
                if not pool:
                    return []
                kind, addr, id_topic, hype0 = pool
                frm = max(1, self._last.get(token, head - int(WINDOW / spb)) + 1)
                topics = [V4_SWAP, id_topic] if kind == "v4" else [V3_SWAP if kind == "v3" else V2_SWAP]
                logs = await self._logs({"address": addr, "topics": topics}, frm, head, 80)
                pts = self._pts.setdefault(token, [])
                for lg in logs:
                    p = self._price(kind, lg.get("data") or "", hype0, decimals)
                    if p > 0:
                        pts.append((int(lg["blockNumber"], 16), p))
                self._last[token] = head
                keep_from = head - int(WINDOW / spb)
                pts[:] = [x for x in pts if x[0] >= keep_from]
            except Exception as ex:
                log.info("swaps %s: %s", token, ex)
                return []
        if len(pts) < 2 or not hype_usd:
            return []
        buckets: dict[int, list[float]] = {}
        for blk, p in pts:
            ts = hts - (hb - blk) * spb
            buckets.setdefault(int(ts // 900 * 900), []).append(p * hype_usd)
        out, prev = [], None
        start = min(buckets)
        for b in range(start, int(hts // 900 * 900) + 1, 900):   # flat candles where nothing traded
            v = buckets.get(b) or ([prev] if prev is not None else None)
            if not v:
                continue
            o = prev if prev is not None else v[0]
            out.append((float(b), o, max(o, *v), min(o, *v), v[-1], 0.0))
            prev = v[-1]
        return out[-96:]
