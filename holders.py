"""Token holders: explorer API (Blockscout-style) first, otherwise counted on-chain from the token's
Transfer logs since its launch block. Contracts (pools, pad curves, PoolManagers) are marked 🏊."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

import config as C
import logscan
from evm import RpcError

log = logging.getLogger("holders")
TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
ZERO = "0x" + "0" * 40
DEAD = {ZERO, "0x000000000000000000000000000000000000dead"}
MAX_BLOCKS = int(C.env("HOLDERS_MAX_BLOCKS", "6000000"))   # how far back to look for an old token's launch


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
            return await asyncio.wait_for(self._onchain(token, supply), 90)
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

    async def _onchain(self, token: str, supply: int) -> Holders:
        """First call: all Transfer logs of the token since launch (from the launch block if the bot saw it,
        else read backwards until the mint of the whole supply). Later calls only read the new blocks."""
        lock = self._locks.setdefault(token, asyncio.Lock())
        async with lock:
            head = int(await self.rpc.req("eth_blockNumber"), 16)
            st = self._state.get(token)
            flt = {"address": token, "topics": [TRANSFER]}
            if not st:
                row = self.store.deploy(token)
                if row and row.get("block"):
                    logs = await logscan.get_logs(self.rpc, flt, int(row["block"]), head)
                    partial = False
                else:
                    zero_topic = "0x" + "0" * 64

                    def minted_all(ls):
                        m = sum(int(x.get("data") or "0x0", 16) for x in ls
                                if len(x.get("topics") or []) == 3 and x["topics"][1] == zero_topic)
                        return supply and m >= supply * 0.999
                    logs, _ = await logscan.scan_back(self.rpc, flt, head, minted_all, MAX_BLOCKS)
                    partial = not minted_all(logs)
                st = {"block": head, "bal": {}, "partial": partial}
                self._apply(st["bal"], logs)
                self._state[token] = st
            elif st["block"] < head:
                self._apply(st["bal"], await logscan.get_logs(self.rpc, flt, st["block"] + 1, head))
                st["block"] = head
            live = {a: v for a, v in st["bal"].items() if v > 0 and a not in DEAD}
            ranked = sorted(live.items(), key=lambda x: -x[1])[:12]
            flags = await asyncio.gather(*(self._is_contract(a) for a, _ in ranked))
            top = [(a, v / supply * 100 if supply else 0, c) for (a, v), c in zip(ranked, flags)]
            return Holders(len(live), top, st["partial"], "on-chain")

    @staticmethod
    def _apply(bal: dict, logs: list[dict]):
        for lg in logs:
            t = lg.get("topics") or []
            if len(t) != 3:
                continue
            f, to_a = "0x" + t[1][-40:], "0x" + t[2][-40:]
            v = int(lg.get("data") or "0x0", 16)
            bal[f] = bal.get(f, 0) - v
            bal[to_a] = bal.get(to_a, 0) + v

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

