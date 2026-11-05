"""Fast eth_getLogs over long block ranges: Elysium nodes allow ~10k blocks per call, so ranges are cut into
chunks fetched in parallel; a chunk the node refuses is split in half and retried."""
from __future__ import annotations

import asyncio
import logging

import config as C
from evm import RpcError

log = logging.getLogger("logscan")
STEP = int(C.env("LOGS_STEP", "10000"))
CONC = int(C.env("LOGS_CONCURRENCY", "8"))


async def _chunk(rpc, flt: dict, a: int, b: int, depth: int = 0) -> list[dict]:
    try:
        return await rpc.req("eth_getLogs", [{**flt, "fromBlock": hex(a), "toBlock": hex(b)}]) or []
    except RpcError as ex:
        if b - a < 200 or depth > 6:
            raise
        mid = (a + b) // 2
        log.debug("split %s-%s: %s", a, b, ex)
        left, right = await asyncio.gather(_chunk(rpc, flt, a, mid, depth + 1), _chunk(rpc, flt, mid + 1, b, depth + 1))
        return left + right


async def get_logs(rpc, flt: dict, frm: int, to: int, step: int = STEP, conc: int = CONC) -> list[dict]:
    """All logs in [frm, to], in block order."""
    if to < frm:
        return []
    ranges = [(a, min(to, a + step - 1)) for a in range(frm, to + 1, step)]
    sem = asyncio.Semaphore(conc)

    async def one(r):
        async with sem:
            return await _chunk(rpc, flt, *r)
    parts = await asyncio.gather(*(one(r) for r in ranges))
    return [lg for p in parts for lg in p]


async def scan_back(rpc, flt: dict, head: int, stop, max_blocks: int, step: int = STEP, conc: int = CONC):
    """Reads logs backwards from head in rounds of `conc` chunks, newest first. After each round calls
    stop(logs_so_far) — True ends the scan. Returns (logs in block order, lowest block reached)."""
    out: list[dict] = []
    hi = head
    low_limit = max(1, head - max_blocks)
    while hi >= low_limit:
        lo = max(low_limit, hi - step * conc + 1)
        got = await get_logs(rpc, flt, lo, hi, step, conc)
        out = got + out
        hi = lo - 1
        if stop(out):
            break
    return out, hi + 1
