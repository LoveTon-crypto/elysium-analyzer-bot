"""Token holders: explorer API (Blockscout-style) first, otherwise counted on-chain from the token's
Transfer logs since its launch block. Contracts (pools, pad curves, PoolManagers) are marked 🏊."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

import config as C
from evm import RpcError

log = logging.getLogger("holders")
TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
ZERO = "0x" + "0" * 40
DEAD = {ZERO, "0x000000000000000000000000000000000000dead"}
MAX_CHUNKS = int(C.env("HOLDERS_MAX_CHUNKS", "60"))     # getLogs calls per scan (first scan of an old token)


@dataclass
class Holders:
    total: int = 0
    top: list = field(default_factory=list)   # [(address, pct, is_contract)]
    partial: bool = False                     # on-chain count didn't reach the launch block
    source: str = ""

    @property
    def top10(self) -> float:
        return sum(p for _, p, c in self.top if not c)


class HolderScanner:
    def __init__(self, rpc, store, http):
        self.rpc, self.store, self.http = rpc, store, http
        self._state: dict[str, dict] = {}       # token → {"block", "bal", "start", "partial"}
        self._code: dict[str, bool] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._api_ok = True

    async def get(self, token: str, supply: int) -> Holders:
        token = token.lower()
        h = await self._api(token, supply) if C.EXPLORER_API and self._api_ok else None
        if h and h.top:
            return h
        try:
            return await asyncio.wait_for(self._onchain(token, supply), 25)
        except Exception as ex:
            log.info("holders on-chain %s: %s", token, ex)
            return Holders()

    async def _api(self, token: str, supply: int) -> Holders | None:
        try:
            r1, r2 = await asyncio.gather(self.http.get(f"{C.EXPLORER_API}/tokens/{token}/counters", timeout=6),
                                          self.http.get(f"{C.EXPLORER_API}/tokens/{token}/holders", timeout=6))
            if r2.status_code == 404 and r1.status_code == 404:
                return None
            items = (r2.json().get("items") or []) if r2.status_code < 400 else []
            total = int((r1.json() if r1.status_code < 400 else {}).get("token_holders_count") or len(items))
            top = []
            for it in items:
                a = ((it.get("address") or {}).get("hash") or "").lower()
                v = int(it.get("value") or 0)
                if a and a not in DEAD and supply:
                    top.append((a, v / supply * 100, bool((it.get("address") or {}).get("is_contract"))))
            return Holders(total, top[:12], False, "explorer")
        except Exception as ex:
            log.info("holders api %s: %s", token, ex)
            return None

    async def _start_block(self, token: str, head: int) -> tuple[int, bool]:
        row = self.store.deploy(token)
        if row and row.get("block"):
            return int(row["block"]), False
        # find the creation block by binary search on eth_getCode (needs historical state on the node)
        lo, hi = max(1, head - 5_000_000), head
        try:
            if (await self.rpc.req("eth_getCode", [token, hex(lo)])) not in ("0x", "0x0", None):
                return lo, True
            while hi - lo > 1:
                mid = (lo + hi) // 2
                code = await self.rpc.req("eth_getCode", [token, hex(mid)])
                if code and code not in ("0x", "0x0"):
                    hi = mid
                else:
                    lo = mid
            return hi, False
        except RpcError:
            return max(1, head - MAX_CHUNKS * 10_000), True

    async def _onchain(self, token: str, supply: int) -> Holders:
        lock = self._locks.setdefault(token, asyncio.Lock())
        async with lock:
            head = int(await self.rpc.req("eth_blockNumber"), 16)
            st = self._state.get(token)
            if not st:
                start, partial = await self._start_block(token, head)
                st = {"block": start - 1, "bal": {}, "partial": partial}
                self._state[token] = st
            step, calls = 50_000, 0
            while st["block"] < head and calls < MAX_CHUNKS:
                frm, to = st["block"] + 1, min(head, st["block"] + step)
                try:
                    logs = await self.rpc.req("eth_getLogs", [{"address": token, "fromBlock": hex(frm),
                                                               "toBlock": hex(to), "topics": [TRANSFER]}])
                except RpcError as ex:
                    if step > 1000:
                        step //= 4
                        continue
                    raise ex
                calls += 1
                bal = st["bal"]
                for lg in logs or []:
                    t = lg.get("topics") or []
                    if len(t) != 3:
                        continue
                    f, to_a = "0x" + t[1][-40:], "0x" + t[2][-40:]
                    v = int(lg.get("data") or "0x0", 16)
                    bal[f] = bal.get(f, 0) - v
                    bal[to_a] = bal.get(to_a, 0) + v
                st["block"] = to
            if st["block"] < head:
                st["partial"] = True
                st["block"] = head  # skip what's left: count from here on
            live = {a: v for a, v in st["bal"].items() if v > 0 and a not in DEAD}
            ranked = sorted(live.items(), key=lambda x: -x[1])[:12]
            flags = await asyncio.gather(*(self._is_contract(a) for a, _ in ranked))
            top = [(a, v / supply * 100 if supply else 0, c) for (a, v), c in zip(ranked, flags)]
            return Holders(len(live), top, st["partial"], "on-chain")

    async def _is_contract(self, a: str) -> bool:
        if a not in self._code:
            try:
                code = await self.rpc.code(a)
                self._code[a] = bool(code and code not in ("0x", "0x0"))
            except RpcError:
                return False
        return self._code[a]


def holder_lines(h: Holders, link, dev: str = "") -> list[str]:
    """NEAR-analyzer style block: '👥 N holders · TOP-10: x%' + two holders per line + pools."""
    if not h.top:
        return ["👥 Holders: no data"]
    n = f"{h.total}+" if h.partial else str(h.total)
    out = [f"👥 <b>{n}</b> holders · TOP-10: <b>{h.top10:.1f}%</b>"]
    people = [(a, p) for a, p, c in h.top if not c][:10]
    cells = [f'{i}. {"🛠" if dev and a == dev else "👤"} <a href="{link(a)}">{p:.2f}%</a>'
             for i, (a, p) in enumerate(people, 1)]
    for i in range(0, len(cells), 2):
        out.append("   ".join(cells[i:i + 2]))
    pools = [(a, p) for a, p, c in h.top if c][:3]
    if pools:
        out.append("🏊 Contracts: " + " · ".join(f'<a href="{link(a)}">{a[:6]}…{a[-4:]}</a> {p:.2f}%' for a, p in pools))
    return out

